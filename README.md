# local-agent-senses

## English

![CI](https://github.com/Scheme0/local-agent-senses/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

Local vision, video and speech tools for text-only LLM agents. The default
backend is Ollama on your own machine; media is not uploaded unless you
explicitly configure an OpenAI-compatible endpoint.

中文说明见 [README.zh-CN.md](README.zh-CN.md)。

## 中文

中文版本请见 [README.zh-CN.md](README.zh-CN.md)。

## Features

- Image understanding and verbatim OCR/transcription.
- Video sampling for scenes, contact sheets, time windows and subtitles.
- Optional FunASR speech-to-text with timestamps.
- Windows screen and clipboard capture, plus platform adapters.
- CLI for shell-based agents and MCP tools for Codex, Claude Desktop, Cursor,
  Cline and other MCP clients.
- SSRF checks, download limits, media duration limits and no disk cache by
  default.

## Quick start

Requirements: Python 3.10+, Ollama, and ffmpeg. Speech and video-site URL
resolution are optional extras.

```bash
git clone https://github.com/Scheme0/local-agent-senses.git
cd local-agent-senses
ollama pull haervwe/GLM-4.6V-Flash-9B
ollama pull qwen3.5:4b
python vision.py --check
python vision.py photo.png --prompt "Describe this image"
python vision.py document.png --mode text --transcribe
python vision.py clip.mp4 --mode scenes --prompt "List each scene"
```

Install console commands with `pip install .`:

```bash
vision --check
vision-mcp
vision-adapters --all
```

The adapter generator is implemented by `scripts/generate_adapters.py` and can
also be invoked directly when integrating with another agent.

Optional dependencies:

```bash
pip install .[speech]
pip install .[ytdlp]
pip install .[test]
```

## MCP

Run the stdio server with:

```bash
python extras/mcp_server.py
```

Register it in Codex, for example:

```bash
codex mcp add vision -- python /absolute/path/extras/mcp_server.py
```

Available tools are `describe_image`, `transcribe`, `analyze_video`,
`transcribe_audio`, `vision_status` and `vision_check`.

MCP validates image count, prompt size, frame count and FPS before invoking the
CLI. The server uses an in-memory result cache. Disk caching is **disabled by
default** because results may contain screenshots, transcripts or private
documents. Enable it only when appropriate:

```bash
set VISION_MCP_CACHE=1       # Windows
export VISION_MCP_CACHE=1    # Linux/macOS
```

## Remote media and security

HTTP and HTTPS media URLs are treated as untrusted input. Local, private,
loopback, link-local, multicast, cloud-metadata and NAT64 addresses are
blocked, and redirects are checked again. By default, remote media is
downloaded through the checked Python path before ffmpeg sees it. This avoids
the DNS re-resolution gap that can occur when ffmpeg opens a URL directly.

Direct ffmpeg URL streaming is available only as an explicit compatibility
option and should be used only with trusted URLs:

```bash
set VISION_DIRECT_URL_STREAM=1       # Windows
export VISION_DIRECT_URL_STREAM=1    # Linux/macOS
```

Set limits in `vision-config.json` or with environment variables:

| Setting | Default | Purpose |
|---|---:|---|
| `VISION_MAX_IMAGE_MB` | 20 | Image size cap |
| `VISION_MAX_DOWNLOAD_MB` | 500 | Unknown/remote media cap |
| `VISION_MAX_DURATION_H` | 6 | Audio/video duration cap |
| `VISION_MCP_CACHE` | false | Persist MCP results to disk |
| `VISION_DIRECT_URL_STREAM` | false | Let ffmpeg open remote URLs directly |

Read [SECURITY.md](SECURITY.md) before exposing the MCP server to another
machine. Media content is untrusted data; do not follow instructions found in
images, subtitles or model output.

## Configuration

Copy `vision-config.example.json` to `vision-config.json`. Configuration
priority is environment variables, then JSON config, then auto-detection.
Important settings include `text_model`, `quick_model`, `ollama_host`,
`api_base`, `api_key`, `ffmpeg`, `speech_python`, `mcp_cache_dir` and the limits
listed above.

## Limitations

- Video understanding is frame-based rather than end-to-end temporal reasoning.
- OCR accuracy depends on resolution, layout, font and the selected model.
- FunASR is optional and may require a separate environment and model download.
- HEIC, AVIF and other uncommon formats depend on the installed ffmpeg build.
- Configuring a cloud endpoint sends model inputs to that endpoint; local
  sampling and audio processing remain local.

## Development

```bash
python -m pytest tests/ -v
python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md) and
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

## License

MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
