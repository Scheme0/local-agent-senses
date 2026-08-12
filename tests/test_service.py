"""Shared service facade tests: no Ollama, ffmpeg or subprocesses required."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import service  # noqa: E402
import vision  # noqa: E402


def test_describe_image_uses_in_process_vision(monkeypatch):
    calls = []
    monkeypatch.setattr(vision, "ensure_ollama_started", lambda: True)
    monkeypatch.setattr(vision, "resolve_model", lambda mode: "test-model")

    def fake_analyze(args):
        calls.append(args)
        print(json.dumps({"text": "ok", "mode": "image"}))

    monkeypatch.setattr(vision, "analyze_images", fake_analyze)
    result = service.execute("describe_image", {
        "images": ["a.png"], "prompt": "describe", "size": "small"
    })

    assert json.loads(result)["text"] == "ok"
    assert calls[0].media == ["a.png"]
    assert calls[0].size == "small"


def test_unknown_service_operation_is_rejected(monkeypatch):
    monkeypatch.setattr(vision, "ensure_ollama_started", lambda: True)
    try:
        service.execute("not-a-tool", {})
    except RuntimeError as exc:
        assert "Unknown tool" in str(exc)
    else:
        raise AssertionError("unknown service operation was accepted")


def test_video_time_arguments_and_global_state_are_restored(monkeypatch):
    calls = []
    monkeypatch.setattr(vision, "ensure_ollama_started", lambda: True)
    monkeypatch.setattr(vision, "resolve_model", lambda mode: "temporary-model")

    def fake_analyze(media, args):
        calls.append((media, args))
        assert vision.ACTIVE_MODEL == "temporary-model"
        print(json.dumps({"text": "ok", "mode": "window"}))

    monkeypatch.setattr(vision, "analyze_video", fake_analyze)
    vision.ACTIVE_MODEL = "original-model"
    vision.CTX_OVERRIDE = 1234
    result = service.execute("analyze_video", {
        "video": "clip.mp4", "mode": "window", "from": "1:20",
        "to": "2:00", "prompt": "look", "context": "prior",
        "duration": 4, "fps": 2, "max_frames": 8, "no_dedupe": True,
    })

    assert json.loads(result)["text"] == "ok"
    media, args = calls[0]
    assert media == "clip.mp4"
    assert args.start == 80.0 and args.end == 120.0
    assert args.context == "prior"
    assert args.duration == 4 and args.fps == 2 and args.max_frames == 8
    assert args.no_dedupe is True
    assert vision.ACTIVE_MODEL == "original-model"
    assert vision.CTX_OVERRIDE == 1234


def test_execute_result_normalizes_json_payload(monkeypatch):
    monkeypatch.setattr(service, "execute", lambda tool, args: json.dumps({
        "text": "recognized", "mode": "image", "media": ["a.png"]
    }))
    result = service.execute_result("describe_image", {"images": ["a.png"]})
    assert result.text == "recognized"
    assert result.kind == "describe_image"
    assert result.mode == "image"
    assert result.metadata == {"media": ["a.png"]}
    assert result.to_dict()["warnings"] == []


def test_execute_result_wraps_expected_errors(monkeypatch):
    monkeypatch.setattr(service, "execute",
                        lambda tool, args: (_ for _ in ()).throw(RuntimeError("bad input")))
    try:
        service.execute_result("describe_image", {})
    except service.ServiceError as exc:
        assert exc.code == "service_error"
        assert exc.to_dict()["message"] == "bad input"
    else:
        raise AssertionError("expected structured service error")
