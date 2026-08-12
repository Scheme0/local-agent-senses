# -*- coding: utf-8 -*-
"""Pure-function unit tests: no GPU / Ollama / ffmpeg needed, CI-safe."""
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import vision  # noqa: E402

TMP_ROOT = Path(tempfile.gettempdir()) / "local-agent-senses-tests"


def _with_config(cfg: dict, fn) -> None:
    """Write a temp config under the repo, set VISION_CONFIG, then restore."""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    d = tempfile.mkdtemp(dir=str(TMP_ROOT))
    p = Path(d) / "vision-config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    old = os.environ.get("VISION_CONFIG")
    os.environ["VISION_CONFIG"] = str(p)
    config.user_config.cache_clear()
    try:
        fn()
    finally:
        if old is None:
            os.environ.pop("VISION_CONFIG", None)
        else:
            os.environ["VISION_CONFIG"] = old
        config.user_config.cache_clear()
        shutil.rmtree(d, ignore_errors=True)


def test_parse_time():
    assert vision.parse_time("90") == 90
    assert vision.parse_time("1:30") == 90
    assert vision.parse_time("00:01:30") == 90
    try:
        vision.parse_time("abc")
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_mojibake_score():
    assert vision.mojibake_score("????? Visual Test\n?? 2026-08-06 | ?? #42") >= vision.MOJIBAKE_THRESHOLD
    assert vision.mojibake_score("\u89c6\u89c9\u529f\u80fd\u6d4b\u8bd5 Visual Test") < vision.MOJIBAKE_THRESHOLD
    assert vision.mojibake_score("[?] [?] \u65e0\u6cd5\u8fa8\u8ba4\u5904") < vision.MOJIBAKE_THRESHOLD
    assert vision.mojibake_score("What??? \u8fd9\u662f\u4ec0\u4e48") < vision.MOJIBAKE_THRESHOLD
    assert vision.mojibake_score("\ufffd\ufffd \u4e71\u7801") >= vision.MOJIBAKE_THRESHOLD


def test_transcribe_prompt_enforced():
    prompt = vision.build_transcribe_prompt("answer with text only")
    assert "transcription engine" in prompt
    assert "Additional requirement: answer with text only" in prompt
    assert "Do not summarize" in prompt


def test_image_transcribe_multi():
    p2 = vision.build_image_transcribe_prompt(2, None, None)
    assert "Image N:" in p2
    assert "Transcribe each one" in p2
    p1 = vision.build_image_transcribe_prompt(1, None, None)
    assert "Image N:" not in p1


def test_video_transcribe_prompt_no_description_wrapper():
    frameset = types.SimpleNamespace(
        frames=[types.SimpleNamespace(t=0.0), types.SimpleNamespace(t=1.5)],
        meta={"band": "bottom"},
        mode="text",
        duration=2.0,
    )
    p = vision.build_video_transcribe_prompt(frameset, None, "subtitles")
    assert "Frame1@0.0s" in p
    assert "Frame2@1.5s" in p
    assert "Describe the scene content and changes in chronological order" not in p


def test_config_file_values():
    cfg = {
        "text_model": "m-text",
        "quick_model": "m-quick",
        "max_tokens": "1234",
        "quick_think": "true",
        "single_resident": "false",
        "keep_alive": "5m",
        "max_duration_h": "2",
        "max_image_mb": "7",
        "mcp_cache": "false",
        "direct_url_stream": "true",
    }

    def check():
        assert config.text_model() == "m-text"
        assert config.quick_model() == "m-quick"
        assert config.text_max_tokens() == 1234
        assert config.quick_think() is True
        assert config.single_resident() is False
        assert config.keep_alive() == "5m"
        assert config.max_duration_h() == 2.0
        assert config.max_image_mb() == 7
        assert config.mcp_cache_enabled() is False
        assert config.direct_url_streaming() is True

    _with_config(cfg, check)


def test_env_overrides_config():
    try:
        os.environ["VISION_TEXT_MODEL"] = "from-env"

        def check():
            assert config.text_model() == "from-env"

        _with_config({"text_model": "from-file"}, check)
    finally:
        os.environ.pop("VISION_TEXT_MODEL", None)


def test_explicit_config_path_no_fallback():
    os.environ["VISION_CONFIG"] = str(Path("Z:/nonexistent/vision-config.json"))
    config.user_config.cache_clear()
    try:
        assert config.user_config() == {}
        assert config.text_model() == "haervwe/GLM-4.6V-Flash-9B"
    finally:
        os.environ.pop("VISION_CONFIG", None)
        config.user_config.cache_clear()


def test_speech_langs_single_source():
    assert config.SPEECH_LANGS == ("auto", "zh", "en", "yue", "ja", "ko")


def test_json_result_helpers():
    frameset = types.SimpleNamespace(
        frames=[types.SimpleNamespace(t=0.0, w=640, h=480),
                types.SimpleNamespace(t=1.5, w=640, h=480)],
        mode="skim", duration=2.0, meta={"sampled": 2},
    )
    out = vision.json_result_video("hello", frameset, "t.mkv", "url")
    assert out["text"] == "hello"
    assert out["mode"] == "skim"
    assert out["duration"] == 2.0
    assert out["frames"] == [{"t": 0.0, "w": 640, "h": 480},
                             {"t": 1.5, "w": 640, "h": 480}]
    assert out["title"] == "t.mkv" and out["source"] == "url"
    assert out["meta"] == {"sampled": 2}
    assert vision.json_result_image("hi", ["a.png", "b.png"]) == {
        "text": "hi", "mode": "image", "media": ["a.png", "b.png"]}
    assert vision.json_result_audio("words", "asr") == {
        "text": "words", "source": "asr"}


def test_enforce_duration_limit(monkeypatch):
    monkeypatch.setenv("VISION_MAX_DURATION_H", "2")
    vision.enforce_duration_limit(1.0, "Video (x)")  # no error
    try:
        vision.enforce_duration_limit(3 * 3600, "Video (x)")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "3.0 hours" in str(e)
        assert "VISION_MAX_DURATION_H" in str(e)
    monkeypatch.setenv("VISION_MAX_DURATION_H", "0")
    vision.enforce_duration_limit(1000.0, "Video (x)")  # disabled


def test_run_speech_proc_starts_pump_before_stdin_write():
    """Regression: a chatty child must not deadlock a large stdin payload."""
    child = ("import sys, time; "
             "sys.stdout.write('x' * 300000); sys.stdout.flush(); "
             "time.sleep(1); data = sys.stdin.buffer.read(); "
             "print('GOT', len(data))")
    out = vision._run_speech_proc(
        [sys.executable, "-c", child], b"y" * 2000000, timeout=60)
    assert out.startswith("x" * 300000)
    assert out.rstrip().endswith("GOT 2000000")


def test_gen_probe_image_returns_valid_png():
    data = vision.gen_probe_image()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 1000


def test_analyze_audio_rejects_image_input(monkeypatch):
    args = types.SimpleNamespace(json=False, lang="auto", asr_model="sensevoice")
    spec = vision.media.MediaSpec(kind="image", source="local", path="x.png")
    monkeypatch.setattr(vision.media, "resolve_input",
                        lambda *a, **k: spec)
    try:
        vision.analyze_audio("x.png", args)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "is an image" in str(e)
