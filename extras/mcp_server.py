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
import logging
import math
import os
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "local-agent-senses"
PROTOCOL_VERSION = "2024-11-05"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    import config as vision_config  # noqa: E402
    import service as vision_service  # noqa: E402
except Exception:
    vision_config = None
    vision_service = None

# Single version source: the shared package version, with a source-tree
# fallback when the package is not installed.
SERVER_VERSION = (vision_config.package_version()
                  if vision_config is not None else "0+source")

logger = logging.getLogger("vision-mcp")

# Result cache: agents often re-ask about the same media, and local vision
# inference is the slow part. Same tool + same arguments (with local file
# mtime/size invalidation) are served from memory for CACHE_TTL seconds, and
# persisted to disk so a server restart still hits for long-video questions.
CACHE_TTL = 300
CACHE_MAX = 64
CACHE_MAX_RESULT_BYTES = 2_000_000     # per-entry result size cap (memory+disk)
CACHE_MAX_FILES = 512                  # max .json files kept in the cache dir
CACHE_MAX_FILE_BYTES = 10_000_000      # max single cache file on disk
CACHEABLE = {"describe_image", "transcribe", "analyze_video", "transcribe_audio"}
_cache: dict[tuple, tuple[float, str]] = {}

# Captured at import time (pathlib.Path switches implementation based on
# os.name, so the helpers read this flag instead of flipping os.name).
_IS_POSIX = os.name != "nt"


def _chmod_cache_dir(directory: Path) -> None:
    """Restrict the cache directory to the current user (POSIX only)."""
    if _IS_POSIX:
        os.chmod(directory, 0o700)


def _chmod_cache_file(path: Path) -> None:
    """Restrict a cache file to the current user (POSIX only)."""
    if _IS_POSIX:
        os.chmod(path, 0o600)

# Server-side validation bounds. The MCP inputSchema is a hint only: a client
# may send raw JSON-RPC, so every tool re-validates its arguments here.
MAX_IMAGES = 16
MAX_MEDIA_LENGTH = 4096
MAX_PROMPT_LENGTH = 20000
MAX_CONTEXT_LENGTH = 20000
MAX_CROP_LENGTH = 128
MAX_TIME_LENGTH = 64
ANALYZE_MODES = ("auto", "contact", "scenes", "skim", "window", "segments", "burst")
ALLOWED_SIZES = ("full", "small")
ALLOWED_BANDS = ("bottom", "top", "middle", "full")
ALLOWED_LANGS = ("auto", "zh", "en", "yue", "ja", "ko")
ALLOWED_ASR_MODELS = ("sensevoice", "paraformer")


class MCPInputError(ValueError):
    """Invalid client arguments; surfaced as {code: invalid_input}."""

    code = "invalid_input"


def _require_text(args: dict, key: str, default: str | None = None,
                  max_length: int = MAX_PROMPT_LENGTH) -> str | None:
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if not isinstance(value, str):
        raise MCPInputError(f"'{key}' must be a string")
    if len(value) > max_length:
        raise MCPInputError(f"'{key}' must be at most {max_length} characters")
    return value


def _require_media(args: dict, key: str, max_length: int = MAX_MEDIA_LENGTH) -> str:
    value = _require_text(args, key, max_length=max_length)
    if value is None or not value.strip():
        raise MCPInputError(f"'{key}' is required and cannot be empty")
    return value


def _optional_int(args: dict, key: str, minimum: int = 1, maximum: int = 1000) -> int | None:
    if key not in args or args[key] is None:
        return None
    value = args[key]
    if isinstance(value, bool):
        raise MCPInputError(f"'{key}' must be an integer, not a boolean")
    if isinstance(value, float):
        if not value.is_integer():
            raise MCPInputError(f"'{key}' must be an integer")
        value = int(value)
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise MCPInputError(f"'{key}' must be an integer") from None
    if not isinstance(value, int):
        raise MCPInputError(f"'{key}' must be an integer")
    if not minimum <= value <= maximum:
        raise MCPInputError(f"'{key}' must be between {minimum} and {maximum}")
    return value


