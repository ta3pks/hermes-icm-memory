"""Sole owner of every ``subprocess`` invocation against the ``icm`` binary.

Architecture compliance:

* AD-12 / NFR-MAINT-2 — ``cli_runner`` is the **only** module under
  ``hermes_icm_memory/`` that imports ``subprocess``. v0.2 extends this with
  a private MCP-stdio client section: the ``icm serve`` daemon path lives
  here too so the AD-12 invariant survives.
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

v0.2 — ``transport`` kwarg toggles between two implementations:

* ``cli`` (default) — fresh ``subprocess.run`` per call (the v0.1.x path).
* ``mcp`` — one long-lived ``icm serve`` subprocess per provider lifetime,
  spoken to via newline-delimited JSON-RPC 2.0 over stdin/stdout. Lazy
  spawn via :func:`mcp_start`, explicit teardown via :func:`mcp_stop`, and
  an ``atexit`` backstop so torn-down sessions never leak orphans.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import re
import subprocess  # AD-12: ONLY import in the package.
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    ICMMalformedOutputError,
    ICMNonZeroExitError,
    ICMNotFoundError,
    ICMTimeoutError,
)

__all__ = [
    "mcp_start",
    "mcp_stop",
    "run_health",
    "run_recall",
    "run_store",
    "run_topics",
]

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
    *,
    use_embeddings: bool = True,
    transport: str = "cli",
    topic: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``icm recall`` and return the parsed list of hits.

    ``transport`` selects between ``cli`` (fresh subprocess per call, v0.1.x
    path) and ``mcp`` (long-lived ``icm serve`` daemon over JSON-RPC, v0.2).
    The MCP path requires :func:`mcp_start` to have been called earlier in
    the provider lifecycle.

    ``use_embeddings`` controls whether ``--no-embeddings`` is appended to argv:

    * ``True`` (default, v0.1.1) — omits the flag so ``icm`` uses its configured
      embedding model and runs full semantic search.
    * ``False`` — appends ``--no-embeddings`` for keyword-only recall.
    """
    if transport == "mcp":
        return _mcp_recall(
            query=query,
            limit=limit,
            timeout_ms=timeout_ms,
            topic=topic,
            project=project,
        )
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
    if not use_embeddings:
        argv.append("--no-embeddings")
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
    *,
    transport: str = "cli",
) -> None:
    """Run ``icm store``. Stdout is discarded; only ``returncode`` matters.

    ``transport='mcp'`` routes through the long-lived ``icm serve`` daemon
    instead of spawning a fresh subprocess.
    """
    if transport == "mcp":
        _mcp_store(
            topic=topic,
            content=content,
            importance=importance,
            keywords=keywords,
            raw=raw,
            timeout_ms=timeout_ms,
        )
        return
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


def run_topics(
    db_path: Path | None,
    timeout_ms: int,
    *,
    transport: str = "cli",
) -> list[dict[str, Any]]:
    """Run ``icm topics`` (no ``--format json`` — recall-only on icm 0.10.43).

    Parses the aligned-table stdout: column splits on two-or-more spaces,
    one row per line; the first row is treated as a header and used to key
    each row's columns. Single-column output falls back to
    ``[{"topic": <line>}, ...]``.

    ``transport='mcp'`` calls ``icm_memory_list_topics`` over the daemon
    instead and parses the MCP text response into the same shape.
    """
    if transport == "mcp":
        return _mcp_topics(timeout_ms=timeout_ms)
    argv = ["icm", *_db_args(db_path), "topics"]
    proc = _run(argv, timeout_ms)
    return _parse_topics_table(proc.stdout)


def run_health(
    db_path: Path | None,
    timeout_ms: int,
    topic: str | None = None,
    *,
    transport: str = "cli",
) -> dict[str, Any]:
    """Run ``icm health`` (no ``--format json`` — recall-only on icm 0.10.43).

    Parses ``key: value`` lines into a dict. ``-t topic`` narrows the scope.
    Raises ``ICMMalformedOutputError`` when non-blank stdout yields zero
    parseable lines.

    ``transport='mcp'`` returns ``{"raw": <text>}`` because the MCP-shaped
    health report is multi-line per-topic and not symmetrical with the
    flat key:value CLI form. The downstream ``handle_tool_call`` payload
    just JSON-serialises this dict.
    """
    if transport == "mcp":
        return _mcp_health(topic=topic, timeout_ms=timeout_ms)
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


