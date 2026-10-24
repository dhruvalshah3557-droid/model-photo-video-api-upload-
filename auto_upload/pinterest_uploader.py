import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

# Pinterest API v5. Production base by default; override with PINTEREST_API_BASE
# for testing against the sandbox.
PIN_BASE = os.getenv("PINTEREST_API_BASE", "https://api.pinterest.com/v5")

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


def _is_video_url(url):
    return any(ext in url.lower() for ext in _VIDEO_EXTS)


class PinterestUploader:
    """Publish image / video / carousel pins via the Pinterest API v5.

    The account's `platform_account_id` should hold the target board ID; if
    empty, the first board on the account is used. `credential_property_key`
    should point to an env var holding the Pinterest access token (defaults to
    the shared `PINTEREST_ACCESS_TOKEN`).
    """

    def __init__(self, access_token="", board_id="", account_name=""):
        self.access_token = access_token or _env("PINTEREST_ACCESS_TOKEN")
        self.board_id = board_id
        self.account_name = account_name
        if not self.access_token:
            raise Exception(
                "Pinterest access token missing: set PINTEREST_ACCESS_TOKEN"
            )

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _resolve_board_id(self):
        if self.board_id:
            return self.board_id
        resp = requests.get(
            f"{PIN_BASE}/boards",
            headers=self._headers(),
            params={"page_size": 25},
            timeout=30,
        )
        if resp.status_code != 200:
            raise Exception(f"Pinterest boards error {resp.status_code}: {resp.text[:500]}")
        items = resp.json().get("items", [])
        if not items:
            raise Exception("Pinterest account has no boards; set platform_account_id to a board ID")
        board = items[0]
        logger.info(f"[{self.account_name}] Using Pinterest board '{board.get('name')}' ({board.get('id')})")
        return board["id"]

    def _create_pin(self, payload):
        board_id = self._resolve_board_id()
        body = {"board_id": board_id, **payload}
        resp = requests.post(
            f"{PIN_BASE}/pins",
            headers=self._headers(),
            data=json.dumps(body),
            timeout=120,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"Pinterest pin error {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def upload(self, media_url, caption="", title="", link="", thumbnail_url="", is_video=None):
        """Create a single image or video pin on the account board."""
        if is_video is None:
            is_video = _is_video_url(media_url)
        payload = {"description": (caption or "")[:500], "title": (title or "")[:100]}
        if is_video:
            payload["media_source"] = {
                "source_type": "video_url",
                "url": media_url,
                "cover_image_url": thumbnail_url or media_url,
            }
            if link:
                payload["link"] = link
        else:
            payload["media_source"] = {"source_type": "image_url", "url": media_url}
            if link:
                payload["link"] = link
        result = self._create_pin(payload)
        pin_id = result.get("id", "")
        url = result.get("url") or f"https://www.pinterest.com/pin/{pin_id}/"
        logger.info(f"[{self.account_name}] Pinterest pin created: {pin_id}")
        return {"id": pin_id, "url": url}

    def upload_carousel(self, image_urls, caption="", title="", link="", product_id=""):
        """Create a multi-image pin (up to 5 images)."""
        items = [{"url": u} for u in image_urls[:5]]
        if not items:
            raise Exception("No images provided for Pinterest carousel")
        payload = {
            "description": (caption or "")[:500],
            "title": (title or "")[:100],
            "media_source": {"source_type": "multiple_image_urls", "items": items},
        }
        if link:
            payload["link"] = link
        result = self._create_pin(payload)
        pin_id = result.get("id", "")
        url = result.get("url") or f"https://www.pinterest.com/pin/{pin_id}/"
        logger.info(f"[{self.account_name}] Pinterest carousel pin created: {pin_id}")
        return {"id": pin_id, "url": url}
