"""LLM-facing tool dispatch (FR8, FR11, FR13, FR17, FR19).

Pure dispatch functions: ``provider.handle_tool_call`` delegates to
:func:`handle_tool_call` here, which routes the four canonical names
(``icm_recall``, ``icm_store``, ``icm_topics``, ``icm_health``) to the
private ``_handle_*`` functions.

Architecture invariants:

* **AD-10** — every handler returns ``json.dumps(...)``. Never a dict.
* **AD-12 / NFR-MAINT-2** — this module MUST NOT import ``subprocess``.
  All ICM I/O flows through :mod:`hermes_icm_memory.cli_runner`.
* **AD-13 / NFR-OBS-1** — module-level ``logger = logging.getLogger(__name__)``.
* **AD-07 / NFR-REL-1 / FR19** — every handler is wrapped at the outermost
  boundary; on any failure we log a WARNING with ``extra={...}`` and return
  the documented degrade JSON. No exception escapes to the agent turn.
* **NFR-PERF-1 / FR13** — ``icm_store`` is non-blocking: validate, enqueue
  via ``provider._write_queue.put_nowait``, return. The actual ``icm store``
  subprocess runs on the daemon worker (S08).
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import queue
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from .cli_runner import run_health, run_recall, run_topics
from .errors import ICMError

if TYPE_CHECKING:
    from .provider import IcmMemoryProvider

__all__ = ["get_tool_schemas", "handle_tool_call"]

logger = logging.getLogger(__name__)

# ---- Defaults (overridden by provider config when present) ------------------

_DEFAULT_IMPORTANCE: Final[str] = "high"
_DEFAULT_RECALL_LIMIT: Final[int] = 5
_DEFAULT_READ_TIMEOUT_MS: Final[int] = 2000

# ---- Tool schemas (PRD §8.6) — frozen at import; deep-copied to callers ----

_TOOL_SCHEMAS: Final[list[dict[str, Any]]] = [
    {
        "name": "icm_recall",
        "description": "Search ICM memory for hits matching the query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query.",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional topic filter (e.g. 'preferences').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of hits to return.",
                    "default": _DEFAULT_RECALL_LIMIT,
                },
                "project": {
                    "type": "string",
                    "description": "Optional project scope.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "icm_store",
        "description": (
            "Record a memory in ICM. Non-blocking — returns as soon as the "
            "task is queued for the background writer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "ICM topic (e.g. 'preferences', 'errors-resolved').",
                },
                "content": {
                    "type": "string",
                    "description": "Memory content to store.",
                },
                "importance": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                    "default": _DEFAULT_IMPORTANCE,
                    "description": "Importance level for ICM ranking.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Optional keyword list for hybrid recall.",
                },
                "raw": {
                    "type": "string",
                    "description": "Optional raw payload retained alongside content.",
                },
            },
            "required": ["topic", "content"],
        },
    },
    {
        "name": "icm_topics",
        "description": "List the ICM topics currently populated in the local DB.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "icm_health",
        "description": (
            "Return ICM's staleness/consolidation report (optionally scoped "
            "to a topic)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Optional topic filter.",
                },
            },
            "required": [],
        },
    },
]


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return a fresh deep copy of the four LLM-facing tool schemas (AC1, AC2)."""
    return copy.deepcopy(_TOOL_SCHEMAS)


# ---- Internal helpers --------------------------------------------------------


def _read_timeout_ms(provider: IcmMemoryProvider) -> int:
    """Resolve the read-path timeout from provider config or fall back."""
    raw = provider._config.get("command_timeout_read_ms")
    if isinstance(raw, int) and raw > 0:
        return raw
    return _DEFAULT_READ_TIMEOUT_MS


