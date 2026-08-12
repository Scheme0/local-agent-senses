#!/usr/bin/env python3
"""MCP stdio server for local-agent-senses.

Exposes vision.py's CLI as MCP tools, so any MCP-capable client (Codex, Claude
Desktop, Cursor, Cline, ...) can call them directly.

Protocol: stdio + newline-delimited JSON-RPC 2.0 (MCP 2024-11-05).

Run:
    python extras/mcp_server.py

Register (Codex):
    codex mcp add vision -- python <absolute-path>/extras/mcp_server.py

Register (Claude Code):
    claude mcp add vision -- python <absolute-path>/extras/mcp_server.py

Tools: describe_image / transcribe / analyze_video / transcribe_audio /
vision_status / vision_check.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
VISION_PY = ROOT / "vision.py"
SERVER_NAME = "local-agent-senses"
SERVER_VERSION = "0.2.2"
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 1800

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    import config as vision_config  # noqa: E402
    import service as vision_service  # noqa: E402
except Exception:
    vision_config = None
    vision_service = None

# Result cache: agents often re-ask about the same media, and local vision
# inference is the slow part. Same tool + same arguments (with local file
# mtime/size invalidation) are served from memory for CACHE_TTL seconds, and
# persisted to disk so a server restart still hits for long-video questions.
CACHE_TTL = 300
CACHE_MAX = 64
CACHEABLE = {"describe_image", "transcribe", "analyze_video", "transcribe_audio"}
_cache: dict[tuple, tuple[float, str]] = {}

TOOLS = [
    {
        "name": "describe_image",
        "description": "Describe or understand one or more images with a local "
                       "vision model (scene, objects, colors, UI, etc.). "
                       "Returns JSON: {\"text\": ..., \"mode\": \"image\", "
                       "\"media\": [...]}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Image paths or URLs, at least one",
                },
                "prompt": {"type": "string", "description": "Question or instruction"},
                "crop": {"type": "string", "description": "Optional WxH+X+Y crop"},
                "size": {"type": "string", "enum": ["full", "small"],
                         "description": "small=320px thumbnail, full=original size"},
            },
            "required": ["images"],
        },
    },
    {
        "name": "transcribe",
        "description": "Verbatim transcription of text in images/documents/"
                       "screenshots/video subtitles (no summary, judgment, or "
                       "interpretation). Returns JSON with text and, for "
                       "videos, frame timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media": {"type": "string",
                          "description": "Image or video path / URL"},
                "prompt": {"type": "string"},
                "context": {"type": "string"},
                "crop": {"type": "string"},
                "size": {"type": "string", "enum": ["full", "small"]},
                "max_frames": {"type": "integer",
                               "description": "Frame cap for video transcription "
                                              "(default 48)"},
            },
            "required": ["media"],
        },
    },
    {
        "name": "analyze_video",
        "description": "Analyze a video with a local vision model: scenes, "
                       "actions, time-window deep reading, contact sheets, etc. "
                       "Returns JSON: {\"text\": ..., \"mode\": ..., "
                       "\"frames\": [{\"t\": ..., \"w\": ..., \"h\": ...}], "
                       "\"duration\": ...}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video": {"type": "string", "description": "Video path / URL"},
                "prompt": {"type": "string", "description": "Question or instruction"},
                "mode": {"type": "string",
                         "enum": ["auto", "contact", "scenes", "skim", "window",
                                  "segments", "burst"],
                         "default": "auto"},
                "from": {"type": "string", "description": "window/burst start, e.g. 1:20"},
                "to": {"type": "string", "description": "window end"},
                "fps": {"type": "number", "description": "Sampling frame rate"},
                "max_frames": {"type": "integer", "description": "Frame cap"},
            },
            "required": ["video"],
        },
    },
    {
        "name": "transcribe_audio",
        "description": "Speech-to-text (FunASR SenseVoice, with timestamps; "
                       "embedded subtitle tracks are preferred when available). "
                       "Returns JSON: {\"text\": ..., \"source\": \"asr\"|"
                       "\"subtitle\"}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media": {"type": "string",
                          "description": "Audio/video file path / URL"},
                "lang": {"type": "string", "default": "auto",
                         "description": "auto/zh/en/yue/ja/ko"},
                "asr_model": {"type": "string", "enum": ["sensevoice", "paraformer"],
                              "default": "sensevoice"},
            },
            "required": ["media"],
        },
    },
    {
        "name": "vision_status",
        "description": "Show the vision backend status (models, backend type, "
                       "ffmpeg, speech environment, GPU, watchdog).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "vision_check",
        "description": "Run the environment health check (image reading, "
                       "transcription, video sampling; can be slow).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def run_service(tool: str, args: dict) -> str:
    return vision_service.execute(tool, args)


def _cache_key(name: str, args: dict) -> tuple:
    """Stable cache key. Local files are keyed by path + size + mtime so that
    editing a file invalidates the entry; URLs keep their plain string."""
    parts = []
    for key in sorted(args):
        value = args[key]
        if isinstance(value, list):
            value = tuple(_fingerprint(item) for item in value)
        else:
            value = _fingerprint(value)
        parts.append((key, value))
    return (name, tuple(parts))


def _fingerprint(value):
    if isinstance(value, str):
        path = Path(value)
        try:
            if path.is_file():
                stat = path.stat()
                return (value, stat.st_size, stat.st_mtime_ns)
        except OSError:
            pass
    return value


def _cache_dir() -> Path | None:
    """Disk-cache directory, or None when disabled (VISION_MCP_CACHE=0)."""
    if vision_config is not None:
        if not vision_config.mcp_cache_enabled():
            return None
        explicit = vision_config.mcp_cache_dir()
    else:
        explicit = os.environ.get("VISION_MCP_CACHE_DIR", "")
    if explicit:
        return Path(explicit)
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_CACHE_HOME")
            or str(Path.home() / ".cache"))
    return Path(base) / "local-agent-senses" / "mcp-cache"


def _disk_path(key: tuple) -> Path | None:
    d = _cache_dir()
    if d is None:
        return None
    digest = hashlib.sha256(
        json.dumps(key, sort_keys=True, ensure_ascii=False, default=str)
        .encode("utf-8")).hexdigest()
    return d / f"{digest}.json"


def _disk_read(key: tuple) -> str | None:
    try:
        p = _disk_path(key)
        if p is None or not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) <= CACHE_TTL:
            return str(data.get("text", ""))
        p.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def _disk_write(key: tuple, text: str) -> None:
    try:
        p = _disk_path(key)
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".cache-", suffix=".tmp",
                                         dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "text": text}, f,
                          ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, p)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        files = sorted(p.parent.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for old in files[:max(0, len(files) - CACHE_MAX * 4)]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def call_tool(name: str, args: dict) -> str:
    if name in CACHEABLE:
        key = _cache_key(name, args)
        now = time.monotonic()
        hit = _cache.get(key)
        if hit is None:
            disk = _disk_read(key)
            if disk is not None:
                _cache[key] = (now, disk)
                return disk
        else:
            cached_at, text = hit
            if now - cached_at <= CACHE_TTL:
                return text
            del _cache[key]
        text = _call_tool(name, args)
        _cache[key] = (now, text)
        _disk_write(key, text)
        if len(_cache) > CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            del _cache[oldest]
        return text
    return _call_tool(name, args)


def _call_tool(name: str, args: dict) -> str:
    if name == "describe_image":
        images = args.get("images") or []
        if not images:
            raise RuntimeError("images requires at least one image path or URL")
        if not isinstance(images, list) or len(images) > 16 or not all(isinstance(x, str) and x.strip() for x in images):
            raise RuntimeError("images must be a non-empty list of at most 16 strings")
        prompt = args.get("prompt", "Describe these images in detail, including all visible text and UI elements.")
        if not isinstance(prompt, str) or len(prompt) > 20000:
            raise RuntimeError("prompt must be a string no longer than 20000 characters")
        cmd = list(images) + ["--json", "--prompt",
                              prompt]
        if args.get("crop"):
            cmd += ["--crop", str(args["crop"])]
        if args.get("size") == "small":
            cmd += ["--size", "small"]
        return run_service("describe_image", {"images": images,
            "prompt": prompt, "crop": args.get("crop"), "size": args.get("size", "full")})

    if name == "transcribe":
        media = args.get("media")
        if not media:
            raise RuntimeError("media cannot be empty")
        cmd = [str(media), "--mode", "text", "--transcribe", "--json"]
        if args.get("max_frames"):
            max_frames = int(args["max_frames"])
            if not 1 <= max_frames <= 1000:
                raise RuntimeError("max_frames must be between 1 and 1000")
            cmd += ["--max-frames", str(max_frames)]
        return run_service("transcribe", {"media": str(media),
            "max_frames": int(args["max_frames"]) if args.get("max_frames") else None})

    if name == "analyze_video":
        video = args.get("video")
        if not video:
            raise RuntimeError("video cannot be empty")
        cmd = [str(video), "--json", "--prompt",
               str(args.get("prompt", "Describe in chronological order what happens "
                                      "in this video, including scene changes and "
                                      "all visible text."))]
        mode = args.get("mode", "auto")
        if mode != "auto":
            cmd += ["--mode", str(mode)]
        for flag, key in (("--from", "from"), ("--to", "to"), ("--fps", "fps"),
                          ("--max-frames", "max_frames")):
            if args.get(key) is not None:
                if key == "fps" and not 0 < float(args[key]) <= 60:
                    raise RuntimeError("fps must be greater than 0 and at most 60")
                if key == "max_frames" and not 1 <= int(args[key]) <= 1000:
                    raise RuntimeError("max_frames must be between 1 and 1000")
                cmd += [flag, str(args[key])]
        return run_service("analyze_video", {"video": str(video),
            "prompt": args.get("prompt"), "mode": mode, "from": args.get("from"),
            "to": args.get("to"), "fps": args.get("fps"),
            "max_frames": args.get("max_frames")})

    if name == "transcribe_audio":
        media = args.get("media")
        if not media:
            raise RuntimeError("media cannot be empty")
        cmd = [str(media), "--mode", "audio", "--json",
               "--lang", str(args.get("lang", "auto")),
               "--asr-model", str(args.get("asr_model", "sensevoice"))]
        return run_service("transcribe_audio", {"media": str(media),
            "lang": args.get("lang", "auto"), "asr_model": args.get("asr_model", "sensevoice")})

    if name == "vision_status":
        return run_service("vision_status", {})

    if name == "vision_check":
        return run_service("vision_check", {})

    raise RuntimeError(f"Unknown tool: {name}")


def handle_request(msg: dict):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            text = call_tool(name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True,
                },
            }
    if method is None or str(method).startswith("notifications/"):
        return None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
