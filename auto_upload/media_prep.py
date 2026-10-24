import logging
import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from http.client import IncompleteRead
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

try:
    from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout
except ImportError:
    ChunkedEncodingError = getattr(requests, "ChunkedEncodingError", Exception)
    ConnectionError = getattr(requests, "ConnectionError", Exception)
    Timeout = getattr(requests, "Timeout", Exception)

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DOWNLOAD_HEADERS = {
    "User-Agent": USER_AGENT,
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}
_DOWNLOAD_RETRIES = 3
_TRANSIENT_DOWNLOAD_ERRORS = (
    ChunkedEncodingError,
    ConnectionError,
    Timeout,
    IncompleteRead,
)

_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")

_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"RIFF", "WebP/RIFF"),
)

_VIDEO_MAGIC = (
    (b"\x1a\x45\xdf\xa3", "WebM/MKV"),
    (b"RIFF", "AVI"),
)

_IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")
_VIDEO_TYPES = (
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
    "video/mov",
    "video/mpeg",
)

REELS_WIDTH, REELS_HEIGHT = 1080, 1920
# Only replace a soundtrack when it is genuinely inaudible.  The previous
# -45 dB cutoff treated quiet camera/ambient audio as silence and replaced the
# model video's original sound.
SILENCE_THRESHOLD_DB = -80.0
_AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg")
_CC0_SOURCE = (
    "https://raw.githubusercontent.com/effacestudios/"
    "Royalty-Free-Music-Pack/2ce8458293fe4eeb91414a19d6d7ecd1562a5949"
)
# Every file at this pinned commit is covered by the repository's CC0-1.0
# licence.  Keep the commit immutable and the explicit allow-list auditable:
# never discover or download arbitrary music at runtime.
_BUNDLED_CC0_TRACK_NAMES = (
    "Beeper.mp3",
    "Bubbles.mp3",
    "Cinemato.mp3",
    "Dubstepper.mp3",
    "Fury.mp3",
    "Gamer Guy.mp3",
    "Happy Life.mp3",
    "Illusionist.mp3",
    "My Inventions.mp3",
    "Mysterious.mp3",
    "Newness.mp3",
    "Outsider.mp3",
    "Party Time.mp3",
    "Planning.mp3",
    "Science Fiction.mp3",
    "Sports Spirit.mp3",
    "Starter.mp3",
    "Sudden Tour.mp3",
    "THE CLOWN.mp3",
    "The Champion.mp3",
    "The Mystery.mp3",
    "The Templer.mp3",
    "Unexpected.mp3",
    "Unknown.mp3",
    "Unpredicted.mp3",
    "Worship Me.mp3",
    "Yo Vender Music.mp3",
    "alarming.mp3",
    "biography.mp3",
    "breaker.mp3",
    "commercial.mp3",
    "lunatic.mp3",
    "my snares.mp3",
    "our show.mp3",
    "player.mp3",
    "recipe.mp3",
    "reminer.mp3",
    "respectation.mp3",
    "right after.mp3",
    "slow down.mp3",
    "technologist.mp3",
)
BUNDLED_CC0_MUSIC_URLS = tuple(
    f"{_CC0_SOURCE}/{quote(name)}" for name in _BUNDLED_CC0_TRACK_NAMES
)

# Additional immutable CC0 fallback pack. The repository publishes both the
# rendered tracks and their source under CC0 1.0; pinning the commit prevents
# an upstream file from changing after review.
_DURU_CC0_SOURCE = (
    "https://raw.githubusercontent.com/uncle-sheepsky/"
    "duru-ai-cc0-bgm/1f63fef279098a082f2f77b3f74940b2784d37b7/mp3"
)
_DURU_CC0_TRACK_NAMES = (
    "duru-ai-ep2-music.mp3", "duru-arcade-vibe.mp3",
    "duru-denparcade.mp3", "duru-rhythm-fever.mp3", "duru-rondo.mp3",
    "duru-roomscene-lofi.mp3", "duru-winter-arcade.mp3",
    "hyak-ep1-rhythm.mp3", "hyak-ep2-korobeiniki-tetris.mp3",
    "hyak-ep3-suika.mp3", "hyak-ep4-blackhole.mp3", "hyak-ep5-2048.mp3",
    "hyak-ep6-galaxy.mp3", "hyak-ep7-danmaku.mp3",
    "korobeiniki-arcade-fullver.mp3", "korobeiniki-boombap-loop.mp3",
    "mountainking-arcade-loop.mp3",
)
DURU_CC0_MUSIC_URLS = tuple(
    f"{_DURU_CC0_SOURCE}/{quote(name)}" for name in _DURU_CC0_TRACK_NAMES
)
BUNDLED_CC0_MUSIC_URLS = tuple(dict.fromkeys(
    BUNDLED_CC0_MUSIC_URLS + DURU_CC0_MUSIC_URLS
))


