"""Sole owner of every ``subprocess`` invocation against the ``icm`` binary.

Architecture compliance:

* AD-12 / NFR-MAINT-2 — ``cli_runner`` is the **only** module under
  ``hermes_icm_memory/`` that imports ``subprocess``. A future v2 may swap
  the internals (e.g. talk to ``icm serve`` over MCP) without touching
  ``provider.py`` / ``tools.py`` / ``hooks.py``.
* AD-19 / NFR-SEC-3 — list-form argv only; never ``shell=True``; user-supplied
  values (query, topic, content) flow as discrete argv elements.
* AD-08 / NFR-PERF-3 — every public ``run_*`` carries a ``timeout_ms``
  parameter that is forwarded to ``subprocess.run`` as ``timeout=ms / 1000``.
* AD-07 — ``cli_runner`` itself never degrades: it raises typed exceptions
  from ``errors.py``. Upstream callers (``tools.py`` / ``hooks.py``) catch
  ``ICMError`` and produce the documented degrade response.
* AD-13 / NFR-OBS-2 — every invocation logs at DEBUG with redacted argv and
  elapsed milliseconds via ``extra={...}`` (no f-string interpolation).

Manager directive (binding, verified on icm 0.10.43): ``--format json`` is
supported only on ``icm recall`` — NOT on ``icm topics`` or ``icm health``.
``run_topics`` / ``run_health`` therefore parse the aligned-table /
``key: value`` stdout into shapes equivalent to what JSON would have given.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess  # AD-12: ONLY import in the package.
import time
from pathlib import Path
from typing import Any

from .errors import (
    ICMMalformedOutputError,
    ICMNonZeroExitError,
    ICMNotFoundError,
    ICMTimeoutError,
)

__all__ = ["run_health", "run_recall", "run_store", "run_topics"]

logger = logging.getLogger("hermes_icm_memory.cli_runner")

# Redaction policy (AC5): truncate any argv token longer than this to its
# first (LIMIT - 1) chars + a single ellipsis, so the redacted token length
# stays bounded by LIMIT. The test ``test_debug_log_emits_redacted_argv``
# pins the ``len(tok) <= 80`` invariant.
_ARG_TRUNCATE_LIMIT = 80
_TRUNCATE_MARKER = "…"  # single-char horizontal ellipsis ("…")
_STDOUT_SNIPPET = 200

_COL_SPLIT = re.compile(r"\s{2,}")


def _redact_argv(argv: list[str]) -> list[str]:
    """Truncate any argv token longer than ``_ARG_TRUNCATE_LIMIT`` chars."""
    redacted: list[str] = []
    for token in argv:
        if len(token) > _ARG_TRUNCATE_LIMIT:
            redacted.append(token[: _ARG_TRUNCATE_LIMIT - 1] + _TRUNCATE_MARKER)
        else:
            redacted.append(token)
    return redacted


def _run(argv: list[str], timeout_ms: int) -> subprocess.CompletedProcess[str]:
    """Invoke ``icm`` and translate every documented failure into a typed error.

    Always emits one DEBUG log entry with the redacted argv and elapsed_ms.
    Raises ``ICMNotFoundError`` / ``ICMTimeoutError`` / ``ICMNonZeroExitError``
    from ``errors.py``; never catches ``Exception`` broadly.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_ms / 1000,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.debug(
            "icm invocation: not found",
            extra={"argv": _redact_argv(argv), "elapsed_ms": elapsed_ms},
        )
        raise ICMNotFoundError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.debug(
            "icm invocation: timeout",
            extra={"argv": _redact_argv(argv), "elapsed_ms": elapsed_ms},
        )
        raise ICMTimeoutError(str(exc)) from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.debug(
        "icm invocation: complete",
        extra={
            "argv": _redact_argv(argv),
            "elapsed_ms": elapsed_ms,
            "returncode": proc.returncode,
        },
    )
    if proc.returncode != 0:
        raise ICMNonZeroExitError(proc.stderr or f"icm exited with {proc.returncode}")
    return proc


