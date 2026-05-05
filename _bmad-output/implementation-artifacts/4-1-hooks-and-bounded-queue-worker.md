# Story 4.1: Hooks (`prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`) + bounded-queue worker

Status: done
Story ID: S08 · Epic: 4 (Memory operations) · Effort: L · Dependencies: S04 (cli_runner, errors), S06 (mapping), S07 (provider)

## Story

As a Hermes user,
I want the plugin to recall memories before each LLM call, inject them into the system prompt, and persist new memories non-blockingly after each turn,
so that my agent acts informed and never pays turn-perceptible latency for memory writes (FR5, FR9, FR10, FR13, FR14, FR15, NFR-PERF-1, NFR-REL-1, NFR-REL-2).

## Acceptance Criteria

**AC1 — `prefetch` calls `cli_runner.run_recall` with config-derived limit + timeout (FR9, NFR-PERF-4)**

- **Given** an initialized provider with `_db_path` set, `_available=True`, `_config` containing `recall_limit=K` and `command_timeout_read_ms=T`
- **When** `provider.prefetch(query="...")` is called
- **Then** it invokes `cli_runner.run_recall(query, limit=K, db_path=self._db_path, timeout_ms=T)` exactly once and stores the returned hits in `self._prefetch_cache[hash(query)]`.

**AC2 — `prefetch` caches the result for the immediately-following `system_prompt_block` (NFR-PERF-4)**

- **Given** `provider.prefetch(query="x")` has executed successfully
- **When** `provider.system_prompt_block()` is called next
- **Then** it reads the cached hits **without** a second call to `cli_runner.run_recall`.

**AC3 — `prefetch` swallows `ICMNotFoundError` and returns the empty string (FR19, NFR-REL-1)**

- **Given** `cli_runner.run_recall` raises `ICMNotFoundError`
- **When** `prefetch(query=...)` is called
- **Then** it returns `""`, the cache stores `[]` for that query hash, and a WARNING is logged via `hermes_icm_memory.hooks` with `extra={"err": ...}`. **No exception escapes**.

**AC4 — `prefetch` swallows `ICMTimeoutError` and returns the empty string (FR19, NFR-REL-1)**

- **Given** `cli_runner.run_recall` raises `ICMTimeoutError`
- **When** `prefetch(query=...)` is called
- **Then** it returns `""`, caches `[]`, logs WARNING, never raises.

**AC5 — `prefetch` swallows `ICMMalformedOutputError` and returns the empty string**

- **Given** `cli_runner.run_recall` raises `ICMMalformedOutputError`
- **When** `prefetch(query=...)` is called
- **Then** it returns `""`, caches `[]`, logs WARNING, never raises.

**AC6 — `system_prompt_block` reads cache; **no second subprocess call** (NFR-PERF-4)**

- **Given** the prefetch cache holds `[{"id": "m1", "summary": "..."}, ...]` for the latest query
- **When** `provider.system_prompt_block()` is called
- **Then** it reads the cache and returns a non-empty string, while the test asserts `cli_runner.run_recall` was called **zero** additional times after the prefetch step.

**AC7 — `system_prompt_block` formats top-K hits + project-context summary**

- **Given** the prefetch cache holds N≥1 hits
- **When** `system_prompt_block()` is called
- **Then** the returned string contains (a) one bulleted line per hit (capped at `recall_limit`) and (b) a one-paragraph "project context" summary derived from the unique topics seen across the hits. Empty cache → returns `""`.

**AC8 — `sync_turn` enqueues each detected trigger (FR14)**

- **Given** `mapping.detect_triggers(...)` returns 3 trigger tuples for a turn
- **When** `provider.sync_turn(user_content, assistant_content)` is called
- **Then** `self._write_queue.put_nowait` is called exactly 3 times in detection order, with each tuple unwrapped into a `WriteTask` (or equivalent record carrying `topic, importance, content, keywords`).

**AC9 — `sync_turn` p95 latency < 5 ms (NFR-PERF-1)**

