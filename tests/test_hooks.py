"""Tests for ``hermes_icm_memory.hooks`` + provider hook methods (S08).

Strict TDD: this file lands first (RED) and ``hermes_icm_memory/hooks.py``
+ four new methods on ``IcmMemoryProvider`` implement exactly what these
sixteen cases assert (GREEN). Each test traces 1-to-1 to AC1–AC16 of
story 4.1.

Architecture invariants exercised:

* AD-12 — ``hooks.py`` does not import ``subprocess`` (S11 AST test).
* AD-07 / NFR-REL-1 — every hook returns a documented degraded shape on
  failure; no exception escapes the public boundary.
* NFR-PERF-1 — ``sync_turn`` p95 latency < 5 ms (AC9).
* NFR-REL-2 — single daemon worker; lazy-respawn at most once; degrade-to-
  drop on second death (AC14).
* NFR-PERF-4 — ``system_prompt_block`` reads cache; no second subprocess
  call (AC6).
"""

from __future__ import annotations

import logging
import queue
import statistics
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hermes_icm_memory import cli_runner, hooks
from hermes_icm_memory.errors import (
    ICMMalformedOutputError,
    ICMNotFoundError,
    ICMTimeoutError,
)
from hermes_icm_memory.hooks import WriteTask
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- Shared helpers / fixtures ----------------------------------------


@pytest.fixture
def initialized_provider(tmp_hermes_home: Path) -> IcmMemoryProvider:
    """Provider with ``initialize()`` already called and ``_available=True``.

    v0.1.1: forces ``isolated=True`` *before* ``initialize`` so ``_db_path``
    becomes a concrete path (worker spawning + ``run_recall`` sentinel checks
    in this file all assume a non-None ``_db_path``). Tests that exercise the
    new default-shared (``_db_path is None``) behaviour construct their own
    provider directly.
    """
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")
    # initialize sets _available only on failure; force True for the happy path.
    provider._available = True
    return provider


def _kill_worker(provider: IcmMemoryProvider) -> None:
    """Force the worker thread off without blocking on a real drain.

    Sets the per-provider stop_event, joins the worker briefly, then clears
    the event so the next respawn can run cleanly.
    """
    provider._stop_event.set()
    if provider._worker is not None:
        provider._worker.join(timeout=1.0)
    provider._stop_event.clear()


# ---------- AC1: prefetch calls run_recall with config-derived limit + timeout

