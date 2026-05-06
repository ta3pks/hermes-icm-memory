"""S13 / v0.3 — Failure-mode degrade matrix end-to-end (FR19, NFR-REL-1).

v0.3 removed the LLM-tool surface (``handle_tool_call`` / ``get_tool_schemas`` /
``hermes_icm_memory.tools``); the only remaining plugin-internal read path is
``provider.prefetch`` → ``hooks.run_prefetch`` → ``cli_runner.run_recall``.
The eight architecture §6.3 failure modes are therefore exercised against:

* ``provider.is_available`` (mode 1).
* ``provider.prefetch`` (modes 2, 3, 4, 5).
* ``provider.sync_turn`` worker death (modes 6, 7).
* ``provider.initialize`` filesystem failure (mode 8).

Each entry-point must (a) return its documented degraded shape, (b) emit one
WARNING/CRITICAL with the exception text inline (AC8 — the v0.3 ``%r`` fix
that surfaced the Pi outage), (c) never raise into the caller.

Mocking convention: subprocess patches use the string path
``"hermes_icm_memory.cli_runner.subprocess.run"`` (mirrors
``tests/test_cli_runner.py``).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- Shared fixtures + helpers ---------------------------------------


@pytest.fixture
def initialized_provider(tmp_hermes_home: Path) -> IcmMemoryProvider:
    """Provider with ``initialize()`` called, ``_available=True``, worker spun-up.

    v0.1.1: opts into ``isolated=True`` *before* ``initialize`` so ``_db_path``
    becomes a concrete path — the worker is gated on ``_db_path is not None``,
    and these failure-mode tests need the worker spun up to exercise it.
    Tests of the v0.1.1 default-shared behaviour (``_db_path is None``) use
    a separate fixture or construct providers directly.
    """
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")
    provider._available = True
    provider._ensure_worker()
    return provider


def _kill_worker(provider: IcmMemoryProvider) -> None:
    """Force the worker thread off without blocking on a real drain.

    Replicates the technique from ``tests/test_hooks.py::_kill_worker`` —
    set ``_stop_event``, join briefly, clear the event so the next respawn
    can run cleanly.
    """
    provider._stop_event.set()
    if provider._worker is not None:
        provider._worker.join(timeout=1.0)
    provider._stop_event.clear()


# ---------- Subprocess factories --------------------------------------------


def _stub_run_nonzero(*_a: Any, **_kw: Any) -> Any:
    """Mode 2 — ``icm`` exits non-zero. ``cli_runner._run`` raises ``ICMNonZeroExitError``."""
    return MagicMock(returncode=2, stdout="", stderr="boom: simulated non-zero exit")


def _stub_run_timeout(*a: Any, **_kw: Any) -> NoReturn:
    """Mode 3 — ``subprocess.run`` raises ``TimeoutExpired``."""
    cmd = a[0] if a else ["icm"]
    raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)


def _stub_run_malformed(*_a: Any, **_kw: Any) -> Any:
    """Mode 4 — non-JSON stdout. ``run_recall`` raises ``ICMMalformedOutputError``."""
    return MagicMock(returncode=0, stdout="not valid json {{{", stderr="")


def _stub_run_not_found(*_a: Any, **_kw: Any) -> NoReturn:
    """Mode 1 partner — ``subprocess.run`` raises ``FileNotFoundError`` (icm missing)."""
    raise FileNotFoundError("icm: command not found")


# ---------- Mode 1: icm not on PATH -----------------------------------------


def test_mode1_icm_not_on_path_degrades_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 1 — ``shutil.which("icm")`` → None AND subprocess raises ``FileNotFoundError``.

    Asserts:
        * ``is_available()`` is ``False`` (no exception).
        * ``prefetch()`` returns ``""`` (short-circuits on the unavailable check
          before invoking ``cli_runner``).
        * No exception escapes any entry-point.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    spy = MagicMock(side_effect=_stub_run_not_found)
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", spy)

    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    assert provider.is_available() is False, "shutil.which → None must flip is_available to False"

    with caplog.at_level(logging.WARNING):
        prefetch_out = provider.prefetch(query="anything")

    # ``prefetch`` short-circuits on ``not is_available()`` and returns "" without
    # invoking ``cli_runner``.
    assert prefetch_out == ""
    assert spy.call_count == 0, (
        f"prefetch must NOT invoke subprocess when is_available()=False; "
        f"got {spy.call_count} call(s)"
    )


# ---------- Modes 2/3/4: subprocess-level failures degrade prefetch ---------


_SUBPROC_FAILURE_MODES: list[tuple[str, Any]] = [
    ("mode2_nonzero", _stub_run_nonzero),
    ("mode3_timeout", _stub_run_timeout),
    ("mode4_malformed", _stub_run_malformed),
]


@pytest.mark.parametrize(("mode_id", "stub"), _SUBPROC_FAILURE_MODES)
def test_modes_2_3_4_subprocess_failure_degrades_prefetch(
    mode_id: str,
    stub: Any,
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Modes 2/3/4 (non-zero exit, timeout, malformed) on the prefetch path.

    All three subprocess-level failures funnel through ``cli_runner._run``
    into ``hooks.run_prefetch`` and degrade identically: ``""`` returned,
    ``[]`` cached so ``system_prompt_block`` does not retry, WARNING logged
    with the exception text inline (AC8). No exception escapes.
    """
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", stub)

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        result = initialized_provider.prefetch(query="x")

    assert result == "", f"{mode_id}: degraded shape regression"
    # Cache poisoning prevents system_prompt_block from re-attempting.
    assert initialized_provider._prefetch_cache.get(hash("x")) == []
    # No second subprocess call from system_prompt_block (NFR-PERF-4).
    assert initialized_provider.system_prompt_block() == ""

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "hermes_icm_memory.hooks"
    ]
    assert warnings, (
        f"{mode_id}: expected WARNING from hermes_icm_memory.hooks logger"
    )
    # AC8: exception text inlined into the format string itself, not just
    # buried in ``extra={...}``. Ensures the default Python formatter
    # surfaces the cause — the v0.2-era code dropped this and made the
    # 2026-05-06 Pi outage undebuggable.
    assert any(
        "recall failed" in r.message or "unexpected" in r.message
        for r in warnings
    ), f"{mode_id}: WARNING did not name the failure: {[r.message for r in warnings]!r}"


