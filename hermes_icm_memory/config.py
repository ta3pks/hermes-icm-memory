"""Config schema, validation, and DB-path resolution (FR2, FR6, FR7).

Pure module. No subprocess, no logging, no network. Filesystem touch is limited
to :func:`resolve_db_path` (path construction; non-strict ``resolve``) and
:func:`mkdir_parent` (the single ``mkdir(parents=True, exist_ok=True)`` call
mandated by AD-06).

Public surface (frozen post-v1 per NFR-MAINT-1):

* :func:`get_default_schema` — list of twelve schema entries (ten architecture-§10.1
  defaults + the v0.1.1 additions ``isolated`` and ``use_embeddings``).
* :func:`validate` — structural validation, never raises (AD-18).
* :func:`resolve_db_path` — ``<hermes_home>/icm/<profile>.db`` (AD-05).
* :func:`mkdir_parent` — idempotent parent-dir creation (AD-06).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Final

__all__ = [
    "IMPORTANCE_CHOICES",
    "get_default_schema",
    "mkdir_parent",
    "resolve_db_path",
    "validate",
]

_DEFAULT_PROFILE: Final[str] = "default"

#: Importance enum values accepted by ICM (architecture §11.1). Public so
#: ``tools.py`` can derive its enum without duplicating the literal tuple.
IMPORTANCE_CHOICES: Final[tuple[str, ...]] = ("critical", "high", "medium", "low")

#: Architecture §10.1 — ten frozen config entries. Module-private; callers get
#: a defensive copy via :func:`get_default_schema`.
_SCHEMA_ENTRIES: Final[list[dict[str, Any]]] = [
    {
        "key": "default_importance",
        "type": "enum",
        "default": "high",
        "choices": list(IMPORTANCE_CHOICES),
        "secret": False,
        "required": False,
        "description": (
            "Importance applied when an icm_store call omits it. "
            "One of critical/high/medium/low."
        ),
    },
    {
        "key": "topic_prefix",
        "type": "string",
        "default": "",
        "secret": False,
        "required": False,
        "description": "Optional prefix prepended to every stored topic, e.g. 'hermes/'.",
    },
    {
        "key": "recall_limit",
        "type": "int",
        "default": 5,
        "secret": False,
        "required": False,
        "description": "Top-K for prefetch + system_prompt_block.",
    },
    {
        "key": "prefetch_enabled",
        "type": "bool",
        "default": True,
        "secret": False,
        "required": False,
        "description": (
            "If false, prefetch no-ops and system_prompt_block returns the empty string."
        ),
    },
    {
        "key": "sync_write_queue_size",
        "type": "int",
        "default": 64,
        "secret": False,
        "required": False,
        "description": "Bounded write queue capacity (drop-on-full per AD-04).",
    },
    {
        "key": "command_timeout_read_ms",
        "type": "int",
        "default": 2000,
        "secret": False,
        "required": False,
        "description": "Timeout for read-path icm calls (recall, topics, health).",
    },
    {
        "key": "command_timeout_write_ms",
        "type": "int",
        "default": 5000,
        "secret": False,
        "required": False,
        "description": "Timeout for write-path icm calls (store).",
    },
    {
        "key": "session_end_grace_ms",
        "type": "int",
        "default": 1500,
        "secret": False,
        "required": False,
        "description": "on_session_end drain window before remaining writes are dropped.",
    },
    {
        "key": "periodic_progress_every_n_turns",
        "type": "int",
        "default": 20,
        "secret": False,
        "required": False,
        "description": "How often the periodic-progress trigger fires (per-session counter).",
    },
    {
        "key": "consolidate_on_session_end",
        "type": "bool",
        "default": False,
        "secret": False,
        "required": False,
        "description": "If true, fire icm consolidate on configured topics at session end.",
    },
    # ---- v0.1.1 additions ---------------------------------------------------
    {
        "key": "isolated",
        "type": "bool",
        "default": False,
        "secret": False,
        "required": False,
        "description": (
            "If true, use a per-profile DB at <hermes_home>/icm/<profile>.db "
            "(parallel silo). If false (default, the brief's value prop), share "
            "icm's canonical default DB with Claude Code, Cursor, OpenCode, etc."
        ),
    },
    {
        "key": "use_embeddings",
        "type": "bool",
        "default": True,
        "secret": False,
        "required": False,
        "description": (
            "If true (default), icm recall uses semantic search via the "
            "multilingual-e5-base ONNX model — the Brief's value prop. "
            "On Pi-class hardware the model loads ~50s per subprocess call "
            "(no daemon to amortize), so Pi users should set this to false "
            "until v0.2's MCP transport (icm serve) lands. Desktop / cloud "
            "hosts are fine with the default."
        ),
    },
]

#: Per-key minimum integer values. Keys absent from this map have no lower bound
#: beyond ``>= 0``. Zero is permitted only for ``session_end_grace_ms`` (means
#: "drop instantly", supported by AD-16).
_INT_MIN: Final[dict[str, int]] = {
    "recall_limit": 1,
    "sync_write_queue_size": 1,
    "command_timeout_read_ms": 1,
    "command_timeout_write_ms": 1,
    "session_end_grace_ms": 0,
    "periodic_progress_every_n_turns": 1,
}

#: Module-level lookup table built once at import. Used by :func:`validate`.
_SCHEMA_BY_KEY: Final[dict[str, dict[str, Any]]] = {
    entry["key"]: entry for entry in _SCHEMA_ENTRIES
}


def get_default_schema() -> list[dict[str, Any]]:
    """Return a fresh copy of the ten architecture §10.1 schema entries.

    Callers may mutate the returned list and inner dicts without affecting the
    module-level template (NFR-MAINT-1: schema shape is frozen).
    """
    return copy.deepcopy(_SCHEMA_ENTRIES)


def _coerce_bool(raw: object) -> bool | None:
    """Return ``True``/``False`` or ``None`` if ``raw`` is not a recognised bool form."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _coerce_int(raw: object) -> int | None:
    """Return an int or ``None`` if ``raw`` is not a clean int form. Rejects bools."""
    if isinstance(raw, bool):
        # bool subclasses int in Python; refuse it explicitly for int-typed keys.
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _validate_one(key: str, raw: object, entry: dict[str, Any]) -> tuple[bool, object]:
    """Validate + coerce a single key. Returns ``(True, normalized)`` or ``(False, error_msg)``."""
    kind = entry["type"]
    if kind == "int":
        coerced = _coerce_int(raw)
        if coerced is None:
            return False, f"{key}: expected int, got {type(raw).__name__}"
        minimum = _INT_MIN.get(key, 0)
        if coerced < minimum:
            return False, f"{key}: value {coerced} below minimum {minimum}"
        return True, coerced
    if kind == "bool":
        coerced_bool = _coerce_bool(raw)
        if coerced_bool is None:
            return False, f"{key}: expected bool or 'true'/'false', got {raw!r}"
        return True, coerced_bool
    if kind == "string":
        if not isinstance(raw, str):
            return False, f"{key}: expected string, got {type(raw).__name__}"
        return True, raw
    if kind == "enum":
        choices = entry["choices"]
        if not isinstance(raw, str) or raw not in choices:
            return False, f"{key}: expected one of {choices}, got {raw!r}"
        return True, raw
    # Unknown type in the schema literal would be a developer error caught
    # by tests, not user input — fail loudly via the error channel.
    return False, f"{key}: unknown schema type {kind!r}"  # pragma: no cover