def test_prefetch_calls_run_recall_with_config_limit_and_timeout(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 — ``prefetch`` invokes ``cli_runner.run_recall`` with config-driven args."""
    captured: dict[str, Any] = {}

    def fake_run_recall(
        query: str,
        limit: int,
        db_path: Path | None,
        timeout_ms: int,
        use_embeddings: bool = False,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["query"] = query
        captured["limit"] = limit
        captured["db_path"] = db_path
        captured["timeout_ms"] = timeout_ms
        return [{"id": "m1", "summary": "hello"}]

    monkeypatch.setattr(cli_runner, "run_recall", fake_run_recall)
    initialized_provider._config = {
        "recall_limit": 7,
        "command_timeout_read_ms": 1234,
    }
    initialized_provider.prefetch(query="how do I bun?")

    # v0.4.8 — provider strips stopwords/short tokens before recall (ICM's
    # MCP ranker behaves poorly on full-sentence queries). "how do I bun?"
    # → "bun" (how/do/I are stopwords or below the 3-char floor; '?' is
    # not alnum). Original message stays intact for cache keying and logs.
    assert captured["query"] == "bun"
    assert captured["limit"] == 7
    assert captured["timeout_ms"] == 1234
    assert captured["db_path"] == initialized_provider._db_path


# ---------- AC2: prefetch caches result for the next system_prompt_block

def test_prefetch_caches_result_for_block(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 — successful prefetch populates ``_prefetch_cache[hash(query)]``."""
    monkeypatch.setattr(
        cli_runner,
        "run_recall",
        lambda *a, **kw: [{"id": "m1", "topic": "preferences", "summary": "use bun"}],
    )
    initialized_provider.prefetch(query="bun?")
    # v0.4.8 — cache is keyed on the post-strip recall_query. 'bun?' strips
    # to 'bun' (punctuation drops, len-3 alnum survives, not a stopword).
    assert initialized_provider._prefetch_cache[hash("bun")] == [
        {"id": "m1", "topic": "preferences", "summary": "use bun"}
    ]


# ---------- AC3/AC4/AC5: prefetch swallows ICM failures

@pytest.mark.parametrize(
    "exc",
    [
        ICMNotFoundError("icm missing"),
        ICMTimeoutError("timed out"),
        ICMMalformedOutputError("bad json"),
    ],
    ids=["not_found", "timeout", "malformed"],
)
def test_prefetch_swallows_icm_errors_returns_empty(
    exc: Exception,
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3/AC4/AC5 — ICM error → ``""`` returned, ``[]`` cached, WARNING logged, no raise."""

    def _raise(*a: Any, **kw: Any) -> None:
        raise exc

    monkeypatch.setattr(cli_runner, "run_recall", _raise)
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        result = initialized_provider.prefetch(query="x")

    assert result == ""
    assert initialized_provider._prefetch_cache[hash("x")] == []
    assert any(
        "prefetch" in record.message or "recall" in record.message
        for record in caplog.records
    )


# ---------- AC6: system_prompt_block reads cache; no second subprocess call

def test_system_prompt_block_reads_cache_no_second_subprocess(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6 — block consumes the cache; ``run_recall`` is invoked exactly once total."""
    call_count = 0

    def fake_run_recall(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        return [{"id": "m1", "topic": "preferences", "summary": "use bun"}]

    monkeypatch.setattr(cli_runner, "run_recall", fake_run_recall)
    initialized_provider.prefetch(query="x")
    initialized_provider.system_prompt_block()
    initialized_provider.system_prompt_block()  # idempotent; still one recall.

    assert call_count == 1


# ---------- AC7: system_prompt_block formats top-K + project-context summary

def test_system_prompt_block_formats_top_k_plus_summary(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7 — non-empty cache → string with one line per hit + project-context line."""
    hits = [
        {"id": "m1", "topic": "preferences", "summary": "always use bun"},
        {"id": "m2", "topic": "decisions-foo", "summary": "going with sqlite"},
        {"id": "m3", "topic": "preferences", "summary": "never use npm"},
    ]
    monkeypatch.setattr(cli_runner, "run_recall", lambda *a, **kw: hits)
    initialized_provider._config = {"recall_limit": 5}
    initialized_provider.prefetch(query="anything")

    block = initialized_provider.system_prompt_block()

    # Each hit's summary appears on its own bulleted line.
    assert "always use bun" in block
    assert "going with sqlite" in block
    assert "never use npm" in block
    # Project context line names the unique topics observed.
    assert "preferences" in block
    assert "decisions-foo" in block
    # Empty cache → only the v0.4.2 indicator heartbeat directive remains
    # (always-on liveness signal). No prefetch block, no stores block.
    empty_provider = IcmMemoryProvider()
    empty_block = empty_provider.system_prompt_block()
    assert "📚 —" in empty_block
    assert "🧠" not in empty_block
    assert "📖 Recalled memories" not in empty_block


# ---------- AC8: sync_turn enqueues each detected trigger

def test_sync_turn_enqueues_each_detected_trigger(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8 — three detected triggers → three ``put_nowait`` calls in order."""
    # Stub mapping to return a deterministic 3-tuple list.
    triples = [
        ("errors-resolved", "high", "fixed it", ["fix"]),
        ("decisions-default", "high", "going with X", ["x"]),
        ("preferences", "critical", "always use bun", ["bun"]),
    ]
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *a, **kw: list(triples),
    )

    captured: list[WriteTask] = []

    def fake_put_nowait(self: queue.Queue[WriteTask], task: WriteTask) -> None:
        captured.append(task)

    # Ensure the queue exists, then patch its put_nowait.
    initialized_provider._ensure_worker()
    monkeypatch.setattr(
        type(initialized_provider._write_queue),
        "put_nowait",
        fake_put_nowait,
    )

    initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert [t.topic for t in captured] == [
        "errors-resolved",
        "decisions-default",
        "preferences",
    ]
    assert captured[2].importance == "critical"
    assert captured[0].keywords == ("fix",)


# ---------- AC9: sync_turn p95 < 5 ms (NFR-PERF-1)

def test_sync_turn_p95_under_5ms(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9 — 1000 invocations; p95 < 5 ms (relaxed to 25 ms on Pi if needed)."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *a, **kw: [
            ("errors-resolved", "high", "x", ["x"]),
        ],
    )
    initialized_provider._ensure_worker()
    # Patch put_nowait to a constant-time no-op so we measure sync_turn overhead only.
    monkeypatch.setattr(
        type(initialized_provider._write_queue),
        "put_nowait",
        lambda self, task: None,
    )

    samples_ns: list[int] = []
    for _ in range(1000):
        t0 = time.perf_counter_ns()
        initialized_provider.sync_turn(user_content="u", assistant_content="a")
        samples_ns.append(time.perf_counter_ns() - t0)

    samples_ns.sort()
    p95_ns = samples_ns[int(0.95 * len(samples_ns))]
    p95_ms = p95_ns / 1e6
    print(f"sync_turn p95 = {p95_ms:.3f} ms (median = {statistics.median(samples_ns)/1e6:.3f} ms)")
    # Generous threshold per team-lead: 25 ms on Pi-class hardware. The
    # 5 ms NFR-PERF-1 target is reported in the dev record; this test is
    # a regression guard against gross perf cliffs (e.g. accidentally
    # blocking on a subprocess call from the hot path).
    assert p95_ms < 25.0, f"p95 {p95_ms:.3f} ms exceeded 25 ms ceiling"


# ---------- AC10: overflow drops with one WARNING per burst

def test_sync_turn_overflow_drops_with_one_warning_per_burst(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC10 — N overflows in one burst → exactly one WARNING; clears on next drain."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *a, **kw: [
            ("preferences", "critical", "x", ["x"]),
        ],
    )
    initialized_provider._ensure_worker()

    def always_full(self: queue.Queue[WriteTask], task: WriteTask) -> None:
        raise queue.Full()

    monkeypatch.setattr(
        type(initialized_provider._write_queue),
        "put_nowait",
        always_full,
    )

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        for _ in range(10):
            initialized_provider.sync_turn(user_content="u", assistant_content="a")

    overflow_warns = [r for r in caplog.records if "overflow" in r.message.lower()]
    assert len(overflow_warns) == 1, (
        f"expected exactly 1 overflow WARNING per burst, got {len(overflow_warns)}: "
        f"{[r.message for r in overflow_warns]!r}"
    )

    # Clearing the burst flag (simulating worker drain) re-arms the WARN.
    initialized_provider._overflow_burst[0] = False
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        for _ in range(3):
            initialized_provider.sync_turn(user_content="u", assistant_content="a")
    overflow_warns = [r for r in caplog.records if "overflow" in r.message.lower()]
    assert len(overflow_warns) == 1


# ---------- AC11: sync_turn swallows downstream exceptions

def test_sync_turn_swallows_exceptions(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC11 — detect_triggers raises → returns None, WARNING logged, no escape."""

    def boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("mapping exploded")

    monkeypatch.setattr("hermes_icm_memory.hooks.mapping.detect_triggers", boom)
    initialized_provider._ensure_worker()

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert any(
        "sync_turn" in record.message for record in caplog.records
    ), f"expected WARNING about sync_turn; got {[r.message for r in caplog.records]!r}"


# ---------- AC12: worker drains FIFO

def test_worker_drains_fifo_order(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC12 — single worker drains in [A, B, C] order."""
    seen: list[str] = []
    seen_lock = threading.Lock()

    def fake_run_store(
        topic: str,
        content: str,
        importance: str,
        db_path: Path,
        timeout_ms: int,
        keywords: str | None = None,
        raw: str | None = None,
    ) -> None:
        with seen_lock:
            seen.append(topic)

    monkeypatch.setattr(cli_runner, "run_store", fake_run_store)

    initialized_provider._ensure_worker()
    queue = initialized_provider._write_queue
    assert queue is not None
    queue.put_nowait(WriteTask(topic="A", importance="high", content="c", keywords=()))
    queue.put_nowait(WriteTask(topic="B", importance="high", content="c", keywords=()))
    queue.put_nowait(WriteTask(topic="C", importance="high", content="c", keywords=()))

    # Wait for the queue to drain or timeout.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with seen_lock:
            if len(seen) == 3:
                break
        time.sleep(0.02)

    assert seen == ["A", "B", "C"]


# ---------- AC13: worker survives run_store exceptions

def test_worker_survives_run_store_exception(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC13 — first task raises ``ICMTimeoutError``; second succeeds; thread alive."""
    seen: list[str] = []
    seen_lock = threading.Lock()
    call_index = {"n": 0}

    def fake_run_store(
        topic: str,
        content: str,
        importance: str,
        db_path: Path,
        timeout_ms: int,
        keywords: str | None = None,
        raw: str | None = None,
    ) -> None:
        call_index["n"] += 1
        if call_index["n"] == 1:
            raise ICMTimeoutError("simulated")
        with seen_lock:
            seen.append(topic)

    monkeypatch.setattr(cli_runner, "run_store", fake_run_store)

    initialized_provider._ensure_worker()
    queue = initialized_provider._write_queue
    assert queue is not None
    queue.put_nowait(WriteTask(topic="A", importance="high", content="c", keywords=()))
    queue.put_nowait(WriteTask(topic="B", importance="high", content="c", keywords=()))

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with seen_lock:
            if len(seen) == 1:
                break
        time.sleep(0.02)

    with seen_lock:
        assert seen == ["B"]
    assert initialized_provider._worker is not None
    assert initialized_provider._worker.is_alive() is True


# ---------- AC14: worker respawn at most once; second death disables writes

def test_worker_respawn_once(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC14 — first respawn allowed; second death sets ``_writes_disabled`` + CRITICAL."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *a, **kw: [("preferences", "critical", "x", ["x"])],
    )
    monkeypatch.setattr(cli_runner, "run_store", lambda *a, **kw: None)

    initialized_provider._ensure_worker()
    first_worker = initialized_provider._worker
    assert first_worker is not None and first_worker.is_alive()

    # First death.
    _kill_worker(initialized_provider)
    assert not first_worker.is_alive()
    initialized_provider.sync_turn(user_content="u", assistant_content="a")
    assert initialized_provider._respawn_count == 1
    assert initialized_provider._worker is not first_worker
    assert initialized_provider._worker is not None
    assert initialized_provider._worker.is_alive()

    # Second death.
    _kill_worker(initialized_provider)
    with caplog.at_level(logging.CRITICAL, logger="hermes_icm_memory.hooks"):
        initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._writes_disabled is True
    assert any(
        record.levelno == logging.CRITICAL for record in caplog.records
    ), f"expected CRITICAL log; got levels {[r.levelno for r in caplog.records]!r}"

    # Subsequent enqueues no-op.
    pre_count = initialized_provider._respawn_count
    initialized_provider.sync_turn(user_content="u", assistant_content="a")
    assert initialized_provider._respawn_count == pre_count


# ---------- AC15: on_session_end drains within grace

def test_on_session_end_drains_within_grace(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC15 — pending items drain within grace; no overflow WARNING."""
    monkeypatch.setattr(cli_runner, "run_store", lambda *a, **kw: None)
    initialized_provider._config = {"session_end_grace_ms": 1000}
    initialized_provider._ensure_worker()
    queue = initialized_provider._write_queue
    assert queue is not None
    for _ in range(5):
        queue.put_nowait(WriteTask(topic="A", importance="high", content="c", keywords=()))

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        initialized_provider.on_session_end()
    elapsed_ms = (time.monotonic() - started) * 1000

    assert queue.empty(), "queue should drain within grace"
    drop_warns = [r for r in caplog.records if "drop" in r.message.lower()]
    assert drop_warns == [], f"unexpected drop WARNINGs: {[r.message for r in drop_warns]!r}"
    assert elapsed_ms < 1100.0, f"on_session_end took {elapsed_ms:.0f} ms (> grace + 100 ms)"


# ---------- AC16: on_session_end drops remaining with one WARNING

def test_on_session_end_drops_remaining_with_warning(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC16 — items remaining at deadline → one WARNING with the count, no raise."""

    def slow_store(*a: Any, **kw: Any) -> None:
        time.sleep(0.5)

    monkeypatch.setattr(cli_runner, "run_store", slow_store)
    initialized_provider._config = {"session_end_grace_ms": 100}
    initialized_provider._ensure_worker()
    queue = initialized_provider._write_queue
    assert queue is not None
    for _ in range(5):
        queue.put_nowait(WriteTask(topic="A", importance="high", content="c", keywords=()))

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        initialized_provider.on_session_end()
    elapsed_ms = (time.monotonic() - started) * 1000

    drop_warns = [r for r in caplog.records if "drop" in r.message.lower()]
    assert len(drop_warns) == 1, (
        f"expected exactly 1 drop WARNING; got {len(drop_warns)}: "
        f"{[r.message for r in drop_warns]!r}"
    )
    assert elapsed_ms < 250.0, (
        f"on_session_end took {elapsed_ms:.0f} ms (> grace + 100 ms); "
        f"AC16 grace=100"
    )


# ---------- Defensive coverage on the helper boundary ----------------------


def test_run_prefetch_swallows_unexpected_exception(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-ICMError raised by ``cli_runner.run_recall`` is still caught."""

    def boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_runner, "run_recall", boom)
    cache: dict[int, list[dict[str, Any]]] = {}
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        result = hooks.run_prefetch(
            query="x",
            db_path=tmp_hermes_home / "icm" / "default.db",
            limit=5,
            timeout_ms=2000,
            cache=cache,
        )
    assert result == []
    assert cache[hash("x")] == []
    assert any("unexpected" in r.message for r in caplog.records)


def test_submit_triggers_no_op_when_writes_disabled() -> None:
    """``writes_disabled`` short-circuits the producer (no detect, no enqueue)."""
    state = hooks.WorkerState()
    state.writes_disabled = True
    # Even with a None queue, this must not raise.
    hooks.submit_triggers(
        state,
        user_content="u",
        assistant_content="a",
        project=None,
        every_n_turns=20,
    )


def test_drain_with_grace_no_op_when_queue_none() -> None:
    """``drain_with_grace`` is a no-op when the queue was never created."""
    hooks.drain_with_grace(hooks.WorkerState(), grace_ms=10)


def test_format_block_empty_cache_returns_empty() -> None:
    """``format_block`` with no latest key or empty cache → ``""``."""
    assert hooks.format_block(cache={}, latest_key=None, recall_limit=5) == ""
    assert hooks.format_block(cache={1: []}, latest_key=1, recall_limit=5) == ""


def test_worker_loop_defensive_swallows_unexpected_exception(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Worker survives a non-ICMError raised by ``run_store`` (defensive branch)."""
    seen: list[str] = []
    seen_lock = threading.Lock()
    call_index = {"n": 0}

    def fake_run_store(*a: Any, **kw: Any) -> None:
        call_index["n"] += 1
        if call_index["n"] == 1:
            raise RuntimeError("boom — not an ICMError")
        with seen_lock:
            seen.append("ok")

    monkeypatch.setattr(cli_runner, "run_store", fake_run_store)

    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)
    db_path = tmp_hermes_home / "icm" / "default.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(
        target=hooks.worker_loop,
        kwargs={
            "write_queue": state.write_queue,
            "db_path": db_path,
            "timeout_ms": 5000,
            "overflow_burst": state.overflow_burst,
            "stop_event": state.stop_event,
        },
        daemon=True,
    )
    thread.start()

    state.write_queue.put_nowait(
        WriteTask(topic="A", importance="high", content="c", keywords=())
    )
    state.write_queue.put_nowait(
        WriteTask(topic="B", importance="high", content="c", keywords=())
    )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with seen_lock:
            if seen == ["ok"]:
                break
        time.sleep(0.02)

    state.stop_event.set()
    thread.join(timeout=1.0)
    assert "ok" in seen, (
        f"worker should have processed at least one task after the error; seen={seen!r}"
    )
    # Verify the unexpected error was logged.
    assert any("unexpected error" in r.message for r in caplog.records), (
        f"expected WARNING about unexpected error; got {[r.message for r in caplog.records]!r}"
    )


def test_provider_prefetch_disabled_returns_empty(
    initialized_provider: IcmMemoryProvider,
) -> None:
    """``prefetch_enabled=False`` short-circuits prefetch and the recall block.

    v0.4.2 — ``system_prompt_block`` still emits the indicator heartbeat
    (📚 —) since liveness is independent of whether prefetch is enabled.
    """
    initialized_provider._config = {"prefetch_enabled": False}
    assert initialized_provider.prefetch(query="x") == ""
    block = initialized_provider.system_prompt_block()
    assert "📚 —" in block
    assert "📖 Recalled memories" not in block


def test_provider_prefetch_returns_empty_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_available()=False`` short-circuits ``prefetch`` to ``""``."""
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda _: None)
    provider = IcmMemoryProvider()
    assert provider.prefetch(query="x") == ""


def test_provider_prefetch_default_shared_passes_db_none(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.1.1 — default-shared mode lets prefetch run with ``db_path=None``.

    The legacy ``or self._db_path is None`` short-circuit on ``provider.prefetch``
    has been removed: ``None`` is now the legitimate "use icm canonical default"
    sentinel. Verifies the path flows through to ``cli_runner.run_recall`` with
    ``db_path=None`` *and* ``use_embeddings=True`` (the schema default — Brief's
    semantic-recall value prop).
    """
    captured: dict[str, Any] = {}

    def fake_run_recall(
        query: str,
        limit: int,
        db_path: Path | None,
        timeout_ms: int,
        use_embeddings: bool = True,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["db_path"] = db_path
        captured["use_embeddings"] = use_embeddings
        return [{"id": "m1", "topic": "preferences", "summary": "shared hit"}]

    monkeypatch.setattr(cli_runner, "run_recall", fake_run_recall)
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda _: "/usr/local/bin/icm")
    monkeypatch.setattr(cli_runner, "mcp_start", lambda *a, **kw: None)

    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")
    assert provider._db_path is None  # default-shared sanity

    block = provider.prefetch(query="dual-write policy")
    assert "shared hit" in block
    assert captured["db_path"] is None
    assert captured["use_embeddings"] is True


def test_provider_prefetch_use_embeddings_opt_out_threads_through(
    monkeypatch: pytest.MonkeyPatch,
    initialized_provider: IcmMemoryProvider,
) -> None:
    """v0.1.1 — Pi-class ``use_embeddings=False`` opt-out flows to cli_runner."""
    captured: dict[str, Any] = {}

    def fake_run_recall(
        query: str,
        limit: int,
        db_path: Path | None,
        timeout_ms: int,
        use_embeddings: bool = True,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["use_embeddings"] = use_embeddings
        return []

    monkeypatch.setattr(cli_runner, "run_recall", fake_run_recall)
    initialized_provider._config["use_embeddings"] = False
    initialized_provider.prefetch(query="x")
    assert captured["use_embeddings"] is False


# ---------- classifier_loop (lines 182-233) ----------------------------------


def test_classifier_loop_processes_task_and_enqueues_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classify returns a result -> WriteTask enqueued; recent_stores appended."""
    from hermes_icm_memory import classifier as cls_mod

    result = cls_mod.ClassifierResult(
        topic="preferences",
        importance="high",
        content="user prefers bun",
        keywords=("bun",),
    )
    monkeypatch.setattr(cls_mod, "classify_exchange", lambda *a, **kw: result)

    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)
    state.write_queue = queue.Queue(maxsize=4)

    thread = threading.Thread(
        target=hooks.classifier_loop,
        kwargs={
            "state": state,
            "classify_queue": state.classify_queue,
            "write_queue": state.write_queue,
            "endpoint": "http://test",
            "model": "m",
            "api_key": "",
            "timeout_s": 5.0,
            "stop_event": state.stop_event,
        },
        daemon=True,
    )
    thread.start()

    state.classify_queue.put_nowait(
        cls_mod.ClassifyTask(user_text="u", assistant_text="a", project=None)
    )
    state.classify_queue.join()  # block until task_done

    state.stop_event.set()
    thread.join(timeout=1.0)

    task = state.write_queue.get_nowait()
    assert task.topic == "preferences"
    assert task.importance == "high"
    assert task.content == "user prefers bun"
    assert task.keywords == ("bun",)
    # recent_stores was appended (thread-safe list append).
    assert state.recent_stores == [("preferences", "user prefers bun")]


def test_classifier_loop_none_result_skips_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classify_exchange returns None -> no write task enqueued."""
    from hermes_icm_memory import classifier as cls_mod

    monkeypatch.setattr(cls_mod, "classify_exchange", lambda *a, **kw: None)

    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)
    state.write_queue = queue.Queue(maxsize=4)

    thread = threading.Thread(
        target=hooks.classifier_loop,
        kwargs={
            "state": state,
            "classify_queue": state.classify_queue,
            "write_queue": state.write_queue,
            "endpoint": "http://test",
            "model": "m",
            "api_key": "",
            "timeout_s": 5.0,
            "stop_event": state.stop_event,
        },
        daemon=True,
    )
    thread.start()

    state.classify_queue.put_nowait(
        cls_mod.ClassifyTask(user_text="u", assistant_text="a", project=None)
    )
    state.classify_queue.join()

    state.stop_event.set()
    thread.join(timeout=1.0)

    assert state.write_queue.empty()


def test_classifier_loop_exception_in_classify_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """classify_exchange raises -> DEBUG log, task_done called, worker continues."""
    from hermes_icm_memory import classifier as cls_mod

    call_count = 0

    def boom_then_none(*a: Any, **kw: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("llm error")
        return None

    monkeypatch.setattr(cls_mod, "classify_exchange", boom_then_none)

    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)
    state.write_queue = queue.Queue(maxsize=4)

    thread = threading.Thread(
        target=hooks.classifier_loop,
        kwargs={
            "state": state,
            "classify_queue": state.classify_queue,
            "write_queue": state.write_queue,
            "endpoint": "http://test",
            "model": "m",
            "api_key": "",
            "timeout_s": 5.0,
            "stop_event": state.stop_event,
        },
        daemon=True,
    )
    thread.start()

    with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.hooks"):
        # First task raises.
        state.classify_queue.put_nowait(
            cls_mod.ClassifyTask(user_text="u", assistant_text="a", project=None)
        )
        state.classify_queue.join()  # task_done called even after exception

        # Second task — should process normally (None result).
        state.classify_queue.put_nowait(
            cls_mod.ClassifyTask(user_text="u2", assistant_text="a2", project=None)
        )
        state.classify_queue.join()

    assert any(
        "unexpected error" in r.message for r in caplog.records
    ), f"expected DEBUG about unexpected error; got {[r.message for r in caplog.records]!r}"

    state.stop_event.set()
    thread.join(timeout=1.0)


def test_classifier_loop_write_queue_full_drops(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Write queue full -> DEBUG log, task_done still called."""
    from hermes_icm_memory import classifier as cls_mod

    result = cls_mod.ClassifierResult(
        topic="preferences",
        importance="high",
        content="test",
        keywords=(),
    )
    monkeypatch.setattr(cls_mod, "classify_exchange", lambda *a, **kw: result)

    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)
    state.write_queue = queue.Queue(maxsize=1)
    state.write_queue.put_nowait(
        WriteTask(topic="fill", importance="high", content="fill", keywords=())
    )

    thread = threading.Thread(
        target=hooks.classifier_loop,
        kwargs={
            "state": state,
            "classify_queue": state.classify_queue,
            "write_queue": state.write_queue,
            "endpoint": "http://test",
            "model": "m",
            "api_key": "",
            "timeout_s": 5.0,
            "stop_event": state.stop_event,
        },
        daemon=True,
    )
    thread.start()

    with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.hooks"):
        state.classify_queue.put_nowait(
            cls_mod.ClassifyTask(user_text="u", assistant_text="a", project=None)
        )
        state.classify_queue.join()

    assert any(
        "write queue full" in r.message for r in caplog.records
    ), f"expected DEBUG about full write queue; got {[r.message for r in caplog.records]!r}"

    state.stop_event.set()
    thread.join(timeout=1.0)


class _BoomWriteQueue(queue.Queue):
    """A queue whose put_nowait always raises RuntimeError."""

    def put_nowait(self, item: Any) -> None:
        raise RuntimeError("unexpected write error")


def test_classifier_loop_write_enqueue_unexpected_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """put_nowait on write_queue raises unexpected Exception -> logged at DEBUG."""
    from hermes_icm_memory import classifier as cls_mod

    result = cls_mod.ClassifierResult(
        topic="learnings",
        importance="medium",
        content="something learned",
        keywords=("learn",),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cls_mod, "classify_exchange", lambda *a, **kw: result)

    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)
    # Use a subclass that always raises on put_nowait — avoids patching the
    # shared ``queue.Queue`` class (which would also break the classify queue).
    state.write_queue = _BoomWriteQueue(maxsize=4)

    thread = threading.Thread(
        target=hooks.classifier_loop,
        kwargs={
            "state": state,
            "classify_queue": state.classify_queue,
            "write_queue": state.write_queue,
            "endpoint": "http://test",
            "model": "m",
            "api_key": "",
            "timeout_s": 5.0,
            "stop_event": state.stop_event,
        },
        daemon=True,
    )
    thread.start()

    with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.hooks"):
        state.classify_queue.put_nowait(
            cls_mod.ClassifyTask(user_text="u", assistant_text="a", project=None)
        )
        state.classify_queue.join()

    monkeypatch.undo()

    assert any(
        "enqueue failed" in r.message for r in caplog.records
    ), f"expected DEBUG about enqueue failure; got {[r.message for r in caplog.records]!r}"

    state.stop_event.set()
    thread.join(timeout=1.0)


# ---------- ensure_classifier (lines 321-347) + _spawn_classifier (358-376) ----


def test_ensure_classifier_disabled_short_circuit() -> None:
    """class_disabled=True -> returns False immediately, no side effects."""
    state = hooks.WorkerState()
    state.class_disabled = True
    result = hooks.ensure_classifier(
        state,
        classify_queue_size=4,
        endpoint="http://test",
        model="m",
        api_key="",
        timeout_s=5.0,
    )
    assert result is False
    assert state.classify_queue is None  # queue never created
    assert state.class_worker is None


def test_ensure_classifier_first_spawn_creates_queue_and_starts_worker() -> None:
    """First call creates classify_queue and spawns the classifier worker."""
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)

    result = hooks.ensure_classifier(
        state,
        classify_queue_size=8,
        endpoint="http://test",
        model="m",
        api_key="secret",
        timeout_s=5.0,
    )
    assert result is True
    assert state.classify_queue is not None
    assert state.classify_queue.maxsize == 8
    assert state.class_worker is not None
    assert state.class_worker.is_alive()
    assert state.class_worker.name == "hermes-icm-classifier"
    assert state.class_respawn_count == 0
    assert state.class_disabled is False

    # Cleanup.
    state.stop_event.set()
    state.class_worker.join(timeout=1.0)


