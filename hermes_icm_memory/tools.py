"""LLM-facing tool dispatch — four canonical tools for the agent turn.

``provider.handle_tool_call`` delegates to :func:`handle_tool_call` here,
which routes ``icm_recall`` / ``icm_store`` / ``icm_topics`` / ``icm_health``
to private handlers. Every handler returns a ``json.dumps(...)`` string —
never a dict, never raises. ``cli_runner`` is the only ICM I/O surface; this
module never imports ``subprocess``. ``icm_store`` is non-blocking: it
validates, enqueues via ``provider._write_queue.put_nowait``, and returns;
the actual ``icm store`` subprocess runs on the daemon worker.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import queue
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from . import config
from .cli_runner import run_health, run_recall, run_topics
from .hooks import WriteTask

if TYPE_CHECKING:
    from .provider import IcmMemoryProvider

__all__ = ["get_tool_schemas", "handle_tool_call"]

logger = logging.getLogger(__name__)

# ---- Defaults (overridden by provider config when present) ------------------

_DEFAULT_IMPORTANCE: Final[str] = "high"
_DEFAULT_RECALL_LIMIT: Final[int] = 5
_DEFAULT_READ_TIMEOUT_MS: Final[int] = 2000

#: Single source of truth for the importance enum, shared with the config
#: schema. Bogus values fall back rather than reach the worker.
_VALID_IMPORTANCE: Final[frozenset[str]] = frozenset(config.IMPORTANCE_CHOICES)

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
                    "enum": list(config.IMPORTANCE_CHOICES),
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
    """Return a fresh deep copy of the four LLM-facing tool schemas."""
    return copy.deepcopy(_TOOL_SCHEMAS)


# ---- Internal helpers --------------------------------------------------------


def _positive_int(value: object) -> int | None:
    """Return ``value`` if it is a positive int (rejecting bool); else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _provider_config(provider: IcmMemoryProvider) -> dict[str, Any]:
    """Return the provider's ``_config`` dict, or ``{}`` if missing/corrupt.

    Defensive: if a future caller rebinds ``_config`` to a non-mapping value,
    fall back rather than raise ``AttributeError`` and route to the generic
    crash response (which would violate the documented per-tool degrade shape).
    """
    cfg = getattr(provider, "_config", None)
    return cfg if isinstance(cfg, dict) else {}


def _read_timeout_ms(provider: IcmMemoryProvider) -> int:
    """Resolve the read-path timeout from provider config or fall back."""
    return _positive_int(_provider_config(provider).get("command_timeout_read_ms")) or (
        _DEFAULT_READ_TIMEOUT_MS
    )


def _use_embeddings(provider: IcmMemoryProvider) -> bool:
    """Resolve the ``use_embeddings`` flag from provider config (default ``False``).

    v0.1.1: ``False`` keeps the hot path Pi-safe; opt-in via provider config.
    """
    raw = _provider_config(provider).get("use_embeddings", False)
    return bool(raw) if isinstance(raw, bool) else False


def _recall_limit(provider: IcmMemoryProvider, override: object) -> int:
    """Resolve the recall limit from caller arg, then config, then default."""
    return (
        _positive_int(override)
        or _positive_int(_provider_config(provider).get("recall_limit"))
        or _DEFAULT_RECALL_LIMIT
    )


def _importance_for(provider: IcmMemoryProvider, override: object) -> str:
    """Resolve importance from caller arg, then config, then default.

    Validates against the schema enum so bogus values fall back rather than
    reach ``icm store -i <bogus>`` on the worker thread.
    """
    if isinstance(override, str) and override in _VALID_IMPORTANCE:
        return override
    cfg_importance = _provider_config(provider).get("default_importance")
    if isinstance(cfg_importance, str) and cfg_importance in _VALID_IMPORTANCE:
        return cfg_importance
    return _DEFAULT_IMPORTANCE


def _normalize_keywords(raw: object) -> list[str]:
    """Coerce caller ``keywords`` to ``list[str]``.

    Accepts a list, a comma-separated string, or anything else (→ ``[]``).
    Used by :func:`_handle_store` so the queue task shape stays uniform.
    """
    if isinstance(raw, list):
        return [str(k) for k in raw]
    if isinstance(raw, str):
        return [tok.strip() for tok in raw.split(",") if tok.strip()]
    return []


def _iso_now() -> str:
    """UTC-aware ISO 8601 timestamp with microsecond precision."""
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")


