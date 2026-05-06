"""v0.3 invariant — ``cli_runner`` exposes only the CLI subprocess path (AC4, AC9).

The v0.2 MCP transport (``mcp_start`` / ``mcp_stop`` / ``_McpDaemon`` / etc.)
was deleted in v0.3 because hermes-agent v0.3.0+ owns the
``mcp_servers.icm:`` surface natively. Pins:

1. ``run_recall`` / ``run_store`` / ``run_topics`` / ``run_health`` carry
   no ``transport=`` parameter.
2. ``cli_runner.__all__`` does not advertise ``mcp_start`` / ``mcp_stop``.
3. The cli_runner module text contains no ``subprocess.Popen`` call site
   (only ``subprocess.run`` for one-shot invocations).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from hermes_icm_memory import cli_runner


def test_run_recall_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_recall)
    assert "transport" not in sig.parameters, (
        f"run_recall must not carry transport kwarg in v0.3; sig={sig}"
    )


def test_run_store_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_store)
    assert "transport" not in sig.parameters, (
        f"run_store must not carry transport kwarg in v0.3; sig={sig}"
    )


def test_run_topics_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_topics)
    assert "transport" not in sig.parameters, (
        f"run_topics must not carry transport kwarg in v0.3; sig={sig}"
    )


def test_run_health_has_no_transport_kwarg() -> None:
    sig = inspect.signature(cli_runner.run_health)
    assert "transport" not in sig.parameters, (
        f"run_health must not carry transport kwarg in v0.3; sig={sig}"
    )


def test_cli_runner_all_does_not_include_mcp_helpers() -> None:
    exported = set(cli_runner.__all__)
    assert "mcp_start" not in exported
    assert "mcp_stop" not in exported


def test_cli_runner_module_has_no_mcp_attributes() -> None:
    """No ``mcp_*`` / ``_mcp_*`` / ``_McpDaemon`` symbols leaked in.

    Catches a regression where someone re-introduces the daemon path under
    a slightly different name.
    """
    suspicious = [
        name
        for name in dir(cli_runner)
        if (
            name.startswith("mcp_")
            or name.startswith("_mcp_")
            or name.startswith("_McpDaemon")
            or name.startswith("_MCP_")
        )
    ]
    assert suspicious == [], (
        f"cli_runner must not carry MCP daemon symbols in v0.3; found: {suspicious}"
    )


def test_cli_runner_source_has_no_subprocess_popen() -> None:
    """Source text contains no ``subprocess.Popen`` call.

    AC4 invariant — the only subprocess primitive in v0.3 is
    ``subprocess.run`` (one-shot CLI invocations). ``Popen`` is what the
    deleted MCP daemon path used to keep an ``icm serve`` long-lived.
    """
    source = Path(cli_runner.__file__).read_text(encoding="utf-8")
    assert "subprocess.Popen" not in source, (
        "cli_runner.py must not call subprocess.Popen in v0.3 — only "
        "subprocess.run for one-shot CLI invocations"
    )
