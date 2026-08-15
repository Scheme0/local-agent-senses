# -*- coding: utf-8 -*-
"""Media format table and magic-byte sniffing tests: no network access."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import media_formats as formats  # noqa: E402


def test_common_extensions_covered():
    # images
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
                ".avif", ".tiff", ".tif", ".heic", ".heif"):
        assert ext in formats.IMAGE_EXTS, ext
    # videos
    for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
                ".wmv", ".ts", ".m3u8", ".flv", ".3gp", ".mpg",
                ".mpeg", ".ogv", ".vob"):
        assert ext in formats.VIDEO_EXTS, ext
    # audio
    for ext in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
                ".opus", ".wma", ".amr", ".aiff", ".aif", ".ape", ".ac3"):
        assert ext in formats.AUDIO_EXTS, ext


def test_direct_media_suffixes_covers_all():
    # Documents (.pdf) are direct media suffixes too: they are handled by the
    # PDF rasterize path, so the yt-dlp resolver must skip them as well.
    all_exts = formats.IMAGE_EXTS | formats.AUDIO_EXTS | formats.VIDEO_EXTS | formats.DOCUMENT_EXTS
    assert set(formats.DIRECT_MEDIA_SUFFIXES) == all_exts


def test_sniff_image_magic():
    cases = [
        b"\xff\xd8\xff\xe0" + b"x" * 12,                 # JPEG
        b"\x89PNG\r\n\x1a\n" + b"x" * 8,                 # PNG
        b"GIF89a" + b"x" * 10,                           # GIF
        b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 4,          # WebP
        b"BM" + b"x" * 14,                               # BMP
        b"\x00\x00\x00\x18ftypavif" + b"x" * 4,          # AVIF
        b"II*\x00" + b"x" * 12,                          # TIFF LE
        b"MM\x00*" + b"x" * 12,                          # TIFF BE
    ]
    for head in cases:
        assert formats.sniff_head(head) == "image", head[:8]


def test_sniff_audio_magic():
    cases = [
        b"ID3\x04\x00" + b"x" * 10,                      # MP3 (ID3)
        b"\xff\xfb\x90\x00" + b"x" * 12,                 # MP3 (MPEG sync)
        b"fLaC" + b"x" * 12,                             # FLAC
        b"RIFF\x00\x00\x00\x00WAVE" + b"x" * 4,          # WAV
        b"#!AMR\n" + b"x" * 9,                           # AMR
        b"MAC " + b"x" * 12,                             # APE
        b"OggS\x00\x02" + b"x" * 10,                     # OGG / Opus
    ]
    for head in cases:
        assert formats.sniff_head(head) == "audio", head[:8]


def test_sniff_ogg_without_extension_is_audio():
    # Regression: OggS used to fall through to the "video" default, so a
    # no-extension OGG/Opus file was misrouted to the video pipeline.
    assert formats.sniff_head(b"OggS" + b"\x00" * 12) == "audio"


def test_sniff_unknown_defaults_video():
    assert formats.sniff_head(b"") == "video"
    assert formats.sniff_head(b"\x00" * 16) == "video"


def test_is_native_image():
    assert formats.is_native_image(b"\xff\xd8\xff\xe0" + b"x" * 12)
    assert formats.is_native_image(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
    assert formats.is_native_image(b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 4)
    assert not formats.is_native_image(b"GIF89a" + b"x" * 10)
    assert not formats.is_native_image(b"\x00" * 16)
