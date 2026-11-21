"""Keep the documented and served version aligned."""

import tomllib
from pathlib import Path

from main import app
from version import __version__


def test_version_matches_project_and_changelog() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert project["project"]["version"] == __version__
    assert app.version == __version__
    assert f"## [{__version__}]" in changelog
