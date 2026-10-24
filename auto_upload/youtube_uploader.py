import hashlib
import pickle
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
import mimetypes

from media_prep import prepare_video

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CRED_DIR = Path(__file__).parent / "credentials"
TOKEN_FILE = CRED_DIR / "youtube_token.pickle"
CLIENT_SECRET_FILE = CRED_DIR / "youtube_client_secret.json"
AUTH_CODE_FILE = CRED_DIR / "youtube_auth_code.txt"
LOOPBACK_REDIRECT_URI = "http://127.0.0.1:8080/"


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


def token_cache_path(client_id="", refresh_token=""):
    """Keep ColourDiamondss and JIYA OAuth pickles from overwriting each other."""
    client_id = (client_id or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not client_id and not refresh_token:
        return TOKEN_FILE
    digest = hashlib.sha256(
        f"{client_id}\n{refresh_token}".encode("utf-8")
    ).hexdigest()[:20]
    return CRED_DIR / f"youtube_token_{digest}.pickle"


def _extract_code(value):
    """Extract the auth code from a pasted value (raw code or full redirect URL)."""
    value = value.strip()
    if "code=" in value:
        params = parse_qs(urlsplit(value).query)
        if "code" in params:
            return params["code"][0]
    return value


class YouTubeUploader:
    def __init__(self, refresh_token=None, client_id=None, client_secret=None):
        self.service = self._authenticate(refresh_token, client_id, client_secret)

    def _authenticate(self, refresh_token=None, client_id=None, client_secret=None):
        refresh_token = refresh_token or _env("YOUTUBE_OAUTH_REFRESH_TOKEN")
        client_id = client_id or _env("YOUTUBE_CLIENT_ID")
        client_secret = client_secret or _env("YOUTUBE_CLIENT_SECRET")

        if (not client_id or not client_secret) and CLIENT_SECRET_FILE.exists():
            with open(CLIENT_SECRET_FILE) as f:
                _data = json.load(f)
            _installed = _data.get("installed") or _data.get("web") or {}
            client_id = client_id or _installed.get("client_id", "")
            client_secret = client_secret or _installed.get("client_secret", "")

        token_file = token_cache_path(client_id, refresh_token)
        CRED_DIR.mkdir(parents=True, exist_ok=True)

        if refresh_token and client_id and client_secret:
            logger.info("Authenticating YouTube with OAuth refresh token")
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=SCOPES,
            )
            creds.refresh(Request())
            with open(token_file, "wb") as f:
                pickle.dump(creds, f)
            return build("youtube", "v3", credentials=creds)

        creds = None
        if token_file.exists():
            with open(token_file, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not CLIENT_SECRET_FILE.exists():
                    raise RuntimeError(
                        "YouTube OAuth is not configured. Set YOUTUBE_CLIENT_ID, "
                        "YOUTUBE_CLIENT_SECRET and YOUTUBE_OAUTH_REFRESH_TOKEN "
                        f"env vars, or place a client secret at {CLIENT_SECRET_FILE}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CLIENT_SECRET_FILE), SCOPES
                )
                flow.redirect_uri = LOOPBACK_REDIRECT_URI
                auth_url, _ = flow.authorization_url(
                    access_type="offline", include_granted_scopes="true"
                )
                print("\n" + "=" * 60)
                print("YOUTUBE AUTH REQUIRED - Visit this URL:")
                print("=" * 60)
                print(auth_url)
                print("\nAfter authorizing, paste the code in this file:")
                print(str(AUTH_CODE_FILE))
                print("=" * 60 + "\n")

                if not AUTH_CODE_FILE.exists():
                    raise RuntimeError(
                        f"Please:\n"
                        f"1. Open the URL above in your browser\n"
                        f"2. Authorize the app\n"
                        f"3. Copy the code and save it to:\n"
                        f"   {AUTH_CODE_FILE}\n"
                        f"4. Run the tool again"
                    )
                with open(AUTH_CODE_FILE) as f:
                    code = _extract_code(f.read())
                creds = flow.fetch_token(code=code)
                creds = flow.credentials
                AUTH_CODE_FILE.unlink(missing_ok=True)
            with open(token_file, "wb") as f:
                pickle.dump(creds, f)

        return build("youtube", "v3", credentials=creds)

    def _download_video(self, url):
        logger.info(f"Downloading video for YouTube: {url}")
        name, content, content_type = prepare_video(
            url, fill_9x16=True, platform="youtube"
        )
        return io.BytesIO(content), content_type

    def upload(self, media_url, title, description="", tags=None):
        logger.info("Uploading to YouTube...")
        video_content, content_type = self._download_video(media_url)

        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags or [],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }

        mime_type = self._resolve_mime_type(media_url, content_type)

        media = MediaIoBaseUpload(video_content, mimetype=mime_type, resumable=True)

        request = self.service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = request.execute()
        video_id = response.get("id")
        logger.info(f"YouTube uploaded: https://youtu.be/{video_id}")
        return response

    @staticmethod
    def _resolve_mime_type(media_url, content_type):
        """Pick a MIME type from the download's Content-Type or the URL's extension."""
        if content_type and content_type.split(";")[0].strip().startswith("video/"):
            return content_type.split(";")[0].strip()
        guessed, _ = mimetypes.guess_type(media_url.split("?")[0])
        return guessed or "video/mp4"
