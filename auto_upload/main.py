#!/usr/bin/env python3
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from caption_generator import generate_caption, generate_hashtags
from config import Config
from facebook_uploader import FacebookUploader
from instagram_uploader import (
    IGAccountNotLinkedError,
    InstagramRateLimitError,
    InstagramUploader,
)
from job_generator import generate_jobs, is_model_media, model_media_priority
from line_uploader import LineUploader
from linkedin_uploader import LinkedInUploader
from media_prep import media_kind, validate_media_url
from pinterest_uploader import PinterestUploader
from shopee_uploader import ShopeeUploader
from sheets_reader import SheetsReader
from tiktok_uploader import TikTokUploader, TikTokDeliveryUncertain
from twitch_uploader import TwitchUploader
from wechat_uploader import WeChatUploader
from x_uploader import XUploader
from youtube_uploader import YouTubeUploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("/tmp/auto_upload.log")],
)
logger = logging.getLogger("main")


_BROKEN_MEDIA_MARKERS = (
    "404 client error",
    "http error code 404",
    "404 not found",
    "media download has failed",
    "media could not be fetched",
    "could not be fetched from this uri",
    "video download failed with",
    "only photo or video can be accepted as media type",
    "media validation failed",
    "video transcoding error",
    "broken container",
    "dead links",
    "media urls are unavailable",
    "failed validation",
    "nothing to publish",
    "name or service not known",
    "failed to resolve",
    "name resolutionerror",
)


class DeliveryUncertainError(Exception):
    """The platform may have accepted a post; automatic retry is unsafe."""


_TRANSIENT_MEDIA_MARKERS = (
    "timed out",
    "timeout",
    "incompleteread",
    "connection broken",
    "chunkedencodingerror",
    "connection reset",
    "connection aborted",
)


def _is_broken_media_error(message):
    """Dead/broken media (404, un-fetchable URI) will not heal on retry."""
    lowered = message.lower()
    if any(marker in lowered for marker in _TRANSIENT_MEDIA_MARKERS):
        return False
    return any(marker in lowered for marker in _BROKEN_MEDIA_MARKERS)


_MEDIA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; auto-upload)"}
_MEDIA_CLASS_CACHE = {}


def _classify_media_url(url):
    """Classify a media URL as 'image', 'video', 'invalid' or 'unknown'.

    Used to drop dead links before handing media to uploaders. The Instagram
    Graph API rejects carousel children whose URL does not return an actual
    image or video with "Only photo or video can be accepted as media type.".
    URLs that are unreachable or report no usable content type are treated as
    'unknown' (kept): the failure is likely transient and the uploader's
    normal error handling still applies.
    """
    url = str(url or "").strip()
    if not url:
        return "invalid"
    if url in _MEDIA_CLASS_CACHE:
        return _MEDIA_CLASS_CACHE[url]
    result = "unknown"
    try:
        resp = requests.get(
            url, headers=_MEDIA_HEADERS, timeout=20, stream=True, allow_redirects=True
        )
        content_type = (resp.headers.get("Content-Type") or "").lower().split(";", 1)[0]
        status = resp.status_code
        resp.close()
        if status not in (200, 206):
            result = "invalid"
        elif content_type.startswith("image/"):
            result = "image"
        elif content_type.startswith("video/"):
            result = "video"
        elif content_type in ("", "application/octet-stream"):
            result = "unknown"
        else:
            result = "invalid"
    except requests.RequestException:
        result = "unknown"
    _MEDIA_CLASS_CACHE[url] = result
    return result


def _filter_valid_media(media):
    """Drop media URLs that definitively do not point to an image/video.

    A single dead URL (404 HTML page, PDF, etc.) fails the whole post on the
    IG/FB APIs, so broken links are removed up front; if none of the resolved
    media is usable the job fails with a clear message instead of an opaque
    platform API error.
    """
    kept = []
    for url in media:
        if _classify_media_url(url) == "invalid":
            logger.warning(
                f"Media URL is not an accessible image/video, dropping from post: {url}"
            )
        else:
            kept.append(url)
    return kept


def _probe_job_media(job, media):
    """Strictly probe media that passed the content-type filter.

    _filter_valid_media only checks that a URL reports an image/video
    Content-Type. A truncated or corrupt video (e.g. an MP4 missing its moov
    box) still passes that check and then fails on the platform API with a
    "Video Transcoding Error / Broken container" that retries forever. When
    MEDIA_VALIDATION is enabled, videos are also probed with ffprobe and the
    job is raised as needs_review so it stops burning attempts.
    """
    if not Config.MEDIA_VALIDATION:
        return media
    strict = []
    selection = str(job.get("media_selection", "") or "")
    expects_video = (
        str(job.get("format", "") or "").strip().lower() == "video"
        or selection == "product_video"
        or selection.startswith("model_video:")
    )
    for url in media:
        # Product/model video jobs are videos by contract even when their CDN
        # URL has no extension or reports application/octet-stream. Force an
        # ffprobe before choosing the account turn so bad audio-only/truncated
        # sources cannot consume a publish attempt.
        if expects_video or media_kind(url) == "video":
            reason = validate_media_url(url, kind="video", ffprobe=True)
            if reason:
                raise Exception(f"Media validation failed ({url}): {reason}")
        strict.append(url)
    return strict


def open_sheets_with_retry():
    """Open Google Sheets robustly, including quota errors raised during init."""
    delays = (0, 15, 30, 60, 60, 60)
    last_error = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            logger.warning(f"Waiting {delay}s before retrying Google Sheets connection")
            time.sleep(delay)
        try:
            return SheetsReader()
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            transient = "429" in text or "quota" in text or "500" in text or "503" in text
            if not transient or attempt == len(delays):
                raise
            logger.warning(
                f"Google Sheets connection attempt {attempt}/{len(delays)} failed: {exc}"
            )
    raise last_error


_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def _is_video_url(url):
    return any(ext in str(url or "").lower() for ext in _VIDEO_EXTS)


def _is_carousel_image_url(url):
    """Instagram carousel image URLs must point to an actual image, not a PDF."""
    lowered = str(url or "").split("?", 1)[0].lower()
    return bool(lowered) and not lowered.endswith(".pdf") and not _is_video_url(lowered)


