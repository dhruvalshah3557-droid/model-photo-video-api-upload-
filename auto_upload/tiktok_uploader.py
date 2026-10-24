"""TikTok video/photo publishing through the connected Zernio account."""
import os
import uuid

import requests

BASE = "https://zernio.com/api/v1"


class TikTokDeliveryUncertain(Exception):
    """A post may exist; do not automatically submit it again."""


class TikTokUploader:
    def __init__(self, access_token="", account_name="", account_id="", job_id=""):
        self.token = access_token or os.getenv("ZERNIO_API_KEY", "")
        self.account_id = account_id
        self.job_id = job_id
        if not self.token or not self.account_id:
            raise ValueError("TikTok requires ZERNIO_API_KEY and a Zernio account ID")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def creator_info(self, media_type):
        response = requests.get(
            f"{BASE}/accounts/{self.account_id}/tiktok/creator-info",
            headers=self._headers(), params={"mediaType": media_type}, timeout=30,
        )
        response.raise_for_status()
        info = response.json()
        levels = [p.get("value") if isinstance(p, dict) else p for p in info.get("privacyLevels", [])]
        if "PUBLIC_TO_EVERYONE" not in levels:
            raise ValueError("TikTok public posting is unavailable for this account")
        if info.get("creator", {}).get("canPostMore") is False:
            raise ValueError("TikTok creator posting limit reached")
        return info

    def _publish(self, urls, caption, media_type):
        if not urls or any(not str(url).startswith("https://") for url in urls):
            raise ValueError("TikTok requires publicly accessible HTTPS media URLs")
        if media_type == "photo" and len(urls) > 35:
            raise ValueError("TikTok photo posts allow at most 35 images")
        info = self.creator_info(media_type)
        settings = {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": False,
            "content_preview_confirmed": True,
            "express_consent_given": True,
            "commercialContentType": "brand_organic",
        }
        # This account publishes its own product catalogue. Interactions are off;
        # the operator authorized public automated publishing of the sheet queue.
        if media_type == "photo":
            settings.update(media_type="photo", photo_cover_index=0,
                            description=caption[:4000], auto_add_music=True)
        else:
            settings.update(allow_duet=False, allow_stitch=False)
        body = {
            "content": caption[:90] if media_type == "photo" else caption[:2200],
            "mediaItems": [{"type": "image" if media_type == "photo" else "video", "url": u} for u in urls],
            "platforms": [{"platform": "tiktok", "accountId": self.account_id}],
            "tiktokSettings": settings,
            "publishNow": True,
        }
        headers = self._headers()
        headers["x-request-id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, self.account_id + ":" + self.job_id)) if self.job_id else str(uuid.uuid4())
        try:
            response = requests.post(f"{BASE}/posts", headers=headers, json=body, timeout=180)
            if response.status_code >= 500:
                raise TikTokDeliveryUncertain(f"Zernio HTTP {response.status_code}; check dashboard before retry")
            if response.status_code == 409:
                raise TikTokDeliveryUncertain("Zernio reports duplicate content; reconcile existing post")
            response.raise_for_status()
            data = response.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TikTokDeliveryUncertain("Zernio request interrupted; check dashboard before retry") from exc
        except ValueError as exc:
            raise TikTokDeliveryUncertain("Zernio returned an unreadable response") from exc
        post = data.get("post") or data.get("existingPost") or {}
        targets = [p for p in post.get("platforms", []) if p.get("platform") == "tiktok"]
        if not targets or targets[0].get("status") != "published":
            raise TikTokDeliveryUncertain(f"Zernio post {post.get('_id', 'unknown')} not confirmed published; check dashboard")
        return {"id": post.get("_id", ""), "url": targets[0].get("platformPostUrl") or ""}

    def upload(self, media_url, title="", description="", is_video=None):
        return self._publish([media_url], description or title, "video")

    def upload_carousel(self, image_urls, caption="", product_id=""):
        return self._publish(image_urls, caption, "photo")
