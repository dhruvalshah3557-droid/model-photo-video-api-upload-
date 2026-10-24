import json
import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import requests
from media_prep import _download_bytes, audio_state, prepare_video, video_layout_from_bytes

logger = logging.getLogger(__name__)

FB_GRAPH_URL = "https://graph.facebook.com/v26.0"

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")

_REGIONAL_AUDIO_QUERIES = (
    (("vietnam",), ("Vietnam trending", "Vietnamese pop", "Vietnam luxury instrumental")),
    (("japan",), ("Japan trending", "Japanese pop", "Japanese luxury instrumental")),
    (("korea",), ("Korea trending", "K-pop", "Korean luxury instrumental")),
    (("russia",), ("Russia trending", "Russian pop", "Russian luxury instrumental")),
    (("philippines", "colour diam ph"), ("Philippines trending", "OPM", "Filipino pop")),
    (("myanmar",), ("Myanmar trending", "Burmese pop", "Myanmar instrumental")),
    (("bangkok",), ("Thailand trending", "Thai pop", "Thai luxury instrumental")),
    (("indonesia",), ("Indonesia trending", "Indonesian pop", "Indonesia instrumental")),
    (("dubai",), ("Dubai trending", "Arabic luxury", "Arabic instrumental")),
    (("kuwait",), ("Kuwait trending", "Khaleeji", "Arabic luxury")),
    (("pakistan",), ("Pakistan trending", "Pakistani pop", "South Asian instrumental")),
    (("singapore",), ("Singapore trending", "Singapore pop", "Asian luxury instrumental")),
    (("hong kong", "colour diam hk", "colourdiamhk"), ("Hong Kong trending", "Cantopop", "Hong Kong luxury instrumental")),
    (("israel",), ("Israel trending", "Hebrew pop", "Hebrew instrumental")),
    (("spain",), ("Spain trending", "Spanish pop", "Latin luxury")),
    (("italy",), ("Italy trending", "Italian pop", "Italian luxury instrumental")),
    (("sweden",), ("Sweden trending", "Swedish pop", "Scandinavian instrumental")),
    (("germany",), ("Germany trending", "German pop", "European luxury instrumental")),
    (("poland",), ("Poland trending", "Polish pop", "Polish instrumental")),
    (("denmark",), ("Denmark trending", "Danish pop", "Scandinavian instrumental")),
    (("france",), ("France trending", "French pop", "French luxury instrumental")),
    (("turkey",), ("Turkey trending", "Turkish pop", "Turkish instrumental")),
    (("china",), ("China trending", "Chinese pop", "Chinese instrumental")),
    (("greece",), ("Greece trending", "Greek pop", "Greek luxury instrumental")),
    (("lebanon",), ("Lebanon trending", "Levantine pop", "Arabic luxury")),
    (("czech", "czechia"), ("Czech trending", "Czech pop", "Central European instrumental")),
)
_DEFAULT_AUDIO_QUERIES = ("trending luxury", "elegant instrumental", "cinematic fashion")

# Additional gentle catalog searches for silent Reels. These are mood based so
# Meta returns music licensed for the destination account and region. Audible
# source videos never enter this path and retain their original soundtrack.
_SOFT_AUDIO_QUERIES = (
    "soft luxury piano", "gentle piano instrumental", "calm elegant piano",
    "soft ambient instrumental", "peaceful cinematic instrumental",
    "delicate acoustic guitar", "warm acoustic instrumental",
    "relaxing luxury lounge", "soft jazz instrumental",
    "smooth lounge instrumental", "dreamy instrumental",
    "serene ambient music", "romantic piano instrumental",
    "graceful strings instrumental", "light classical instrumental",
    "minimal piano ambience", "calm fashion instrumental",
    "elegant jewellery music", "soft bossa nova instrumental",
    "gentle harp instrumental", "peaceful flute instrumental",
    "slow luxury instrumental", "soothing instrumental",
    "quiet morning piano", "soft sunset ambience",
)


def _is_video_url(url):
    return any(ext in str(url or "").lower() for ext in _VIDEO_EXTS)


