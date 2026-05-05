"""Architectural invariant test (S11 / AC1).

Enforces NFR-MAINT-2 / AD-12: only ``hermes_icm_memory/cli_runner.py`` may
import ``subprocess``. Every other source file under ``hermes_icm_memory/``
must be ``subprocess``-free, so the architecture-v2 MCP swap (AD-D1) can
replace ``cli_runner.py`` wholesale without rippling through the package.

The walk uses ``ast`` (not regex) so it handles aliasing such as
``import subprocess as sp`` and ``from subprocess import run`` uniformly.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "hermes_icm_memory"
ALLOWED_FILENAME = "cli_runner.py"


def _imports_subprocess(source: str) -> bool:
    """Return True iff ``source`` imports the ``subprocess`` stdlib module."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" or alias.name.startswith("subprocess."):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            return True
    return False


def _iter_package_py_files() -> Iterable[Path]:
    yield from PACKAGE_ROOT.rglob("*.py")


def test_only_cli_runner_imports_subprocess() -> None:
    """No file under ``hermes_icm_memory/`` other than ``cli_runner.py`` imports subprocess."""
    offenders: list[str] = []
    for path in _iter_package_py_files():
        if path.name == ALLOWED_FILENAME:
            continue
        source = path.read_text(encoding="utf-8")
        if _imports_subprocess(source):
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))
    assert offenders == [], (
        "subprocess imported outside cli_runner.py — AD-12 violated. "
        f"Offending files: {offenders}"
    )


def test_ast_walker_detects_subprocess_imports() -> None:
    """Negative control: prove ``_imports_subprocess`` actually has teeth."""
    # Each variant the AST walker must catch.
    assert _imports_subprocess("import subprocess\n") is True
    assert _imports_subprocess("import subprocess as sp\n") is True
    assert _imports_subprocess("from subprocess import run\n") is True
    assert _imports_subprocess("from subprocess import run, PIPE\n") is True
    # Benign code must not trigger.
    assert _imports_subprocess("import os\n") is False
    assert _imports_subprocess("from os import path\n") is False
    assert _imports_subprocess("x = 'subprocess'  # string, not import\n") is False