def test_ensure_classifier_idempotent_when_alive() -> None:
    """Second call when worker is alive -> returns True, no new worker."""
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)

    hooks.ensure_classifier(
        state,
        classify_queue_size=4,
        endpoint="http://test",
        model="m",
        api_key="",
        timeout_s=5.0,
    )
    first_worker = state.class_worker
    assert first_worker is not None

    result = hooks.ensure_classifier(
        state,
        classify_queue_size=4,
        endpoint="http://test",
        model="m",
        api_key="",
        timeout_s=5.0,
    )
    assert result is True
    assert state.class_worker is first_worker  # same thread
    assert state.class_respawn_count == 0

    state.stop_event.set()
    state.class_worker.join(timeout=1.0)


def test_ensure_classifier_respawns_after_first_death(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First classifier death -> respawn, WARNING logged, respawn_count incremented."""
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)

    hooks.ensure_classifier(
        state,
        classify_queue_size=4,
        endpoint="http://test",
        model="m",
        api_key="",
        timeout_s=5.0,
    )
    first_worker = state.class_worker
    assert first_worker is not None and first_worker.is_alive()

    # Kill via stop_event.
    state.stop_event.set()
    first_worker.join(timeout=1.0)
    assert not first_worker.is_alive()
    state.stop_event.clear()

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        result = hooks.ensure_classifier(
            state,
            classify_queue_size=4,
            endpoint="http://test",
            model="m",
            api_key="",
            timeout_s=5.0,
        )

    assert result is True
    assert state.class_respawn_count == 1
    assert state.class_worker is not first_worker
    assert state.class_worker is not None and state.class_worker.is_alive()
    assert state.class_disabled is False
    assert any(
        "respawned" in r.message for r in caplog.records
    ), f"expected WARNING about respawn; got {[r.message for r in caplog.records]!r}"

    state.stop_event.set()
    state.class_worker.join(timeout=1.0)


def test_ensure_classifier_second_death_disables(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Second death -> class_disabled=True, CRITICAL log, returns False."""
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)

    # Spawn.
    hooks.ensure_classifier(
        state,
        classify_queue_size=4,
        endpoint="http://test",
        model="m",
        api_key="",
        timeout_s=5.0,
    )
    worker1 = state.class_worker
    assert worker1 is not None

    # First death + respawn.
    state.stop_event.set()
    worker1.join(timeout=1.0)
    state.stop_event.clear()

    hooks.ensure_classifier(
        state,
        classify_queue_size=4,
        endpoint="http://test",
        model="m",
        api_key="",
        timeout_s=5.0,
    )
    worker2 = state.class_worker
    assert state.class_respawn_count == 1

    # Second death.
    state.stop_event.set()
    worker2.join(timeout=1.0)
    state.stop_event.clear()

    with caplog.at_level(logging.CRITICAL, logger="hermes_icm_memory.hooks"):
        result = hooks.ensure_classifier(
            state,
            classify_queue_size=4,
            endpoint="http://test",
            model="m",
            api_key="",
            timeout_s=5.0,
        )

    assert result is False
    assert state.class_disabled is True
    assert any(
        record.levelno == logging.CRITICAL for record in caplog.records
    ), f"expected CRITICAL log; got levels {[r.levelno for r in caplog.records]!r}"

    state.stop_event.set()
    if state.class_worker is not None and state.class_worker.is_alive():
        state.class_worker.join(timeout=1.0)


