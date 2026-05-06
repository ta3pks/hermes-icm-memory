"""S13 — Failure-mode degrade matrix end-to-end (architecture §6.3, FR19, NFR-REL-1).

Mocking convention: subprocess patches use the string path
``"hermes_icm_memory.cli_runner.subprocess.run"`` (mirrors
``tests/test_cli_runner.py``). This keeps ``mypy --strict`` happy — the
``cli_runner`` module does not re-export ``subprocess`` in ``__all__``, so an
attribute-style ``setattr(cli_runner.subprocess, "run", …)`` would fail the
``attr-defined`` check.


Each of the eight rows in architecture §6.3 is exercised against the public
plugin entry-point that observes it. Assertions cover (a) the documented
return shape, (b) the documented log level, (c) no exception escapes.

Mode coverage:

1. ``icm`` not on PATH — ``shutil.which`` → None + ``subprocess.run`` →
   ``FileNotFoundError``. Entry-points: ``is_available``, three read-tool
   handlers, ``prefetch``. All degrade silently with WARNING.
2. ``icm`` exits non-zero. ``handle_tool_call("icm_recall", …)`` →
   ``{"hits": []}`` + WARNING.
3. ``icm`` raises ``TimeoutExpired`` → ``{"hits": []}`` + WARNING.
4. ``icm`` stdout malformed JSON → ``{"hits": []}`` + WARNING.
5. ``icm`` first-call slow path (succeeds eventually) — no degrade, no
   WARNING/CRITICAL. The architecture §6.3 row 5 INFO-log enhancement
   ("ICM is downloading model") is recorded as a deferred enhancement
   (see story spec); the current ``cli_runner`` records ``elapsed_ms`` at
   DEBUG only.
6. Worker thread dies once → lazy respawn (AD-15) + WARNING.
7. Worker thread dies twice → ``_writes_disabled = True`` + CRITICAL.
8. ``hermes_home`` parent unwritable → ``initialize`` self-disables; WARNING.

Plus a stress sub-test (AC2): each of modes 2/3/4 injected on every call
across 100 iterations of ``icm_recall`` → no exception escapes; every call
returns ``{"hits": []}``.

Mocking convention: patch ``cli_runner.subprocess.run`` (the only place the
package interacts with ``subprocess``, per AD-12). For mode 8 we patch
``hermes_icm_memory.config.mkdir_parent`` so the test stays portable across
host filesystems and root-vs-non-root execution contexts.

Worker-death convention mirrors ``tests/test_hooks.py::_kill_worker``: set
``_stop_event``, join the worker briefly, then clear the event so the next
respawn can run cleanly.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- Shared fixtures + helpers ---------------------------------------


@pytest.fixture
def initialized_provider(tmp_hermes_home: Path) -> IcmMemoryProvider:
    """Provider with ``initialize()`` called, ``_available=True``, worker spun-up.

    Mirrors ``tests/test_hooks.py::initialized_provider`` so the failure-mode
    tests share its proven setup pattern.
    """
    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")
    provider._available = True
    provider._ensure_worker()
    return provider


def _kill_worker(provider: IcmMemoryProvider) -> None:
    """Force the worker thread off without blocking on a real drain.

    Replicates the technique from ``tests/test_hooks.py::_kill_worker`` named
    in the manager prompt: set ``_stop_event``, join briefly, clear the event
    so the next respawn can run cleanly.
    """
    provider._stop_event.set()
    if provider._worker is not None:
        provider._worker.join(timeout=1.0)
    provider._stop_event.clear()


# ---------- Subprocess factories --------------------------------------------


def _stub_run_nonzero(*_a: Any, **_kw: Any) -> Any:
    """Mode 2 — ``icm`` exits non-zero. ``cli_runner._run`` raises ``ICMNonZeroExitError``."""
    return MagicMock(returncode=2, stdout="", stderr="boom: simulated non-zero exit")


def _stub_run_timeout(*a: Any, **_kw: Any) -> Any:
    """Mode 3 — ``subprocess.run`` raises ``TimeoutExpired``.

    The first positional arg in ``cli_runner._run`` is the ``argv`` list, so
    we forward it back to ``TimeoutExpired(cmd=…)`` as a faithful simulation.
    """
    cmd = a[0] if a else ["icm"]
    raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)


def _stub_run_malformed(*_a: Any, **_kw: Any) -> Any:
    """Mode 4 — non-JSON stdout. ``run_recall`` raises ``ICMMalformedOutputError``."""
    return MagicMock(returncode=0, stdout="not valid json {{{", stderr="")


def _stub_run_not_found(*_a: Any, **_kw: Any) -> Any:
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
        * Each read-tool handler returns its documented empty payload.
        * ``prefetch()`` returns ``""``.
        * At least one WARNING is emitted across the failed read paths.
        * No exception escapes any entry-point.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", _stub_run_not_found)

    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    assert provider.is_available() is False, "shutil.which → None must flip is_available to False"

    with caplog.at_level(logging.WARNING):
        recall_out = provider.handle_tool_call("icm_recall", {"query": "anything"})
        topics_out = provider.handle_tool_call("icm_topics", {})
        health_out = provider.handle_tool_call("icm_health", {})
        prefetch_out = provider.prefetch(query="anything")

    assert json.loads(recall_out) == {"hits": []}
    assert json.loads(topics_out) == {"topics": []}
    assert json.loads(health_out) == {"report": {}}
    # ``prefetch`` short-circuits on ``not is_available()`` and returns "" without
    # invoking ``cli_runner`` — proves the cheaper guard wins over a subprocess hop.
    assert prefetch_out == ""

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected at least one WARNING from the failed read paths"


# ---------- Modes 2/3/4: subprocess-level failures degrade ------------------


@pytest.mark.parametrize(
    ("mode_id", "stub"),
    [
        ("mode2_nonzero", _stub_run_nonzero),
        ("mode3_timeout", _stub_run_timeout),
        ("mode4_malformed", _stub_run_malformed),
    ],
)
def test_subprocess_failure_modes_degrade_to_empty_hits(
    mode_id: str,
    stub: Any,
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Modes 2/3/4 — subprocess-level failures degrade to ``{"hits": []}`` + WARNING.

    Each parametrized case patches ``cli_runner.subprocess.run`` with the
    matching stub, calls ``handle_tool_call("icm_recall", …)``, and asserts
    the documented degraded shape + WARNING + no-exception-escape.
    """
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", stub)

    with caplog.at_level(logging.WARNING):
        out = initialized_provider.handle_tool_call("icm_recall", {"query": "anything"})

    parsed = json.loads(out)
    assert parsed == {"hits": []}, f"{mode_id}: expected empty hits, got {parsed!r}"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, f"{mode_id}: expected at least one WARNING"
    assert any(
        "icm_recall" in r.message or "failed" in r.message.lower()
        for r in warnings
    ), f"{mode_id}: WARNING messages did not mention icm_recall: {[r.message for r in warnings]!r}"


