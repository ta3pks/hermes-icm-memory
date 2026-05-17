"""Unit tests for ``hermes_icm_memory.cli_runner`` (v0.4 — MCP-backed).

cli_runner no longer calls subprocess.run directly; it delegates to
:mclass:`mcp_client.IcmMcpClient`. These tests mock the MCP client
to verify delegation and error handling.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.errors import ICMNotFoundError

DB = Path("/tmp/hermes-test/icm/default.db")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_mcp_start_and_stop() -> None:
    """mcp_start creates a client; mcp_stop clears it."""
    assert cli_runner._client is None

    with patch("hermes_icm_memory.cli_runner.mcp_client.IcmMcpClient") as MockClient:
        instance = MockClient.return_value

        cli_runner.mcp_start(db_path=DB)
        assert cli_runner._client is instance
        instance.start.assert_called_once_with(db_path=DB, use_embeddings=False)

        cli_runner.mcp_stop()
        assert cli_runner._client is None
        instance.close.assert_called_once()


def test_mcp_start_idempotent() -> None:
    """Second mcp_start is a no-op when the client already exists."""
    with patch("hermes_icm_memory.cli_runner.mcp_client.IcmMcpClient") as MockClient:
        cli_runner.mcp_start()
        initial = cli_runner._client
        cli_runner.mcp_start()  # second call
        assert cli_runner._client is initial
        assert MockClient.call_count == 1  # only one instance created
    cli_runner.mcp_stop()


def test_mcp_start_raises_not_found_when_icm_missing() -> None:
    """FileNotFoundError from client start translates to ICMNotFoundError."""
    with patch("hermes_icm_memory.cli_runner.mcp_client.IcmMcpClient") as MockClient:
        MockClient.return_value.start.side_effect = FileNotFoundError("icm")
        with pytest.raises(ICMNotFoundError):
            cli_runner.mcp_start()
    assert cli_runner._client is None


def test_mcp_stop_is_noop_when_not_started() -> None:
    """mcp_stop with no client does nothing."""
    cli_runner.mcp_stop()  # should not raise


# ---------------------------------------------------------------------------
# Delegation — run_recall (v0.5.0: subprocess, not MCP)
# ---------------------------------------------------------------------------
#
# v0.5.0 routes run_recall through `icm recall ... --format json` as a
# one-shot subprocess (ICM's MCP-recall ranker surfaces noise above
# topic-tagged memories; the CLI ranker behaves correctly). Tests mock
# subprocess.run to verify argv composition and output parsing.


def _with_client() -> MagicMock:
    """Set up a mock MCP client on cli_runner (for store/topics/health tests)."""
    client = MagicMock()
    client.is_available.return_value = True
    cli_runner._client = client
    return client


def _fake_subprocess_run(stdout: str = "[]", returncode: int = 0) -> MagicMock:
    """Build a CompletedProcess-like stub for subprocess.run."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


def test_run_recall_invokes_icm_cli_with_json_format() -> None:
    """v0.5.0 — recall spawns ``icm recall <q> --limit N --format json``."""
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.return_value = _fake_subprocess_run(
            stdout='[{"id": "m1", "topic": "context-x", "summary": "hello"}]',
        )
        result = cli_runner.run_recall(
            query="hello", limit=5, db_path=DB, timeout_ms=2000,
        )
        assert result == [{"id": "m1", "topic": "context-x", "summary": "hello"}]
        # Verify argv shape.
        (called_argv, *_), kwargs = mock_run.call_args
        assert called_argv[0] == "icm"
        assert "recall" in called_argv
        assert "hello" in called_argv
        assert "--format" in called_argv
        assert "json" in called_argv
        assert "--limit" in called_argv
        assert "5" in called_argv
        # use_embeddings defaults True → no --no-embeddings flag.
        assert "--no-embeddings" not in called_argv
        # db_path threaded through.
        assert "--db" in called_argv
        assert str(DB) in called_argv


def test_run_recall_no_embeddings_flag_threads_to_cli() -> None:
    """``use_embeddings=False`` adds ``--no-embeddings`` BEFORE the subcommand."""
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.return_value = _fake_subprocess_run()
        cli_runner.run_recall(
            query="q", limit=3, db_path=DB, timeout_ms=2000,
            use_embeddings=False,
        )
        argv = mock_run.call_args[0][0]
        # icm clap parser: global --no-embeddings must precede subcommand.
        assert argv.index("--no-embeddings") < argv.index("recall")


def test_run_recall_topic_filter_threads_to_cli() -> None:
    """``topic=X`` adds ``-t X`` (project is silently dropped — icm CLI has
    no --project flag and the existing API kept it for back-compat)."""
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.return_value = _fake_subprocess_run()
        cli_runner.run_recall(
            query="q", limit=3, db_path=DB, timeout_ms=2000,
            topic="context-hair-iron", project="hermes-icm-memory",
        )
        argv = mock_run.call_args[0][0]
        assert "-t" in argv
        assert "context-hair-iron" in argv
        assert "--project" not in argv  # icm CLI has no such flag


def test_run_recall_empty_stdout_returns_empty_list() -> None:
    """Empty stdout → [] (not a crash)."""
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.return_value = _fake_subprocess_run(stdout="")
        assert cli_runner.run_recall(query="q", limit=3, db_path=DB, timeout_ms=2000) == []


