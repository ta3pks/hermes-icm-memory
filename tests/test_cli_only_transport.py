"""v0.4 invariants — ``cli_runner`` delegates to the MCP daemon.

The v0.3 CLI-only mode has been replaced by a warm MCP daemon via
``mcp_client.IcmMcpClient``. Pins:

1. ``run_recall`` / ``run_store`` / ``run_topics`` / ``run_health`` carry
   no ``transport=`` parameter (same as v0.3 — the transport is internal).
2. ``cli_runner.__all__`` exports ``mcp_start`` / ``mcp_stop`` for lifecycle
   management.
3. ``cli_runner.py`` no longer calls ``subprocess.Popen`` or ``subprocess.run``
   directly — that's delegated to ``mcp_client.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from hermes_icm_memory import cli_runner


def test_run_recall_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_recall)
    assert "transport" not in sig.parameters, (
        f"run_recall must not carry transport kwarg; sig={sig}"
    )


def test_run_store_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_store)
    assert "transport" not in sig.parameters, (
        f"run_store must not carry transport kwarg; sig={sig}"
    )


def test_run_topics_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_topics)
    assert "transport" not in sig.parameters, (
        f"run_topics must not carry transport kwarg; sig={sig}"
    )


def test_run_health_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_health)
    assert "transport" not in sig.parameters, (
        f"run_health must not carry transport kwarg; sig={sig}"
    )


def test_cli_runner_all_exports_mcp_lifecycle() -> None:
    """v0.4 — cli_runner exports mcp_start/mcp_stop for lifecycle management."""
    exported = set(cli_runner.__all__)
    assert "mcp_start" in exported, "mcp_start must be exported in v0.4"
    assert "mcp_stop" in exported, "mcp_stop must be exported in v0.4"
    assert "run_recall" in exported
    assert "run_store" in exported
    assert "run_topics" in exported
    assert "run_health" in exported


def test_cli_runner_subprocess_use_is_scoped_to_recall() -> None:
    """v0.5.0 — cli_runner.run_recall calls subprocess.run directly; the
    other public helpers (run_store/topics/health) still delegate to the
    warm MCP daemon. v0.4 banned subprocess from cli_runner entirely, but
    ICM's MCP-served recall ranker surfaces empty-topic memoir blobs above
    topic-tagged memories (the CLI ranker is correct), so recall was
    reverted to the subprocess path.

    Pin the scope: subprocess.run must appear EXACTLY ONCE in cli_runner.py.
    If a future change broadens subprocess use back to store/topics/health,
    that's a deliberate decision that should update this test consciously.
    """
    source = Path(cli_runner.__file__).read_text(encoding="utf-8")
    assert source.count("subprocess.run(") == 1, (
        "expected exactly one subprocess.run call in cli_runner "
        f"(in run_recall); found {source.count('subprocess.run(')}"
    )