def _dedupe_media(urls):
    ordered = []
    seen = set()
    for url in urls:
        url = str(url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered


def job_unique_key(job):
    """Unique key for a queue job: SKU + account + platform + format + media type.

    Guarantees the same job is never generated twice. The key must match
    SheetsReader.get_existing_job_keys() exactly.
    """
    return (
        job["sku"],
        job["account_id"],
        job["platform"],
        job["format"],
        job["media_selection"],
    )


def _lang_code(primary_language):
    value = str(primary_language or "en").strip().lower()
    if value in {"zh-hk", "zh-tw", "zh-mo", "zh-hant"}:
        return "zh-hant"
    return value.split("-")[0]


def resolve_media(job, source):
    """Resolve ordered media URLs for one queue job.

    Carousels are image-only and ordered as:
      1) main product image
      2) certificate image on Instagram (when a public image URL is supplied)
      3) remaining product images
    Instagram accepts up to 10 carousel children, so its list is capped at 10.
    Product/model videos always remain separate video/Reel jobs.
    """
    selection = job.get("media_selection", "")
    if selection == "carousel":
        main_image = source.get("main_image", "")
        side_images = list(source.get("side_images", []))

        if job.get("platform", "").lower() == "instagram":
            # Keep the carousel image-only. Product/model videos are generated as
            # separate Reel jobs, so embedding the product video here creates a
            # duplicate post and makes one bad video fail an otherwise valid carousel.
            media = []
            if main_image:
                media.append(main_image)

            certificate_media = source.get("certificate_media_url", "")
            if certificate_media:
                if _is_carousel_image_url(certificate_media):
                    media.append(certificate_media)
                else:
                    logger.warning(
                        f"SKU {source.get('sku', '')}: certificate media is not a public image URL; "
                        "skipping it in the Instagram carousel"
                    )

            media.extend(side_images)
            return _dedupe_media(media)[:10]

        media = []
        if main_image:
            media.append(main_image)
        media.extend(side_images)
        return _dedupe_media(media)

    if selection == "product_video":
        return [source["video_url"]] if source["video_url"] else []
    if selection.startswith("model_photo:"):
        try:
            idx = int(selection.split(":", 1)[1])
            photos = source.get("model_images", [])
            return [photos[idx]] if 0 <= idx < len(photos) else []
        except (ValueError, IndexError):
            return []
    if selection.startswith("model_video:"):
        try:
            idx = int(selection.split(":", 1)[1])
            videos = source["model_videos"]
            return [videos[idx]] if 0 <= idx < len(videos) else []
        except (ValueError, IndexError):
            return []
    return []


_PRODUCT_LINK_LABELS = {
    "ar": "عرض المنتج", "vi": "Xem sản phẩm", "zh": "查看产品",
    "zh-hant": "查看產品",
    "sv": "Visa produkt", "de": "Produkt ansehen", "pl": "Zobacz produkt",
    "da": "Se produkt", "fr": "Voir le produit", "tr": "Ürünü görüntüle",
    "it": "Visualizza prodotto", "es": "Ver producto", "ja": "商品を見る",
    "ko": "제품 보기", "ru": "Посмотреть товар", "he": "לצפייה במוצר",
    "id": "Lihat produk", "my": "ကုန်ပစ္စည်းကိုကြည့်ရန်", "th": "ดูสินค้า",
    "tl": "Tingnan ang produkto", "fil": "Tingnan ang produkto",
    "el": "Δείτε το προϊόν",
    "lb": "عرض المنتج", "cs": "Zobrazit produkt",
    "ur": "پروڈکٹ دیکھیں",
}


def _append_product_link(caption, source, lang="en", platform="", account_id=""):
    caption = str(caption or "").strip()
    product_link = str(source.get("product_link", "") or "").strip()
    if not product_link or product_link in caption:
        return caption
    # Instagram does not make URLs in post/Reel captions clickable. For the
    # Kuwait account, send shoppers to the clickable profile link and retain
    # the SKU so they land on (or can request) the exact product. Facebook
    # continues to receive the direct, clickable product URL below.
    if platform == "instagram" and account_id == "IG-KUWAIT":
        sku = str(source.get("sku", "") or "").strip()
        link_line = "تسوقي عبر الرابط في السيرة الذاتية: colourdiam.com"
        if sku:
            link_line += f" • رمز المنتج: {sku}"
        return f"{caption}\n\n{link_line}" if caption else link_line
    label = _PRODUCT_LINK_LABELS.get(lang, "View product")
    link_line = f"{label}: {product_link}"
    return f"{caption}\n\n{link_line}" if caption else link_line


def _normalize_hashtags(value):
    """Return valid, space-separated hashtags from inconsistent sheet input.

    Regional source cells sometimes contain comma-separated words without '#',
    while others contain an already formatted whitespace-separated hashtag list.
    Normalize both forms before publishing so tags remain clickable.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    explicit = re.findall(r"#[^\s,;،#]+", raw)
    if len(explicit) > 1 and not re.search(r"[,;،\n]", raw):
        parts = explicit
    else:
        parts = re.split(r"[,;،\n]+", raw)
        if len(parts) == 1 and "#" not in raw:
            parts = raw.split()
    normalized = []
    seen = set()
    for part in parts:
        token = re.sub(r"[^\w]", "", str(part).lstrip("#"), flags=re.UNICODE)
        if not token:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(f"#{token}")
    return " ".join(normalized)


_REGIONAL_FALLBACK_HASHTAGS = {
    "ar": "#ألماس #مجوهرات #مجوهرات_فاخرة",
    "vi": "#KimCương #TrangSức #TrangSứcCaoCấp",
    "zh": "#钻石 #珠宝 #高级珠宝",
    "zh-hant": "#鑽石 #珠寶 #高級珠寶 #香港",
    "sv": "#Diamanter #Smycken #Lyxsmycken",
    "de": "#Diamanten #Schmuck #Luxusschmuck",
    "pl": "#Diamenty #Biżuteria #BiżuteriaLuksusowa",
    "da": "#Diamanter #Smykker #Luksussmykker",
    "fr": "#Diamants #Bijoux #HauteJoaillerie",
    "tr": "#Elmas #Mücevher #LüksMücevher",
    "it": "#Diamanti #Gioielli #AltaGioielleria",
    "es": "#Diamantes #Joyas #AltaJoyería",
    "ja": "#ダイヤモンド #ジュエリー #高級ジュエリー",
    "ko": "#다이아몬드 #주얼리 #명품주얼리",
    "ru": "#Бриллианты #Украшения #ЮвелирныеИзделия",
    "he": "#יהלומים #תכשיטים #תכשיטי_יוקרה",
    "id": "#Berlian #Perhiasan #PerhiasanMewah",
    "my": "#စိန် #လက်ဝတ်ရတနာ #အဆင့်မြင့်လက်ဝတ်ရတနာ",
    "th": "#เพชร #เครื่องประดับ #เครื่องประดับหรู",
    "tl": "#Brilyante #Alahas #MarangyangAlahas",
    "fil": "#Brilyante #Alahas #MarangyangAlahas",
    "el": "#Διαμάντια #Κοσμήματα #ΠολυτελήΚοσμήματα",
    "lb": "#ألماس #مجوهرات #مجوهرات_فاخرة",
    "cs": "#Diamanty #Šperky #LuxusníŠperky",
    "ur": "#ہیرے #زیورات #لگژری_زیورات #پاکستان",
}


_CARAT_WEIGHT_RE = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*-?\s*"
    r"(?:ct|cts|carats?|karats?|karatowy|karatów|karata|"
    r"quilates?|carati|карата?|克拉|캐럿|カラット|قيراط|קראט|"
    r"กะรัต|ကာရက်)\b",
    re.IGNORECASE,
)
_SHAPE_ALIASES = {
    "marquise": (
        "marquise", "marquesa", "маркиз", "마퀴즈", "マーキース", "马眼",
        "ماركيز", "מרקיזה",
    ),
    "pear": (
        "pear", "poire", "pera", "hạt lê", "quả lê", "груш", "gruszk",
        "pear-shaped", "taglio a pera", "talla pera", "taille poire",
        "كمثرى", "אגס", "ペア", "페어", "birnen", "pære", "pæreform",
        "αχλαδ",
    ),
    "oval": (
        "oval", "ovale", "óvalo", "овал", "椭圆", "オーバル", "오벌",
        "بيضاوي", "אובל", "owal",
    ),
    "round": (
        "round", "brilliant", "rond", "redondo", "кругл", "圆形",
        "ラウンド", "라운드",
    ),
    "cushion": ("cushion", "coussin", "cashion", "подуш"),
    "radiant": ("radiant", "радиант"),
    "princess": ("princess", "принцесс"),
    "emerald": ("emerald", "smaragd", "esmeralda", "изумруд"),
    "heart": ("heart", "cuore", "corazón", "coeur", "сердц"),
    "asscher": ("asscher",),
}


def _carat_weights(text):
    """Return normalized carat weights mentioned in product copy."""
    return [float(value.replace(",", ".")) for value in _CARAT_WEIGHT_RE.findall(str(text or ""))]


def _caption_body(caption_text):
    return re.sub(r"#\S+", " ", str(caption_text or ""))


def _shape_token_in_text(text, alias):
    alias = str(alias or "").casefold()
    body = str(text or "").casefold()
    if not alias or not body:
        return False
    if re.search(r"[a-z]", alias):
        return re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", body) is not None
    return alias in body


def _canonical_shape(value):
    text = str(value or "").strip()
    if not text:
        return ""
    for canonical, aliases in _SHAPE_ALIASES.items():
        for alias in (canonical,) + tuple(aliases):
            if _shape_token_in_text(text, alias):
                return canonical
    return ""


def _shapes_in_text(text):
    found = set()
    for canonical, aliases in _SHAPE_ALIASES.items():
        for alias in (canonical,) + tuple(aliases):
            if _shape_token_in_text(text, alias):
                found.add(canonical)
                break
    return found


def _is_loose_diamond(source):
    link = str((source or {}).get("product_link", "") or "").lower()
    return any(
        token in link
        for token in ("/product/diamond", "/diamonddetails", "/diamonds/product")
    )


def _product_weight(source):
    weights = _carat_weights((source or {}).get("product_name", ""))
    if weights:
        return weights[0]
    fields = SheetsReader._details_fields((source or {}).get("details", ""))
    raw = fields.get("weight") or fields.get("carat") or fields.get("cts") or ""
    try:
        return float(str(raw).replace(",", ".").split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _product_shape(source):
    fields = SheetsReader._details_fields((source or {}).get("details", ""))
    return _canonical_shape(fields.get("shape") or (source or {}).get("product_name", ""))


_BLANK_PRODUCT_CAPTION_RE = re.compile(
    r"(with\s+\.|beauty of\s+\.|Stunning\s+just arrived|Make a statement with\s+\.|✨\s+—)",
    re.IGNORECASE,
)


def _product_title(source):
    name = str((source or {}).get("product_name") or "").strip()
    if name:
        return name
    details = str((source or {}).get("details") or "").strip()
    for line in details.replace("\r", "").split("\n"):
        line = line.strip()
        if line and not line.lower().startswith("sku"):
            return line[:120]
    sku = str((source or {}).get("sku") or "").strip()
    return f"STK {sku} Diamond Jewelry" if sku else "Diamond Jewelry"


def _caption_has_blank_product(caption_text):
    text = re.sub(r"\s+", " ", str(caption_text or "")).strip()
    return bool(_BLANK_PRODUCT_CAPTION_RE.search(text))


def _caption_product_mismatch_reason(caption_text, source):
    """Return why caption copy does not describe this product, if known."""
    if _caption_has_blank_product(caption_text):
        return "blank product caption"
    if not _is_loose_diamond(source):
        return ""
    body = _caption_body(caption_text)
    expected_weight = _product_weight(source)
    mentioned = _carat_weights(body)
    if expected_weight is not None and mentioned:
        if not any(round(value, 2) == round(expected_weight, 2) for value in mentioned):
            found = ", ".join(f"{value:g}" for value in mentioned)
            return (
                f"{source.get('sku', 'unknown SKU')} is {expected_weight:g} ct, "
                f"but the caption mentions {found} ct"
            )
    expected_shape = _product_shape(source)
    mentioned_shapes = _shapes_in_text(body)
    if expected_shape and mentioned_shapes and expected_shape not in mentioned_shapes:
        found = ", ".join(sorted(mentioned_shapes))
        return (
            f"{source.get('sku', 'unknown SKU')} is {expected_shape}, "
            f"but the caption describes {found}"
        )
    return ""


def _validate_caption_product_match(caption_text, source):
    """Block a loose-diamond caption that describes a different stone."""
    reason = _caption_product_mismatch_reason(caption_text, source)
    if not reason:
        return
    if reason == "blank product caption":
        raise ValueError("Blank product caption; refusing to publish")
    raise ValueError(f"Caption/product mismatch: {reason}")


def _generated_product_caption(source, account, lang):
    product_title = _product_title(source)
    product_info = {
        "title": product_title,
        "description": str(source.get("details") or "").strip() or product_title,
        "keywords": [product_title],
    }
    caption = generate_caption(
        product_info, account.get("account_name", ""), lang
    )
    return caption, product_info


def build_caption(job, source, account):
    """Build a localized caption and always append the product page URL.

    Regional accounts require their primary-language translation and use a
    guaranteed native hashtag set. English accounts retain the normal platform
    fallback behavior. The direct product page label is localized too.
    """
    platform = job.get("platform", "")
    lang = _lang_code(account.get("primary_language", ""))
    fallback = _lang_code(account.get("fallback_language", ""))

    lang_captions = source.get("lang_captions", {})
    lang_hashtags = source.get("lang_hashtags", {})
    hashtags = _normalize_hashtags(source.get("hashtags", ""))

    caption_text = ""
    if lang != "en":
        localized_caption = str(lang_captions.get(lang, "") or "").strip()
        if localized_caption and _caption_product_mismatch_reason(localized_caption, source):
            logger.warning(
                "Discarding mismatched %s caption for SKU %s",
                lang, source.get("sku", ""),
            )
            localized_caption = ""
        if not localized_caption:
            # Empty Source Import cells must not starve the market or burn the
            # production run on English-fallback refusals. Greece has no dedicated
            # column; other regional headers can also be blank. Always generate
            # native copy in the account language instead of mixing English.
            localized_caption, _ = _generated_product_caption(source, account, lang)
        tags = _REGIONAL_FALLBACK_HASHTAGS.get(lang, "")
        if not tags:
            raise ValueError(
                f"Missing required {lang} regional hashtags; refusing English fallback"
            )
        caption_text = f"{localized_caption}\n\n{tags}"
    else:
        for code in (lang, fallback):
            candidate = str(lang_captions.get(code, "") or "").strip()
            if not candidate:
                continue
            if _caption_product_mismatch_reason(candidate, source):
                logger.warning(
                    "Discarding mismatched %s caption for SKU %s",
                    code, source.get("sku", ""),
                )
                continue
            tags = _normalize_hashtags(lang_hashtags.get(code)) or hashtags
            caption_text = f"{candidate}\n\n{tags}" if tags else candidate
            break

    if not caption_text:
        if platform in ("facebook", "wechat", "pinterest"):
            caption = source.get("facebook_caption", "")
        elif platform in ("instagram", "line"):
            caption = source.get("instagram_caption", "")
        elif platform == "youtube":
            caption = source.get("youtube_shorts_caption", "") or source.get("facebook_caption", "")
        elif platform in ("tiktok", "x", "twitch", "shopee", "lazada"):
            caption = source.get("instagram_caption", "") or source.get("facebook_caption", "")
        elif platform == "linkedin":
            caption = source.get("facebook_caption", "") or source.get("instagram_caption", "")
        else:
            caption = source.get("instagram_caption", "")

        if _caption_has_blank_product(caption) or _caption_product_mismatch_reason(caption, source):
            caption = ""
        if caption and hashtags:
            caption_text = f"{caption}\n\n{hashtags}"
        elif caption:
            caption_text = caption

    if not caption_text or _caption_product_mismatch_reason(caption_text, source):
        auto_caption, product_info = _generated_product_caption(source, account, lang)
        auto_hashtags = generate_hashtags(
            product_info, account.get("account_name", ""), lang
        )
        if lang != "en":
            tags = _REGIONAL_FALLBACK_HASHTAGS.get(lang) or auto_hashtags
            caption_text = f"{auto_caption}\n\n{tags}"
        else:
            caption_text = f"{auto_caption}\n\n{auto_hashtags}"

    _validate_caption_product_match(caption_text, source)
    return _append_product_link(
        caption_text, source, lang, platform, str(job.get("account_id", "") or "")
    )


def _carousel_images(media):
    return [u for u in media if not _is_video_url(u)]


def _facebook_post_url(page_id, post_id):
    """Fallback FB post URL when the Graph permalink lookup fails."""
    numeric = str(post_id).rsplit("_", 1)[-1]
    return f"https://www.facebook.com/{page_id}/posts/{numeric}"


def _tag_value(job):
    return job.get("stock_id_tag", "") or job.get("sku", "")


def publish_job(job, source, account):
    """Publish one job and return (post_id, published_url)."""
    media = resolve_media(job, source)
    if not media:
        raise Exception("No media resolved for job")

    platform = job.get("platform", "")
    format_type = job.get("format", "")
    caption = build_caption(job, source, account)

    media = _filter_valid_media(media)
    if not media:
        raise Exception("All resolved media URLs are unavailable (404/dead links); nothing to publish")

    media = _probe_job_media(job, media)
    if not media:
        raise Exception("All resolved media URLs failed validation; nothing to publish")

    tag = _tag_value(job) if account.get("product_tagging") else ""
    # Instagram product tagging needs a real product item ID from a connected
    # catalog. Sending the raw SKU as product_id makes the whole post fail with
    # "(#100) ... not a valid product item ID", so fall back to untagged posts
    # until a catalog is configured for the account.
    if platform == "instagram" and tag and not account.get("catalog_or_store_id"):
        tag = ""

    if platform == "facebook":
        token = Config.get_token(account.get("credential_property_key", ""))
        if not token:
            raise Exception("No access token configured for this account")
        uploader = FacebookUploader(account["platform_account_id"], token, account.get("account_name", ""))
        if format_type == "carousel":
            images = _carousel_images(media)
            if not images:
                post = uploader.upload(media[0], caption, tag)
            else:
                post = uploader.upload_carousel(images, caption, tag)
        else:
            post = uploader.upload(media[0], caption, tag)
        post_id = post.get("id", "")
        url = uploader.permalink_url(post_id) or _facebook_post_url(
            account["platform_account_id"], post_id
        )
        return post_id, url

    if platform == "instagram":
        token = Config.get_token(account.get("credential_property_key", ""))
        if not token:
            raise Exception("No access token configured for this account")
        uploader = InstagramUploader(account["platform_account_id"], token, account.get("account_name", ""))
        if format_type == "carousel":
            post = uploader.upload_carousel(media, caption, tag)
        else:
            post = uploader.upload(
                media[0], caption, tag, cover_url=source.get("main_image", ""),
                force_video=(format_type == "video"),
            )
        post_id = post.get("id", "")
        url = uploader.permalink(post_id) or f"https://www.instagram.com/p/{post_id}"
        return post_id, url

    if platform == "youtube":
        yt_key = account.get("credential_property_key", "") or "YOUTUBE_OAUTH_REFRESH_TOKEN"
        yt_token = os.getenv(yt_key) or os.getenv("YOUTUBE_OAUTH_REFRESH_TOKEN")
        if not yt_token:
            raise Exception(
                "No YouTube OAuth refresh token configured "
                f"(set {yt_key} / YOUTUBE_OAUTH_REFRESH_TOKEN)"
            )
        if account.get("credential_property_key") and not os.getenv(yt_key):
            raise Exception(
                f"No YouTube OAuth refresh token configured for {yt_key} "
                "(a per-account token must be set; refusing to fall back to the shared token)"
            )
        client_kwargs = {}
        if yt_key == "YOUTUBE_OAUTH_REFRESH_TOKEN_COLOURDIAMONDSS":
            client_kwargs = {
                "client_id": os.getenv("YOUTUBE_CLIENT_ID_COLOURDIAMONDSS", "").strip(),
                "client_secret": os.getenv("YOUTUBE_CLIENT_SECRET_COLOURDIAMONDSS", "").strip(),
            }
            if not all(client_kwargs.values()):
                raise Exception("ColourDiamondss requires its dedicated YouTube client ID and secret")
        uploader = YouTubeUploader(refresh_token=yt_token, **client_kwargs)
        title = (job.get("title") or source.get("product_name") or "Video")[:100]
        description = caption
        response = uploader.upload(media[0], title, description)
        post_id = response.get("id", "")
        url = f"https://youtu.be/{post_id}"
        return post_id, url

    if platform == "line":
        line_token = os.getenv(account.get("credential_property_key", "")) or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        if not line_token:
            raise Exception("No LINE channel access token configured (set LINE_CHANNEL_ACCESS_TOKEN)")
        uploader = LineUploader(line_token, account.get("account_name", ""))
        if format_type == "carousel":
            images = _carousel_images(media)
            post = uploader.upload_carousel(images or media, caption)
        else:
            is_video = format_type == "video" or any(
                ext in str(media[0]).lower() for ext in (".mp4", ".mov", ".avi", ".mkv", ".webm")
            )
            thumbnail = source.get("main_image", "") if is_video else ""
            if is_video and not thumbnail:
                images = list(source.get("images") or []) + list(source.get("side_images") or [])
                thumbnail = next((u for u in images if u), "")
            post = uploader.upload(media[0], caption, is_video=is_video, thumbnail_url=thumbnail)
        post_id = post.get("id", "")
        url = post.get("url", "https://line.me/")
        return post_id, url

    if platform == "wechat":
        wechat_cred = os.getenv(account.get("credential_property_key", ""))
        uploader = WeChatUploader(
            credential=wechat_cred,
            account_name=account.get("account_name", ""),
        )
        title = (job.get("title") or source.get("product_name") or "Product")[:64]
        if format_type == "carousel":
            images = _carousel_images(media)
            post = uploader.upload_carousel(images or media, caption, title)
        else:
            is_video = format_type == "video"
            post = uploader.upload(media[0], caption, title=title, is_video=is_video)
        post_id = post.get("id", "")
        url = post.get("url", "https://mp.weixin.qq.com/")
        return post_id, url

    if platform == "pinterest":
        pin_token = os.getenv(account.get("credential_property_key", "")) or os.getenv("PINTEREST_ACCESS_TOKEN")
        uploader = PinterestUploader(
            access_token=pin_token,
            board_id=account.get("platform_account_id", ""),
            account_name=account.get("account_name", ""),
        )
        title = (job.get("title") or source.get("product_name") or "Product")[:100]
        product_link = source.get("product_link", "")
        if format_type == "carousel":
            images = _carousel_images(media)
            post = uploader.upload_carousel(images or media, caption, title, product_link)
        else:
            is_video = format_type == "video"
            thumbnail = source.get("main_image", "") if is_video else ""
            post = uploader.upload(media[0], caption, title=title, link=product_link,
                                   thumbnail_url=thumbnail, is_video=is_video)
        post_id = post.get("id", "")
        url = post.get("url", f"https://www.pinterest.com/pin/{post_id}/")
        return post_id, url

    if platform == "tiktok":
        token = os.getenv(account.get("credential_property_key", "")) or os.getenv("ZERNIO_API_KEY")
        uploader = TikTokUploader(token, account.get("account_name", ""),
                                  account.get("platform_account_id", ""), job.get("job_id", ""))
        try:
            if format_type == "carousel":
                post = uploader.upload_carousel(_carousel_images(media), caption)
            else:
                post = uploader.upload(media[0], description=caption)
        except TikTokDeliveryUncertain as exc:
            raise DeliveryUncertainError(str(exc)) from exc
        return post.get("id", ""), post.get("url", "")

    if platform == "twitch":
        if format_type != "video":
            raise Exception("Twitch only supports video jobs (carousel/image posts are not available on Twitch)")
        twitch_key = os.getenv(account.get("credential_property_key", "")) or os.getenv("TWITCH_STREAM_KEY")
        if not twitch_key:
            raise Exception("No Twitch stream key configured (set TWITCH_STREAM_KEY or META_TOKEN_TWITCH_*)")
        uploader = TwitchUploader(
            stream_key=twitch_key,
            broadcaster_id=account.get("platform_account_id", ""),
            account_name=account.get("account_name", ""),
        )
        title = (job.get("title") or source.get("product_name") or "Video")[:140]
        post = uploader.upload(media[0], title=title, description=caption)
        post_id = post.get("id", "")
        channel = account.get("username_or_channel", "") or account.get("account_name", "")
        url = post.get("url") or f"https://www.twitch.tv/{channel}"
        return post_id, url

    if platform == "shopee":
        if format_type != "carousel":
            raise Exception("Shopee media upload supports image/carousel jobs only")
        uploader = ShopeeUploader(account_name=account.get("account_name", ""))
        images = _carousel_images(media)
        title = (job.get("title") or source.get("product_name") or "Product")[:100]
        results = uploader.upload_carousel(images or media, caption, title)
        first = results[0] if results else {}
        post_id = first.get("id", "")
        url = first.get("url", "")
        return post_id, url

    if platform == "lazada":
        if format_type != "carousel":
            raise Exception("Lazada media upload supports image/carousel jobs only")
        uploader = LazadaUploader(account_name=account.get("account_name", ""))
        images = _carousel_images(media)
        title = (job.get("title") or source.get("product_name") or "Product")[:100]
        results = uploader.upload_carousel(images or media, caption, title)
        first = results[0] if results else {}
        post_id = first.get("id", "")
        url = first.get("url", "")
        return post_id, url

    if platform == "x":
        uploader = XUploader(account_name=account.get("account_name", ""))
        title = (job.get("title") or source.get("product_name") or "Video")[:280]
        if format_type == "carousel":
            images = _carousel_images(media)
            post = uploader.upload_carousel(images or media, caption)
        else:
            is_video = format_type == "video"
            post = uploader.upload(media[0], caption, title=title, is_video=is_video)
        post_id = post.get("id", "")
        url = post.get("url", f"https://x.com/i/status/{post_id}")
        return post_id, url

    if platform == "linkedin":
        li_token = os.getenv(account.get("credential_property_key", "")) or os.getenv("LINKEDIN_ACCESS_TOKEN")
        uploader = LinkedInUploader(
            access_token=li_token,
            author=account.get("platform_account_id", ""),
            account_name=account.get("account_name", ""),
        )
        title = (job.get("title") or source.get("product_name") or "Video")[:100]
        if format_type == "carousel":
            images = _carousel_images(media)
            post = uploader.upload_carousel(images or media, caption)
        else:
            is_video = format_type == "video"
            post = uploader.upload(media[0], caption, title=title, is_video=is_video)
        post_id = post.get("id", "")
        url = post.get("url", f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}")
        return post_id, url

    raise Exception(f"Unsupported platform: {platform}")


def read_upload_guide(sheets):
    """Read the UPLOAD GUIDE before uploading and log its safety rules."""
    guide_rows = sheets.get_upload_guide()
    if not guide_rows:
        logger.error(f"UPLOAD GUIDE could not be read: {sheets.guide_error or 'tab empty'}")
        return []
    logger.info(f"UPLOAD GUIDE loaded ({len(guide_rows)} rows) before upload")
    for rule in sheets.guide_safety_rules(guide_rows):
        logger.info(f"[UPLOAD GUIDE] {rule}")
    return guide_rows


def insert_logs_newest_first(sheets, entries):
    """Write log entries under the header so the newest result is always on top."""
    if not entries:
        return
    rows = [
        [entry.get(col, "") for col in sheets.LOG_COLS]
        for entry in reversed(entries)
    ]
    targets = [(sheets.log_ws, sheets.log_header_row, "primary")]
    if getattr(sheets, "mirror_log_ws", None) is not None:
        targets.append((
            sheets.mirror_log_ws,
            sheets.mirror_log_header_row,
            "model-media mirror",
        ))
    for worksheet, header_row, label in targets:
        for attempt, delay in enumerate((0, 15, 30, 60), start=1):
            if delay:
                time.sleep(delay)
            try:
                worksheet.insert_rows(
                    rows, row=header_row + 1, value_input_option="USER_ENTERED"
                )
                break
            except Exception as exc:
                text = str(exc).lower()
                if ("429" not in text and "quota" not in text) or attempt == 4:
                    if label == "primary":
                        raise
                    logger.error("Could not mirror Publishing Log: %s", exc)
                    break
                logger.warning("%s Publishing Log insert hit quota; retrying: %s", label, exc)


def _round_robin_jobs(jobs, limit):
    """Select up to `limit` jobs, alternating across platforms.

    Without this, FIFO selection lets one platform's backlog (e.g. a large FB
    pending backlog sitting below newer IG/YT rows) starve the others, so
    those platforms never publish and produce no log entries.
    """
    by_platform = {}
    order = []
    for job in jobs:
        platform = job.get("platform", "")
        if platform not in by_platform:
            by_platform[platform] = []
            order.append(platform)
        by_platform[platform].append(job)
    for platform_jobs in by_platform.values():
        platform_jobs.sort(key=lambda job: (
            model_media_priority(job),
            int(job.get("attempts", 0) or 0),
            int(job.get("row", 0) or 0),
        ))
    selected = []
    while len(selected) < limit and any(by_platform.values()):
        for platform in order:
            if len(selected) >= limit:
                break
            if by_platform[platform]:
                selected.append(by_platform[platform].pop(0))
    return selected


def process_pending(sheets=None):
    try:
        sheets = sheets or open_sheets_with_retry()
    except Exception as e:
        logger.error(f"Sheets connection failed: {e}")
        return

    read_upload_guide(sheets)

    accounts = {a["account_id"]: a for a in sheets.get_accounts()}
    sources = sheets.get_source_rows()
    jobs = [job for job in sheets.get_pending_jobs() if is_model_media(job)]
    if not jobs:
        logger.info("No pending model photo/video jobs")
        return

    logger.info(f"Processing {len(jobs)} pending model photo/video job(s)")
    log_buffer = []
    try:
        for job in _round_robin_jobs(jobs, Config.MAX_JOBS_PER_RUN):
            job_id = job["job_id"]
            logger.info(f"Job {job_id}: {job['platform']}/{job['format']} - {job['media_selection']} (SKU {job['sku']})")

            account = accounts.get(job["account_id"])
            if not account or not account.get("enabled"):
                sheets.update_job(job, {"status": Config.JOB_STATUS_SKIPPED, "notes": "Account disabled or missing"})
                log_buffer.append(sheets.log_entry(job, "skipped", "Account disabled or missing"))
                logger.warning(f"Job {job_id}: skipped - account not enabled")
                continue

            source = sources.get(SheetsReader._normalize_sku(job.get("sku", "")))
            if not source:
                sheets.update_job(job, {"status": Config.JOB_STATUS_SKIPPED, "notes": "SKU not found in Source Import"})
                log_buffer.append(sheets.log_entry(job, "skipped", "SKU not found in Source Import"))
                logger.warning(f"Job {job_id}: skipped - SKU {job['sku']} missing")
                continue

            post_id, url = "", ""
            try:
                caption = build_caption(job, source, account)
                post_id, url = publish_job(job, source, account)
                updates = {
                    "status": Config.JOB_STATUS_UPLOADED,
                    "platform_post_id": post_id,
                    "published_url": url,
                    "caption_final": caption,
                    "tag_stock_id_used": _tag_value(job),
                    "tagging_status": "Tagged" if account.get("product_tagging") else "Unavailable",
                    "last_attempt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "error_message": "",
                }
                sheets.update_job(job, updates)
                log_buffer.append(sheets.log_entry(
                    {**job, "platform_post_id": post_id, "published_url": url},
                    "success",
                ))
                logger.info(f"Job {job_id}: uploaded -> {url}")
            except DeliveryUncertainError as e:
                message = str(e)
                sheets.update_job(job, {
                    "status": "hold",
                    "last_attempt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "error_message": message[:2000],
                    "notes": (
                        f"{job.get('notes', '')} | DELIVERY_UNCERTAIN: "
                        "platform may have accepted the post; do not auto-retry"
                    ).strip(" |"),
                })
                log_buffer.append(sheets.log_entry(job, "uncertain", message))
                logger.error(
                    f"Job {job_id}: delivery uncertain; held to prevent duplicate: {message}"
                )
            except InstagramRateLimitError as e:
                message = str(e)
                sheets.update_job(job, {
                    "status": "pending",
                    "attempts": int(job.get("attempts", 0) or 0),
                    "last_attempt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "error_message": message[:2000],
                    "notes": "Meta application limit; cooldown active, retry preserved",
                })
                log_buffer.append(sheets.log_entry(job, "deferred", message, "4/2207051"))
                logger.warning(
                    f"Job {job_id}: Instagram rate limited; keeping pending "
                    f"(no attempt consumed): {message}"
                )
            except IGAccountNotLinkedError as e:
                sheets.update_job(job, {
                    "status": "pending",
                    "last_attempt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "error_message": str(e)[:2000],
                    "notes": "IG business account not linked yet; waiting to retry",
                })
                logger.warning(
                    f"Job {job_id}: IG business account not linked, "
                    f"keeping pending (no attempt consumed): {e}"
                )
            except Exception as e:
                message = str(e)
                api_code = ""
                if "error" in message.lower():
                    try:
                        api_code = message.split("(")[-1].rstrip(")").split(" ")[0]
                    except Exception:
                        api_code = ""
                if post_id:
                    attempts = int(job.get("attempts", 0) or 0)
                    status = Config.JOB_STATUS_NEEDS_REVIEW
                    updates = {
                        "status": status,
                        "attempts": attempts,
                        "platform_post_id": post_id,
                        "published_url": url,
                        "last_attempt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "error_message": (
                            "Platform publish succeeded but final Sheets update failed: "
                            + message
                        )[:2000],
                        "notes": "DELIVERY_CONFIRMED_DO_NOT_RETRY; reconcile queue/log manually",
                    }
                else:
                    attempts = job.get("attempts", 0) + 1
                    if _is_broken_media_error(message):
                        status = Config.JOB_STATUS_NEEDS_REVIEW
                    else:
                        status = (
                            Config.JOB_STATUS_FAILED
                            if attempts >= Config.MAX_JOB_ATTEMPTS else "pending"
                        )
                    updates = {
                        "status": status,
                        "attempts": attempts,
                        "last_attempt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "error_message": message[:2000],
                        "tagging_status": "Failed"
                            if (status == Config.JOB_STATUS_FAILED and account.get("product_tagging"))
                            else job.get("tagging_status", "Pending"),
                    }
                sheets.update_job(job, updates)
                log_buffer.append(sheets.log_entry(job, "failed", message, api_code))
                logger.error(f"Job {job_id}: failed ({status}): {message}")

            time.sleep(max(10, int(os.getenv("UPLOAD_INTERVAL_SECONDS", "10"))))
    finally:
        if log_buffer:
            insert_logs_newest_first(sheets, log_buffer)
            logger.info(
                f"Inserted {len(log_buffer)} entr(y/ies) at top of Publishing Log"
            )


def run_generate(sheets=None):
    try:
        sheets = sheets or open_sheets_with_retry()
    except Exception as e:
        logger.error(f"Sheets connection failed: {e}")
        return
    read_upload_guide(sheets)
    accounts = sheets.get_accounts()
    sources = sheets.get_source_rows()
    existing = sheets.get_existing_job_keys()
    existing_job_ids = sheets.get_existing_job_ids()
    # Accounts with no queue history must not wait behind destinations that
    # already have thousands of jobs. Snapshot counts before adding missing
    # keys so empty/new queues (including LINE) are filled first.
    existing_counts = {}
    for key in existing:
        account_id = str(key[1] or "review")
        existing_counts[account_id] = existing_counts.get(account_id, 0) + 1

    missing_by_account = {}
    already_present = 0
    for job in generate_jobs(sources, accounts):
        key = job_unique_key(job)
        job_id = str(job.get("job_id", "") or "").strip()
        if job_id in existing_job_ids or key in existing:
            already_present += 1
            continue
        existing.add(key)
        existing_job_ids.add(job_id)
        account_id = str(job.get("account_id", "") or "review")
        missing_by_account.setdefault(account_id, []).append(job)

    for bucket in missing_by_account.values():
        bucket.sort(key=lambda job: (
            model_media_priority(job),
            str(job.get("media_selection", "") or ""),
            str(job.get("sku", "") or ""),
        ))

    # Select missing work fairly. The former first-N loop repeatedly exhausted
    # its cap on early Accounts rows, so newly added destinations such as Spain,
    # Italy, Vietnam, Pakistan, Kuwait and Dubai never received queue entries.
    # Rotate account windows each 10-minute production slot, then take one job
    # per account per pass until the generation cap is full.
    account_ids = list(missing_by_account)
    if account_ids:
        slot = int(time.time() // 600)
        start = (slot * max(1, Config.MAX_GENERATE_JOBS)) % len(account_ids)
        account_ids = account_ids[start:] + account_ids[:start]
        rotation_rank = {account_id: i for i, account_id in enumerate(account_ids)}
        account_ids.sort(
            key=lambda account_id: (
                existing_counts.get(account_id, 0) > 0,
                rotation_rank[account_id],
            )
        )

    new_jobs = []

    def _fill_missing(want_model, cap):
        ids = list(account_ids)
        while ids and len(new_jobs) < cap:
            next_pass = []
            for account_id in ids:
                bucket = missing_by_account[account_id]
                idx = next(
                    (
                        i for i, job in enumerate(bucket)
                        if is_model_media(job) is want_model
                    ),
                    None,
                )
                if idx is not None and len(new_jobs) < cap:
                    new_jobs.append(bucket.pop(idx))
                if any(is_model_media(job) is want_model for job in bucket):
                    next_pass.append(account_id)
            ids = next_pass

    _fill_missing(True, Config.MAX_GENERATE_JOBS)

    remaining = sum(len(bucket) for bucket in missing_by_account.values())
    if remaining:
        logger.info(
            "Generation cap reached (%s); %s missing job(s) remain for later fair rotations",
            Config.MAX_GENERATE_JOBS, remaining,
        )
    sheets.append_jobs(new_jobs)
    logger.info(
        f"Queue generation: {len(new_jobs)} new job(s) appended, "
        f"{already_present} already queued (skipped)"
    )


def run_cycle():
    """Generate missing jobs and upload pending jobs using ONE Sheets connection."""
    logger.info("=== Auto Upload: Generate + Upload Cycle ===")
    try:
        sheets = open_sheets_with_retry()
    except Exception as e:
        logger.error(f"Sheets connection failed after retries: {e}")
        return
    run_generate(sheets)
    # Reuse the same workbook/worksheet objects instead of reconnecting. Give
    # Google's per-minute read quota a fresh window before the publish phase.
    logger.info("Generation complete; waiting 65s for Sheets quota window before upload")
    time.sleep(65)
    process_pending(sheets)


def run_once():
    logger.info("=== Auto Upload: Single Run ===")
    process_pending()


def run_loop():
    interval = 300
    logger.info(f"=== Auto Upload: Loop every {interval}s ===")
    while True:
        process_pending()
        time.sleep(interval)


_PLATFORM_TARGETS = {
    "both": {"facebook", "instagram"},
    "all": {"facebook", "instagram", "youtube"},
}


def run_direct(media_url, caption, platform, product_url="", product_id=""):
    logger.info(f"=== Auto Upload: Direct Upload ({platform}) {media_url} ===")
    if not media_url:
        logger.error("No media URL provided")
        return
    is_video = any(e in media_url.lower() for e in _VIDEO_EXTS)
    target_platforms = _PLATFORM_TARGETS.get(platform, {platform})
    try:
        sheets = open_sheets_with_retry()
    except Exception as e:
        logger.error(f"Sheets connection failed: {e}")
        return
    read_upload_guide(sheets)
    accounts = [a for a in sheets.get_accounts() if a.get("enabled") and a.get("platform") in target_platforms]
    if not accounts:
        logger.warning("No enabled accounts for this platform")
        return
    for account in accounts:
        job = {
            "job_id": f"DIRECT-{account['platform']}-{int(time.time())}",
            "sku": product_id or "direct",
            "account_id": account["account_id"],
            "media_selection": "product_video" if is_video else "carousel",
            "platform": account["platform"],
            "format": "video" if is_video else "carousel",
            "stock_id_tag": product_id or "",
            "language": account.get("primary_language", ""),
        }
        source = {
            "sku": product_id or "direct",
            "video_url": media_url,
            "main_image": "" if is_video else media_url,
            "side_images": [],
            "images": [media_url],
            "model_videos": [],
            "certificate_media_url": "",
            "product_link": product_url or "",
            "product_name": (caption or "Video")[:100],
            "facebook_caption": caption or "",
            "instagram_caption": caption or "",
            "youtube_shorts_caption": caption or "",
            "hashtags": "",
            "lang_captions": {},
            "lang_hashtags": {},
        }
        try:
            post_id, url = publish_job(job, source, account)
            logger.info(f"[{account['account_id']}] direct upload -> {url}")
        except Exception as e:
            logger.error(f"[{account['account_id']}] direct upload failed: {e}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "--loop":
        run_loop()
    elif mode == "--generate":
        run_generate()
    elif mode == "--cycle":
        run_cycle()
    elif mode == "--direct":
        run_direct(
            os.getenv("MEDIA_URL", ""),
            os.getenv("CAPTION", ""),
            os.getenv("PLATFORM", "both").lower(),
            os.getenv("PRODUCT_URL", ""),
            os.getenv("PRODUCT_ID", ""),
        )
    else:
        run_once()
