"""Map Colour Diam FTP model-media files onto public HTTPS URLs."""
import ftplib
import logging
import os
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

DEFAULT_FTP_DIRS = (
    "Product/Model Photo Video",
    "Product/Jewellery/Model images",
)
DEFAULT_PUBLIC_BASES = (
    "https://colourdiam.com/Product/Model%20Photo%20Video/",
    "https://colourdiam.com/Product/Jewellery/Model%20images/",
)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_LISTING_CACHE = None


def normalize_sku(value):
    """Match FTP folders/files to Source Import STK (298.0 -> 298)."""
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _split_csv(value):
    text = str(value or "").strip()
    if not text:
        return []
    parts = []
    for chunk in text.replace("\r", "\n").replace(";", "\n").replace(",", "\n").split("\n"):
        item = chunk.strip().strip("/")
        if item and item not in parts:
            parts.append(item)
    return parts


def ftp_config():
    directories = _split_csv(os.getenv("FTP_MODEL_DIR", "")) or list(DEFAULT_FTP_DIRS)
    bases = _split_csv(os.getenv("FTP_PUBLIC_BASE", "")) or list(DEFAULT_PUBLIC_BASES)
    if len(bases) < len(directories):
        bases.extend([DEFAULT_PUBLIC_BASES[-1]] * (len(directories) - len(bases)))
    return {
        "host": str(os.getenv("FTP_HOST", "") or "").strip(),
        "user": str(os.getenv("FTP_USER", "") or "").strip(),
        "password": str(os.getenv("FTP_PASSWORD", "") or "").strip(),
        "directories": directories,
        "public_bases": [base.rstrip("/") + "/" for base in bases],
        "directory": directories[0],
        "public_base": bases[0].rstrip("/") + "/",
    }


def public_url(base, sku, filename):
    root = str(base or DEFAULT_PUBLIC_BASES[0]).rstrip("/") + "/"
    sku_path = quote(str(sku or "").strip(), safe="_-")
    file_path = quote(unquote(str(filename or "").strip()), safe="._-")
    if not sku_path or not file_path:
        return ""
    return root + sku_path + "/" + file_path


def public_file_url(base, filename):
    root = str(base or DEFAULT_PUBLIC_BASES[0]).rstrip("/") + "/"
    file_path = quote(unquote(str(filename or "").strip()), safe="._-")
    if not file_path:
        return ""
    return root + file_path


def _basename(name):
    text = unquote(str(name or "").replace("\\", "/").rstrip("/"))
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip()


def _ext(name):
    text = _basename(name).lower()
    if "." not in text:
        return ""
    return "." + text.rsplit(".", 1)[-1]


def _stem(name):
    filename = _basename(name)
    ext = _ext(filename)
    if not ext:
        return filename.strip()
    return filename[: len(filename) - len(ext)].strip()


def _is_media_file(name):
    return _ext(name) in VIDEO_EXTS or _ext(name) in IMAGE_EXTS


def _add_media(bucket, filename, url):
    if not bucket or not url or url in bucket["images"] + bucket["videos"]:
        return False
    ext = _ext(filename)
    if ext in VIDEO_EXTS:
        bucket["videos"].append(url)
        return True
    if ext in IMAGE_EXTS:
        bucket["images"].append(url)
        return True
    return False


def _wanted_keys(wanted):
    return {normalize_sku(sku) for sku in (wanted or ()) if str(sku).strip()}


def _sku_from_filename(filename, wanted):
    stem = normalize_sku(_stem(filename))
    if not stem:
        return ""
    keys = _wanted_keys(wanted)
    if stem in keys:
        return stem
    for sku in keys:
        if stem.startswith(sku + "_") or stem.startswith(sku + "-"):
            return sku
    if keys:
        return ""
    return stem


def _connect(cfg, factory=None):
    factory = factory or ftplib.FTP
    client = factory()
    client.connect(cfg["host"], timeout=20)
    client.login(cfg["user"], cfg["password"])
    client.set_pasv(True)
    return client


def _nlst(client, path):
    try:
        return list(client.nlst(path) or [])
    except Exception:
        try:
            return list(client.nlst() or [])
        except Exception:
            return []