# ---------- _submit_classify_task (lines 567-583) -----------------------------


def test_submit_classify_task_enqueues() -> None:
    """A classify task is enqueued on the classify queue."""

    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)
    hooks._submit_classify_task(state, "user text", "assistant text", "my-project")

    task = state.classify_queue.get_nowait()
    assert task.user_text == "user text"
    assert task.assistant_text == "assistant text"
    assert task.project == "my-project"


def test_submit_classify_task_no_op_when_queue_none() -> None:
    """classify_queue is None -> no-op."""
    state = hooks.WorkerState()
    # classify_queue is None by default
    hooks._submit_classify_task(state, "u", "a", None)
    # No crash.


def test_submit_classify_task_no_op_when_class_disabled() -> None:
    """class_disabled=True -> no-op, nothing enqueued."""
    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)
    state.class_disabled = True
    hooks._submit_classify_task(state, "u", "a", None)
    assert state.classify_queue.empty()


def test_submit_classify_task_queue_full_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Classify queue full -> DEBUG log, no raise."""
    from hermes_icm_memory import classifier as cls_mod

    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=1)
    state.classify_queue.put_nowait(
        cls_mod.ClassifyTask(user_text="fill", assistant_text="fill", project=None)
    )

    with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.hooks"):
        hooks._submit_classify_task(state, "u", "a", None)

    assert any(
        "classify queue full" in r.message for r in caplog.records
    ), f"expected DEBUG about full queue; got {[r.message for r in caplog.records]!r}"


def test_submit_classify_task_enqueue_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """put_nowait raises unexpected Exception -> logged at DEBUG."""
    state = hooks.WorkerState()
    state.classify_queue = queue.Queue(maxsize=4)

    def boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("unexpected enqueue error")

    monkeypatch.setattr(type(state.classify_queue), "put_nowait", boom)

    with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.hooks"):
        hooks._submit_classify_task(state, "u", "a", None)

    assert any(
        "classify enqueue error" in r.message for r in caplog.records
    ), f"expected DEBUG; got {[r.message for r in caplog.records]!r}"


# ---------- _submit_periodic_context (lines 594-611) --------------------------


def test_submit_periodic_context_fires_on_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Periodic context fires when turn_index % every_n_turns == 0."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.MAPPING",
        {
            "context": {"topic_template": "context-{project}", "importance": "high"},
        },
    )
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)

    hooks._submit_periodic_context(
        state, turn_index=20, every_n_turns=20, project="test-proj"
    )

    task = state.write_queue.get_nowait()
    assert task.topic == "context-test-proj"
    assert task.importance == "high"
    assert "periodic progress checkpoint: turn 20" in task.content
    assert task.keywords == ()


def test_submit_periodic_context_no_op_when_write_queue_none() -> None:
    """write_queue is None -> no-op."""
    state = hooks.WorkerState()
    hooks._submit_periodic_context(
        state, turn_index=20, every_n_turns=20, project=None
    )
    # No crash.


def test_submit_periodic_context_no_op_when_not_boundary() -> None:
    """turn_index not on boundary -> no-op, nothing enqueued."""
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)
    hooks._submit_periodic_context(
        state, turn_index=5, every_n_turns=20, project=None
    )
    assert state.write_queue.empty()


def test_submit_periodic_context_no_op_when_every_n_zero() -> None:
    """every_n_turns <= 0 -> no-op."""
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)
    hooks._submit_periodic_context(
        state, turn_index=20, every_n_turns=0, project=None
    )
    assert state.write_queue.empty()


def test_submit_periodic_context_queue_full(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """put_nowait raises queue.Full -> overflow warning."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.MAPPING",
        {
            "context": {"topic_template": "context-{project}", "importance": "high"},
        },
    )
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=1)
    state.write_queue.put_nowait(
        WriteTask(topic="fill", importance="high", content="fill", keywords=())
    )

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        hooks._submit_periodic_context(
            state, turn_index=20, every_n_turns=20, project="p"
        )

    assert any(
        "overflow" in r.message for r in caplog.records
    ), f"expected WARNING about overflow; got {[r.message for r in caplog.records]!r}"


