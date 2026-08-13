"""Central configuration: environment variables > config file > auto-detection.

Config file lookup order:
  1. $VISION_CONFIG (explicit path; no fallback when set)
  2. <repo>/vision-config.json
  3. ~/.config/vision/config.json  (XDG-style, cross-platform)
  4. ~/.cc-switch/vision-config.json (legacy, kept for compatibility)

Copy vision-config.example.json to vision-config.json to get started.
"""

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TEXT_MODEL = "haervwe/GLM-4.6V-Flash-9B"
DEFAULT_QUICK_MODEL = "qwen3.5:4b"
DEFAULT_SPEECH_ENV = "funasr"
SCRIPT_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path(tempfile.gettempdir()) / "codex-vision"

# Conda env names tried before scanning all environments for funasr.
SPEECH_ENV_PREFERENCES = ("funasr", "speech", "pytorch1")

# SenseVoice language codes (shared by vision.py and extras/speech.py).
SPEECH_LANGS = ("auto", "zh", "en", "yue", "ja", "ko")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@functools.lru_cache(maxsize=1)
def user_config() -> dict:
    """User-level JSON config; VISION_CONFIG overrides the search entirely."""
    explicit = env("VISION_CONFIG")
    if explicit:
        try:
            data = json.loads(Path(explicit).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    candidates = [
        SCRIPT_DIR / "vision-config.json",
        Path.home() / ".config" / "vision" / "config.json",
        Path.home() / ".cc-switch" / "vision-config.json",  # legacy
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            continue
    return {}


def cfg_value(key: str) -> str:
    """Read a value from the user config (fields documented in SKILL.md)."""
    return str(user_config().get(key, "") or "")


def _cfg_int(env_name: str, cfg_key: str, default: int) -> int:
    raw = env(env_name) or cfg_value(cfg_key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _cfg_bool(env_name: str, cfg_key: str, default: bool) -> bool:
    raw = (env(env_name) or cfg_value(cfg_key)).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def text_model() -> str:
    return env("VISION_TEXT_MODEL") or cfg_value("text_model") or DEFAULT_TEXT_MODEL


def quick_model() -> str:
    return env("VISION_QUICK_MODEL") or cfg_value("quick_model") or DEFAULT_QUICK_MODEL


def ollama_base() -> str:
    return (env("OLLAMA_HOST") or cfg_value("ollama_host")
            or "http://localhost:11434").rstrip("/")


def keep_alive() -> str:
    return env("VISION_KEEP_ALIVE") or cfg_value("keep_alive") or "10m"


def text_max_tokens() -> int:
    return _cfg_int("VISION_MAX_TOKENS", "max_tokens", 98304)


def quick_output_limit() -> int:
    return _cfg_int("VISION_QUICK_MAX_TOKENS", "quick_max_tokens", 16384)


def quick_think() -> bool:
    return _cfg_bool("VISION_QUICK_THINK", "quick_think", False)


def single_resident() -> bool:
    """Keep only one model resident by default; set VISION_SINGLE_RESIDENT=0
    to allow both models to stay loaded."""
    return _cfg_bool("VISION_SINGLE_RESIDENT", "single_resident", True)


def budget_pixels() -> int:
    return _cfg_int("VISION_BUDGET_PIXELS", "budget_pixels", 20_000_000)


def max_download_mb() -> int:
    """Size cap for buffered unknown-type URLs in MB (0 disables the cap)."""
    return _cfg_int("VISION_MAX_DOWNLOAD_MB", "max_download_mb", 500)


def max_duration_h() -> float:
    """Media duration cap in hours (0 disables the check)."""
    raw = env("VISION_MAX_DURATION_H") or cfg_value("max_duration_h")
    try:
        return float(raw) if raw else 6.0
    except ValueError:
        return 6.0


def max_image_mb() -> int:
    """Local/stdin image size cap in MB (0 disables the check)."""
    return _cfg_int("VISION_MAX_IMAGE_MB", "max_image_mb", 20)


def max_stdin_mb() -> int:
    """Piped stdin non-image media size cap in MB (0 disables the check).

    Stdin video/audio is buffered fully in memory before ffmpeg can probe it,
    so a separate generous cap guards against unbounded growth.
    """
    return _cfg_int("VISION_MAX_STDIN_MB", "max_stdin_mb", 2000)


def mcp_cache_enabled() -> bool:
    """Persist MCP results on disk across server restarts."""
    return _cfg_bool("VISION_MCP_CACHE", "mcp_cache", False)


def direct_url_streaming() -> bool:
    """Allow ffmpeg to resolve remote media URLs directly.

    Disabled by default because ffmpeg performs its own DNS resolution after
    Python's SSRF check. The secure default buffers remote media through the
    checked Python downloader before handing it to ffmpeg.
    """
    return _cfg_bool("VISION_DIRECT_URL_STREAM", "direct_url_stream", False)


def service_timeout_sec() -> float:
    raw = env("VISION_SERVICE_TIMEOUT", "") or cfg_value("service_timeout_sec")
    try:
        return max(0.1, float(raw)) if raw else 1800.0
    except ValueError:
        return 1800.0


def service_max_concurrency() -> int:
    """Admission concurrency for the in-process service facade.

    The vision engine writes results through process-global stdout and is
    single-threaded, so this caps queued admission rather than enabling true
    parallel inference; a value >1 does not make requests run concurrently.
    """
    return max(1, _cfg_int("VISION_MAX_CONCURRENCY", "max_concurrency", 1))


def mcp_cache_dir() -> str:
    """Explicit MCP disk-cache directory (empty means a per-OS default)."""
    return env("VISION_MCP_CACHE_DIR") or cfg_value("mcp_cache_dir")


def api_base() -> str:
    """OpenAI-compatible endpoint (e.g. Ollama /v1, Zhipu, DashScope);
    empty means local Ollama."""
    return (env("VISION_API_BASE") or cfg_value("api_base") or "").rstrip("/")


def api_key() -> str:
    """API key for the OpenAI-compatible endpoint; usually empty for local."""
    return env("VISION_API_KEY") or cfg_value("api_key") or ""


def use_openai_api() -> bool:
    return bool(api_base())


def speech_python() -> str:
    """Locate the Python interpreter used for speech transcription.

    Priority:
      1. VISION_SPEECH_PYTHON env var or speech_python config field
      2. A conda env matching VISION_SPEECH_ENV (default "funasr"),
         then a few common names (funasr / speech / pytorch1)
      3. Any conda env that can import funasr (auto-detection)
      4. Common conda install locations with the configured env name

    Returns "" when nothing is found; callers show a readable hint.
    """
    explicit = env("VISION_SPEECH_PYTHON") or cfg_value("speech_python")
    if explicit:
        return explicit
    env_name = env("VISION_SPEECH_ENV") or cfg_value("speech_env") or DEFAULT_SPEECH_ENV
    exe_name = "python.exe" if os.name == "nt" else "bin/python"
    found = _find_conda_env_python(env_name, exe_name)
    if found:
        return found
    for alt in SPEECH_ENV_PREFERENCES:
        if alt != env_name:
            found = _find_conda_env_python(alt, exe_name)
            if found:
                return found
    found = _find_env_with_funasr(exe_name)
    if found:
        return found
    program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    home = Path.home()
    roots = [
        program_data / "anaconda3",
        program_data / "miniconda3",
        home / "anaconda3",
        home / "miniconda3",
        home / "miniconda",
    ]
    for root in roots:
        for candidate in (root / "envs" / env_name / exe_name, root / exe_name):
            if candidate.exists():
                return str(candidate)
    return ""


def speech_python_explicit() -> str:
    """Return only an explicitly configured speech interpreter.

    Fast diagnostics must not invoke conda discovery or import checks.
    """
    return env("VISION_SPEECH_PYTHON") or cfg_value("speech_python")


@functools.lru_cache(maxsize=4)
def _find_conda_env_python(env_name: str, exe_name: str) -> str:
    """Locate an interpreter inside a conda env by name."""
    conda = shutil.which("conda") or shutil.which("micromamba")
    if not conda:
        return ""
    try:
        out = subprocess.run([conda, "env", "list"], capture_output=True, text=True,
                             timeout=30)
    except Exception:
        return ""
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, path = parts[0], parts[-1]
        if name != env_name and Path(path).name != env_name:
            continue
        exe = Path(path) / exe_name
        if exe.exists():
            return str(exe)
    return ""


@functools.lru_cache(maxsize=1)
def _find_env_with_funasr(exe_name: str) -> str:
    """Scan conda envs for the first interpreter that can import funasr."""
    conda = shutil.which("conda") or shutil.which("micromamba")
    if not conda:
        return ""
    try:
        out = subprocess.run([conda, "env", "list"], capture_output=True, text=True,
                             timeout=30)
    except Exception:
        return ""
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        exe = Path(parts[-1]) / exe_name
        if not exe.exists():
            continue
        try:
            r = subprocess.run([str(exe), "-c", "import funasr"],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return str(exe)
        except Exception:
            continue
    return ""


def ollama_exe() -> str | None:
    """Locate the ollama executable; explicit OLLAMA_EXE / config wins."""
    explicit = env("OLLAMA_EXE") or cfg_value("ollama_exe")
    if explicit:
        return explicit
    candidates = []
    for key, sub in (("LOCALAPPDATA", r"Programs\Ollama\ollama.exe"),
                     ("ProgramFiles", r"Ollama\ollama.exe"),
                     ("ProgramFiles(x86)", r"Ollama\ollama.exe")):
        base = os.environ.get(key, "")
        if base:
            candidates.append(Path(base) / sub)
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("ollama")


def models_dir() -> Path:
    return Path(env("OLLAMA_MODELS") or cfg_value("ollama_models")
                or str(Path.home() / ".ollama" / "models"))
