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
    monkeypatch.setattr(service, "execute", lambda tool, args, deadline=None: json.dumps({
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
                        lambda tool, args, deadline=None:
                        (_ for _ in ()).throw(RuntimeError("bad input")))
    try:
        service.execute_result("describe_image", {})
    except service.ServiceError as exc:
        assert exc.code == "service_error"
        assert exc.to_dict()["message"] == "bad input"
    else:
        raise AssertionError("expected structured service error")


def test_service_busy_error(monkeypatch):
    monkeypatch.setattr(service, "execute", lambda tool, args, deadline=None: '{"text":"ok"}')
    monkeypatch.setattr(service, "_service_slot", lambda: _BusySlot())
    try:
        service.execute_result("describe_image", {})
    except service.ServiceError as exc:
        assert exc.code == "busy"
    else:
        raise AssertionError("expected busy error")


class _BusySlot:
    def acquire(self, timeout=None):
        return False

    def release(self):
        pass


def test_deadline_is_attached_to_request_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(vision, "ensure_ollama_started", lambda: True)
    monkeypatch.setattr(vision, "resolve_model", lambda mode: "m")

    def fake_analyze(args):
        captured["deadline"] = getattr(args, "deadline", None)
        print(json.dumps({"text": "ok", "mode": "image"}))

    monkeypatch.setattr(vision, "analyze_images", fake_analyze)
    service.execute_result("describe_image", {"images": ["a.png"]})
    assert captured["deadline"] is not None
    assert captured["deadline"] > 0


def test_execute_result_maps_deadline_exceeded(monkeypatch):
    def boom(tool, args, deadline=None):
        raise service.DeadlineExceededError("deadline exceeded at video probe")

    monkeypatch.setattr(service, "execute", boom)
    try:
        service.execute_result("analyze_video", {"video": "a.mp4"})
    except service.ServiceError as exc:
        assert exc.code == "deadline_exceeded"
    else:
        raise AssertionError("expected deadline_exceeded service error")


def test_execute_result_classifies_errors(monkeypatch):
    cases = {
        "security_error": "Blocked media URL (SSRF): 127.0.0.1",
        "media_error": "ffmpeg frame extraction failed: corrupt stream",
        "backend_unavailable": "Cannot connect to Ollama (connection refused)",
        "dependency_missing": "ffmpeg not found",
        "invalid_input": "max_frames must be an integer",
    }
    for expected, message in cases.items():
        def boom(tool, args, deadline=None, _msg=message):
            raise RuntimeError(_msg)
        monkeypatch.setattr(service, "execute", boom)
        try:
            service.execute_result("describe_image", {})
            raise AssertionError("expected an error")
        except service.ServiceError as exc:
            assert exc.code == expected, (expected, message, exc.code)


def test_execute_result_honors_deadline_after_execution(monkeypatch):
    import config as _config

    monkeypatch.setattr(service, "execute",
                        lambda tool, args, deadline=None: '{"text":"late"}')
    monkeypatch.setattr(_config, "service_execution_timeout_sec", lambda: 0.001)
    monkeypatch.setattr(service, "time", _AdvancingTime())
    try:
        service.execute_result("describe_image", {})
        raise AssertionError("expected deadline_exceeded")
    except service.ServiceError as exc:
        assert exc.code == "deadline_exceeded"


class _AdvancingTime:
    """monotonic() advances past the deadline on every call."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        self.t += 10000.0
        return self.t


def test_vision_stage_check_blocks_after_deadline():
    """The stage checks must not start a new expensive stage once the
    deadline has passed."""
    import time as _t
    deadline = _t.monotonic() - 1  # already expired
    try:
        vision._check_deadline(deadline, "video probe")
        raise AssertionError("expected DeadlineExceededError")
    except service.DeadlineExceededError as exc:
        assert "video probe" in str(exc)
    vision._check_deadline(None, "video probe")  # no deadline: no-op


def test_ask_model_uses_remaining_timeout(monkeypatch):
    captured = {}

    def fake_chat(model, prompt, images, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return "ok"

    monkeypatch.setattr(vision.ollama_client, "chat", fake_chat)
    monkeypatch.setattr(vision, "ensure_single_resident", lambda *a: None)
    deadline = __import__("time").monotonic() + 30
    vision.ask_model("p", [], "auto", deadline=deadline)
    assert 1 <= captured["timeout"] <= 30
