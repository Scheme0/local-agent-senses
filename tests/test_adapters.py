# -*- coding: utf-8 -*-
"""Adapter-generation and release-hygiene tests: no external services."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / ".tmp" / "adapters"
GENERATOR = ROOT / "scripts" / "generate_adapters.py"

ADAPTER_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".clinerules/vision.md",
    ".cursor/rules/vision.mdc",
    ".mcp.json",
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _generate_all():
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(GENERATOR), "--all", "--out", str(OUT), "--force"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_skill_frontmatter():
    text = _read("SKILL.md")
    assert text.startswith("---")
    head, _, _ = text.partition("---")[2].partition("---")
    assert "name: vision" in head
    assert "description:" in head
    assert "does not support image inputs" in head


def test_template_is_single_source():
    text = _read("scripts/agent-adapter.md")
    assert "vision.py" in text
    assert "--mode text --transcribe" in text
    assert "never upload" in text.lower()


def test_generator_creates_all_adapters():
    _generate_all()
    for rel in ADAPTER_FILES:
        path = OUT / rel
        assert path.exists(), rel
        text = path.read_text(encoding="utf-8")
        if rel == ".mcp.json":
            assert "mcpServers" in text
            assert "vision" in text
            assert "mcp_server.py" in text
        else:
            assert "vision.py" in text
            assert "--mode text --transcribe" in text


def test_mcp_json_points_to_current_python():
    _generate_all()
    import json

    data = json.loads((OUT / ".mcp.json").read_text(encoding="utf-8"))
    server = data["mcpServers"]["vision"]
    assert server["command"] == sys.executable
    assert server["args"][0].endswith("mcp_server.py")


def test_cursor_mdc_frontmatter():
    _generate_all()
    text = (OUT / ".cursor/rules/vision.mdc").read_text(encoding="utf-8")
    assert text.startswith("---")
    head = text.split("---", 2)[1]
    assert "description:" in head


def test_generator_skips_existing_without_force():
    _generate_all()
    before = (OUT / "CLAUDE.md").read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, str(GENERATOR), "--all", "--out", str(OUT)],
        check=True,
        capture_output=True,
        text=True,
    )
    after = (OUT / "CLAUDE.md").read_text(encoding="utf-8")
    assert before == after


def test_repo_tracks_no_static_adapters():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    for rel in ADAPTER_FILES:
        assert rel not in tracked, rel


def test_readme_is_bilingual_and_has_generator():
    readme = _read("README.md")
    assert "README.en.md" not in readme
    assert "## 中文" in readme
    assert "## English" in readme
    assert "MCP" in readme
    assert "generate_adapters" in readme
    assert "assets/demo.png" in readme


def test_config_api_base_env():
    old = os.environ.get("VISION_API_BASE")
    old_key = os.environ.get("VISION_API_KEY")
    os.environ["VISION_API_BASE"] = "http://127.0.0.1:9999/v1"
    os.environ["VISION_API_KEY"] = "sk-test"
    try:
        import config  # noqa: F401

        assert config.use_openai_api() is True
        assert config.api_base() == "http://127.0.0.1:9999/v1"
        assert config.api_key() == "sk-test"
    finally:
        if old is None:
            os.environ.pop("VISION_API_BASE", None)
        else:
            os.environ["VISION_API_BASE"] = old
        if old_key is None:
            os.environ.pop("VISION_API_KEY", None)
        else:
            os.environ["VISION_API_KEY"] = old_key
        config.user_config.cache_clear()
