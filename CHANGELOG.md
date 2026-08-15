# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-08-15

### Added

- `ConfigError` for broken config files: an explicit `VISION_CONFIG` that is
  missing, unreadable, invalid JSON, or not an object is a hard error with a
  readable message (no silent fallback to defaults); the CLI exits 2 and
  `--doctor --json` reports a structured `config` check instead of a traceback.
- `MAX_THUMBNAIL_FRAMES` hard cap (1200) on video thumbnail candidates, plus
  duration-bounded sampling FPS and windowed single-pass extraction so a short
  time window never decodes the whole video.
- Server-side MCP argument validation (`invalid_input` code): media strings,
  image lists, prompt/context/crop length caps, finite positive numbers
  (rejecting NaN/Infinity/negatives/bool-as-int), and strict mode/band/lang/
  asr_model enums.
- Staged execution deadline in the service facade: `deadline_exceeded` code
  distinct from `busy`, stage checks before probe/sampling/model/ASR/formatting,
  and remaining-time budgets for ffmpeg, speech subprocesses and Ollama calls.
- Remote endpoint policy (`api_base` must be https except localhost) and basic
  external-tool path validation for explicitly configured executables.
- MCP cache hardening: restrictive permissions on POSIX, per-entry/disk size
  and file-count limits, version/model/backend-aware cache keys, and controlled
  error logging without prompts, paths, keys or URLs.
- Locked dependencies (`uv.lock`) and a CI split into lint / unit-tests /
  packaging, with Actions pinned to commit SHAs and a clean-venv install check
  of the built wheel.
- `config.package_version()` as the single runtime version source; the CLI and
  the MCP server no longer hardcode version strings.

### Fixed

- Video thumbnail sampling no longer buffers unbounded raw frames from very
  long videos.
- yt-dlp resolution re-checks the original URL and every returned stream URL.
- `scripts/setup.sh` / `scripts/setup.ps1` now fail the health check by default
  (opt-out via `ALLOW_CHECK_FAILURE=1` / `-AllowCheckFailure`).
- Docs no longer over-claim DNS rebinding protection; direct ffmpeg URL
  streaming is documented as best-effort.

## [0.5.0] - 2026-08-13

### Added

- PDF rasterization via poppler `pdftoppm`: PDF files, URLs, and stdin are
  detected (`%PDF` magic / `.pdf` extension) and rendered page-by-page to PNG
  before being sent to the vision model as a multi-image request. Configure the
  renderer with `VISION_PDF_RENDERER` / `pdf_renderer`, and cap work with
  `VISION_MAX_PDF_PAGES` (default 50) and `VISION_PDF_DPI` (default 150).

## [0.4.6] - 2026-08-13

### Fixed

- `VISION_FFMPEG_HEADERS` (per-request site-stream headers) is restored to its
  prior value after each analysis, so resolved headers no longer leak into
  later ffmpeg calls in the in-process service facade.
- `vision_check` through MCP now surfaces the health-check detail on failure
  instead of a bare "failed" message.
- Piped stdin media now aborts early while streaming once the kind-appropriate
  size cap is exceeded, instead of buffering the whole stream before checking.
- The MCP `transcribe` tool now forwards `prompt` / `context` / `crop` / `size`
  (previously declared in its schema but silently dropped).

### Changed

- MCP `describe_image` / `analyze_video` / `transcribe` schemas now expose the
  `context`, `band`, `duration`, and `no_dedupe` options the service facade
  already supports, so the MCP surface mirrors the CLI.
- SKILL.md trigger description no longer lists PDFs (rasterization is not yet
  implemented).
- `service_max_concurrency` now documents that a value >1 queues admission
  rather than enabling parallel inference.

## [0.4.5] - 2026-08-13

### Added

- `VISION_MAX_STDIN_MB` / `max_stdin_mb` cap for piped non-image stdin
  media (default 2000 MB; `0` disables).

### Fixed

- `service.py` serializes its process-global stdout/stderr redirection so the
  in-process facade is safe to call from concurrent threads.
- `extras/speech.py` applies the shared SSRF guard and stdin size cap when run
  standalone.

## [0.4.4] - 2026-08-13

### Added

- `pyproject.toml` ruff configuration and a dedicated lint CI job.

### Changed

- Refactored `scheme_skim`, `scheme_window`, and `scheme_text` to share the
  deduplication / uniform-sampling logic via `_dedupe_and_sample`.

## [0.4.3] - 2026-08-13

### Fixed

- `video_plans._scale_dims` now scales by the longest side, so tall/portrait
  videos and images are no longer returned at full height when `max_side` is set.
- `extras/mcp_server.py` no longer constructs unused CLI command lists or keeps
  stale `VISION_PY` / `DEFAULT_TIMEOUT` constants.

### Changed

- `.gitignore` now ignores `dist-*/` release-verification directories.

## [0.4.2] - 2026-08-12

### Fixed

- Doctor no longer performs conda/FunASR environment discovery, which could
  make otherwise offline diagnostics time out on Ubuntu CI runners.
- Fast doctor checks now inspect only explicit speech configuration; full
  environment discovery remains part of the live health-check path.

## [0.4.1] - 2026-08-12

### Fixed

- `vision --doctor` is now genuinely offline and no longer waits on Ollama or
  remote endpoint probes when those services are unavailable.
- Doctor reports model connectivity as `not-checked` and directs users to the
  live `vision --check` command for inference/backend checks.

## [0.4.0] - 2026-08-12

### Added

- Offline end-to-end smoke tests covering CLI doctor, MCP startup/tool listing,
  and classified errors without Ollama, GPU, or external network access.
- `vision --doctor` with human-readable and JSON dependency diagnostics.
- Configurable service admission timeout and concurrency limit.

### Changed

- Resolver failures are now visible instead of silently falling back to generic
  URL handling after a resolver has matched a page.
- Busy and timeout service failures use explicit machine-readable error codes.

## [0.3.0] - 2026-08-12

### Added

- `ServiceResult` provides a stable structured envelope for service callers.
- `ServiceError` provides machine-readable error codes and messages.
- MCP success and error content now use JSON envelopes while retaining the
  existing MCP text-content transport.

## [0.2.2] - 2026-08-12

### Fixed

- MCP service calls now parse time-window strings and forward the complete
  supported option set.
- Service calls restore process-global model state after completion.
- MCP disk-cache writes are atomic and do not expose partial JSON files.
- MCP schemas no longer require optional prompts.
- CI compile coverage now includes the service facade and Python 3.11/3.14.

## [0.2.1] - 2026-08-12

### Fixed

- Packaging tests no longer depend on build artifacts left in a developer's
  temporary directory.
- CI now builds wheel and sdist artifacts before running packaging smoke checks.

## [0.1.0] - 2026-08-12

### Changed

- Remote media is buffered through the checked Python downloader by default;
  direct ffmpeg URL streaming is now opt-in with `direct_url_stream` or
  `VISION_DIRECT_URL_STREAM=1`.
- MCP disk caching is disabled by default to avoid persisting screenshots,
  transcripts, and other potentially sensitive model output.
- Image conversion uses unique temporary files and is safe for concurrent calls.
- MCP input validation now limits image count, prompt size, frame count, and FPS.

### Fixed

- Tests can run from source archives that do not contain `.git` metadata.

## [0.2.0] - 2026-08-12

### Changed

- MCP requests now use an in-process Python service facade instead of spawning
  a new CLI subprocess for every tool call.
- The new `service.py` boundary is reusable by future Python integrations while
  preserving the existing CLI and MCP result formats.

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
