"""Unit tests for ``hermes_icm_memory.cli_runner`` (S04, Story 2.1).

Every test patches ``hermes_icm_memory.cli_runner.subprocess.run`` — the real
``icm`` binary is never invoked. Mirrors the 13-test plan in
``_bmad-output/implementation-artifacts/2-1-typed-errors-and-cli-runner.md``.

Manager directive (binding): ``--format json`` is supported only on
``icm recall`` (verified on icm 0.10.43); ``run_topics`` / ``run_health``
parse aligned-table / ``key: value`` stdout instead.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.errors import (
    ICMMalformedOutputError,
    ICMNonZeroExitError,
    ICMNotFoundError,
    ICMTimeoutError,
)

DB = Path("/tmp/hermes-test/icm/default.db")
RUN_TARGET = "hermes_icm_memory.cli_runner.subprocess.run"


def _ok(stdout: str = "[]", stderr: str = "", returncode: int = 0) -> MagicMock:
    """Build a MagicMock standing in for a ``CompletedProcess``."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# AC1 — argv shape + parsed return value for run_recall
# ---------------------------------------------------------------------------


def test_run_recall_argv_shape_default() -> None:
    """argv has the locked default shape when topic and project are None.

    v0.1.1: ``use_embeddings`` defaults to ``False`` so ``--no-embeddings`` is
    appended to keep Pi-class hot-path recalls instant (model load is otherwise
    ~50 s per subprocess).
    """
    with patch(RUN_TARGET, return_value=_ok("[]")) as run:
        cli_runner.run_recall(query="hello", limit=5, db_path=DB, timeout_ms=2000)
    argv = run.call_args.args[0]
    assert argv == [
        "icm",
        "--db",
        str(DB),
        "recall",
        "hello",
        "--limit",
        "5",
        "--format",
        "json",
        "--no-embeddings",
    ]


def test_run_recall_argv_shape_with_topic_and_project() -> None:
    """Optional ``-t`` and ``-p`` are appended in that order when supplied."""
    with patch(RUN_TARGET, return_value=_ok("[]")) as run:
        cli_runner.run_recall(
            query="q",
            limit=3,
            db_path=DB,
            timeout_ms=2000,
            topic="errors-resolved",
            project="hermes-icm-memory",
        )
    argv = run.call_args.args[0]
    assert argv[-4:] == ["-t", "errors-resolved", "-p", "hermes-icm-memory"]


def test_run_recall_argv_with_use_embeddings_true_omits_flag() -> None:
    """v0.1.1 — ``use_embeddings=True`` removes the ``--no-embeddings`` flag.

    Lets the user opt back into icm's configured embedding model when they're
    on hardware that can sustain the load (or once v0.2's ``icm-serve`` MCP
    transport amortizes the model warm-up).
    """
    with patch(RUN_TARGET, return_value=_ok("[]")) as run:
        cli_runner.run_recall(
            query="hello",
            limit=5,
            db_path=DB,
            timeout_ms=2000,
            use_embeddings=True,
        )
    argv = run.call_args.args[0]
    assert "--no-embeddings" not in argv
    assert argv == [
        "icm",
        "--db",
        str(DB),
        "recall",
        "hello",
        "--limit",
        "5",
        "--format",
        "json",
    ]


def test_run_recall_argv_with_db_path_none_omits_db_flag() -> None:
    """v0.1.1 — ``db_path=None`` omits ``--db`` so icm uses its canonical default.

    Implements the brief's "shared with editors, not a parallel silo" promise:
    when the plugin runs in default-shared mode, ``cli_runner`` lets icm pick
    its OS-default DB (e.g. ``~/.local/share/icm/memories.db`` on Linux).
    """
    with patch(RUN_TARGET, return_value=_ok("[]")) as run:
        cli_runner.run_recall(query="x", limit=5, db_path=None, timeout_ms=2000)
    argv = run.call_args.args[0]
    assert "--db" not in argv
    # First two argv elements are the binary name and the subcommand.
    assert argv[0] == "icm"
    assert argv[1] == "recall"


def test_run_recall_returns_parsed_list() -> None:
    """Successful JSON-list stdout is parsed into a Python list."""
    with patch(RUN_TARGET, return_value=_ok('[{"id":"x"}]')):
        result = cli_runner.run_recall(query="q", limit=5, db_path=DB, timeout_ms=2000)
    assert result == [{"id": "x"}]


# ---------------------------------------------------------------------------
# AC2 — typed exceptions for every failure mode
# ---------------------------------------------------------------------------


def test_run_recall_raises_not_found() -> None:
    """``FileNotFoundError`` from subprocess.run translates to ``ICMNotFoundError``."""
    with patch(RUN_TARGET, side_effect=FileNotFoundError("icm")), pytest.raises(ICMNotFoundError):
        cli_runner.run_recall(query="q", limit=5, db_path=DB, timeout_ms=2000)


def test_run_recall_raises_timeout() -> None:
    """``TimeoutExpired`` from subprocess.run translates to ``ICMTimeoutError``."""
    exc = subprocess.TimeoutExpired(cmd=["icm"], timeout=2.0)
    with patch(RUN_TARGET, side_effect=exc), pytest.raises(ICMTimeoutError):
        cli_runner.run_recall(query="q", limit=5, db_path=DB, timeout_ms=2000)


def test_run_recall_raises_nonzero() -> None:
    """Non-zero returncode raises ``ICMNonZeroExitError`` with stderr in the message."""
    mock = _ok(stdout="", stderr="boom: db locked", returncode=2)
    with patch(RUN_TARGET, return_value=mock), pytest.raises(ICMNonZeroExitError) as ei:
        cli_runner.run_recall(query="q", limit=5, db_path=DB, timeout_ms=2000)
    assert "boom: db locked" in str(ei.value)


