"""Map Colour Diam FTP model-media files onto public HTTPS URLs."""
import ftplib
import logging
import os
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

DEFAULT_FTP_DIR = "Product/Jewellery/Model images"
DEFAULT_PUBLIC_BASE = "https://colourdiam.com/Product/Jewellery/Model%20images/"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_LISTING_CACHE = None


def ftp_config():
    return {
        "host": str(os.getenv("FTP_HOST", "") or "").strip(),
        "user": str(os.getenv("FTP_USER", "") or "").strip(),
        "password": str(os.getenv("FTP_PASSWORD", "") or "").strip(),
        "directory": str(os.getenv("FTP_MODEL_DIR", "") or "").strip() or DEFAULT_FTP_DIR,
        "public_base": (
            str(os.getenv("FTP_PUBLIC_BASE", "") or "").strip() or DEFAULT_PUBLIC_BASE
        ),
    }


def public_url(base, sku, filename):
    root = str(base or DEFAULT_PUBLIC_BASE).rstrip("/") + "/"
    sku_path = quote(str(sku or "").strip(), safe="_-")
    file_path = quote(unquote(str(filename or "").strip()), safe="._-")
    if not sku_path or not file_path:
        return ""
    return root + sku_path + "/" + file_path


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


def list_model_media(skus=None, client=None, config=None):
    """Return {sku: {"images": [https...], "videos": [https...]}} from FTP.

    Missing FTP credentials are a no-op so local tests and sheet-only runs
    keep working. Each SKU folder is listed once per process.
    """
    global _LISTING_CACHE
    cfg = config or ftp_config()
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        return {}
    wanted = {str(sku).strip() for sku in (skus or ()) if str(sku).strip()}
    if _LISTING_CACHE is not None and client is None:
        if not wanted:
            return _LISTING_CACHE
        return {sku: files for sku, files in _LISTING_CACHE.items() if sku in wanted}

    owned = client is None
    listing = {}
    ftp = None
    try:
        ftp = client or _connect(cfg)
        root = cfg["directory"].strip().rstrip("/")
        cwd = getattr(ftp, "cwd", None)
        if root and cwd:
            try:
                cwd(root)
            except Exception as exc:
                logger.warning("FTP model-media directory unavailable: %s", exc)
                return {}
        entries = [_basename(name) for name in _nlst(ftp, ".")]
        sku_dirs = [name for name in entries if name and name not in (".", "..")]
        if wanted:
            sku_dirs = [name for name in sku_dirs if name in wanted]
        for sku in sku_dirs:
            images = []
            videos = []
            seen = set()
            for raw in _nlst(ftp, sku):
                filename = _basename(raw)
                if not filename or filename in (".", ".."):
                    continue
                ext = _ext(filename)
                url = public_url(cfg["public_base"], sku, filename)
                if not url or url in seen:
                    continue
                seen.add(url)
                if ext in VIDEO_EXTS:
                    videos.append(url)
                elif ext in IMAGE_EXTS:
                    images.append(url)
            if images or videos:
                listing[sku] = {"images": images, "videos": videos}
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
        return {sku: files for sku, files in listing.items() if sku in wanted}
    return listing


def merge_ftp_model_media(sources, listing=None):
    """Add FTP-backed HTTPS URLs for SKUs already present in Source Import."""
    if not sources:
        return sources
    listing = listing if listing is not None else list_model_media(sources.keys())
    if not listing:
        return sources
    added = 0
    for sku, source in sources.items():
        files = listing.get(sku) or listing.get(str(sku).strip())
        if not files:
            continue
        images = list(source.get("model_images") or [])
        videos = list(source.get("model_videos") or [])
        have = {_basename(url).lower() for url in images + videos}
        for url in files.get("images") or []:
            name = _basename(url).lower()
            if name and name not in have:
                images.append(url)
                have.add(name)
                added += 1
        for url in files.get("videos") or []:
            name = _basename(url).lower()
            if name and name not in have:
                videos.append(url)
                have.add(name)
                added += 1
        source["model_images"] = images
        source["model_videos"] = videos
    if added:
        logger.info("Merged %s FTP model-media URL(s) onto Source Import rows", added)
    return sources
