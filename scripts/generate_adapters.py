#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate per-agent adapter files from a single template.

The repo deliberately ships no AGENTS.md / CLAUDE.md / GEMINI.md /
.clinerules / .cursor files: five near-identical copies in-tree would drift
apart and make the repository look bloated. Instead, this script writes the
adapter(s) for the agents detected on the current machine (or an explicit
selection), so every user gets exactly the file(s) their tooling reads.

Examples:
  python scripts/generate_adapters.py                  # auto-detect installed agents
  python scripts/generate_adapters.py --all            # every known tool
  python scripts/generate_adapters.py --agents claude,cursor
  python scripts/generate_adapters.py --agents mcp     # write .mcp.json for MCP clients
  python scripts/generate_adapters.py --out /path/to/project
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "agent-adapter.md"

CURSOR_FRONTMATTER = (
    "---\n"
    "description: Local multimodal vision & speech via vision.py "
    "(OCR, image/video understanding, ASR). Use when the user asks to read, "
    "analyze, transcribe, or summarize images, screenshots, charts, documents, "
    "videos, subtitles, audio, or media URLs, or when the model cannot natively "
    "see images.\n"
    "globs:\n"
    "---\n\n"
)

# agent name -> relative output path
ADAPTERS = {
    "codex": "AGENTS.md",
    "claude": "CLAUDE.md",
    "gemini": "GEMINI.md",
    "cline": ".clinerules/vision.md",
    "cursor": ".cursor/rules/vision.mdc",
    "mcp": ".mcp.json",
}


def detect_agents() -> list[str]:
    """Return the names of agent tools detected on this machine."""
    home = Path.home()
    found = []
    if shutil.which("codex") or (home / ".codex").exists():
        found.append("codex")
    if shutil.which("claude") or (home / ".claude").exists():
        found.append("claude")
    if shutil.which("gemini") or (home / ".gemini").exists():
        found.append("gemini")
    if shutil.which("cline") or (home / ".cline").exists():
        found.append("cline")
    cursor_hint = bool(shutil.which("cursor") or (home / ".cursor").exists())
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        cursor_hint = cursor_hint or (local / "Programs" / "Cursor" / "Cursor.exe").exists()
    if cursor_hint:
        found.append("cursor")
    return found


def build_content(agent: str) -> str:
    if agent == "mcp":
        return build_mcp_json()
    body = TEMPLATE.read_text(encoding="utf-8").rstrip() + "\n"
    if agent == "cursor":
        return CURSOR_FRONTMATTER + body
    return body


def build_mcp_json() -> str:
    """Minimal stdio MCP config that works in Claude Code / Cursor / Windsurf
    and most other MCP-capable clients."""
    payload = {
        "mcpServers": {
            "vision": {
                "command": sys.executable,
                "args": [str(ROOT / "extras" / "mcp_server.py")],
            }
        }
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def generate(agents: list[str], out_dir: Path, force: bool = False):
    written: list[str] = []
    skipped: list[str] = []
    for agent in agents:
        rel = ADAPTERS[agent]
        target = out_dir / rel
        if target.exists() and not force:
            skipped.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(build_content(agent), encoding="utf-8")
        written.append(rel)
    return written, skipped


def parse_agents(value: str) -> list[str]:
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    unknown = [name for name in names if name not in ADAPTERS]
    if unknown:
        raise SystemExit(
            "unknown agent(s): %s (known: %s)"
            % (", ".join(unknown), ", ".join(sorted(ADAPTERS)))
        )
    return names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate agent adapter files (AGENTS.md / CLAUDE.md / "
        "GEMINI.md / .clinerules / .cursor / .mcp.json) from "
        "scripts/agent-adapter.md."
    )
    parser.add_argument("--out", default=".", help="target directory (default: current dir)")
    parser.add_argument(
        "--agents",
        help="comma-separated subset: codex,claude,gemini,cline,cursor,mcp "
        "(default: auto-detect installed agents)",
    )
    parser.add_argument("--all", action="store_true",
                        help="generate adapters for every known tool (incl. .mcp.json)")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)

    if args.all:
        agents = list(ADAPTERS)
    elif args.agents:
        agents = parse_agents(args.agents)
    else:
        agents = detect_agents()
        if not agents:
            print(
                "No known agent tools detected. Re-run with --all or "
                "--agents codex,claude,gemini,cline,cursor,mcp."
            )
            return 0

    written, skipped = generate(agents, Path(args.out), force=args.force)
    if written:
        print("Generated (%s): %s" % (args.out, ", ".join(written)))
    if skipped:
        print("Skipped (already exist; use --force to overwrite): %s" % ", ".join(skipped))
    if not written and not skipped:
        print("Nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
