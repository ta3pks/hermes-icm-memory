"""``IcmMemoryProvider`` — Hermes-side memory provider class.

Implements every Hermes-required lifecycle method on top of
:mod:`hermes_icm_memory.config`. Architecture invariants:

* **AD-12** — this module MUST NOT import ``subprocess`` (S11 AST test enforces).
* **AD-13** — module-level ``logger = logging.getLogger(__name__)``; never root.
* **AD-07** — every public method catches at the boundary and returns the
  documented degraded shape. No exception ever propagates into a Hermes turn.
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

logger = logging.getLogger(__name__)

#: Placeholder JSON returned by :meth:`IcmMemoryProvider.handle_tool_call`
#: until S09 wires real tool handlers.
_TOOL_UNAVAILABLE_JSON: Final[str] = json.dumps({"error": "tool unavailable"})

#: Filename of the JSON sidecar persisted under ``<hermes_home>/icm/``.
_CONFIG_SIDECAR_NAME: Final[str] = "config.json"


class IcmMemoryProvider:
    """Hermes ``MemoryProvider`` backed by the local ``icm`` CLI.

    All public methods are non-raising at their boundary (AD-07): on any
    failure they log a WARNING and return a documented degraded shape.
    """

    #: Plugin name as registered with Hermes. Frozen — architecture §11.8.
    name: str = "icm"

    def __init__(self) -> None:
        """Initialise empty state holders. No I/O, no subprocess, no network."""
        self._db_path: Path | None = None
        self._available: bool | None = None
        self._config: dict[str, Any] = {}
        self._session_id: str | None = None
        # ``(session_id, str(hermes_home), profile)`` — set by initialize and
        # used to detect an idempotent re-init with the same arguments. Also
        # serves as the "have we initialised at all" flag (None == no).
        self._init_args: tuple[str, str, str | None] | None = None

    # ------------------------------------------------------------------ availability

    def is_available(self) -> bool:
        """Return ``True`` iff ``icm`` is on PATH (cached after the first call).

        Self-disable: :meth:`initialize` flips the cache to ``False`` (sticky)
        when the filesystem is unwritable — failure-mode matrix §6.3 row 8.
        """
        if self._available is not None:
            return self._available
        try:
            self._available = bool(shutil.which("icm"))
        except Exception as exc:  # pragma: no cover — shutil.which is total
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

        Idempotent on the same ``(session_id, hermes_home, profile)`` triple.

        On ``OSError`` from :func:`config.mkdir_parent` (read-only filesystem),
        logs a WARNING, sets ``_available = False`` (sticky), records the
        failed args so re-calls with the same triple stay no-ops, and returns
        without raising — failure-mode matrix §6.3 row 8.
        """
        args_key = (session_id, str(hermes_home), profile)
        if self._init_args == args_key:
            return

        try:
            db_path = config.resolve_db_path(hermes_home, profile)
            config.mkdir_parent(db_path)
        except OSError as exc:
            logger.warning(
                "initialize failed: hermes_home not writable; provider self-disabling",
                extra={"hermes_home": str(hermes_home), "err": repr(exc)},
            )
            self._available = False
            self._init_args = args_key
            return

        self._db_path = db_path
        self._session_id = session_id
        self._init_args = args_key

    # ------------------------------------------------------------------ config

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Return a fresh defensive copy of the architecture §10.1 schema."""
        return config.get_default_schema()

    def save_config(
        self,
        values: dict[str, Any],
        hermes_home: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any] | None:
        """Validate ``values`` and (if ``hermes_home`` given) persist a JSON sidecar.

        Returns ``None`` on success or ``{"error": "<msg>"}`` on validation
        failure (FR7 / AD-18). Disk-write failures surface in the same
        error-dict shape; never raises.

        ``hermes_home=None`` is accepted (the S11 NFR-SEC-1 invariant calls
        ``save_config({})`` without one): validation runs and ``_config`` is
        updated, but no sidecar is written.
        """
        ok, result = config.validate(values)
        if not ok:
            return result

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
        """Return the LLM-facing tool schemas. Empty stub until S09."""
        return []

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> str:  # noqa: ARG002
        """Dispatch an LLM tool call. Always returns a JSON-encoded string.

        Stub returns ``{"error": "tool unavailable"}`` for every name; S09
        replaces the body with a dispatch table into ``tools.py``.
        """
        return _TOOL_UNAVAILABLE_JSON
