"""Input resolution and media transforms: a unified MediaSpec. Video/audio
URLs stream straight into ffmpeg instead of being buffered as whole files."""

import base64
import ipaddress
import os
import re
import socket
import sys
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import config
import media_formats as formats
import video_plans

_STDIN_BUFFER: bytes | None = None
EXTRAS_DIR = Path(__file__).resolve().parent / "extras"

# Media URLs are treated as untrusted input (an agent may be tricked into
# fetching attacker-controlled links). Block addresses that are local to the
# machine or private network: loopback, RFC1918, CGNAT, link-local (including
# cloud metadata 169.254.169.254), multicast, and benchmarking ranges.
_UNSAFE_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64: can alias private IPv4
)


def _ip_is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _UNSAFE_NETS)


def _host_is_unsafe(host: str) -> bool:
    """True when the host is an IP literal in a blocked range, or when any of
    its DNS results points into a blocked range."""
    try:
        return _ip_is_unsafe(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return True
    return any(_ip_is_unsafe(ipaddress.ip_address(info[4][0])) for info in infos)


def unsafe_url_reason(url: str) -> str | None:
    """Return a human-readable reason when a media URL must be blocked, else None."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme '{parsed.scheme or '(none)'}' (only http/https)"
    host = parsed.hostname
    if not host:
        return "missing host"
    if _host_is_unsafe(host):
        return f"'{host}' resolves to a local/private/link-local network"
    return None


def ensure_url_safe(url: str) -> None:
    """Raise RuntimeError for media URLs that could reach local infrastructure."""
    reason = unsafe_url_reason(url)
    if reason:
        raise RuntimeError(f"Blocked media URL ({reason}): {url[:200]}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect hop so a safe-looking URL cannot bounce to
    localhost / cloud metadata."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ensure_url_safe(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _load_sources():
    """Lazily load the optional site resolvers (extras/sources.py); None when
    missing or import fails."""
    try:
        if str(EXTRAS_DIR) not in sys.path:
            sys.path.insert(0, str(EXTRAS_DIR))
        import sources  # noqa: F401
        return sources
    except Exception:
        return None


@dataclass
class MediaSpec:
    kind: str                       # image / video / audio
    source: str                     # local / url / hls / stdin
    path: str | None = None         # file path or streamable URL
    data: bytes | None = None       # in-memory bytes (image / stdin / unknown URL)
    headers: dict | None = None     # direct-link request headers (site signature / UA)
    subtitle_text: str | None = None
    title: str | None = None


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def read_stdin() -> bytes:
    global _STDIN_BUFFER
    if _STDIN_BUFFER is None:
        _STDIN_BUFFER = sys.stdin.buffer.read()
    return _STDIN_BUFFER


def fetch_url_bytes(url: str, max_bytes: int) -> bytes:
    """Download a URL into memory; max_bytes<=0 means no size cap."""
    ensure_url_safe(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    try:
        with opener.open(req, timeout=60) as resp:
            total = 0
            chunks = []
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes > 0 and total > max_bytes:
                    raise RuntimeError(f"Download exceeded the size cap "
                                       f"({max_bytes // (1 << 20)} MB)")
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Download failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Download failed: {e.reason}") from e


def sniff_bytes(data: bytes) -> str:
    return formats.sniff_head(data[:16])


def sniff_kind(path: Path, head: bytes | None = None) -> str:
    ext = path.suffix.lower()
    if head is None:
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except Exception:
            head = b""
    kind = formats.sniff_head(head)
    # Container signatures are ambiguous: OggS opens both .ogg audio and .ogv
    # video, EBML opens both .mka and .mkv. Resolve those conflicts with the
    # file extension; everything else trusts the magic bytes.
    if kind == "audio" and ext in formats.VIDEO_EXTS:
        return "video"
    if kind == "video":
        if ext in formats.AUDIO_EXTS:
            return "audio"
        if ext in formats.IMAGE_EXTS:
            return "image"
    return kind


def _check_image_size(size: int) -> None:
    limit = config.max_image_mb() * (1 << 20)
    if limit > 0 and size > limit:
        raise RuntimeError(
            f"Image is {size // (1 << 20)} MB, exceeding the "
            f"{config.max_image_mb()} MB limit "
            f"(set VISION_MAX_IMAGE_MB / max_image_mb to adjust).")


def normalize_image(data: bytes) -> bytes:
    """Ollama natively supports png/jpeg/webp; other raster formats
    (AVIF/TIFF/HEIC, ...) are converted to PNG via ffmpeg."""
    if formats.is_native_image(data[:16]):
        return data
    conv_dir = config.TEMP_DIR / "conv"
    conv_dir.mkdir(parents=True, exist_ok=True)
    fd, src_name = tempfile.mkstemp(prefix="in-", suffix=".img", dir=conv_dir)
    os.close(fd)
    src = Path(src_name)
    src.write_bytes(data)
    try:
        code, out, err = video_plans.run_ffmpeg(
            ["-i", str(src), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            timeout=60,
        )
        if code != 0 or not out:
            raise RuntimeError(f"Image conversion failed (ffmpeg cannot decode "
                               f"this format): {err.strip()[:300]}")
        return out
    finally:
        try:
            src.unlink()
        except Exception:
            pass


def resolve_input(arg: str, want: str = "auto") -> MediaSpec:
    """Unify any input (path / URL / video-site link / m3u8 / stdin) into a
    MediaSpec. Known video/audio URL extensions return a streamable address
    (ffmpeg reads with request headers) instead of buffering; unknown types
    are buffered and sniffed."""
    if arg == "-":
        data = read_stdin()
        kind = sniff_bytes(data)
        if kind == "image":
            _check_image_size(len(data))
        return MediaSpec(kind=kind, source="stdin", data=data)

    if is_url(arg):
        ensure_url_safe(arg)
        if want in ("video", "audio"):
            resolver = _load_sources()
            source = resolver.try_resolve(arg) if resolver else None
            key = "video_url" if want == "video" else "audio_url"
            if source and getattr(source, key):
                direct = getattr(source, key)
                ensure_url_safe(direct)
                if not config.direct_url_streaming():
                    data = fetch_url_bytes(direct, config.max_download_mb() * (1 << 20))
                    return MediaSpec(kind=sniff_bytes(data), source="url", data=data,
                                     subtitle_text=source.subtitle_text,
                                     title=source.title)
                return MediaSpec(
                    kind=want,
                    source="url",
                    path=direct,
                    headers=source.headers,
                    subtitle_text=source.subtitle_text,
                    title=source.title,
                )
        clean = arg.rsplit("?", 1)[0].lower()
        if clean.endswith(tuple(formats.IMAGE_EXTS)):
            # 0 (disabled) falls back to the generic download cap so remote
            # images still cannot buffer without bound.
            cap_mb = config.max_image_mb() or config.max_download_mb() or 500
            data = fetch_url_bytes(arg, cap_mb * (1 << 20))
            return MediaSpec(kind="image", source="url", data=data)
        if clean.endswith(tuple(formats.AUDIO_EXTS)) and config.direct_url_streaming():
            return MediaSpec(kind="audio", source="url", path=arg,
                             headers={"User-Agent": "Mozilla/5.0"})
        if clean.endswith(tuple(formats.VIDEO_EXTS)) and config.direct_url_streaming():
            return MediaSpec(kind="video", source="url", path=arg,
                             headers={"User-Agent": "Mozilla/5.0"})
        # Unknown type: buffer and sniff (keeps the existing fallback behavior).
        data = fetch_url_bytes(arg, config.max_download_mb() * (1 << 20))
        return MediaSpec(kind=sniff_bytes(data), source="url", data=data)

    path = Path(arg)
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    spec = MediaSpec(kind=sniff_kind(path), source="local", path=str(path))
    if spec.kind == "image":
        _check_image_size(path.stat().st_size)
    return spec


def crop_image_bytes(data: bytes, crop: str) -> bytes:
    m = re.fullmatch(r"(\d+)x(\d+)(?:\+(-?\d+))?(?:\+(-?\d+))?", crop.strip())
    if not m:
        raise RuntimeError(
            f"Invalid --crop format: {crop!r} "
            "(expected WxH+X+Y, e.g. 100x50+10+20)")
    crop = f"{m.group(1)}:{m.group(2)}:{m.group(3) or '0'}:{m.group(4) or '0'}"
    code, out, err = video_plans.run_ffmpeg(
        ["-f", "image2pipe", "-i", "pipe:0", "-vf", f"crop={crop}", "-f", "image2pipe",
         "-vcodec", "mjpeg", "-q:v", "2", "-"],
        timeout=60, input=data,
    )
    if code != 0 or not out:
        raise RuntimeError(f"Image crop failed (--crop format WxH+X+Y): "
                           f"{err.strip()[:300]}")
    return out


def scale_image_bytes(data: bytes, max_side: int) -> bytes:
    code, out, err = video_plans.run_ffmpeg(
        ["-f", "image2pipe", "-i", "pipe:0", "-vf", f"scale=min({max_side}\\,iw):-2",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "-"],
        timeout=60, input=data,
    )
    if code != 0 or not out:
        raise RuntimeError(f"Image resize failed: {err.strip()[:300]}")
    return out


def image_to_b64(data: bytes, crop: str | None, size: str) -> str:
    data = normalize_image(data)
    if crop:
        data = crop_image_bytes(data, crop)
    if size == "small":
        data = scale_image_bytes(data, 320)
    return base64.b64encode(data).decode("ascii")
