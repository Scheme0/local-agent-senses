# local-agent-senses — Local Vision & Speech for Text-Only Agents

This tool gives a text-only LLM agent working eyes and ears: `python vision.py`
(Ollama + FunASR, fully local, no API keys, nothing is uploaded).

Use it when the user asks you to read, analyze, transcribe, or summarize images,
screenshots, charts, documents, videos, subtitles, audio, or media URLs — or when
image input fails because the current model is text-only ("does not support image
inputs" / "image content omitted").

## How to call

```bash
python vision.py <path-or-url> --prompt "<question>"   # describe / understand
python vision.py <image> --mode text --transcribe       # verbatim OCR, no summary
python vision.py <video> --mode text --transcribe       # subtitles / lyrics
python vision.py <video> --prompt "..."                 # scene understanding
python vision.py <file> --mode audio                    # speech-to-text
python vision.py --status                               # environment status
python vision.py --check                                # health check
```

If the repo is not the current working directory, call it by absolute path, e.g.
`python /path/to/local-agent-senses/vision.py ...`.

## Rules

- Text-heavy media (documents, screenshots, tables, subtitles) MUST use
  `--mode text --transcribe`; the vision model only transcribes, and you (the main
  model) do the analysis.
- Never ask the vision model to summarize; ask it to transcribe/describe, then interpret.
- Media stays on the local machine; never upload files.
- Config: copy `vision-config.example.json` to `vision-config.json` and fill in
  machine-specific paths, or use `VISION_*` environment variables. See README.md.