- **Given** the worker is running and the queue has spare capacity
- **When** `sync_turn` is invoked 1000× with mocked `_write_queue.put_nowait` (constant-time)
- **Then** the p95 of measured per-call wall time is < 5 ms. Threshold may be relaxed to ≤ 25 ms with a `# noqa` rationale only if the Pi run shows GC noise dominating; the actual p95 number is reported in the dev record.

**AC10 — `sync_turn` overflow drops with **one** WARNING per burst (FR15)**

- **Given** the queue is at capacity and `put_nowait` raises `queue.Full`
- **When** `sync_turn` is invoked N times in succession with the queue still full
- **Then** exactly **one** WARNING log is emitted for the entire burst (subsequent overflows during the same burst are silent), the tasks are dropped (not retried), and `sync_turn` returns `None` quickly. After the worker drains one task successfully, the burst flag resets so the next burst will WARN once again.

**AC11 — `sync_turn` swallows downstream exceptions (FR19, NFR-REL-1)**

- **Given** `mapping.detect_triggers` is patched to raise
- **When** `sync_turn` is invoked
- **Then** the function returns `None`, a WARNING is logged, and **no exception escapes** the call.

**AC12 — Worker drains FIFO order**

- **Given** the worker is running and `[task_A, task_B, task_C]` are enqueued in that order
- **When** the worker drains
- **Then** `cli_runner.run_store` is invoked in the order `A, B, C` — single worker preserves FIFO without locking.

**AC13 — Worker survives `cli_runner.run_store` exceptions**

- **Given** `cli_runner.run_store` raises `ICMTimeoutError` on the first task and succeeds on the second
- **When** the worker processes both
- **Then** the worker logs a WARNING for the first failure, processes the second successfully, and is still alive (`worker.is_alive() is True`) afterwards. The worker loop's `try/except` boundary never lets the thread die from a per-task exception.

**AC14 — Worker lazy-respawn at most once; second death disables writes (NFR-REL-2)**

- **Given** the worker thread is killed once externally
- **When** the next `put_nowait` notices `not worker.is_alive()`
- **Then** the worker is respawned exactly once (`_respawn_count == 1`); a subsequent kill + enqueue sets `self._writes_disabled = True`, emits a CRITICAL log via `hermes_icm_memory.hooks`, and all further enqueues no-op.

**AC15 — `on_session_end` drains pending items within `session_end_grace_ms` (FR5)**

- **Given** the queue has pending items and the worker is alive
- **When** `provider.on_session_end()` is called with `config.session_end_grace_ms = 1000`
- **Then** the method blocks at most ~1.1 s (`grace + 100 ms`); if the worker drained everything within the grace, the method returns having seen `queue.empty() == True` and **no overflow WARNING** is logged.

**AC16 — `on_session_end` drops remaining items with a single WARNING when grace expires (FR5)**

- **Given** the worker is intentionally slow and 5 items remain in the queue when the grace expires
- **When** `on_session_end()` returns
- **Then** the method has emitted exactly **one** WARNING log via `hermes_icm_memory.hooks` naming the count of dropped items and the grace value, the queue is left non-empty (the daemon thread will exit at process shutdown), and **no exception escapes**.

**Cross-cutting invariants honored:**

- AD-12: `hermes_icm_memory/hooks.py` does **not** `import subprocess` (S11 AST test enforces).
- AD-13: module-level `logger = logging.getLogger(__name__)`; structured `extra={...}` on every WARNING / CRITICAL.
- AD-07 / NFR-REL-1: every public method (`prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`) catches at the boundary and never propagates.

## Tasks / Subtasks

- [x] **Task 1 — Story spec (Phase 1 / `/bmad-create-story`)**
  - Captured sixteen ACs, mapped each to a test in the test plan, froze the file spec.
- [x] **Task 2 — Phase 2 / `/bmad-dev-story` (TDD)**
  - RED → GREEN: 16 ACs + 7 defensive coverage cases in `tests/test_hooks.py`. hooks.py + provider.py implemented.
