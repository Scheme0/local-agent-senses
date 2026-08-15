# -*- coding: utf-8 -*-
"""SSRF guard tests for media URLs (pure logic, no network)."""
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import media  # noqa: E402


def test_private_and_local_urls_blocked():
    for url in (
        "http://127.0.0.1/x.png",
        "http://10.0.0.5/x.png",
        "http://172.16.0.9/stream.m3u8",
        "http://192.168.1.1/video.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/x.mp4",
        "http://0.0.0.0/x.png",
        "http://224.0.0.1/x.png",
        "http://[::1]/x.png",
        "http://[fc00::1]/x.png",
        "http://[64:ff9b::7f00:1]/x.png",  # NAT64 alias of 127.0.0.1
    ):
        assert media.unsafe_url_reason(url) is not None, url
        try:
            media.ensure_url_safe(url)
            raise AssertionError(f"expected RuntimeError for {url}")
        except RuntimeError:
            pass


def test_unsupported_schemes_blocked():
    for url in (
        "file:///etc/passwd",
        "ftp://example.com/a.png",
        "data:image/png;base64,AAAA",
    ):
        assert media.unsafe_url_reason(url) is not None, url


def test_public_ip_literal_allowed():
    assert media.unsafe_url_reason("https://93.184.216.34/photo.jpg") is None


def test_dns_resolution_checked(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "evil.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        if host == "ok.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        raise OSError("no such host")

    monkeypatch.setattr(media.socket, "getaddrinfo", fake_getaddrinfo)
    assert media.unsafe_url_reason("http://evil.example/x.png") is not None
    assert media.unsafe_url_reason("http://ok.example/x.png") is None


def test_fetch_url_bytes_refuses_localhost_before_network():
    try:
        media.fetch_url_bytes("http://127.0.0.1:9/x.png", 1000)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "Blocked" in str(e)


def test_api_base_local_http_and_https_allowed():
    config.validate_api_base("")  # local mode: no error
    config.validate_api_base("http://localhost:11434")
    config.validate_api_base("http://127.0.0.1:11434")
    config.validate_api_base("http://[::1]:11434")
    config.validate_api_base("https://api.example.com/v1")


def test_api_base_rejects_invalid_and_insecure():
    for bad in (
        "api.example.com/v1",          # no scheme
        "http://api.example.com/v1",   # public http
        "ftp://example.com/v1",        # non-http(s)
        "http://user:pass@localhost/v1",  # embedded credentials
        "http://0.0.0.0/v1",
    ):
        try:
            config.validate_api_base(bad)
            raise AssertionError(f"expected ConfigError for {bad!r}")
        except config.ConfigError:
            pass


def test_api_base_credential_error_does_not_include_secret(monkeypatch):
    monkeypatch.setenv("VISION_API_KEY", "sk-topsecret")
    try:
        config.validate_api_base("https://user:hunter2@example.com/v1")
        raise AssertionError("expected ConfigError")
    except config.ConfigError as exc:
        msg = str(exc)
        assert "sk-topsecret" not in msg
        assert "hunter2" not in msg


def test_external_tool_validation_hard_failures(tmp_path):
    missing = tmp_path / "nope.exe"
    try:
        config.validate_external_tool(str(missing), "TOOL")
        raise AssertionError("expected ConfigError")
    except config.ConfigError as exc:
        assert "not found" in str(exc)
    try:
        config.validate_external_tool("tool.exe", "TOOL")  # relative
        raise AssertionError("expected ConfigError")
    except config.ConfigError:
        pass
    d = tmp_path / "dir"
    d.mkdir()
    try:
        config.validate_external_tool(str(d), "TOOL")
        raise AssertionError("expected ConfigError")
    except config.ConfigError as exc:
        assert "regular file" in str(exc)


def test_external_tool_validation_accepts_absolute_file(tmp_path):
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ")
    issues = config.validate_external_tool(str(exe), "TOOL")
    assert isinstance(issues, list)


def test_ffmpeg_bin_validates_explicit_path(monkeypatch, tmp_path):
    import video_plans
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("VISION_FFMPEG", str(exe))
    monkeypatch.setattr(video_plans.config, "cfg_value", lambda key: "")
    assert video_plans.ffmpeg_bin() == str(exe)


def test_dns_rebinding_is_documented_as_best_effort():
    """The SSRF guard re-checks redirects but cannot pin the connection to a
    resolved IP; the source must not claim rebinding is fully solved."""
    src = (ROOT / "media.py").read_text(encoding="utf-8")
    assert "DNS rebinding" in src
    assert "best-effort" in src


def test_download_cap_zero_still_hard_capped(monkeypatch):
    assert config.HARD_MAX_DOWNLOAD_MB > 0
    monkeypatch.setattr(config, "max_download_mb", lambda: 0)
    assert config.effective_download_cap_mb() == config.HARD_MAX_DOWNLOAD_MB
    monkeypatch.setattr(config, "max_download_mb", lambda: 10 ** 9)
    assert config.effective_download_cap_mb() == config.HARD_MAX_DOWNLOAD_MB
    monkeypatch.setattr(config, "max_download_mb", lambda: 300)
    assert config.effective_download_cap_mb() == 300


def test_pdf_pages_zero_still_hard_capped(monkeypatch):
    assert config.HARD_MAX_PDF_PAGES > 0
    monkeypatch.setattr(config, "max_pdf_pages", lambda: 0)
    assert config.effective_pdf_pages() == config.HARD_MAX_PDF_PAGES
    monkeypatch.setattr(config, "max_pdf_pages", lambda: 10 ** 9)
    assert config.effective_pdf_pages() == config.HARD_MAX_PDF_PAGES
    monkeypatch.setattr(config, "max_pdf_pages", lambda: 25)
    assert config.effective_pdf_pages() == 25
