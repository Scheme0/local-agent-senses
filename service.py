"""In-process service facade shared by MCP and Python callers.

The CLI remains the compatibility entry point. This module provides a small,
structured boundary so MCP does not need to spawn a second Python process for
each request.
"""

from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace


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
    """Execute one public service operation and return its JSON/text output."""
    import vision
    import config

    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
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
                            crop=args.get("crop"), size=args.get("size", "full"))
            vision.ACTIVE_MODEL = vision.resolve_model("auto")
            vision.analyze_images(request)
        elif tool == "transcribe":
            request = _args(media=[args["media"]], prompt=None,
                            max_frames=args.get("max_frames"), transcribe=True,
                            mode="text")
            vision.ACTIVE_MODEL = vision.resolve_model("text")
            vision.analyze_video(args["media"], request)
        elif tool == "analyze_video":
            request = _args(prompt=args.get("prompt"), mode=args.get("mode", "auto"),
                            start=args.get("from"), end=args.get("to"),
                            fps=args.get("fps"), max_frames=args.get("max_frames"))
            vision.ACTIVE_MODEL = vision.resolve_model(request.mode)
            vision.analyze_video(args["video"], request)
        elif tool == "transcribe_audio":
            request = _args(lang=args.get("lang", "auto"),
                            asr_model=args.get("asr_model", "sensevoice"), mode="audio")
            vision.analyze_audio(args["media"], request)
        else:
            raise RuntimeError(f"Unknown tool: {tool}")

    result = out.getvalue().strip()
    if not result:
        raise RuntimeError(err.getvalue().strip() or "Vision service returned no output")
    return result