def _is_cover_image_url(url):
    clean = str(url or "").strip()
    path = clean.split("?", 1)[0].lower()
    return clean.startswith(("https://", "http://")) and path.endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    )


_COVER_PROBE_CACHE = {}
_COVER_PROBE_HEADERS = {"User-Agent": "facebookexternalhit/1.1"}


def _cover_is_publicly_fetchable(url):
    """True when Meta's crawler can fetch the Reel cover as an image."""
    clean = str(url or "").strip()
    if clean in _COVER_PROBE_CACHE:
        return _COVER_PROBE_CACHE[clean]
    ok = False
    try:
        resp = requests.head(
            clean,
            headers=_COVER_PROBE_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        content_type = (resp.headers.get("Content-Type") or "").lower().split(";", 1)[0]
        ok = resp.status_code in (200, 206) and content_type.startswith("image/")
        if not ok and resp.status_code in (403, 405, 501):
            resp = requests.get(
                clean,
                headers=_COVER_PROBE_HEADERS,
                timeout=15,
                stream=True,
                allow_redirects=True,
            )
            content_type = (resp.headers.get("Content-Type") or "").lower().split(";", 1)[0]
            ok = resp.status_code in (200, 206) and content_type.startswith("image/")
            resp.close()
    except requests.RequestException:
        ok = False
    _COVER_PROBE_CACHE[clean] = ok
    return ok


def _usable_cover_url(url):
    """Return a public image URL Meta can fetch, else empty.

    A 404/HTML cover on a Reel container is rejected with
    'Only photo or video can be accepted as media type.' and parks the job.
    """
    clean = str(url or "").strip()
    if not _is_cover_image_url(clean):
        return ""
    if not _cover_is_publicly_fetchable(clean):
        logger.warning("Skipping unfetchable Instagram Reel cover: %s", clean)
        return ""
    return clean


_IMAGE_MAGIC = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
)
_UNFETCHABLE_MEDIA_MARKERS = (
    "could not be fetched",
    "media download has failed",
    "only photo or video can be accepted as media type",
    "doesn't meet our requirements",
    "does not meet our requirements",
    "the media uri",
    "image_url is required",
    "video_url is required",
    "the parameter image_url",
    "the parameter video_url",
)
def _is_unfetchable_media_error(error, result=None):
    blob = " ".join((
        str((error or {}).get("message", "") or ""),
        str((error or {}).get("error_user_msg", "") or ""),
        str((error or {}).get("error_user_title", "") or ""),
        str(result or ""),
    )).lower()
    return any(marker in blob for marker in _UNFETCHABLE_MEDIA_MARKERS)


class IGAccountNotLinkedError(Exception):
    """Configured Facebook page is not linked to an Instagram Business account."""


class InstagramRateLimitError(Exception):
    """Meta temporarily blocked Instagram publishing because app capacity was exceeded."""


