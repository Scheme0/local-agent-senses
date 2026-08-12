#!/usr/bin/env python3
"""Cross-platform VRAM watchdog for local-agent-senses.

Runs in the background and unloads Ollama models when:
  1. another process starts using a lot of VRAM (nvidia-smi),
  2. the vision skill has been idle for a while (last-use marker),
  3. (optional) a watched process is no longer running (VISION_WATCH_PROCESS,
     e.g. "codex" to unload when the host assistant exits).

Usage:
    python extras/vram_watchdog.py [--poll 15] [--idle 600]
                                   [--threshold-mb 4096] [--watch codex]

The PID file and last-use marker live in the system temp dir under
"codex-vision", matching vision.py's expectations.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
import config  # noqa: E402

POLL_SECONDS = 15
IDLE_SECONDS = 600
GPU_THRESHOLD_MB = 4096


def _http_json(url: str, payload: dict | None = None, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def loaded_models() -> list[str]:
    """Models currently resident in Ollama (falls back to the configured one)."""
    try:
        data = _http_json(config.ollama_base() + "/api/ps", timeout=10)
        models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        if models:
            return models
    except Exception:
        pass
    return [config.text_model()]


def unload_all() -> None:
    for name in loaded_models():
        try:
            _http_json(config.ollama_base() + "/api/generate",
                       {"model": name, "prompt": "", "keep_alive": 0}, timeout=15)
        except Exception:
            pass


def other_process_high_vram(threshold_mb: int) -> bool:
    """True when any non-ollama process uses more than threshold MB of VRAM."""
    nvidia = os.environ.get("VISION_NVIDIA_SMI") or _which("nvidia-smi")
    if not nvidia:
        return False
    try:
        out = subprocess.run(
            [nvidia, "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return False
    my_pid = str(os.getpid())
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        pid, mem = parts[0], parts[1]
        if pid == my_pid:
            continue
        try:
            if int(mem) <= threshold_mb:
                continue
        except ValueError:
            continue
        if _process_name(pid) in ("", "ollama", "ollama.exe"):
            continue
        return True
    return False


def _which(name: str) -> str:
    import shutil
    return shutil.which(name) or ""


def _process_name(pid: str) -> str:
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            if out.strip() and "," in out:
                return out.strip().split(",")[0].strip('"')
            return ""
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def watched_process_running(watch: str) -> bool:
    """Check whether any process matching `watch` (substring) is alive."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout.lower()
            return watch.lower() in out
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/comm", "r", encoding="utf-8") as f:
                    if watch.lower() in f.read().strip().lower():
                        return True
            except Exception:
                continue
        return False
    except Exception:
        return True  # cannot determine; do not unload


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="VRAM watchdog for local-agent-senses")
    parser.add_argument("--poll", type=int, default=POLL_SECONDS)
    parser.add_argument("--idle", type=int, default=IDLE_SECONDS)
    parser.add_argument("--threshold-mb", type=int, default=GPU_THRESHOLD_MB)
    parser.add_argument("--watch", default=os.environ.get("VISION_WATCH_PROCESS", ""))
    args = parser.parse_args()

    temp_dir = config.TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)
    marker = temp_dir / ".last-use"
    pid_file = temp_dir / ".watchdog.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")

    while True:
        try:
            if args.watch and not watched_process_running(args.watch):
                unload_all()
                return 0
            if other_process_high_vram(args.threshold_mb):
                unload_all()
                try:
                    marker.unlink()
                except OSError:
                    pass
            if marker.exists():
                age = time.time() - marker.stat().st_mtime
                if age > args.idle:
                    unload_all()
                    try:
                        marker.unlink()
                    except OSError:
                        pass
        except Exception:
            pass
        time.sleep(max(1, args.poll))


if __name__ == "__main__":
    sys.exit(main())
