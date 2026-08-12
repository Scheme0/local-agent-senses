# -*- coding: utf-8 -*-
"""SSRF guard tests for media URLs (pure logic, no network)."""
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
