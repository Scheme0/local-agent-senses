# Security

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Instead, report
them privately so they can be fixed before disclosure:

- Open a GitHub private vulnerability report:
  **Security → Report a vulnerability** on this repository
  (preferred), or
- Send an email to the maintainer with enough detail to reproduce the issue
  (affected version, OS, steps, and any logs without secrets).

We aim to acknowledge reports within 7 days and to ship a fix as soon as the
impact is understood.

## What we already do

- **Local-first by default**: media is processed on the machine running the
  tool. No API keys are required in local mode and nothing is uploaded.
- **SSRF guard**: media URLs are restricted to public `http`/`https`.
  Loopback, link-local, private and cloud-metadata addresses are blocked
  before download, and every redirect hop is re-checked.
- **Secrets handling**: API keys are only read from environment variables or
  the local config file; `vision-config.json` and generated adapter files are
  gitignored. Never commit real keys.
- **Untrusted-media posture**: images/videos/web pages may contain injected
  instructions (for example "ignore previous instructions"). The tool's
  output must be treated as data, not as commands — this is enforced in the
  prompts and documented in `SKILL.md`.
- **No bundled binaries**: Ollama, ffmpeg, model weights and speech models are
  installed by the user; the repository ships no executables.

## Scope

The code in this repository. Model weights, Ollama, ffmpeg, FunASR, yt-dlp and
other third-party components are covered by their own projects' security
policies (see `THIRD_PARTY_NOTICES.md`).