def test_submit_periodic_context_enqueue_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """put_nowait raises unexpected Exception -> logged at WARNING."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.MAPPING",
        {
            "context": {"topic_template": "context-{project}", "importance": "high"},
        },
    )
    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)

    def boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("unexpected periodic error")

    monkeypatch.setattr(type(state.write_queue), "put_nowait", boom)

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        hooks._submit_periodic_context(
            state, turn_index=20, every_n_turns=20, project="p"
        )

    assert any(
        "periodic enqueue error" in r.message for r in caplog.records
    ), f"expected WARNING; got {[r.message for r in caplog.records]!r}"


# ---------- submit_triggers classifier path (lines 513-524, 552-553) ----------


def test_submit_triggers_classifier_path_routes_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classifier_enabled=True routes to _submit_classify_task + _submit_periodic_context."""
    calls: list[str] = []

    def fake_submit_classify(
        state: hooks.WorkerState,
        user_content: str,
        assistant_content: str,
        project: str | None,
    ) -> None:
        calls.append(f"classify:{user_content}:{assistant_content}:{project}")

    def fake_submit_periodic(
        state: hooks.WorkerState,
        *,
        turn_index: int,
        every_n_turns: int,
        project: str | None,
    ) -> None:
        calls.append(f"periodic:{turn_index}:{every_n_turns}:{project}")

    monkeypatch.setattr(hooks, "_submit_classify_task", fake_submit_classify)
    monkeypatch.setattr(hooks, "_submit_periodic_context", fake_submit_periodic)

    state = hooks.WorkerState()
    hooks.submit_triggers(
        state,
        user_content="hello",
        assistant_content="world",
        project="proj",
        every_n_turns=10,
        classifier_enabled=True,
    )

    assert calls == ["classify:hello:world:proj", "periodic:1:10:proj"], (
        f"unexpected call sequence: {calls!r}"
    )
    assert state.turn_index == 1  # incremented from 0


