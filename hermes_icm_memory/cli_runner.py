"""Sole owner of every ``subprocess`` invocation against the ``icm`` binary.

Architecture compliance:

* AD-12 / NFR-MAINT-2 — ``cli_runner`` (and ``mcp_client.py``) are the **only**
  modules under ``hermes_icm_memory/`` that import ``subprocess``.
* AD-19 / NFR-SEC-3 — list-form argv only; never ``shell=True``.
* AD-08 / NFR-PERF-3 — every public ``run_*`` carries a ``timeout_ms``
  parameter.
* AD-07 — ``cli_runner`` itself never degrades: it raises typed exceptions
  from ``errors.py``. Upstream callers (``hooks.py`` / ``provider.py``) catch
  ``ICMError`` and produce the documented degrade response.

v0.4 — The internal subprocess transport has been replaced with a warm MCP
daemon (:class:`mcp_client.IcmMcpClient`). The public ``run_*`` surface is
preserved so ``hooks.py`` and ``provider.py`` require no changes.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from . import mcp_client
from .errors import ICMConnectionError, ICMNotFoundError, ICMTimeoutError

__all__ = [
    "mcp_start",
    "mcp_stop",
    "run_health",
    "run_recall",
    "run_store",
    "run_topics",
]

logger = logging.getLogger(__name__)

#: Module-level MCP daemon shared across all ``run_*`` calls.
#: Lazy-started by ``mcp_start``; torn down by ``mcp_stop``.
_client: mcp_client.IcmMcpClient | None = None


# ------------------------------------------------------------------ lifecycle


def mcp_start(
    db_path: Path | None = None,
    use_embeddings: bool = False,
) -> None:
    """Start (or ensure running) the MCP daemon.

    Module-level singleton: the first call spawns ``icm serve``; subsequent
    calls are no-ops. Raises ``ICMNotFoundError`` if ``icm`` is missing,
    or ``ICMConnectionError`` if the MCP handshake fails.

    v0.4.4 — emits an INFO log on actual spawn (with subprocess pid when
    available) so the gateway log shows definitively whether the warm
    daemon ever came up. Pre-v0.4.4 a silent ``_client is None`` was
    indistinguishable from "never tried"; the daemon-PID stamp closes
    that ambiguity for future post-mortems.
    """
    global _client  # noqa: PLW0603
    if _client is not None:
        return
    try:
        cl = mcp_client.IcmMcpClient()
        cl.start(db_path=db_path, use_embeddings=use_embeddings)
        _client = cl
        _pid = getattr(getattr(cl, "_proc", None), "pid", None)
        logger.info(
            "mcp_start: icm serve daemon started (pid=%s, use_embeddings=%s)",
            _pid, use_embeddings,
        )
    except FileNotFoundError as exc:
        _client = None
        raise ICMNotFoundError(str(exc)) from exc
    except ICMConnectionError:
        _client = None
        raise
    except OSError as exc:
        _client = None
        raise ICMConnectionError(str(exc)) from exc


def mcp_stop() -> None:
    """Shut down the MCP daemon (no-op if not running).

    v0.4.4 — this is now ONLY called from the :func:`atexit` hook below
    (and from tests). Per-provider ``shutdown`` no longer invokes it
    because the daemon is a process-wide singleton — see the comment in
    ``provider.IcmMemoryProvider.shutdown``.
    """
    global _client  # noqa: PLW0603
    if _client is not None:
        _pid = getattr(getattr(_client, "_proc", None), "pid", None)
        logger.info("mcp_stop: stopping icm serve daemon (pid=%s)", _pid)
        _client.close()
        _client = None


def _atexit_stop() -> None:
    """Final daemon teardown on interpreter shutdown.

    Registered via :func:`atexit.register` at module import time. The
    daemon is a process-wide singleton (see ``mcp_stop`` docstring) and
    must outlive any individual ``IcmMemoryProvider.shutdown`` call;
    cleanest deterministic teardown point is therefore process exit.
    """
    with contextlib.suppress(Exception):  # atexit must never raise
        mcp_stop()


atexit.register(_atexit_stop)


# ------------------------------------------------------------------ public helpers


def _get_client() -> mcp_client.IcmMcpClient:
    """Return the global client or raise ``ICMConnectionError``."""
    if _client is None:
        raise ICMConnectionError("MCP client not started — call mcp_start first")
    if not _client.is_available():
        raise ICMConnectionError("MCP client is disabled after repeated failures")
    return _client


def run_recall(
    query: str,
    limit: int,
    db_path: Path | None,
    timeout_ms: int,
    *,
    use_embeddings: bool = True,
    topic: str | None = None,
    project: str | None = None,  # noqa: ARG001 — icm CLI has no --project flag
) -> list[dict[str, Any]]:
    """Run ``icm recall`` via a one-shot CLI subprocess (v0.5.0).

    v0.4 routed recall through the warm MCP daemon for speed (~10ms/call vs
    ~150ms cold subprocess). v0.5.0 reverts to subprocess because ICM's
    MCP-served recall ranker surfaces empty-topic memoir entries above
    topic-tagged memories — the CLI ranker behaves correctly on the same
    data. Speed cost is real (subprocess startup) but correctness wins.

    Store / topics / health calls still go through the warm MCP daemon —
    only recall is broken upstream.

    Returns the parsed list of memory dicts; empty list on no-results or
    on any failure that doesn't deserve raising.
    """
    argv: list[str] = ["icm"]
    if not use_embeddings:
        # Global flag MUST precede the subcommand per icm's clap parser.
        argv.append("--no-embeddings")
    argv += ["recall", query, "--limit", str(limit), "--format", "json"]
    if topic:
        argv += ["-t", topic]
    if db_path is not None:
        argv += ["--db", str(db_path)]

    try:
        result = subprocess.run(  # noqa: S603 — argv is list, no shell, AD-19 safe
            argv,
            capture_output=True,
            text=True,
            timeout=max(timeout_ms, 1) / 1000.0,
        )
    except FileNotFoundError as exc:
        raise ICMNotFoundError(f"icm binary not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ICMTimeoutError(f"icm recall timed out after {timeout_ms}ms") from exc

    if result.returncode != 0:
        raise ICMConnectionError(
            f"icm recall exit {result.returncode}: {result.stderr.strip()[:200]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        return []
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        # icm prints a plain "No memories found." sentinel for empty queries
        # under some build flavours — treat as zero hits, not a crash.
        return []
    if not isinstance(parsed, list):
        return []
    # Defensive: ensure each entry is a dict (icm should always return that
    # shape under --format json, but a future schema change shouldn't crash
    # the caller).
    return [h for h in parsed if isinstance(h, dict)]


def run_store(
    topic: str,
    content: str,
    importance: str,
    db_path: Path | None,  # noqa: ARG001
    timeout_ms: int,  # noqa: ARG001
    keywords: str | None = None,
    raw: str | None = None,  # noqa: ARG001 — MCP store doesn't support raw via tools
) -> None:
    """Run ``icm store`` via the MCP daemon. Returns None on success."""
    cl = _get_client()
    success = cl.call_store(topic=topic, content=content, importance=importance, keywords=keywords)
    if not success:
        raise ICMConnectionError(f"store failed for topic {topic!r}")


def run_topics(
    db_path: Path | None,  # noqa: ARG001
    timeout_ms: int,  # noqa: ARG001
) -> list[dict[str, Any]]:
    """Run ``icm topics`` via the MCP daemon."""
    cl = _get_client()
    return cl.call_topics()


def run_health(
    db_path: Path | None,  # noqa: ARG001
    timeout_ms: int,  # noqa: ARG001
    topic: str | None = None,
) -> dict[str, Any]:
    """Run ``icm health`` via the MCP daemon."""
    cl = _get_client()
    return cl.call_health(topic=topic)
