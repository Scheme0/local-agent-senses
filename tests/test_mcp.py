# -*- coding: utf-8 -*-
"""MCP server protocol tests: no GPU / Ollama, just stdio JSON-RPC round-trips."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "extras" / "mcp_server.py"
_TMP = Path(tempfile.gettempdir()) / "local-agent-senses-tests" / "mcp-cache"
_DISK = _TMP / "disk-cache"


@pytest.fixture(autouse=True)
def _isolated_disk_cache():
    _DISK.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("VISION_MCP_CACHE_DIR")
    old_enabled = os.environ.get("VISION_MCP_CACHE")
    os.environ["VISION_MCP_CACHE_DIR"] = str(_DISK)
    os.environ["VISION_MCP_CACHE"] = "1"
    yield
    if old is None:
        os.environ.pop("VISION_MCP_CACHE_DIR", None)
    else:
        os.environ["VISION_MCP_CACHE_DIR"] = old
    if old_enabled is None:
        os.environ.pop("VISION_MCP_CACHE", None)
    else:
        os.environ["VISION_MCP_CACHE"] = old_enabled
    shutil.rmtree(_DISK, ignore_errors=True)


def _call(lines: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MCP_SERVER)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _responses(proc: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _load_mcp_module():
    spec = importlib.util.spec_from_file_location("mcp_server_under_test", MCP_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _temp_file(name: str, data: bytes) -> Path:
    _TMP.mkdir(parents=True, exist_ok=True)
    path = _TMP / name
    path.write_bytes(data)
    return path


def test_mcp_initialize():
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }
    proc = _call([json.dumps(req, ensure_ascii=False)])
    assert proc.returncode == 0
    resp = _responses(proc)[0]
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "local-agent-senses"


def test_mcp_tools_list():
    proc = _call([
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    ])
    resp = _responses(proc)[0]
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "describe_image" in names
    assert "transcribe" in names
    assert "analyze_video" in names
    assert "transcribe_audio" in names
    assert "vision_status" in names


def test_mcp_ping_and_notification():
    proc = _call([
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized",
                    "params": {}}),
    ])
    resps = _responses(proc)
    assert resps[0]["id"] == 3 and resps[0]["result"] == {}
    assert len(resps) == 1  # notifications get no response


def test_mcp_unknown_tool_returns_error_content():
    proc = _call([
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "no_such_tool", "arguments": {}}}),
    ])
    resp = _responses(proc)[0]
    assert resp["result"]["isError"] is True
    assert "Unknown tool" in resp["result"]["content"][0]["text"]


def test_mcp_unknown_method():
    proc = _call([
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "bogus", "params": {}}),
    ])
    resp = _responses(proc)[0]
    assert resp["error"]["code"] == -32601


def test_mcp_cache_serves_second_call_without_running_cli():
    mod = _load_mcp_module()
    calls = []

    def fake_run_cli(args):
        calls.append(args)
        return "cached-output"

    mod.run_service = lambda tool, args: fake_run_cli([])
    img = _temp_file("cache-hit.png", b"fake-image-bytes")
    args = {"images": [str(img)], "prompt": "what is this"}

    first = mod.call_tool("describe_image", args)
    second = mod.call_tool("describe_image", args)

    assert first == second == "cached-output"
    assert len(calls) == 1


def test_mcp_cache_invalidates_when_local_file_changes():
    mod = _load_mcp_module()
    outputs = iter(["old-output", "new-output"])

    def fake_run_cli(args):
        return next(outputs)

    mod.run_service = lambda tool, args: fake_run_cli([])
    img = _temp_file("cache-invalidate.png", b"version-one")
    args = {"images": [str(img)], "prompt": "what is this"}

    assert mod.call_tool("describe_image", args) == "old-output"
    img.write_bytes(b"version-two-longer")
    assert mod.call_tool("describe_image", args) == "new-output"


def test_mcp_cache_expires_after_ttl():
    mod = _load_mcp_module()
    outputs = iter(["first", "second"])

    def fake_run_cli(args):
        return next(outputs)

    mod.run_service = lambda tool, args: fake_run_cli([])
    img = _temp_file("cache-ttl.png", b"fake-image-bytes")
    args = {"images": [str(img)], "prompt": "what is this"}

    assert mod.call_tool("describe_image", args) == "first"
    mod.CACHE_TTL = -1
    assert mod.call_tool("describe_image", args) == "second"


def test_mcp_disk_cache_persists_across_instances():
    mod1 = _load_mcp_module()
    calls1 = []

    def fake_run_cli(args):
        calls1.append(args)
        return "disk-persisted"

    mod1.run_service = lambda tool, args: fake_run_cli([])
    img = _temp_file("disk-cache-hit.png", b"same-bytes")
    args = {"images": [str(img)], "prompt": "q"}
    assert mod1.call_tool("describe_image", args) == "disk-persisted"
    assert len(calls1) == 1

    mod2 = _load_mcp_module()  # fresh module: empty in-memory cache
    calls2 = []
    mod2.run_service = lambda tool, args: calls2.append((tool, args)) or "unexpected"
    assert mod2.call_tool("describe_image", args) == "disk-persisted"
    assert calls2 == []


def test_mcp_disk_cache_disabled_writes_nothing():
    os.environ["VISION_MCP_CACHE"] = "0"
    mod = _load_mcp_module()
    mod.run_service = lambda tool, args: "x"
    img = _temp_file("disk-off.png", b"bytes")
    assert mod.call_tool("describe_image",
                         {"images": [str(img)], "prompt": "q"}) == "x"
    assert list(_DISK.glob("*.json")) == []


def test_mcp_tools_use_json_flag():
    mod = _load_mcp_module()
    captured = []
    mod.run_service = lambda tool, args: captured.append((tool, args)) or "{}"
    mod.call_tool("describe_image", {"images": ["a.png"], "prompt": "p"})
    mod.call_tool("transcribe", {"media": "a.mp4"})
    mod.call_tool("analyze_video", {"video": "a.mp4", "prompt": "p"})
    mod.call_tool("transcribe_audio", {"media": "a.wav"})
    assert len(captured) == 4
    assert {tool for tool, args in captured} == {
        "describe_image", "transcribe", "analyze_video", "transcribe_audio"
    }


def test_mcp_schema_has_only_truly_required_media_fields():
    tools = {tool["name"]: tool for tool in _load_mcp_module().TOOLS}
    assert tools["describe_image"]["inputSchema"]["required"] == ["images"]
    assert tools["analyze_video"]["inputSchema"]["required"] == ["video"]


def test_mcp_service_success_is_structured_json(monkeypatch):
    mod = _load_mcp_module()
    mod.run_service = lambda tool, args: json.dumps({
        "text": "ok", "kind": tool, "mode": "image", "metadata": {}, "warnings": []
    })
    response = mod.handle_request({
        "jsonrpc": "2.0", "id": 22, "method": "tools/call",
        "params": {"name": "describe_image", "arguments": {"images": ["a.png"]}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert response["result"]["isError"] is False
    assert payload["kind"] == "describe_image"


def test_mcp_service_error_has_code_and_message(monkeypatch):
    mod = _load_mcp_module()
    error = RuntimeError("invalid media")
    error.code = "invalid_input"
    mod.run_service = lambda tool, args: (_ for _ in ()).throw(error)
    response = mod.handle_request({
        "jsonrpc": "2.0", "id": 23, "method": "tools/call",
        "params": {"name": "describe_image", "arguments": {"images": ["a.png"]}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert response["result"]["isError"] is True
    assert payload == {"code": "invalid_input", "message": "invalid media"}


def test_mcp_disk_cache_write_is_atomic(monkeypatch):
    mod = _load_mcp_module()
    key = ("describe_image", (("images", ("x.png",)),))
    mod._disk_write(key, "atomic")
    path = mod._disk_path(key)
    assert path is not None and path.exists()
    assert not list(path.parent.glob("*.tmp"))


def test_mcp_transcribe_forwards_full_option_set():
    mod = _load_mcp_module()
    captured = []
    mod.run_service = lambda tool, args: captured.append((tool, args)) or "{}"
    mod.call_tool("transcribe", {
        "media": "a.mp4", "prompt": "p", "context": "c", "crop": "1x1+0+0",
        "size": "small", "max_frames": 8, "band": "top", "no_dedupe": True,
    })
    assert captured == [("transcribe", {
        "media": "a.mp4", "prompt": "p", "context": "c", "crop": "1x1+0+0",
        "size": "small", "max_frames": 8, "band": "top", "no_dedupe": True,
    })]
