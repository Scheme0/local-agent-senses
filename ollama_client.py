"""Vision backend client: local Ollama or any OpenAI-compatible endpoint.

All backend calls go through this module. Set api_base to switch to the
OpenAI-compatible protocol (POST {api_base}/chat/completions); otherwise the
native Ollama API is used.
"""

import functools
import base64
import json
import urllib.error
import urllib.request

import config


def _headers(token: bool = False) -> dict:
    h = {"Content-Type": "application/json"}
    if token and config.api_key():
        h["Authorization"] = "Bearer " + config.api_key()
    return h


def api(method: str, path: str, payload: dict | None = None, timeout: int = 600):
    """Native Ollama API."""
    url = config.ollama_base() + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {e.code}: {body[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot connect to Ollama ({config.ollama_base()}): "
                           f"{e.reason}") from e


def openai_api(method: str, path: str, payload: dict | None = None, timeout: int = 600):
    """OpenAI-compatible endpoint."""
    url = config.api_base() + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(token=True))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vision backend returned HTTP {e.code}: {body[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot connect to vision backend ({config.api_base()}): "
                           f"{e.reason}") from e


def ready() -> bool:
    try:
        if config.use_openai_api():
            openai_api("GET", "/models", timeout=5)
        else:
            api("GET", "/api/version", timeout=5)
        return True
    except Exception:
        return False


def ps() -> list[str]:
    """Models currently loaded (local Ollama only; remote endpoints return [])."""
    if config.use_openai_api():
        return []
    data = api("GET", "/api/ps", timeout=10)
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def tags() -> list[str]:
    """Available model names (Ollama /api/tags or OpenAI-compatible /models)."""
    if config.use_openai_api():
        data = openai_api("GET", "/models", timeout=5)
        models = data.get("data") or data.get("models") or []
        return [m.get("id", "") or m.get("name", "")
                for m in models if isinstance(m, dict)]
    data = api("GET", "/api/tags", timeout=5)
    return [m.get("name", "") for m in data.get("models", [])]


def _data_uri(b64: str) -> str:
    """Data URI with the real image MIME (sniffed from the base64 head).

    Images are normalized to png/jpeg/webp before reaching this point, so
    only those three types are detected; unknown bytes default to image/jpeg.
    """
    try:
        head = base64.b64decode(b64[:32])
    except Exception:
        head = b""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif head[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{b64}"


def build_openai_messages(prompt: str, images_b64: list[str]) -> list[dict]:
    """OpenAI-compatible messages: images become data-URI image_url blocks
    with their real MIME type (png/jpeg/webp)."""
    content: list[dict] = [{"type": "text", "text": prompt}]
    for b64 in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": _data_uri(b64)},
        })
    return [{"role": "user", "content": content}]


def _extract_openai_text(data: dict) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Model returned no content: "
                           + json.dumps(data, ensure_ascii=False)[:400])
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            return content
    elif isinstance(content, list):
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if text.strip():
            return text
    # Thinking models (Qwen / DeepSeek style) may put the visible answer in
    # reasoning_content with an empty content field.
    thinking = message.get("reasoning_content")
    if isinstance(thinking, str) and thinking.strip():
        return thinking
    raise RuntimeError("Model returned unparsable content: "
                       + json.dumps(data, ensure_ascii=False)[:400])


def chat(model: str, prompt: str, images_b64: list[str], *,
         num_predict: int, num_ctx: int, temperature: float = 0,
         think: bool | None = None, keep_alive: str = "10m", timeout: int = 600) -> str:
    if config.use_openai_api():
        payload = {
            "model": model,
            "messages": build_openai_messages(prompt, images_b64),
            "max_tokens": num_predict,
            "temperature": temperature,
        }
        result = openai_api("POST", "/chat/completions", payload, timeout=timeout)
        return _extract_openai_text(result)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt, "images": images_b64}],
        "stream": False,
        "options": {"num_predict": num_predict, "num_ctx": num_ctx, "temperature": temperature},
        "keep_alive": keep_alive,
    }
    if think is not None:
        payload["think"] = think
    result = api("POST", "/api/chat", payload, timeout=timeout)
    message = result.get("message", {}) or {}
    content = message.get("content", "")
    if not content:
        # Qwen thinking mode returns the visible answer under `thinking` when
        # the content field is empty.
        thinking = message.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            content = thinking
    if not content:
        raise RuntimeError("Model returned no content: "
                           + json.dumps(result, ensure_ascii=False)[:400])
    return content


def unload_models(models: list[str]) -> None:
    """Unload models from local Ollama; no-op for remote endpoints."""
    if config.use_openai_api():
        return
    for m in models:
        api("POST", "/api/generate", {"model": m, "prompt": "", "keep_alive": 0}, timeout=30)


@functools.lru_cache(maxsize=32)
def model_context_limit(model: str) -> int | None:
    """Best-effort context length for a local Ollama model (None when unknown).

    Reads the model's metadata via /api/show and looks for a context_length
    entry (e.g. llama.context_length). Remote endpoints always return None.
    """
    if config.use_openai_api():
        return None
    try:
        data = api("POST", "/api/show", {"model": model}, timeout=10)
    except Exception:
        return None
    info = data.get("model_info") or {}
    for key, val in info.items():
        if str(key).endswith("context_length") and isinstance(val, int) and val > 0:
            return val
    return None
