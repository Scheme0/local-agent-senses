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


class DeadlineExceededError(RuntimeError):
    """A request's execution deadline passed before the work completed.

    Raised by the vision engine when a stage check sees the deadline has
    passed, so callers never confuse "timed out in the queue" with "the
    request hit its execution budget".
    """

    code = "deadline_exceeded"


def _classify_error(exc: Exception) -> str:
    """Map an unexpected RuntimeError to a stable error code."""
    code = getattr(exc, "code", None)
    if code:
        return code
    text = str(exc).lower()
    if "blocked media url" in text or "ssrf" in text or "unsafe" in text:
        return "security_error"
    if ("not found" in text
            and any(k in text for k in ("ffmpeg", "pdftoppm", "yt-dlp", "ollama",
                                        "speech environment"))):
        return "dependency_missing"
    if "cannot connect" in text or "backend" in text or "endpoint" in text:
        return "backend_unavailable"
    if "deadline" in text or "time budget" in text:
        return "deadline_exceeded"
    if any(k in text for k in ("must be", "invalid", "expected",
                               "cannot be empty", "is required")):
        return "invalid_input"
    if any(k in text for k in ("ffmpeg", "frame extraction", "media", "video",
                               "image", "probe", "rasteri")):
        return "media_error"
    return "service_error"


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
        "lang": "auto", "asr_model": "sensevoice", "deadline": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def execute(tool: str, args: dict, deadline=None) -> str:
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
                    summary = "\n".join(
                        x for x in (out.getvalue().strip(), err.getvalue().strip()) if x)
                    raise RuntimeError("Vision health check failed"
                                       + (f":\n{summary}" if summary else ""))
                return out.getvalue().strip()

            if not vision.ensure_ollama_started():
                target = config.api_base() if config.use_openai_api() else "Ollama"
                raise RuntimeError(f"Cannot connect to {target}")

            if tool == "describe_image":
                images = args["images"]
                request = _args(media=list(images), prompt=args.get("prompt"),
                                context=args.get("context"), crop=args.get("crop"),
                                size=args.get("size", "full"), deadline=deadline)
                vision.ACTIVE_MODEL = vision.resolve_model("auto")
                vision.analyze_images(request)
            elif tool == "transcribe":
                request = _args(media=[args["media"]], prompt=args.get("prompt"),
                                context=args.get("context"), crop=args.get("crop"),
                                size=args.get("size", "full"),
                                max_frames=args.get("max_frames"), transcribe=True,
                                mode="text", band=args.get("band", "bottom"),
                                no_dedupe=args.get("no_dedupe", False),
                                deadline=deadline)
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
                                no_dedupe=args.get("no_dedupe", False),
                                deadline=deadline)
                vision.ACTIVE_MODEL = vision.resolve_model(request.mode)
                vision.analyze_video(args["video"], request)
            elif tool == "transcribe_audio":
                request = _args(lang=args.get("lang", "auto"),
                                asr_model=args.get("asr_model", "sensevoice"),
                                mode="audio", deadline=deadline)
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
    deadline = time.monotonic() + config.service_execution_timeout_sec()
    slot = _service_slot()
    if not slot.acquire(timeout=config.service_queue_timeout_sec()):
        raise ServiceError("busy", "Vision service is busy; retry later")
    try:
        raw = execute(tool, args, deadline=deadline)
    except ServiceError:
        raise
    except DeadlineExceededError as exc:
        raise ServiceError("deadline_exceeded", str(exc)) from exc
    except RuntimeError as exc:
        raise ServiceError(_classify_error(exc), str(exc)) from exc
    finally:
        slot.release()
    if time.monotonic() >= deadline:
        raise ServiceError("deadline_exceeded",
                           "Vision service request exceeded its execution time budget")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ServiceResult(text=raw, kind=tool)
    if not isinstance(data, dict):
        return ServiceResult(text=raw, kind=tool)
    text = str(data.pop("text", raw))
    mode = data.pop("mode", None)
    return ServiceResult(text=text, kind=tool, mode=mode, metadata=data)