#: Empty degrade payload for each read-path tool name (single-pair JSON).
_EMPTY_READ_PAYLOAD: Final[dict[str, dict[str, Any]]] = {
    "icm_recall": {"hits": []},
    "icm_topics": {"topics": []},
    "icm_health": {"report": {}},
}


def _run_read(
    provider: IcmMemoryProvider,
    name: str,
    payload_key: str,
    do_call: Callable[[Any, int], Any],
) -> str:
    """Shared guard + try/except/log/degrade shape for every read tool.

    The caller passes a ``do_call(db_path, timeout_ms) -> result`` closure
    so each handler can shape ``cli_runner``'s positional args itself
    (``run_recall`` leads with ``query, limit``; ``run_topics`` /
    ``run_health`` lead with ``db_path, timeout_ms``).

    v0.1.1: the "not initialized" guard checks ``provider._init_args`` —
    ``_db_path is None`` is a *legitimate* state under default-shared mode
    (the plugin lets ``icm`` use its canonical OS-default DB, no ``--db``
    forwarded). The actual "never initialized" condition is ``_init_args``
    being ``None``.
    """
    if provider._init_args is None:
        logger.warning("%s: provider not initialized", name, extra={"tool": name})
        return json.dumps(_EMPTY_READ_PAYLOAD[name])
    db_path = provider._db_path
    try:
        result = do_call(db_path, _read_timeout_ms(provider))
    except Exception as exc:  # noqa: BLE001 — every read path degrades, never raises.
        logger.warning("%s failed", name, extra={"tool": name, "err": repr(exc)})
        return json.dumps(_EMPTY_READ_PAYLOAD[name])
    return json.dumps({payload_key: result})


# ---- Per-tool handlers -------------------------------------------------------


def _handle_recall(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    query = args.get("query")
    if not isinstance(query, str) or not query:
        logger.warning(
            "icm_recall: missing or empty 'query'",
            extra={"tool": "icm_recall", "args_keys": list(args.keys())},
        )
        return json.dumps({"hits": []})

    limit = _recall_limit(provider, args.get("limit"))
    topic = args.get("topic") if isinstance(args.get("topic"), str) else None
    project = args.get("project") if isinstance(args.get("project"), str) else None
    use_embeddings = _use_embeddings(provider)

    return _run_read(
        provider,
        "icm_recall",
        "hits",
        lambda db, ms: run_recall(
            query,
            limit,
            db,
            ms,
            use_embeddings=use_embeddings,
            topic=topic,
            project=project,
        ),
    )


def _handle_store(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    topic = args.get("topic")
    if not isinstance(topic, str) or not topic:
        return json.dumps({"error": "missing required arg: topic"})
    content = args.get("content")
    if not isinstance(content, str) or not content:
        return json.dumps({"error": "missing required arg: content"})

    task = WriteTask(
        topic=topic,
        importance=_importance_for(provider, args.get("importance")),
        content=content,
        keywords=tuple(_normalize_keywords(args.get("keywords"))),
    )

    write_queue = getattr(provider, "_write_queue", None)
    if write_queue is None:
        logger.warning(
            "icm_store: write queue unavailable",
            extra={"tool": "icm_store", "topic": topic},
        )
        return json.dumps({"error": "store queue unavailable"})

    try:
        write_queue.put_nowait(task)
    except queue.Full:
        logger.warning(
            "icm_store: write queue full",
            extra={"tool": "icm_store", "topic": topic},
        )
        return json.dumps({"error": "store queue full"})

    return json.dumps({"accepted": True, "queued_at": _iso_now()})


def _handle_topics(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    del args  # icm_topics takes no parameters
    return _run_read(provider, "icm_topics", "topics", run_topics)


def _handle_health(provider: IcmMemoryProvider, args: dict[str, Any]) -> str:
    topic = args.get("topic") if isinstance(args.get("topic"), str) else None
    return _run_read(
        provider, "icm_health", "report", lambda db, ms: run_health(db, ms, topic=topic)
    )


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
    """Route a tool call to the right handler. Never raises.

    Non-dict ``args`` (None, list, scalar) are coerced to ``{}`` so the
    per-tool degrade shape still wins over the generic crash JSON. The
    outermost ``except Exception`` is defense-in-depth against a future
    handler regression that forgets its own try/except.
    """
    handler = _DISPATCH.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    safe_args: dict[str, Any] = args if isinstance(args, dict) else {}
    try:
        return handler(provider, safe_args)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tool handler crashed", extra={"tool": name, "err": repr(exc)}
        )
        return json.dumps({"error": "tool handler crashed"})