def _db_args(db_path: Path | None) -> list[str]:
    """Return ``["--db", str(path)]`` when set, else ``[]`` (icm uses canonical default).

    Omitting ``--db`` lets icm pick its OS-default location (e.g. XDG-resolved
    ``~/.local/share/icm/memories.db`` on Linux), which is the same database
    Claude Code, Cursor, OpenCode, etc. share. Passing an explicit ``db_path``
    isolates the plugin into a separate file (opt-in profile isolation).
    """
    return ["--db", str(db_path)] if db_path is not None else []


def run_recall(
    query: str,
    limit: int,
    db_path: Path | None,
    timeout_ms: int,
    topic: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``icm recall`` and return the parsed JSON list of hits."""
    argv: list[str] = [
        "icm",
        *_db_args(db_path),
        "recall",
        query,
        "--limit",
        str(limit),
        "--format",
        "json",
    ]
    if topic is not None:
        argv += ["-t", topic]
    if project is not None:
        argv += ["-p", project]

    proc = _run(argv, timeout_ms)
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ICMMalformedOutputError(proc.stdout[:_STDOUT_SNIPPET]) from exc
    if not isinstance(parsed, list):
        raise ICMMalformedOutputError(proc.stdout[:_STDOUT_SNIPPET])
    return parsed


def run_store(
    topic: str,
    content: str,
    importance: str,
    db_path: Path | None,
    timeout_ms: int,
    keywords: str | None = None,
    raw: str | None = None,
) -> None:
    """Run ``icm store``. Stdout is discarded; only ``returncode`` matters."""
    argv: list[str] = [
        "icm",
        *_db_args(db_path),
        "store",
        "-t",
        topic,
        "-c",
        content,
        "-i",
        importance,
    ]
    if keywords is not None:
        argv += ["-k", keywords]
    if raw is not None:
        argv += ["-r", raw]
    _run(argv, timeout_ms)


def run_topics(db_path: Path, timeout_ms: int) -> list[dict[str, Any]]:
    """Run ``icm topics`` (no ``--format json`` — recall-only on icm 0.10.43).

    Parses the aligned-table stdout: column splits on two-or-more spaces,
    one row per line; the first row is treated as a header and used to key
    each row's columns. Single-column output falls back to
    ``[{"topic": <line>}, ...]``.
    """
    argv = ["icm", *_db_args(db_path), "topics"]
    proc = _run(argv, timeout_ms)
    return _parse_topics_table(proc.stdout)


def run_health(
    db_path: Path | None,
    timeout_ms: int,
    topic: str | None = None,
) -> dict[str, Any]:
    """Run ``icm health`` (no ``--format json`` — recall-only on icm 0.10.43).

    Parses ``key: value`` lines into a dict. ``-t topic`` narrows the scope.
    Raises ``ICMMalformedOutputError`` when non-blank stdout yields zero
    parseable lines.
    """
    argv: list[str] = ["icm", *_db_args(db_path), "health"]
    if topic is not None:
        argv += ["-t", topic]
    proc = _run(argv, timeout_ms)
    return _parse_health_kv(proc.stdout)


# ---------- private parsers ---------------------------------------------------


def _normalize_key(raw: str) -> str:
    """Lower-case + collapse whitespace runs to underscores for dict keys."""
    return re.sub(r"\s+", "_", raw.strip().lower())


def _parse_topics_table(stdout: str) -> list[dict[str, Any]]:
    """Convert ``icm topics`` aligned-table stdout into ``list[dict]``.

    The exact aligned-table format is speculative against icm 0.10.43;
    real-binary verification is S14 (integration tests). On unrecognized
    formats we degrade to the single-column fallback rather than raising.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return []

    header_cols = [_normalize_key(c) for c in _COL_SPLIT.split(lines[0].strip())]
    if len(header_cols) <= 1:
        return [{"topic": line.strip()} for line in lines]

    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        values = [v.strip() for v in _COL_SPLIT.split(line.strip())]
        rows.append(
            {header_cols[i]: values[i] for i in range(min(len(header_cols), len(values)))}
        )
    return rows


def _parse_health_kv(stdout: str) -> dict[str, Any]:
    """Convert ``icm health`` ``key: value`` line output into a dict."""
    result: dict[str, Any] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        result[_normalize_key(key)] = value.strip()
    if not result and stdout.strip():
        raise ICMMalformedOutputError(stdout[:_STDOUT_SNIPPET])
    return result
