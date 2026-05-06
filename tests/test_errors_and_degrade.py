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
from typing import Any, NoReturn
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


def _stub_run_timeout(*a: Any, **_kw: Any) -> NoReturn:
    """Mode 3 — ``subprocess.run`` raises ``TimeoutExpired``.

    The first positional arg in ``cli_runner._run`` is the ``argv`` list, so
    we forward it back to ``TimeoutExpired(cmd=…)`` as a faithful simulation.
    ``-> NoReturn`` documents that this stub always raises (no fallthrough).
    """
    cmd = a[0] if a else ["icm"]
    raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)


def _stub_run_malformed(*_a: Any, **_kw: Any) -> Any:
    """Mode 4 — non-JSON stdout. ``run_recall`` raises ``ICMMalformedOutputError``."""
    return MagicMock(returncode=0, stdout="not valid json {{{", stderr="")


def _stub_run_not_found(*_a: Any, **_kw: Any) -> NoReturn:
    """Mode 1 partner — ``subprocess.run`` raises ``FileNotFoundError`` (icm missing).

    ``-> NoReturn`` documents that this stub always raises (no fallthrough).
    """
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
    # ``side_effect`` makes the spy raise per ``_stub_run_not_found`` while
    # ``call_count`` records each subprocess attempt — proves prefetch's
    # ``is_available()`` guard short-circuits before the subprocess hop.
    spy = MagicMock(side_effect=_stub_run_not_found)
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", spy)

    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    assert provider.is_available() is False, "shutil.which → None must flip is_available to False"

    with caplog.at_level(logging.WARNING):
        recall_out = provider.handle_tool_call("icm_recall", {"query": "anything"})
        topics_out = provider.handle_tool_call("icm_topics", {})
        health_out = provider.handle_tool_call("icm_health", {})
        calls_after_reads = spy.call_count
        prefetch_out = provider.prefetch(query="anything")
        calls_after_prefetch = spy.call_count

    assert json.loads(recall_out) == {"hits": []}
    assert json.loads(topics_out) == {"topics": []}
    assert json.loads(health_out) == {"report": {}}
    # Each read tool actually attempts the subprocess and the failure surfaces
    # through the typed-exception channel — proves the degrade path is real,
    # not a short-circuit elsewhere.
    assert calls_after_reads >= 3, (
        f"expected ≥3 subprocess attempts across recall/topics/health; "
        f"got {calls_after_reads}"
    )
    # ``prefetch`` short-circuits on ``not is_available()`` and returns "" without
    # invoking ``cli_runner`` — guards a regression where prefetch bypasses
    # the cheaper guard.
    assert prefetch_out == ""
    assert calls_after_prefetch == calls_after_reads, (
        f"prefetch must NOT invoke subprocess when is_available()=False; "
        f"got {calls_after_prefetch - calls_after_reads} extra call(s)"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected at least one WARNING from the failed read paths"


# ---------- Modes 2/3/4: subprocess-level failures degrade ------------------


# Per-tool (degrade-payload, mode-applicability) catalogue. Modes 2 & 3 are
# transport failures (non-zero exit, timeout) — they degrade the same way
# regardless of which read tool was invoked, so they apply to all three. Mode
# 4 (malformed stdout) is parser-specific:
#   * ``icm_recall`` parses with ``json.loads`` → JSONDecodeError → degrade.
#   * ``icm_health`` parses ``key: value`` lines → raises
#     ``ICMMalformedOutputError`` when stdout has no parseable lines.
#   * ``icm_topics`` is text-permissive by design (single-column fallback in
#     ``_parse_topics_table``) — there is no "malformed topics output" path.
# So mode 4 cross-product excludes ``icm_topics``; the topics-parser
# permissiveness is documented behaviour, not a gap.
_READ_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("icm_recall", {"hits": []}),
    ("icm_topics", {"topics": []}),
    ("icm_health", {"report": {}}),
]
_MODES_2_3: list[tuple[str, Any]] = [
    ("mode2_nonzero", _stub_run_nonzero),
    ("mode3_timeout", _stub_run_timeout),
]
# Mode 4 excludes ``icm_topics`` per the catalogue comment above. Derive
# from ``_READ_TOOLS`` so a future fourth read-tool entry is opt-out, not
# opt-in.
_MODE_4_TOOLS: list[tuple[str, dict[str, Any]]] = [
    t for t in _READ_TOOLS if t[0] != "icm_topics"
]
# Stress-test catalogue (modes 2, 3, 4 — all subprocess-failure modes).
_ALL_SUBPROC_FAILURE_MODES: list[tuple[str, Any]] = _MODES_2_3 + [
    ("mode4_malformed", _stub_run_malformed),
]