def _env(key, default=""):
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


def _env_true(key, default="true"):
    return _env(key, default).lower() in ("true", "1", "yes", "on")


def _fresh_media_url(media_url, token=None):
    """Return a one-use URL so replaced source files cannot be served from stale CDN cache."""
    parts = urlsplit(media_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("_cd_refresh", str(token if token is not None else time.time_ns())))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _request_get(url, headers=None, timeout=180, stream=False):
    """GET with retries for truncated Colour Diam CDN responses."""
    last_error = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers=headers or DOWNLOAD_HEADERS,
                stream=stream,
            )
            resp.raise_for_status()
            if stream:
                return resp
            content = resp.content
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            expected = resp.headers.get("Content-Length")
            if expected and not encoding and len(content) < int(expected):
                raise IncompleteRead(content, int(expected) - len(content))
            return resp
        except _TRANSIENT_DOWNLOAD_ERRORS as exc:
            last_error = exc
            logger.warning(
                "Media download attempt %s/%s failed for %s: %s",
                attempt, _DOWNLOAD_RETRIES, url, exc,
            )
            if attempt == _DOWNLOAD_RETRIES:
                break
            time.sleep(min(8, 2 * attempt))
    raise last_error


def _download_bytes(media_url):
    """Download fresh original file bytes, bypassing stale URL/CDN cache entries."""
    resp = _request_get(_fresh_media_url(media_url), timeout=180, headers=DOWNLOAD_HEADERS)
    name = media_url.split("?")[0].rsplit("/", 1)[-1] or "media"
    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    return name, resp.content, content_type


def _probe_duration(video_path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 30.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", video_path],
            check=False, capture_output=True, text=True, timeout=60,
        )
        return max(0.5, float((out.stdout or "").strip()))
    except Exception:
        return 30.0


def audio_state(video_path):
    """Return audible, silent, missing, or unknown for a local video.

    This detects both videos with no audio stream and videos that technically
    contain an audio stream but are effectively muted/silent.
    """
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe:
        return "unknown"
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path],
            check=False, capture_output=True, text=True, timeout=60,
        )
        if not (probe.stdout or "").strip():
            return "missing"
        if not ffmpeg:
            return "audible"

        detect = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-t", "30", "-i", video_path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
            check=False, capture_output=True, text=True, timeout=90,
        )
        text = (detect.stderr or "") + "\n" + (detect.stdout or "")
        match = re.search(r"max_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", text, flags=re.I)
        if not match:
            return "audible"
        raw = match.group(1).lower()
        if raw in ("-inf", "inf"):
            return "silent"
        max_db = float(raw)
        return "silent" if max_db <= SILENCE_THRESHOLD_DB else "audible"
    except Exception as exc:
        logger.warning(f"Audio analysis failed: {exc}")
        return "unknown"


def _download_music_url(url, out_path):
    resp = _request_get(url, timeout=180, headers={"User-Agent": USER_AGENT})
    Path(out_path).write_bytes(resp.content)
    return out_path


def _stable_choice(items, media_key):
    """Choose repeatably from a library while distributing different videos."""
    digest = hashlib.sha256(media_key.encode("utf-8", errors="ignore")).digest()
    return items[int.from_bytes(digest[:8], "big") % len(items)]


def _configured_music_urls():
    raw = _env("BACKGROUND_MUSIC_URLS", "")
    return [part.strip() for part in re.split(r"[\n,]+", raw) if part.strip()]


def _music_url_library():
    """Merge configured licensed tracks with the pinned CC0 fallback library."""
    return list(dict.fromkeys(_configured_music_urls() + list(BUNDLED_CC0_MUSIC_URLS)))


def _music_files(path):
    root = Path(path)
    if root.is_file() and root.suffix.lower() in _AUDIO_EXTS:
        return [root]
    if root.is_dir():
        return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)
    return []


