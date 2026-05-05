"""``IcmMemoryProvider`` — Hermes-side memory provider class (S07).

Implements every Hermes-required lifecycle method (``name``, ``is_available``,
``initialize``, ``get_config_schema``, ``save_config``, ``get_tool_schemas``,
``handle_tool_call``) on top of the modules from Epic 2:

* :mod:`hermes_icm_memory.config` — schema, validation, db-path resolution.
* :mod:`hermes_icm_memory.errors` — typed exceptions raised by ``cli_runner``
  (caught at the boundary; not re-raised here).

Architecture compliance:

* **AD-12 / NFR-MAINT-2** — this module MUST NOT import ``subprocess``. The
  S11 AST invariant test enforces that. Shellouts to ``icm`` happen only
  inside :mod:`hermes_icm_memory.cli_runner` (used by ``tools.py`` /
  ``hooks.py`` in later stories — not directly here).
* **AD-13 / NFR-OBS-2** — module-level ``logger = logging.getLogger(__name__)``.
  Never the root logger; never ``print()``.
* **AD-07 / NFR-REL-1** — every public method catches at the boundary and
  returns the documented degraded shape. No exception ever propagates into
  the Hermes turn loop.
* **AD-18** — :meth:`save_config` delegates to :func:`config.validate`
  (already AD-18-compliant) and additionally wraps the JSON-sidecar write in
  ``try/except OSError`` so a read-only filesystem returns
  ``{"error": "could not persist config: …"}`` rather than crashing.
* **AD-05 / AD-06** — :meth:`initialize` calls
  :func:`config.resolve_db_path` and :func:`config.mkdir_parent`. The plugin
  never runs ``icm init``; SQLite auto-creates the DB file on first write.
* **NFR-SEC-1** — :meth:`is_available` uses :func:`shutil.which` only;
  nothing in this module opens a socket. Verified by
  ``tests/test_no_network_calls.py``.

S08 will add the four hook methods (``prefetch``, ``system_prompt_block``,
``sync_turn``, ``on_session_end``) on this same class. S09 will replace the
:meth:`handle_tool_call` and :meth:`get_tool_schemas` stubs with real
dispatch into ``tools.py``. S10 will swap ``register(ctx)`` to construct
this class instead of the S01 stub.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Final

from . import config

__all__ = ["IcmMemoryProvider"]

logger = logging.getLogger(__name__)  # AD-13 — module-level named logger.

#: The placeholder JSON returned by :meth:`IcmMemoryProvider.handle_tool_call`
#: until S09 wires the four real tool handlers from ``tools.py``.
_TOOL_UNAVAILABLE_JSON: Final[str] = json.dumps({"error": "tool unavailable"})

#: Filename of the JSON sidecar that :meth:`IcmMemoryProvider.save_config`
#: persists under ``<hermes_home>/icm/``. Kept tiny (sort_keys + indent for
#: stable diffs); not part of the frozen public API surface §11.8.
_CONFIG_SIDECAR_NAME: Final[str] = "config.json"


class IcmMemoryProvider:
    """Hermes ``MemoryProvider`` backed by the local ``icm`` CLI.

    All public methods are non-raising at their boundary (AD-07 / NFR-REL-1):
    on any failure they log a WARNING and return a documented degraded shape.
    """

    #: Plugin name as registered with Hermes. Frozen — architecture §11.8.
    name: str = "icm"

    def __init__(self) -> None:
        """Initialise empty state holders. No I/O, no subprocess, no network."""
        self._db_path: Path | None = None
        self._available: bool | None = None
        self._config: dict[str, Any] = {}
        self._session_id: str | None = None
        self._initialized: bool = False
        # Tuple ``(session_id, str(hermes_home), profile)`` — used to detect
        # an idempotent re-init with the same arguments (FR4, NFR-REL-5).
        self._init_args: tuple[str, str, str | None] | None = None

    # ------------------------------------------------------------------ availability

    def is_available(self) -> bool:
        """Return ``True`` iff ``icm`` is on PATH (cached after the first call).

        Caching: the first call computes ``bool(shutil.which("icm"))`` and
        stores the result. Subsequent calls return the cached value without
        re-invoking :func:`shutil.which` (verified by AC4 test).

        Self-disable: :meth:`initialize` may flip the cache to ``False`` when
        the filesystem is unwritable (failure-mode matrix §6.3 row 8). The
        flip is sticky for the rest of the process.

        NFR-SEC-1: never opens a socket. Always safe to call.
        """
        if self._available is not None:
            return self._available
        try:
            self._available = bool(shutil.which("icm"))
        except Exception as exc:  # pragma: no cover — defensive; shutil.which is total
            logger.warning("is_available probe raised", extra={"err": repr(exc)})
            self._available = False
        return self._available

    # ------------------------------------------------------------------ initialize

    def initialize(
        self,
        session_id: str,
        hermes_home: str | os.PathLike[str],
        profile: str | None = None,
        **kwargs: Any,  # noqa: ARG002 — Hermes contract may pass extra kwargs.
    ) -> None:
        """Resolve the per-profile DB path and ensure ``<hermes_home>/icm/`` exists.

        Idempotent on the same ``(session_id, hermes_home, profile)`` triple
        (AC8 / FR4 / NFR-REL-5). Different args trigger re-resolution.

        On ``OSError`` from :func:`config.mkdir_parent` (read-only
        filesystem), logs a WARNING, sets ``_available = False`` (sticky),
        marks ``_initialized = True`` so the broken state persists, and
        returns without raising (AC9 / failure-mode matrix §6.3 row 8).
        """
        args_key = (session_id, str(hermes_home), profile)
        if self._initialized and self._init_args == args_key:
            return  # idempotent no-op (AC8)

        try:
            db_path = config.resolve_db_path(hermes_home, profile)
            config.mkdir_parent(db_path)
        except OSError as exc:
            logger.warning(
                "initialize failed: hermes_home not writable; provider self-disabling",
                extra={"hermes_home": str(hermes_home), "err": repr(exc)},
            )
            self._available = False  # sticky False for the rest of the process
            self._initialized = True
            self._init_args = args_key
            return

        self._db_path = db_path
        self._session_id = session_id
        self._initialized = True
        self._init_args = args_key

    # ------------------------------------------------------------------ config

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Return a fresh defensive copy of the architecture §10.1 schema.

        Pure delegation to :func:`config.get_default_schema`, which already
        returns a deep copy each call — caller mutation cannot poison the
        next call (AC10).
        """
        return config.get_default_schema()

    def save_config(
        self,
        values: dict[str, Any],
        hermes_home: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any] | None:
        """Validate ``values`` and (if ``hermes_home`` given) persist a JSON sidecar.

        Returns ``None`` on success or ``{"error": "<msg>"}`` on validation
        failure (FR7 / AD-18). Never raises — disk-write failures are caught
        and surfaced as the same error-dict shape.

        ``hermes_home=None`` is supported (the S11 NFR-SEC-1 invariant test
        calls ``save_config({})`` without a hermes_home): in that case
        validation runs and ``_config`` is updated, but no sidecar is
        written.
        """
        ok, result = config.validate(values)
        if not ok:
            # `result` is already shaped as {"error": "..."}.
            return result

        # Merge normalized values into in-memory config (AC11).
        self._config.update(result)

        if hermes_home is None:
            return None

        try:
            db_path = config.resolve_db_path(hermes_home, profile=None)
            config.mkdir_parent(db_path)
            sidecar = db_path.parent / _CONFIG_SIDECAR_NAME
            sidecar.write_text(
                json.dumps(self._config, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "save_config: could not persist sidecar",
                extra={"hermes_home": str(hermes_home), "err": repr(exc)},
            )
            return {"error": f"could not persist config: {exc}"}

        return None

    # ------------------------------------------------------------------ tools (S09 stubs)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return the LLM-facing tool schemas.

        S07 stub: returns ``[]``. S09 will replace this with the four real
        schemas (``icm_recall``, ``icm_store``, ``icm_topics``, ``icm_health``)
        from ``tools.py``.
        """
        return []

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> str:
        """Dispatch an LLM tool call by name. Always returns a JSON-encoded string.

        S07 stub: every tool name returns
        ``json.dumps({"error": "tool unavailable"})``. S09 will replace the
        body with a dispatch table into ``tools.icm_recall / icm_store /
        icm_topics / icm_health``.
        """
        _ = (name, args)  # parameters preserved for the S09 contract.
        return _TOOL_UNAVAILABLE_JSON
