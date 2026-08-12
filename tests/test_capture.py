# -*- coding: utf-8 -*-
"""Capture command-builder tests: never performs a real capture in CI."""
import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".tmp" / "capture"

from extras import capture  # noqa: E402


def _decode_ps(cmd: list[str]) -> str:
    idx = cmd.index("-EncodedCommand")
    return base64.b64decode(cmd[idx + 1]).decode("utf-16-le")


def test_windows_screen_command():
    cmd = capture._windows_command("screen", Path("C:/tmp/s.png"))
    assert cmd[0] == "powershell"
    assert "-NoProfile" in cmd
    assert "-STA" in cmd
    script = _decode_ps(cmd)
    assert "CopyFromScreen" in script
    assert "ImageFormat]::Png" in script


def test_windows_clipboard_command():
    cmd = capture._windows_command("clipboard", Path("C:/tmp/c.png"))
    script = _decode_ps(cmd)
    assert "Clipboard]::GetImage()" in script
    assert "exit 2" in script


def test_macos_commands():
    assert capture._macos_command("screen", Path("/tmp/s.png")) == [
        "screencapture", "-x", str(Path("/tmp/s.png")),
    ]
    assert capture._macos_command("clipboard", Path("/tmp/c.png")) == [
        "pngpaste", str(Path("/tmp/c.png")),
    ]


def test_linux_commands(monkeypatch):
    monkeypatch.setattr(
        capture.shutil,
        "which",
        lambda name: name if name in ("gnome-screenshot", "xclip") else None,
    )
    assert capture._linux_commands("screen", Path("/tmp/s.png")) == [
        ["gnome-screenshot", "-f", str(Path("/tmp/s.png"))],
    ]
    clip = capture._linux_commands("clipboard", Path("/tmp/c.png"))
    assert clip == [["xclip", "-selection", "clipboard", "-t", "image/png",
                     "-o"]]


def test_capture_rejects_unknown_mode():
    with pytest.raises(ValueError):
        capture.capture_image("webcam")


def test_capture_linux_missing_tool(monkeypatch):
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    monkeypatch.setattr(capture.sys, "platform", "linux")
    out = TMP / "missing-tool"
    out.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="no capture tool found"):
        capture.capture_image("screen", out)
