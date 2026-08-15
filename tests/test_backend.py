# -*- coding: utf-8 -*-
"""Backend message-construction tests: no network access."""
import base64
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import ollama_client  # noqa: E402


def test_build_openai_messages():
    msgs = ollama_client.build_openai_messages("hi", ["AAAA", "BBBB"])
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,AAAA"},
    }
    assert len(content) == 3


def test_openai_data_uri_uses_real_mime():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"payload").decode()
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"payload").decode()
    webp = base64.b64encode(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"data").decode()
    msgs = ollama_client.build_openai_messages("hi", [png, jpeg, webp])
    urls = [part["image_url"]["url"] for part in msgs[0]["content"][1:]]
    assert urls[0].startswith("data:image/png;base64,")
    assert urls[1].startswith("data:image/jpeg;base64,")
    assert urls[2].startswith("data:image/webp;base64,")


def test_openai_extract_list_content_and_thinking_fallback():
    list_data = {"choices": [{"message": {
        "content": [{"type": "text", "text": "hello"}]}}]}
    assert ollama_client._extract_openai_text(list_data) == "hello"
    thinking_data = {"choices": [{"message": {
        "content": "", "reasoning_content": "visible answer"}}]}
    assert ollama_client._extract_openai_text(thinking_data) == "visible answer"
    try:
        ollama_client._extract_openai_text(
            {"choices": [{"message": {"content": ""}}]})
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_native_chat_falls_back_to_thinking(monkeypatch):
    monkeypatch.setattr(config, "use_openai_api", lambda: False)

    def fake_api(method, path, payload, timeout=600):
        assert path == "/api/chat"
        return {"message": {"content": "", "thinking": "answer from thinking"}}

    monkeypatch.setattr(ollama_client, "api", fake_api)
    out = ollama_client.chat("m", "p", [], num_predict=1, num_ctx=2)
    assert out == "answer from thinking"


def test_native_chat_raises_when_content_and_thinking_empty(monkeypatch):
    monkeypatch.setattr(config, "use_openai_api", lambda: False)
    monkeypatch.setattr(ollama_client, "api",
                        lambda *a, **k: {"message": {"content": ""}})
    try:
        ollama_client.chat("m", "p", [], num_predict=1, num_ctx=2)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_openai_error_does_not_leak_api_key(monkeypatch):
    monkeypatch.setattr(config, "api_base",
                        lambda: "https://api.example.com/v1")
    monkeypatch.setenv("VISION_API_KEY", "sk-topsecret")

    def boom(req, data=None, method=None, timeout=600):
        raise urllib.error.HTTPError("https://api.example.com/v1/chat/completions",
                                     401, "Unauthorized", {}, None)

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", boom)
    try:
        ollama_client.openai_api("POST", "/chat/completions", {"model": "x"})
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "sk-topsecret" not in str(exc)
        assert "Bearer" not in str(exc)