def _optional_number(args: dict, key: str, minimum: float | None = None,
                     maximum: float | None = None) -> float | None:
    if key not in args or args[key] is None:
        return None
    value = args[key]
    if isinstance(value, bool):
        raise MCPInputError(f"'{key}' must be a number, not a boolean")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise MCPInputError(f"'{key}' must be a finite number") from None
    if not isinstance(value, (int, float)):
        raise MCPInputError(f"'{key}' must be a number")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise MCPInputError(f"'{key}' must be finite (NaN and Infinity are not allowed)")
    if minimum is not None and value < minimum:
        raise MCPInputError(f"'{key}' must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise MCPInputError(f"'{key}' must be at most {maximum}")
    return value


def _optional_bool(args: dict, key: str, default: bool = False) -> bool:
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if not isinstance(value, bool):
        raise MCPInputError(f"'{key}' must be a boolean")
    return value


def _choice(value, allowed, label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MCPInputError(f"{label} must be one of: {', '.join(allowed)}")
    return value


def _optional_time(args: dict, key: str) -> str | None:
    value = _require_text(args, key, max_length=MAX_TIME_LENGTH)
    if value is None:
        return None
    if value.strip().startswith("-"):
        raise MCPInputError(f"'{key}' cannot be a negative time")
    return value

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
                "context": {"type": "string", "description": "Previous summary (summary chain)"},
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
                "band": {"type": "string",
                         "enum": ["bottom", "top", "middle", "full"],
                         "description": "Text band to read (default bottom)"},
                "no_dedupe": {"type": "boolean",
                              "description": "Disable static-frame dedupe"},
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
                "context": {"type": "string", "description": "Previous summary (summary chain)"},
                "mode": {"type": "string",
                         "enum": ["auto", "contact", "scenes", "skim", "window",
                                  "segments", "burst"],
                         "default": "auto"},
                "from": {"type": "string", "description": "window/burst start, e.g. 1:20"},
                "to": {"type": "string", "description": "window end"},
                "duration": {"type": "number", "description": "burst duration in seconds"},
                "fps": {"type": "number", "description": "Sampling frame rate"},
                "max_frames": {"type": "integer", "description": "Frame cap"},
                "no_dedupe": {"type": "boolean",
                              "description": "Disable static-frame dedupe"},
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
    return vision_service.execute_result(tool, args).to_json()


def _cache_model() -> str:
    """Model context for cache keys; changes invalidate cached results."""
    if vision_config is None:
        return "unknown"
    try:
        return vision_config.text_model() + "/" + vision_config.quick_model()
    except Exception:
        return "unknown"


def _cache_backend() -> str:
    """Backend type (openai vs ollama); the API key is never part of the key."""
    if vision_config is None:
        return "unknown"
    try:
        return "openai" if vision_config.use_openai_api() else "ollama"
    except Exception:
        return "unknown"


def _cache_key(name: str, args: dict) -> tuple:
    """Stable cache key: server version + tool + model + backend + arguments.
    Local files are keyed by path + size + mtime so that editing a file
    invalidates the entry; URLs keep their plain string. No API key, full URL
    or prompt content is stored in the key beyond the hashed disk filename."""
    parts = [
        ("server_version", SERVER_VERSION),
        ("model", _cache_model()),
        ("backend", _cache_backend()),
    ]
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
        if p.stat().st_size > CACHE_MAX_FILE_BYTES:
            p.unlink(missing_ok=True)
            logger.warning("MCP cache file exceeded the size limit and was removed")
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) <= CACHE_TTL:
            return str(data.get("text", ""))
        p.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("MCP cache read failed: %s", type(exc).__name__)
    return None


def _disk_write(key: tuple, text: str) -> None:
    try:
        p = _disk_path(key)
        if p is None:
            return
        if len(text.encode("utf-8")) > CACHE_MAX_RESULT_BYTES:
            logger.warning("MCP cache entry exceeded the result size limit and was skipped")
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        _chmod_cache_dir(p.parent)
        fd, temp_name = tempfile.mkstemp(prefix=".cache-", suffix=".tmp",
                                         dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "text": text}, f,
                          ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, p)
            _chmod_cache_file(p)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            files = sorted(p.parent.glob("*.json"), key=lambda f: f.stat().st_mtime)
            for old in files[:max(0, len(files) - CACHE_MAX_FILES)]:
                old.unlink(missing_ok=True)
            for f in files:
                if f.stat().st_size > CACHE_MAX_FILE_BYTES:
                    f.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("MCP cache cleanup failed: %s", type(exc).__name__)
    except Exception as exc:
        logger.warning("MCP cache write failed: %s", type(exc).__name__)