# ---------- MCP transport (v0.2) --------------------------------------------
#
# The MCP path keeps a single ``icm serve`` subprocess alive across calls so
# the embedding-model cold start (~50s on a 4GB Pi) is paid once instead of
# per-recall. State is module-level (one daemon per provider lifetime —
# Hermes runs one provider per service so this is sufficient) and guarded
# by a lock that serialises the JSON-RPC request/response cycle.
#
# The MCP tool names exposed by ``icm serve`` (verified by ``tools/list``
# probe on icm 0.10.43) use the ``icm_memory_*`` prefix; that mapping lives
# inside this section so the plugin's external surface is unchanged.

_MCP_PROTOCOL_VERSION: str = "2024-11-05"
_MCP_TOOL_RECALL: str = "icm_memory_recall"
_MCP_TOOL_TOPICS: str = "icm_memory_list_topics"
_MCP_TOOL_HEALTH: str = "icm_memory_health"
_MCP_TOOL_STORE: str = "icm_memory_store"

# Cap on how many lines we consume looking for a matching response id before
# concluding the daemon is wedged. Each line is parsed once; bounded by both
# this counter and the per-call timeout budget.
_MCP_MAX_RESPONSE_LINES: int = 256


@dataclass
class _McpDaemon:
    """Per-provider MCP daemon state.

    Only one of these is alive at a time, held by the module-level
    :data:`_mcp_state` slot. A new ``mcp_start`` after ``mcp_stop`` produces
    a fresh instance; the per-process ``atexit`` hook calls ``mcp_stop``.
    """

    proc: subprocess.Popen[str]
    cached_db_path: Path | None
    cached_use_embeddings: bool
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_id: int = 1


# Module-level holders. Functions read/write through narrow accessors so the
# (rare) test reset path stays predictable.
_mcp_state: _McpDaemon | None = None
_mcp_disabled: bool = False
_mcp_atexit_registered: bool = False


def _mcp_reset_state_for_tests() -> None:
    """Test-only helper: clear daemon state without sending JSON-RPC."""
    global _mcp_state, _mcp_disabled
    if _mcp_state is not None:
        with contextlib.suppress(Exception):
            if _mcp_state.proc.stdin is not None:
                _mcp_state.proc.stdin.close()
        with contextlib.suppress(Exception):
            _mcp_state.proc.terminate()
    _mcp_state = None
    _mcp_disabled = False


def mcp_start(*, db_path: Path | None, use_embeddings: bool) -> None:
    """Spawn ``icm serve`` and complete the MCP handshake.

    Idempotent on consecutive calls with a live daemon — the second call
    is a no-op so callers (provider.initialize) don't have to track state.
    Resets the ``_mcp_disabled`` sentinel: an explicit start clears the
    "give up" flag so an operator-driven restart can recover from a
    double-death scenario.
    """
    global _mcp_state, _mcp_disabled, _mcp_atexit_registered
    _mcp_disabled = False
    if _mcp_state is not None and _mcp_state.proc.poll() is None:
        # Already alive; treat as no-op so initialize stays idempotent.
        return
    _mcp_state = _mcp_spawn(db_path=db_path, use_embeddings=use_embeddings)
    if not _mcp_atexit_registered:
        atexit.register(mcp_stop)
        _mcp_atexit_registered = True


