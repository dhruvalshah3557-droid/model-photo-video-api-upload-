import json
import logging
import os
import time

import requests

from media_prep import prepare_video

logger = logging.getLogger(__name__)

WECHAT_BASE = "https://api.weixin.qq.com"

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")

# In-process access-token cache: {appid: (token, expires_at_epoch)}.
_TOKEN_CACHE = {}


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


def _is_video_url(url):
    return any(ext in url.lower() for ext in _VIDEO_EXTS)


class WeChatUploader:
    """Publish to a WeChat Official Account (公众号) via the cgi-bin API.

    Images/videos are uploaded as permanent materials, then delivered to
    followers through the mass-send (群发) API. Carousels (multiple images) are
    published as draft articles (图文) via the draft + freepublish flow because
    WeChat mass-send supports a single material per message.

    Credentials: the account's `credential_property_key` should point to an
    env var containing `APPID:SECRET` (e.g. `WECHAT_CD=wx123:secret`), or the
    shared `WECHAT_APPID`/`WECHAT_APPSECRET` env vars are used as a fallback.
    """

    def __init__(self, credential="", appid="", appsecret="", account_name=""):
        self.account_name = account_name
        if credential:
            parts = str(credential).split(":", 1)
            appid = parts[0].strip()
            appsecret = parts[1].strip() if len(parts) > 1 else ""
        self.appid = appid or _env("WECHAT_APPID")
        self.appsecret = appsecret or _env("WECHAT_APPSECRET")
        if not self.appid or not self.appsecret:
            raise Exception(
                "WeChat credentials missing: set {key}=APPID:SECRET or "
                "WECHAT_APPID/WECHAT_APPSECRET"
            )

    # ------------------------------------------------------------------
    # Access token
    # ------------------------------------------------------------------
    def _get_token(self):
        cached = _TOKEN_CACHE.get(self.appid)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        resp = requests.get(
            f"{WECHAT_BASE}/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": self.appid,
                "secret": self.appsecret,
            },
            timeout=30,
        )
        data = resp.json()
        if "access_token" not in data:
            raise Exception(f"WeChat token error: {data}")
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 7200))
        _TOKEN_CACHE[self.appid] = (token, time.time() + expires_in)
        return token

    @staticmethod
    def _raise_for_err(data):
        errcode = data.get("errcode", 0)
        if errcode and errcode != 0:
            raise Exception(
                f"WeChat API error {errcode}: {data.get('errmsg', '')}"
            )

    # ------------------------------------------------------------------
    # Media download
    # ------------------------------------------------------------------
    @staticmethod
    def _download_media(media_url):
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

    # ------------------------------------------------------------------
    # Permanent material upload
    # ------------------------------------------------------------------
    def add_image_material(self, media_url):
        """Upload a permanent image material; returns (media_id, image_url)."""
        name, content, content_type = self._download_media(media_url)
        resp = requests.post(
            f"{WECHAT_BASE}/cgi-bin/material/add_material",
            params={"access_token": self._get_token(), "type": "image"},
            files={"media": (name, content, content_type)},
            timeout=120,
        )
        data = resp.json()
        self._raise_for_err(data)
        if "media_id" not in data:
            raise Exception(f"WeChat image material error: {data}")
        logger.info(f"[{self.account_name}] WeChat image material uploaded: {data['media_id']}")
        return data["media_id"], data.get("url", "")

    def add_video_material(self, media_url, title, description):
        """Upload a permanent video material; returns media_id."""
        name, content, content_type = prepare_video(media_url)
        description_json = json.dumps(
            {"title": (title or "")[:64], "introduction": (description or "")[:120]},
            ensure_ascii=False,
        )
        resp = requests.post(
            f"{WECHAT_BASE}/cgi-bin/material/add_material",
            params={"access_token": self._get_token(), "type": "video"},
            files={"media": (name, content, content_type)},
            data={"description": description_json},
            timeout=600,
        )
        data = resp.json()
        self._raise_for_err(data)
        if "media_id" not in data:
            raise Exception(f"WeChat video material error: {data}")
        logger.info(f"[{self.account_name}] WeChat video material uploaded: {data['media_id']}")
        return data["media_id"]

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------
    def _mass_send(self, msgtype, payload):
        body = {"filter": {"is_to_all": True}, "msgtype": msgtype, msgtype: payload}
        resp = requests.post(
            f"{WECHAT_BASE}/cgi-bin/message/mass/sendall",
            params={"access_token": self._get_token()},
            data=json.dumps(body, ensure_ascii=False),
            timeout=60,
        )
        data = resp.json()
        self._raise_for_err(data)
        logger.info(f"[{self.account_name}] WeChat mass-send {msgtype} ok: {data}")
        return data

    def upload(self, media_url, caption="", title="", is_video=None):
        """Publish a single image or video to all followers via mass-send."""
        if is_video is None:
            is_video = _is_video_url(media_url)
        if is_video:
            media_id = self.add_video_material(media_url, title, caption)
            result = self._mass_send("video", {"media_id": media_id, "title": (title or "")[:64]})
        else:
            media_id, _ = self.add_image_material(media_url)
            result = self._mass_send("image", {"media_id": media_id})
        msg_id = result.get("msg_id", "")
        return {"id": msg_id or media_id, "url": "https://mp.weixin.qq.com/"}

    def upload_carousel(self, image_urls, caption="", title="", product_id=""):
        """Publish multiple images as a draft article (图文) via draft+freepublish."""
        if not image_urls:
            raise Exception("No images provided for WeChat article")
        thumb_media_id, _ = self.add_image_material(image_urls[0])
        image_urls_in_article = []
        for url in image_urls:
            _, page_url = self.add_image_material(url)
            image_urls_in_article.append(page_url or url)
        content = "\n".join(f'<p><img src="{u}"/></p>' for u in image_urls_in_article)
        articles = [{
            "title": (title or caption or "Product")[:64],
            "content": content,
            "thumb_media_id": thumb_media_id,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }]
        resp = requests.post(
            f"{WECHAT_BASE}/cgi-bin/draft/add",
            params={"access_token": self._get_token()},
            data=json.dumps({"articles": articles}, ensure_ascii=False),
            timeout=60,
        )
        data = resp.json()
        self._raise_for_err(data)
        draft_media_id = data.get("media_id")
        if not draft_media_id:
            raise Exception(f"WeChat draft error: {data}")
        resp = requests.post(
            f"{WECHAT_BASE}/cgi-bin/freepublish/submit",
            params={"access_token": self._get_token()},
            data=json.dumps({"media_id": draft_media_id}, ensure_ascii=False),
            timeout=60,
        )
        data = resp.json()
        self._raise_for_err(data)
        logger.info(f"[{self.account_name}] WeChat article published: {data}")
        return {"id": data.get("publish_id", draft_media_id), "url": "https://mp.weixin.qq.com/"}
