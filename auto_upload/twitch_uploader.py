import logging
import os
import shutil
import subprocess
import tempfile
import time

import requests

logger = logging.getLogger(__name__)

TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_AUTH = "https://id.twitch.tv/oauth2/token"
TWITCH_RTMP = "rtmp://live.twitch.tv/app"
_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


class TwitchUploader:
    """Publish a product video to Twitch as a live broadcast (VOD).

    Twitch's Helix API has no video file upload endpoint, so a pre-recorded
    video is pushed to the RTMP ingest as a live stream with ffmpeg; the
    resulting broadcast becomes a VOD on the channel (when VOD storage is
    enabled). The Helix API is used best-effort to set the stream title and
    to resolve the latest VOD URL afterwards.

    Requires:
    - stream_key: Twitch stream key (from the dashboard or API) OR the shared
      TWITCH_STREAM_KEY env var.
    - client_id / client_secret: Twitch application credentials (optional, for
      VOD lookup).
    - broadcaster_id: numeric Twitch user id of the channel (optional, for VOD
      lookup).
    - user_token: broadcaster OAuth user token with channel:manage:broadcast
      (optional, to set the stream title).
    """

    def __init__(self, stream_key="", client_id="", client_secret="",
                 broadcaster_id="", user_token="", account_name=""):
        self.stream_key = stream_key or _env("TWITCH_STREAM_KEY")
        self.client_id = client_id or _env("TWITCH_CLIENT_ID")
        self.client_secret = client_secret or _env("TWITCH_CLIENT_SECRET")
        self.broadcaster_id = broadcaster_id or _env("TWITCH_BROADCASTER_ID")
        self.user_token = user_token or _env("TWITCH_ACCESS_TOKEN")
        self.account_name = account_name
        if not self.stream_key:
            raise Exception(
                "Twitch stream key missing: set TWITCH_STREAM_KEY "
                "(or the account's META_TOKEN_TWITCH_* env var)"
            )
        self._app_token = ""
        self._app_token_expires = 0

    def _app_access_token(self):
        """Return a cached Twitch app access token (client_credentials)."""
        if not self.client_id or not self.client_secret:
            return ""
        if self._app_token and time.time() < self._app_token_expires - 60:
            return self._app_token
        try:
            resp = requests.post(
                TWITCH_AUTH,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=30,
            )
            data = resp.json()
            self._app_token = data.get("access_token", "")
            self._app_token_expires = time.time() + int(data.get("expires_in", 3600))
        except Exception as exc:
            logger.warning(f"Twitch app token fetch failed: {exc}")
            self._app_token = ""
        return self._app_token

    def _set_stream_title(self, title):
        """Best-effort: update the channel stream title before broadcasting."""
        if not (self.user_token and self.broadcaster_id and title):
            return
        try:
            requests.patch(
                f"{TWITCH_API}/channels",
                params={"broadcaster_id": self.broadcaster_id},
                headers={"Authorization": f"Bearer {self.user_token}", "Client-Id": self.client_id},
                json={"title": title[:140]},
                timeout=30,
            )
            logger.info(f"[{self.account_name}] Twitch stream title set")
        except Exception as exc:
            logger.warning(f"Twitch stream title update failed (ignored): {exc}")

    def _latest_vod(self):
        """Best-effort: return (vod_id, vod_url) for the most recent archived VOD."""
        if not (self.client_id and self.broadcaster_id):
            return "", ""
        token = self._app_access_token()
        if not token:
            return "", ""
        try:
            resp = requests.get(
                f"{TWITCH_API}/videos",
                params={"user_id": self.broadcaster_id, "type": "archive", "first": "1"},
                headers={"Authorization": f"Bearer {token}", "Client-Id": self.client_id},
                timeout=30,
            )
            items = resp.json().get("data", [])
            if items:
                return str(items[0].get("id", "")), str(items[0].get("url", ""))
        except Exception as exc:
            logger.warning(f"Twitch VOD lookup failed: {exc}")
        return "", ""

    def _download_video(self, media_url):
        resp = requests.get(media_url, timeout=180, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        return resp.content

    def _video_duration(self, video_path):
        if not shutil.which("ffprobe"):
            return 0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", video_path],
                check=False, capture_output=True, text=True,
            )
            return float(out.stdout.strip())
        except Exception:
            return 0

    def upload(self, media_url, title="", description=""):
        """Broadcast a video file to Twitch via RTMP and return the VOD/channel URL."""
        if not any(ext in media_url.lower() for ext in _VIDEO_EXTS):
            raise Exception(
                f"Twitch only supports video media; got: {media_url[:120]}"
            )
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise Exception("ffmpeg not found; Twitch broadcasting requires ffmpeg")

        self._set_stream_title(title)

        fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            with open(tmp_path, "wb") as f:
                f.write(self._download_video(media_url))

            duration = self._video_duration(tmp_path)
            timeout = int(duration) + 300 if duration > 0 else 900
            cmd = [
                ffmpeg, "-y", "-re", "-i", tmp_path,
                "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
                "-c:a", "aac", "-b:a", "128k",
                "-f", "flv", f"{TWITCH_RTMP}/{self.stream_key}",
            ]
            logger.info(f"[{self.account_name}] Broadcasting to Twitch ({int(duration)}s video)")
            result = subprocess.run(
                cmd, check=False, capture_output=True, timeout=timeout
            )
            if result.returncode != 0:
                detail = (result.stderr or b"").decode(errors="ignore")[-500:]
                raise Exception(f"Twitch RTMP broadcast failed: {detail}")
            logger.info(f"[{self.account_name}] Twitch broadcast finished")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        vod_id, vod_url = self._latest_vod()
        return {"id": vod_id, "url": vod_url}
