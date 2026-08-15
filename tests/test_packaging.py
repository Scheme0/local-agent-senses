# -*- coding: utf-8 -*-
"""Packaging metadata tests: pyproject is the single version source and the
runtime reads it (installed metadata) instead of hardcoding copies."""
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import vision  # noqa: E402


def _field(text: str, key: str) -> str:
    m = re.search(rf"^{key}\s*=\s*\"([^\"]+)\"", text, re.MULTILINE)
    assert m, key
    return m.group(1)


def test_pyproject_metadata():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert _field(text, "name") == "local-agent-senses"
    assert _field(text, "version")  # pyproject.toml is the single version source
    assert _field(text, "requires-python") == ">=3.10"
    scripts = text.split("[project.scripts]")[1].split("[tool.setuptools]")[0]
    assert "vision = \"vision:main\"" in scripts
    assert "vision-mcp = \"extras.mcp_server:main\"" in scripts


def test_runtime_version_is_single_source():
    """CLI and MCP server must read the shared package version; none of them
    may hardcode a version string that can drift."""
    assert vision.__version__ == config.package_version()
    for rel in ("vision.py", "extras/mcp_server.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "package_version()" in src, rel


def test_server_version_reads_shared_source():
    spec = importlib.util.spec_from_file_location(
        "mcp_server_version_check", ROOT / "extras" / "mcp_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SERVER_VERSION == config.package_version()


def test_no_stale_version_references():
    for rel in ("README.md", "SKILL.md", "docs/COMPATIBILITY.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "0.7.0" not in text, rel
        assert "0.6." not in text, rel


def test_source_manifest_contains_release_docs():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "include README.md" in manifest
    assert "include README.zh-CN.md" in manifest
    assert "include LICENSE" in manifest
    assert (ROOT / "README.zh-CN.md").is_file()
    assert (ROOT / "vision-config.example.json").is_file()