def _resolve_music(media_key, temp_paths):
    """Choose real music from configured sources or a pinned CC0 library."""
    audio_urls = _music_url_library()
    local_path = _env("BACKGROUND_MUSIC_PATH", "")

    if audio_urls:
        audio_url = _stable_choice(audio_urls, media_key)
        fd, path = tempfile.mkstemp(suffix=".music")
        os.close(fd)
        temp_paths.append(path)
        _download_music_url(audio_url, path)
        source = "configured licensed" if audio_url in _configured_music_urls() else "pinned CC0"
        track_id = Path(audio_url.split("?")[0]).name
        logger.info(
            "Selected %s music %d/%d track=%s",
            source, audio_urls.index(audio_url) + 1, len(audio_urls), track_id,
        )
        return path

    music_files = _music_files(local_path) if local_path else []
    if music_files:
        selected = _stable_choice(music_files, media_key)
        logger.info("Selected configured music track: %s", selected.name)
        return str(selected)

    raise RuntimeError("No usable background music source is configured")


def _mix_music(video_path, music_path, out_path, media_key, volume=0.72):
    """Attach background music to a silent/muted video."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to add audio to silent video")
    video_duration = _probe_duration(video_path)
    music_duration = _probe_duration(music_path)
    usable_start = max(0.0, music_duration - video_duration - 1.0)
    digest = hashlib.sha256((media_key + "|offset").encode("utf-8", errors="ignore")).digest()
    start_offset = (int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)) * usable_start
    fade_out_start = max(0.0, video_duration - 0.8)
    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-ss", f"{start_offset:.3f}", "-i", music_path,
        "-filter_complex",
        (
            f"[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume={volume},"
            f"afade=t=in:st=0:d=0.35,afade=t=out:st={fade_out_start}:d=0.8[a]"
        ),
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
        out_path,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="ignore")[-800:]
        raise RuntimeError(f"Automatic audio mix failed: {detail}")
    logger.info("Added real background music (start %.1fs)", start_offset)
    return out_path


def _stream_rotation(stream):
    """Return display rotation in degrees from tags or side data."""
    tags = stream.get("tags") or {}
    raw = tags.get("rotate")
    if raw not in (None, ""):
        try:
            return abs(int(float(raw))) % 180
        except (TypeError, ValueError):
            pass
    for item in stream.get("side_data_list") or []:
        raw = item.get("rotation")
        if raw in (None, ""):
            continue
        try:
            return abs(int(float(raw))) % 180
        except (TypeError, ValueError):
            continue
    return 0


def _probe_dimensions(video_path):
    """Return the first video stream's display dimensions, accounting for rotation."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        probe = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height:stream_tags=rotate:stream_side_data",
                "-of", "json", video_path,
            ],
            check=False, capture_output=True, text=True, timeout=60,
        )
        data = json.loads(probe.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        rotation = _stream_rotation(stream)
        return (height, width) if rotation == 90 else (width, height)
    except Exception as exc:
        logger.warning("Video dimension probe failed: %s", exc)
        return None


def video_layout(video_path):
    """Return vertical, landscape, square, or unknown for a local video."""
    dims = _probe_dimensions(video_path)
    if not dims or dims[0] <= 0 or dims[1] <= 0:
        return "unknown"
    width, height = dims
    if height > width:
        return "vertical"
    if width == height:
        return "square"
    return "landscape"


def video_layout_from_bytes(content, suffix=".mp4"):
    """Inspect uploaded bytes so callers can choose Reels vs feed video."""
    if not content:
        return "unknown"
    fd, path = tempfile.mkstemp(suffix=suffix or ".mp4")
    os.close(fd)
    try:
        Path(path).write_bytes(content)
        return video_layout(path)
    except Exception as exc:
        logger.warning("Video layout probe failed: %s", exc)
        return "unknown"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _audio_encode_args():
    return ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def _foreground_canvas(platform):
    """Instagram Reels UI covers more of the frame than Facebook Reels."""
    if str(platform or "").strip().lower() == "instagram":
        return 928, 1498
    return REELS_WIDTH, REELS_HEIGHT


def _to_9x16_fill(video_path, out_path, platform=""):
    """Fit the complete source into a verified 1080x1920 Reel without cropping.

    Facebook uses the full 1080x1920 foreground. Instagram leaves extra margin
    so username/caption chrome does not cover the model or jewellery. Original
    sound is re-encoded to AAC so Meta actually plays it; it is never replaced.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for Reel normalization")
    input_audio = audio_state(video_path)
    fg_w, fg_h = _foreground_canvas(platform)
    filter_complex = (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={REELS_WIDTH}:{REELS_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={REELS_WIDTH}:{REELS_HEIGHT},boxblur=24:8[bg];"
        f"[fgsrc]scale={fg_w}:{fg_h}:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,setdar=9/16[v]"
    )
    cmd = [
        ffmpeg, "-y", "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart",
        *_audio_encode_args(),
        "-metadata:s:v:0", "rotate=0", out_path,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="ignore")[-800:]
        raise RuntimeError(f"9:16 conversion failed: {detail}")

    dimensions = _probe_dimensions(out_path)
    if dimensions and dimensions != (REELS_WIDTH, REELS_HEIGHT):
        raise RuntimeError(
            f"Reel conversion produced {dimensions[0]}x{dimensions[1]}, "
            f"expected {REELS_WIDTH}x{REELS_HEIGHT}"
        )
    output_audio = audio_state(out_path)
    if input_audio in ("audible", "silent") and output_audio == "missing":
        raise RuntimeError("Reel conversion dropped the original audio stream")
    if input_audio == "audible" and output_audio != "audible":
        raise RuntimeError(
            f"Reel conversion did not preserve audible original audio (output={output_audio})"
        )
    logger.info(
        "Verified %s Reel output: %dx%d fg=%sx%s input audio=%s output audio=%s",
        platform or "facebook", REELS_WIDTH, REELS_HEIGHT, fg_w, fg_h,
        input_audio, output_audio,
    )
    return True


def _ensure_playable_audio(video_path, out_path):
    """Re-encode original soundtrack to AAC so Facebook/Instagram play it."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg, "-y", "-i", video_path,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy",
        *_audio_encode_args(),
        "-movflags", "+faststart",
        out_path,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p",
            *_audio_encode_args(),
            "-movflags", "+faststart",
            out_path,
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="ignore")[-400:]
        logger.warning("Could not remux original audio to AAC: %s", detail)
        return False
    if audio_state(out_path) != "audible":
        logger.warning("AAC remux lost original audio; keeping source bytes")
        return False
    logger.info("Remuxed original soundtrack to AAC so Meta will play it")
    return True


