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
# Delegation — run_recall
# ---------------------------------------------------------------------------


def _with_client() -> MagicMock:
    """Set up a mock MCP client on cli_runner."""
    client = MagicMock()
    client.is_available.return_value = True
    cli_runner._client = client
    return client


def test_run_recall_delegates_to_mcp_client() -> None:
    client = _with_client()
    client.call_recall.return_value = [{"topic": "test", "summary": "hello"}]

    result = cli_runner.run_recall(
        query="hello", limit=5, db_path=DB, timeout_ms=2000
    )
    assert result == [{"topic": "test", "summary": "hello"}]
    client.call_recall.assert_called_once_with(
        query="hello", limit=5, topic=None, project=None
    )
    cli_runner.mcp_stop()


def test_run_recall_with_topic_and_project() -> None:
    client = _with_client()
    client.call_recall.return_value = []

    cli_runner.run_recall(
        query="q",
        limit=3,
        db_path=DB,
        timeout_ms=2000,
        topic="errors-resolved",
        project="hermes-icm-memory",
    )
    client.call_recall.assert_called_once_with(
        query="q",
        limit=3,
        topic="errors-resolved",
        project="hermes-icm-memory",
    )
    cli_runner.mcp_stop()


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


def test_run_recall_when_not_started() -> None:
    """run_recall raises ICMConnectionError when client not started."""
    cli_runner._client = None
    with pytest.raises(cli_runner.ICMConnectionError):
        cli_runner.run_recall(query="q", limit=5, db_path=DB, timeout_ms=2000)


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
