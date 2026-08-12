"""Screen / clipboard capture bridges (optional add-on).

Windows uses built-in PowerShell + .NET (no third-party dependency).
macOS uses `screencapture` for the screen and `pngpaste` for the clipboard.
Linux uses `gnome-screenshot` / ImageMagick `import` for the screen and
`xclip` / `wl-paste` for the clipboard.

Usage (through the CLI):
    python vision.py --capture screen --prompt "描述桌面"
    python vision.py --capture clipboard --prompt "这个截图里有什么"
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config


def _ps1_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _windows_script(mode: str, path: Path) -> str:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
    )
    if mode == "screen":
        ps += (
            "$src = New-Object System.Drawing.Bitmap "
            "$([System.Windows.Forms.SystemInformation]::VirtualScreen.Width), "
            "$([System.Windows.Forms.SystemInformation]::VirtualScreen.Height); "
            "$g = [System.Drawing.Graphics]::FromImage($src); "
            "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$g.CopyFromScreen($b.X, $b.Y, 0, 0, $src.Size); "
        )
    else:
        ps += (
            "$src = [System.Windows.Forms.Clipboard]::GetImage(); "
            "if ($null -eq $src) { Write-Error 'no image on clipboard'; exit 2 }; "
        )
    safe = str(path).replace("'", "''")
    ps += f"$src.Save('{safe}', [System.Drawing.Imaging.ImageFormat]::Png); "
    return ps


def _windows_command(mode: str, path: Path) -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-STA",
        "-EncodedCommand",
        _ps1_encoded(_windows_script(mode, path)),
    ]


def _macos_command(mode: str, path: Path) -> list[str]:
    if mode == "screen":
        return ["screencapture", "-x", str(path)]
    return ["pngpaste", str(path)]


def _linux_commands(mode: str, path: Path) -> list[list[str]]:
    if mode == "screen":
        if shutil.which("gnome-screenshot"):
            return [["gnome-screenshot", "-f", str(path)]]
        if shutil.which("import"):
            return [["import", "-window", "root", str(path)]]
        return []
    candidates = []
    if shutil.which("xclip"):
        candidates.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
    if shutil.which("wl-paste"):
        candidates.append(["wl-paste", "--type", "image/png"])
    return candidates


def _run(cmd: list[str], path: Path, stdout_image: bool = False) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except FileNotFoundError as e:
        raise RuntimeError(f"required tool not found: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("capture timed out") from e
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(err or f"capture failed (exit code {proc.returncode})")
    if stdout_image:
        path.write_bytes(proc.stdout)
    elif not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("capture produced no image")


def capture_image(mode: str, out_dir: Path | None = None) -> Path:
    """Capture the screen or clipboard into a PNG file and return its path."""
    if mode not in ("screen", "clipboard"):
        raise ValueError("mode must be 'screen' or 'clipboard'")
    out_dir = out_dir or config.TEMP_DIR / "capture"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{mode}-{os.getpid()}.png"

    if sys.platform == "win32":
        _run(_windows_command(mode, path), path)
    elif sys.platform == "darwin":
        _run(_macos_command(mode, path), path)
    else:
        candidates = _linux_commands(mode, path)
        if not candidates:
            raise RuntimeError(
                "no capture tool found: install gnome-screenshot or ImageMagick "
                "'import' for screen, or xclip / wl-paste for clipboard"
            )
        last_err: RuntimeError | None = None
        for cmd in candidates:
            try:
                _run(cmd, path, stdout_image=cmd[0] in ("xclip", "wl-paste"))
                return path
            except RuntimeError as e:
                last_err = e
        raise last_err  # type: ignore[misc]

    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("capture produced no image")
    return path