def prepare_video(media_url, fill_9x16=False, selection_key="", platform=""):
    """Return (name, bytes, content_type) for a video upload.

    Production rule: silent/muted videos MUST receive audio automatically.
    Audible videos keep their original soundtrack. Vertical Reels that are not
    already 1080x1920 are padded with a blurred edge-fill; Facebook uses the
    full canvas and Instagram leaves extra UI margin. Landscape/square sources
    stay original size and are published as feed/Page video.
    """
    auto_audio = _env_true("AUTO_ADD_AUDIO", "true")
    suffix = Path(media_url.split("?")[0]).suffix or ".mp4"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    temp_paths = [tmp_path]
    try:
        download_url = _fresh_media_url(media_url)
        resp = _request_get(download_url, timeout=180, headers=DOWNLOAD_HEADERS)
        with open(tmp_path, "wb") as f:
            f.write(resp.content)

        name = media_url.split("?")[0].rsplit("/", 1)[-1] or "video.mp4"
        current = tmp_path

        source_digest = hashlib.sha256(Path(tmp_path).read_bytes()).hexdigest()[:16]
        state = audio_state(tmp_path)
        dims = _probe_dimensions(tmp_path)
        layout = video_layout(tmp_path)
        logger.info(
            "Downloaded fresh source: sha256=%s bytes=%d source audio=%s layout=%s size=%s",
            source_digest,
            Path(tmp_path).stat().st_size,
            state,
            layout,
            f"{dims[0]}x{dims[1]}" if dims else "unknown",
        )
        if auto_audio and state in ("missing", "silent"):
            music_key = selection_key or media_url
            music_path = _resolve_music(music_key, temp_paths)
            mixed_path = tmp_path + ".mixed.mp4"
            temp_paths.append(mixed_path)
            current = _mix_music(tmp_path, music_path, mixed_path, music_key)
            verify = audio_state(current)
            if verify in ("missing", "silent"):
                raise RuntimeError("Automatic audio was added but output is still silent; refusing upload")
        elif state == "audible":
            logger.info("Video already has usable audio; preserving original sound")
        elif auto_audio and state == "unknown":
            raise RuntimeError("Could not verify video audio; refusing silent-risk upload")

        current_dims = _probe_dimensions(current) or dims
        current_layout = video_layout(current) if current != tmp_path else layout
        dest = str(platform or "").strip().lower()
        if fill_9x16 and current_layout == "vertical":
            already_reel = current_dims == (REELS_WIDTH, REELS_HEIGHT)
            if already_reel and dest != "instagram":
                logger.info(
                    "Vertical source already %dx%d; keeping pixels and original soundtrack",
                    REELS_WIDTH, REELS_HEIGHT,
                )
            else:
                fitted = current + ".9x16.mp4"
                temp_paths.append(fitted)
                _to_9x16_fill(current, fitted, platform=dest)
                current = fitted
        elif fill_9x16:
            logger.info(
                "Keeping original %s size %s and soundtrack; not stretching onto 1080x1920",
                current_layout,
                f"{current_dims[0]}x{current_dims[1]}" if current_dims else "unknown",
            )

        if audio_state(current) == "audible" and current == tmp_path:
            remuxed = current + ".aac.mp4"
            temp_paths.append(remuxed)
            if _ensure_playable_audio(current, remuxed):
                current = remuxed

        content = Path(current).read_bytes()
        output_name = f"{Path(name).stem}.mp4"
        return output_name, content, "video/mp4"
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass


