# -*- coding: utf-8 -*-
"""Packaging metadata tests: keep pyproject and runtime version in sync."""
import re
import tarfile
import zipfile
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _field(text: str, key: str) -> str:
    m = re.search(rf"^{key}\s*=\s*\"([^\"]+)\"", text, re.MULTILINE)
    assert m, key
    return m.group(1)


def test_pyproject_metadata():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert _field(text, "name") == "local-agent-senses"
    assert _field(text, "version") == "0.2.0"
    assert _field(text, "requires-python") == ">=3.10"
    scripts = text.split("[project.scripts]")[1].split("[tool.setuptools]")[0]
    assert "vision = \"vision:main\"" in scripts
    assert "vision-mcp = \"extras.mcp_server:main\"" in scripts


def test_server_version_matches_pyproject():
    src = (ROOT / "extras" / "mcp_server.py").read_text(encoding="utf-8")
    m = re.search(r'SERVER_VERSION = "([^"]+)"', src)
    assert m and m.group(1) == "0.2.0"


def test_cli_version_matches_pyproject():
    src = (ROOT / "vision.py").read_text(encoding="utf-8")
    m = re.search(r'__version__ = "([^"]+)"', src)
    assert m and m.group(1) == "0.2.0"


def test_no_stale_version_references():
    for rel in ("README.md", "SKILL.md", "docs/COMPATIBILITY.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "0.7.0" not in text, rel
        assert "0.6." not in text, rel


def test_source_archive_contains_release_docs():
    dist = Path(tempfile.gettempdir()) / "local-agent-senses-dist"
    sdists = sorted(dist.glob("local_agent_senses-*.tar.gz"))
    wheels = sorted(dist.glob("local_agent_senses-*.whl"))
    assert sdists and wheels
    try:
        with tarfile.open(sdists[-1]) as archive:
            names = archive.getnames()
    except PermissionError:
        pytest.skip("existing dist directory is not readable in this environment")
    assert any(name.endswith("/README.zh-CN.md") for name in names)
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = archive.namelist()
    assert any(name.endswith("/vision-config.example.json") for name in names)
