import datetime
import functools
import logging
import os
import random
import re
import threading
import time
from urllib.parse import quote, unquote, urlparse

import gspread
from config import Config
from oauth2client.service_account import ServiceAccountCredentials

logger = logging.getLogger(__name__)

_RETRY_STATUSES = (429, 500, 502, 503, 504)
_RETRY_MAX_ATTEMPTS = 8
_WRITE_INTERVAL = 1.15
_write_lock = threading.Lock()
_last_write = 0.0
_READ_INTERVAL = 0.35
_read_lock = threading.Lock()
_last_read = 0.0


def _throttle_write(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _last_write
        with _write_lock:
            elapsed = time.time() - _last_write
            if elapsed < _WRITE_INTERVAL:
                time.sleep(_WRITE_INTERVAL - elapsed)
            _last_write = time.time()
        return func(*args, **kwargs)
    return wrapper


def _throttle_read(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _last_read
        with _read_lock:
            elapsed = time.time() - _last_read
            if elapsed < _READ_INTERVAL:
                time.sleep(_READ_INTERVAL - elapsed)
            _last_read = time.time()
        return func(*args, **kwargs)
    return wrapper


def _retry_gsheet(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                return func(*args, **kwargs)
            except gspread.exceptions.APIError as err:
                response = getattr(err, "response", None)
                status = getattr(response, "status_code", None)
                if status not in _RETRY_STATUSES or attempt == _RETRY_MAX_ATTEMPTS:
                    raise
                if status == 429:
                    backoff = min(60.0, 10.0 * (2 ** (attempt - 1))) * (0.8 + 0.4 * random.random())
                else:
                    backoff = (2 ** attempt) * (0.5 + random.random())
                logger.warning(
                    f"Google Sheets API returned {status}; retrying in {backoff:.1f}s "
                    f"(attempt {attempt}/{_RETRY_MAX_ATTEMPTS})"
                )
                time.sleep(backoff)
    return wrapper


class SheetsReader:
    SCOPE = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    SOURCE_HEADER_ROW = 1
    HEADER_SCAN_ROWS = 5
    MIN_HEADER_MATCHES = 3

    ACCOUNTS_HEADER_COLS = [
        "account_id", "platform", "account_name", "platform_account_id",
        "username_or_channel", "primary_language", "fallback_language",
        "timezone", "enabled", "allowed_formats", "caption_style",
        "hashtag_set", "cta_rule", "posting_window", "min_gap_minutes",
        "product_tagging", "catalog_or_store_id", "credential_property_key",
        "approval_required", "notes",
    ]

    IMAGE_COLS = ["image1 link", "image2 link", "image3 link", "image4 link",
                  "image5 link", "image6 link", "image7 link", "image8 link"]
    MODEL_IMAGE_HEADERS = [
        "model image link 1", "model image link 2", "model image link 3",
        "multiple model photo link",
    ]
    MODEL_VIDEO_HEADERS = [
        "model video link 1", "model video link 2", "model video link 3",
        "multiple model video link",
    ]
    MODEL_IMAGE_INDEXES = (21, 22, 23, 24)
    MODEL_VIDEO_INDEXES = (25, 26, 27, 28)
    MODEL_IMAGE_DEFAULTS = ("1.jpeg", "2.jpeg", "3.jpeg", None)
    MODEL_VIDEO_DEFAULTS = ("video.mp4", "video-2.mp4", "video-3.mp4", None)
    MODEL_MEDIA_PREFIX = "https://colourdiam.com/Product/Model%20Photo%20Video/"
    MODEL_MEDIA_HTTP_FALLBACK_PREFIX = "https://colourdiam.com/Product/Jewellery/Model%20images/"
    _COLOURDIAM_HOST_RE = re.compile(r"(^|\.)colourdiam\.com$")
    _DEAD_MODEL_HOSTS = {
        "images.colourdiam.com",
        "videos.colourdiam.com",
        "cdn.colourdiam.com",
    }
    _SHEET_ERROR_CELLS = {"#ref!", "#n/a", "#value!", "#name?", "#div/0!"}
    _GLUED_URL_RE = re.compile(r"(?i)(?=https?://)")

    LANG_CAPTION_COLS = {
        "my": "Burmese Description", "th": "Thai Description",
        "tl": "Filipino Description", "fil": "Filipino Description",
        "zh": "Chinese Description", "ru": "Russian Description",
        "ja": "Japanese Description", "ko": "Korean Description",
        "he": "israli description", "es": "spanish description",
        "ar": "arabic description", "vi": "Vietnamese Description",
        "id": "Indonesian description", "it": "italian description",
        "fr": "french description", "de": "german description",
        "da": "danish description", "pl": "polish description",
        "tr": "turkish description", "sv": "sweden description",
        "lb": "lebenesse description", "cs": "vestslavic description",
    }
    LANG_TAG_COLS = {
        "my": "Burmese Hashtag", "th": "Thai Hashtag",
        "tl": "Filipino Hashtag", "fil": "Filipino Hashtag",
        "zh": "Chinese Hashtag", "ru": "Russian Hashtag",
        "ja": "Japanese Hashtag", "ko": "Korean Hashtag",
        "he": "israli hashtag", "es": "spanish hashtag",
        "ar": "arabic hashtag", "vi": "Vietnamese Hashtag",
        "id": "Indonesian hashtag", "it": "italian hashtag",
        "fr": "french hashtag", "de": "german hashtag",
        "da": "danish hashtag", "pl": "polish hahstag",
        "tr": "turkish hashtag", "sv": "sweden hashtag",
        "lb": "lebenesse hashtag", "cs": "vestslavic hashtag",
    }
    # Keep legacy/configured names above, while accepting the headers currently
    # used by Source Import. Header matching is exact after normalization.
    # Live workbook stores Turkish copy in the misnamed `greek description` /
    # `greek hashtag` columns (there is no `turkish description` header).
    LANG_CAPTION_ALIASES = {
        "vi": ("vietnam description",),
        "tr": ("greek description",),
        "lb": ("lebanese description", "lebanon description"),
        "cs": ("czech description", "west slavic description", "westslavic description"),
    }
    LANG_TAG_ALIASES = {
        "vi": ("vietnam hashtag",),
        "tr": ("greek hashtag",),
        "lb": ("lebanese hashtag", "lebanon hashtag"),
        "cs": ("czech hashtag", "west slavic hashtag", "westslavic hashtag"),
    }

    QUEUE_COLS = [
        "job_id", "sku", "account_id", "media_selection", "platform", "format",
        "language", "scheduled_at", "timezone", "stock_id_tag", "status",
        "attempts", "last_attempt_at", "platform_post_id", "published_url",
        "error_message", "notes", "tagging_status", "tag_stock_id_used",
        "caption_final",
    ]
    LOG_COLS = [
        "job_id", "attempt_time", "result", "platform_post_id", "published_url",
        "api_error_code", "error_message", "next_retry_at",
        "raw_response_reference", "notes",
    ]

    SKIP_STATUSES = {
        Config.JOB_STATUS_UPLOADED,
        Config.JOB_STATUS_FAILED,
        Config.JOB_STATUS_SKIPPED,
        Config.JOB_STATUS_NEEDS_REVIEW,
    }

    def __init__(self):
        self.client = self._authenticate()
        if Config.GOOGLE_SHEET_URL:
            sheet = self.client.open_by_url(Config.GOOGLE_SHEET_URL)
        else:
            sheet = self.client.open(Config.GOOGLE_SHEET_NAME)
        self.source_ws = sheet.worksheet(Config.SOURCE_IMPORT_SHEET)
        self.accounts_ws = sheet.worksheet(Config.ACCOUNTS_SHEET)
        self.queue_ws = sheet.worksheet(Config.QUEUE_SHEET)
        self.log_ws = sheet.worksheet(Config.LOG_SHEET)
        self.model_media_ws = None
        self.mirror_log_ws = None
        self.mirror_log_header_row = None
        if Config.MODEL_MEDIA_SHEET_URL:
            try:
                model_sheet = self.client.open_by_url(Config.MODEL_MEDIA_SHEET_URL)
                self.model_media_ws = model_sheet.worksheet(Config.MODEL_MEDIA_SOURCE_SHEET)
            except Exception as exc:
                # The primary workbook must remain usable if the supplemental
                # workbook is temporarily unavailable or has not yet been
                # shared with the service account.
                logger.warning("Model-media workbook unavailable: %s", exc)
        self.guide_ws = None
        self.guide_error = ""
        try:
            self.guide_ws = sheet.worksheet(Config.UPLOAD_GUIDE_SHEET)
        except Exception as e:
            self.guide_error = str(e)
        self.accounts_header_row = self._detect_header_row(self.accounts_ws, self.ACCOUNTS_HEADER_COLS)
        self.queue_header_row = self._detect_header_row(self.queue_ws, self.QUEUE_COLS)
        self.log_header_row = self._detect_header_row(self.log_ws, self.LOG_COLS)
        self.source_cols = self._col_map(self.source_ws, self.SOURCE_HEADER_ROW)
        self.accounts_cols = self._col_map(self.accounts_ws, self.accounts_header_row)
        self.queue_cols = self._col_map(self.queue_ws, self.queue_header_row)
        self.log_cols = self._col_map(self.log_ws, self.log_header_row)

    def _authenticate(self):
        creds_path = Config.GOOGLE_SHEET_CREDENTIALS
        if not os.path.exists(creds_path):
            raise FileNotFoundError(f"Service account credentials not found at: {creds_path}")
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, self.SCOPE)
        return gspread.authorize(creds)

    @staticmethod
    def _col_map(worksheet, header_row=1):
        headers = worksheet.row_values(header_row)
        return {str(h).strip().lower(): idx + 1 for idx, h in enumerate(headers)}

    @classmethod
    def _detect_header_row(cls, worksheet, expected_headers, scan_rows=None):
        scan_rows = scan_rows or cls.HEADER_SCAN_ROWS
        expected = {str(h).strip().lower() for h in expected_headers}
        best_row, best_score = None, 0
        for row in range(1, scan_rows + 1):
            values = worksheet.row_values(row)
            norm = {str(v).strip().lower() for v in values if str(v).strip()}
            score = len(norm & expected)
            if score > best_score:
                best_row, best_score = row, score
        if best_row is None or best_score < cls.MIN_HEADER_MATCHES:
            raise ValueError(
                f"Could not locate the header row in worksheet '{worksheet.title}' "
                f"(best row {best_row} matched {best_score} of at least "
                f"{cls.MIN_HEADER_MATCHES} expected header columns)"
            )
        headers = worksheet.row_values(best_row)
        normalized = [str(h).strip().lower() for h in headers if str(h).strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                f"Header row {best_row} in worksheet '{worksheet.title}' contains "
                f"duplicate column names; fix the sheet before continuing"
            )
        return best_row

    @staticmethod
    def _col_letter(col_idx):
        letter = ""
        while col_idx > 0:
            col_idx -= 1
            letter = chr(ord("A") + col_idx % 26) + letter
            col_idx //= 26
        return letter

    @staticmethod
    def _pick(row, *names):
        # gspread preserves header whitespace in get_all_records(). Regional
        # columns are maintained by people and may contain trailing spaces or
        # capitalization differences (for example "turkish hashtag "). Match
        # exact keys first, then normalize both configured and live headers.
        for name in names:
            v = row.get(name)
            if v is not None and str(v).strip():
                return str(v).strip()

        normalized = {
            str(key).strip().casefold(): value
            for key, value in (row or {}).items()
            if str(key).strip()
        }
        for name in names:
            v = normalized.get(str(name).strip().casefold())
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    @staticmethod
    def _split_lines(value):
        if not value:
            return []
        items = []
        for line in str(value).replace("\r", "").split("\n"):
            line = line.strip()
            if line:
                items.append(line)
        return items

    @classmethod
    def _split_urls(cls, value):
        """Split newline, comma, or glued http(s) media cells into one URL each."""
        text = str(value or "").strip()
        if not text or text.strip().casefold() in cls._SHEET_ERROR_CELLS:
            return []
        chunks = []
        for line in text.replace("\r", "").replace(",", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            pieces = [part.strip() for part in cls._GLUED_URL_RE.split(line) if part.strip()]
            if pieces:
                chunks.extend(pieces)
            else:
                chunks.append(line)
        urls = []
        seen = set()
        for chunk in chunks:
            if not chunk.lower().startswith("http"):
                continue
            if chunk not in seen:
                seen.add(chunk)
                urls.append(chunk)
        return urls

    @classmethod
    def _model_media_filename(cls, url, kind):
        raw = str(url or "").strip()
        if not raw:
            return "video.mp4" if kind == "video" else "1.jpeg"
        name = os.path.basename(unquote(urlparse(raw).path or "").rstrip("/"))
        if not name or name in (".", ".."):
            return "video.mp4" if kind == "video" else "1.jpeg"
        _stem, ext = os.path.splitext(name)
        if kind == "video":
            if ext.lower() not in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                return "video.mp4"
            return name
        if not ext:
            return name + ".jpeg"
        return name

    @classmethod
    def _is_colourdiam_host(cls, url):
        host = (urlparse(str(url or "")).hostname or "").strip().lower()
        return bool(host) and bool(cls._COLOURDIAM_HOST_RE.search(host))

    @classmethod
    def _is_model_photo_video_path(cls, url):
        path = unquote(urlparse(str(url or "")).path or "").lower()
        return "/product/model photo video/" in path

    @classmethod
    def _model_media_http_fallback(cls, url, sku=None):
        raw = str(url or "").strip()
        if not raw or not cls._is_model_photo_video_path(raw):
            return ""
        try:
            parsed = urlparse(raw)
        except Exception:
            return ""
        path = unquote(parsed.path or "").rstrip("/")
        parts = [part for part in path.split("/") if part]
        sku_folder = ""
        filename = ""
        for i, part in enumerate(parts):
            if part.lower() != "model photo video" or i + 1 >= len(parts):
                continue
            sku_folder = parts[i + 1]
            if i + 2 < len(parts):
                filename = parts[-1]
            break
        if not sku_folder and sku:
            sku_folder = str(sku).strip()
        if not filename:
            filename = os.path.basename(path)
        if not sku_folder or not filename:
            return ""
        if filename.lower() in {"model photo video", "product"}:
            return ""
        return (
            cls.MODEL_MEDIA_HTTP_FALLBACK_PREFIX
            + quote(sku_folder, safe="_-")
            + "/"
            + quote(filename, safe="._-")
        )

    @classmethod
    def _rewrite_model_media_url(cls, url, sku, kind, filename=None):
        raw = str(url or "").strip()
        if not raw or not sku:
            return raw
        try:
            parsed = urlparse(raw)
        except Exception:
            return raw
        host = (parsed.hostname or "").strip().lower()
        if host and not cls._is_colourdiam_host(raw):
            return raw
        if cls._is_model_photo_video_path(raw):
            # The FTP lookup supplies exact folder/file names, including known
            # compound-SKU folder aliases. Preserve this authoritative URL.
            return raw
        elif host in cls._DEAD_MODEL_HOSTS:
            name = filename or cls._model_media_filename(raw, kind)
        else:
            name = cls._model_media_filename(raw, kind)
        if not name:
            name = filename or ("video.mp4" if kind == "video" else "1.jpeg")
        sku_path = quote(str(sku).strip(), safe="_-")
        file_path = quote(str(name).strip(), safe="._-")
        return f"{cls.MODEL_MEDIA_PREFIX}{sku_path}/{file_path}"

    @classmethod
    def _collect_model_urls(cls, rec, names, sku, kind, raw_row=None, indexes=(), defaults=()):
        collected = []
        seen = set()
        for i, name in enumerate(names):
            default = defaults[i] if i < len(defaults) else None
            value = cls._pick(rec, name)
            if not value and raw_row is not None and i < len(indexes):
                index = indexes[i]
                if 0 <= index < len(raw_row):
                    value = raw_row[index]
            for url in cls._split_urls(value):
                rewritten = cls._rewrite_model_media_url(url, sku, kind, filename=default)
                if rewritten and rewritten not in seen:
                    seen.add(rewritten)
                    collected.append(rewritten)
        return collected

    @staticmethod
    def _normalize_sku(value):
        if value is None:
            return ""
        s = str(value).strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s

    @staticmethod
    def _sku_path_tokens(sku):
        text = str(sku or "").strip().lower()
        if not text:
            return []
        return [part for part in re.split(r"[_\-/]+", text) if part]

    @classmethod
    def _url_matches_sku(cls, url, sku):
        """True when a media/product URL belongs to this SKU.

        Compound SKUs such as 665_142 are stored as 665/142 in productdetail
        paths. A raw substring check treated those as a different product and
        blocked every paired jewellery row from auto-publish.
        """
        url_l = str(url or "").strip().lower()
        sku_l = str(sku or "").strip().lower()
        if not sku_l:
            return True
        if not url_l:
            return False
        if sku_l in url_l:
            return True
        tokens = cls._sku_path_tokens(sku_l)
        if len(tokens) <= 1:
            return False
        path = urlparse(url_l).path
        segments = [seg for seg in re.split(r"[/_\-]+", path) if seg]
        segment_set = set(segments)
        return all(token in segment_set for token in tokens)

    @staticmethod
    def _details_fields(details):
        fields = {}
        for line in str(details or "").replace("\r", "").split("\n"):
            line = line.strip()
            if not line:
                continue
            for separator in (" - ", ": "):
                if separator not in line:
                    continue
                key, value = line.split(separator, 1)
                key = key.strip().lower()
                value = value.strip()
                if key and value and len(key) <= 24:
                    fields[key] = value
                    break
        return fields

    @classmethod
    def _product_name_from_row(cls, rec, sku):
        """Build a real product title from Source Import after PRODUCT NAME vanished."""
        name = cls._pick(rec, "PRODUCT NAME", "Product Name", "product name")
        if name:
            return name
        details = cls._pick(rec, "DETAILS", "Details")
        code = cls._pick(rec, "CODE", "Code")
        fields = cls._details_fields(details)
        weight = fields.get("weight") or fields.get("carat") or fields.get("cts")
        colour = fields.get("colour") or fields.get("color") or code
        shape = fields.get("shape")
        clarity = fields.get("clarity")
        jewellery = "ring" in details.lower() or "18k" in details.lower()
        if weight and colour:
            parts = [f"{weight} Ct", colour]
            if shape:
                parts.append(shape)
            parts.append("Ring" if jewellery else "Diamond")
            if clarity:
                parts.append(clarity)
            return " ".join(part for part in parts if part)
        for line in str(details).replace("\r", "").split("\n"):
            line = line.strip()
            if line and not line.lower().startswith("sku"):
                return line[:120]
        if code:
            return f"{code} Diamond"
        return f"STK {sku} Diamond Jewelry" if sku else "Diamond Jewelry"

    @staticmethod
    def _is_center(url):
        return "center" in os.path.basename(str(url).split("?")[0]).lower()

    @_retry_gsheet
    def get_upload_guide(self):
        if self.guide_ws is None:
            return []
        return self.guide_ws.get_all_values()

    @staticmethod
    def guide_safety_rules(guide_rows):
        keywords = (
            "non certified", "do not publish", "do not auto-publish", "needs review",
            "block", "oauth", "business id", "disabled", "429", "error", "must not",
        )
        rules = []
        for row in guide_rows:
            for cell in row:
                text = str(cell or "").strip()
                if text and any(k in text.lower() for k in keywords):
                    rules.append(text)
                    break
        return rules

    @_retry_gsheet
    def get_accounts(self):
        records = self.accounts_ws.get_all_records(head=self.accounts_header_row)
        accounts = []
        for idx, rec in enumerate(records, start=self.accounts_header_row + 1):
            account_id = str(rec.get("account_id", "")).strip()
            if not account_id:
                continue
            enabled = str(rec.get("enabled", "")).strip().lower() in ("yes", "true", "1", "y")
            accounts.append({
                "row": idx,
                "account_id": account_id,
                "platform": str(rec.get("platform", "")).strip().lower(),
                "account_name": str(rec.get("account_name", "")).strip(),
                "platform_account_id": str(rec.get("platform_account_id", "")).strip(),
                "username_or_channel": str(rec.get("username_or_channel", "")).strip(),
                "primary_language": str(rec.get("primary_language", "")).strip(),
                "fallback_language": str(rec.get("fallback_language", "")).strip(),
                "timezone": str(rec.get("timezone", "")).strip(),
                "enabled": enabled,
                "allowed_formats": str(rec.get("allowed_formats", "")).strip(),
                "caption_style": str(rec.get("caption_style", "")).strip(),
                "hashtag_set": str(rec.get("hashtag_set", "")).strip(),
                "min_gap_minutes": str(rec.get("min_gap_minutes", "")).strip(),
                "product_tagging": str(rec.get("product_tagging", "")).strip().lower() in ("yes", "true", "1", "y"),
                "catalog_or_store_id": str(rec.get("catalog_or_store_id", "")).strip(),
                "credential_property_key": str(rec.get("credential_property_key", "")).strip(),
                "approval_required": str(rec.get("approval_required", "")).strip().lower() in ("yes", "true", "1", "y"),
                "notes": str(rec.get("notes", "")).strip(),
            })
        return accounts

    @_retry_gsheet
    def get_source_rows(self):
        # IMPORTRANGE/source changes can temporarily create duplicate headers.
        # gspread refuses get_all_records in that case and used to stop every
        # upload. Read raw values and keep the first occurrence of each header.
        values = self.source_ws.get_all_values()
        header_index = self.SOURCE_HEADER_ROW - 1
        if len(values) <= header_index:
            return {}
        columns = []
        seen_headers = set()
        for col_index, value in enumerate(values[header_index]):
            header = str(value or "").strip()
            if not header or header in seen_headers:
                continue
            seen_headers.add(header)
            columns.append((col_index, header))
        data_rows = values[header_index + 1:]
        records = [
            {
                header: (row[col_index] if col_index < len(row) else "")
                for col_index, header in columns
            }
            for row in data_rows
        ]
        sources = {}
        for idx, (rec, raw_row) in enumerate(
            zip(records, data_rows), start=self.SOURCE_HEADER_ROW + 1
        ):
            sku = self._normalize_sku(rec.get("STK", ""))
            if not sku:
                continue
            images = [v for v in (rec.get(c, "") for c in self.IMAGE_COLS) if str(v).strip()]
            images = [str(v).strip() for v in images]
            main_image = next(
                (u for u in images if self._is_center(u)),
                images[0] if images else "",
            )
            side_images = self._split_urls(rec.get("multiple side image link", ""))
            if not side_images:
                side_images = [
                    u for u in images
                    if u != main_image and not self._is_center(u)
                ]
            else:
                side_images = [
                    u for u in side_images
                    if u != main_image and not self._is_center(u)
                ]
            model_images = self._collect_model_urls(
                rec, self.MODEL_IMAGE_HEADERS, sku, "image",
                raw_row=raw_row, indexes=self.MODEL_IMAGE_INDEXES,
                defaults=self.MODEL_IMAGE_DEFAULTS,
            )
            model_videos = self._collect_model_urls(
                rec, self.MODEL_VIDEO_HEADERS, sku, "video",
                raw_row=raw_row, indexes=self.MODEL_VIDEO_INDEXES,
                defaults=self.MODEL_VIDEO_DEFAULTS,
            )
            certificate_media_url = self._pick(
                rec,
                "CERTIFICATE IMAGE LINK", "Certificate Image Link", "certificate image link",
                "CERTIFICATE MEDIA LINK", "Certificate Media Link", "certificate media link",
            )
            lang_captions = {
                code: self._pick(rec, col, *self.LANG_CAPTION_ALIASES.get(code, ()))
                for code, col in self.LANG_CAPTION_COLS.items()
            }
            lang_hashtags = {
                code: self._pick(rec, col, *self.LANG_TAG_ALIASES.get(code, ()))
                for code, col in self.LANG_TAG_COLS.items()
            }
            integrity_errors = []
            if main_image and not self._url_matches_sku(main_image, sku):
                integrity_errors.append(
                    f"main image belongs to another SKU ({main_image})"
                )
            mismatched_product_images = [
                url for url in images
                if "/product/jewellery/" in url.lower()
                and not self._url_matches_sku(url, sku)
            ]
            if mismatched_product_images:
                integrity_errors.append(
                    "product image set contains another SKU "
                    f"({mismatched_product_images[0]})"
                )
            product_link = str(rec.get("PRODUCT LINK", "")).strip()
            if product_link and not self._url_matches_sku(product_link, sku):
                integrity_errors.append(
                    f"product link belongs to another SKU ({product_link})"
                )
            details = self._pick(rec, "DETAILS", "Details")
            sources[sku] = {
                "row": idx,
                "sku": sku,
                "sr_no": str(rec.get("SR NO", "")).strip(),
                "lab": str(rec.get("LAB", "")).strip(),
                "certificate_id": str(rec.get("CERTIFICATE ID.", "")).strip(),
                "certificate_media_url": certificate_media_url,
                "source_status": str(rec.get("Status", "")).strip(),
                "product_link": product_link,
                "details": details,
                "product_name": self._product_name_from_row(rec, sku),
                "images": images,
                "main_image": main_image,
                "side_images": side_images,
                "video_url": str(rec.get("video link", "")).strip(),
                "multiple_video": self._split_lines(rec.get("multiple video link", "")),
                "model_images": model_images,
                "model_videos": model_videos,
                "facebook_caption": self._pick(rec, "FACEBOOK CAPTION", "Facebook Caption"),
                "instagram_caption": self._pick(rec, "INSTAGRAM CAPTION", "Instagram Caption"),
                "youtube_shorts_caption": self._pick(rec, "YouTube Shorts Caption", "YOUTUBE SHORTS CAPTION"),
                "youtube_product_id": self._pick(rec, "youtube_product_id", "YouTube Product ID", "YOUTUBE PRODUCT ID"),
                "hashtags": self._pick(rec, "HASHTAGS", "Hashtags"),
                "lang_captions": lang_captions,
                "lang_hashtags": lang_hashtags,
                "integrity_error": "; ".join(integrity_errors),
            }
        self._merge_model_media_rows(sources)
        return sources

    def _merge_model_media_rows(self, sources):
        """Merge supplemental model media by SKU without creating new products.

        The main Source Import remains authoritative for product copy, account
        scheduling, and duplicate prevention. The dedicated workbook can add
        model photos/videos only, so a copied queue can never publish twice.
        """
        if getattr(self, "model_media_ws", None) is None:
            return
        values = self.model_media_ws.get_all_values()
        if not values:
            return
        headers = [str(value or "").strip().casefold() for value in values[0]]
        positions = {header: idx for idx, header in enumerate(headers) if header}
        sku_col = positions.get("sku", positions.get("stock id"))
        if sku_col is None:
            logger.warning("Model-media source has no SKU/Stock ID header; ignoring it")
            return
        photo_cols = [
            positions[name] for name in (
                "model image link 1", "model image link 2", "model image link 3",
                "multiple model photo link", "model photo links",
            ) if name in positions
        ]
        video_cols = [
            positions[name] for name in (
                "model video link 1", "model video link 2", "model video link 3",
                "multiple model video link", "model video links",
            ) if name in positions
        ]
        for row in values[1:]:
            sku = self._normalize_sku(row[sku_col] if sku_col < len(row) else "")
            source = sources.get(sku)
            if not source:
                continue
            for col in photo_cols:
                value = row[col] if col < len(row) else ""
                for url in self._split_urls(value):
                    if url not in source["model_images"]:
                        source["model_images"].append(url)
            for col in video_cols:
                value = row[col] if col < len(row) else ""
                for url in self._split_urls(value):
                    if url not in source["model_videos"]:
                        source["model_videos"].append(url)

    def get_source_row(self, sku):
        return self.get_source_rows().get(self._normalize_sku(sku))

    @_retry_gsheet
    def get_pending_jobs(self):
        records = self.queue_ws.get_all_records(head=self.queue_header_row)
        jobs = []
        for idx, rec in enumerate(records, start=self.queue_header_row + 1):
            status = str(rec.get("status", "")).strip().lower()
            if status in self.SKIP_STATUSES:
                continue
            job_id = str(rec.get("job_id", "")).strip()
            if not job_id:
                continue
            media_selection = str(rec.get("media_selection", "")).strip()
            if not (
                media_selection.startswith("model_video:")
                or media_selection.startswith("model_photo:")
            ):
                continue
            jobs.append({
                "row": idx,
                "job_id": job_id,
                "sku": self._normalize_sku(rec.get("sku", "")),
                "account_id": str(rec.get("account_id", "")).strip(),
                "media_selection": media_selection,
                "platform": str(rec.get("platform", "")).strip().lower(),
                "format": str(rec.get("format", "")).strip().lower(),
                "language": str(rec.get("language", "")).strip(),
                "timezone": str(rec.get("timezone", "")).strip(),
                "stock_id_tag": self._normalize_sku(rec.get("stock_id_tag", "")),
                "tagging_status": str(rec.get("tagging_status", "")).strip() or "Pending",
                "tag_stock_id_used": self._normalize_sku(rec.get("tag_stock_id_used", "")),
                "caption_final": str(rec.get("caption_final", "")).strip(),
                "attempts": int(rec.get("attempts") or 0) if str(rec.get("attempts") or "").strip().isdigit() else 0,
                "last_attempt_at": str(rec.get("last_attempt_at", "") or "").strip(),
                "error_message": str(rec.get("error_message", "") or "").strip(),
                "notes": str(rec.get("notes", "")).strip(),
            })
        return jobs

    @_retry_gsheet
    def find_job(self, sku, account_id, platform, fmt, media_selection):
        records = self.queue_ws.get_all_records(head=self.queue_header_row)
        for idx, rec in enumerate(records, start=self.queue_header_row + 1):
            if (self._normalize_sku(rec.get("sku", "")) == self._normalize_sku(sku)
                    and str(rec.get("account_id", "")).strip() == account_id
                    and str(rec.get("platform", "")).strip().lower() == platform
                    and str(rec.get("format", "")).strip().lower() == fmt
                    and str(rec.get("media_selection", "")).strip() == media_selection):
                return idx
        return None

    @_retry_gsheet
    def get_existing_job_keys(self):
        keys = set()
        records = self.queue_ws.get_all_records(head=self.queue_header_row)
        for rec in records:
            sku = self._normalize_sku(rec.get("sku", ""))
            if not sku:
                continue
            keys.add((sku, str(rec.get("account_id", "")).strip(),
                      str(rec.get("platform", "")).strip().lower(),
                      str(rec.get("format", "")).strip().lower(),
                      str(rec.get("media_selection", "")).strip()))
        return keys

    @_retry_gsheet
    def get_existing_job_ids(self):
        """Return stable queue IDs, independent of mutable media metadata."""
        records = self.queue_ws.get_all_records(head=self.queue_header_row)
        return {
            str(rec.get("job_id", "") or "").strip()
            for rec in records
            if str(rec.get("job_id", "") or "").strip()
        }

    @_retry_gsheet
    @_throttle_write
    def append_jobs(self, jobs):
        if not jobs:
            return
        rows = [[job.get(col, "") for col in self.QUEUE_COLS] for job in jobs]
        for start in range(0, len(rows), 200):
            self.queue_ws.append_rows(rows[start:start + 200], value_input_option="USER_ENTERED")

    @_retry_gsheet
    @_throttle_write
    def update_job(self, job, updates):
        row = job.get("row") if isinstance(job, dict) else job
        if not row:
            return
        data = []
        for key, value in updates.items():
            col_idx = self.queue_cols.get(str(key).strip().lower())
            if not col_idx:
                continue
            data.append({
                "range": f"{self._col_letter(col_idx)}{row}",
                "values": [[value]],
            })
        if not data:
            return
        self.queue_ws.batch_update(data, value_input_option="USER_ENTERED")

    @_retry_gsheet
    @_throttle_write
    def write_log(self, entry):
        row = [entry.get(col, "") for col in self.LOG_COLS]
        self.log_ws.append_row(row, value_input_option="USER_ENTERED")
        if getattr(self, "mirror_log_ws", None) is not None:
            self.mirror_log_ws.append_row(row, value_input_option="USER_ENTERED")

    @_retry_gsheet
    @_throttle_write
    def append_logs(self, entries):
        if not entries:
            return
        rows = [[entry.get(col, "") for col in self.LOG_COLS] for entry in entries]
        for start in range(0, len(rows), 200):
            self.log_ws.append_rows(rows[start:start + 200], value_input_option="USER_ENTERED")
            if getattr(self, "mirror_log_ws", None) is not None:
                self.mirror_log_ws.append_rows(
                    rows[start:start + 200], value_input_option="USER_ENTERED"
                )

    @staticmethod
    def _normalize_api_error_code(error_message="", api_error_code=""):
        """Return a stable, useful error code for Publishing Log."""
        message = str(error_message or "")
        lower = message.lower()
        supplied = str(api_error_code or "").strip()

        if "invalid_client" in lower:
            return "YOUTUBE_INVALID_CLIENT"
        if "error validating access token" in lower or "session has been invalidated" in lower:
            return "META_TOKEN_INVALID"
        if "pages_manage_posts" in lower or "no permission to publish" in lower:
            return "META_PERMISSION"
        if "unpublished posts must be posted to a page" in lower:
            return "META_PAGE_TOKEN_REQUIRED"
        if "name resolution" in lower or "failed to resolve" in lower or "name or service not known" in lower:
            return "DNS_ERROR"
        if "404" in lower or supplied == "404":
            return "HTTP_404"
        if "429" in lower or "quota" in lower or "rate limit" in lower:
            return "RATE_LIMIT"
        if "pinterest" in lower and ("401" in lower or "authentication failed" in lower):
            return "PINTEREST_AUTH"
        if "boards:write" in lower or "pins:write" in lower:
            return "PINTEREST_SCOPE"
        if "only photo or video can be accepted" in lower:
            return "MEDIA_TYPE_INVALID"
        if "media validation failed" in lower or "broken container" in lower or "transcoding" in lower:
            return "MEDIA_INVALID"
        if "all resolved media urls are unavailable" in lower or "dead links" in lower:
            return "MEDIA_UNAVAILABLE"
        if "timeout" in lower:
            return "TIMEOUT"
        if "fatal" == lower.strip():
            return "PLATFORM_FATAL"
        if "invalid parameter" in lower:
            return "INVALID_PARAMETER"

        junk = {"", "error", "[errno", "pinterest", "fatal"}
        if supplied.lower() not in junk:
            return supplied[:80]
        return "UNKNOWN_ERROR" if message else ""

    def log_entry(self, job, result, error_message="", api_error_code="", notes=""):
        now = datetime.datetime.utcnow().isoformat()
        normalized_code = self._normalize_api_error_code(error_message, api_error_code)
        return {
            "job_id": job.get("job_id", ""),
            "attempt_time": now,
            "result": result,
            "platform_post_id": job.get("platform_post_id", ""),
            "published_url": job.get("published_url", ""),
            "api_error_code": normalized_code,
            "error_message": error_message,
            "next_retry_at": "",
            "raw_response_reference": "",
            "notes": notes,
        }