def call_tool(name: str, args) -> str:
    if not isinstance(args, dict):
        raise MCPInputError("tool arguments must be a JSON object")
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
        images = args.get("images")
        if not isinstance(images, list) or not images:
            raise MCPInputError("images must be a non-empty list")
        if len(images) > MAX_IMAGES:
            raise MCPInputError(f"images must contain at most {MAX_IMAGES} items")
        for item in images:
            if not isinstance(item, str) or not item.strip():
                raise MCPInputError("every image must be a non-empty string")
            if len(item) > MAX_MEDIA_LENGTH:
                raise MCPInputError(f"each image path/URL must be at most "
                                    f"{MAX_MEDIA_LENGTH} characters")
        prompt = _require_text(
            args, "prompt",
            default="Describe these images in detail, including all visible "
                    "text and UI elements.")
        context = _require_text(args, "context", max_length=MAX_CONTEXT_LENGTH)
        crop = _require_text(args, "crop", max_length=MAX_CROP_LENGTH)
        size = _choice(args.get("size", "full"), ALLOWED_SIZES, "size")
        return run_service("describe_image", {
            "images": images, "prompt": prompt, "context": context,
            "crop": crop, "size": size,
        })

    if name == "transcribe":
        media = _require_media(args, "media")
        prompt = _require_text(args, "prompt", max_length=MAX_PROMPT_LENGTH)
        context = _require_text(args, "context", max_length=MAX_CONTEXT_LENGTH)
        crop = _require_text(args, "crop", max_length=MAX_CROP_LENGTH)
        size = _choice(args.get("size", "full"), ALLOWED_SIZES, "size")
        band = _choice(args.get("band", "bottom"), ALLOWED_BANDS, "band")
        max_frames = _optional_int(args, "max_frames", 1, 1000)
        no_dedupe = _optional_bool(args, "no_dedupe", False)
        return run_service("transcribe", {
            "media": media, "prompt": prompt, "context": context,
            "crop": crop, "size": size, "max_frames": max_frames,
            "band": band, "no_dedupe": no_dedupe,
        })

    if name == "analyze_video":
        video = _require_media(args, "video")
        mode = _choice(args.get("mode", "auto"), ANALYZE_MODES, "mode")
        prompt = _require_text(args, "prompt", max_length=MAX_PROMPT_LENGTH)
        context = _require_text(args, "context", max_length=MAX_CONTEXT_LENGTH)
        start = _optional_time(args, "from")
        end = _optional_time(args, "to")
        duration = _optional_number(args, "duration", minimum=0.0, maximum=3600.0)
        if duration is not None and duration <= 0:
            raise MCPInputError("duration must be greater than 0 and at most 3600")
        fps = _optional_number(args, "fps", minimum=0.0, maximum=60.0)
        if fps is not None and fps <= 0:
            raise MCPInputError("fps must be greater than 0 and at most 60")
        max_frames = _optional_int(args, "max_frames", 1, 1000)
        no_dedupe = _optional_bool(args, "no_dedupe", False)
        return run_service("analyze_video", {
            "video": video, "prompt": prompt, "context": context,
            "mode": mode, "from": start, "to": end,
            "duration": duration, "fps": fps, "max_frames": max_frames,
            "no_dedupe": no_dedupe,
        })

    if name == "transcribe_audio":
        media = _require_media(args, "media")
        lang = _choice(args.get("lang", "auto"), ALLOWED_LANGS, "lang")
        asr_model = _choice(args.get("asr_model", "sensevoice"),
                            ALLOWED_ASR_MODELS, "asr_model")
        return run_service("transcribe_audio", {
            "media": media, "lang": lang, "asr_model": asr_model,
        })

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
            error = {"code": getattr(e, "code", "tool_error"),
                     "message": str(e)}
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(error, ensure_ascii=False)}],
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