def _list_one_directory(ftp, root, public_base, wanted):
    cwd = getattr(ftp, "cwd", None)
    if root and cwd:
        try:
            cwd("/")
        except Exception:
            pass
        try:
            cwd(root)
        except Exception as exc:
            logger.warning("FTP model-media directory unavailable (%s): %s", root, exc)
            return {}
    entries = [_basename(name) for name in _nlst(ftp, ".")]
    wanted_keys = _wanted_keys(wanted)
    listing = {}
    for name in entries:
        if not name or name in (".", ".."):
            continue
        if _is_media_file(name):
            sku = _sku_from_filename(name, wanted_keys)
            if not sku:
                continue
            url = public_file_url(public_base, name)
            bucket = listing.setdefault(sku, {"images": [], "videos": []})
            _add_media(bucket, name, url)
            if not bucket["images"] and not bucket["videos"]:
                listing.pop(sku, None)
            continue
        sku = normalize_sku(name)
        if wanted_keys and sku not in wanted_keys and name not in wanted:
            continue
        bucket = listing.setdefault(sku, {"images": [], "videos": []})
        for raw in _nlst(ftp, name):
            filename = _basename(raw)
            if not filename or filename in (".", ".."):
                continue
            url = public_url(public_base, name, filename)
            _add_media(bucket, filename, url)
        if not bucket["images"] and not bucket["videos"]:
            listing.pop(sku, None)
    return listing


def _merge_listings(primary, extra):
    if not extra:
        return primary
    for sku, files in extra.items():
        current = primary.setdefault(sku, {"images": [], "videos": []})
        have = {_basename(url).lower() for url in current["images"] + current["videos"]}
        for url in files.get("images") or []:
            name = _basename(url).lower()
            if name and name not in have:
                current["images"].append(url)
                have.add(name)
        for url in files.get("videos") or []:
            name = _basename(url).lower()
            if name and name not in have:
                current["videos"].append(url)
                have.add(name)
    return primary


def list_model_media(skus=None, client=None, config=None):
    """Return {sku: {"images": [https...], "videos": [https...]}} from FTP.

    Missing FTP credentials are a no-op so local tests and sheet-only runs
    keep working. Each SKU folder is listed once per process. The main folder
    list is tried in order: Model Photo Video, then Jewellery/Model images.
    """
    global _LISTING_CACHE
    cfg = config or ftp_config()
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        return {}
    wanted = {normalize_sku(sku) for sku in (skus or ()) if str(sku).strip()}
    if _LISTING_CACHE is not None and client is None:
        if not wanted:
            return _LISTING_CACHE
        return {
            sku: files for sku, files in _LISTING_CACHE.items()
            if normalize_sku(sku) in wanted
        }

    directories = list(cfg.get("directories") or ([cfg.get("directory")] if cfg.get("directory") else DEFAULT_FTP_DIRS))
    bases = list(cfg.get("public_bases") or ([cfg.get("public_base")] if cfg.get("public_base") else DEFAULT_PUBLIC_BASES))
    if len(bases) < len(directories):
        bases.extend([DEFAULT_PUBLIC_BASES[-1]] * (len(directories) - len(bases)))

    owned = client is None
    listing = {}
    ftp = None
    try:
        ftp = client or _connect(cfg)
        for root, public_base in zip(directories, bases):
            listing = _merge_listings(
                listing,
                _list_one_directory(ftp, str(root or "").strip().rstrip("/"), public_base, wanted),
            )
    except Exception as exc:
        logger.warning("FTP model-media listing failed: %s", exc)
        return {}
    finally:
        if owned and ftp is not None:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    if client is None and not wanted:
        _LISTING_CACHE = listing
    if wanted:
        return {
            sku: files for sku, files in listing.items()
            if normalize_sku(sku) in wanted
        }
    return listing


def merge_ftp_model_media(sources, listing=None):
    """Replace model photo/video URLs with files listed in the FTP folder."""
    if not sources:
        return sources
    listing = listing if listing is not None else list_model_media(sources.keys())
    if not listing:
        for source in sources.values():
            source["model_images"] = []
            source["model_videos"] = []
        return sources
    replaced = 0
    for sku, source in sources.items():
        key = normalize_sku(sku)
        files = (
            listing.get(sku)
            or listing.get(str(sku).strip())
            or listing.get(key)
        )
        if not files:
            source["model_images"] = []
            source["model_videos"] = []
            continue
        source["model_images"] = list(files.get("images") or [])
        source["model_videos"] = list(files.get("videos") or [])
        replaced += 1
    if replaced:
        logger.info("Loaded model media for %s SKU(s) from FTP folder", replaced)
    return sources