def _exercise_read_tool_degrade(
    *,
    provider: IcmMemoryProvider,
    tool_name: str,
    stub: Any,
    expected_payload: dict[str, Any],
    mode_id: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shared assertion body for modes 2/3/4 × read tools.

    Patches ``cli_runner.subprocess.run`` with ``stub``, calls the named
    tool, and asserts: documented empty payload, ≥1 WARNING from
    ``hermes_icm_memory.tools``, message names the tool. No exception
    escapes (any leak surfaces as a pytest failure).
    """
    args: dict[str, Any] = {"query": "anything"} if tool_name == "icm_recall" else {}
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", stub)

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        out = provider.handle_tool_call(tool_name, args)

    parsed = json.loads(out)
    assert parsed == expected_payload, (
        f"{mode_id}/{tool_name}: expected {expected_payload!r}, got {parsed!r}"
    )

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "hermes_icm_memory.tools"
    ]
    assert warnings, (
        f"{mode_id}/{tool_name}: expected at least one WARNING from "
        f"hermes_icm_memory.tools logger"
    )
    assert any(
        tool_name in r.message or getattr(r, "tool", None) == tool_name
        for r in warnings
    ), (
        f"{mode_id}/{tool_name}: WARNINGs did not name {tool_name}: "
        f"{[(r.message, getattr(r, 'tool', None)) for r in warnings]!r}"
    )


@pytest.mark.parametrize(("mode_id", "stub"), _MODES_2_3)
@pytest.mark.parametrize(("tool_name", "expected_payload"), _READ_TOOLS)
def test_modes_2_3_transport_failure_degrades_all_read_tools(
    mode_id: str,
    stub: Any,
    tool_name: str,
    expected_payload: dict[str, Any],
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Modes 2 & 3 (non-zero exit, timeout) across all three read tools.

    Transport-level failures degrade identically regardless of parser, so the
    full cross-product is exercised — guards against a regression where (e.g.)
    ``icm_topics`` is wired to a different exception channel.
    """
    _exercise_read_tool_degrade(
        provider=initialized_provider,
        tool_name=tool_name,
        stub=stub,
        expected_payload=expected_payload,
        mode_id=mode_id,
        monkeypatch=monkeypatch,
        caplog=caplog,
    )


@pytest.mark.parametrize(("tool_name", "expected_payload"), _MODE_4_TOOLS)
def test_mode4_malformed_stdout_degrades_recall_and_health(
    tool_name: str,
    expected_payload: dict[str, Any],
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 4 (malformed stdout) on JSON-parsing tools only.

    ``icm_topics`` is excluded by design — its parser is text-permissive
    (single-column fallback) and intentionally has no "malformed" failure
    mode. See the ``_MODE_4_TOOLS`` comment above for the rationale.
    """
    _exercise_read_tool_degrade(
        provider=initialized_provider,
        tool_name=tool_name,
        stub=_stub_run_malformed,
        expected_payload=expected_payload,
        mode_id="mode4_malformed",
        monkeypatch=monkeypatch,
        caplog=caplog,
    )


def test_mode3_timeout_in_prefetch_caches_empty(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 3 (timeout) on the ``prefetch`` path — empty string returned, ``[]`` cached.

    Per architecture §8 failure variant: prefetch must (a) return ``""``,
    (b) write ``[]`` into the cache so ``system_prompt_block`` does not retry,
    (c) log WARNING. Proves the degrade contract on the hook path (modes
    2/3/4 are otherwise only exercised on the LLM-tool path above).
    """
    monkeypatch.setattr("hermes_icm_memory.cli_runner.subprocess.run", _stub_run_timeout)

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        result = initialized_provider.prefetch(query="x")

    assert result == ""
    # Cache poisoning prevents system_prompt_block from re-attempting.
    assert initialized_provider._prefetch_cache.get(hash("x")) == []
    # No second subprocess call from system_prompt_block (NFR-PERF-4 +
    # architecture §8 failure variant).
    assert initialized_provider.system_prompt_block() == ""

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "hermes_icm_memory.hooks"
    ]
    assert warnings, "expected WARNING from hermes_icm_memory.hooks on prefetch failure"


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
        # 2 ms is enough for ``time.perf_counter`` to register a non-zero
        # delta on every supported platform (Linux/macOS/Windows clock
        # granularity is sub-millisecond) while keeping the test fast.
        time.sleep(0.002)
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
    elapsed_records = [
        getattr(r, "elapsed_ms", None) for r in debug_records
        if getattr(r, "elapsed_ms", None) is not None
    ]
    assert elapsed_records, "expected at least one DEBUG record carrying elapsed_ms extra"
    # ``elapsed_ms`` must be a positive number, not just present — catches a
    # regression where the field is wired but always 0/None.
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
    # Filter by logger AND assert message-content keyword so an unrelated
    # CRITICAL emitted by some other subsystem doesn't satisfy the assertion.
    critical_records = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and r.name == "hermes_icm_memory.hooks"
        and "second death" in r.message.lower()
    ]
    assert critical_records, (
        f"expected CRITICAL 'second death' log from hermes_icm_memory.hooks; "
        f"got {[(r.levelname, r.name, r.message) for r in caplog.records]!r}"
    )

    # Subsequent enqueues are no-ops — respawn count + writes_disabled stay put,
    # no new task is enqueued, no exception escapes.
    pre_count = initialized_provider._respawn_count
    queue_obj = initialized_provider._write_queue
    pre_qsize = queue_obj.qsize() if queue_obj is not None else 0

    initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._respawn_count == pre_count
    assert initialized_provider._writes_disabled is True, (
        "writes_disabled must stay True on subsequent sync_turn (sticky degrade)"
    )
    post_qsize = queue_obj.qsize() if queue_obj is not None else 0
    assert post_qsize == pre_qsize, (
        f"sync_turn must not enqueue when writes_disabled=True; "
        f"qsize {pre_qsize} → {post_qsize}"
    )


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


