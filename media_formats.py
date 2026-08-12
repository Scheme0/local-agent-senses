"""Media format tables and magic-byte sniffing.

media.py and sources.py share one source of truth for extensions/magic bytes
so the two modules cannot drift apart. ffmpeg can decode far more formats than
are listed here; inputs outside the tables fall back to magic-byte sniffing,
and raster images are auto-converted (see media.normalize_image).
"""

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif",
    ".tif", ".tiff", ".jxl", ".heic", ".heif",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".amr", ".aiff", ".aif", ".ape", ".ac3", ".caf", ".mka",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv", ".ts", ".m3u8",
    ".flv", ".3gp", ".mpg", ".mpeg", ".ogv", ".vob", ".rmvb", ".asf",
}

# Direct-link suffixes: the yt-dlp resolver skips these URLs so it never
# hijacks plain media links.
DIRECT_MEDIA_SUFFIXES = tuple(sorted(IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS))


def is_native_image(head: bytes) -> bool:
    """Bitmaps Ollama accepts natively (png/jpeg/webp) without conversion."""
    return (head[:3] == b"\xff\xd8\xff"
            or head[:8] == b"\x89PNG\r\n\x1a\n"
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


# Magic-byte signatures: (kind, offset, prefix, check_offset, check_bytes).
# `prefix` must match at `offset`; when `check_bytes` is set, the bytes at
# `check_offset` must be one of the listed values (RIFF container subtype or
# ISO-BMFF brand). Entries are evaluated in order.
_SIGNATURES = (
    # images
    ("image", 0, b"\xff\xd8\xff", None, None),               # JPEG
    ("image", 0, b"\x89PNG\r\n\x1a\n", None, None),          # PNG
    ("image", 0, b"GIF87a", None, None),                     # GIF
    ("image", 0, b"GIF89a", None, None),                     # GIF
    ("image", 0, b"RIFF", 8, (b"WEBP",)),                    # WebP
    ("image", 0, b"BM", None, None),                         # BMP
    ("image", 4, b"ftyp", 8,
     (b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1")),  # AVIF/HEIC
    ("image", 0, b"\x49\x49\x2a\x00", None, None),           # TIFF LE
    ("image", 0, b"\x4d\x4d\x00\x2a", None, None),           # TIFF BE
    # audio
    ("audio", 0, b"ID3", None, None),                        # MP3 (ID3 tag)
    ("audio", 0, b"\xff\xfb", None, None),                   # MP3 (MPEG sync)
    ("audio", 0, b"\xff\xf3", None, None),
    ("audio", 0, b"\xff\xf1", None, None),
    ("audio", 0, b"\xff\xf9", None, None),
    ("audio", 0, b"fLaC", None, None),                       # FLAC
    ("audio", 0, b"RIFF", 8, (b"WAVE",)),                    # WAV
    ("audio", 0, b"OggS", None, None),                       # OGG / Opus
    ("audio", 0, b"#!AMR", None, None),                      # AMR
    ("audio", 0, b"MAC ", None, None),                       # APE
)


def _match(head: bytes, sig: tuple) -> bool:
    _, offset, prefix, check_offset, check_bytes = sig
    end = offset + len(prefix)
    if len(head) < end or head[offset:end] != prefix:
        return False
    if check_bytes is None:
        return True
    need = len(check_bytes[0])
    return (len(head) >= check_offset + need
            and head[check_offset:check_offset + need] in check_bytes)


def sniff_head(head: bytes) -> str:
    """Classify a buffer as image / audio / video by magic bytes (unknown
    defaults to video)."""
    for sig in _SIGNATURES:
        if _match(head, sig):
            return sig[0]
    return "video"