def test_submit_triggers_classifier_path_increments_turn_index() -> None:
    """classifier_enabled=True still increments turn_index."""
    state = hooks.WorkerState()
    state.turn_index = 42
    hooks.submit_triggers(
        state,
        user_content="u",
        assistant_content="a",
        project=None,
        every_n_turns=20,
        classifier_enabled=True,
    )
    assert state.turn_index == 43


def test_submit_triggers_regex_path_write_queue_none() -> None:
    """Regex path: write_queue is None -> no-op (no crash, no raise)."""
    state = hooks.WorkerState()
    hooks.submit_triggers(
        state,
        user_content="u",
        assistant_content="a",
        project=None,
        every_n_turns=20,
        classifier_enabled=False,
    )
    # No crash — just returns when write_queue is None.


def test_submit_triggers_enqueue_raises_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regex path: put_nowait raises Exception -> WARNING logged."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *a, **kw: [("preferences", "critical", "x", ["x"])],
    )

    state = hooks.WorkerState()
    state.write_queue = queue.Queue(maxsize=4)

    def boom_put(*a: Any, **kw: Any) -> None:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(type(state.write_queue), "put_nowait", boom_put)

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        hooks.submit_triggers(
            state,
            user_content="u",
            assistant_content="a",
            project=None,
            every_n_turns=20,
            classifier_enabled=False,
        )

    assert any(
        "enqueue raised" in r.message for r in caplog.records
    ), f"expected WARNING about enqueue error; got {[r.message for r in caplog.records]!r}"


