import logging
import os

import requests

from media_prep import prepare_video

logger = logging.getLogger(__name__)

X_MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
X_TWEET_URL = "https://api.twitter.com/2/tweets"
X_ME_URL = "https://api.twitter.com/2/users/me"

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
_VIDEO_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MB, well under the 5 MB APPEND cap


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


def _is_video_url(url):
    return any(ext in url.lower() for ext in _VIDEO_EXTS)


class XUploader:
    """Publish image/video/carousel tweets via the X (Twitter) API v2.

    Media is uploaded through the v1.1 media/upload endpoint (OAuth 1.0a user
    context; videos use the chunked INIT/APPEND/FINALIZE flow), then the post
    is created with the v2 /tweets endpoint. Credentials come from the
    consumer key/secret + access token/secret pair.
    """

    def __init__(self, consumer_key="", consumer_secret="", access_token="",
                 access_token_secret="", account_name=""):
        self.consumer_key = consumer_key or _env("X_CONSUMER_KEY")
        self.consumer_secret = consumer_secret or _env("X_CONSUMER_SECRET")
        self.access_token = access_token or _env("X_ACCESS_TOKEN")
        self.access_token_secret = access_token_secret or _env("X_ACCESS_TOKEN_SECRET")
        self.account_name = account_name
        if not (self.consumer_key and self.consumer_secret and self.access_token and self.access_token_secret):
            raise Exception(
                "X credentials missing: set X_CONSUMER_KEY, X_CONSUMER_SECRET, "
                "X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET"
            )

    def _auth(self):
        from requests_oauthlib import OAuth1
        return OAuth1(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=self.access_token,
            resource_owner_secret=self.access_token_secret,
        )

    def _download_media(self, media_url):
        resp = requests.get(
            media_url,
            timeout=180,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        resp.raise_for_status()
        name = media_url.split("?")[0].rsplit("/", 1)[-1] or "media"
        return name, resp.content, resp.headers.get("Content-Type", "application/octet-stream")

    def _upload_image(self, media_url):
        name, content, content_type = self._download_media(media_url)
        resp = requests.post(
            X_MEDIA_UPLOAD_URL,
            auth=self._auth(),
            files={"media": (name, content, content_type)},
            data={"media_category": "tweet_image"},
            timeout=300,
        )
        data = resp.json()
        media_id = data.get("media_id_string") or data.get("media_id")
        if not media_id:
            raise Exception(f"X image upload error: {data}")
        logger.info(f"[{self.account_name}] X image uploaded: {media_id}")
        return str(media_id)

    def _upload_video(self, media_url):
        name, content, content_type = prepare_video(media_url)
        total_bytes = len(content)

        init = requests.post(
            X_MEDIA_UPLOAD_URL,
            auth=self._auth(),
            data={
                "command": "INIT",
                "media_type": content_type,
                "total_bytes": total_bytes,
                "media_category": "tweet_video",
            },
            timeout=60,
        )
        init_data = init.json()
        media_id = init_data.get("media_id_string") or init_data.get("media_id")
        if not media_id:
            raise Exception(f"X video INIT error: {init_data}")
        media_id = str(media_id)

        for idx in range(0, total_bytes, _VIDEO_CHUNK_BYTES):
            chunk = content[idx:idx + _VIDEO_CHUNK_BYTES]
            append = requests.post(
                X_MEDIA_UPLOAD_URL,
                auth=self._auth(),
                files={"media": ("chunk", chunk, content_type)},
                data={
                    "command": "APPEND",
                    "media_id": media_id,
                    "segment_index": idx // _VIDEO_CHUNK_BYTES,
                },
                timeout=300,
            )
            if append.status_code != 200:
                raise Exception(f"X video APPEND error {append.status_code}: {append.text[:500]}")

        final = requests.post(
            X_MEDIA_UPLOAD_URL,
            auth=self._auth(),
            data={"command": "FINALIZE", "media_id": media_id},
            timeout=300,
        )
        final_data = final.json()
        if not (final_data.get("media_id_string") or final_data.get("media_id")):
            raise Exception(f"X video FINALIZE error: {final_data}")
        logger.info(f"[{self.account_name}] X video uploaded: {media_id}")
        return media_id

    def _create_tweet(self, caption, media_ids):
        body = {"text": (caption or "")[:280]}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        resp = requests.post(
            X_TWEET_URL,
            auth=self._auth(),
            json=body,
            timeout=60,
        )
        data = resp.json()
        tweet_id = data.get("data", {}).get("id")
        if not tweet_id:
            raise Exception(f"X tweet error: {data}")
        logger.info(f"[{self.account_name}] X tweet created: {tweet_id}")
        return str(tweet_id)

    def upload(self, media_url, caption="", title="", is_video=None):
        """Publish a single image or video tweet."""
        if is_video is None:
            is_video = _is_video_url(media_url)
        media_id = self._upload_video(media_url) if is_video else self._upload_image(media_url)
        tweet_id = self._create_tweet(caption, [media_id])
        return {"id": tweet_id, "url": f"https://x.com/i/status/{tweet_id}"}

    def upload_carousel(self, image_urls, caption="", product_id=""):
        """Publish up to 4 images in a single tweet."""
        media_ids = [self._upload_image(u) for u in image_urls[:4]]
        tweet_id = self._create_tweet(caption, media_ids)
        return {"id": tweet_id, "url": f"https://x.com/i/status/{tweet_id}"}