# ---------- Mode 5: first-call slow path (no degrade) -----------------------


def test_mode5_first_call_slow_path_no_degrade(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 5 — slow first call eventually succeeds; no degrade, no WARNING/CRITICAL."""

    def _slow_then_succeed(*_a: Any, **_kw: Any) -> Any:
        time.sleep(0.002)
        return MagicMock(
            returncode=0,
            stdout='[{"id": "m1", "topic": "preferences", "summary": "use bun"}]',
            stderr="",
        )

    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", _slow_then_succeed)

    with caplog.at_level(logging.DEBUG):
        block = initialized_provider.prefetch(query="anything")

    assert "use bun" in block, (
        f"slow happy path must return a populated prefetch block; got {block!r}"
    )

    bad_levels = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert bad_levels == [], (
        f"slow happy path must NOT degrade (no WARNING/CRITICAL); got "
        f"{[(r.levelname, r.message) for r in bad_levels]!r}"
    )

    # DEBUG ``elapsed_ms`` log is recorded by ``cli_runner._run`` on every
    # invocation — proves the slow-call observability hook is alive.
    debug_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and r.name == "hermes_icm_memory.cli_runner"
    ]
    elapsed_records = [
        getattr(r, "elapsed_ms", None) for r in debug_records
        if getattr(r, "elapsed_ms", None) is not None
    ]
    assert elapsed_records, "expected at least one DEBUG record carrying elapsed_ms extra"
    assert any(
        isinstance(v, (int, float)) and v > 0 for v in elapsed_records
    ), f"expected elapsed_ms > 0 on the slow-call DEBUG record; got {elapsed_records!r}"


# ---------- Mode 6: worker dies once → lazy respawn (AD-15) -----------------


def test_mode6_worker_dies_once_lazy_respawn(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 6 — worker thread dies once → respawned + WARNING; no exception."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *_a, **_kw: [("preferences", "critical", "x", ["x"])],
    )
    monkeypatch.setattr(cli_runner, "run_store", lambda *_a, **_kw: None)

    first_worker = initialized_provider._worker
    assert first_worker is not None and first_worker.is_alive()

    _kill_worker(initialized_provider)
    assert not first_worker.is_alive()

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._respawn_count == 1
    assert initialized_provider._worker is not None
    assert initialized_provider._worker is not first_worker
    assert initialized_provider._worker.is_alive()
    assert initialized_provider._writes_disabled is False
    assert any(
        r.levelno == logging.WARNING and "respawn" in r.message.lower()
        for r in caplog.records
    ), f"expected respawn WARNING; got {[(r.levelname, r.message) for r in caplog.records]!r}"


# ---------- Mode 7: worker dies twice → degrade-to-drop + CRITICAL ----------


def test_mode7_worker_dies_twice_degrades_with_critical(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 7 — second death sets ``_writes_disabled`` + CRITICAL log; sync_turn no-raises."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *_a, **_kw: [("preferences", "critical", "x", ["x"])],
    )
    monkeypatch.setattr(cli_runner, "run_store", lambda *_a, **_kw: None)

    # First death + respawn (mode 6 territory).
    _kill_worker(initialized_provider)
    initialized_provider.sync_turn(user_content="u", assistant_content="a")
    assert initialized_provider._respawn_count == 1
    second_worker = initialized_provider._worker
    assert second_worker is not None
    assert second_worker.is_alive()

    # Second death — degrade-to-drop forever.
    _kill_worker(initialized_provider)
    assert not second_worker.is_alive(), (
        "second worker must be dead before the degrade-disable sync_turn; "
        "if join timed out the next assertion would silently flake"
    )

    with caplog.at_level(logging.CRITICAL, logger="hermes_icm_memory.hooks"):
        initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._writes_disabled is True
    critical_records = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and r.name == "hermes_icm_memory.hooks"
        and "second death" in r.message.lower()
    ]
    assert critical_records, (
        f"expected CRITICAL 'second death' log from hermes_icm_memory.hooks; "
        f"got {[(r.levelname, r.name, r.message) for r in caplog.records]!r}"
    )

    # Subsequent enqueues are no-ops.
    pre_count = initialized_provider._respawn_count
    queue_obj = initialized_provider._write_queue
    pre_qsize = queue_obj.qsize() if queue_obj is not None else 0

    initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._respawn_count == pre_count
    assert initialized_provider._writes_disabled is True
    post_qsize = queue_obj.qsize() if queue_obj is not None else 0
    assert post_qsize == pre_qsize


# ---------- Mode 8: hermes_home parent unwritable ---------------------------


def test_mode8_hermes_home_unwritable_self_disables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 8 — ``initialize`` catches ``OSError`` from ``mkdir_parent``.

    ``OSError`` → WARNING log + self-disable; no exception escapes.
    """

    def _raise_perm(_db_path: Path) -> None:
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr("hermes_icm_memory.config.mkdir_parent", _raise_perm)

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    assert provider.is_available() is False, "init failure must flip is_available to False (sticky)"
    assert any(
        r.levelno == logging.WARNING and "hermes_home not writable" in r.message
        for r in caplog.records
    ), f"expected hermes_home WARNING; got {[(r.levelname, r.message) for r in caplog.records]!r}"


def test_mode8_self_disable_is_sticky_across_reinit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
) -> None:
    """Mode 8 follow-up — ``_available=False`` survives a successful re-init."""
    mkdir_calls = {"n": 0}

    def _raise_perm_first_call_only(_db_path: Path) -> None:
        mkdir_calls["n"] += 1
        if mkdir_calls["n"] == 1:
            raise PermissionError("read-only filesystem")

    monkeypatch.setattr(
        "hermes_icm_memory.config.mkdir_parent", _raise_perm_first_call_only
    )

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")
    assert provider.is_available() is False
    assert mkdir_calls["n"] == 1

    provider.initialize(
        session_id="s2", hermes_home=tmp_hermes_home, profile="other"
    )
    assert mkdir_calls["n"] == 2
    assert provider.is_available() is False


