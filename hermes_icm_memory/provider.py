"""``IcmMemoryProvider`` — Hermes-side memory provider class.

v0.3 — lifecycle-only. The provider exposes the four Hermes lifecycle hooks
(``prefetch``, ``system_prompt_block``, ``sync_turn``, ``on_session_end``)
plus a ``shutdown()`` no-op so hermes-agent's memory_manager doesn't log
``'IcmMemoryProvider' object has no attribute 'shutdown'`` at gateway
restart. The LLM-facing ``icm_memory_*`` tools are exposed by hermes-native
``mcp_servers.icm:`` config (hermes-agent v0.3.0+) — this provider no
longer carries an LLM tool surface (AD-19).

Architecture invariants:

* **AD-12** — this module MUST NOT import ``subprocess`` (S11 AST test enforces).
* **AD-13** — module-level ``logger = logging.getLogger(__name__)``; never root.
  WARNINGs include the exception text inline via ``%r`` *and* in
  ``extra={...}`` so default + JSON log formatters both surface it (AC8).
* **AD-07** — every public method catches at the boundary and returns the
  documented degraded shape. No exception ever propagates into a Hermes turn.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Final

import yaml

from . import cli_runner, config, hooks, mapping

__all__ = ["IcmMemoryProvider"]

logger = logging.getLogger(__name__)

#: Filename of the JSON sidecar persisted under ``<hermes_home>/icm/``.
_CONFIG_SIDECAR_NAME: Final[str] = "config.json"

#: Manifest name (from ``plugin.yaml``). Hermes stores per-plugin config under
#: ``plugins.<this-key>.*`` in the hermes_home ``config.yaml``; v0.4.5
#: ``_load_hermes_plugin_config`` reads that section at initialize time so
#: operator-set keys like ``use_embeddings: false`` actually take effect
#: instead of being silently overridden by the schema default.
_PLUGIN_MANIFEST_NAME: Final[str] = "hermes-icm-memory"

#: Frozen architecture §10.1 defaults, materialised once. Avoids the per-call
#: deep-copy in :meth:`IcmMemoryProvider._cfg` (otherwise ``sync_turn`` would
#: pay an O(N) copy of the schema list every turn).
_DEFAULT_CONFIG: Final[dict[str, Any]] = {
    entry["key"]: entry["default"] for entry in config.get_default_schema()
}


# ---------------------------------------------------------------- v0.4.3 indicator
#
# Module-level shared state for the user-visible per-turn indicator footer
# (``📚 N · 💾 topic``). Module-level (not instance-level) because under the
# v0.4.3 dual-load arrangement (kind=standalone) there are two
# ``IcmMemoryProvider`` instances in the gateway process — one owned by
# ``memory_manager`` (receives prefetch/sync_turn), one owned by the general
# ``PluginManager`` (whose ``transform_llm_output`` hook is the one that
# actually fires per turn). They share this one dict so a recall captured by
# the memory_manager instance lands in the footer appended by the
# PluginManager instance.

#: v0.5.2 — keyed by ``session_id`` to prevent cross-session contamination
#: when two concurrent sessions' prefetches fire in rapid succession (the
#: hair-iron turn + a system-note interrupted-turn observed live in v0.5.1).
#: Pre-v0.5.2 the second prefetch's counts overwrote the first, so the
#: transform_llm_output hook fired for session A read session B's stale
#: numbers (footer showed ``📚 —`` for a turn that had 3 hair-iron hits).
#:
#: An empty string key is the bucket for "session_id unknown" (best-effort
#: fallback so a missing kwarg doesn't crash; should never happen in
#: practice since Hermes passes session_id everywhere).
_INDICATOR_STATE: dict[str, dict[str, Any]] = {}


def _indicator_slot(session_id: str | None) -> dict[str, Any]:
    """Get or create the per-session state slot. Centralises the default shape."""
    key = session_id or ""
    return _INDICATOR_STATE.setdefault(
        key, {"recall_count": 0, "last_save_topic": None, "recall_topic": None},
    )


def _capture_recall_count(
    count: int, *, session_id: str | None = None, topic: str | None = None,
) -> None:
    """Producer hook called by ``IcmMemoryProvider.prefetch`` after run_recall.

    v0.5.3 — also captures the inferred topic (if any) so the user-visible
    footer can show it next to the 📚 count (operator request).
    """
    slot = _indicator_slot(session_id)
    slot["recall_count"] = count
    slot["recall_topic"] = topic


def _capture_save_topic(topic: str, *, session_id: str | None = None) -> None:
    """Producer hook called by ``hooks.submit_triggers`` / classifier worker."""
    if topic:
        _indicator_slot(session_id)["last_save_topic"] = topic


def _render_indicator_footer(
    recall: int, save: str | None, *, recall_topic: str | None = None,
) -> str:
    """Build the literal footer line the user sees at the bottom of replies.

    Format:
        ``📚 N <topic>``           — recall fired, topic inferred
        ``📚 N``                   — recall fired, no topic inferred
        ``📚 N <topic> · 💾 <save>``  — both halves
        ``💾 <save>``              — save only, no recall
        ``📚 —``                  — heartbeat (nothing fired) — always shown
                                    on silent turns so the user has a per-
                                    turn liveness signal.
    """
    parts: list[str] = []
    if recall > 0:
        if recall_topic:
            parts.append(f"📚 {recall} {recall_topic}")
        else:
            parts.append(f"📚 {recall}")
    if save:
        parts.append(f"💾 {save}")
    return " · ".join(parts) if parts else "📚 —"


#: Matches a single-line indicator footer the model (or this hook) may have
#: previously appended — used by :func:`_do_indicator_transform` to strip
#: any stale instance before appending the freshly-rendered one.
#:
#: v0.4.6 — the v0.4.5 exact-match de-dupe was insufficient because the
#: ``system_prompt_block`` directive renders BEFORE prefetch fires, so the
#: model copies a ``📚 —`` heartbeat into its reply while the hook (which
#: runs AFTER prefetch) sees the real ``📚 N`` count. Exact-match doesn't
#: catch the mismatch — both footers end up in the reply. Switching the
#: hook to be the single source of truth (strip-then-append) fixes that
#: AND demotes the fallback directive to a no-op when the hook is wired.
_FOOTER_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"\n*📚[^\n]*$",
)


def _do_indicator_transform(
    response_text: str = "",
    session_id: str = "",
    **_kwargs: Any,
) -> str | None:
    """``transform_llm_output`` plugin-hook implementation.

    Reads the per-session slot in :data:`_INDICATOR_STATE` (v0.5.2 — keyed
    by ``session_id`` so concurrent sessions don't contaminate each other's
    footers), strips any trailing ``📚 …`` line, and appends a fresh one
    rendered from the snapshot. Resets that session's state. Returns
    ``None`` when the response is empty.
    """
    if not response_text:
        return None
    slot = _indicator_slot(session_id)
    snapshot_recall = int(slot.get("recall_count", 0) or 0)
    snapshot_save = slot.get("last_save_topic")
    snapshot_recall_topic = slot.get("recall_topic")
    # v0.5.4 — observability. INFO log fires on EVERY transform invocation
    # so we can confirm (a) the hook is actually being called, and (b) the
    # session_id Hermes passes here matches the session_id provider.prefetch
    # used to write the slot. A footer mismatch with the prefetch log was
    # otherwise indistinguishable from "hook silently never fired".
    logger.info(
        "transform_llm_output: session_id=%r known_slots=%d "
        "snapshot=(recall=%d, topic=%r, save=%r)",
        session_id, len(_INDICATOR_STATE),
        snapshot_recall, snapshot_recall_topic, snapshot_save,
    )
    # Reset THIS session's slot before formatting so the next-turn snapshot
    # starts clean even if the format step raises.
    slot["recall_count"] = 0
    slot["last_save_topic"] = None
    slot["recall_topic"] = None
    footer = _render_indicator_footer(
        snapshot_recall, snapshot_save, recall_topic=snapshot_recall_topic,
    )
    cleaned = _FOOTER_LINE_RE.sub("", response_text).rstrip()
    return f"{cleaned}\n\n{footer}"


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
        self._hermes_home: Path | None = None
        self._hermes_config: dict[str, Any] = {}
        # ``(session_id, str(hermes_home), profile)`` — set by initialize and
        # used to detect an idempotent re-init with the same arguments. Also
        # serves as the "have we initialised at all" flag (None == no).
        self._init_args: tuple[str, str, str | None] | None = None
        # S08 hot-path state: prefetch cache + worker bundle.
        self._prefetch_cache: dict[int, list[dict[str, Any]]] = {}
        self._latest_prefetch_key: int | None = None
        self._worker_state: hooks.WorkerState = hooks.WorkerState()
        # v0.5.1 — keyword→topic index built from icm topics list at
        # initialize time. Empty dict (not None) when no topics fetched
        # or when the icm corpus is empty, so per-prefetch lookup stays
        # branch-free at the call site.
        self._topic_keyword_map: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ tool schemas

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas this provider exposes.

        The ICM provider doesn't expose any agent-callable tools
        — all memory operations are handled internally via prefetch/sync_turn.
        """
        return []

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
            logger.warning(
                "is_available probe raised: %r", exc, extra={"err": repr(exc)}
            )
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
        """Record the session and (when opted in) resolve a per-profile DB path.

        v0.1.1 default behaviour (``isolated=False``) — the brief's
        "shared memory with editors" promise: ``_db_path`` stays ``None`` and
        ``cli_runner`` omits ``--db`` so ``icm`` uses its OS-canonical default
        DB (the same file Claude Code, Cursor, OpenCode, etc. share).

        Opt-in (``isolated=True``) — restores v0.1.0 behaviour: resolves
        ``<hermes_home>/icm/<profile>.db`` and ensures the parent dir exists
        via :func:`config.mkdir_parent`.

        Idempotent on the same ``(session_id, hermes_home, profile)`` triple.

        On ``OSError`` from :func:`config.mkdir_parent` under ``isolated=True``
        (read-only filesystem), logs a WARNING, sets ``_available = False``
        (sticky), records the failed args so re-calls with the same triple
        stay no-ops, and returns without raising — failure-mode matrix §6.3
        row 8. Default-shared mode never reaches the OSError branch.
        """
        args_key = (session_id, str(hermes_home), profile)
        is_repeat = self._init_args == args_key

        if not is_repeat:
            if self._config_bool("isolated"):
                try:
                    db_path = config.resolve_db_path(hermes_home, profile)
                    config.mkdir_parent(db_path)
                except OSError as exc:
                    logger.warning(
                        "initialize failed: hermes_home not writable; "
                        "provider self-disabling: %r",
                        exc,
                        extra={"hermes_home": str(hermes_home), "err": repr(exc)},
                    )
                    self._available = False
                    self._init_args = args_key
                    return
                self._db_path = db_path
            # else: default-shared — ``_db_path`` stays ``None`` and ``cli_runner``
            # omits ``--db`` so ``icm`` uses its canonical OS-default DB.

            self._session_id = session_id
            self._init_args = args_key
            self._hermes_home = Path(hermes_home)
            self._hermes_config = self._read_hermes_config()
            # v0.4.5 — merge operator-set keys from plugins.<name>.* in
            # config.yaml so settings like ``use_embeddings: false`` actually
            # take effect on this instance (pre-v0.4.5 they were silently
            # ignored — see _load_hermes_plugin_config docstring).
            self._load_hermes_plugin_config()

        # v0.4.4 — ALWAYS ensure the warm MCP daemon is up, even on the
        # idempotent re-init path. Pre-v0.4.4 this call lived inside the
        # ``not is_repeat`` branch, which meant any code path that nulled
        # ``cli_runner._client`` between turns (sub-agent shutdown, the
        # gateway's review_agent flow, etc.) would leave subsequent
        # ``provider.prefetch`` calls raising
        # ``ICMConnectionError('MCP client not started')`` because re-init
        # short-circuited via ``args_key`` match. ``mcp_start`` is itself
        # idempotent (no-op when ``_client`` is already set) so this is
        # cheap on the happy path.
        try:
            cli_runner.mcp_start(
                db_path=self._db_path,
                use_embeddings=self._config_bool("use_embeddings"),
            )
        except Exception as exc:
            logger.warning(
                "initialize: MCP daemon start failed: %r — provider self-disabling",
                exc,
                extra={"err": repr(exc)},
            )
            self._available = False

        # v0.5.1 — build the keyword→topic index from the live corpus so
        # prefetch can add ``-t <topic>`` when the user's query overlaps a
        # specific project. Done lazily once per session (per initialize
        # call); refreshes naturally on the next session start. Topics
        # path uses the warm MCP daemon (not the v0.5.0 CLI subprocess
        # path — only RECALL is broken in MCP, not topics).
        try:
            topics_response = cli_runner.run_topics(
                db_path=self._db_path,
                timeout_ms=self._config_int("command_timeout_read_ms"),
            )
            # v0.5.2 — strip the ': N memories' suffix that ICM's MCP
            # topics tool appends to each topic string (e.g.
            # ``context-hair-iron: 3 memories`` → ``context-hair-iron``).
            # Pre-v0.5.2 the suffixed string was passed both to the
            # keyword index and to ``-t <topic>`` in recall; icm's filter
            # accepted it only via substring match. Cleaning here gives
            # us exact-match filtering and a tidy ``inferred_topic=`` log.
            topic_names = [
                str(t["topic"]).split(":", 1)[0].strip()
                for t in topics_response
                if isinstance(t, dict) and t.get("topic")
            ]
            topic_names = [t for t in topic_names if t]  # drop empties post-strip
            self._topic_keyword_map = mapping.build_topic_keyword_map(topic_names)
            logger.info(
                "initialize: built topic_keyword_map from %d topics → %d keywords",
                len(topic_names), len(self._topic_keyword_map),
            )
        except Exception as exc:
            logger.debug(
                "initialize: topic_keyword_map build failed (non-fatal): %r",
                exc,
                extra={"err": repr(exc)},
            )
            self._topic_keyword_map = {}

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
                "save_config: could not persist sidecar: %r",
                exc,
                extra={"hermes_home": str(hermes_home), "err": repr(exc)},
            )
            return {"error": f"could not persist config: {exc}"}

        return None

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

    def _config_str(self, key: str) -> str:
        """Read a string-shaped config value (enum or string-typed key)."""
        value = self._config.get(key, _DEFAULT_CONFIG[key])
        return str(value) if value is not None else ""

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
        """Lazy-spawn / respawn the worker; returns False if writes are disabled.

        v0.4 — ``_db_path`` may be ``None`` (default-shared mode); the warm
        MCP daemon started in :meth:`initialize` owns the DB at write time,
        so the worker spawns regardless. The legacy v0.1.1 guard against
        ``_db_path is None`` was removed because it silently no-op'd
        ``sync_turn`` for every user running the recommended ``isolated:
        false`` config (README "Known limitations" §0.3).
        """
        return hooks.ensure_worker(
            self._worker_state,
            queue_size=self._config_int("sync_write_queue_size"),
            db_path=self._db_path,
            write_timeout_ms=self._config_int("command_timeout_write_ms"),
        )

    def _read_hermes_config(self) -> dict[str, Any]:
        """Parse the Hermes config.yaml into a dict.

        Returns ``{}`` on any failure (file missing, bad YAML).
        """
        if not self._hermes_home:
            return {}
        config_path = self._hermes_home / "config.yaml"
        try:
            with open(config_path) as f:
                parsed = yaml.safe_load(f)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            logger.debug(
                "hermes_config: could not read: %r",
                exc,
                extra={"path": str(config_path), "err": repr(exc)},
            )
            return {}

    def _load_hermes_plugin_config(self) -> None:
        """Merge ``plugins.hermes-icm-memory.*`` from Hermes config.yaml into
        ``self._config`` (v0.4.5).

        Pre-v0.4.5 the plugin only consulted operator-set values via
        :meth:`save_config` (which writes a JSON sidecar but is never auto-
        called by Hermes on plugin load). Result: an operator who put
        ``plugins.hermes-icm-memory.use_embeddings: false`` in their
        config.yaml saw their setting silently ignored — the plugin always
        used the schema default ``True`` and spawned ``icm serve`` without
        ``--no-embeddings``, breaking recall quality on Pi-class hosts
        (where the multilingual-e5-base ONNX model can't realistically be
        loaded). This helper closes that gap.

        Behaviour:

        - Reads :attr:`self._hermes_config['plugins'][<manifest-name>]`.
        - Filters to keys that exist in the schema (silently drops typos /
          unknown keys to keep the boundary tight).
        - In-place ``self._config.update(...)``; subsequent
          :meth:`save_config` calls keep working as before and override.
        - Tolerant of missing / malformed sections — returns silently and
          relies on schema defaults.
        """
        if not isinstance(self._hermes_config, dict):
            return
        plugins_section = self._hermes_config.get("plugins")
        if not isinstance(plugins_section, dict):
            return
        my_section = plugins_section.get(_PLUGIN_MANIFEST_NAME)
        if not isinstance(my_section, dict):
            return
        merged = {k: v for k, v in my_section.items() if k in _DEFAULT_CONFIG}
        if merged:
            self._config.update(merged)
            logger.info(
                "config: loaded %d key(s) from plugins.%s.* in hermes config.yaml: %s",
                len(merged), _PLUGIN_MANIFEST_NAME, sorted(merged.keys()),
            )

    def _resolve_classifier_config(self) -> dict[str, str] | None:
        """Resolve classifier endpoint, model, and API key from Hermes config.

        Priority:
        1. ``classifier_endpoint`` plugin config — overrides everything when set
        2. ``classifier_provider`` + ``classifier_model`` plugin config
        3. Fall back to main ``model.provider`` + ``model.default`` from config.yaml

        Returns ``{"endpoint": ..., "model": ..., "api_key": ...}`` or ``None``
        if resolution fails.
        """
        # Step 1: explicit endpoint override
        explicit_endpoint = self._config_str("classifier_endpoint")
        explicit_model = self._config_str("classifier_model")

        if explicit_endpoint and explicit_model:
            api_key = os.environ.get("HERMES_CLASSIFIER_API_KEY", "")
            if not api_key:
                api_key = os.environ.get("OPENROUTER_API_KEY", "")
            return {
                "endpoint": explicit_endpoint,
                "model": explicit_model,
                "api_key": api_key,
            }

        # Step 2: resolve from Hermes provider config
        cfg = self._hermes_config
        main_model = cfg.get("model") or {}

        provider = self._config_str("classifier_provider") or main_model.get("provider", "")
        model = self._config_str("classifier_model") or main_model.get("default", "")

        if not provider or not model:
            logger.debug(
                "classifier: could not resolve provider/model",
                extra={"provider": provider, "model": model},
            )
            return None

        # Step 3: find the base_url for this provider
        base_url = ""
        # Check if it's the main provider
        main_provider = main_model.get("provider", "")
        if provider == main_provider:
            base_url = main_model.get("base_url", "")
        else:
            provider_config = (cfg.get("providers") or {}).get(provider, {})
            base_url = provider_config.get("base_url", "")

        if not base_url:
            logger.debug(
                "classifier: no base_url for provider %r",
                provider,
                extra={"provider": provider},
            )
            return None

        # Step 4: construct the chat completions endpoint URL
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint + "/chat/completions"

        # Step 5: find the API key
        api_key = ""
        # Try multiple env var patterns — provider names in config don't always
        # match the env var convention (e.g. "opencode" → OPENCODE_ZEN_API_KEY)
        provider_upper = provider.upper().replace("-", "_")
        candidates = [
            f"{provider_upper}_API_KEY",
            f"{provider_upper}_ZEN_API_KEY",
            f"{provider_upper}_GO_API_KEY",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
        ]
        for candidate in candidates:
            val = os.environ.get(candidate, "")
            if val:
                api_key = val
                logger.debug(
                    "classifier: found API key via %s", candidate,
                    extra={"provider": provider, "env_var": candidate},
                )
                break

        return {
            "endpoint": endpoint,
            "model": model,
            "api_key": api_key,
        }

    def _ensure_classifier(self) -> bool:
        """Lazy-spawn / respawn the classifier; returns False if misconfigured."""
        # Classifier needs the write queue to exist first.
        if self._worker_state.write_queue is None:
            return False

        resolved = self._resolve_classifier_config()
        if resolved is None:
            logger.debug("classifier: disabled — could not resolve config")
            return False

        endpoint = resolved["endpoint"]
        model = resolved["model"]
        api_key = resolved["api_key"]

        if not endpoint or not model:
            logger.debug(
                "classifier: disabled — endpoint or model empty",
                extra={"endpoint": endpoint, "model": model},
            )
            return False

        return hooks.ensure_classifier(
            self._worker_state,
            classify_queue_size=self._config_int("classify_queue_size"),
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout_s=self._config_int("classifier_timeout_ms") / 1000.0,
        )

    # ------------------------------------------------------------------ prefetch

    def prefetch(self, query: str = "", **kwargs: Any) -> str:  # noqa: ARG002
        """Recall via ``cli_runner``, cache the hits, return a formatted string.

        Returns the empty string when prefetching is disabled, ICM is
        unavailable, or any failure is caught at the hooks-helper boundary.
        """
        if not self._config_bool("prefetch_enabled"):
            return ""
        if not self.is_available():
            return ""
        recall_limit = self._config_int("recall_limit")
        # v0.5.3 — when the query's tokens overlap a real topic name in
        # the corpus, REPLACE the recall query with just those matched
        # keywords (and add ``-t <topic>``). ICM scores entries against
        # the full natural-language query first, THEN applies ``-t``;
        # the stopword-heavy form ("whats going on with hair iron")
        # craters topic-tagged entries' scores below threshold so the
        # filter returns 0. Substituting "hair iron" as the recall query
        # lets icm score the right entries highest before the filter
        # applies. This is NOT a generic stopword strip — only words
        # that explicitly mapped to a real topic name are used.
        # When no topic is inferred, the original query passes through
        # unchanged (same as v0.5.0/v0.5.1).
        inferred_topic, matched_keywords = mapping.infer_topic_and_keywords(
            query, self._topic_keyword_map,
        )
        recall_query = (
            " ".join(matched_keywords) if (inferred_topic and matched_keywords) else query
        )
        # v0.1.1: ``_db_path is None`` is a legitimate "use icm canonical
        # default DB" sentinel (default-shared mode), not a "not initialized"
        # signal. The ``_init_args`` check upstream handles the latter.
        try:
            hits = hooks.run_prefetch(
                query=recall_query,
                db_path=self._db_path,
                limit=recall_limit,
                timeout_ms=self._config_int("command_timeout_read_ms"),
                cache=self._prefetch_cache,
                use_embeddings=self._config_bool("use_embeddings"),
                topic=inferred_topic,
            )
        except Exception as exc:  # AD-07 boundary — helper already swallows
            logger.warning(
                "prefetch: outer boundary caught: %r",
                exc,
                extra={"err": repr(exc)},
            )
            return ""
        # v0.4.2 — feed the system_prompt_block indicator footer with the
        # capped hit count so the user sees a `📚 N` heartbeat per turn.
        # v0.4.3 — ALSO write through to the module-level _INDICATOR_STATE so
        # the dual-loaded transform_llm_output hook can read it (the hook
        # fires on the standalone-loaded instance, not this memory_manager
        # one). The per-instance ``_worker_state`` write below is retained
        # only for the fallback directive path in ``system_prompt_block``.
        capped_count = len(hits[:recall_limit])
        self._worker_state.recent_recall_count = capped_count
        _capture_recall_count(
            capped_count, session_id=self._session_id, topic=inferred_topic,
        )
        # v0.4.7 — observability. Surface BOTH the original and the
        # stopword-stripped query alongside the hit count and top topics
        # so operators can diagnose either a bad stripped query or an
        # ICM-side ranking miss without having to instrument the plugin.
        _q_preview = (query[:120] + "…") if len(query) > 120 else query
        _rq_preview = (
            (recall_query[:120] + "…") if len(recall_query) > 120 else recall_query
        )
        _top_topics = ", ".join(
            str(h.get("topic") or "?") for h in hits[: min(3, recall_limit)]
        ) or "none"
        logger.info(
            "prefetch: query=%r recall_query=%r inferred_topic=%r "
            "raw_hits=%d capped=%d top_topics=[%s]",
            _q_preview, _rq_preview, inferred_topic,
            len(hits), capped_count, _top_topics,
        )
        # Bound the cache to the latest entry. ``system_prompt_block`` only
        # reads ``_latest_prefetch_key`` (NFR-PERF-4), so older entries are
        # dead weight that would otherwise leak monotonically across the
        # gateway's lifetime. v0.4.8 — cache key uses the STRIPPED query so
        # it lines up with the key ``hooks.run_prefetch`` writes under
        # (it hashes whatever query is passed in, which is ``recall_query``
        # post-strip).
        latest_key = hash(recall_query)
        self._prefetch_cache = {latest_key: self._prefetch_cache.get(latest_key, [])}
        self._latest_prefetch_key = latest_key
        # ``format_block`` returns ``""`` on empty hits — no extra short-circuit.
        return hooks.format_block(
            cache=self._prefetch_cache,
            latest_key=latest_key,
            recall_limit=recall_limit,
        )

    # ------------------------------------------------------------------ system_prompt_block

    def system_prompt_block(self, **kwargs: Any) -> str:  # noqa: ARG002
        """Format the cached prefetch hits into a prompt-ready block.

        Reads the cache only — never invokes ``cli_runner`` (NFR-PERF-4).
        Disabled prefetch / empty cache → ``""``.

        v0.4.2 — also appends a user-visible indicator footer directive
        (📚 N · 💾 topic) that the LLM is asked to copy verbatim to the end
        of its reply. Heartbeats with ``📚 —`` on silent turns so the user
        always sees evidence the plugin is alive.
        """
        blocks: list[str] = []
        ws = self._worker_state

        # Snapshot indicator state BEFORE Part 1 drains recent_stores. Last
        # save in the buffer wins the 💾 slot (most recent = most relevant).
        recall_count = ws.recent_recall_count if ws else 0
        last_save_topic = ws.recent_stores[-1][0] if (ws and ws.recent_stores) else None

        # Part 1: recently stored memories (from async classifier + regex path).
        if ws and ws.recent_stores:
            store_lines = ["🧠 New memories stored:"]
            for topic, content in ws.recent_stores:
                store_lines.append(f"  - [{topic}] {content}")
            blocks.append("\n".join(store_lines))
            ws.recent_stores.clear()

        # Part 2: recalled memories from prefetch cache.
        if self._config_bool("prefetch_enabled"):
            try:
                recalled = hooks.format_block(
                    cache=self._prefetch_cache,
                    latest_key=self._latest_prefetch_key,
                    recall_limit=self._config_int("recall_limit"),
                )
                if recalled:
                    blocks.append(recalled)
            except Exception as exc:  # defensive boundary
                logger.warning(
                    "system_prompt_block: outer boundary caught: %r",
                    exc,
                    extra={"err": repr(exc)},
                )

        # Part 3: indicator footer directive. Always fires (heartbeat on
        # silent turns) so the user has a per-turn liveness signal.
        blocks.append(self._render_indicator_directive(recall_count, last_save_topic))

        # Reset per-turn counters now that the directive carries them.
        if ws:
            ws.recent_recall_count = 0

        return "\n\n".join(blocks) if blocks else ""

    @staticmethod
    def _render_indicator_directive(recall_count: int, save_topic: str | None) -> str:
        """Build the literal indicator block the LLM is asked to echo verbatim.

        v0.4.3 — fallback path only. The primary indicator path is the
        :func:`_do_indicator_transform` ``transform_llm_output`` hook, which
        appends the footer programmatically and detects already-appended
        footers (so this directive being followed doesn't cause a double).
        Kept as belt-and-suspenders for code paths the hook can't reach
        (e.g. streamed partial deliveries that bypass the final-response
        transform).
        """
        footer = _render_indicator_footer(recall_count, save_topic)
        return (
            "════════════════════════════════════════════════════════════════\n"
            "MANDATORY OUTPUT FORMAT — MEMORY INDICATOR FOOTER\n"
            "════════════════════════════════════════════════════════════════\n"
            "Your reply MUST end with the following line on its own line, "
            "copied character-for-character (no paraphrase, no translation, "
            "no extra formatting, no surrounding code fence):\n"
            f"\n    {footer}\n\n"
            "This is non-negotiable. If you omit this footer the user "
            "loses their memory-system liveness signal."
        )

    # ------------------------------------------------------------------ sync_turn

    def sync_turn(
        self,
        user_content: str = "",
        assistant_content: str = "",
        **kwargs: Any,
    ) -> None:
        """Detect triggers from the just-completed turn and enqueue writes.

        Returns within p95 < 5 ms (NFR-PERF-1). Drop-on-full overflow with
        one WARNING per burst (FR15). Never raises.
        """
        classifier_enabled = self._config_bool("classifier_enabled")

        if not self._ensure_worker():
            return

        if classifier_enabled and not self._ensure_classifier():
            # If classifier is misconfigured, fall through with classifier_enabled=False
            # so regex path runs instead.
            classifier_enabled = False

        # v0.5.2 — Hermes always passes session_id in kwargs; thread it through
        # so save captures land in the right per-session indicator slot.
        # Falls back to ``self._session_id`` (set by initialize) when the
        # kwarg is missing — e.g. test callers that don't simulate Hermes
        # fully.
        session_id = str(kwargs.get("session_id") or self._session_id or "")

        try:
            hooks.submit_triggers(
                self._worker_state,
                user_content=user_content,
                assistant_content=assistant_content,
                project=None,
                every_n_turns=self._config_int("periodic_progress_every_n_turns"),
                classifier_enabled=classifier_enabled,
                session_id=session_id,
            )
        except Exception as exc:  # outer boundary — must not raise into the turn
            logger.warning(
                "sync_turn: outer boundary caught: %r",
                exc,
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
                "on_session_end: outer boundary caught: %r",
                exc,
                extra={"err": repr(exc)},
            )

    # ------------------------------------------------------------------ shutdown

    def shutdown(self) -> None:
        """Per-instance cleanup. Does NOT stop the MCP daemon.

        v0.4.4 — the warm ``icm serve`` daemon spawned by
        :func:`cli_runner.mcp_start` is a PROCESS-wide singleton shared by
        every ``IcmMemoryProvider`` instance in the gateway (the
        ``review_agent`` flow creates short-lived secondary instances). If
        per-instance shutdown killed the daemon, the next call to
        ``provider.prefetch`` on the main agent would see
        ``cli_runner._client is None`` and raise
        ``ICMConnectionError('MCP client not started')`` — that was the
        v0.4.3 silent-recall regression.

        Daemon teardown is registered via :func:`atexit.register` inside
        :mod:`hermes_icm_memory.cli_runner` so the subprocess is closed
        cleanly when the gateway process exits — no per-instance call
        needed.

        Kept as a no-op so subclasses can override and so future per-instance
        resources (file handles, threads owned by this instance only, etc.)
        have a hook to clean up if added.
        """
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