def test_mode8_self_disable_is_sticky_across_reinit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
) -> None:
    """Mode 8 follow-up — ``_available=False`` survives a successful re-init.

    Once ``initialize`` self-disables (because ``mkdir_parent`` raised), a
    later re-init with DIFFERENT args must not re-enable the provider — the
    sticky-False guarantee in ``provider.py::is_available`` is a load-bearing
    contract for downstream tools (recall/topics/health/prefetch all check
    ``is_available()`` and would silently start hitting subprocess again if
    the flag flipped back).
    """
    mkdir_calls = {"n": 0}

    def _raise_perm_first_call_only(_db_path: Path) -> None:
        mkdir_calls["n"] += 1
        if mkdir_calls["n"] == 1:
            raise PermissionError("read-only filesystem")
        # Second call returns ``None`` (matches ``mkdir_parent``'s real
        # signature) — simulates the underlying error condition clearing.

    monkeypatch.setattr(
        "hermes_icm_memory.config.mkdir_parent", _raise_perm_first_call_only
    )

    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")
    assert provider.is_available() is False
    assert mkdir_calls["n"] == 1, "first initialize must call mkdir_parent exactly once"

    # Re-init with different args: ``mkdir_parent`` is invoked again (not
    # short-circuited by ``_init_args``) and now returns successfully — but
    # the sticky-False on ``_available`` must be preserved.
    provider.initialize(
        session_id="s2", hermes_home=tmp_hermes_home, profile="other"
    )
    assert mkdir_calls["n"] == 2, (
        "re-init with different args must re-invoke mkdir_parent; "
        "without this the stickiness assertion below would pass trivially"
    )
    assert provider.is_available() is False, (
        "re-init with different args must not flip _available back to True; "
        "sticky-False is a load-bearing contract"
    )


# ---------- AC2: stress sub-test --------------------------------------------


@pytest.mark.parametrize(("mode_id", "stub"), _ALL_SUBPROC_FAILURE_MODES)
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
    # Filter by logger so an unrelated WARNING from another subsystem
    # doesn't inflate the count and break the strict equality.
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        for _ in range(iterations):
            out = initialized_provider.handle_tool_call("icm_recall", {"query": "x"})
            parsed = json.loads(out)
            assert parsed == {"hits": []}, f"{mode_id}: degraded shape regression"

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "hermes_icm_memory.tools"
    ]
    assert len(warnings) == iterations, (
        f"{mode_id}: expected {iterations} WARNINGs from hermes_icm_memory.tools "
        f"(one per call, NOT rate-limited at the tool boundary); got {len(warnings)}"
    )
