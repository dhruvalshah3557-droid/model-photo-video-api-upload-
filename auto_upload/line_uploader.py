import logging
import time
import uuid

import requests

logger = logging.getLogger(__name__)

LINE_BOT_API = "https://api.line.me/v2/bot/message/broadcast"
LINE_TEXT_LIMIT = 5000

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def _is_video_url(url):
    return any(ext in str(url).lower() for ext in _VIDEO_EXTS)


def _is_https(url):
    return str(url or "").strip().lower().startswith("https://")


def _is_image_url(url):
    lowered = str(url or "").split("?", 1)[0].lower()
    return any(lowered.endswith(ext) for ext in _IMAGE_EXTS)


class LineUploader:
    """Publish content to LINE Official Account followers via the Messaging API.

    LINE has no native timeline/carousel post; content is delivered as a
    broadcast message to all followers. A "carousel" is sent as a caption text
    plus up to five image messages per broadcast request. Media URLs must be
    publicly reachable HTTPS URLs (LINE fetches them itself).
    """

    _MAX_MESSAGES = 5

    def __init__(self, channel_token, account_name=""):
        self.channel_token = channel_token
        self.account_name = account_name

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.channel_token}",
            "X-Line-Retry-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _broadcast(self, messages):
        if not messages:
            raise Exception("No messages to broadcast")
        headers = self._headers()
        resp = requests.post(
            LINE_BOT_API,
            headers=headers,
            json={"messages": messages},
            timeout=60,
        )
        if resp.status_code != 200:
            body = resp.text[:2000]
            raise Exception(f"LINE API error {resp.status_code}: {body}")
        try:
            result = resp.json() or {}
        except ValueError:
            result = {}

        # LINE broadcast commonly returns an empty JSON object on HTTP 200.
        # That is a confirmed accepted broadcast, not a failure. Preserve a
        # traceable ID using LINE's response header or our valid retry UUID.
        sent = result.get("sentMessages") or []
        if not sent:
            request_id = (
                str(getattr(resp, "headers", {}).get("x-line-request-id", "") or "").strip()
                or headers["X-Line-Retry-Key"]
            )
            sent = [{"id": request_id}]
            result["sentMessages"] = sent
        logger.info(f"[{self.account_name}] LINE broadcast accepted {len(sent)} message(s)")
        return result

    def _text_message(self, caption):
        text = str(caption or "").strip()[:LINE_TEXT_LIMIT]
        if not text:
            return None
        return {"type": "text", "text": text}

    def _image_message(self, media_url):
        return {
            "type": "image",
            "originalContentUrl": media_url,
            "previewImageUrl": media_url,
        }

    def _video_message(self, media_url, thumbnail_url=""):
        preview = thumbnail_url if _is_https(thumbnail_url) and _is_image_url(thumbnail_url) else ""
        if not preview:
            raise Exception("LINE video requires a public HTTPS JPEG/PNG preview image")
        return {
            "type": "video",
            "originalContentUrl": media_url,
            "previewImageUrl": preview,
        }

    def _https_urls(self, urls):
        out = []
        for url in urls:
            value = str(url or "").strip()
            if _is_https(value):
                out.append(value)
        return out

    def upload(self, media_url, caption="", is_video=None, thumbnail_url=""):
        """Send caption text plus a single image or video to all followers."""
        if not _is_https(media_url):
            raise Exception("LINE media URL must be a public HTTPS URL")
        if is_video is None:
            is_video = _is_video_url(media_url)
        messages = []
        text = self._text_message(caption)
        if text:
            messages.append(text)
        messages.append(
            self._video_message(media_url, thumbnail_url)
            if is_video
            else self._image_message(media_url)
        )
        result = self._broadcast(messages)
        message_id = result["sentMessages"][0].get("id", "")
        return {"id": message_id, "url": "https://line.me/"}

    def upload_carousel(self, image_urls, caption="", product_id=""):
        """Send caption text plus HTTPS images, batched at 5 messages."""
        messages = []
        text = self._text_message(caption)
        if text:
            messages.append(text)
        images = self._https_urls(image_urls)
        if not images:
            raise Exception("No public HTTPS image URLs for LINE carousel")
        messages.extend(self._image_message(u) for u in images)
        ids = []
        for start in range(0, len(messages), self._MAX_MESSAGES):
            result = self._broadcast(messages[start:start + self._MAX_MESSAGES])
            ids.extend(m.get("id", "") for m in result.get("sentMessages", []))
            time.sleep(2)
        return {"id": ids[0] if ids else "", "url": "https://line.me/"}
