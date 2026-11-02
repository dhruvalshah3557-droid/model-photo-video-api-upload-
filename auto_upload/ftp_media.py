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
    sku_dirs = [name for name in entries if name and name not in (".", "..")]
    if wanted:
        sku_dirs = [name for name in sku_dirs if name in wanted]
    listing = {}
    for sku in sku_dirs:
        images = []
        videos = []
        seen = set()
        for raw in _nlst(ftp, sku):
            filename = _basename(raw)
            if not filename or filename in (".", ".."):
                continue
            ext = _ext(filename)
            url = public_url(public_base, sku, filename)
            if not url or url in seen:
                continue
            seen.add(url)
            if ext in VIDEO_EXTS:
                videos.append(url)
            elif ext in IMAGE_EXTS:
                images.append(url)
        if images or videos:
            listing[sku] = {"images": images, "videos": videos}
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
    wanted = {str(sku).strip() for sku in (skus or ()) if str(sku).strip()}
    if _LISTING_CACHE is not None and client is None:
        if not wanted:
            return _LISTING_CACHE
        return {sku: files for sku, files in _LISTING_CACHE.items() if sku in wanted}

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
        return {sku: files for sku, files in listing.items() if sku in wanted}
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
        files = listing.get(sku) or listing.get(str(sku).strip())
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