class InstagramUploader:
    _IG_ID_CACHE = {}
    _LAST_AUDIO_BY_ACCOUNT = {}
    _RATE_LIMIT_UNTIL = 0.0

    @classmethod
    def _ensure_not_rate_limited(cls):
        remaining = int(cls._RATE_LIMIT_UNTIL - time.monotonic())
        if remaining > 0:
            raise InstagramRateLimitError(
                f"META_RATE_LIMIT active; retry after {remaining}s "
                "(code=4 subcode=2207051)"
            )

    @classmethod
    def _record_rate_limit(cls):
        cooldown = max(60, int(os.getenv("IG_RATE_LIMIT_COOLDOWN_SECONDS", "900")))
        cls._RATE_LIMIT_UNTIL = max(cls._RATE_LIMIT_UNTIL, time.monotonic() + cooldown)
        return cooldown

    @staticmethod
    def _identity_key(value):
        # Meta page names commonly contain spaces while Instagram usernames do
        # not. Normalising both forms lets "Colour Diam Sweden" match
        # "@colourdiamsweden" without relying on a fragile literal substring.
        key = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
        return key.replace("colordiam", "colourdiam")

    def __init__(self, ig_user_id, access_token, page_name=""):
        self.ig_user_id = self._resolve_ig_user_id(ig_user_id, access_token, page_name)
        self.access_token = access_token
        self.page_name = page_name

    @classmethod
    def _resolve_ig_user_id(cls, configured_id, access_token, page_name=""):
        configured_id = str(configured_id or "").strip()
        cache_key = configured_id or page_name
        if cache_key and cache_key in cls._IG_ID_CACHE:
            return cls._IG_ID_CACHE[cache_key]

        if configured_id:
            cls._IG_ID_CACHE[cache_key] = configured_id
            return configured_id

        try:
            response = requests.get(
                f"{FB_GRAPH_URL}/me/accounts",
                params={
                    "fields": "id,name,instagram_business_account{id,username}",
                    "access_token": access_token,
                },
                timeout=15,
            )
            response.raise_for_status()
            pages = response.json().get("data", [])
        except Exception:
            pages = []

        matches = []
        wanted = cls._identity_key(page_name)
        for page in pages:
            ig = page.get("instagram_business_account") or {}
            ig_id = str(ig.get("id", "") or "").strip()
            if not ig_id:
                continue
            page_label = cls._identity_key(page.get("name", ""))
            ig_username = cls._identity_key(ig.get("username", ""))
            identities = (page_label, ig_username)
            if not wanted or any(
                identity and (wanted == identity or wanted in identity)
                for identity in identities
            ):
                matches.append(ig_id)

        if len(matches) == 1:
            resolved = matches[0]
        elif len(matches) > 1 and not wanted:
            resolved = matches[0]
        else:
            resolved = ""

        if resolved and cache_key:
            cls._IG_ID_CACHE[cache_key] = resolved
        if not resolved:
            raise IGAccountNotLinkedError(
                f"Could not resolve Instagram Business Account ID for '{page_name}'. "
                "Set the verified Instagram Business ID in Accounts.platform_account_id."
            )
        return resolved

    @staticmethod
    def _json_or_error(resp):
        try:
            return resp.json()
        except Exception:
            return {"error": {"message": f"Instagram API returned HTTP {resp.status_code}: {resp.text[:500]}"}}

    def _remote_audio_state(self, media_url):
        suffix = Path(str(media_url).split("?")[0]).suffix or ".mp4"
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            with requests.get(media_url, stream=True, timeout=180) as resp:
                resp.raise_for_status()
                with open(path, "wb") as out:
                    for chunk in resp.iter_content(1024 * 1024):
                        if chunk:
                            out.write(chunk)
            return audio_state(path)
        except Exception as exc:
            logger.warning(f"[{self.page_name}] Could not inspect Reel audio: {exc}")
            return "unknown"
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _trending_audio_configuration(self, media_key):
        digest = hashlib.sha256(media_key.encode("utf-8", errors="ignore")).digest()
        params = {
            "audio_type": "music",
            "user_id": self.ig_user_id,
            "access_token": self.access_token,
        }
        page_label = str(self.page_name or "").strip().lower()
        regional_queries = next(
            (queries for labels, queries in _REGIONAL_AUDIO_QUERIES if any(label in page_label for label in labels)),
            _DEFAULT_AUDIO_QUERIES,
        )
        queries_raw = os.getenv("IG_AUDIO_SEARCH_QUERIES", "").strip()
        configured_queries = [q.strip() for q in re.split(r"[\n,]+", queries_raw) if q.strip()]
        # Keep regional music prominent while using the gentle pool on roughly
        # half of silent Reels. Generic/main accounts use the full soft pool.
        include_soft = regional_queries == _DEFAULT_AUDIO_QUERIES or digest[-1] % 2 == 0
        soft_queries = list(_SOFT_AUDIO_QUERIES) if include_soft else []
        queries = list(dict.fromkeys(
            list(regional_queries) + soft_queries + configured_queries
        ))
        if not queries:
            search_query = os.getenv("IG_AUDIO_SEARCH_QUERY", "").strip()
            queries = [search_query] if search_query else list(regional_queries)
        search_query = queries[int.from_bytes(digest[:4], "big") % len(queries)] if queries else ""
        if search_query:
            params["search_query"] = search_query
        resp = requests.get(f"{FB_GRAPH_URL}/ig_audio", params=params, timeout=30)
        result = self._json_or_error(resp)
        tracks = result.get("data") or []
        if not tracks:
            message = result.get("error", {}).get("message", "No authorized Instagram audio returned")
            raise Exception(f"Could not select Instagram music: {message}")
        # The old implementation always used tracks[0], making every Reel use
        # the same song. Rotate deterministically across all returned results.
        track_index = int.from_bytes(digest[4:12], "big") % len(tracks)
        track = tracks[track_index]
        audio_id = str(track.get("audio_id") or track.get("id") or "").strip()
        if not audio_id:
            raise Exception("Instagram audio search returned a track without an audio ID")
        last_audio_id = self._LAST_AUDIO_BY_ACCOUNT.get(self.ig_user_id)
        if audio_id == last_audio_id and len(tracks) > 1:
            track = tracks[(track_index + 1) % len(tracks)]
            audio_id = str(track.get("audio_id") or track.get("id") or "").strip()
        self._LAST_AUDIO_BY_ACCOUNT[self.ig_user_id] = audio_id
        logger.info(
            f"[{self.page_name}] Selected Instagram audio: "
            f"{track.get('title', audio_id)}"
        )
        return json.dumps({
            "audio_id": audio_id,
            "audio_volume": int(os.getenv("IG_AUDIO_VOLUME", "80")),
            "video_volume": int(os.getenv("IG_VIDEO_VOLUME", "60")),
            "should_loop_audio": True,
        })

    @staticmethod
    def _download_image_bytes(media_url):
        """Download original image bytes Meta's crawler could not fetch."""
        name, content, content_type = _download_bytes(media_url)
        content_type = (content_type or "").lower().split(";", 1)[0]
        content = content or b""
        if content_type.startswith("text/") or "html" in content_type:
            raise Exception(f"Downloaded media is not an image ({content_type})")
        if not any(content.startswith(magic) for magic in _IMAGE_MAGIC):
            raise Exception("Downloaded bytes are not a valid image")
        name = name or media_url.split("?")[0].rsplit("/", 1)[-1] or "image.jpg"
        if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            if content.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
            elif content.startswith(b"\x89PNG"):
                content_type = "image/png"
            elif content.startswith(b"GIF8"):
                content_type = "image/gif"
            else:
                content_type = "image/jpeg"
        return name, content, content_type

    def _create_resumable_image(self, media_url, caption, product_id="", carousel_item=False):
        """Upload image bytes when Meta cannot fetch the public URL."""
        name, content, content_type = self._download_image_bytes(media_url)
        params = {
            "media_type": "IMAGE",
            "upload_type": "resumable",
            "access_token": self.access_token,
        }
        if carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["caption"] = caption
        if product_id and not carousel_item:
            params["product_tags"] = f'[{{"product_id":"{product_id}"}}]'
        logger.info(
            f"[{self.page_name}] Uploading IG image bytes because Meta could not fetch {media_url}"
        )
        self._ensure_not_rate_limited()
        response = requests.post(
            f"{FB_GRAPH_URL}/{self.ig_user_id}/media",
            data=params,
            timeout=60,
        )
        session = self._json_or_error(response)
        container_id = str(session.get("id", "") or "").strip()
        if not container_id:
            error = session.get("error", {})
            if error.get("code") == 4 or error.get("error_subcode") == 2207051:
                cooldown = self._record_rate_limit()
                raise InstagramRateLimitError(
                    f"META_RATE_LIMIT code={error.get('code')} "
                    f"subcode={error.get('error_subcode')}; retry after {cooldown}s: "
                    f"{error.get('message', 'Application request limit reached')}"
                )
            raise Exception(session.get("error", {}).get("message", str(session)))
        upload_url = session.get("uri") or (
            f"https://rupload.facebook.com/ig-api-upload/v26.0/{container_id}"
        )
        upload = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {self.access_token}",
                "Content-Type": content_type or "image/jpeg",
                "file_size": str(len(content)),
                "offset": "0",
                "file_name": name,
            },
            data=content,
            timeout=180,
        )
        upload_result = self._json_or_error(upload)
        if not upload.ok or not (
            upload_result.get("success") or upload_result.get("id") or upload.status_code < 300
        ):
            error = upload_result.get("error", {})
            if error.get("code") == 4 or error.get("error_subcode") == 2207051:
                cooldown = self._record_rate_limit()
                raise InstagramRateLimitError(
                    f"META_RATE_LIMIT code={error.get('code')} "
                    f"subcode={error.get('error_subcode')}; retry after {cooldown}s: "
                    f"{error.get('message', 'Application request limit reached')}"
                )
            raise Exception(
                upload_result.get("error", {}).get("message")
                or str(upload_result)
            )
        logger.info(f"[{self.page_name}] Uploaded IG image bytes: {name}")
        return container_id

    def _create_resumable_reel(self, media_url, caption, product_id="", cover_url=""):
        """Upload single videos as Reels, retaining the prepared size and soundtrack."""
        selection_key = f"instagram|{self.ig_user_id}|{media_url}"
        name, content, content_type = prepare_video(
            media_url,
            fill_9x16=True,
            selection_key=selection_key,
            platform="instagram",
        )
        layout = video_layout_from_bytes(content)
        # Single-video resumable publishing uses REELS. VIDEO is reserved for
        # carousel children; sending it here rejects square/landscape sources.
        media_type = "REELS"
        params = {
            "media_type": media_type,
            "upload_type": "resumable",
            "share_to_feed": "true",
            "caption": caption,
            "access_token": self.access_token,
        }
        cover = _usable_cover_url(cover_url) if media_type == "REELS" else ""
        if cover:
            params["cover_url"] = cover
        else:
            params["thumb_offset"] = 1000
        if product_id:
            params["product_tags"] = f'[{{"product_id":"{product_id}"}}]'
        response = requests.post(
            f"{FB_GRAPH_URL}/{self.ig_user_id}/media",
            data=params,
            timeout=60,
        )
        session = self._json_or_error(response)
        container_id = str(session.get("id", "") or "").strip()
        if not container_id:
            error = session.get("error", {})
            if params.get("cover_url") and _is_unfetchable_media_error(error, session):
                logger.warning(
                    f"[{self.page_name}] Reel cover {params['cover_url']} was "
                    "rejected by Meta; retrying with thumb_offset"
                )
                params.pop("cover_url", None)
                params["thumb_offset"] = 1000
                response = requests.post(
                    f"{FB_GRAPH_URL}/{self.ig_user_id}/media",
                    data=params,
                    timeout=60,
                )
                session = self._json_or_error(response)
                container_id = str(session.get("id", "") or "").strip()
                error = session.get("error", {})
        if not container_id:
            error = session.get("error", {})
            if error.get("code") == 4 or error.get("error_subcode") == 2207051:
                cooldown = self._record_rate_limit()
                raise InstagramRateLimitError(
                    f"META_RATE_LIMIT code={error.get('code')} "
                    f"subcode={error.get('error_subcode')}; retry after {cooldown}s: "
                    f"{error.get('message', 'Application request limit reached')}"
                )
            raise Exception(session.get("error", {}).get("message", str(session)))
        upload_url = session.get("uri") or (
            f"https://rupload.facebook.com/ig-api-upload/v26.0/{container_id}"
        )
        upload = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {self.access_token}",
                "Content-Type": content_type or "video/mp4",
                "file_size": str(len(content)),
                "offset": "0",
            },
            data=content,
            timeout=300,
        )
        upload_result = self._json_or_error(upload)
        if not upload.ok or not upload_result.get("success"):
            error = upload_result.get("error", {})
            if error.get("code") == 4 or error.get("error_subcode") == 2207051:
                cooldown = self._record_rate_limit()
                raise InstagramRateLimitError(
                    f"META_RATE_LIMIT code={error.get('code')} "
                    f"subcode={error.get('error_subcode')}; retry after {cooldown}s: "
                    f"{error.get('message', 'Application request limit reached')}"
                )
            raise Exception(
                upload_result.get("error", {}).get("message")
                or upload_result.get("debug_info", {}).get("message")
                or str(upload_result)
            )
        logger.info(
            f"[{self.page_name}] Uploaded {media_type} ({layout}) with original size/sound: {name}"
        )
        return container_id

    def _create_media_container(
        self, media_url, caption, is_video=False, product_id="",
        carousel_item=False, cover_url="",
    ):
        media_url = str(media_url or "").strip()
        if not media_url:
            raise Exception("Instagram media URL is empty")

        params = {"access_token": self.access_token}
        if carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["caption"] = caption

        if is_video:
            # Carousel children remain URL-based VIDEO containers. Standalone
            # videos use the resumable original-size path in upload().
            params["media_type"] = "VIDEO" if carousel_item else "REELS"
            params["video_url"] = media_url
            if not carousel_item:
                cover = _usable_cover_url(cover_url)
                if cover:
                    params["cover_url"] = cover
                else:
                    params["thumb_offset"] = 1000
        else:
            params["image_url"] = media_url

        if product_id and not carousel_item:
            params["product_tags"] = f"[{{\"product_id\":\"{product_id}\"}}]"

        url = f"{FB_GRAPH_URL}/{self.ig_user_id}/media"
        kind = "reel" if is_video and not carousel_item else "video" if is_video else "image"
        logger.info(f"[{self.page_name}] Creating IG {kind} container")
        self._ensure_not_rate_limited()
        resp = requests.post(url, data=params, timeout=60)
        result = self._json_or_error(resp)
        if "id" not in result:
            error = result.get("error", {})
            if error.get("code") == 4 or error.get("error_subcode") == 2207051:
                cooldown = self._record_rate_limit()
                raise InstagramRateLimitError(
                    f"META_RATE_LIMIT code=4 subcode=2207051; retry after {cooldown}s"
                )
            if _is_unfetchable_media_error(error, result):
                if is_video and not carousel_item and params.get("cover_url"):
                    logger.warning(
                        f"[{self.page_name}] Reel cover {params['cover_url']} was "
                        "rejected by Meta; retrying with thumb_offset"
                    )
                    params.pop("cover_url", None)
                    params["thumb_offset"] = 1000
                    resp = requests.post(url, data=params, timeout=60)
                    result = self._json_or_error(resp)
                    if "id" in result:
                        logger.info(f"[{self.page_name}] Container created: {result['id']}")
                        return result["id"]
                    error = result.get("error", {})
                logger.warning(
                    f"[{self.page_name}] Meta could not fetch {media_url}; "
                    "uploading original bytes instead"
                )
                if is_video:
                    return self._create_resumable_reel(
                        media_url, caption, product_id, cover_url=""
                    )
                return self._create_resumable_image(
                    media_url, caption, product_id, carousel_item
                )
            logger.error(f"[{self.page_name}] Container failed: {result}")
            raise Exception(result.get("error", {}).get("message", str(result)))
        logger.info(f"[{self.page_name}] Container created: {result['id']}")
        return result["id"]

    def _create_carousel_container(self, child_ids, caption, product_id=""):
        if len(child_ids) < 2:
            raise Exception("Instagram carousel requires at least two child containers")
        params = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": self.access_token,
        }
        if product_id:
            params["product_tags"] = f"[{{\"product_id\":\"{product_id}\"}}]"
        url = f"{FB_GRAPH_URL}/{self.ig_user_id}/media"
        logger.info(f"[{self.page_name}] Creating IG carousel container")
        self._ensure_not_rate_limited()
        resp = requests.post(url, data=params, timeout=60)
        result = self._json_or_error(resp)
        if "id" not in result:
            error = result.get("error", {})
            if error.get("code") == 4 or error.get("error_subcode") == 2207051:
                cooldown = self._record_rate_limit()
                raise InstagramRateLimitError(
                    f"META_RATE_LIMIT code=4 subcode=2207051; retry after {cooldown}s"
                )
            logger.error(f"[{self.page_name}] Carousel container failed: {result}")
            raise Exception(result.get("error", {}).get("message", str(result)))
        logger.info(f"[{self.page_name}] Carousel container created: {result['id']}")
        return result["id"]

    def _publish_container(self, container_id, retries=6):
        url = f"{FB_GRAPH_URL}/{self.ig_user_id}/media_publish"
        params = {"creation_id": container_id, "access_token": self.access_token}
        last_error = None
        for attempt in range(retries + 1):
            logger.info(
                f"[{self.page_name}] Publishing container {container_id} "
                f"(attempt {attempt + 1}/{retries + 1})"
            )
            self._ensure_not_rate_limited()
            resp = requests.post(url, data=params, timeout=60)
            result = self._json_or_error(resp)
            if "id" in result:
                logger.info(f"[{self.page_name}] IG post published: {result['id']}")
                return result
            error = result.get("error", {})
            message = error.get("message", str(result))
            code = error.get("code")
            subcode = error.get("error_subcode")
            if code == 4 or subcode == 2207051:
                cooldown = self._record_rate_limit()
                logger.warning(
                    f"[{self.page_name}] Meta application limit reached; "
                    f"pausing Instagram for {cooldown}s"
                )
                raise InstagramRateLimitError(
                    f"META_RATE_LIMIT code={code} subcode={subcode}; "
                    f"retry after {cooldown}s: {message}"
                )
            lowered = message.lower()
            transient = (
                code in (9007,)
                or "not available" in lowered
                or "processing" in lowered
                or "not ready" in lowered
            )
            if not transient:
                logger.error(f"[{self.page_name}] Publish failed: {result}")
                raise Exception(message)
            last_error = Exception(message)
            wait = min(60, 15 * (attempt + 1))
            logger.warning(f"[{self.page_name}] Media not ready yet; retrying in {wait}s")
            time.sleep(wait)
        raise last_error or Exception("Instagram container never became ready")

    def upload_carousel(self, media_urls, caption, product_id=""):
        media_urls = [str(url or "").strip() for url in media_urls if str(url or "").strip()]
        if not media_urls:
            raise Exception("Instagram carousel has no usable media")
        if len(media_urls) == 1:
            logger.warning(f"[{self.page_name}] Carousel reduced to one media item; publishing as a single post")
            return self.upload(media_urls[0], caption, product_id)

        child_ids = []
        child_urls = []
        for media_url in media_urls[:10]:
            try:
                child_id = self._create_media_container(
                    media_url,
                    caption,
                    is_video=_is_video_url(media_url),
                    product_id="",
                    carousel_item=True,
                )
            except InstagramRateLimitError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[{self.page_name}] Skipping unusable IG carousel child {media_url}: {exc}"
                )
                continue
            child_ids.append(child_id)
            child_urls.append(media_url)
            time.sleep(max(5, int(os.getenv("IG_CAROUSEL_ITEM_DELAY_SECONDS", "8"))))

        if len(child_ids) == 1:
            logger.warning(
                f"[{self.page_name}] Carousel collapsed to one usable child; "
                "publishing as a single post"
            )
            return self.upload(child_urls[0], caption, product_id)
        container_id = self._create_carousel_container(child_ids, caption, product_id)
        time.sleep(15)
        return self._publish_container(container_id)

    def upload(self, media_url, caption, product_id="", cover_url="", force_video=None):
        # Signed/CDN URLs often omit a file extension. The queue format is the
        # authoritative media type after preflight, so let the caller force a
        # video job to remain a Reel instead of accidentally creating an image.
        is_video = _is_video_url(media_url) if force_video is None else bool(force_video)
        if is_video:
            logger.info(
                f"[{self.page_name}] Uploading original video bytes; "
                "preserve sound and size, Reels only when vertical"
            )
            container_id = self._create_resumable_reel(
                media_url, caption, product_id, cover_url
            )
            logger.info(f"[{self.page_name}] Waiting 30s for media processing...")
            time.sleep(30)
            return self._publish_container(container_id)
        container_id = self._create_media_container(
            media_url, caption, is_video, product_id, cover_url=cover_url
        )
        wait = 30 if is_video else 5
        logger.info(f"[{self.page_name}] Waiting {wait}s for media processing...")
        time.sleep(wait)
        return self._publish_container(container_id)

    def permalink(self, media_id):
        if not media_id:
            return ""
        try:
            resp = requests.get(
                f"{FB_GRAPH_URL}/{media_id}",
                params={"fields": "permalink", "access_token": self.access_token},
                timeout=15,
            )
            data = resp.json()
            return str(data.get("permalink", "") or "").strip()
        except Exception:
            return ""
