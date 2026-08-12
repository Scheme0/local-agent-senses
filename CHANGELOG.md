# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.1] - 2026-08-12

### Added

- Fully local multimodal vision & speech for text-only LLM agents (Codex,
  Claude Code, Cursor, Cline, Gemini CLI, and similar).
- `vision` CLI: images, PDFs, screenshots, video, audio, and media URLs, with
  `--transcribe`, `--capture`, `--crop`, and `--mode contact|scenes|skim|text|audio`.
- `vision-mcp` MCP server exposing the same capabilities over the Model
  Context Protocol.
- Ollama-backed OCR and quick vision presets (GLM-4.6V-Flash for verbatim
  transcription, Qwen3.5-4B for fast image/video understanding), plus an
  optional OpenAI-compatible endpoint.
- FunASR speech support through a configurable `speech_python` environment.
- Unified site resolution through yt-dlp by mechanism (common video sites),
  with a generic direct-link/buffer path for media URLs and no per-site parsers.
- Built-in probe assets so `vision --check` works even without ffmpeg.
- Optional `vision-adapters` generator for per-agent adapter files.

### Security

- NAT64 SSRF protection for URL fetching.
- Strict validation of `--crop` values before they reach ffmpeg.
- Transcription-by-default posture treats media text as untrusted data.
