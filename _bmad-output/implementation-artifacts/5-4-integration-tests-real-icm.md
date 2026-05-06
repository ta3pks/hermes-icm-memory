# Story 5.4: Integration tests against a real `icm` binary

Status: in-progress
Story ID: S14 · Epic: 5 (Hardening, invariants, integration) · Effort: M · Dependencies: S07 (provider), S08 (hooks + worker), S09 (tools), S10 (register wiring)

## Story

As a maintainer,
I want integration tests that exercise the real `icm` binary against a `tmp_path`-bound DB to verify cross-session recall, cross-tool sharing, and `sync_turn` stress behavior,
so that the unit-mocked claims are backed by end-to-end evidence (FR12, FR15, SM2, SM3).

## Acceptance Criteria

**AC1 — Cross-session recall round-trip via the plugin's tool surface**

- **Given** a fresh `IcmMemoryProvider` initialized against `tmp_path` with a real `icm` binary on PATH
- **When** the test stores a memory through `provider.handle_tool_call("icm_store", ...)` (which enqueues; the test then drains the queue via `provider.on_session_end()` + `Queue.join()`), then invokes `provider.handle_tool_call("icm_recall", ...)`
- **Then** the recall payload contains a hit whose summary matches the stored content. Tests use `--no-embeddings` (injected via a `cli_runner._run` wrapper fixture) to avoid embedding-model download in CI; the assertion is on keyword-search hits.

**AC2 — Cross-tool memory sharing (FR12)**

- **Given** the same `tmp_path`-bound DB the provider uses
- **When** the test invokes `subprocess.run(["icm", "--no-embeddings", "--db", <db>, "store", ...])` directly (simulating a Claude Code write), then invokes `provider.handle_tool_call("icm_recall", ...)`
- **Then** the plugin sees the externally-written memory in its recall hits.

**AC3 — `sync_turn` stress: bounded queue + FIFO + single WARNING + no exception (FR15)**

- **Given** a provider configured with `sync_write_queue_size = N` (small, e.g. 4) and the cli_runner `run_store` monkeypatched to block on a controlled `threading.Event` until the producer burst completes
- **When** the test fires `2 * N` `provider.sync_turn` calls in rapid succession, each producing exactly one trigger with unique content
- **Then**
  - (a) the worker processes accepted items in FIFO order (the recorded `processed` list is a strict prefix of the produced sequence),
  - (b) at least one item is dropped (accepted < `2 * N`),
  - (c) exactly one `WARNING` is logged for the overflow burst,
  - (d) no exception escapes any `sync_turn` / `on_session_end` call,
  - (e) once the gate opens and the queue is fully drained, the eventually-stored ICM DB contains exactly `accepted` entries (verified via plugin recall).

**AC4 — Module-level skip when `icm` is not on PATH**

- **Given** any of the three integration test files
- **When** the test process runs on a host without `icm` on PATH
- **Then** `pytestmark = pytest.mark.skipif(shutil.which("icm") is None, reason="icm not on PATH")` skips the whole module at collection time with a clear reason — no provider construction, no subprocess invocation. On the CI Pi (and on this dev Pi) `icm` IS on PATH, so the tests run green, not skipped.

**AC5 — `subprocess` stays out of the source tree**

- **Given** the `hermes_icm_memory/` package
- **When** S11's AST invariant test runs after this story lands
- **Then** the only file under `hermes_icm_memory/` that imports `subprocess` is still `cli_runner.py`. The S14 test files (`tests/integration/test_real_icm_cross_tool.py` in particular) are allowed to import `subprocess` because the S11 test is scoped to `hermes_icm_memory/` only.

**AC6 — Quality gates clean**

- `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` passes (integration tests run, not skip).
- `mypy --strict hermes_icm_memory tests` passes (full scope including the new integration files).
- `ruff check .` clean.

## Tasks / Subtasks

- [ ] Task 1: Create `tests/integration/__init__.py` (empty marker) so pytest picks the dir up under `tests/` collection (AC1–AC4)
- [ ] Task 2: Write `tests/integration/test_real_icm_recall.py` (TDD — tests first) (AC1, AC4)
  - [ ] Module-level `pytestmark` skip-if-no-icm
  - [ ] Module-scope `_no_embeddings_subprocess` fixture (or per-test) that monkeypatches `cli_runner._run` to inject `--no-embeddings` after the `icm` argv head
  - [ ] `test_store_then_recall_returns_hit` — store via tool path → drain → recall via tool path → assert hit
- [ ] Task 3: Write `tests/integration/test_real_icm_cross_tool.py` (AC2, AC4)
  - [ ] Module-level `pytestmark` skip-if-no-icm
  - [ ] Reuse the `--no-embeddings` injection fixture for the plugin's recall side
  - [ ] `test_external_write_visible_to_plugin` — `subprocess.run(["icm", "--no-embeddings", ...])` writes externally, then plugin recall sees it
