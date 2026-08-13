#!/usr/bin/env python3
"""
Local multimodal vision & speech helper for text-only LLM agents
(images / videos / audio) via Ollama and FunASR.

Usage:
    python vision.py <media...> --prompt "question"
    python vision.py <video> --mode contact|scenes|skim|window|segments|burst|text
    python vision.py <video|audio> --mode audio
    python vision.py --status | --unload | --watchdog-start | --check

Design principles:
  - Pure standard library for the vision path (no third-party deps).
  - Intermediate media stays in memory only (no media temp files on disk);
    remote URLs are buffered in RAM and fed to ffmpeg through pipes.
  - Speech transcription runs in a FunASR-enabled Python environment
    (extras/speech.py, optional; auto-detected via conda).
  - Budget enforcement (pixel cap) prevents oversized requests from failing.
"""

import argparse
import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True  # keep the skill directory free of __pycache__

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import config  # noqa: E402
import media  # noqa: E402
import ollama_client  # noqa: E402
import video_plans  # noqa: E402

__version__ = "0.4.6"

ACTIVE_MODEL = ""
TRANSCRIBE_PROMPT = (
    "You are a document transcription engine. Transcribe ALL visible text in "
    "the image, strictly following these rules:\n"
    "1. Output only text that actually appears, verbatim, preserving order, "
    "paragraphs, and punctuation.\n"
    "2. Do not summarize, shorten, rewrite, complete, or explain.\n"
    "3. Do not add comments, conclusions, or meta notes like 'this page shows...'.\n"
    "4. Transcribe tables cell by cell; for charts include the title, axis labels, "
    "legend, and any visible text inside the figure.\n"
    "5. Mark unreadable regions as [?]; never guess.\n"
    "6. Transcription must be complete; prefer longer output over omissions."
)
MOJIBAKE_THRESHOLD = 5
ENCODING_HINT = (
    "Hint: if these '?'/replacement characters really exist in the image or "
    "video itself, ignore this message. Otherwise the text was likely mangled "
    "by shell/pipe encoding. In PowerShell, run:\n"
    "  $OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
    "or save scripts/commands as UTF-8 files before running them."
)
TEMP_DIR = config.TEMP_DIR
LAST_USE_FILE = TEMP_DIR / ".last-use"
PID_FILE = TEMP_DIR / ".watchdog.pid"
WATCHDOG_PS1 = SCRIPT_DIR / "vram-watchdog.ps1"
EXTRAS_DIR = SCRIPT_DIR / "extras"
if not WATCHDOG_PS1.exists():
    WATCHDOG_PS1 = EXTRAS_DIR / "vram-watchdog.ps1"
WATCHDOG_PY = EXTRAS_DIR / "vram_watchdog.py"
SPEECH_PY = EXTRAS_DIR / "speech.py"
if not SPEECH_PY.exists():
    SPEECH_PY = SCRIPT_DIR / "speech.py"

env = config.env
ollama_base = config.ollama_base
keep_alive = config.keep_alive
text_max_tokens = config.text_max_tokens
quick_output_limit = config.quick_output_limit
quick_think = config.quick_think
budget_pixels = config.budget_pixels
max_download_mb = config.max_download_mb
ollama_exe = config.ollama_exe


def model_name() -> str:
    if ACTIVE_MODEL:
        return ACTIVE_MODEL
    override = env("VISION_MODEL")
    if override:
        return override
    return config.quick_model()


def resolve_model(mode: str) -> str:
    override = env("VISION_MODEL")
    if override:
        return override
    if mode == "text":
        return config.text_model()
    return config.quick_model()


def is_text_model() -> bool:
    return model_name() == config.text_model()


def _normalize_model(name: str) -> str:
    """Strip the implicit :latest tag so configured vs. installed model names
    compare cleanly."""
    n = name.strip()
    if n.lower().endswith(":latest"):
        n = n[: -len(":latest")]
    return n


def _model_available(model: str, have: list[str]) -> bool:
    target = _normalize_model(model)
    return any(_normalize_model(h) == target for h in have)


def output_tokens(mode: str = "auto") -> int:
    """Output budget by mode (large for text, quick otherwise), capped per model."""
    budget = text_max_tokens() if mode == "text" else quick_output_limit()
    if is_text_model():
        # GLM-4.6V-Flash: up to 32K output inside a 64K window, with headroom.
        return min(budget, 32768)
    return min(budget, 98304)


CTX_OVERRIDE = 0


def context_window(mode: str = "auto", n_images: int = 1) -> int:
    """Context window = input estimate + output budget, capped by model limits;
    --ctx forces a value (still capped to a sane upper bound)."""
    cap = 65536 if is_text_model() else 196608
    limit = ollama_client.model_context_limit(model_name())
    if limit:
        cap = min(cap, limit)
    if CTX_OVERRIDE:
        return min(CTX_OVERRIDE, cap)
    # Roughly 2K tokens per image plus prompt headroom; many frames (video)
    # grow the window automatically so nothing gets truncated.
    input_est = max(n_images, 1) * 2048 + 2048
    total = max(input_est + output_tokens(mode), 8192)
    return min(total, cap)


