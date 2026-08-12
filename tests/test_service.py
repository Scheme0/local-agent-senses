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
