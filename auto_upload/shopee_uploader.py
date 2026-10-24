import hashlib
import hmac
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# Region -> API base. Thailand uses the global partner host; other regions are
# routed via a country suffix (e.g. partner.my.shopeemobile.com).
SHOPEE_BASE = os.getenv("SHOPEE_API_BASE", "https://partner.shopeemobile.com")
_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


class ShopeeUploader:
    """Upload product media to a Shopee shop via the Open Platform API v2.

    Shopee has no "media post" API; media is attached to product listings.
    This uploader hosts the product images on Shopee (product/upload_img) so
    they are ready to be referenced by a listing once the shop is connected.

    NOTE: requires an approved Shopee Open Platform app + a Seller Center shop
    authorized to it. Signing follows the v2 spec and must be validated against
    real credentials once approved.
    """

    def __init__(self, partner_id="", partner_key="", access_token="",
                 shop_id="", account_name=""):
        self.partner_id = partner_id or _env("SHOPEE_PARTNER_ID")
        self.partner_key = partner_key or _env("SHOPEE_PARTNER_KEY")
        self.access_token = access_token or _env("SHOPEE_ACCESS_TOKEN")
        self.shop_id = shop_id or _env("SHOPEE_SHOP_ID")
        self.account_name = account_name
        if not (self.partner_id and self.partner_key and self.access_token and self.shop_id):
            raise Exception(
                "Shopee credentials missing: set SHOPEE_PARTNER_ID, "
                "SHOPEE_PARTNER_KEY, SHOPEE_ACCESS_TOKEN and SHOPEE_SHOP_ID"
            )

    def _headers(self):
        return {"Content-Type": "application/json"}

    def _sign(self, method, path, params):
        """Shopee v2 signature: HMAC-SHA256(partner_key, base_string).

        base_string = timestamp + partner_id + method + path + sorted_query
        where sorted_query excludes the `sign` parameter.
        """
        sorted_query = "&".join(
            f"{k}={v}" for k, v in sorted(params.items()) if k != "sign"
        )
        base_string = (
            f"{params['timestamp']}{self.partner_id}{method}{path}{sorted_query}"
        )
        return hmac.new(
            self.partner_key.encode(),
            base_string.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _request(self, path, method="GET", params=None, files=None):
        params = dict(params or {})
        params.setdefault("timestamp", int(time.time()))
        params.setdefault("access_token", self.access_token)
        params.setdefault("shop_id", self.shop_id)
        params["partner_id"] = self.partner_id
        params["sign"] = self._sign(method, path, params)
        url = f"{SHOPEE_BASE}{path}"
        if method == "POST" and not files:
            resp = requests.post(url, params=params, data=params, timeout=60)
        elif method == "POST":
            resp = requests.post(url, params=params, files=files, timeout=120)
        else:
            resp = requests.get(url, params=params, timeout=60)
        data = resp.json()
        if data.get("error"):
            raise Exception(
                f"Shopee error {data.get('error')} ({data.get('message')})"
            )
        return data.get("response") or data

    def upload_media(self, media_url):
        """Host one product image on Shopee and return (image_id, image_url)."""
        if any(ext in media_url.lower() for ext in _VIDEO_EXTS):
            raise Exception(
                f"Shopee media upload supports images only; got: {media_url[:120]}"
            )
        resp = requests.get(media_url, timeout=180, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        name = media_url.split("?")[0].rsplit("/", 1)[-1] or "image.jpg"
        files = {"images[]": (name, resp.content, "image/jpeg")}
        result = self._request(
            "/api/v2/product/upload_img", method="POST", files=files
        )
        image_id = result.get("image_id", "")
        image_url = (result.get("image_info") or {}).get("image_url", "")
        logger.info(f"[{self.account_name}] Shopee media hosted: {image_url}")
        return image_id, image_url

    def upload_carousel(self, image_urls, caption="", title=""):
        """Host every carousel image on Shopee."""
        results = []
        for url in image_urls[:9]:
            image_id, image_url = self.upload_media(url)
            results.append({"id": image_id, "url": image_url})
        return results
