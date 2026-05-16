"""Tests for ``hermes_icm_memory.provider.IcmMemoryProvider`` (S07).

Strict TDD: this file lands first (RED), then ``provider.py`` implements
exactly what these cases assert (GREEN). The 14 cases trace 1-to-1 to
ACs 1–14 of story 3.1; ACs 15 (no-subprocess invariant) and 16 (S11
forward-compat skips light up later) are validated by the existing
``tests/test_no_subprocess_outside_cli_runner.py`` and
``tests/test_no_network_calls.py`` invariant suites.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
from pathlib import Path
from typing import Any, NoReturn

import pytest

from hermes_icm_memory import cli_runner, config
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- AC1: name ---------------------------------------------------------


def test_name_is_icm() -> None:
    """AC1 — ``provider.name`` is the literal ``"icm"``."""
    assert IcmMemoryProvider().name == "icm"


# ---------- AC2/AC3/AC4: is_available + caching -------------------------------


def test_is_available_true_when_icm_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2 — ``shutil.which("icm")`` truthy → ``True``."""
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/local/bin/icm" if name == "icm" else None
    )
    assert IcmMemoryProvider().is_available() is True


def test_is_available_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 — ``shutil.which`` returns ``None`` → ``False``."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert IcmMemoryProvider().is_available() is False


def test_is_available_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4 — second call must not invoke ``shutil.which`` again."""
    calls: list[str] = []

    def _which(name: str) -> str | None:
        calls.append(name)
        return "/usr/local/bin/icm"

    monkeypatch.setattr(shutil, "which", _which)
    provider = IcmMemoryProvider()
    assert provider.is_available() is True
    assert provider.is_available() is True
    assert calls == ["icm"], f"expected exactly one which() call, got {calls!r}"


# ---------- AC5: no socket during is_available --------------------------------


def _raise_on_socket(*args: object, **kwargs: object) -> NoReturn:
    raise RuntimeError("network forbidden during plugin lifecycle (NFR-SEC-1)")


def test_is_available_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5 — patching ``socket.socket`` to raise must not break ``is_available()``."""
    monkeypatch.setattr(socket, "socket", _raise_on_socket)
    # is_available must work whether or not icm is on PATH; the contract is
    # "no network", not "always True". Force a deterministic path via which patch.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/icm")
    IcmMemoryProvider().is_available()


# ---------- AC6/AC7/AC8: initialize -------------------------------------------


def test_initialize_default_shared_db_path_stays_none(tmp_hermes_home: Path) -> None:
    """v0.1.1 — default config keeps ``_db_path = None`` (brief's shared-with-editors).

    ``initialize`` records ``_session_id`` + ``_init_args`` but performs no
    path resolution and no filesystem touch under ``<hermes_home>/icm/``. This
    is the brief's value-prop default — the plugin shells out to ``icm`` with
    no ``--db`` so icm uses its canonical OS-default DB (the same file Claude
    Code, Cursor, OpenCode, etc. share).
    """
    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")

    assert provider._db_path is None
    assert provider._session_id == "s1"
    assert provider._init_args == ("s1", str(tmp_hermes_home), "work")
    # No filesystem touch under hermes_home/icm/ in default-shared mode.
    assert not (tmp_hermes_home / "icm").exists()


def test_initialize_isolated_resolves_db_path(tmp_hermes_home: Path) -> None:
    """v0.1.1 — opt-in ``isolated=True`` restores the v0.1.0 per-profile DB path."""
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")

    expected = config.resolve_db_path(tmp_hermes_home, "work")
    assert provider._db_path == expected
    assert provider._session_id == "s1"


def test_initialize_creates_parent_dir_when_isolated(tmp_hermes_home: Path) -> None:
    """v0.1.1 — under ``isolated=True``, ``<hermes_home>/icm/`` is created (no .db file).

    Default-shared mode performs no mkdir; this AC fires only on opt-in.
    """
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    icm_dir = tmp_hermes_home / "icm"
    assert icm_dir.is_dir(), f"expected {icm_dir} to be created by initialize"
    assert not (icm_dir / "default.db").exists(), "plugin must not invoke icm init"


