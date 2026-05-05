"""Architectural invariant test (S11 / AC2).

Enforces FR2 (path injection): no source file under ``hermes_icm_memory/``
may contain the literal string ``"~/.hermes"``. Paths must be derived from
``kwargs['hermes_home']`` (or its env-var fallback) so two Hermes profiles
cannot collide on a single hardcoded directory.

A simple text scan (rather than AST) is correct here because the literal
must be absent from comments, docstrings, and string literals alike.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "hermes_icm_memory"
FORBIDDEN_LITERAL = "~/.hermes"


def _iter_package_py_files() -> Iterable[Path]:
    yield from PACKAGE_ROOT.rglob("*.py")


def test_no_dotcache_literal_in_source() -> None:
    """The literal ``"~/.hermes"`` must not appear in any package source file."""
    offenders: list[str] = []
    for path in _iter_package_py_files():
        content = path.read_text(encoding="utf-8")
        if FORBIDDEN_LITERAL in content:
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))
    assert offenders == [], (
        f'Hardcoded "{FORBIDDEN_LITERAL}" found — FR2 violated. '
        "Derive the path from hermes_home (or HERMES_HOME env var) instead. "
        f"Offending files: {offenders}"
    )


def test_dotcache_scanner_detects_literal() -> None:
    """Negative control: prove the scan would catch a regression if introduced."""
    synthetic = 'DEFAULT_DB = Path("~/.hermes/icm/icm.db")\n'
    assert FORBIDDEN_LITERAL in synthetic
    benign = 'DEFAULT_DB = Path(os.environ["HERMES_HOME"]) / "icm" / "icm.db"\n'
    assert FORBIDDEN_LITERAL not in benign