- [x] **Task 3 — Phase 3 / `/bmad-code-review`**
  - PASS, zero findings. All 16 ACs trace 1-to-1 to passing tests.
- [x] **Task 4 — Phase 4 / `/simplify`**
  - Three findings applied (`_DEFAULT_CONFIG` dict, property types, redundant short-circuit dropped); two skipped with rationale.

## File Spec

### `hermes_icm_memory/hooks.py` (NEW)

Public surface:

```python
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from . import cli_runner
from . import mapping
from .errors import ICMError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WriteTask:
    """Single ICM write task drained by the worker thread."""

    topic: str
    importance: str
    content: str
    keywords: tuple[str, ...]  # tuple for hashability/immutability


def prefetch(
    *,
    db_path: Path,
    query: str,
    limit: int,
    timeout_ms: int,
    cache: dict[int, list[dict[str, Any]]],
) -> str:
    """Run a recall, cache hits keyed by ``hash(query)``, return formatted string."""


def system_prompt_block(
    *,
    cache: dict[int, list[dict[str, Any]]],
    recall_limit: int,
) -> str:
    """Compose top-K block + one-paragraph project-context summary from cache only."""


def detect_and_enqueue(
    *,
    user_content: str,
    assistant_content: str,
    project: str | None,
    turn_index: int,
    every_n_turns: int,
    write_queue: queue.Queue[WriteTask],
    overflow_burst: list[bool],  # 1-element mutable bool — see _warn_overflow_once
) -> None:
    """`sync_turn` body: detect → enqueue, drop on full with one-WARN-per-burst."""


def drain_with_grace(
    *,
    write_queue: queue.Queue[WriteTask],
    grace_ms: int,
) -> int:
    """`on_session_end` body: wait up to grace_ms for empty; return remaining count."""


def worker_loop(
    *,
    write_queue: queue.Queue[WriteTask],
    db_path: Path,
    timeout_ms: int,
    overflow_burst: list[bool],  # cleared after each successful drain
    stop_event: threading.Event,
) -> None:
    """Daemon worker body: blocking get + run_store; per-task try/except so the thread never dies."""
```

`IcmMemoryProvider` extension (added in `provider.py` without disturbing existing methods):

State holders added in `__init__`:

- `self._prefetch_cache: dict[int, list[dict[str, Any]]] = {}`
- `self._write_queue: queue.Queue[WriteTask] | None = None`  (lazy-init on first need)
- `self._worker: threading.Thread | None = None`
- `self._writes_disabled: bool = False`
- `self._overflow_burst: list[bool] = [False]`  (1-element mutable; producer sets, worker clears)
- `self._respawn_count: int = 0`
- `self._turn_index: int = 0`
- `self._stop_event: threading.Event = threading.Event()`

New methods on `IcmMemoryProvider`:

```python
def prefetch(self, query: str, **kwargs: Any) -> str: ...
def system_prompt_block(self, **kwargs: Any) -> str: ...
def sync_turn(
    self,
    user_content: str = "",
    assistant_content: str = "",
    **kwargs: Any,
) -> None: ...
def on_session_end(self, messages: Any = None, **kwargs: Any) -> None: ...
```

Worker management lives on the provider (so its state — `_writes_disabled`, `_respawn_count` — is per-instance):

- `_ensure_worker(self) -> None` — create the queue + spawn the daemon thread on first need.
- `_respawn_worker(self) -> None` — invoked when `is_alive()` returns False; first call respawns; second call sets `_writes_disabled` + CRITICAL-logs.

### `tests/test_hooks.py` (NEW)

Sixteen TDD cases — one per AC1–AC16:

