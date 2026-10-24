import json
import logging

import requests

from media_prep import _download_bytes, prepare_video, video_layout_from_bytes

logger = logging.getLogger(__name__)

FB_GRAPH_URL = "https://graph.facebook.com/v26.0"

_PAGE_TOKEN_CACHE = {}


class FacebookUploader:
    def __init__(self, page_id, page_token, page_name=""):
        self.page_id = page_id
        self.access_token = self._resolve_page_token(page_id, page_token)
        self.page_name = page_name

    @staticmethod
    def _download_media(media_url):
        """Download original media bytes for image uploads."""
        return _download_bytes(media_url)

    @classmethod
    def _resolve_page_token(cls, page_id, token):
        """Resolve a page-scoped token from a user token."""
        cached = _PAGE_TOKEN_CACHE.get(page_id)
        if cached:
            return cached
        try:
            resp = requests.get(
                f"{FB_GRAPH_URL}/{page_id}",
                params={"fields": "access_token", "access_token": token},
                timeout=15,
            )
            data = resp.json()
            resolved = data.get("access_token")
            if resolved:
                _PAGE_TOKEN_CACHE[page_id] = resolved
                return resolved
        except Exception:
            pass
        return token

    def upload_photo(self, media_url, caption, product_id=""):
        url = f"{FB_GRAPH_URL}/{self.page_id}/photos"
        data = {"caption": caption, "access_token": self.access_token}
        if product_id:
            data["product_tags"] = json.dumps([{"product_id": product_id}])
        logger.info(f"[{self.page_name}] Posting photo" + (" with product tag" if product_id else ""))
        resp = requests.post(url, data=data, files={"source": self._download_media(media_url)}, timeout=120)
        result = resp.json()
        if "id" in result:
            logger.info(f"[{self.page_name}] Photo posted: {result['id']}")
            return result
        logger.error(f"[{self.page_name}] Photo failed: {result}")
        raise Exception(result.get("error", {}).get("message", str(result)))

    @staticmethod
    def _json_or_error(response):
        try:
            return response.json()
        except Exception:
            return {
                "error": {
                    "message": (
                        f"Facebook API returned HTTP {response.status_code}: "
                        f"{response.text[:500]}"
                    )
                }
            }

    def upload_video(self, media_url, caption, product_id=""):
        """Publish original video bytes. Vertical uses Reels; landscape/square use Page video."""
        logger.info(
            f"[{self.page_name}] Preparing Facebook video with original size and sound"
        )
        name, content, content_type = prepare_video(
            media_url,
            fill_9x16=True,
            selection_key=f"facebook|{self.page_id}|{media_url}",
            platform="facebook",
        )
        layout = video_layout_from_bytes(content)
        if layout != "vertical":
            logger.info(
                f"[{self.page_name}] Publishing original {layout} video as a Page video"
            )
            return self._upload_page_video(name, content, content_type, caption, product_id)

        logger.info(f"[{self.page_name}] Publishing original vertical video as a Reel")
        reels_url = f"{FB_GRAPH_URL}/{self.page_id}/video_reels"
        start = requests.post(
            reels_url,
            data={
                "upload_phase": "start",
                "access_token": self.access_token,
            },
            timeout=60,
        )
        session = self._json_or_error(start)
        video_id = str(session.get("video_id") or session.get("id") or "").strip()
        upload_url = str(session.get("upload_url") or "").strip()
        if not video_id or not upload_url:
            logger.error(f"[{self.page_name}] Reel session failed: {session}")
            raise Exception(session.get("error", {}).get("message", str(session)))

        upload = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {self.access_token}",
                "Content-Type": content_type or "video/mp4",
                "file_size": str(len(content)),
                "offset": "0",
                "file_name": name,
            },
            data=content,
            timeout=600,
        )
        upload_result = self._json_or_error(upload)
        if not upload.ok or not (
            upload_result.get("success")
            or upload_result.get("video_id")
            or upload.status_code < 300
        ):
            logger.error(f"[{self.page_name}] Reel byte upload failed: {upload_result}")
            raise Exception(
                upload_result.get("error", {}).get("message", str(upload_result))
            )

        finish_data = {
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": self.access_token,
        }
        # The Page Reels endpoint does not consistently support product_tags.
        # Keep the verified website link in the caption and never fall back to a
        # standard video post merely to attach a product tag.
        if product_id:
            logger.info(
                f"[{self.page_name}] Product {product_id} retained in Reel caption; "
                "native product tag is not supported by this publishing flow"
            )
        finish = requests.post(reels_url, data=finish_data, timeout=120)
        result = self._json_or_error(finish)
        if finish.ok and result.get("success"):
            result.setdefault("id", video_id)
            logger.info(f"[{self.page_name}] Facebook Reel published: {video_id}")
            return result
        logger.error(f"[{self.page_name}] Reel publish failed: {result}")
        raise Exception(result.get("error", {}).get("message", str(result)))

    def _upload_page_video(self, name, content, content_type, caption, product_id=""):
        """Keep landscape/square videos at original size on the Page /videos endpoint."""
        data = {
            "description": caption,
            "access_token": self.access_token,
        }
        if product_id:
            data["product_tags"] = json.dumps([{"product_id": product_id}])
        resp = requests.post(
            f"{FB_GRAPH_URL}/{self.page_id}/videos",
            data=data,
            files={"source": (name, content, content_type or "video/mp4")},
            timeout=600,
        )
        result = self._json_or_error(resp)
        if "id" in result:
            logger.info(f"[{self.page_name}] Facebook video posted: {result['id']}")
            return result
        logger.error(f"[{self.page_name}] Page video failed: {result}")
        raise Exception(result.get("error", {}).get("message", str(result)))

    def upload_carousel(self, image_urls, caption, product_id=""):
        if not image_urls:
            raise Exception("No images provided for carousel")
        photos_url = f"{FB_GRAPH_URL}/{self.page_id}/photos"
        child_ids = []
        for image_url in image_urls:
            params = {"published": "false", "access_token": self.access_token}
            resp = requests.post(photos_url, data=params, files={"source": self._download_media(image_url)}, timeout=120)
            result = resp.json()
            if "id" not in result:
                raise Exception(result.get("error", {}).get("message", str(result)))
            child_ids.append({"media_fbid": result["id"]})

        feed_url = f"{FB_GRAPH_URL}/{self.page_id}/feed"
        params = {
            "access_token": self.access_token,
            "message": caption,
            "attached_media": json.dumps(child_ids),
        }
        if product_id:
            params["product_tags"] = json.dumps([{"product_id": product_id}])
        logger.info(f"[{self.page_name}] Publishing carousel with {len(child_ids)} image(s)")
        resp = requests.post(feed_url, data=params, timeout=60)
        result = resp.json()
        if "id" in result:
            logger.info(f"[{self.page_name}] Carousel posted: {result['id']}")
            return result
        logger.error(f"[{self.page_name}] Carousel failed: {result}")
        raise Exception(result.get("error", {}).get("message", str(result)))

    def upload(self, media_url, caption, product_id=""):
        is_video = any(ext in media_url.lower() for ext in [".mp4", ".mov", ".avi", ".mkv", ".webm"])
        if is_video:
            return self.upload_video(media_url, caption, product_id)
        return self.upload_photo(media_url, caption, product_id)

    @staticmethod
    def _absolute_permalink(value):
        """Normalize Meta permalink_url into a full public Facebook URL."""
        value = str(value or "").strip()
        if not value:
            return ""
        if value.startswith("https://") or value.startswith("http://"):
            return value
        if value.startswith("/"):
            return f"https://www.facebook.com{value}"
        return f"https://www.facebook.com/{value.lstrip('/')}"

    def permalink_url(self, object_id):
        """Resolve the canonical public URL for a created object."""
        if not object_id:
            return ""
        try:
            resp = requests.get(
                f"{FB_GRAPH_URL}/{object_id}",
                params={"fields": "permalink_url", "access_token": self.access_token},
                timeout=15,
            )
            data = resp.json()
            return self._absolute_permalink(data.get("permalink_url", ""))
        except Exception:
            return ""
