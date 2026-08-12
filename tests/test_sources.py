# -*- coding: utf-8 -*-
"""Site resolver registry tests: no network access."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extras"))

import sources  # noqa: E402


class FakeResolver:
    name = "fake"

    def match(self, url: str) -> bool:
        return url.startswith("https://fake.example/")

    def resolve(self, url: str) -> sources.ResolvedStream:
        return sources.ResolvedStream(video_url="https://fake.example/stream.mp4",
                                      platform="fake")


def test_registry_register_and_dedupe():
    before = [r.name for r in sources._REGISTRY]
    sources.register(FakeResolver())
    sources.register(FakeResolver())
    names = [r.name for r in sources._REGISTRY]
    assert names == before + ["fake"]
    # Clean up so other tests are unaffected.
    sources._REGISTRY.pop()


def test_try_resolve_matches_registered_resolver():
    sources._REGISTRY.append(FakeResolver())
    try:
        result = sources.try_resolve("https://fake.example/v/1")
        assert result is not None
        assert result.video_url == "https://fake.example/stream.mp4"
        assert result.platform == "fake"
    finally:
        sources._REGISTRY.pop()


def test_try_resolve_returns_none_for_unknown():
    # No registered resolver matches: return None so the generic path is used,
    # and never touch the network.
    assert sources.try_resolve("https://example.com/video.mp4") is None


def test_registered_contains_only_unified_resolver():
    # All video sites go through one channel (yt-dlp); no per-site resolvers.
    assert sources.registered() == ["yt-dlp"]


def test_ytdlp_match_rules(monkeypatch):
    r = sources.YtDlpResolver()
    monkeypatch.setattr(sources, "ytdlp_available", lambda: True)
    assert r.match("https://www.youtube.com/watch?v=BaW_jenozKc")
    assert r.match("https://twitter.com/x/status/123")
    assert r.match("https://vimeo.com/12345")
    assert not r.match("https://example.com/video.mp4")
    assert not r.match("https://example.com/stream.m3u8")
    assert not r.match("https://example.com/photo.png")
    assert not r.match("C:/path/video.mp4")
    monkeypatch.setattr(sources, "ytdlp_available", lambda: False)
    assert not r.match("https://www.youtube.com/watch?v=BaW_jenozKc")


def test_pick_ytdlp_streams_combined_preferred():
    info = {"formats": [
        {"format_id": "a", "vcodec": "none", "acodec": "mp4a",
         "url": "audio.m4a", "tbr": 128},
        {"format_id": "v", "vcodec": "avc1", "acodec": "none",
         "url": "video.mp4", "height": 720, "tbr": 2000},
        {"format_id": "c", "vcodec": "avc1", "acodec": "mp4a",
         "url": "combined.mp4", "height": 1080, "tbr": 5000},
    ]}
    v, a = sources._pick_ytdlp_streams(info)
    assert v == "combined.mp4"
    assert a == ""


def test_pick_ytdlp_streams_separate_audio():
    info = {"formats": [
        {"format_id": "v", "vcodec": "avc1", "acodec": "none",
         "url": "video.mp4", "height": 720, "tbr": 2000},
        {"format_id": "a", "vcodec": "none", "acodec": "mp4a",
         "url": "audio.m4a", "tbr": 256},
    ]}
    v, a = sources._pick_ytdlp_streams(info)
    assert v == "video.mp4"
    assert a == "audio.m4a"


def test_best_stream_ytdlp_style_fields():
    # yt-dlp formats: url/height/tbr/vcodec/acodec drive the selection.
    formats_list = [
        {"url": "audio.m4a", "vcodec": "none", "acodec": "mp4a", "tbr": 128},
        {"url": "video-720.mp4", "vcodec": "avc1", "acodec": "none",
         "height": 720, "tbr": 2000},
        {"url": "video-1080.mp4", "vcodec": "avc1", "acodec": "none",
         "height": 1080, "tbr": 5000},
    ]
    assert sources._best_stream(formats_list, want_video=True) == "video-1080.mp4"
    assert sources._best_stream(formats_list, want_video=False) == "audio.m4a"
    assert sources._best_stream([{"url": "x.mp4", "vcodec": "avc1",
                                  "acodec": "none"}], want_video=False) == ""