- [ ] Task 4: Write `tests/integration/test_sync_turn_stress.py` (AC3, AC4)
  - [ ] Module-level `pytestmark` skip-if-no-icm
  - [ ] Reuse the `--no-embeddings` injection fixture
  - [ ] `test_overflow_fifo_warning_no_exception` — gate the worker via monkeypatched `cli_runner.run_store`; fire `2 * N` sync_turns; assert FIFO, dropped > 0, exactly one WARNING, no exception, DB count == accepted
- [ ] Task 5: Run the integration tests in isolation: `pytest tests/integration/ -v` (AC1–AC4)
- [ ] Task 6: Quality gates (AC6)
  - [ ] Full pytest with coverage gate
  - [ ] `mypy --strict hermes_icm_memory tests`
  - [ ] `ruff check .`
- [ ] Task 7: Commit (`docs(S14)` for story, `feat(S14)` for the four files)

## Dev Notes

**Architecture references:**

- `architecture.md` §4 (component map): `tests/integration/{test_real_icm_recall.py, test_real_icm_cross_tool.py, test_sync_turn_stress.py}` are the three files this story creates.
- `architecture.md` §13.2 (Integration tests — real `icm`): module-level skip on `shutil.which("icm") is None`; DB always under `tmp_path`; use `--no-embeddings` to dodge model-download flakiness in CI; assert on hybrid keyword-search hits.
- `architecture.md` §6.1 / §6.3 row 7 (Bounded queue full): single WARNING per overflow burst, drop-on-full — the stress test is the FR15 evidence.

**Locked decisions (manager prompt):**

- All integration tests use the module-level skip pattern; the entire file is collected-skipped on hosts without `icm`.
- DB lives under `tmp_path`; `provider.initialize("...", str(tmp_path))` resolves it to `<tmp_path>/icm/default.db`.
- `--no-embeddings` injection is scoped to tests via a `cli_runner._run` monkeypatch — production `cli_runner` is unchanged. (Direct `subprocess.run` in `test_real_icm_cross_tool.py` passes the flag explicitly in argv.)
- The worker is spawned lazily by `provider.sync_turn` (or by an explicit `provider._ensure_worker()` call); `icm_store` requires the queue to exist, so tests pre-spawn via a no-trigger `sync_turn` call.
- Test queue draining: `provider.on_session_end()` followed by `provider._write_queue.join()` to wait for in-flight tasks (the production `drain_with_grace` returns when the queue is empty, which happens BEFORE the worker finishes processing the last popped item; tests need the stronger sync).
- Stress test gating: monkeypatch `cli_runner.run_store` to wait on a `threading.Event` before processing. Capture order in a list. Open the gate after the producer burst completes.

**`subprocess` import is allowed in `tests/integration/test_real_icm_cross_tool.py`:** S11's AST invariant test is scoped to `hermes_icm_memory/` source files only; tests are exempt by design (the cross-tool scenario inherently requires simulating an external writer).

**Test plan (TDD; tests first; tests in this story ARE the implementation — they exercise existing code from earlier stories):**

1. `test_real_icm_recall.py::test_store_then_recall_returns_hit` — write via plugin → drain → recall via plugin → assert hit summary contains the stored marker.
2. `test_real_icm_cross_tool.py::test_external_write_visible_to_plugin` — write directly with `subprocess.run(["icm", "--no-embeddings", "--db", ..., "store", ...])` → recall via plugin → assert hit (FR12).
3. `test_sync_turn_stress.py::test_overflow_fifo_warning_no_exception` — gate-then-burst pattern; assert FIFO of accepted, exactly one WARNING, no exception, DB count == accepted (FR15).
4. Module-level skip logic at the top of each file: `pytestmark = pytest.mark.skipif(shutil.which("icm") is None, reason="icm not on PATH")`.

**Coverage delta expectation:** integration tests exercise `provider.handle_tool_call`, `cli_runner.run_store` / `run_recall`, the worker loop, and `drain_with_grace`. They lift overall package branch coverage marginally; the 85% gate stays green (already comfortably above on Sprint 1 close).

## Dev Agent Record

### Context Reference

- Manager prompt for S14 (this conversation, team-lead message)
- `_bmad-output/planning-artifacts/epics-and-stories.md` Story 5.4 (lines 800–838)
- `_bmad-output/planning-artifacts/architecture.md` §4, §6.3, §13.2
- `_bmad-output/planning-artifacts/prd.md` FR12, FR15, SM2, SM3

### Agent Model Used

claude-opus-4 (BMAD-aligned, Phase 1 of 4-phase chain)

### Debug Log References

n/a (Phase 1 — story creation only)

### Completion Notes List

(to be filled by Phase 2)

### File List

- `tests/integration/__init__.py` (NEW — empty marker)
- `tests/integration/test_real_icm_recall.py` (NEW)
- `tests/integration/test_real_icm_cross_tool.py` (NEW)
- `tests/integration/test_sync_turn_stress.py` (NEW)

### Change Log

- 2026-05-06 — S14 story drafted (Phase 1 of 4-phase chain).