def _normalize_keywords(raw: object) -> list[str]:
    """Coerce caller ``keywords`` into a ``list[str]``.

    Accepts ``list[str]``, comma-separated string, or anything else (→ ``[]``).
    Used by :func:`_handle_store` so the queue task shape stays uniform.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(k) for k in raw]
    if isinstance(raw, str):
        return [tok.strip() for tok in raw.split(",") if tok.strip()]
    return []


def _iso_now() -> str:
    """UTC-aware ISO 8601 timestamp with microsecond precision."""
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")


# ---- Per-tool handlers -------------------------------------------------------


def _handle_recall(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    """Run ``icm recall`` via ``cli_runner``; degrade to ``{"hits": []}`` on failure."""
    query = args.get("query")
    if not isinstance(query, str) or not query:
        logger.warning(
            "icm_recall: missing or empty 'query'",
            extra={"args_keys": list(args.keys())},
        )
        return json.dumps({"hits": []})

    db_path = provider._db_path
    if db_path is None:
        logger.warning("icm_recall: provider not initialized")
        return json.dumps({"hits": []})

    topic = args.get("topic") if isinstance(args.get("topic"), str) else None
    project = args.get("project") if isinstance(args.get("project"), str) else None

    raw_limit = args.get("limit")
    if isinstance(raw_limit, int) and raw_limit > 0:
        limit = raw_limit
    else:
        cfg_limit = provider._config.get("recall_limit")
        limit = cfg_limit if isinstance(cfg_limit, int) and cfg_limit > 0 else _DEFAULT_RECALL_LIMIT

    try:
        hits = run_recall(
            query,
            limit,
            db_path,
            _read_timeout_ms(provider),
            topic=topic,
            project=project,
        )
    except ICMError as exc:
        logger.warning("icm_recall failed", extra={"err": repr(exc)})
        return json.dumps({"hits": []})

    return json.dumps({"hits": hits})


def _handle_store(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    """Validate + enqueue a write task; never blocks (FR13, NFR-PERF-1)."""
    topic = args.get("topic")
    if not isinstance(topic, str) or not topic:
        return json.dumps({"error": "missing required arg: topic"})
    content = args.get("content")
    if not isinstance(content, str) or not content:
        return json.dumps({"error": "missing required arg: content"})

    importance_raw = args.get("importance")
    if isinstance(importance_raw, str) and importance_raw:
        importance = importance_raw
    else:
        cfg = provider._config.get("default_importance")
        importance = cfg if isinstance(cfg, str) and cfg else _DEFAULT_IMPORTANCE

    keywords = _normalize_keywords(args.get("keywords"))

    task: tuple[str, str, str, list[str]] = (topic, importance, content, keywords)

    write_queue = getattr(provider, "_write_queue", None)
    if write_queue is None:
        logger.warning(
            "icm_store: write queue unavailable",
            extra={"topic": topic},
        )
        return json.dumps({"error": "store queue unavailable"})

    try:
        write_queue.put_nowait(task)
    except queue.Full:
        logger.warning("icm_store: write queue full", extra={"topic": topic})
        return json.dumps({"error": "store queue full"})

    return json.dumps({"accepted": True, "queued_at": _iso_now()})


def _handle_topics(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    """Return the ICM topic list; degrade to ``{"topics": []}`` on failure."""
    del args  # unused — icm_topics takes no parameters
    db_path = provider._db_path
    if db_path is None:
        logger.warning("icm_topics: provider not initialized")
        return json.dumps({"topics": []})
    try:
        topics = run_topics(db_path, _read_timeout_ms(provider))
    except ICMError as exc:
        logger.warning("icm_topics failed", extra={"err": repr(exc)})
        return json.dumps({"topics": []})
    return json.dumps({"topics": topics})


def _handle_health(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    """Return the ICM health report; degrade to ``{"report": {}}`` on failure."""
    topic = args.get("topic") if isinstance(args.get("topic"), str) else None
    db_path = provider._db_path
    if db_path is None:
        logger.warning("icm_health: provider not initialized")
        return json.dumps({"report": {}})
    try:
        report = run_health(db_path, _read_timeout_ms(provider), topic=topic)
    except ICMError as exc:
        logger.warning("icm_health failed", extra={"err": repr(exc)})
        return json.dumps({"report": {}})
    return json.dumps({"report": report})


# ---- Public dispatch ---------------------------------------------------------

_DISPATCH: Final[
    dict[str, Callable[[IcmMemoryProvider, dict[str, Any]], str]]
] = {
    "icm_recall": _handle_recall,
    "icm_store": _handle_store,
    "icm_topics": _handle_topics,
    "icm_health": _handle_health,
}


def handle_tool_call(
    provider: IcmMemoryProvider, name: str, args: dict[str, Any] | None
) -> str:
    """Dispatch an LLM tool call to the right ``_handle_*``.

    Always returns ``json.dumps(...)``. Never raises (AD-07 / FR19): the
    outermost ``except Exception`` net traps anything the handlers fail to
    catch (untyped runtime explosions, garbage input that slips past
    validation) and produces a documented error JSON instead.
    """
    handler = _DISPATCH.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        return handler(provider, args or {})
    except Exception as exc:  # noqa: BLE001 — AD-07 outermost net is intentional
        logger.warning(
            "tool handler crashed",
            extra={"tool": name, "err": repr(exc)},
        )
        return json.dumps({"error": "tool handler crashed"})
