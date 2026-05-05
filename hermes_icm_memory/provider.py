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
import queue
import shutil
import threading
from pathlib import Path
from typing import Any, Final

from . import config, hooks

__all__ = ["IcmMemoryProvider"]

logger = logging.getLogger(__name__)

#: Placeholder JSON returned by :meth:`IcmMemoryProvider.handle_tool_call`
#: until S09 wires real tool handlers.
_TOOL_UNAVAILABLE_JSON: Final[str] = json.dumps({"error": "tool unavailable"})

#: Filename of the JSON sidecar persisted under ``<hermes_home>/icm/``.
_CONFIG_SIDECAR_NAME: Final[str] = "config.json"

#: Frozen architecture §10.1 defaults, materialised once. Avoids the per-call
#: deep-copy in :meth:`IcmMemoryProvider._cfg` (otherwise ``sync_turn`` would
#: pay an O(N) copy of the schema list every turn).
_DEFAULT_CONFIG: Final[dict[str, Any]] = {
    entry["key"]: entry["default"] for entry in config.get_default_schema()
}


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
        # S08 hot-path state: prefetch cache + worker bundle.
        self._prefetch_cache: dict[int, list[dict[str, Any]]] = {}
        self._latest_prefetch_key: int | None = None
        self._worker_state: hooks.WorkerState = hooks.WorkerState()

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

    # ------------------------------------------------------------------ S08 hot-path
    # The four hook methods + worker plumbing live here as thin wrappers around
    # ``hermes_icm_memory.hooks`` helpers; the hooks module owns the
    # FIFO-bounded-queue + worker model (AD-15 / NFR-REL-2).

    def _config_int(self, key: str) -> int:
        """Read an int config value (caller-saved override or schema default)."""
        return int(self._config.get(key, _DEFAULT_CONFIG[key]))

    def _config_bool(self, key: str) -> bool:
        """Read a bool config value (caller-saved override or schema default)."""
        return bool(self._config.get(key, _DEFAULT_CONFIG[key]))

    # State exposed on the provider (read-only properties + one mutable list).

    @property
    def _write_queue(self) -> queue.Queue[hooks.WriteTask] | None:
        return self._worker_state.write_queue

    @property
    def _worker(self) -> threading.Thread | None:
        return self._worker_state.worker

    @property
    def _stop_event(self) -> threading.Event:
        return self._worker_state.stop_event

    @property
    def _overflow_burst(self) -> list[bool]:
        # 1-element mutable list — producer flips to True, worker resets to False.
        return self._worker_state.overflow_burst

    @property
    def _respawn_count(self) -> int:
        return self._worker_state.respawn_count

    @property
    def _writes_disabled(self) -> bool:
        return self._worker_state.writes_disabled

    def _ensure_worker(self) -> bool:
        """Lazy-spawn / respawn the worker; returns False if writes are disabled."""
        if self._db_path is None:
            return False
        return hooks.ensure_worker(
            self._worker_state,
            queue_size=self._config_int("sync_write_queue_size"),
            db_path=self._db_path,
            write_timeout_ms=self._config_int("command_timeout_write_ms"),
        )

    # ------------------------------------------------------------------ prefetch

    def prefetch(self, query: str = "", **kwargs: Any) -> str:  # noqa: ARG002
        """Recall via ``cli_runner``, cache the hits, return a formatted string.

        Returns the empty string when prefetching is disabled, ICM is
        unavailable, or any failure is caught at the hooks-helper boundary.
        """
        if not self._config_bool("prefetch_enabled"):
            return ""
        if not self.is_available() or self._db_path is None:
            return ""
        try:
            hooks.run_prefetch(
                query=query,
                db_path=self._db_path,
                limit=self._config_int("recall_limit"),
                timeout_ms=self._config_int("command_timeout_read_ms"),
                cache=self._prefetch_cache,
            )
        except Exception as exc:  # belt-and-braces; helper already swallows
            logger.warning(
                "prefetch: outer boundary caught", extra={"err": repr(exc)}
            )
            return ""
        self._latest_prefetch_key = hash(query)
        # ``format_block`` returns ``""`` on empty hits — no extra short-circuit.
        return hooks.format_block(
            cache=self._prefetch_cache,
            latest_key=self._latest_prefetch_key,
            recall_limit=self._config_int("recall_limit"),
        )

    # ------------------------------------------------------------------ system_prompt_block

    def system_prompt_block(self, **kwargs: Any) -> str:  # noqa: ARG002
        """Format the cached prefetch hits into a prompt-ready block.

        Reads the cache only — never invokes ``cli_runner`` (NFR-PERF-4).
        Disabled prefetch / empty cache → ``""``.
        """
        if not self._config_bool("prefetch_enabled"):
            return ""
        try:
            return hooks.format_block(
                cache=self._prefetch_cache,
                latest_key=self._latest_prefetch_key,
                recall_limit=self._config_int("recall_limit"),
            )
        except Exception as exc:  # defensive boundary
            logger.warning(
                "system_prompt_block: outer boundary caught",
                extra={"err": repr(exc)},
            )
            return ""

    # ------------------------------------------------------------------ sync_turn

    def sync_turn(
        self,
        user_content: str = "",
        assistant_content: str = "",
        **kwargs: Any,  # noqa: ARG002 — Hermes contract may pass extra kwargs.
    ) -> None:
        """Detect triggers from the just-completed turn and enqueue writes.

        Returns within p95 < 5 ms (NFR-PERF-1). Drop-on-full overflow with
        one WARNING per burst (FR15). Never raises.
        """
        if not self._ensure_worker():
            return
        try:
            hooks.submit_triggers(
                self._worker_state,
                user_content=user_content,
                assistant_content=assistant_content,
                project=None,
                every_n_turns=self._config_int("periodic_progress_every_n_turns"),
            )
        except Exception as exc:  # outer boundary — must not raise into the turn
            logger.warning(
                "sync_turn: outer boundary caught",
                extra={"err": repr(exc)},
            )

    # ------------------------------------------------------------------ on_session_end

    def on_session_end(
        self,
        messages: Any = None,  # noqa: ARG002 — Hermes contract may pass extra args.
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Drain the queue up to ``session_end_grace_ms``; drop the rest with WARN.

        Does NOT join the worker thread — daemon threads exit at interpreter
        shutdown.
        """
        try:
            hooks.drain_with_grace(
                self._worker_state,
                grace_ms=self._config_int("session_end_grace_ms"),
            )
        except Exception as exc:  # defensive boundary
            logger.warning(
                "on_session_end: outer boundary caught",
                extra={"err": repr(exc)},
            )
