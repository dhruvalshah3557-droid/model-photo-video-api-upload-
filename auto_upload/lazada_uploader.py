import hashlib
import hmac
import logging
import os
import time
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# Region -> API base. Thailand uses the .co.th host.
LAZADA_BASES = {
    "th": "https://api.lazada.co.th",
    "my": "https://api.lazada.com.my",
    "ph": "https://api.lazada.com.ph",
    "sg": "https://api.lazada.sg",
    "vn": "https://api.lazada.vn",
    "id": "https://api.lazada.co.id",
}
LAZADA_BASE = LAZADA_BASES.get(
    os.getenv("LAZADA_REGION", "th").lower(),
    os.getenv("LAZADA_API_BASE", "https://api.lazada.co.th"),
)
_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


class LazadaUploader:
    """Upload product media to a Lazada shop via the Open Platform API.

    Lazada has no "media post" API; media is attached to product listings. This
    uploader migrates product images onto Lazada's CDN (image/migrate) so they
    are ready to be referenced by a listing.

    NOTE: requires an approved Lazada Open Platform app + a Seller Center shop
    authorized to it. The v2 signature must be validated against real
    credentials once approved.
    """

    def __init__(self, app_key="", app_secret="", access_token="", account_name=""):
        self.app_key = app_key or _env("LAZADA_APP_KEY")
        self.app_secret = app_secret or _env("LAZADA_APP_SECRET")
        self.access_token = access_token or _env("LAZADA_ACCESS_TOKEN")
        self.account_name = account_name
        if not (self.app_key and self.app_secret and self.access_token):
            raise Exception(
                "Lazada credentials missing: set LAZADA_APP_KEY, "
                "LAZADA_APP_SECRET and LAZADA_ACCESS_TOKEN"
            )

    def _sign(self, method, path, params):
        """Lazada v2 signature: HMAC-SHA256(app_secret, canonical_string).

        The canonical string is the sorted, URL-encoded query parameters
        (excluding `sign`) joined with '&'.
        """
        sorted_query = "&".join(
            f"{quote(str(k))}={quote(str(v))}"
            for k, v in sorted(params.items()) if k != "sign"
        )
        base_string = f"{method}&{path}&{quote(sorted_query)}"
        return hmac.new(
            self.app_secret.encode(),
            base_string.encode(),
            hashlib.sha256,
        ).hexdigest().upper()

    def _request(self, api_name, params=None, method="GET"):
        params = dict(params or {})
        params.setdefault("timestamp", int(time.time() * 1000))
        params.setdefault("sign_method", "sha256")
        params.setdefault("access_token", self.access_token)
        params["app_key"] = self.app_key
        params["api"] = api_name
        params["sign"] = self._sign(method, api_name, params)
        url = f"{LAZADA_BASE}{api_name}"
        if method == "POST":
            resp = requests.post(url, params=params, timeout=120)
        else:
            resp = requests.get(url, params=params, timeout=60)
        data = resp.json()
        if data.get("code") and str(data.get("code")) not in ("0",):
            raise Exception(
                f"Lazada error {data.get('code')}: {data.get('message')}"
            )
        return data.get("result") or data

    def upload_media(self, media_url):
        """Migrate one product image onto Lazada's CDN and return its URL."""
        if any(ext in media_url.lower() for ext in _VIDEO_EXTS):
            raise Exception(
                f"Lazada media upload supports images only; got: {media_url[:120]}"
            )
        result = self._request(
            "/image/migrate",
            params={"imageUrl": media_url},
            method="POST",
        )
        image_url = str(result.get("imageUrl") or "").strip()
        if not image_url:
            raise Exception(f"Lazada image/migrate returned no URL: {result}")
        logger.info(f"[{self.account_name}] Lazada media migrated: {image_url}")
        return image_url

    def upload_carousel(self, image_urls, caption="", title=""):
        """Migrate every carousel image onto Lazada's CDN."""
        results = []
        for url in image_urls:
            migrated = self.upload_media(url)
            results.append({"id": migrated, "url": migrated})
        return results