def test_run_recall_raises_malformed() -> None:
    """Stdout that isn't valid JSON raises ``ICMMalformedOutputError`` with first 200 chars."""
    payload = "not json " + "x" * 500
    with patch(RUN_TARGET, return_value=_ok(payload)), pytest.raises(ICMMalformedOutputError) as ei:
        cli_runner.run_recall(query="q", limit=5, db_path=DB, timeout_ms=2000)
    msg = str(ei.value)
    assert payload[:200] in msg
    # And nothing past 200 chars of the original leaks into the message.
    assert payload[201:] not in msg


# ---------------------------------------------------------------------------
# AC3 — run_store argv shape + stdout-ignored
# ---------------------------------------------------------------------------


def test_run_store_argv_shape() -> None:
    """argv contains store/-t/-c/-i; -k and -r appear only when supplied, in order."""
    with patch(RUN_TARGET, return_value=_ok("ok-ignored")) as run:
        cli_runner.run_store(
            topic="decisions-hermes-icm-memory",
            content="we decided to ship",
            importance="high",
            db_path=DB,
            timeout_ms=5000,
        )
    argv = run.call_args.args[0]
    assert argv == [
        "icm",
        "--db",
        str(DB),
        "store",
        "-t",
        "decisions-hermes-icm-memory",
        "-c",
        "we decided to ship",
        "-i",
        "high",
    ]

    with patch(RUN_TARGET, return_value=_ok("ok-ignored")) as run:
        cli_runner.run_store(
            topic="t",
            content="c",
            importance="high",
            db_path=DB,
            timeout_ms=5000,
            keywords="a,b,c",
            raw="orig text",
        )
    argv = run.call_args.args[0]
    assert argv[-4:] == ["-k", "a,b,c", "-r", "orig text"]


def test_run_store_does_not_parse_stdout() -> None:
    """run_store ignores stdout content; returns None when returncode is zero."""
    with patch(RUN_TARGET, return_value=_ok("garbage\x00not json")):
        cli_runner.run_store(
            topic="t",
            content="c",
            importance="high",
            db_path=DB,
            timeout_ms=5000,
        )
    # No assertion needed beyond reaching here without raising; the return is
    # typed ``None`` so we don't bind it (mypy --strict catches that).


# ---------------------------------------------------------------------------
# AC4 — run_topics + run_health (line-split parsers; NO --format json)
# ---------------------------------------------------------------------------


def test_run_topics_argv_and_parse() -> None:
    """argv has 'topics' (no --format json); aligned-table stdout → list[dict]."""
    stdout = "Topic            Count\nerrors-resolved  3\ndecisions-x      7\n"
    with patch(RUN_TARGET, return_value=_ok(stdout)) as run:
        result = cli_runner.run_topics(db_path=DB, timeout_ms=2000)
    argv = run.call_args.args[0]
    assert argv == ["icm", "--db", str(DB), "topics"]
    assert "--format" not in argv
    assert {"topic": "errors-resolved", "count": "3"} in result
    assert {"topic": "decisions-x", "count": "7"} in result


def test_run_health_argv_with_topic() -> None:
    """argv ends with -t <topic>; key:value stdout → dict (no --format json)."""
    stdout = "Total memories: 42\nStale: 0\nLast consolidation: 2026-05-05\n"
    with patch(RUN_TARGET, return_value=_ok(stdout)) as run:
        result = cli_runner.run_health(
            db_path=DB,
            timeout_ms=2000,
            topic="errors-resolved",
        )
    argv = run.call_args.args[0]
    assert argv == ["icm", "--db", str(DB), "health", "-t", "errors-resolved"]
    assert "--format" not in argv
    assert result["total_memories"] == "42"
    assert result["stale"] == "0"
    assert result["last_consolidation"] == "2026-05-05"


# ---------------------------------------------------------------------------
# AC5 — DEBUG log emits redacted argv + elapsed_ms
# ---------------------------------------------------------------------------


def test_debug_log_emits_redacted_argv(caplog: pytest.LogCaptureFixture) -> None:
    """Long argv tokens (>80 chars) are truncated; record carries argv + elapsed_ms."""
    long_query = "q" * 200
    with (
        caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.cli_runner"),
        patch(RUN_TARGET, return_value=_ok("[]")),
    ):
        cli_runner.run_recall(query=long_query, limit=5, db_path=DB, timeout_ms=2000)

    matching = [
        r
        for r in caplog.records
        if r.name == "hermes_icm_memory.cli_runner" and r.levelno == logging.DEBUG
    ]
    assert matching, "expected at least one DEBUG record from cli_runner"

    record = matching[-1]
    argv = record.__dict__.get("argv")
    elapsed_ms = record.__dict__.get("elapsed_ms")
    assert isinstance(argv, list), "DEBUG record must carry the argv list in 'extra'"
    assert isinstance(elapsed_ms, int), "DEBUG record must carry int elapsed_ms in 'extra'"
    # No token in the redacted argv exceeds the 80-char ceiling.
    assert all(len(tok) <= 80 for tok in argv), f"argv contained an unredacted long token: {argv}"
    # The 200-char query token must not appear verbatim.
    assert long_query not in argv


# ---------------------------------------------------------------------------
# AC6 — locked subprocess.run kwargs
# ---------------------------------------------------------------------------


def test_subprocess_invoked_with_shell_false_and_timeout() -> None:
    """Every subprocess.run call uses the locked NFR-SEC-3 / NFR-PERF-3 kwargs."""
    with patch(RUN_TARGET, return_value=_ok("[]")) as run:
        cli_runner.run_recall(query="q", limit=5, db_path=DB, timeout_ms=5000)
    kwargs = run.call_args.kwargs
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == pytest.approx(5.0)