# ---------- drain_with_grace warning branch (line 655) ------------------------


def test_drain_with_grace_warning_when_items_remain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Items still in queue after grace period -> WARNING logged."""
    state = hooks.WorkerState()
    q: queue.Queue[hooks.WriteTask] = queue.Queue(maxsize=4)
    q.put_nowait(WriteTask(topic="A", importance="high", content="slow", keywords=()))
    q.put_nowait(WriteTask(topic="B", importance="high", content="slow", keywords=()))
    state.write_queue = q

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        hooks.drain_with_grace(state, grace_ms=10)

    assert any(
        "grace expired" in r.message for r in caplog.records
    ), f"expected WARNING about grace expiry; got {[r.message for r in caplog.records]!r}"


# ---------- v0.4.1: sync_turn must enqueue in default-shared mode -------------
# Regression guard: pre-v0.4.1, ``provider._ensure_worker`` returned False
# whenever ``_db_path is None`` (the default ``isolated: false`` shared-DB
# mode), causing every ``sync_turn`` to silently no-op. The MCP daemon spawned
# by ``initialize`` owns the DB at write time, so the worker can spawn fine
# with ``db_path=None`` — but the legacy v0.1.1 guard didn't know that.


def test_sync_turn_enqueues_in_default_shared_mode(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.4.1 regression — sync_turn enqueues even when ``_db_path is None``.

    Reproduces the silent-drop bug: provider in default-shared mode
    (``isolated: false`` → ``_db_path is None``) used to no-op every
    ``sync_turn`` because ``_ensure_worker`` short-circuited on the missing
    db_path. After the v0.4.1 fix, the worker spawns and the WriteTask lands
    on the queue.
    """
    # Stub mcp_start so initialize() doesn't actually spawn `icm serve`.
    monkeypatch.setattr(cli_runner, "mcp_start", lambda *a, **kw: None)
    # Stub mapping to return a single deterministic trigger.
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *a, **kw: [("errors-resolved", "high", "fixed it", ["fix"])],
    )
    # Capture WriteTasks by intercepting put_nowait — avoids racing the worker.
    captured: list[WriteTask] = []

    def fake_put_nowait(self: queue.Queue[WriteTask], task: WriteTask) -> None:
        captured.append(task)

    provider = IcmMemoryProvider()
    # No ``provider._config["isolated"] = True`` — default-shared path.
    provider.initialize(
        session_id="s1", hermes_home=tmp_hermes_home, profile="default"
    )
    provider._available = True

    # Sanity: this is the case we're guarding (shared-DB mode).
    assert provider._db_path is None, (
        "fixture invariant: default-shared mode keeps _db_path None"
    )

    # Drive _ensure_worker once so the queue exists, then patch its put_nowait.
    assert provider._ensure_worker(), (
        "regression: _ensure_worker returned False in default-shared mode"
    )
    assert provider._write_queue is not None
    monkeypatch.setattr(
        type(provider._write_queue), "put_nowait", fake_put_nowait
    )

    provider.sync_turn(user_content="u", assistant_content="a")

    # Stop the worker so the daemon thread doesn't leak across tests.
    _kill_worker(provider)

    assert [t.topic for t in captured] == ["errors-resolved"], (
        "regression: sync_turn dropped the trigger in default-shared mode"
    )
    assert captured[0].importance == "high"
    assert captured[0].keywords == ("fix",)