def media_kind(media_url):
    lower = (media_url.split("?")[0] or "").lower()
    if lower.endswith(_VIDEO_EXTS):
        return "video"
    return "image"


def _matches_magic(header, kind):
    if kind == "video":
        if len(header) >= 8 and header[4:8] == b"ftyp":
            return True
        return any(header.startswith(m) for m, _ in _VIDEO_MAGIC)
    return any(header.startswith(m) for m, _ in _IMAGE_MAGIC)


def _probe_video(media_url):
    """Probe a local download of the video so CDN stalls cannot freeze ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return ""
    suffix = Path(str(media_url).split("?")[0]).suffix or ".mp4"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        resp = _request_get(_fresh_media_url(media_url), timeout=180, headers=DOWNLOAD_HEADERS)
        with open(tmp_path, "wb") as handle:
            handle.write(resp.content)
        last_error = ""
        for attempt in range(1, 3):
            try:
                out = subprocess.run(
                    [
                        ffprobe, "-v", "error",
                        "-show_entries", "format=format_name,duration",
                        "-of", "csv=p=0", tmp_path,
                    ],
                    check=False, capture_output=True, text=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                last_error = "ffprobe timed out"
                logger.warning("ffprobe attempt %s timed out for %s", attempt, media_url)
                continue
            if out.returncode != 0:
                detail = (out.stderr or "").strip().splitlines()
                return (detail[-1] if detail else "ffprobe rejected the file")[:300]
            if "N/A" in out.stdout or not out.stdout.strip():
                return "ffprobe could not read a valid stream"
            return ""
        return last_error
    except _TRANSIENT_DOWNLOAD_ERRORS as exc:
        logger.warning("Video probe download failed for %s: %s", media_url, exc)
        return ""
    except Exception as exc:
        return f"ffprobe error: {exc}"
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def validate_media_url(media_url, kind=None, ffprobe=True):
    kind = kind or media_kind(media_url)
    if not kind:
        return "cannot determine media kind"

    fresh_url = _fresh_media_url(media_url)
    headers = dict(DOWNLOAD_HEADERS)
    headers["Range"] = "bytes=0-65535"
    try:
        resp = _request_get(fresh_url, timeout=180, headers=headers)
    except _TRANSIENT_DOWNLOAD_ERRORS as exc:
        logger.warning("Media validation download failed for %s: %s", media_url, exc)
        return ""
    except Exception:
        return f"HTTP error for {media_url}"

    content_type = (resp.headers.get("Content-Type", "") or "").lower().split(";")[0]
    expected = _VIDEO_TYPES if kind == "video" else _IMAGE_TYPES
    if content_type and content_type != "application/octet-stream" and content_type not in expected:
        return f"unexpected Content-Type '{content_type}' (expected {kind})"

    if not _matches_magic(resp.content[:16], kind):
        return f"magic bytes do not match {kind} media"

    if kind == "video" and ffprobe:
        return _probe_video(fresh_url)
    return ""