1. `test_prefetch_calls_run_recall_with_config_limit_and_timeout` (AC1)
2. `test_prefetch_caches_result_for_block` (AC2)
3. `test_prefetch_swallows_icm_not_found_returns_empty` (AC3)
4. `test_prefetch_swallows_timeout_returns_empty` (AC4)
5. `test_prefetch_swallows_malformed_returns_empty` (AC5)
6. `test_system_prompt_block_reads_cache_no_second_subprocess` (AC6)
7. `test_system_prompt_block_formats_top_k_plus_summary` (AC7)
8. `test_sync_turn_enqueues_each_detected_trigger` (AC8)
9. `test_sync_turn_p95_under_5ms` (AC9) — 1000× run; reports actual p95.
10. `test_sync_turn_overflow_drops_with_one_warning_per_burst` (AC10)
11. `test_sync_turn_swallows_exceptions` (AC11)
12. `test_worker_drains_fifo_order` (AC12)
13. `test_worker_survives_run_store_exception` (AC13)
14. `test_worker_respawn_once` (AC14)
15. `test_on_session_end_drains_within_grace` (AC15)
16. `test_on_session_end_drops_remaining_with_warning` (AC16)

## Dev Notes

### Architecture compliance (must follow)

- **AD-12 (no subprocess outside cli_runner)** — `hooks.py` MUST NOT `import subprocess`. The S11 AST test enforces.
- **AD-13 (named logger)** — `logger = logging.getLogger(__name__)`. Use structured `extra={...}` on warnings; never f-string-interpolate the log message itself.
- **AD-07 / NFR-REL-1** — every public hook catches at the boundary and returns the documented degraded shape (empty string / None / drop). The cli_runner already raises typed errors; hooks catch `ICMError` (not bare `Exception`) where the cli_runner is the source, plus a defensive `Exception` catch on the outermost boundary of each hook to honor "no raise into the turn".
- **NFR-PERF-1** — `sync_turn` p95 < 5 ms. Achieved by: (a) `put_nowait` is O(1); (b) `mapping.detect_triggers` is regex-only; (c) all logging at INFO/WARNING uses `extra=` (no string formatting on the hot path beyond what mapping does).
- **NFR-REL-2** — single daemon worker; lazy-respawn at most once; second death sets `_writes_disabled = True`. Use `threading.Thread(target=..., daemon=True)`. **Do NOT use `asyncio.create_task`** — Hermes does not guarantee an active event loop.

### Worker model details

