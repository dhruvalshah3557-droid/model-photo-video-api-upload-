import json
import logging
import os

import requests

from media_prep import prepare_video

logger = logging.getLogger(__name__)

LINKEDIN_API = "https://api.linkedin.com/v2"
LINKEDIN_UPLOAD_API = "https://api.linkedin.com/rest"

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


def _is_video_url(url):
    return any(ext in url.lower() for ext in _VIDEO_EXTS)


class LinkedInUploader:
    """Publish image / video / carousel posts via the LinkedIn API v2.

    Images and videos are registered as assets, uploaded to the returned
    uploadUrl, then referenced in a UGC post (POST /v2/ugcPosts). The author
    is either the authenticated person (`urn:li:person:{id}`) or an
    organization (`urn:li:organization:{id}`) when the account's
    `platform_account_id` is an org id / org URN. Credentials come from the
    account's `credential_property_key` env var (the access token).
    """

    def __init__(self, access_token="", author="", account_name=""):
        self.access_token = access_token or _env("LINKEDIN_ACCESS_TOKEN")
        self.author = author or _env("LINKEDIN_AUTHOR")
        self.account_name = account_name
        if not self.access_token:
            raise Exception("LinkedIn access token missing: set LINKEDIN_ACCESS_TOKEN")

    def _headers(self, rest=False):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "LinkedIn-Version": "202401",
        }
        if rest:
            headers["Content-Type"] = "application/json"
        return headers

    def _resolve_author(self):
        if self.author:
            if self.author.startswith("urn:li:"):
                return self.author
            # A bare numeric value is treated as a person id.
            return f"urn:li:person:{self.author}"
        try:
            resp = requests.get(
                f"{LINKEDIN_API}/userinfo",
                headers=self._headers(),
                timeout=30,
            )
            data = resp.json()
            sub = data.get("sub")
            if sub:
                return f"urn:li:person:{sub}"
        except Exception as e:
            logger.warning(f"Could not resolve LinkedIn author via userinfo: {e}")
        raise Exception("LinkedIn author unknown: set LINKEDIN_AUTHOR or platform_account_id")

    # ------------------------------------------------------------------
    # Image asset
    # ------------------------------------------------------------------
    def _register_image_asset(self, author_urn):
        body = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": author_urn,
                "serviceRelationships": [
                    {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                ],
            }
        }
        resp = requests.post(
            f"{LINKEDIN_UPLOAD_API}/assets?action=registerUpload",
            headers=self._headers(rest=True),
            data=json.dumps(body),
            timeout=60,
        )
        data = resp.json()
        value = data.get("value", {})
        upload_url = value.get("uploadMechanism", {}).get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}).get("uploadUrl")
        asset = value.get("asset")
        if not upload_url or not asset:
            raise Exception(f"LinkedIn image register error: {data}")
        return upload_url, asset

    def _upload_bytes(self, upload_url, content, content_type):
        resp = requests.post(
            upload_url,
            data=content,
            headers={"Content-Type": content_type},
            timeout=300,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"LinkedIn media upload error {resp.status_code}: {resp.text[:500]}")

    def _add_image_asset(self, media_url, author_urn):
        resp = requests.get(
            media_url,
            timeout=180,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        upload_url, asset = self._register_image_asset(author_urn)
        self._upload_bytes(upload_url, content, content_type)
        logger.info(f"[{self.account_name}] LinkedIn image asset ready: {asset}")
        return asset

    # ------------------------------------------------------------------
    # Video asset
    # ------------------------------------------------------------------
    def _register_video_asset(self, author_urn, file_size_bytes):
        body = {
            "initializeUploadRequest": {
                "owner": author_urn,
                "fileSizeBytes": file_size_bytes,
                "uploadIntent": "POST",
            }
        }
        resp = requests.post(
            f"{LINKEDIN_UPLOAD_API}/videos?action=initializeUpload",
            headers=self._headers(rest=True),
            data=json.dumps(body),
            timeout=60,
        )
        data = resp.json()
        value = data.get("value", {})
        upload_url = value.get("uploadUrl")
        video_urn = value.get("video")
        if not upload_url or not video_urn:
            raise Exception(f"LinkedIn video register error: {data}")
        return upload_url, video_urn

    def _add_video_asset(self, media_url, author_urn):
        name, content, content_type = prepare_video(media_url)
        upload_url, video_urn = self._register_video_asset(author_urn, len(content))
        self._upload_bytes(upload_url, content, content_type)
        logger.info(f"[{self.account_name}] LinkedIn video asset ready: {video_urn}")
        return video_urn

    # ------------------------------------------------------------------
    # UGC post
    # ------------------------------------------------------------------
    def _create_ugc_post(self, author_urn, caption, category, assets):
        body = {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": (caption or "")[:3000]},
                    "shareMediaCategory": category,
                    "media": [
                        {
                            "status": "READY",
                            "description": {"text": (caption or "")[:200]},
                            "media": asset,
                        }
                        for asset in assets
                    ],
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        resp = requests.post(
            f"{LINKEDIN_API}/ugcPosts",
            headers=self._headers(rest=True),
            data=json.dumps(body),
            timeout=60,
        )
        data = resp.json()
        post_id = data.get("id")
        if not post_id:
            raise Exception(f"LinkedIn UGC post error: {data}")
        logger.info(f"[{self.account_name}] LinkedIn post created: {post_id}")
        return str(post_id)

    def upload(self, media_url, caption="", title="", is_video=None):
        """Publish a single image or video post."""
        if is_video is None:
            is_video = _is_video_url(media_url)
        author_urn = self._resolve_author()
        if is_video:
            asset = self._add_video_asset(media_url, author_urn)
            post_id = self._create_ugc_post(author_urn, caption, "VIDEO", [asset])
        else:
            asset = self._add_image_asset(media_url, author_urn)
            post_id = self._create_ugc_post(author_urn, caption, "IMAGE", [asset])
        return {"id": post_id, "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}"}

    def upload_carousel(self, image_urls, caption="", product_id=""):
        """Publish up to 9 images in a single multi-image post."""
        author_urn = self._resolve_author()
        assets = [self._add_image_asset(u, author_urn) for u in image_urls[:9]]
        post_id = self._create_ugc_post(author_urn, caption, "IMAGE", assets)
        return {"id": post_id, "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}"}
