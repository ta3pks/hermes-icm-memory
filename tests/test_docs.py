"""Documentation shape tests for S03 (README + CONTRIBUTING).

Pure read-and-assert tests. No subprocess, no network. Asserts the docs
contain the install commands, dev-loop instructions, and upstream links
mandated by the story spec (epics-and-stories.md §Story 1.3).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"


def test_readme_has_quickstart() -> None:
    text = README.read_text(encoding="utf-8")
    assert "## Quickstart" in text, "README must have a '## Quickstart' section"
    assert "pip install hermes-icm-memory" in text
    assert "hermes plugins enable hermes-icm-memory" in text
    assert "hermes memory setup icm" in text


def test_contributing_has_dev_loop() -> None:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "ruff check" in text, "CONTRIBUTING must mention 'ruff check'"
    assert "mypy" in text, "CONTRIBUTING must mention 'mypy'"
    assert "pytest" in text, "CONTRIBUTING must mention 'pytest'"
    assert "85" in text, "CONTRIBUTING must mention the 85% coverage threshold"


def test_readme_links_upstreams() -> None:
    text = README.read_text(encoding="utf-8")
    assert "https://github.com/rtk-ai/icm" in text
    assert "https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins" in text