def inference_timeout(mode: str = "auto") -> int:
    # Estimate generation time from the output budget (~20 token/s floor),
    # at least 10 minutes.
    return max(600, output_tokens(mode) // 20 + 300)


def friendly_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if any(k in low for k in ("out of memory", "cuda out of memory", "failed to allocate",
                              "insufficient", "memory error", "vram")):
        return (f"{msg}\nHint: VRAM/RAM is insufficient. Lower "
                f"VISION_MAX_TOKENS / VISION_QUICK_MAX_TOKENS, run --unload to "
                f"release models, or close other memory-hungry programs.")
    if "timed out" in low or "timeout" in low:
        return (f"{msg}\nHint: generation timed out. Large output budgets can "
                f"exceed the default timeout; lower VISION_MAX_TOKENS or split "
                f"the task into smaller segments.")
    return msg


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def ensure_ollama_started() -> bool:
    if ollama_client.ready():
        return True
    if config.use_openai_api():
        # Remote endpoints have no local process to start; let the caller
        # surface the configuration problem.
        return False
    exe = ollama_exe()
    if not exe:
        return False
    try:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        subprocess.Popen([exe, "serve"], **kwargs)
    except Exception:
        return False
    deadline = time.time() + 25
    while time.time() < deadline:
        if ollama_client.ready():
            return True
        time.sleep(2)
    return False


def touch_last_use() -> None:
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        LAST_USE_FILE.touch()
    except Exception:
        pass


def start_watchdog(required: bool = False) -> None:
    """Start the background VRAM watchdog (cross-platform Python version).

    Optional by default: silently skip when no watchdog script is present.
    Pass required=True (used by --watchdog-start) to surface the problem.
    """
    if not WATCHDOG_PY.exists():
        if sys.platform == "win32" and WATCHDOG_PS1.exists():
            _start_watchdog_ps1()
            return
        if required:
            print(f"Watchdog script not found: {WATCHDOG_PY}", file=sys.stderr)
        return
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                os.kill(old_pid, 0)
                return
            except (OSError, ValueError):
                pass
        cmd = [sys.executable, str(WATCHDOG_PY)]
        watch = env("VISION_WATCH_PROCESS")
        if watch:
            cmd += ["--watch", watch]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        print(f"Watchdog failed to start (analysis continues): {e}", file=sys.stderr)


def _start_watchdog_ps1() -> None:
    """Fallback: launch the legacy Windows PowerShell watchdog."""
    try:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-WindowStyle", "Hidden", "-File", str(WATCHDOG_PS1)]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        print(f"Watchdog failed to start (analysis continues): {e}", file=sys.stderr)


def status_text() -> str:
    lines = [
        f"Text model: {config.text_model()}",
        f"Quick model: {config.quick_model()}",
        f"Active model: {model_name()}",
        f"Backend: {('OpenAI-compatible ' + config.api_base()) if config.use_openai_api() else ('Ollama ' + ollama_base())}",
    ]
    try:
        loaded = ollama_client.ps()
        lines.append("Loaded: " + (", ".join(loaded) if loaded
                                   else "(remote endpoint, no local residency)" if config.use_openai_api() else "(none)"))
    except Exception as e:
        lines.append(f"Ollama status: {e}")
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            out = subprocess.run([nvidia, "--query-gpu=name,memory.used,memory.total",
                                  "--format=csv,noheader"], capture_output=True, text=True,
                                 timeout=15)
            if out.stdout.strip():
                lines.append("GPU: " + out.stdout.strip())
        except Exception:
            pass
    try:
        have = ollama_client.tags()
        label = "Endpoint models" if config.use_openai_api() else "Local models"
        lines.append(label + ": " + (", ".join(have) if have else "(none)"))
        missing = [m for m in (config.text_model(), config.quick_model())
                   if have and not _model_available(m, have)]
        if missing:
            lines.append("WARNING: configured model(s) not found: "
                         + ", ".join(missing))
            lines.append("  Set VISION_TEXT_MODEL / VISION_QUICK_MODEL or edit "
                         "vision-config.json. Any vision-capable Ollama model "
                         "works (e.g. qwen3.5:4b, llava, minicpm-v).")
    except Exception as e:
        lines.append(f"Failed to read model list: {e}")
    try:
        lines.append("ffmpeg: available (" + video_plans.ffmpeg_bin() + ")")
    except Exception as e:
        lines.append(f"ffmpeg: MISSING! {e}")
    py = config.speech_python()
    if py and Path(py).exists():
        lines.append("Speech env: available (" + py + ")")
    else:
        lines.append("Speech env: missing"
                     + (f": {py}" if py
                        else " (not found; set VISION_SPEECH_PYTHON)"))
    try:
        free = shutil.disk_usage(config.models_dir()).free
        lines.append(f"Model disk free: {free // (1 << 30)} GB")
    except Exception:
        pass
    try:
        wd_pid = int(PID_FILE.read_text().strip())
        lines.append("Watchdog: " + ("running" if pid_alive(wd_pid) else "not running"))
    except Exception:
        lines.append("Watchdog: not running")
    return "\n".join(lines)


def unload_model() -> None:
    if config.use_openai_api():
        print("Remote endpoint has no local models to unload; skipping.")
        return
    targets: list[str] = []
    try:
        targets = ollama_client.ps()
    except Exception:
        pass
    for extra in (model_name(), config.text_model(),
                  config.quick_model(), env("VISION_MODEL")):
        if extra and extra not in targets:
            targets.append(extra)
    failed = False
    for m in targets:
        try:
            ollama_client.unload_models([m])
        except Exception as e:
            failed = True
            print(f"Failed to unload {m}: {e}", file=sys.stderr)
    if failed:
        sys.exit(1)
    print(f"Unloaded {len(targets)} model(s): {', '.join(targets) or 'none'}. "
          "VRAM released.")


def parse_time(s: str) -> float:
    s = s.strip()
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise ValueError(f"Cannot parse time: {s} (supported: 90 / 1:30 / 00:01:30)")


def fit_budget(images: list[dict], budget: int) -> list[dict]:
    while len(images) > 1:
        total = sum(im["w"] * im["h"] for im in images)
        if total <= budget:
            break
        images = images[::2]
    return images


def ask_model(prompt: str, images_b64: list[str], mode: str = "auto") -> str:
    ensure_single_resident(model_name())
    think = None
    if model_name() == config.quick_model() and not quick_think():
        think = False
    try:
        return ollama_client.chat(
            model_name(), prompt, images_b64,
            num_predict=output_tokens(mode),
            num_ctx=context_window(mode, len(images_b64)),
            think=think,
            keep_alive=keep_alive(),
            timeout=inference_timeout(mode),
        )
    except Exception as e:
        raise RuntimeError(friendly_error(e)) from e


def ensure_single_resident(target: str) -> None:
    """Keep only one model resident: unload others before switching."""
    if not config.single_resident():
        return
    try:
        loaded = ollama_client.ps()
    except Exception:
        return
    for m in loaded:
        if m != target:
            try:
                ollama_client.unload_models([m])
            except Exception:
                pass


def build_image_prompt(n: int, context: str | None, user_prompt: str) -> str:
    parts = []
    if context:
        parts.append(f"[Previous summary]\n{context}")
    parts.append(f"Below are {n} images, numbered in order.")
    parts.append(f"Task: {user_prompt}")
    parts.append("Describe what you see in each image and label the image number; "
                 "only describe what is actually visible, and note anything "
                 "you are unsure about.")
    return "\n".join(parts)


def build_transcribe_prompt(user_prompt: str | None) -> str:
    """Fixed transcription prompt: verbatim output only; the user's --prompt is
    appended as an additional requirement and never overrides the rules."""
    if user_prompt:
        return TRANSCRIBE_PROMPT + f"\nAdditional requirement: {user_prompt}"
    return TRANSCRIBE_PROMPT


def build_image_transcribe_prompt(n: int, context: str | None,
                                  user_prompt: str | None) -> str:
    parts = []
    if context:
        parts.append(f"[Previous summary]\n{context}")
    if n > 1:
        parts.append(f"Below are {n} images, numbered in order. Transcribe each one: "
                     f'start with "Image N:", then transcribe all visible text '
                     f"verbatim; do not skip any image.")
    parts.append(build_transcribe_prompt(user_prompt))
    return "\n".join(parts)


def build_video_prompt(frameset, context: str | None, user_prompt: str) -> str:
    lines = []
    if context:
        lines.append(f"[Previous summary]\n{context}")
    lines.append(f"The video is about {frameset.duration:.1f} seconds long; "
                 f"{len(frameset.frames)} frames were sampled (mode: {frameset.mode}).")
    if frameset.mode == "contact":
        ts = frameset.meta.get("timestamps", [])
        lines.append(f"The contact sheet is a {frameset.meta.get('cols')}x"
                     f"{frameset.meta.get('rows')} grid with {len(ts)} cells; "
                     f"each cell is a thumbnail sampled from the video in time "
                     f"order (left to right, top to bottom), corresponding to "
                     f"timestamps: {', '.join(f'{t:.1f}s' for t in ts)}")
        lines.append("Describe what each cell shows (colors, elements, text) and "
                     "note what changes between adjacent cells.")
    else:
        times = ", ".join(f"Frame{i + 1}@{f.t:.1f}s" for i, f in enumerate(frameset.frames))
        lines.append(f"Frame order: {times}")
    if frameset.mode == "text":
        band = frameset.meta.get("band", "bottom")
        band_name = ("bottom" if band == "bottom" else "top" if band == "top"
                     else "middle" if band == "middle" else "full-frame")
        lines.append(f"These frames are crops of the video's {band_name} "
                     f"subtitle/lyrics/text band, in time order.")
        lines.append("Transcribe all visible text in every frame, labeling frame "
                     "number and timestamp; frames with identical text may be "
                     "merged, but every text occurrence must be covered.")
    lines.append(f"Task: {user_prompt}")
    lines.append("Describe the scene content and changes in chronological order, "
                 "including all visible text; only describe what is actually "
                 "visible and note anything you are unsure about.")
    return "\n".join(lines)


def build_video_transcribe_prompt(frameset, context: str | None,
                                  user_prompt: str | None) -> str:
    """Transcription-only video prompt: frame positions + transcription rules,
    with no description/summary guidance."""
    lines = []
    if context:
        lines.append(f"[Previous summary]\n{context}")
    times = ", ".join(f"Frame{i + 1}@{f.t:.1f}s" for i, f in enumerate(frameset.frames))
    lines.append(f"Frame order: {times}")
    if frameset.mode == "text":
        band = frameset.meta.get("band", "bottom")
        band_name = ("bottom" if band == "bottom" else "top" if band == "top"
                     else "middle" if band == "middle" else "full-frame")
        lines.append(f"These frames are crops of the video's {band_name} "
                     f"subtitle/lyrics/text band, in time order.")
        lines.append('Transcribe all visible text frame by frame using the format '
                     '"Frame N@time: content"; frames with identical text may be '
                     "merged, but every text occurrence must be covered.")
    lines.append(build_transcribe_prompt(user_prompt))
    return "\n".join(lines)


def mojibake_score(text: str) -> int:
    """Estimate how likely text was mangled into '?'/U+FFFD; the transcription
    contract's [?] marks do not count."""
    compact = text.replace("[?]", "")
    runs = re.findall(r"\?{4,}", compact)
    run_q = sum(len(r) for r in runs)
    return 6 * len(runs) + max(0, compact.count("?") - run_q) + 5 * compact.count("\ufffd")


def warn_encoding_if_suspicious(text: str, media_label: str = "") -> None:
    if mojibake_score(text) < MOJIBAKE_THRESHOLD:
        return
    label = f" ({media_label})" if media_label else ""
    print(f"[vision] Output may have encoding problems{label}: many '?' or "
          "replacement characters detected; the input may have been re-encoded.",
          file=sys.stderr)
    print(ENCODING_HINT, file=sys.stderr)


def enforce_duration_limit(duration: float, label: str) -> None:
    """Reject media longer than the configured cap before expensive work."""
    limit = config.max_duration_h() * 3600
    if limit > 0 and duration > limit:
        raise RuntimeError(
            f"{label} is {duration / 3600:.1f} hours long, exceeding the "
            f"{config.max_duration_h():.1f}-hour limit "
            f"(set VISION_MAX_DURATION_H / max_duration_h to adjust).")


def json_result_image(text: str, media: list[str]) -> dict:
    return {"text": text, "mode": "image", "media": list(media)}


def json_result_video(text: str, frameset, title: str = "", source: str = "") -> dict:
    result = {
        "text": text,
        "mode": frameset.mode,
        "duration": round(frameset.duration, 2),
        "frames": [{"t": round(f.t, 2), "w": f.w, "h": f.h}
                   for f in frameset.frames],
        "meta": frameset.meta or {},
    }
    if title:
        result["title"] = title
    if source:
        result["source"] = source
    return result


def json_result_audio(text: str, source: str) -> dict:
    return {"text": text, "source": source}


def print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def run_speech(media: str, lang: str, asr_model: str,
               input_bytes: bytes | None = None,
               echo_stderr: bool = False) -> tuple[str, bool]:
    """Transcribe audio; segment lines stream to stdout and (full text,
    whether a fallback model was used) is returned. With echo_stderr=True the
    streaming lines go to stderr so stdout stays a single clean JSON object."""
    py = config.speech_python()
    if not py or not Path(py).exists():
        raise RuntimeError("Speech environment not found"
                           + (f": {py}" if py else " (not found)")
                           + " (set VISION_SPEECH_PYTHON)")

    def transcribe(model: str) -> str:
        return _run_speech_proc(
            [py, str(SPEECH_PY), media, "--lang", lang, "--model", model],
            input_bytes=input_bytes, echo_stderr=echo_stderr,
        )

    text = transcribe(asr_model)
    if text or asr_model != "paraformer":
        return text, False
    # paraformer is speech-oriented and often misses singing/music;
    # fall back to SenseVoice, which handles music much better.
    try:
        text2 = transcribe("sensevoice")
    except Exception:
        return text, False
    return (text2, True) if text2 else (text, False)


def _run_speech_proc(cmd: list[str], input_bytes: bytes | None = None,
                     timeout: int = 1800, echo_stderr: bool = False) -> str:
    """Run a speech subprocess and return its full stdout text.

    The stdout/stderr pump threads start BEFORE stdin is written: a chatty
    child (torch / FunASR / ffmpeg logging) can otherwise fill the pipe
    buffer while the parent is still writing a large media payload, which
    deadlocks both processes.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out_lines: list[str] = []
    err_lines: list[str] = []

    def pump(stream, lines: list[str], echo: bool) -> None:
        for raw in stream:
            line = raw.decode("utf-8", errors="replace")
            lines.append(line)
            if echo:
                out = sys.stderr if echo_stderr else sys.stdout
                out.write(line)
                out.flush()

    tout = threading.Thread(target=pump, args=(proc.stdout, out_lines, True), daemon=True)
    terr = threading.Thread(target=pump, args=(proc.stderr, err_lines, False), daemon=True)
    tout.start()
    terr.start()
    try:
        if input_bytes is not None:
            try:
                proc.stdin.write(input_bytes)
            except BrokenPipeError:
                pass  # child exited before reading; proc.wait reports its status
            proc.stdin.close()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"Speech transcription timed out (>{timeout // 60} minutes)")
    tout.join(timeout=10)
    terr.join(timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(("".join(err_lines)).strip() or "Speech transcription failed")
    return "".join(out_lines).strip()


def analyze_images(args) -> None:
    b64_list = []
    for arg in args.media:
        spec = media.resolve_input(arg, want="image")
        if spec.kind != "image":
            raise RuntimeError(f"Multi-image mode accepts images only; got: {arg}")
        raw = spec.data if spec.data is not None else Path(spec.path).read_bytes()
        b64_list.append(media.image_to_b64(raw, args.crop, args.size))
    if not b64_list:
        raise RuntimeError("No analyzable images.")
    if args.transcribe:
        prompt = build_image_transcribe_prompt(len(b64_list), args.context, args.prompt)
    else:
        prompt = build_image_prompt(len(b64_list), args.context,
                                    args.prompt or "Describe these images in detail, "
                                                   "including all visible text and UI elements.")
    result = ask_model(prompt, b64_list, args.mode)
    warn_encoding_if_suspicious(result, args.media[0] if args.media else "")
    if args.json:
        print_json(json_result_image(result, args.media))
        return
    print(result, flush=True)


@contextlib.contextmanager
def _ffmpeg_headers_scope():
    """Restore VISION_FFMPEG_HEADERS to its prior value when the block exits.

    Resolved site streams may set per-request headers (signature / referer /
    user-agent) via this env var before ffmpeg opens the URL. Restoring the
    prior value on exit prevents those headers from leaking into later ffmpeg
    calls that have no headers of their own (the in-process service facade
    runs many such calls in one process).
    """
    previous = os.environ.get("VISION_FFMPEG_HEADERS")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("VISION_FFMPEG_HEADERS", None)
        else:
            os.environ["VISION_FFMPEG_HEADERS"] = previous


def analyze_video(arg: str, args) -> None:
    spec = media.resolve_input(arg, want="video")
    if spec.kind == "image":
        analyze_images(args)
        return
    if spec.kind == "audio":
        raise RuntimeError("Pure audio: use --mode audio")
    media_arg = spec.path if spec.path is not None else "-"
    if not media_arg:
        raise RuntimeError("Resolved to a video but no playable video stream is available.")
    max_side = 320 if args.size == "small" else 1600
    with _ffmpeg_headers_scope():
        if spec.headers:
            os.environ["VISION_FFMPEG_HEADERS"] = json.dumps(spec.headers)
        for attempt in range(1, 4):
            try:
                info = video_plans.probe(media_arg, input=spec.data)
                if info["duration"] <= 0:
                    raise RuntimeError("Cannot read video duration; the file may be "
                                       "corrupted or not a valid video.")
                enforce_duration_limit(info["duration"], f"Video ({arg})")
                if args.mode == "auto":
                    if args.transcribe:
                        mode = "text"  # transcription defaults to the text band, not scene guessing
                    else:
                        mode = "skim" if info["duration"] <= 60 else "contact"
                else:
                    mode = args.mode
                if mode == "window":
                    if args.start is None or args.end is None:
                        raise RuntimeError("--mode window requires --from and --to")
                    frameset = video_plans.scheme_window(
                        media_arg, info, args.start, args.end, fps=args.fps or 1.0,
                        max_frames=args.max_frames or 24, max_side=max_side,
                        dedupe=not args.no_dedupe, input=spec.data)
                elif mode == "burst":
                    if args.start is None:
                        raise RuntimeError("--mode burst requires --from")
                    frameset = video_plans.scheme_burst(
                        media_arg, info, args.start, duration=args.duration or 3.0,
                        fps=args.fps or 5.0, max_frames=args.max_frames or 24,
                        max_side=max_side, input=spec.data)
                elif mode == "segments":
                    frameset = video_plans.scheme_segments(
                        media_arg, info, n=args.max_frames or 24, max_side=max_side, input=spec.data)
                elif mode == "text":
                    frameset = video_plans.scheme_text(
                        media_arg, info, fps=args.fps or 1.0,
                        max_frames=args.max_frames or 48, max_side=max_side,
                        dedupe=not args.no_dedupe, band=args.band, input=spec.data)
                elif mode == "scenes":
                    frameset = video_plans.scheme_scenes(
                        media_arg, info, max_frames=args.max_frames or 24,
                        max_side=max_side, input=spec.data)
                elif mode == "contact":
                    frameset = video_plans.scheme_contact(
                        media_arg, info, n=args.max_frames or 24, input=spec.data)
                else:
                    frameset = video_plans.scheme_skim(
                        media_arg, info, fps=args.fps or 1.0,
                        max_frames=args.max_frames or 24, max_side=max_side,
                        dedupe=not args.no_dedupe, input=spec.data)
                break
            except Exception as e:
                if spec.source != "url" or attempt == 3:
                    raise
                print(f"[vision] URL stream read failed; re-resolving and retrying "
                      f"({attempt + 1}/3): {e}",
                      file=sys.stderr)
                spec = media.resolve_input(arg, want="video")
                if spec.headers:
                    os.environ["VISION_FFMPEG_HEADERS"] = json.dumps(spec.headers)
                media_arg = spec.path if spec.path is not None else "-"

    images = [{"b64": base64.b64encode(f.jpeg).decode("ascii"), "w": f.w, "h": f.h, "t": f.t}
              for f in frameset.frames]
    images = fit_budget(images, budget_pixels())
    if len(images) < len(frameset.frames):
        step = max(1, len(frameset.frames) // len(images))
        frameset.frames = frameset.frames[::step][: len(images)]
        print(f"[vision] Auto-downsampled to {len(frameset.frames)} frames "
              "by pixel budget", file=sys.stderr)
    if args.transcribe:
        prompt = build_video_transcribe_prompt(frameset, args.context, args.prompt)
    else:
        prompt = build_video_prompt(frameset, args.context,
                                    args.prompt or "Describe in chronological order what "
                                                   "happens in this video, including scene "
                                                   "changes and all visible text.")
    result = ask_model(prompt, [im["b64"] for im in images], mode)
    warn_encoding_if_suspicious(result, arg)
    if args.json:
        print_json(json_result_video(result, frameset, spec.title or "",
                                     spec.source))
        return
    print(result, flush=True)
    times = ", ".join(f"{f.t:.1f}s" for f in frameset.frames)
    title = f" title={spec.title}" if spec.title else ""
    print(f"\n[meta] mode={frameset.mode} frames={len(frameset.frames)} "
          f"duration={frameset.duration:.1f}s times={times}{title}", flush=True)


def analyze_audio(arg: str, args) -> None:
    spec = media.resolve_input(arg, want="audio")
    if spec.kind == "image":
        raise RuntimeError(
            f"'{arg}' is an image; use default mode for image analysis "
            "(or --mode text --transcribe for OCR).")
    if spec.subtitle_text:
        if args.json:
            print_json(json_result_audio(spec.subtitle_text, "subtitle"))
            return
        print(f"[Subtitle track]\n{spec.subtitle_text}")
        print("\n[meta] source=subtitle")
        return
    media_arg = spec.path if spec.path is not None else "-"
    if spec.path is None and spec.data is None:
        raise RuntimeError("Resolved to an audio stream but no playable stream is available.")
    with _ffmpeg_headers_scope():
        if spec.headers:
            os.environ["VISION_FFMPEG_HEADERS"] = json.dumps(spec.headers)
        for attempt in range(1, 4):
            try:
                info = video_plans.probe(media_arg, input=spec.data)
                if info.get("has_subtitle"):
                    subtitle = video_plans.extract_subtitle_text(media_arg, input=spec.data)
                    if subtitle:
                        if args.json:
                            print_json(json_result_audio(subtitle, "subtitle"))
                            return
                        print(f"[Subtitle track]\n{subtitle}")
                        print("\n[meta] source=subtitle")
                        return
                enforce_duration_limit(info["duration"], f"Audio ({arg})")
                text, fallback = run_speech(media_arg, args.lang, args.asr_model,
                                            input_bytes=spec.data, echo_stderr=args.json)
                break
            except Exception as e:
                if spec.source != "url" or attempt == 3:
                    raise
                print(f"[vision] URL audio stream read failed; re-resolving and retrying "
                      f"({attempt + 1}/3): {e}",
                      file=sys.stderr)
                spec = media.resolve_input(arg, want="audio")
                if spec.headers:
                    os.environ["VISION_FFMPEG_HEADERS"] = json.dumps(spec.headers)
                if spec.path is None and spec.data is None:
                    raise RuntimeError(
                        "Resolved to an audio stream but no playable stream is available.")
                media_arg = spec.path if spec.path is not None else "-"
    if fallback:
        print("[vision] paraformer found no speech (possibly music); "
              "fell back to SenseVoice",
              file=sys.stderr)
    if args.json:
        print_json(json_result_audio(text, "asr"))
        return
    print("\n[meta] source=asr")


# Static probe image: a white background with bold "HELLO 123". It is
# embedded so the image half of the health check runs without ffmpeg or a
# system font (ffmpeg is still exercised by the probe-video step).
PROBE_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAA8AAAAFoCAYAAACYBpIxAAAAAXNSR0IArs4c6QAAAARnQU1BAACx"
    "jwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAABS0SURBVHhe7dfLsu22kUVR/f9PVzWq4ahl6SpB"
    "cOPBHCNidmwdELltBpV//Q8AAAA08Ff+BwAAAPBFFmAAAABasAADAADQggUYAACAFizAAAAAtGAB"
    "BgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAUL"
    "MAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1Y"
    "gAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjB"
    "AgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEAL"
    "FmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABa"
    "sAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQ"
    "ggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACA"
    "FizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAA"
    "tGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAA"
    "oAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAA"
    "AC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEA"
    "AGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwA"
    "AEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAA"
    "AABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAAD"
    "AADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAGgkb/++uu/AoAufPVgo/yX0Gor5bOrzcrz"
    "bg5Wy/8PzsS78vf9U6fL+2Y3y1m+MhdgAYat8qNabaV8drVZed7Nzcizqt0sZ6nWWf4Wv4p5+Zv+"
    "qZPk3Z52orzjaMBdvLWwUX5Eq62Uz642K8+7uRl5VrWb5SzVOsrfYGWMy9/w3zpB3umtTpB3mg24"
    "g7cVNsqPZ7WV8tnVZuV5Nzcjz6p2s5ylWic5+86oy9/u39op7/Krdsg7vB1wNm8pbJQfzWor5bOr"
    "zcrzbm5GnlXtZjlLtQ5y5pPiz/L3qrRD3mFFq+RzfxlwLm8obJQfzGor5bOrzcrzbm5GnlXtZjlL"
    "ta/LeU+Mf5a/VaXV8vmr+6V81qqA83gzYaP8UFZbKZ9dbVaed3Mz8qxqN8tZqn1Zznpy/Lf8jaqt"
    "lM/e1S/kM1YHnMVbCRvlR7LaSvnsarPyvJubkWdVu1nOUu2rcs4b4j/ytxlplXzu7t6UZ+8KOIc3"
    "EjbKD2S1lfLZ1WbleTc3I8+qdrOcpdrX5Hy3xfz/hivkM0/pLXnuroBzeCNho/xAVlspn11tVp53"
    "czPyrGo3y1mqfU3Od2Od5W/xpBXymaf0hjxzd8AZvI2wUX4cq62Uz642K8+7uRl5VrWb5SzVviRn"
    "u7mO8jd42q/l805rVp53QsB+3kTYKD+M1VbKZ1eblefd3Iw8q9rNcpZqX5Fzvdmf5D/7Zp3k7DP9"
    "Wj7vaSn/+6fNyLNG+zv5zzwJ2M+bCBvlh7HaSvnsarPyvGpfk/NVu1nOUu0LcqbZZuRZs31dzvtG"
    "v5TPGq0q/260p/KcahX5N6MBe3kLYaP8KFZbKZ9dbVaeV+1rcr5qN8tZqn1BzvS0N+XZT/uynPWt"
    "fimfVe2JPGOkJ/KMaiPyb0cC9vIWwkb5Uay2Uj672qw8r9rX5HzVbpazVLtdzvOkX8pnPelrcr63"
    "+6V8VrWn8pxqT+QZlZ7IM6oBe3kLYaP8KFZbKZ9dbVaeV+1rcr5qN8tZqt0sZ3nSCvnM0b4kZ/tF"
    "v5LPqTYrz6s2Kv++0lN5TiVgL28hbJQfxWor5bOrzcrzqn1NzlftZjlLtZvlLKOtlM8e7XY5zy/7"
    "lXxOpTfkmdVG5N9WmpFnVQL28hbCRvlRrLZSPrvarDyv2tfkfNVulrNUu1XOMdoOeYeRbpQzrOpX"
    "8jmV3pBnVhuRf1tpRp5VCdjLWwgb5Uex2kr57Gqz8rxqX5PzVbtZzlLtVjnHaDvkHUa7Td5/Vb+S"
    "z6n0hjyz2hN5xj81K8+rBOzlLYSN8qNYbaV8drVZeV61r8n5qt0sZ6l2q5xjpJ3yLiPdJu+/ql/I"
    "Z1R7S55b6U1vn53nVQL28hbCRvlRrLZSPrvarDyv2tfkfNVulrNUu1HOMNIJ8k4j3STvPtrTM34h"
    "n1HtLXlupZPlXSsBe3kLYaP8KFZbKZ9dbVaeV+1rcr5qN8tZqt0oZxjpBHmnkW6Sdx9p5oydfnGP"
    "nK/ayfKulYC9vIWwUX4Uq62Uz642K8+r9jU5X7Wb5SzVbpQzjHSCvNNIN8m7V0r531f6mpyv2qny"
    "ntWAvbyFsFF+FKutlM+uNivPq/Y1OV+1m+Us1W6T9x/pJHm3ajfJu/9bfyf/mUpfk/NVOlnetRKw"
    "nzcRNsoPY7WV8tnVZuV51b4m56t2s5yl2m3y/iOdJO820i3y3v/Un+Q/W+lrcr5Kp8p7VgP28ybC"
    "RvlhrLZSPrvarDyv2tfkfNVulrNUu03ef6ST5N1GukXe++/6N/nPV/qSnK3aifKO1YAzeBtho/w4"
    "Vlspn11tVp5X7Wtyvmo3y1mq3SbvX+1Eecdqt8h7P5kh/67Sl+Rs1U6SdxsNOIO3ETbKj2O1lfLZ"
    "1Wblebf1ljy32s1ylmq3yftXO1Hesdot8t5P7p5/X+krcq5qu+Q93gg4hzcSNsoP5Jealefd1lvy"
    "3Go3y1mq3SbvX+1Eecdqt3jjzjl7pa/IuartkveYDTiLtxI2yo/kl5qV593WW/LcajfLWardJO8+"
    "0onyjtU6ydkrfUHONNIueY+ZgPN4M2Gj/FB+qVl53m29Jc+tdrOcpdpN8u4jnSjvWK2TnL3S7XKe"
    "kXbKuzwJOJc3FDbKD+aXmpXn3dZb8txqN8tZqt0k7z7SifKOI3WRc1e6Wc4y2k55l9GAs3lLYaP8"
    "aH6pWXnebb0lz612s5yl2k3y7iOdKO84Uhc5d6Vb5Ryj7Zb3GQk4nzcVNsoP55ealefd1lvy3Go3"
    "y1mq3STvPtKJ8o4jdZFzV7pRzjDabnmfmYAzeTtho/xYfqlZed5tvSXPrXaznKXaTfLu1U6V9xyp"
    "i5y70m3y/k/aLe/zRsBZvJWwUX4kv9SsPO+23pLnVrtZzlLtJnn3aqfKe47URc5d6SZ59yedIO/0"
    "ZsAZvI2wUX4cv9SsPO+23pLnVrtZzlLtJnn3kU6Udxypi5y70i3y3k86Rd7r7YD9vImwUX4Yv9Ss"
    "PO+23pLnVrtZzlLtJnn3kU6Udxypi5y70g3yzk86yZ/ulfd+GrCXtxA2yo/il5qV593WW/LcajfL"
    "WardJO8+0onyjtU6ydkrnS7v+7Qb5QwjAXt5C2Gj/ChWWymfXW1Wnlfta3K+ajfLWardJO8+0ony"
    "jtU6ydkrnSzv+rTb5TzVgH28gbBRfhCrrZTPrjYrz6v2NTlftZvlLNVuk/evdqK8Y7VOcvZKp8p7"
    "Pu0rcq5qwB7ePtgoP4bVVspnV5uV51X7mpyv2s1ylmq3yftXO1HesVonOXulE+Udn/YlOVs1YA9v"
    "H2yUH8NqK+Wzq83K86p9Tc5X7WY5S7Xb5P2rnSjvWK2TnL3SafJ+T/uinLEasJ43DzbKD2G1lfLZ"
    "1WbledW+JuerdrOcpdpt8v7VTpP3G6mTnL3SSfJuT/uqnLMasJ43DzbKD2G1lfLZ1WbledW+Juer"
    "drOcpdpt8v4jnSTvNlInOXulU+S9nvZ1OW8lYD1vHmyUH8JqK+Wzq83K86p9Tc5X7WY5S7Xb5P1H"
    "OknebaROcvZKJ8g7Pa2DnLkSsJ43DzbKD2G1lfLZ1WbledW+JuerdrOcpdqNcoZqJ8m7Vesm56+0"
    "W97naV3k3JWA9bx5sFF+CKutlM+uNivPq/Y1OV+1m+Us1W6UM4x0grzTSN3k/JV2yrs8qZucvxKw"
    "njcPNsoPYbWV8tnVZuV51b4m56t2s5yl2o1yhpFOkHcaqZucv9IueY8ndZS/QTVgLW8dbJQfwWor"
    "5bOrzcrzqn1NzlftZjlLtRvlDKPtlHcZrZucv9IOeYcn3eLtu+bvUAlYz5sHG+WHsNpK+exqs/K8"
    "al+T81W7Wc5S7VY5x0g75V1G6ih/g0qr5fOfdJK829/1pjy7ErCeNw82yg9htZXy2dVm5XnVvibn"
    "q3aznKXarXKO0XbIO4zWUf4GlVbL54+2W96n0pvy7ErAet482Cg/hNVWymdXm5XnVfuanK/azXKW"
    "ajfLWUZbKZ89Wlf5O1RaKZ892gnyTpXekudWA9bz5sFG+SGstlI+u9qsPK/a1+R81W6Ws1S7Wc7y"
    "pBXymU/qKn+HSqvkc0c7Rd6r2hvyzGrAet482Cg/hNVWymdXm5XnVfuanK/azXKWarfLeZ70S/ms"
    "J3WWv0WlFfKZTzpJ3q3SrDxvJGA9bx5slB/Caivls6vNyvOqfU3OV+1mOUu12+U8T/uFfMbTOsvf"
    "otIK+czRTpP3qzYjz6oG7OHtg43yY1htpXx2tVl5XrWvyfmq3SxnqfYFOdNMb8gzZ+ouf49Kv5bP"
    "G+1EeceRnsgzRgL28PbBRvkxrLZSPrvarDyv2tfkfNVulrNU+4Kc6a1G5d/PxrPf9Nfyeac2Kv9+"
    "pBH5t6MBe3j7YKP8GFZbKZ9dbVaed3tP5Tk3Nir//rZm5XlfiGf/u/5SPuvkRuXfj1aRfzMasI83"
    "EDbKD2K1lfLZ1Wblebf3VJ5zY6Py72/rDXnmzfF/8nep9Ev5rJN7Is84LWAfbyBslB/Eaivls6vN"
    "yvNu76k858ZG5d/f1lvy3BvjP/K3qfQr+ZzTeyLPOClgL28hbJQfxWor5bOrzcrzbu+pPOfGRuXf"
    "39ab8uyb4v/L36fSr+RzTu+pPOeEgP28ibBRfhirrZTPrjYrz7u9p/KcGxuVf39bb8vzb4j/lr9R"
    "pV/IZ9zQjDxrZ8AZvI2wUX4cq62Uz642K8+7vafynBsblX9/W7+Qzzg5/l7+TpV+IZ9xQ7PyvB0B"
    "5/BGwkb5gay2Uj672qw87/aeynNubFT+/W39Sj7nxPhn+VtV+oV8xg29Ic9cGXAWbyVslB/Jaivl"
    "s6vNyvNu76k858ZG5d/f1q/l806If5e/WaVfyGfc0Fvy3BUB5/Fmwkb5oay2Uj672qw87/aeynNu"
    "bFT+/W2tks/dEXX521V6W55/S2/Ks38ZcCZvJ2yUH8tqK+Wzq83K827vqTznxkbl39/Wavn8FTEu"
    "f8NKb8vzb+kX8hlvBpzNWwob5Uez2kr57Gqz8rzbeyrPubFR+fe3tUve4xfxXP6Wld6W59/SL+Wz"
    "ZgLu4G0FgA/Kfzl/EnSS//+vBNzHmwsAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAA"
    "oAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAA"
    "AC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEA"
    "AGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwA"
    "AEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAA"
    "AABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAAD"
    "AADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUY"
    "AACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizA"
    "AAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGAB"
    "BgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAUL"
    "MAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1Y"
    "gAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjB"
    "AgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEAL"
    "FmAAAABasAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABa"
    "sAADAADQggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQ"
    "ggUYAACAFizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACA"
    "FizAAAAAtGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEALFmAAAABasAADAADQggUYAACAFizAAAAA"
    "tGABBgAAoAULMAAAAC1YgAEAAGjBAgwAAEAL/wtpmYToGy8ZaAAAAABJRU5ErkJggg=="
)


def gen_probe_image(text: str = "HELLO 123") -> bytes:
    """Return the static probe image bytes (white background + 'HELLO 123')."""
    return base64.b64decode("".join(PROBE_IMAGE_B64))


def gen_probe_video() -> bytes:
    code, out, err = video_plans.run_ffmpeg(
        ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
         "-t", "2", "-pix_fmt", "yuv420p",
         "-f", "mp4", "-movflags", "frag_keyframe+empty_moov", "pipe:1"],
        timeout=60)
    if code != 0 or not out:
        raise RuntimeError(f"Probe video generation failed: {err.strip()[:300]}")
    return out


def run_cli(args: list[str], stdin_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "vision.py")] + args,
        input=stdin_bytes, capture_output=True, timeout=900)


def run_health_check() -> int:
    """Environment health check: image reading / verbatim transcription /
    stdin pipe / video sampling."""
    results: list[tuple[str, str, str]] = []  # (name, status, detail)

    def record(name: str, status: str, detail: str = "") -> None:
        results.append((name, status, detail))
        print(f"[{status}] {name}" + (f" - {detail}" if detail else ""), flush=True)

    def warn_missing_models() -> None:
        try:
            have = ollama_client.tags()
        except Exception:
            return
        if not have:
            return
        missing = [m for m in (config.text_model(), config.quick_model())
                   if not _model_available(m, have)]
        if missing:
            print("[WARN] configured model(s) not found: " + ", ".join(missing),
                  file=sys.stderr)
            print("  Set VISION_TEXT_MODEL / VISION_QUICK_MODEL or edit "
                  "vision-config.json; any vision-capable Ollama model works "
                  "(e.g. qwen3.5:4b, llava, minicpm-v).", file=sys.stderr)

    warn_missing_models()

    img = None
    try:
        img = gen_probe_image()
        record("probe image generation", "PASS")
    except Exception as e:
        record("probe image generation", "FAIL", str(e)[:200])

    if img:
        try:
            r = run_cli(["-", "--prompt",
                         "What text is in the image? Answer with the text content only"], img)
            out = r.stdout.decode("utf-8", "replace")
            ok = r.returncode == 0 and "HELLO 123" in out
            record("quick image read (stdin)", "PASS" if ok else "FAIL",
                   "" if ok else (out or r.stderr.decode("utf-8", "replace"))[:200])
        except Exception as e:
            record("quick image read (stdin)", "FAIL", str(e)[:200])
        try:
            r = run_cli(["-", "--mode", "text", "--transcribe", "--prompt",
                         "Answer with the text in the image"], img)
            out = r.stdout.decode("utf-8", "replace")
            ok = r.returncode == 0 and "HELLO 123" in out
            record("verbatim transcription (stdin)", "PASS" if ok else "FAIL",
                   "" if ok else (out or r.stderr.decode("utf-8", "replace"))[:200])
        except Exception as e:
            record("verbatim transcription (stdin)", "FAIL", str(e)[:200])

    vid = None
    try:
        vid = gen_probe_video()
        record("probe video generation", "PASS")
    except Exception as e:
        if "ffmpeg not found" in str(e).lower():
            record("probe video generation", "SKIP",
                   "ffmpeg not installed; video checks skipped")
        else:
            record("probe video generation", "FAIL", str(e)[:200])

    if vid:
        try:
            r = run_cli(["-", "--mode", "skim", "--max-frames", "2",
                         "--prompt", "Describe the scene"], vid)
            out = r.stdout.decode("utf-8", "replace")
            ok = r.returncode == 0 and out.strip() and "Analysis failed" not in out
            record("video sampling (stdin)", "PASS" if ok else "FAIL",
                   "" if ok else (out or r.stderr.decode("utf-8", "replace"))[:200])
        except Exception as e:
            record("video sampling (stdin)", "FAIL", str(e)[:200])

    failed = [r for r in results if r[1] == "FAIL"]
    skipped = sum(1 for r in results if r[1] == "SKIP")
    passed = len(results) - len(failed) - skipped
    summary = f"\nHealth check complete: {passed}/{len(results)} passed"
    if skipped:
        summary += f", {skipped} skipped"
    print(summary)
    return 1 if failed else 0


def doctor_report() -> dict:
    """Return dependency/config diagnostics without contacting model inference."""
    checks = []
    def add(name, status, detail="", required=True):
        checks.append({"name": name, "status": status, "detail": detail,
                       "required": required})
    add("python", "pass", sys.version.split()[0])
    try:
        add("ffmpeg", "pass", video_plans.ffmpeg_bin())
    except Exception as exc:
        add("ffmpeg", "missing", str(exc))
    if config.use_openai_api():
        add("model backend", "configured", config.api_base())
        add("models", "not-checked", "run vision --check for live endpoint/model checks")
    else:
        add("ollama", "configured", ollama_base(), required=False)
        add("models", "not-checked", "run vision --check for live Ollama/model checks")
    speech = config.speech_python_explicit()
    add("speech", "available" if speech and Path(speech).exists() else "optional-missing",
        speech or "set VISION_SPEECH_PYTHON", required=False)
    add("mcp", "pass", "in-process service facade")
    failed = [c for c in checks if c["required"] and c["status"] in ("missing", "unavailable", "error")]
    return {"ok": not failed, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local multimodal vision & speech helper (images / videos / audio)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("media", nargs="*", help="image/video/audio file path or URL")
    parser.add_argument("--prompt", default=None, help="question or instruction")
    parser.add_argument("--mode", choices=["auto", "contact", "scenes", "skim", "window",
                                           "segments", "burst", "text", "audio"], default="auto")
    parser.add_argument("--from", dest="start", default=None,
                        help="time-window start (90 / 1:30 / 00:01:30)")
    parser.add_argument("--to", dest="end", default=None, help="time-window end")
    parser.add_argument("--duration", type=float, default=None,
                        help="burst duration (seconds)")
    parser.add_argument("--fps", type=float, default=None, help="sampling frame rate")
    parser.add_argument("--max-frames", type=int, default=None,
                        help="frame cap (each mode has a default)")
    parser.add_argument("--band", choices=["bottom", "top", "middle", "full"], default="bottom",
                        help="text band to read (default bottom: subtitles/lyrics)")
    parser.add_argument("--size", choices=["small", "full"], default="full",
                        help="output size (small=320px / full=1600px)")
    parser.add_argument("--crop", default=None, help="image crop WxH+X+Y")
    parser.add_argument("--model", default=None,
                        help="force a vision model (overrides auto routing)")
    parser.add_argument("--ctx", type=int, default=0,
                        help="force context window size in tokens (0=auto)")
    parser.add_argument("--context", default=None,
                        help="previous summary (summary chain)")
    parser.add_argument("--lang", default="auto",
                        choices=list(config.SPEECH_LANGS),
                        help="speech language (" + "/".join(config.SPEECH_LANGS) + ")")
    parser.add_argument("--asr-model", default="sensevoice",
                        help="ASR model (sensevoice/paraformer)")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="disable static-frame dedupe")
    parser.add_argument("--transcribe", action="store_true",
                        help="verbatim transcription (no summary/judgment; long output)")
    parser.add_argument("--json", action="store_true", dest="json",
                        help="print a structured JSON result on stdout "
                             "{\"text\": ..., \"frames\": [...], \"mode\": ...}")
    parser.add_argument("--capture", choices=["screen", "clipboard"], default=None,
                        help="capture the screen or clipboard image, then analyze it")
    parser.add_argument("--keep-alive", default=None,
                        help="model residency, e.g. 5m / 0")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--doctor", action="store_true", help="diagnose local dependencies")
    parser.add_argument("--unload", action="store_true")
    parser.add_argument("--watchdog-start", action="store_true")
    parser.add_argument("--check", "--self-test", action="store_true", dest="check",
                        help="environment health check (image read/transcription/"
                             "stdin/video sampling)")
    args = parser.parse_args()

    global ACTIVE_MODEL, CTX_OVERRIDE
    ACTIVE_MODEL = args.model or resolve_model(args.mode)
    if args.ctx:
        CTX_OVERRIDE = args.ctx

    if args.keep_alive:
        os.environ["VISION_KEEP_ALIVE"] = args.keep_alive
    if args.transcribe:
        os.environ.setdefault("VISION_MAX_TOKENS", "98304")
    if args.status:
        print(status_text())
        return
    if args.doctor:
        print(json.dumps(doctor_report(), ensure_ascii=False) if args.json else
              "\n".join(f"[{c['status'].upper()}] {c['name']}: {c['detail']}"
                         for c in doctor_report()["checks"]))
        return
    if args.unload:
        unload_model()
        return
    if args.watchdog_start:
        start_watchdog(required=True)
        print("Watchdog started in the background (unloads models when idle "
              "or when another process needs VRAM).")
        return
    if args.check:
        if not ensure_ollama_started():
            if config.use_openai_api():
                print(f"Error: cannot connect to OpenAI-compatible endpoint "
                      f"({config.api_base()}); health check cannot run.",
                      file=sys.stderr)
            else:
                print("Error: cannot connect to Ollama; health check cannot run.",
                      file=sys.stderr)
            sys.exit(1)
        sys.exit(run_health_check())

    if args.capture:
        if args.media:
            parser.error("--capture cannot be combined with media arguments")
        try:
            from extras.capture import capture_image

            args.media = [str(capture_image(args.capture))]
        except Exception as e:
            print(f"Capture failed: {friendly_error(e)}", file=sys.stderr)
            sys.exit(1)

    if not args.media:
        parser.error("a media file path or URL is required")

    if args.start is not None:
        args.start = parse_time(args.start)
    if args.end is not None:
        args.end = parse_time(args.end)

    if not ensure_ollama_started():
        if config.use_openai_api():
            print(f"Error: cannot connect to OpenAI-compatible endpoint "
                  f"({config.api_base()}). Check the api_base / api_key settings.",
                  file=sys.stderr)
        else:
            exe = ollama_exe()
            hint = f"({exe} serve)" if exe else "(set OLLAMA_EXE to point to it)"
            print(f"Error: cannot connect to Ollama and auto-start failed. "
                  f"Start Ollama first {hint}.",
                  file=sys.stderr)
        sys.exit(1)

    try:
        if args.mode == "audio":
            if len(args.media) != 1:
                raise RuntimeError("--mode audio accepts exactly one file")
            analyze_audio(args.media[0], args)
            return
        if len(args.media) > 1:
            analyze_images(args)
        else:
            analyze_video(args.media[0], args)
        touch_last_use()
        if not config.use_openai_api():
            start_watchdog()
    except Exception as e:
        print(f"Analysis failed: {friendly_error(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