def mcp_stop() -> None:
    """Tear down the daemon. Safe to call when no daemon is running."""
    global _mcp_state
    daemon = _mcp_state
    _mcp_state = None
    if daemon is None:
        return
    try:
        if daemon.proc.stdin is not None:
            daemon.proc.stdin.close()
    except Exception as exc:  # noqa: BLE001 — defensive teardown
        logger.debug("mcp_stop: stdin close raised", extra={"err": repr(exc)})
    try:
        daemon.proc.terminate()
        daemon.proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            daemon.proc.kill()
        except Exception as exc:  # noqa: BLE001
            logger.debug("mcp_stop: kill raised", extra={"err": repr(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.debug("mcp_stop: terminate raised", extra={"err": repr(exc)})


def _mcp_spawn(*, db_path: Path | None, use_embeddings: bool) -> _McpDaemon:
    """Build argv, spawn ``icm serve``, complete the MCP handshake.

    Raises :class:`ICMNotFoundError` when ``icm`` is not on PATH; raises
    :class:`ICMTimeoutError` if the handshake response never arrives.
    """
    argv: list[str] = ["icm"]
    if db_path is not None:
        argv += ["--db", str(db_path)]
    if not use_embeddings:
        argv.append("--no-embeddings")
    argv.append("serve")

    logger.debug("mcp_spawn: launching", extra={"argv": _redact_argv(argv)})
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is a list, shell=False
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # line-buffered
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ICMNotFoundError(str(exc)) from exc

    daemon = _McpDaemon(
        proc=proc,
        cached_db_path=db_path,
        cached_use_embeddings=use_embeddings,
    )

    # Handshake: send ``initialize`` + ``notifications/initialized``. Use a
    # generous 30s budget — model-load on Pi can take ~50s but the handshake
    # itself returns before model load (icm serve is lazy-init for embeddings).
    init_id = _mcp_alloc_id(daemon)
    _mcp_write(daemon, {
        "jsonrpc": "2.0",
        "id": init_id,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "hermes-icm-memory", "version": "0.2.0"},
        },
    })
    _mcp_read_response(daemon, init_id, timeout_ms=30000)
    _mcp_write(daemon, {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    return daemon


def _mcp_alloc_id(daemon: _McpDaemon) -> int:
    """Monotonic JSON-RPC id allocator (caller already holds the lock OR
    handshake hasn't started yet)."""
    rid = daemon.next_id
    daemon.next_id += 1
    return rid


def _mcp_write(daemon: _McpDaemon, message: dict[str, Any]) -> None:
    """Serialize ``message`` as one newline-terminated JSON line on stdin."""
    if daemon.proc.stdin is None:  # pragma: no cover — Popen always sets it
        raise ICMNotFoundError("mcp daemon stdin is None")
    line = json.dumps(message) + "\n"
    daemon.proc.stdin.write(line)
    daemon.proc.stdin.flush()


def _mcp_read_response(
    daemon: _McpDaemon, expected_id: int, timeout_ms: int
) -> dict[str, Any]:
    """Read response lines until one matches ``expected_id``.

    Skips JSON-RPC notifications (``id`` absent), id-mismatches, and
    unparseable lines. Raises :class:`ICMTimeoutError` when the wall-clock
    budget expires or :data:`_MCP_MAX_RESPONSE_LINES` lines have been
    consumed without a match.
    """
    if daemon.proc.stdout is None:  # pragma: no cover
        raise ICMNotFoundError("mcp daemon stdout is None")
    deadline = time.monotonic() + timeout_ms / 1000.0
    seen = 0
    while True:
        if time.monotonic() > deadline or seen >= _MCP_MAX_RESPONSE_LINES:
            raise ICMTimeoutError(
                f"mcp response for id={expected_id} did not arrive within {timeout_ms}ms"
            )
        line = daemon.proc.stdout.readline()
        seen += 1
        if not line:
            # EOF or no data yet — fall back to a small sleep to avoid busy loop.
            if daemon.proc.poll() is not None:
                raise BrokenPipeError("mcp daemon exited")
            time.sleep(0.005)
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("id") == expected_id:
            return msg


def _mcp_call(
    method: str,
    params: dict[str, Any],
    timeout_ms: int,
) -> dict[str, Any]:
    """Send one JSON-RPC request, return the parsed ``result`` payload.

    Lazy-respawns the daemon once on broken pipe / unexpected exit. On the
    second consecutive failure, sets ``_mcp_disabled`` and raises
    :class:`ICMNotFoundError` so the caller's degrade path takes over.
    """
    global _mcp_disabled
    if _mcp_disabled:
        raise ICMNotFoundError("mcp transport disabled (post-second-death)")
    if _mcp_state is None:
        raise ICMNotFoundError("mcp transport not started; call mcp_start first")

    try:
        return _mcp_send_and_parse(_mcp_state, method, params, timeout_ms)
    except (BrokenPipeError, ICMNotFoundError) as first_exc:
        logger.warning(
            "mcp: daemon died mid-call; respawning once",
            extra={"err": repr(first_exc), "method": method},
        )
        try:
            _mcp_respawn()
        except Exception as respawn_exc:  # noqa: BLE001 — degrade to disabled
            _mcp_disabled = True
            logger.warning(
                "mcp: respawn failed; transport disabled",
                extra={"err": repr(respawn_exc)},
            )
            raise ICMNotFoundError(
                f"mcp respawn failed: {respawn_exc!r}"
            ) from respawn_exc
        if _mcp_state is None:  # pragma: no cover — _mcp_respawn sets it
            _mcp_disabled = True
            raise ICMNotFoundError("mcp respawn produced no daemon") from first_exc
        try:
            return _mcp_send_and_parse(_mcp_state, method, params, timeout_ms)
        except (BrokenPipeError, ICMNotFoundError) as second_exc:
            _mcp_disabled = True
            logger.warning(
                "mcp: second death — transport disabled for the rest of the lifetime",
                extra={"err": repr(second_exc)},
            )
            raise ICMNotFoundError(
                f"mcp daemon died twice in a row: {second_exc!r}"
            ) from second_exc


def _mcp_respawn() -> None:
    """Tear down the dead daemon and start a fresh one with cached args."""
    global _mcp_state
    cached_db: Path | None = None
    cached_embed: bool = True
    if _mcp_state is not None:
        cached_db = _mcp_state.cached_db_path
        cached_embed = _mcp_state.cached_use_embeddings
    mcp_stop()
    _mcp_state = _mcp_spawn(db_path=cached_db, use_embeddings=cached_embed)


def _mcp_send_and_parse(
    daemon: _McpDaemon,
    method: str,
    params: dict[str, Any],
    timeout_ms: int,
) -> dict[str, Any]:
    """Lock + write + read; return the unwrapped ``result`` dict."""
    with daemon.lock:
        rid = _mcp_alloc_id(daemon)
        _mcp_write(daemon, {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        })
        msg = _mcp_read_response(daemon, rid, timeout_ms=timeout_ms)
    if "error" in msg:
        raise ICMNonZeroExitError(json.dumps(msg["error"])[:_STDOUT_SNIPPET])
    result = msg.get("result")
    if not isinstance(result, dict):
        raise ICMMalformedOutputError(json.dumps(msg)[:_STDOUT_SNIPPET])
    return result


def _mcp_extract_text(result: dict[str, Any]) -> str:
    """Pull ``result.content[0].text`` out of an MCP tools/call response."""
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return ""
    first = content[0]
    if not isinstance(first, dict):
        return ""
    text = first.get("text", "")
    return str(text) if isinstance(text, str) else ""


# ---------- MCP per-tool wrappers --------------------------------------------


def _mcp_recall(
    *,
    query: str,
    limit: int,
    timeout_ms: int,
    topic: str | None,
    project: str | None,
) -> list[dict[str, Any]]:
    """Send ``icm_memory_recall`` over MCP, return parsed hit-shaped dicts.

    ``project`` defaults to ``""`` rather than being omitted. The icm-serve
    upstream defaults to "filter by cwd directory name" when project is
    absent — for Hermes that's the gateway's cwd (e.g. ``hermes-gateway``)
    which silently drops every hit. Empty string defeats the filter.
    """
    arguments: dict[str, Any] = {"query": query, "limit": limit}
    if topic is not None:
        arguments["topic"] = topic
    arguments["project"] = "" if project is None else project
    result = _mcp_call(
        "tools/call",
        {"name": _MCP_TOOL_RECALL, "arguments": arguments},
        timeout_ms,
    )
    text = _mcp_extract_text(result)
    return _parse_mcp_recall_text(text)


def _mcp_topics(*, timeout_ms: int) -> list[dict[str, Any]]:
    """Send ``icm_memory_list_topics`` over MCP, parse text → list[dict]."""
    result = _mcp_call(
        "tools/call",
        {"name": _MCP_TOOL_TOPICS, "arguments": {}},
        timeout_ms,
    )
    return _parse_mcp_topics_text(_mcp_extract_text(result))


def _mcp_health(*, topic: str | None, timeout_ms: int) -> dict[str, Any]:
    """Send ``icm_memory_health`` over MCP. Wraps text in ``{"raw": <text>}``."""
    arguments: dict[str, Any] = {}
    if topic is not None:
        arguments["topic"] = topic
    result = _mcp_call(
        "tools/call",
        {"name": _MCP_TOOL_HEALTH, "arguments": arguments},
        timeout_ms,
    )
    return {"raw": _mcp_extract_text(result)}


def _mcp_store(
    *,
    topic: str,
    content: str,
    importance: str,
    keywords: str | None,
    raw: str | None,
    timeout_ms: int,
) -> None:
    """Send ``icm_memory_store`` over MCP. Discards the response payload."""
    arguments: dict[str, Any] = {
        "topic": topic,
        "content": content,
        "importance": importance,
    }
    if keywords:
        # icm_memory_store wants ``keywords: array``; the CLI form is a CSV.
        arguments["keywords"] = [k.strip() for k in keywords.split(",") if k.strip()]
    if raw is not None:
        arguments["raw_excerpt"] = raw
    _mcp_call(
        "tools/call",
        {"name": _MCP_TOOL_STORE, "arguments": arguments},
        timeout_ms,
    )


# ---------- MCP text-response parsers ----------------------------------------

# Recall text comes back as repeated ``[topic] **title**\n\nbody.\n\n`` blocks.
# This regex finds each block start; we then split + populate hit dicts.
# Recall hits start with ``^[<topic>] `` (group 1 = topic). Whatever follows
# on the rest of that block — until the next ``^[`` line or EOF — is the
# memory content. The optional ``**<title>**\n\n<body>`` shape (which
# ``icm serve`` uses for full-section dumps in non-search modes) is unwrapped
# inside :func:`_parse_mcp_recall_text` so single-line search hits and
# title+body dumps both produce the same ``{topic, summary}`` shape.
_MCP_RECALL_HEADER = re.compile(r"^\[([^\]]+)\]\s?(.*?)$", re.MULTILINE)
_MCP_TITLE_BODY = re.compile(r"^\*\*(.+?)\*\*\s*\n\s*\n(.*)\Z", re.DOTALL)


def _parse_mcp_recall_text(text: str) -> list[dict[str, Any]]:
    """Parse the recall text blob into hit-shaped dicts.

    Each block in the MCP response starts with ``[<topic>] ...`` on its own
    line; everything until the next such line (or EOF) is that hit's body.
    ``icm serve`` uses two block shapes the parser must accept:

    * Search-result form (most common): ``[<topic>] <body>`` — body may span
      multiple lines but does not contain a leading ``**title**``.
    * Section-dump form: ``[<topic>] **<title>**\\n\\n<body>``. We strip the
      title wrapper into a separate ``title`` field and keep ``<body>`` as
      the summary.

    Returns ``[]`` when the text is empty or unrecognized — empty hits are
    a legitimate "no matches" answer, not an error.
    """
    if not text or not text.strip():
        return []

    matches = list(_MCP_RECALL_HEADER.finditer(text))
    if not matches:
        return []

    hits: list[dict[str, Any]] = []
    for i, match in enumerate(matches):
        topic = match.group(1).strip()
        same_line_tail = match.group(2)
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # Reassemble the full block content: same-line tail + any extra lines
        # under it, up to the next block header.
        body_after_first_line = text[match.end() : block_end]
        full_body = (same_line_tail + body_after_first_line).strip()

        title: str | None = None
        title_body_match = _MCP_TITLE_BODY.match(full_body)
        if title_body_match:
            title = title_body_match.group(1).strip()
            full_body = title_body_match.group(2).strip()

        # ``summary`` favours the body; fall back to title alone if body is empty.
        summary = full_body if full_body else (title or "")
        hit: dict[str, Any] = {"topic": topic, "summary": summary}
        if title is not None:
            hit["title"] = title
        hits.append(hit)
    return hits


def _parse_mcp_topics_text(text: str) -> list[dict[str, Any]]:
    """Parse ``Topics:\\n  <topic>: N memories\\n`` → list[{topic, count}].

    Lines outside the ``  <topic>: N memories`` shape are skipped silently
    so the parser tolerates the upstream-format header line.
    """
    rows: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.endswith(":") or stripped.startswith("Topics"):
            continue
        # ``<topic>: N memories``
        topic, _, tail = stripped.partition(":")
        topic = topic.strip()
        if not topic:
            continue
        # Pull the leading int out of the tail; format is ``N memories`` or ``N``.
        tail_tokens = tail.strip().split()
        if not tail_tokens:
            rows.append({"topic": topic})
            continue
        rows.append({"topic": topic, "count": tail_tokens[0]})
    return rows
