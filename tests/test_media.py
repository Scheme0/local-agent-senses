# -*- coding: utf-8 -*-
"""media.py unit tests: image transforms (mocked ffmpeg) and redirect checks."""
import base64
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import media  # noqa: E402
import video_plans  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00" * 8 + b"WEBP" + b"\x00" * 32
_TMP = Path(tempfile.gettempdir()) / "local-agent-senses-tests" / "media"


def test_image_to_b64_native_passthrough(monkeypatch):
    called = []
    monkeypatch.setattr(video_plans, "run_ffmpeg",
                        lambda *a, **k: called.append(a) or (0, b"", ""))
    b64 = media.image_to_b64(PNG, None, "full")
    assert base64.b64decode(b64)[:8] == b"\x89PNG\r\n\x1a\n"
    assert not called


def test_image_to_b64_crop_and_scale_via_ffmpeg(monkeypatch):
    captured = []
    def fake_run_ffmpeg(args, timeout=3600, input=None):
        captured.append(list(args))
        return 0, JPEG, ""
    monkeypatch.setattr(video_plans, "run_ffmpeg", fake_run_ffmpeg)
    b64 = media.image_to_b64(PNG, "100x50+10+20", "small")
    assert base64.b64decode(b64)[:3] == b"\xff\xd8\xff"
    joined = [" ".join(a) for a in captured]
    assert any("crop=100:50:10:20" in j for j in joined)
    assert any("scale=min(320\\,iw):-2" in j for j in joined)


def test_invalid_crop_format_rejected(monkeypatch):
    called = []
    monkeypatch.setattr(video_plans, "run_ffmpeg",
                        lambda *a, **k: called.append(a) or (0, JPEG, ""))
    for bad in ("garbage", "100x", "x50+1+2", "100x50+1:2", "crop=100"):
        try:
            media.crop_image_bytes(PNG, bad)
            raise AssertionError(f"expected RuntimeError for {bad!r}")
        except RuntimeError as e:
            assert "Invalid --crop format" in str(e)
    assert not called


def test_normalize_image_converts_non_native_and_cleans_up(monkeypatch):
    tmp = Path(tempfile.gettempdir()) / "local-agent-senses-tests" / "conv-test"
    tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "TEMP_DIR", tmp)
    monkeypatch.setattr(video_plans, "run_ffmpeg",
                        lambda *a, **k: (0, PNG, ""))
    out = media.normalize_image(WEBP)
    assert out == PNG
    conv = tmp / "conv"
    assert list(conv.glob("*")) == []


def test_redirect_to_private_url_blocked():
    handler = media._SafeRedirectHandler()
    req = urllib.request.Request("https://example.com/start.png")
    try:
        handler.redirect_request(req, None, 302, "Found", {},
                                 "http://127.0.0.1/steal.png")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_redirect_to_public_url_allowed():
    handler = media._SafeRedirectHandler()
    req = urllib.request.Request("https://example.com/start.png")
    new = handler.redirect_request(req, None, 302, "Found", {},
                                   "https://93.184.216.34/ok.png")
    assert new.full_url == "https://93.184.216.34/ok.png"


def test_sniff_kind_resolves_ogg_container_conflicts():
    ogg = b"OggS\x00\x02" + b"\x00" * 16
    assert media.sniff_kind(Path("x.ogg"), head=ogg) == "audio"
    assert media.sniff_kind(Path("x.opus"), head=ogg) == "audio"
    assert media.sniff_kind(Path("x.ogv"), head=ogg) == "video"
    assert media.sniff_kind(Path("x.bin"), head=ogg) == "audio"


def test_local_image_over_size_limit_rejected(monkeypatch):
    monkeypatch.setenv("VISION_MAX_IMAGE_MB", "1")
    _TMP.mkdir(parents=True, exist_ok=True)
    p = _TMP / "big.png"
    p.write_bytes(PNG + b"\x00" * (2 * 1024 * 1024))
    try:
        media.resolve_input(str(p), want="image")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "limit" in str(e)


def test_stdin_image_over_size_limit_rejected(monkeypatch):
    monkeypatch.setenv("VISION_MAX_IMAGE_MB", "1")
    monkeypatch.setattr(media, "read_stdin",
                        lambda: PNG + b"\x00" * (2 * 1024 * 1024))
    try:
        media.resolve_input("-", want="image")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "limit" in str(e)


def test_stdin_non_image_over_size_limit_rejected(monkeypatch):
    monkeypatch.setenv("VISION_MAX_STDIN_MB", "1")
    monkeypatch.setattr(media, "read_stdin",
                        lambda: b"\x00" * (2 * 1024 * 1024 + 1))
    try:
        media.resolve_input("-", want="video")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "stdin limit" in str(e)


def test_stdin_size_cap_zero_disables(monkeypatch):
    monkeypatch.setenv("VISION_MAX_STDIN_MB", "0")
    payload = b"\x00" * (3 * 1024 * 1024)
    monkeypatch.setattr(media, "read_stdin", lambda: payload)
    spec = media.resolve_input("-", want="video")
    assert spec.source == "stdin" and len(spec.data) == len(payload)


def test_url_image_uses_image_size_cap(monkeypatch):
    seen = {}

    def fake_fetch(url, max_bytes):
        seen["max"] = max_bytes
        return PNG

    monkeypatch.setattr(media, "fetch_url_bytes", fake_fetch)
    spec = media.resolve_input("https://93.184.216.34/a.png", want="image")
    assert spec.kind == "image"
    assert seen["max"] == 20 * (1 << 20)


def test_fetch_url_bytes_zero_cap_is_unlimited(monkeypatch):
    """Regression: max_bytes=0 must mean 'no cap', not 'fail immediately'."""
    monkeypatch.setattr(media, "ensure_url_safe", lambda url: None)
    chunk = b"z" * 65536

    class FakeResp:
        def __init__(self, chunks):
            self.left = chunks

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if self.left > 0:
                self.left -= 1
                return chunk
            return b""

    class FakeOpener:
        def __init__(self, chunks):
            self.chunks = chunks

        def open(self, req, timeout=None):
            return FakeResp(self.chunks)

    monkeypatch.setattr(media.urllib.request, "build_opener",
                        lambda *a, **k: FakeOpener(3))
    assert media.fetch_url_bytes("http://example.com/x", 0) == chunk * 3

    monkeypatch.setattr(media.urllib.request, "build_opener",
                        lambda *a, **k: FakeOpener(3))
    try:
        media.fetch_url_bytes("http://example.com/x", 65536)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "size cap" in str(e)


def test_read_stdin_aborts_early_when_over_cap(monkeypatch):
    payload = b"\x00" * (3 * 1024 * 1024)  # 3 MB

    class _Buf:
        def __init__(self, data):
            self._data = data
            self._pos = 0

        def read(self, n=-1):
            if self._pos >= len(self._data):
                return b""
            end = min(len(self._data), self._pos + max(n, 1))
            out = self._data[self._pos:end]
            self._pos = end
            return out

    class _Stdin:
        def __init__(self, buf):
            self.buffer = buf

    buf = _Buf(payload)
    monkeypatch.setattr(sys, "stdin", _Stdin(buf))
    monkeypatch.setattr(media, "_STDIN_BUFFER", None)
    monkeypatch.setenv("VISION_MAX_STDIN_MB", "1")
    try:
        media.read_stdin()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "stdin limit" in str(e)
        assert buf._pos < len(payload)  # aborted before draining stdin
