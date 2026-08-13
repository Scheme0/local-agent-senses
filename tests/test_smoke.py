"""Offline end-to-end smoke tests for the public command paths."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VISION = ROOT / "vision.py"
MCP = ROOT / "extras" / "mcp_server.py"


def _run(path, *args, input_text=None):
    return subprocess.run([sys.executable, str(path), *args], cwd=ROOT,
                          input=input_text, text=True, capture_output=True,
                          timeout=30)


def test_cli_doctor_json_is_offline_and_structured():
    proc = _run(VISION, "--doctor", "--json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert "ok" in payload and isinstance(payload["checks"], list)
    assert {item["name"] for item in payload["checks"]} >= {"python", "mcp"}
    assert any(item["name"] == "models" and item["status"] == "not-checked"
               for item in payload["checks"])


def test_cli_doctor_returns_without_backend_within_smoke_budget(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://192.0.2.1:9")
    proc = subprocess.run([sys.executable, str(VISION), "--doctor", "--json"],
                          cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0


def test_cli_doctor_does_not_scan_conda(monkeypatch):
    import config
    monkeypatch.setattr(config, "speech_python", lambda: (_ for _ in ()).throw(
        AssertionError("doctor must not run conda speech discovery")))
    proc = _run(VISION, "--doctor", "--json")
    assert proc.returncode == 0


def test_mcp_initialize_and_tools_list_smoke():
    messages = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    ]) + "\n"
    proc = _run(MCP, input_text=messages)
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert proc.returncode == 0
    assert responses[0]["result"]["serverInfo"]["version"] == "0.4.3"
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} >= {
        "describe_image", "analyze_video", "vision_status"
    }


def test_mcp_tool_call_without_ollama_returns_classified_error():
    message = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "describe_image",
                                      "arguments": {"images": ["missing.png"]}}})
    proc = _run(MCP, input_text=message + "\n")
    response = json.loads(proc.stdout)
    payload = json.loads(response["result"]["content"][0]["text"])
    assert response["result"]["isError"] is True
    assert payload["code"] in {"service_error", "backend_unavailable", "tool_error"}
    assert payload["message"]