def validate(values: Any) -> tuple[bool, dict[str, Any]]:
    """Structural validation of a config-values dict.

    :param values: Caller-supplied dict of config keys to values. Strings are
        coerced for ``int`` and ``bool`` keys (``"5"`` → ``5``; ``"true"`` →
        ``True``). Unknown keys pass through unchanged (forward-compat).
    :returns: ``(True, normalized_values)`` on success, where ``normalized_values``
        contains every input key with coerced values; ``(False, {"error": msg})``
        on failure, where ``msg`` names the offending key.

    Never raises. Garbage input (None, list, scalar, nested junk) returns the
    failure tuple per AD-18.
    """
    try:
        if not isinstance(values, dict):
            return False, {"error": f"expected dict, got {type(values).__name__}"}

        normalized: dict[str, Any] = {}

        for key, raw in values.items():
            entry = _SCHEMA_BY_KEY.get(key)
            if entry is None:
                # Unknown key: pass through (forward-compat).
                normalized[key] = raw
                continue
            ok, result = _validate_one(key, raw, entry)
            if not ok:
                # `result` is the error string here.
                return False, {"error": str(result)}
            normalized[key] = result

        return True, normalized
    except Exception as exc:  # pragma: no cover - defensive last line per AD-18
        return False, {"error": f"unexpected validation error: {exc!r}"}


def resolve_db_path(
    hermes_home: str | os.PathLike[str],
    profile: str | None = None,
) -> Path:
    """Resolve the per-profile DB path under ``hermes_home`` (AD-05).

    :param hermes_home: Filesystem root supplied by Hermes via ``initialize`` kwargs.
        Accepts both ``str`` and any ``os.PathLike``. ``~`` is expanded.
    :param profile: Profile name. ``None`` (or empty string) falls back to ``"default"``.
    :returns: Absolute :class:`~pathlib.Path` ``<hermes_home>/icm/<profile>.db``.
    """
    base = Path(os.fspath(hermes_home)).expanduser().resolve()
    profile_name = profile if profile else _DEFAULT_PROFILE
    return base / "icm" / f"{profile_name}.db"


def mkdir_parent(db_path: Path) -> None:
    """Idempotently ensure ``db_path.parent`` exists (AD-06).

    Uses ``mkdir(parents=True, exist_ok=True)`` — calling twice is a no-op on
    the second call. The SQLite file itself is auto-created by ICM on the first
    ``--db <path>`` call; this plugin never invokes ``icm init``.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