def test_initialize_idempotent_isolated(
    tmp_hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8 — second initialize() call with same args performs no extra mkdir.

    Pinned under ``isolated=True`` because mkdir only runs in the isolated
    branch — default-shared mode skips mkdir entirely and is trivially
    idempotent. The (session_id, hermes_home, profile) guard still fires in
    both modes.
    """
    real_mkdir = Path.mkdir
    calls: list[Path] = []

    def _counting_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        calls.append(self)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _counting_mkdir)

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")
    first_count = len(calls)
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")
    second_count = len(calls)
    assert second_count == first_count, (
        f"expected idempotent re-init (same args) to skip mkdir; "
        f"first={first_count}, second={second_count}"
    )


def test_initialize_idempotent_default_shared(tmp_hermes_home: Path) -> None:
    """v0.1.1 — default-shared re-init guard fires (no rework on duplicate call)."""
    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile=None)
    first_args = provider._init_args
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile=None)
    assert provider._init_args == first_args
    assert provider._db_path is None


def test_initialize_with_unwritable_hermes_home_self_disables_when_isolated(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC9 — OSError from mkdir under ``isolated=True`` → WARNING + self-disable.

    Default-shared mode never touches the filesystem in ``initialize`` so the
    self-disable branch is unreachable there; this AC pins the v0.1.0 row 8
    failure-mode behavior under the new opt-in.
    """

    def _raise_oserror(_db_path: Path) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(config, "mkdir_parent", _raise_oserror)
    # Pretend icm is on PATH so any non-self-disable bug would have surfaced
    # is_available()=True before we asserted False.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/icm")

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    assert any(
        "hermes_home" in record.message or "initialize" in record.message
        for record in caplog.records
    ), f"expected a WARNING about init failure; got {[r.message for r in caplog.records]!r}"
    assert provider.is_available() is False, "self-disable: is_available must return False"


def test_initialize_default_shared_no_mkdir_on_unwritable(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.1.1 — default-shared mode never calls mkdir, so a read-only FS is benign.

    If an operator points Hermes at a read-only ``hermes_home`` and stays in
    the default-shared mode, ``initialize`` is a no-op as far as the filesystem
    is concerned: the plugin will shell out to ``icm`` with no ``--db`` and let
    icm read/write its own canonical DB instead.
    """

    def _raise_oserror(_db_path: Path) -> None:
        raise OSError("never-fires-in-default-shared")

    monkeypatch.setattr(config, "mkdir_parent", _raise_oserror)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/icm")
    monkeypatch.setattr(cli_runner, "mcp_start", lambda *a, **kw: None)

    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    # No self-disable: the OSError path was never reached.
    assert provider.is_available() is True
    assert provider._db_path is None


# ---------- AC10: get_config_schema ------------------------------------------


def test_get_config_schema_matches_defaults() -> None:
    """AC10 — ``get_config_schema()`` returns ``config.get_default_schema()`` (deep-equal)."""
    provider = IcmMemoryProvider()
    first = provider.get_config_schema()
    assert first == config.get_default_schema()
    # Defensive copy: mutating the returned list must not poison the next call.
    first.clear()
    second = provider.get_config_schema()
    assert second == config.get_default_schema()
    assert second is not first


# ---------- AC11/AC12: save_config -------------------------------------------


def test_save_config_accepts_valid(tmp_hermes_home: Path) -> None:
    """AC11 — valid values → returns None, JSON sidecar written, ``_config`` reflects them."""
    provider = IcmMemoryProvider()
    result = provider.save_config(
        {"recall_limit": 7, "default_importance": "high"},
        hermes_home=tmp_hermes_home,
    )
    assert result is None

    sidecar = tmp_hermes_home / "icm" / "config.json"
    assert sidecar.is_file(), f"expected JSON sidecar at {sidecar}"
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["recall_limit"] == 7
    assert persisted["default_importance"] == "high"
    assert provider._config["recall_limit"] == 7
    assert provider._config["default_importance"] == "high"


def test_save_config_rejects_invalid_returns_error_dict(tmp_hermes_home: Path) -> None:
    """AC12 — invalid values → ``{"error": ...}``, never raises, no sidecar written."""
    provider = IcmMemoryProvider()
    result = provider.save_config({"recall_limit": -1}, hermes_home=tmp_hermes_home)

    assert isinstance(result, dict)
    assert "error" in result
    assert isinstance(result["error"], str)
    assert "recall_limit" in result["error"]

    sidecar = tmp_hermes_home / "icm" / "config.json"
    assert not sidecar.exists(), "invalid save_config must not write the sidecar"
    assert provider._config == {}


# ---------- v0.3 — provider exposes no LLM tool surface ----------------------
# AC13 / AC14 from the v0.1.x story (handle_tool_call dispatch, get_tool_schemas
# shape) are obsolete in v0.3. The corresponding invariant tests now live in
# tests/test_no_tool_surface.py: the provider has no handle_tool_call /
# get_tool_schemas, and the tools module is deleted.


# ---------- Extra coverage: save_config with no hermes_home -----------------


def test_save_config_without_hermes_home_skips_disk_write() -> None:
    """``save_config({}, hermes_home=None)`` validates + updates state, no sidecar.

    This branch is exercised by the S11 NFR-SEC-1 invariant
    ``test_save_config_no_socket`` once S10 wires the real provider into
    ``register(ctx)``. Until then, this test pins the contract directly.
    """
    provider = IcmMemoryProvider()
    result = provider.save_config({"recall_limit": 3})
    assert result is None
    assert provider._config == {"recall_limit": 3}


# ---------- Extra coverage: save_config with unwritable hermes_home ----------


# ---------- v0.3 — MCP transport removed ------------------------------------
# v0.2 added a plugin-managed ``icm serve`` daemon (transport: mcp);
# v0.3 deletes it because hermes-agent v0.3.0+ owns the
# ``mcp_servers.icm:`` surface natively. The lifecycle tests that pinned
# the start/stop wiring are obsolete; tests/test_cli_only_transport.py
# pins the inverse invariant (no transport kwarg, no mcp_* helpers).


def test_on_session_end_does_not_invoke_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_session_end`` gracefully handles the daemon teardown (v0.4).

    Pins that ``on_session_end`` is safe to call even when no MCP daemon
    was started (no AttributeError, no subprocess traffic).
    """
    provider = IcmMemoryProvider()
    # Must not raise even though there is no daemon and no worker yet.
    provider.on_session_end()


def test_save_config_returns_error_dict_on_oserror(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError from sidecar write → return ``{"error": "could not persist config: …"}``.

    Pins AD-07 / AD-18 boundary: validation already passed (so the values are
    in ``_config``), but the disk write fails. The caller sees an error-dict
    just like a validation failure, never an exception.
    """
    real_mkdir = config.mkdir_parent

    def _raise_on_write(path: Path) -> None:
        # Let the *initialize*-style call succeed if needed (none in this test)
        # — but raise specifically for save_config's mkdir_parent prep call.
        raise OSError("Read-only file system")

    monkeypatch.setattr(config, "mkdir_parent", _raise_on_write)

    provider = IcmMemoryProvider()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        result = provider.save_config(
            {"recall_limit": 3}, hermes_home=tmp_hermes_home
        )

    assert isinstance(result, dict)
    assert "error" in result
    assert "could not persist config" in result["error"]
    assert any(
        "could not persist sidecar" in record.message for record in caplog.records
    )
    # In-memory state still updated (validation succeeded before the write).
    assert provider._config == {"recall_limit": 3}

    # Sanity: real_mkdir is used so no global state leaks beyond monkeypatch.
    assert real_mkdir is not _raise_on_write


# ---------- v0.4.2: user-visible indicator footer ----------------------------
#
# Verifies the directive that ``system_prompt_block`` appends to ask the LLM
# to copy a per-turn liveness footer (📚 N · 💾 topic) to its reply. Plain-
# function tests cover render shapes; one integration test confirms the
# directive is wired into ``system_prompt_block`` end-to-end and that
# ``recent_recall_count`` is reset after the read.


def test_indicator_directive_heartbeat_when_silent() -> None:
    """Both counters empty → minimal heartbeat (📚 —) so user sees liveness."""
    out = IcmMemoryProvider._render_indicator_directive(0, None)
    assert "📚 —" in out
    assert "💾" not in out


def test_indicator_directive_recall_only() -> None:
    """recall > 0, no save → footer shows count only."""
    out = IcmMemoryProvider._render_indicator_directive(3, None)
    assert "📚 3" in out
    assert "💾" not in out


def test_indicator_directive_save_only() -> None:
    """recall == 0, save present → footer shows save only (no zero-count noise)."""
    out = IcmMemoryProvider._render_indicator_directive(0, "errors-resolved-moon-backend")
    assert "💾 errors-resolved-moon-backend" in out
    assert "📚" not in out


def test_indicator_directive_both_with_separator() -> None:
    """Both halves present → joined with the dot separator."""
    out = IcmMemoryProvider._render_indicator_directive(2, "decisions-hermes")
    assert "📚 2 · 💾 decisions-hermes" in out


def test_indicator_directive_instructs_verbatim_echo() -> None:
    """Directive text must instruct the LLM to copy the footer literally —
    the directive is the v0.4.3 fallback when transform_llm_output isn't
    wired (e.g. streamed partials). Strengthened wording adopted in v0.4.3."""
    out = IcmMemoryProvider._render_indicator_directive(1, "context-hermes-chat")
    # Stronger v0.4.3 wording must be present.
    assert "MANDATORY" in out
    assert "copied character-for-character" in out
    assert "non-negotiable" in out


def test_system_prompt_block_appends_indicator_and_resets_recall_count() -> None:
    """End-to-end: directive lands in block; recall counter resets after read.

    save_topic is sourced from the LAST entry of ``recent_stores`` (which the
    same call drains), so the second call should heartbeat — both halves clean.
    """
    provider = IcmMemoryProvider()
    provider._worker_state.recent_recall_count = 4
    provider._worker_state.recent_stores.append(
        ("learnings-hermes-icm-memory", "queue worker dies on second OOM"),
    )

    block = provider.system_prompt_block()
    assert "📚 4 · 💾 learnings-hermes-icm-memory" in block
    # Drained the stores list and zeroed the recall count.
    assert provider._worker_state.recent_stores == []
    assert provider._worker_state.recent_recall_count == 0

    # Next turn with no new state → heartbeat only.
    second = provider.system_prompt_block()
    assert "📚 —" in second


# ---------- v0.4.3: transform_llm_output hook (programmatic indicator) -------
#
# The hook is the primary indicator path — appends the footer to the LLM's
# reply regardless of whether the model honoured the system_prompt_block
# directive. Module-level _INDICATOR_STATE is shared across the two
# IcmMemoryProvider instances created under kind=standalone dual-load.


@pytest.fixture
def reset_indicator_state() -> Any:
    """Snapshot + zero + restore module-level _INDICATOR_STATE so tests don't
    bleed state into each other (it lives at module level on purpose — see
    v0.4.3 comment in provider.py). Zeroes BEFORE the test so the test sees a
    clean baseline even when prior tests in the same session left residue."""
    from hermes_icm_memory import provider as _prov
    snapshot = dict(_prov._INDICATOR_STATE)
    _prov._INDICATOR_STATE.clear()
    _prov._INDICATOR_STATE.update({"recall_count": 0, "last_save_topic": None})
    yield
    _prov._INDICATOR_STATE.clear()
    _prov._INDICATOR_STATE.update(snapshot)


def test_indicator_transform_appends_footer(reset_indicator_state: None) -> None:  # noqa: ARG001
    """Hook appends `📚 N · 💾 topic` to a reply that doesn't already have it."""
    from hermes_icm_memory import provider as _prov

    _prov._capture_recall_count(2)
    _prov._capture_save_topic("decisions-hermes")

    out = _prov._do_indicator_transform(response_text="Here is my answer.")
    assert out is not None
    assert out.endswith("📚 2 · 💾 decisions-hermes")
    assert out.startswith("Here is my answer.")


def test_indicator_transform_heartbeat_when_silent(reset_indicator_state: None) -> None:  # noqa: ARG001
    """No captured recall/save → heartbeat footer (📚 —) still appended."""
    from hermes_icm_memory import provider as _prov

    out = _prov._do_indicator_transform(response_text="ok")
    assert out is not None
    assert out.endswith("📚 —")


def test_indicator_transform_skips_when_model_already_complied(
    reset_indicator_state: None,  # noqa: ARG001
) -> None:
    """LLM followed the fallback directive → return None so we don't double."""
    from hermes_icm_memory import provider as _prov

    _prov._capture_recall_count(3)
    response = "Here is my answer.\n\n📚 3"
    out = _prov._do_indicator_transform(response_text=response)
    assert out is None, "must return None when response already ends with the footer"


def test_indicator_transform_returns_none_on_empty_text(
    reset_indicator_state: None,  # noqa: ARG001
) -> None:
    """Empty response → leave it alone (don't manufacture a footer-only reply)."""
    from hermes_icm_memory import provider as _prov

    assert _prov._do_indicator_transform(response_text="") is None


def test_indicator_transform_resets_state_after_consume(
    reset_indicator_state: None,  # noqa: ARG001
) -> None:
    """State drains to 0 / None after a transform so the next turn starts clean."""
    from hermes_icm_memory import provider as _prov

    _prov._capture_recall_count(5)
    _prov._capture_save_topic("learnings-foo")
    _prov._do_indicator_transform(response_text="reply")
    assert _prov._INDICATOR_STATE["recall_count"] == 0
    assert _prov._INDICATOR_STATE["last_save_topic"] is None


def test_capture_save_topic_ignores_empty_topic(
    reset_indicator_state: None,  # noqa: ARG001
) -> None:
    """Defensive: an empty/None topic must not clobber a valid prior capture."""
    from hermes_icm_memory import provider as _prov

    _prov._capture_save_topic("preferences")
    _prov._capture_save_topic("")  # ignored
    assert _prov._INDICATOR_STATE["last_save_topic"] == "preferences"


# ---------- v0.4.3: defensive register() under dual-load ---------------------


class _FakeMemoryCtx:
    """Mimics memory_manager._ProviderCollector — has register_memory_provider,
    register_hook is a no-op."""

    def __init__(self) -> None:
        self.provider = None
        self.hook_calls: list[tuple[str, Any]] = []

    def register_memory_provider(self, provider: Any) -> None:
        self.provider = provider

    def register_hook(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        pass  # no-op like the real _ProviderCollector


class _FakePluginCtx:
    """Mimics hermes_cli.plugins.PluginContext — has register_hook but NO
    register_memory_provider."""

    def __init__(self) -> None:
        self.hook_calls: list[tuple[str, Any]] = []

    def register_hook(self, hook_name: str, callback: Any) -> None:
        self.hook_calls.append((hook_name, callback))


def test_register_with_memory_manager_ctx_creates_provider_skips_hook() -> None:
    """memory_manager path: register_memory_provider called, register_hook no-op."""
    from hermes_icm_memory import register

    ctx = _FakeMemoryCtx()
    register(ctx)
    assert isinstance(ctx.provider, IcmMemoryProvider)


def test_register_with_plugin_manager_ctx_wires_hook_skips_provider() -> None:
    """PluginManager path: no register_memory_provider, so we only wire the hook."""
    from hermes_icm_memory import register
    from hermes_icm_memory.provider import _do_indicator_transform

    ctx = _FakePluginCtx()
    register(ctx)
    assert len(ctx.hook_calls) == 1
    hook_name, callback = ctx.hook_calls[0]
    assert hook_name == "transform_llm_output"
    assert callback is _do_indicator_transform


def test_register_tolerates_minimal_ctx() -> None:
    """ctx with neither method → no error (defensive hasattr checks)."""
    from hermes_icm_memory import register

    class _Empty:
        pass

    register(_Empty())  # must not raise


# ---------- v0.4.4: shutdown doesn't kill the shared MCP daemon --------------


def test_shutdown_does_not_kill_shared_mcp_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: pre-v0.4.4 the per-instance ``shutdown()`` called
    ``cli_runner.mcp_stop()`` which nulled the module-level singleton
    ``_client`` — fine for a single-instance world but broken for the
    gateway, where ``review_agent`` creates short-lived secondary providers
    whose shutdown killed the daemon for the still-active main agent. The
    fix moves daemon teardown to an ``atexit`` hook; per-instance shutdown
    is a no-op for the daemon. This test fails if anyone re-introduces
    a ``cli_runner.mcp_stop()`` call inside ``IcmMemoryProvider.shutdown``.
    """
    from hermes_icm_memory import cli_runner as _cr

    stop_call_count = [0]

    def _spy_stop() -> None:
        stop_call_count[0] += 1

    monkeypatch.setattr(_cr, "mcp_stop", _spy_stop)

    IcmMemoryProvider().shutdown()
    IcmMemoryProvider().shutdown()
    IcmMemoryProvider().shutdown()

    assert stop_call_count[0] == 0, (
        "provider.shutdown must NOT call cli_runner.mcp_stop — the daemon is "
        "process-wide and is torn down by the atexit hook instead"
    )


def test_initialize_loads_plugin_config_from_hermes_yaml(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: pre-v0.4.5 the plugin never read ``plugins.hermes-icm-
    memory.*`` from ``config.yaml``, so operator settings like
    ``use_embeddings: false`` were silently overridden by the schema
    default ``True`` — which on Pi-class hosts spawned ``icm serve``
    without ``--no-embeddings`` and broke recall quality. v0.4.5 merges
    that section into ``self._config`` at initialize so the operator
    setting actually takes effect.
    """
    from hermes_icm_memory import cli_runner as _cr

    # Don't actually try to spawn icm; we're only testing config plumbing.
    monkeypatch.setattr(_cr, "mcp_start", lambda **_kw: None)

    # Operator config that v0.4.5 should pick up.
    (tmp_hermes_home / "config.yaml").write_text(
        "plugins:\n"
        "  hermes-icm-memory:\n"
        "    use_embeddings: false\n"
        "    recall_limit: 7\n"
        "    not_in_schema: ignored\n",
        encoding="utf-8",
    )
    provider = IcmMemoryProvider()
    provider.initialize(
        session_id="s1", hermes_home=tmp_hermes_home, profile="default",
    )
    assert provider._config_bool("use_embeddings") is False, (
        "operator-set use_embeddings: false must beat the schema default True"
    )
    assert provider._config_int("recall_limit") == 7
    # Unknown keys must be filtered (forward-compat / typo guard).
    assert "not_in_schema" not in provider._config


def test_load_plugin_config_no_op_on_missing_section(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / missing plugin section is silently tolerated; schema defaults
    remain in effect — no crash on operators who haven't set anything."""
    from hermes_icm_memory import cli_runner as _cr

    monkeypatch.setattr(_cr, "mcp_start", lambda **_kw: None)

    (tmp_hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled: []\n", encoding="utf-8",
    )
    provider = IcmMemoryProvider()
    provider.initialize(
        session_id="s1", hermes_home=tmp_hermes_home, profile="default",
    )
    # Falls back to the schema default — and the call did not raise.
    assert provider._config_bool("use_embeddings") is True


def test_initialize_idempotent_path_still_revalidates_mcp_client(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: pre-v0.4.4 the ``args_key`` idempotent early-return
    skipped ``mcp_start`` entirely on re-init, so if something nulled
    ``cli_runner._client`` between turns (sub-agent shutdown, etc.) the
    daemon was never re-spawned. v0.4.4 always re-calls ``mcp_start``
    (it's idempotent at the cli_runner layer — no-op when _client is set
    — so cheap on the happy path)."""
    from hermes_icm_memory import cli_runner as _cr

    start_call_count = [0]

    def _spy_start(**_kw: Any) -> None:
        start_call_count[0] += 1

    monkeypatch.setattr(_cr, "mcp_start", _spy_start)

    provider = IcmMemoryProvider()
    provider.initialize(
        session_id="s1", hermes_home=tmp_hermes_home, profile="default",
    )
    assert start_call_count[0] == 1, "first init must call mcp_start once"

    # Idempotent re-init with same args still re-validates the daemon.
    provider.initialize(
        session_id="s1", hermes_home=tmp_hermes_home, profile="default",
    )
    assert start_call_count[0] == 2, (
        "re-init must re-call mcp_start so a previously-nulled _client gets "
        "re-spawned — the v0.4.4 fix for the silent-recall regression"
    )
