#!/usr/bin/env bash
# local-agent-senses one-click environment setup (Linux/macOS).
# Usage: bash scripts/setup.sh
#   SKIP_MODEL_PULL=1 to skip model downloads
#   SKIP_CHECK=1      to skip the health check (models may be absent)
#   ALL_ADAPTERS=1    to generate adapters for every known agent tool
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 1/5 Dependency check =="
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[FAIL] python3 not found; install Python 3.10+"; exit 1
fi
echo "[OK] python: $PY"

OLLAMA="$(command -v ollama || true)"
if [ -z "$OLLAMA" ]; then
  echo "[WARN] ollama not found; install it from https://ollama.com/download"
else
  echo "[OK] ollama: $OLLAMA"
fi

FF="$(command -v ffmpeg || true)"
if [ -z "$FF" ]; then
  echo "[WARN] ffmpeg not found. Image analysis works without it; video and audio need it."
else
  echo "[OK] ffmpeg: $FF"
fi

echo "== 2/5 Config =="
if [ ! -f "$ROOT/vision-config.json" ]; then
  cp "$ROOT/vision-config.example.json" "$ROOT/vision-config.json"
  echo "[OK] Created vision-config.json; edit the path fields if needed"
else
  echo "[OK] vision-config.json already exists"
fi

if [ "${SKIP_MODEL_PULL:-0}" != "1" ] && [ -n "$OLLAMA" ]; then
  echo "== 3/5 Model download (several GB the first time; SKIP_MODEL_PULL=1 skips) =="
  ollama pull haervwe/GLM-4.6V-Flash-9B
  ollama pull qwen3.5:4b
fi

if [ "${SKIP_CHECK:-0}" = "1" ]; then
  echo "== 4/5 Health check (skipped: SKIP_CHECK=1) =="
else
  echo "== 4/5 Health check =="
  "$PY" "$ROOT/vision.py" --check || echo "[WARN] health check failed; continuing"
fi

echo "== 5/5 Agent adapters =="
if [ "${ALL_ADAPTERS:-0}" = "1" ]; then
  "$PY" "$ROOT/scripts/generate_adapters.py" --all --out "$ROOT"
else
  "$PY" "$ROOT/scripts/generate_adapters.py" --out "$ROOT"
fi
echo "Tip: python scripts/generate_adapters.py --all --out <project-dir> generates adapters for every tool"
echo "Done. Example: python vision.py <image> --prompt \"describe the scene\""