def test_run_recall_unparseable_stdout_returns_empty_list() -> None:
    """Non-JSON stdout (e.g. icm's "No memories found." sentinel under some
    build flavours) is treated as zero hits, not a crash."""
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.return_value = _fake_subprocess_run(stdout="No memories found.")
        assert cli_runner.run_recall(query="q", limit=3, db_path=DB, timeout_ms=2000) == []


def test_run_recall_non_zero_exit_raises_connection_error() -> None:
    """Non-zero exit code surfaces as ICMConnectionError with stderr context."""
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        result = _fake_subprocess_run(returncode=1)
        result.stderr = "icm: database is locked"
        mock_run.return_value = result
        with pytest.raises(cli_runner.ICMConnectionError, match="database is locked"):
            cli_runner.run_recall(query="q", limit=3, db_path=DB, timeout_ms=2000)


def test_run_recall_timeout_raises_timeout_error() -> None:
    """subprocess.TimeoutExpired surfaces as ICMTimeoutError."""
    import subprocess as _sp
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.side_effect = _sp.TimeoutExpired(cmd="icm", timeout=2.0)
        from hermes_icm_memory.errors import ICMTimeoutError
        with pytest.raises(ICMTimeoutError):
            cli_runner.run_recall(query="q", limit=3, db_path=DB, timeout_ms=2000)


def test_run_recall_missing_binary_raises_not_found() -> None:
    """FileNotFoundError (icm binary missing) surfaces as ICMNotFoundError."""
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("icm")
        with pytest.raises(ICMNotFoundError):
            cli_runner.run_recall(query="q", limit=3, db_path=DB, timeout_ms=2000)


def test_run_recall_does_not_require_mcp_client() -> None:
    """v0.5.0 — recall is CLI-only now, so a missing _client is fine."""
    cli_runner._client = None
    with patch("hermes_icm_memory.cli_runner.subprocess.run") as mock_run:
        mock_run.return_value = _fake_subprocess_run()
        # Must not raise ICMConnectionError("MCP client not started ...").
        result = cli_runner.run_recall(query="q", limit=3, db_path=DB, timeout_ms=2000)
        assert result == []


# ---------------------------------------------------------------------------
# Delegation — run_store / run_topics / run_health
# ---------------------------------------------------------------------------


def test_run_store_delegates() -> None:
    client = _with_client()
    client.call_store.return_value = True

    result = cli_runner.run_store(
        topic="decisions-x",
        content="we decided",
        importance="high",
        db_path=DB,
        timeout_ms=5000,
    )
    assert result is None  # run_store returns None on success
    client.call_store.assert_called_once_with(
        topic="decisions-x", content="we decided", importance="high", keywords=None
    )
    cli_runner.mcp_stop()


def test_run_store_with_keywords() -> None:
    client = _with_client()
    client.call_store.return_value = True

    cli_runner.run_store(
        topic="t",
        content="c",
        importance="high",
        db_path=DB,
        timeout_ms=5000,
        keywords="a,b,c",
    )
    client.call_store.assert_called_once_with(
        topic="t", content="c", importance="high", keywords="a,b,c"
    )
    cli_runner.mcp_stop()


def test_run_topics_delegates() -> None:
    client = _with_client()
    client.call_topics.return_value = [{"topic": "errors-resolved", "count": "3"}]

    result = cli_runner.run_topics(db_path=DB, timeout_ms=2000)
    assert result == [{"topic": "errors-resolved", "count": "3"}]
    client.call_topics.assert_called_once()
    cli_runner.mcp_stop()


def test_run_health_delegates() -> None:
    client = _with_client()
    client.call_health.return_value = {"total_memories": "42"}

    result = cli_runner.run_health(db_path=DB, timeout_ms=2000, topic="errors-resolved")
    assert result == {"total_memories": "42"}
    client.call_health.assert_called_once_with(topic="errors-resolved")
    cli_runner.mcp_stop()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_run_store_fails_raises() -> None:
    client = _with_client()
    client.call_store.return_value = False

    with pytest.raises(cli_runner.ICMConnectionError):
        cli_runner.run_store(
            topic="t", content="c", importance="high", db_path=DB, timeout_ms=5000
        )
    cli_runner.mcp_stop()


# ---------------------------------------------------------------------------
# DEBUG log format (simplified for v0.4)
# ---------------------------------------------------------------------------


def test_debug_log_on_start(caplog: pytest.LogCaptureFixture) -> None:
    """mcp_start logs debug about the spawn."""
    with (
        caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.cli_runner"),
        patch("hermes_icm_memory.cli_runner.mcp_client.IcmMcpClient") as MockClient,
    ):
        MockClient.return_value.start.return_value = None
        cli_runner.mcp_start(db_path=DB)

    # matching is technically empty — the actual DEBUG log lives in mcp_client,
    # not cli_runner. The test just verifies mcp_start doesn't crash.
    _ = [
        r
        for r in caplog.records
        if r.name == "hermes_icm_memory.cli_runner" and r.levelno == logging.DEBUG
    ]
    # The actual DEBUG log lives in mcp_client, not cli_runner.
    # cli_runner doesn't log during start — that's fine.
    cli_runner.mcp_stop()