# ---------- AC2: stress sub-test --------------------------------------------


@pytest.mark.parametrize(("mode_id", "stub"), _SUBPROC_FAILURE_MODES)
def test_stress_subprocess_failure_no_escape_under_burst(
    mode_id: str,
    stub: Any,
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2 — same failure injected on 100 successive prefetch calls. No exception escapes.

    Each ``prefetch`` invocation must return ``""`` and emit exactly one
    WARNING from ``hermes_icm_memory.hooks``. WARNINGs are NOT rate-limited
    at the helper boundary (only queue-overflow bursts are flag-gated).
    """
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", stub)

    iterations = 100
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        for i in range(iterations):
            # Vary the query so each iteration hits a fresh cache key
            # (otherwise the second call would short-circuit on the cached
            # ``[]`` and skip the subprocess hop, which is correct hot-path
            # behavior but defeats the stress assertion).
            block = initialized_provider.prefetch(query=f"q-{i}")
            assert block == "", f"{mode_id}: degraded shape regression"

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "hermes_icm_memory.hooks"
    ]
    assert len(warnings) == iterations, (
        f"{mode_id}: expected {iterations} WARNINGs from hermes_icm_memory.hooks "
        f"(one per call, NOT rate-limited at the helper boundary); got {len(warnings)}"
    )