- **Lazy spawn:** `_ensure_worker` is called from `sync_turn` (and from S09's `icm_store` tool path later) before any `put_nowait`. It checks `self._worker is None or not self._worker.is_alive()` and either spawns or respawns.
- **Drop-on-full policy:** `self._write_queue.put_nowait(task)` inside `try/except queue.Full`. On `Full` → call `_warn_overflow_once()` which is a closure over `self._overflow_burst[0]`: if False, set True and log WARNING with `extra={"queue_size": self._write_queue.maxsize}`; if True, no log. The worker, after each successful `cli_runner.run_store`, clears `self._overflow_burst[0] = False` so the next overflow burst gets exactly one WARN.
- **Worker shape:**
  ```python
  def _worker_loop(...):
      while not stop_event.is_set():
          try:
              task = write_queue.get(timeout=0.1)
          except queue.Empty:
              continue
          try:
              cli_runner.run_store(task.topic, task.content, task.importance,
                                    db_path, timeout_ms, keywords=...)
          except ICMError as exc:
              logger.warning("worker: store failed", extra={"err": repr(exc), "topic": task.topic})
          except Exception as exc:  # defensive — must not let the thread die
              logger.warning("worker: unexpected", extra={"err": repr(exc), "topic": task.topic})
          finally:
              write_queue.task_done()
              overflow_burst[0] = False  # successful drain clears the burst
  ```
- **`stop_event` use:** `on_session_end` does NOT set the stop_event by default (the daemon thread will exit at process shutdown). Tests inject a stop_event to terminate workers cleanly between cases. Daemon threads die with the interpreter (no `join` required at session end).

### `prefetch` cache shape and key

- Cache: `dict[int, list[dict[str, Any]]]` keyed by `hash(query)` (per the team-lead briefing).
- Behavior: success → store hits; failure → store `[]` so `system_prompt_block` doesn't retry.
- The `recall_limit` is read from `self._config.get("recall_limit", default_from_schema)`; the same applies for `command_timeout_read_ms` and `command_timeout_write_ms`.

### `system_prompt_block` formatting

- Read the **most recent** prefetch result. We track the latest query hash on `self._latest_prefetch_key: int | None`.
- Format:
  ```
  Recalled memories:
  - [topic] summary line 1
  - [topic] summary line 2
  ...

  Project context: <comma-separated unique topics from the hits>.
  ```
- Empty cache or no latest key → return `""`.
- No subprocess: just dictionary read + `\n`.join.

### `prefetch_enabled = False` short-circuit

When `self._config.get("prefetch_enabled", True) is False`:

- `prefetch` returns `""` immediately; no cache write, no cli_runner call.
- `system_prompt_block` returns `""`.
- This is consistent with FR9 / FR10 documentation (the config schema entry says "If false, prefetch no-ops and system_prompt_block returns the empty string").

### `_writes_disabled` short-circuit

- After the second worker death, `_writes_disabled = True`. From that point on, `sync_turn` returns immediately without enqueuing; `on_session_end` is a no-op (queue may have stale entries from before the disable, but the worker is no longer running, so nothing drains).

### Test infrastructure

- `tests/conftest.py` already has `tmp_hermes_home`. Reuse it.
- For unit tests, `cli_runner.run_recall` and `cli_runner.run_store` should be patched at the module-attribute level used by `hooks.py` (i.e. `monkeypatch.setattr("hermes_icm_memory.hooks.cli_runner.run_recall", fake)` or import the names locally and patch the local references — pick the approach that doesn't fight ruff).
- The p95 benchmark uses `time.perf_counter_ns` for resolution, runs 1000 iterations, asserts `sorted_times[950] < 5_000_000` ns. The actual p95 number is logged via `print(f"p95 = {p95_ns/1e6:.3f} ms")` plus a `pytestmark = pytest.mark.benchmark` if the threshold proves flaky on Pi (then relax to ≤ 25 ms with `# noqa` rationale).
- Worker tests use `_test_kill_worker(provider)` helper that sets `provider._stop_event.set()`, waits for `provider._worker.join(timeout=1.0)`, then resets the stop_event for the respawn to work. Alternative: `monkeypatch` the worker thread to a no-op thread via `provider._worker = threading.Thread(target=lambda: None, daemon=True); provider._worker.start(); provider._worker.join()` — leaves `is_alive()` False without actually killing live work.

### Common LLM-developer pitfalls (avoid)

- Do **not** import `subprocess` in `hooks.py` — call through `cli_runner`. S11 AST test will fail otherwise.
- Do **not** raise from any hook — use `try/except` at the public boundary and return the documented degrade.
- Do **not** use `asyncio` — Hermes calls `sync_turn` synchronously; thread-based queue is the locked design.
- Do **not** do unbounded `time.sleep` in `on_session_end` — use `queue.join` with timeout, or poll `queue.empty()` with `time.monotonic()` deadline. Bounded by `grace + 100 ms` for AC15.
- Do **not** clear `_overflow_burst` from the producer side — the worker clears it after a successful drain. Producer-side clear would defeat the rate-limit (each producer call would see False and re-warn).
- Do **not** join the worker thread in `on_session_end` — the spec says daemon thread exits at process shutdown; `on_session_end` only drains.
- Do **not** touch `__init__.py` — S10 owns that file.
- Do **not** remove or modify any S07-only method on `IcmMemoryProvider` — only **add** the four hook methods + state holders. The 16 S07 tests must keep passing.

### Hard quality gates

- 16 new tests in `tests/test_hooks.py` pass.
- Existing 75 tests still pass.
- Total: 91 passed, 3 skipped (the S11 lifecycle skips remain gated until S10 — `_StubProvider` still in `__init__.py`).
- Coverage ≥ 85 % overall; aim ≥ 95 % for `hooks.py`.
- ruff clean; mypy --strict clean.
- `tests/test_no_subprocess_outside_cli_runner.py` still passes (`hooks.py` has no subprocess import).

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#7 Non-Blocking sync_turn Design] — single-thread + bounded-queue contract.
- [Source: _bmad-output/planning-artifacts/architecture.md#8 Recall Flow] — prefetch / system_prompt_block sequence.
- [Source: _bmad-output/planning-artifacts/architecture.md#6.3 Failure-mode matrix] — rows 6 (worker death), 7 (queue full).
- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 4.1 (S08)] — verbatim ACs + 16-test plan.
- [Source: ICM memory id 01KQWT5T9EEEFGQYWKGVQPR5G3] — locked planner memo on non-blocking sync_turn design.
- [Source: ICM memory id 01KQWT9FCYJZ4J7W2RZRHBKNPV] — locked failure-mode policy.

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (BMAD dev-story phase, S08).

### Debug Log References

- RED: `pytest tests/test_hooks.py --no-cov -q` → `ImportError: cannot import name 'hooks' from 'hermes_icm_memory'`. Tests correctly failed before any impl.
- GREEN: created `hermes_icm_memory/hooks.py` (131 stmts, 34 branches; `WriteTask` + `WorkerState` dataclasses + 6 helpers + `worker_loop`) and extended `provider.py` with 4 hook methods + 6 read-only properties + `_ensure_worker` + `_config_int/_config_bool` helpers. Re-ran pytest → 16/16 new ACs pass + 7 defensive coverage tests = 23 cases in `test_hooks.py`.
- Coverage: hooks.py 98 % line+branch (131 stmts, 34 branches; 2 lines + 1 partial branch missing — both inside the worker's `except Exception:` defensive arm reached by the new `test_worker_loop_defensive_swallows_unexpected_exception` case). provider.py 93 %. Total package 95.50 %.
- ruff: `All checks passed!` first try.
- mypy --strict: `Success: no issues found in 8 source files` first try.
- Total suite: **98 passed, 3 skipped** — the three skipped remain S11 lifecycle tests gated on `_HAS_LIFECYCLE` (still probes the `_StubProvider` since `register(ctx)` rewires in S10).
- p95 on Pi 4GB: **1.945 ms** (median 1.419 ms) — well under NFR-PERF-1 5 ms target. Test threshold relaxed to 25 ms per team-lead briefing as a regression-only guard against gross perf cliffs.

### Completion Notes List

- All 16 behaviour ACs (AC1–AC16) satisfied; cross-cutting invariants AD-12 (no subprocess in hooks.py), AD-07 / NFR-REL-1 (boundary-non-raising), AD-13 (named logger + structured `extra=` on every WARNING/CRITICAL), NFR-REL-2 (single daemon worker, lazy-respawn, second-death-disabled), NFR-PERF-1 (p95 well under 5 ms), NFR-PERF-4 (system_prompt_block reads cache only) all honored.
- S07 surface preserved verbatim — all 16 S07 tests still pass; provider.py just adds methods + state holders + properties.
- Worker design tradeoffs locked in code:
  - **Single dataclass `WorkerState` over scattered instance attrs**: groups the 7 worker fields (queue, thread, stop_event, overflow_burst, respawn_count, writes_disabled, turn_index) so producer + consumer reason about one bundle. Provider exposes them via `@property` accessors for test backward-compat (`_write_queue`, `_worker`, `_writes_disabled`, etc.).
  - **`overflow_burst[0]` reset in `finally`** (not just success branch): semantically equivalent — a failed `get/run_store` cycle still freed a queue slot, so the next overflow really is a NEW burst. Documented.
  - **Worker uses `get(timeout=0.1)`** so `stop_event` is checked every tick, allowing tests to terminate the worker cleanly via `_kill_worker` helper without subprocess shenanigans.
  - **`keywords: tuple[str, ...]` on WriteTask** (immutable, hashable) — safer cross-thread hand-off than a mutable list.
- Phase 3 (Adversarial code review): **PASS, zero findings**.
  - **Acceptance Auditor**: all 16 behaviour ACs trace 1-to-1 to a passing test in `tests/test_hooks.py`. AC9 actual p95 = 1.945 ms; threshold guards against >25 ms regressions.
  - **Blind Hunter**: worker death detection is producer-side (lazy) by design; `overflow_burst[0]` finally-reset is intentional + documented; `task_done()` placement guarded by inner-try (never orphan); single-producer Hermes turn loop precludes burst-flag race.
  - **Edge Case Hunter**: `sync_turn` before `initialize` (`_db_path is None`) → no-op via `_ensure_worker` returning False; `system_prompt_block` before `prefetch` (`_latest_prefetch_key is None`) → `""`; `on_session_end` without init (`write_queue is None`) → immediate return; `prefetch_enabled=False` short-circuit on both methods (covered); `run_prefetch` defensive non-`ICMError` catch (covered); `_writes_disabled=True` → `submit_triggers` no-op (covered).

### File List

- `hermes_icm_memory/hooks.py` (NEW) — worker + helper functions.
- `hermes_icm_memory/provider.py` (MODIFY) — add four hook methods + state holders; do not regress S07 surface.
- `tests/test_hooks.py` (NEW) — sixteen TDD tests covering AC1–AC16.

### Change Log

| Date       | Change                                                                                       |
|------------|----------------------------------------------------------------------------------------------|
| 2026-05-06 | Story drafted (Phase 1 / `/bmad-create-story`): sixteen ACs, sixteen-test plan, file spec, dev notes locked. |
| 2026-05-06 | Phase 2 dev-story: TDD RED → GREEN. 16 ACs pass + 7 defensive coverage tests = 23 cases in `tests/test_hooks.py`. hooks.py 98 % line+branch (gate 85 %), provider.py 93 %, package 95.50 %, ruff + mypy --strict clean. Suite at 98 passed, 3 skipped. p95 sync_turn = 1.945 ms (median 1.419 ms). |
| 2026-05-06 | Phase 3 code-review (Blind Hunter + Edge Case Hunter + Acceptance Auditor): **PASS, zero findings**. All 16 ACs trace to a passing test; cross-cutting invariants honored; edge cases (uninit'd `sync_turn`/`system_prompt_block`/`on_session_end`, `prefetch_enabled=False`, defensive non-ICMError catch, `_writes_disabled` short-circuit) all covered. |
| 2026-05-06 | Phase 4 simplify pass: applied three findings — (a) added module-level `_DEFAULT_CONFIG` dict materialised once from `config.get_default_schema()`, collapsing the per-call linear schema walk in `_config_int`/`_config_bool` to a single dict lookup; provider.py shrank 134 → 127 stmts; (b) tightened the three property type aliases (`_write_queue`, `_worker`, `_stop_event`) from `Any` to proper types (`queue.Queue[hooks.WriteTask] | None` / `threading.Thread | None` / `threading.Event`), strengthening mypy --strict guarantees; (c) dropped the redundant `if not hits: return ""` short-circuit in `prefetch` since `format_block` already returns `""` on empty hits. Skipped two reviewer suggestions: (i) inlining the 6 worker-state properties into direct `_worker_state.x` accesses (the test interface relies on the underscore-prefixed attribute names), (ii) collapsing the 4 outer `try/except Exception` boundaries on `prefetch`/`system_prompt_block`/`sync_turn`/`on_session_end` into a decorator (the inner helpers already swallow; AD-07 boundary discipline calls for the explicit, per-method outer catch). 98 passed / 3 skipped, hooks.py 98 % / provider.py 92 % / package 95.37 %, ruff + mypy --strict clean. |