# ---------- Mode 5: first-call slow path (no degrade) -----------------------


def test_mode5_first_call_slow_path_no_degrade(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 5 — slow first call eventually succeeds; no degrade, no WARNING/CRITICAL.

    The architecture §6.3 row 5 calls for an INFO-level "ICM is downloading
    model" log on the first slow call. The current ``cli_runner._run`` only
    records ``elapsed_ms`` at DEBUG. We assert the OBSERVABLE behavior: the
    call returns hits successfully and no WARNING/CRITICAL is emitted. The
    INFO-tier escalation is a deferred enhancement (see story spec deviation
    note).
    """

    def _slow_then_succeed(*_a: Any, **_kw: Any) -> Any:
        time.sleep(0.05)  # 50ms — short enough for fast tests, long enough to register
        return MagicMock(
            returncode=0,
            stdout='[{"id": "m1", "topic": "preferences", "summary": "use bun"}]',
            stderr="",
        )

    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", _slow_then_succeed)

    with caplog.at_level(logging.DEBUG):
        out = initialized_provider.handle_tool_call("icm_recall", {"query": "anything"})

    parsed = json.loads(out)
    assert parsed == {"hits": [{"id": "m1", "topic": "preferences", "summary": "use bun"}]}, (
        f"slow happy path must return real hits, got {parsed!r}"
    )

    bad_levels = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert bad_levels == [], (
        f"slow happy path must NOT degrade (no WARNING/CRITICAL); got "
        f"{[(r.levelname, r.message) for r in bad_levels]!r}"
    )

    # DEBUG ``elapsed_ms`` log is recorded by ``cli_runner._run`` on every
    # invocation — proves the slow-call observability hook is alive even though
    # the INFO-tier escalation is deferred.
    debug_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and r.name == "hermes_icm_memory.cli_runner"
    ]
    assert any(
        getattr(r, "elapsed_ms", None) is not None for r in debug_records
    ), "expected at least one DEBUG record carrying elapsed_ms extra"


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
    assert initialized_provider._worker is not None
    assert initialized_provider._worker.is_alive()

    # Second death — degrade-to-drop forever.
    _kill_worker(initialized_provider)

    with caplog.at_level(logging.CRITICAL, logger="hermes_icm_memory.hooks"):
        initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._writes_disabled is True
    assert any(
        r.levelno == logging.CRITICAL for r in caplog.records
    ), f"expected CRITICAL log; got levels {[r.levelno for r in caplog.records]!r}"

    # Subsequent enqueues are no-ops — respawn count stays put, no exception.
    pre_count = initialized_provider._respawn_count
    initialized_provider.sync_turn(user_content="u", assistant_content="a")
    assert initialized_provider._respawn_count == pre_count


# ---------- Mode 8: hermes_home parent unwritable ---------------------------


def test_mode8_hermes_home_unwritable_self_disables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 8 — ``initialize`` catches ``OSError`` from ``mkdir_parent`` → WARNING + self-disable.

    Patches ``hermes_icm_memory.config.mkdir_parent`` to raise
    ``PermissionError`` (an ``OSError`` subclass). Asserts no exception
    escapes ``initialize``, ``is_available()`` flips False, and a WARNING
    naming the unwritable hermes_home is emitted.
    """

    def _raise_perm(_db_path: Path) -> None:
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr("hermes_icm_memory.config.mkdir_parent", _raise_perm)

    provider = IcmMemoryProvider()

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    assert provider.is_available() is False, "init failure must flip is_available to False (sticky)"
    assert any(
        r.levelno == logging.WARNING and "hermes_home not writable" in r.message
        for r in caplog.records
    ), f"expected hermes_home WARNING; got {[(r.levelname, r.message) for r in caplog.records]!r}"


# ---------- AC2: stress sub-test --------------------------------------------


@pytest.mark.parametrize(
    ("mode_id", "stub"),
    [
        ("mode2_nonzero", _stub_run_nonzero),
        ("mode3_timeout", _stub_run_timeout),
        ("mode4_malformed", _stub_run_malformed),
    ],
)
def test_stress_subprocess_failure_no_escape_under_burst(
    mode_id: str,
    stub: Any,
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2 — same failure injected on 100 successive calls. No exception escapes.

    Each ``icm_recall`` invocation must return ``{"hits": []}``. Per-call
    WARNINGs are NOT rate-limited at the tool boundary (only queue-overflow
    bursts are flag-gated, per AD-04) — we therefore expect 100 WARNINGs and
    100 degraded returns, with zero exceptions.
    """
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", stub)

    iterations = 100
    with caplog.at_level(logging.WARNING):
        for _ in range(iterations):
            out = initialized_provider.handle_tool_call("icm_recall", {"query": "x"})
            parsed = json.loads(out)
            assert parsed == {"hits": []}, f"{mode_id}: degraded shape regression"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == iterations, (
        f"{mode_id}: expected {iterations} WARNINGs (one per call, NOT rate-limited "
        f"at the tool boundary); got {len(warnings)}"
    )
