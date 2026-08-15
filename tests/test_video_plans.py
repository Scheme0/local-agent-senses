# -*- coding: utf-8 -*-
"""video_plans unit tests: pure helpers and scheme logic with stubbed ffmpeg."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import video_plans as vp  # noqa: E402


def _jpeg(marker: int = 0) -> bytes:
    return b"\xff\xd8" + bytes([marker]) * 8 + b"\xff\xd9"


def _info(duration=100.0, width=1280, height=720) -> dict:
    return {"duration": duration, "width": width, "height": height,
            "has_audio": False, "has_subtitle": False}


def test_split_mjpeg_multiple_frames():
    blob = _jpeg(1) + b"noise" + _jpeg(2) + _jpeg(3)
    assert vp._split_mjpeg(blob) == [_jpeg(1), _jpeg(2), _jpeg(3)]
    assert vp._split_mjpeg(b"no markers") == []


def test_split_mjpeg_skips_ffd9_inside_marker_payload():
    # APP1 (EXIF) contains a raw FF D9; only the real EOI ends the frame.
    app1 = b"\xff\xd8\xff\xe1\x00\x06\x41\xff\xd9\x42\x43\x44\xff\xd9"
    assert vp._split_mjpeg(app1) == [app1]
    # Same frame followed by a second regular JPEG.
    blob = app1 + _jpeg(7)
    frames = vp._split_mjpeg(blob)
    assert frames == [app1, _jpeg(7)]


def test_scale_dims():
    assert vp._scale_dims(640, 480, 1600) == (640, 480)
    assert vp._scale_dims(3200, 1800, 1600) == (1600, 900)
    assert vp._scale_dims(900, 3200, 1600) == (450, 1600)
    assert vp._scale_dims(0, 0, 1600) == (1600, 1600)


def test_crop_dims():
    assert vp._crop_dims(1920, 1080, "640:360:100:50") == (640, 360)
    assert vp._crop_dims(1920, 1080, "640:360:-100:-50") == (640, 360)
    assert vp._crop_dims(1920, 1080, "garbage") == (1920, 1080)


def test_band_crop():
    assert vp._band_crop(1920, 1080, "bottom") == "1920:270:0:810"
    assert vp._band_crop(1920, 1080, "top") == "1920:270:0:0"
    assert vp._band_crop(1920, 1080, "middle") == "1920:360:0:360"
    assert vp._band_crop(1920, 1080, "full") is None
    assert vp._band_crop(0, 0, "bottom") is None


def test_pick_uniform():
    assert vp._pick_uniform(list(range(5)), 10) == [0, 1, 2, 3, 4]
    picked = vp._pick_uniform(list(range(100)), 10)
    assert len(picked) == 10
    assert picked == sorted(picked)
    assert all(0 <= i < 100 for i in picked)


def test_frame_diff():
    assert vp.frame_diff(b"\x00" * 4, b"\x10" * 4) == 16.0
    assert vp.frame_diff(b"\x00" * 4, b"\x00" * 3) == 255.0


def test_dedupe_by_difference_and_interval():
    times = [0.0, 1.0, 2.0, 3.0, 4.0]
    frames = [bytes([0]) * 16, bytes([0]) * 16, bytes([50]) * 16,
              bytes([50]) * 16, bytes([200]) * 16]
    assert vp._select_by_dedupe(times, frames, 20.0, 10.0) == [0, 2, 4]
    same = [bytes([0]) * 16] * 5
    assert vp._select_by_dedupe(times, same, 20.0, 3.0) == [0, 3]


def test_inject_http_opts_adds_reconnect_and_headers(monkeypatch):
    monkeypatch.setenv("VISION_FFMPEG_HEADERS", json.dumps({"Referer": "https://x/"}))
    args = ["-i", "http://example.com/a.mp4", "-i", "local.mp4"]
    out = vp._inject_http_opts(args)
    assert out[0] == "-reconnect"
    assert "-headers" in out
    hdr = out[out.index("-headers") + 1]
    assert "Referer" in hdr
    local_idx = out.index("local.mp4")
    first_url = out.index("http://example.com/a.mp4")
    assert "-reconnect" not in out[first_url + 1:local_idx]


def test_inject_http_opts_without_headers(monkeypatch):
    monkeypatch.delenv("VISION_FFMPEG_HEADERS", raising=False)
    out = vp._inject_http_opts(["-i", "http://example.com/a.mp4"])
    assert out[0] == "-reconnect"
    assert "-headers" not in out


def test_probe_parses_stderr(monkeypatch):
    stderr = (
        "  Duration: 00:01:23.45, start: 0.000000, bitrate: 1000 kb/s\n"
        "    Stream #0:0: Video: h264 (High), yuv420p, 1920x1080\n"
        "    Stream #0:1: Audio: aac, 44100 Hz\n"
        "    Stream #0:2: Subtitle: subrip\n"
    )
    monkeypatch.setattr(vp, "run_ffmpeg", lambda *a, **k: (1, b"", stderr))
    info = vp.probe("x.mp4")
    assert info == {"duration": 83.45, "width": 1920, "height": 1080,
                    "has_audio": True, "has_subtitle": True}


def test_extract_thumbnails_splits_raw_frames(monkeypatch):
    monkeypatch.setattr(vp, "run_ffmpeg",
                        lambda *a, **k: (0, b"\x05" * (160 * 90 * 3), ""))
    frames = vp.extract_thumbnails("x.mp4", fps=1.0, width=160, height=90)
    assert len(frames) == 3
    assert frames[0][0] == 0.0 and frames[2][0] == 2.0
    assert all(len(g) == 160 * 90 for _, g in frames)


def test_extract_subtitle_text_parses_srt(monkeypatch):
    srt = ("1\n00:00:01,000 --> 00:00:02,500\nHello\nworld\n\n"
           "2\n00:00:03,000 --> 00:00:04,000\nBye\n")
    monkeypatch.setattr(vp, "run_ffmpeg", lambda *a, **k: (0, srt.encode(), ""))
    text = vp.extract_subtitle_text("x.mkv")
    assert "[00:00:01,000 --> 00:00:02,500] Hello world" in text
    assert "Bye" in text


def test_scheme_skim_dedupes_and_picks_uniform(monkeypatch):
    info = _info(duration=60.0)
    thumbs = [(float(t), bytes([t % 251]) * (160 * 90)) for t in range(60)]
    monkeypatch.setattr(vp, "extract_thumbnails", lambda *a, **k: thumbs)
    seen = {}
    def fake_extract(path, times, max_side, input=None, crop=None,
                     fast_threshold=1800, start=None, end=None, deadline=None):
        seen["times"] = list(times)
        return [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times]
    monkeypatch.setattr(vp, "_extract_selected", fake_extract)
    fs = vp.scheme_skim("x.mp4", info, fps=1.0, max_frames=5)
    assert fs.mode == "skim"
    assert len(fs.frames) == 5
    assert seen["times"] == sorted(set(seen["times"]))
    assert fs.meta["sampled"] == 60


def test_scheme_scenes_detects_cuts(monkeypatch):
    info = _info(duration=10.0)
    thumbs = [(float(t), bytes([200]) * (160 * 90)) for t in range(10)]
    thumbs[0] = (0.0, bytes([0]) * (160 * 90))
    thumbs[1] = (1.0, bytes([0]) * (160 * 90))
    thumbs[2] = (2.0, bytes([0]) * (160 * 90))
    monkeypatch.setattr(vp, "extract_thumbnails", lambda *a, **k: thumbs)
    monkeypatch.setattr(vp, "_extract_selected",
                        lambda path, times, max_side, **k:
                        [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
    fs = vp.scheme_scenes("x.mp4", info, threshold=4.0, max_frames=24)
    assert fs.mode == "scenes"
    assert [f.t for f in fs.frames] == [0.0, 3.0]
    assert fs.meta["scene_frames"] == 2


def test_scheme_scenes_falls_back_to_uniform(monkeypatch):
    info = _info(duration=10.0)
    thumbs = [(float(t), bytes([0]) * (160 * 90)) for t in range(10)]
    monkeypatch.setattr(vp, "extract_thumbnails", lambda *a, **k: thumbs)
    monkeypatch.setattr(vp, "_extract_selected",
                        lambda path, times, max_side, **k:
                        [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
    fs = vp.scheme_scenes("x.mp4", info, max_frames=4)
    assert fs.mode == "scenes"
    assert 0 < len(fs.frames) <= 4


def test_scheme_window_clamps_end_and_requires_valid_range(monkeypatch):
    info = _info(duration=100.0)
    thumbs = [(float(t), bytes([0]) * (160 * 90)) for t in range(10)]
    monkeypatch.setattr(vp, "extract_thumbnails", lambda *a, **k: thumbs)
    monkeypatch.setattr(vp, "_extract_selected",
                        lambda path, times, max_side, **k:
                        [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
    fs = vp.scheme_window("x.mp4", info, 5.0, 200.0, fps=1.0, max_frames=24)
    assert fs.meta["window"] == [5.0, 100.0]
    try:
        vp.scheme_window("x.mp4", info, 10.0, 5.0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_scheme_text_uses_band_crop(monkeypatch):
    info = _info()
    thumbs = [(float(t), bytes([0]) * (160 * 90)) for t in range(10)]
    monkeypatch.setattr(vp, "extract_thumbnails", lambda *a, **k: thumbs)
    crops = []
    def fake_extract(path, times, max_side, input=None, crop=None,
                     fast_threshold=1800, start=None, end=None, deadline=None):
        crops.append(crop)
        return [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times]
    monkeypatch.setattr(vp, "_extract_selected", fake_extract)
    fs = vp.scheme_text("x.mp4", info, fps=1.0, max_frames=8)
    assert fs.meta["band"] == "bottom"
    assert fs.meta["crop"] == "1280:180:0:540"
    assert crops == ["1280:180:0:540"]


def test_scheme_segments_picks_evenly_spaced_times(monkeypatch):
    info = _info(duration=100.0)
    monkeypatch.setattr(vp, "_extract_selected",
                        lambda path, times, max_side, **k:
                        [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
    fs = vp.scheme_segments("x.mp4", info, n=4)
    assert [f.t for f in fs.frames] == [12.5, 37.5, 62.5, 87.5]
    assert fs.meta == {"n": 4}


def test_scheme_burst_delegates_without_dedupe(monkeypatch):
    info = _info(duration=100.0)
    thumbs = [(float(t), bytes([0]) * (160 * 90)) for t in range(30)]
    monkeypatch.setattr(vp, "extract_thumbnails", lambda *a, **k: thumbs)
    monkeypatch.setattr(vp, "_extract_selected",
                        lambda path, times, max_side, **k:
                        [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
    fs = vp.scheme_burst("x.mp4", info, 10.0, duration=3.0, fps=5.0, max_frames=24)
    assert fs.mode == "burst"
    assert fs.meta["window"] == [10.0, 13.0]
    assert len(fs.frames) <= 24


def test_scheme_contact_builds_tile_filter(monkeypatch):
    captured = {}
    def fake_run_ffmpeg(args, timeout=3600, input=None, deadline=None):
        captured["args"] = list(args)
        return 0, _jpeg(), ""
    monkeypatch.setattr(vp, "run_ffmpeg", fake_run_ffmpeg)
    fs = vp.scheme_contact("x.mp4", _info(duration=60.0), n=16)
    assert fs.mode == "contact"
    assert fs.meta["cells"] == 16
    assert fs.meta["cols"] == 4 and fs.meta["rows"] == 4
    vf = captured["args"][captured["args"].index("-vf") + 1]
    assert "scale=160:90" in vf
    assert "tile=4x4" in vf
    assert len(fs.meta["timestamps"]) == 16


def test_fill_gaps_inserts_midpoints():
    all_times = [float(t) for t in range(11)]
    assert vp._fill_gaps([0.0, 3.0], all_times, 2.0, 24) == [0.0, 1.0, 3.0]
    out = vp._fill_gaps([0.0, 10.0], all_times, 1.0, 24)
    assert out == sorted(out) and set(out) == set(all_times)


def test_fill_gaps_respects_frame_cap():
    all_times = [float(t) for t in range(101)]
    out = vp._fill_gaps([0.0, 100.0], all_times, 1.0, 10)
    assert len(out) == 10
    assert out[0] == 0.0 and out[-1] == 100.0


def test_scheme_scenes_fills_static_gaps(monkeypatch):
    info = _info(duration=100.0)
    thumbs = [(float(t), bytes([0]) * (160 * 90)) for t in range(100)]
    monkeypatch.setattr(vp, "extract_thumbnails", lambda *a, **k: thumbs)
    monkeypatch.setattr(vp, "_extract_selected",
                        lambda path, times, max_side, **k:
                        [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
    fs = vp.scheme_scenes("x.mp4", info, threshold=4.0, max_frames=24,
                          max_gap=15.0)
    assert fs.mode == "scenes"
    assert fs.meta["max_gap"] == 15.0
    gaps = [b.t - a.t for a, b in zip(fs.frames, fs.frames[1:])]
    assert gaps and max(gaps) <= 15.0


def test_extract_frame_seek_injects_keyframe_option(monkeypatch):
    captured = {}
    monkeypatch.setattr(vp, "probe", lambda path, input=None, deadline=None: _info(duration=100.0))

    def fake_run(args, timeout=180, input=None, deadline=None):
        captured["args"] = list(args)
        return 0, _jpeg(), ""

    monkeypatch.setattr(vp, "run_ffmpeg", fake_run)
    f = vp.extract_frame_seek("x.mp4", 10.0, 1600, keyframes=True)
    assert f is not None
    assert captured["args"][:2] == ["-skip_frame", "nokey"]
    captured.clear()
    vp.extract_frame_seek("x.mp4", 10.0, 1600)
    assert "-skip_frame" not in captured["args"]


def test_extract_selected_uses_single_pass_short_and_keyframes_long(monkeypatch):
    calls = {"all": 0, "seek": []}

    def fake_all(path, fps, max_side, start=None, end=None, crop=None, input=None,
                 deadline=None):
        calls["all"] += 1
        raise RuntimeError("force seek fallback")

    def fake_seek(path, t, max_side, input=None, crop=None, keyframes=False,
                  deadline=None):
        calls["seek"].append((t, keyframes))
        return vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90)

    monkeypatch.setattr(vp, "extract_all_jpegs", fake_all)
    monkeypatch.setattr(vp, "extract_frame_seek", fake_seek)
    vp._extract_selected("x.mp4", [1.0, 2.0], 1600)
    vp._extract_selected("x.mp4", [2000.0], 1600)
    assert calls["all"] == 1
    assert (1.0, False) in calls["seek"] and (2.0, False) in calls["seek"]
    assert calls["seek"][-1] == (2000.0, True)


def test_run_ffmpeg_timeout_raises_friendly_error(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)

    monkeypatch.setattr(vp, "ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(vp.subprocess, "run", boom)
    try:
        vp.run_ffmpeg(["-i", "x.mp4"], timeout=60)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "time limit" in str(e)


def test_extract_thumbnails_adds_frame_limit_arg(monkeypatch):
    captured = {}

    def fake_run(args, timeout=3600, input=None, deadline=None):
        captured["args"] = list(args)
        return 0, b"\x05" * (160 * 90 * 2), ""

    monkeypatch.setattr(vp, "run_ffmpeg", fake_run)
    frames = vp.extract_thumbnails("x.mp4", fps=1.0, max_frames=5)
    idx = captured["args"].index("-frames:v")
    assert captured["args"][idx + 1] == "5"
    # only 2 frames existed in the output; both are kept
    assert len(frames) == 2
    assert frames[0][0] == 0.0 and frames[1][0] == 1.0


def test_extract_thumbnails_truncates_to_max_frames(monkeypatch):
    blob = b"\x05" * (160 * 90 * 100)
    monkeypatch.setattr(vp, "run_ffmpeg", lambda *a, **k: (0, blob, ""))
    frames = vp.extract_thumbnails("x.mp4", fps=1.0, max_frames=10)
    assert len(frames) == 10


def test_extract_thumbnails_without_limit_keeps_all(monkeypatch):
    captured = {}

    def fake_run(args, timeout=3600, input=None, deadline=None):
        captured["args"] = list(args)
        return 0, b"\x05" * (160 * 90 * 3), ""

    monkeypatch.setattr(vp, "run_ffmpeg", fake_run)
    frames = vp.extract_thumbnails("x.mp4", fps=1.0)
    assert len(frames) == 3
    assert "-frames:v" not in captured["args"]


def test_bounded_fps():
    assert vp._bounded_fps(1.0, 100.0) == 1.0
    assert vp._bounded_fps(10.0, 100.0) == 10.0
    assert vp._bounded_fps(2.0, 1000.0) == pytest.approx(1.2)
    assert vp._bounded_fps(5.0, 1200.0) == 1.0
    assert vp._bounded_fps(1.0, 2000.0) == pytest.approx(0.6)
    assert vp._bounded_fps(5.0, None) == 5.0
    assert vp._bounded_fps(5.0, 0) == 5.0
    assert vp._bounded_fps(2.0, -1) == 2.0


def test_long_video_schemes_use_bounded_fps(monkeypatch):
    """A 2-hour video must not produce unbounded thumbnail candidates."""
    info = _info(duration=7200.0)
    seen = {}

    def fake_thumbs(path, fps=None, max_frames=None, **k):
        seen["fps"] = fps
        seen["max_frames"] = max_frames
        return [(0.0, bytes(160 * 90)), (1.0, bytes(160 * 90))]

    monkeypatch.setattr(vp, "extract_thumbnails", fake_thumbs)
    monkeypatch.setattr(vp, "_extract_selected",
                        lambda path, times, max_side, **k:
                        [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
    vp.scheme_skim("x.mp4", info, fps=1.0, max_frames=5)
    assert seen["max_frames"] == vp.MAX_THUMBNAIL_FRAMES
    assert seen["fps"] == pytest.approx(vp.MAX_THUMBNAIL_FRAMES / 7200.0)


def test_scheme_window_passes_window_bounds_to_final_extraction(monkeypatch):
    info = _info(duration=100.0)
    seen = {}

    def fake_thumbs(path, fps=None, start=None, end=None, **k):
        seen["thumbs"] = (start, end)
        seen["fps"] = fps
        return [(float(t), bytes([0]) * (160 * 90)) for t in range(30)]

    def fake_extract(path, times, max_side, input=None, crop=None,
                     start=None, end=None, deadline=None):
        seen["extract"] = (start, end)
        return [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times]

    monkeypatch.setattr(vp, "extract_thumbnails", fake_thumbs)
    monkeypatch.setattr(vp, "_extract_selected", fake_extract)
    fs = vp.scheme_window("x.mp4", info, 10.0, 20.0, fps=1.0, max_frames=5)
    assert seen["thumbs"] == (10.0, 20.0)
    assert seen["extract"] == (10.0, 20.0)
    assert seen["fps"] == 1.0
    assert fs.meta["window"] == [10.0, 20.0]


def test_scheme_window_short_window_does_not_decode_full_video(monkeypatch):
    """Regression: a 10s window inside a 2h video must bound the ffmpeg pass
    to the window, not decode the whole file."""
    info = _info(duration=7200.0)
    seen = {}

    def fake_thumbs(path, fps=None, start=None, end=None, **k):
        seen["thumbs"] = (start, end)
        return [(float(t), bytes([0]) * (160 * 90)) for t in range(10)]

    def fake_extract(path, times, max_side, input=None, crop=None,
                     start=None, end=None, deadline=None):
        seen["extract"] = (start, end)
        return [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times]

    monkeypatch.setattr(vp, "extract_thumbnails", fake_thumbs)
    monkeypatch.setattr(vp, "_extract_selected", fake_extract)
    vp.scheme_window("x.mp4", info, 100.0, 110.0, fps=1.0, max_frames=5)
    assert seen["thumbs"] == (100.0, 110.0)
    assert seen["extract"] == (100.0, 110.0)


def test_scheme_candidates_never_exceed_hard_cap(monkeypatch):
    """All thumbnail-candidate producers must stay under MAX_THUMBNAIL_FRAMES,
    even when max_frames is small."""
    info = _info(duration=10 ** 6)
    for scheme in (vp.scheme_skim, vp.scheme_scenes, vp.scheme_text):
        seen = {}
        def fake_thumbs(path, fps=None, max_frames=None, **k):
            seen["fps"] = fps
            seen["max_frames"] = max_frames
            return [(0.0, bytes(160 * 90))]
        monkeypatch.setattr(vp, "extract_thumbnails", fake_thumbs)
        monkeypatch.setattr(vp, "_extract_selected",
                            lambda path, times, max_side, **k:
                            [vp.Frame(t=t, jpeg=_jpeg(), w=160, h=90) for t in times])
        fs = scheme("x.mp4", info, max_frames=5)
        assert seen["max_frames"] == vp.MAX_THUMBNAIL_FRAMES
        assert seen["fps"] <= vp.MAX_THUMBNAIL_FRAMES / info["duration"] or len(fs.frames) <= 5
