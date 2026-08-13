"""In-process service facade shared by MCP and Python callers.

The CLI remains the compatibility entry point. This module provides a small,
structured boundary so MCP does not need to spawn a second Python process for
each request.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import time
from dataclasses import asdict, dataclass
from types import SimpleNamespace


@dataclass
class ServiceResult:
    """Stable result envelope for Python and MCP integrations."""

    text: str
    kind: str
    mode: str | None = None
    metadata: dict | None = None
    warnings: list[str] | None = None

    def to_dict(self) -> dict:
        value = asdict(self)
        value["metadata"] = value["metadata"] or {}
        value["warnings"] = value["warnings"] or []
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class ServiceError(RuntimeError):
    """Expected service failure with a machine-readable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


_slots_lock = threading.Lock()
_io_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_slots_size = 0


def _service_slot() -> threading.BoundedSemaphore:
    global _slots, _slots_size
    import config
    size = config.service_max_concurrency()
    with _slots_lock:
        if _slots is None or _slots_size != size:
            _slots = threading.BoundedSemaphore(size)
            _slots_size = size
        return _slots


def _args(**values):
    defaults = {
        "media": [], "prompt": None, "context": None, "crop": None,
        "size": "full", "mode": "auto", "transcribe": False, "json": True,
        "start": None, "end": None, "duration": None, "fps": None,
        "max_frames": None, "band": "bottom", "no_dedupe": False,
        "lang": "auto", "asr_model": "sensevoice",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def execute(tool: str, args: dict) -> str:
    """Execute one public service operation and return its JSON/text output.

    vision.py writes results to process-global stdout, so redirection is
    serialized with _io_lock; the engine itself is single-threaded by design.
    """
    import config
    import vision

    out = io.StringIO()
    err = io.StringIO()
    previous_model = vision.ACTIVE_MODEL
    previous_ctx = vision.CTX_OVERRIDE
    with _io_lock, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if tool == "vision_status":
                return vision.status_text()
            if tool == "vision_check":
                if not vision.ensure_ollama_started():
                    raise RuntimeError("Vision backend is not available")
                code = vision.run_health_check()
                if code:
                    raise RuntimeError("Vision health check failed")
                return out.getvalue().strip()

            if not vision.ensure_ollama_started():
                target = config.api_base() if config.use_openai_api() else "Ollama"
                raise RuntimeError(f"Cannot connect to {target}")

            if tool == "describe_image":
                images = args["images"]
                request = _args(media=list(images), prompt=args.get("prompt"),
                                context=args.get("context"), crop=args.get("crop"),
                                size=args.get("size", "full"))
                vision.ACTIVE_MODEL = vision.resolve_model("auto")
                vision.analyze_images(request)
            elif tool == "transcribe":
                request = _args(media=[args["media"]], prompt=args.get("prompt"),
                                context=args.get("context"), crop=args.get("crop"),
                                size=args.get("size", "full"),
                                max_frames=args.get("max_frames"), transcribe=True,
                                mode="text", band=args.get("band", "bottom"),
                                no_dedupe=args.get("no_dedupe", False))
                vision.ACTIVE_MODEL = vision.resolve_model("text")
                vision.analyze_video(args["media"], request)
            elif tool == "analyze_video":
                start = args.get("from")
                end = args.get("to")
                request = _args(prompt=args.get("prompt"), context=args.get("context"),
                                mode=args.get("mode", "auto"),
                                start=vision.parse_time(start) if start is not None else None,
                                end=vision.parse_time(end) if end is not None else None,
                                duration=args.get("duration"), fps=args.get("fps"),
                                max_frames=args.get("max_frames"),
                                no_dedupe=args.get("no_dedupe", False))
                vision.ACTIVE_MODEL = vision.resolve_model(request.mode)
                vision.analyze_video(args["video"], request)
            elif tool == "transcribe_audio":
                request = _args(lang=args.get("lang", "auto"),
                                asr_model=args.get("asr_model", "sensevoice"), mode="audio")
                vision.analyze_audio(args["media"], request)
            else:
                raise RuntimeError(f"Unknown tool: {tool}")
        finally:
            vision.ACTIVE_MODEL = previous_model
            vision.CTX_OVERRIDE = previous_ctx

    result = out.getvalue().strip()
    if not result:
        raise RuntimeError(err.getvalue().strip() or "Vision service returned no output")
    return result


def execute_result(tool: str, args: dict) -> ServiceResult:
    """Execute a tool and normalize its JSON output into ServiceResult."""
    import config
    started = time.monotonic()
    slot = _service_slot()
    if not slot.acquire(timeout=config.service_timeout_sec()):
        raise ServiceError("busy", "Vision service is busy; retry later")
    try:
        raw = execute(tool, args)
    except ServiceError:
        raise
    except RuntimeError as exc:
        code = "timeout" if "timed out" in str(exc).lower() else "service_error"
        raise ServiceError(code, str(exc)) from exc
    finally:
        slot.release()
    if time.monotonic() - started > config.service_timeout_sec():
        raise ServiceError("timeout", "Vision service request exceeded its time budget")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ServiceResult(text=raw, kind=tool)
    if not isinstance(data, dict):
        return ServiceResult(text=raw, kind=tool)
    text = str(data.pop("text", raw))
    mode = data.pop("mode", None)
    return ServiceResult(text=text, kind=tool, mode=mode, metadata=data)
