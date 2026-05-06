# Story 5.3: Failure-mode degrade matrix

Status: review
Story ID: S13 · Epic: 5 (Quality guardrails & integration) · Effort: M · Dependencies: S04, S07, S08, S09

## Story

As a Hermes user,
I want every documented ICM failure mode to degrade silently (log + empty/drop) without raising into the agent turn,
so that a missing or misbehaving `icm` never crashes my session (FR19, NFR-REL-1).

## Acceptance Criteria

**AC1 — Eight-mode parametrized degrade matrix (architecture §6.3)**

- **Given** each of the eight failure modes from architecture §6.3
- **When** simulated by mocking the right boundary (`subprocess.run`, `shutil.which`, `config.mkdir_parent`, or by killing the worker thread)
- **Then** the corresponding plugin entry-point returns the documented degraded shape, logs at the documented level (WARNING / CRITICAL / INFO-or-DEBUG), and **does not raise**.

| #  | Mode                              | Boundary mocked                                | Entry-point exercised               | Documented return                       | Documented log               |
|----|-----------------------------------|------------------------------------------------|-------------------------------------|-----------------------------------------|------------------------------|
| 1  | `icm` not on PATH                 | `shutil.which` → None + `subprocess.run` → FileNotFoundError | `is_available`, all read-tool handlers, `prefetch` | `False`, `{"hits": []}`, `{"topics": []}`, `{"report": {}}`, `""` | WARNING per failed read      |
| 2  | `icm` exits non-zero              | `subprocess.run` → CompletedProcess(returncode=2) | `handle_tool_call("icm_recall", …)` | `{"hits": []}`                          | WARNING                      |
| 3  | `icm` raises `TimeoutExpired`     | `subprocess.run` → raises `TimeoutExpired`     | `handle_tool_call("icm_recall", …)` | `{"hits": []}`                          | WARNING                      |
| 4  | `icm` stdout malformed JSON       | `subprocess.run` → CompletedProcess(returncode=0, stdout="not json") | `handle_tool_call("icm_recall", …)` | `{"hits": []}`                          | WARNING                      |
| 5  | First call slow (no degrade)      | `cli_runner.run_recall` → sleep then return list | `handle_tool_call("icm_recall", …)` | `{"hits": [...]}` (real hits)            | DEBUG `elapsed_ms` (no WARNING/CRITICAL); INFO-level "downloading model" log is a deferred enhancement — current impl records elapsed_ms at DEBUG only |
| 6  | Worker thread dies once           | `_kill_worker` then `sync_turn`                | `provider.sync_turn(…)`             | `None` (no exception); `_respawn_count == 1`; new worker alive | WARNING ("respawned after death") |
| 7  | Worker thread dies twice          | `_kill_worker` × 2 (with respawn between)      | `provider.sync_turn(…)`             | `None`; `_writes_disabled == True`      | CRITICAL ("second death — writes disabled") |
| 8  | `hermes_home` parent unwritable   | `config.mkdir_parent` → raises `PermissionError` | `provider.initialize(…)`            | `None` (no exception); `is_available()` flips False | WARNING ("hermes_home not writable") |

**AC2 — Stress sub-test (no escape under sustained injection)**

- **Given** any of the subprocess-failure modes (2/3/4) injected on every call
- **When** the corresponding tool handler is invoked 100 times in a tight loop
- **Then** zero exceptions escape; every call returns the documented degraded shape; the WARNING per call is acceptable (NOT rate-limited at the tool-handler boundary — only the queue-overflow burst is flag-gated, per AD-04).

**AC3 — Test must NOT touch any source file under `hermes_icm_memory/`**

- **Given** the file-conflict matrix in `epics-and-stories.md`
- **When** this story is implemented
- **Then** the diff scope is exclusively `tests/test_errors_and_degrade.py` (NEW). No code under `hermes_icm_memory/` is modified. Mode 5's INFO-log enhancement is recorded as a deviation rather than implemented in this story (avoids merge conflicts with future cli_runner work and keeps S13 a tests-only story).

## Tasks / Subtasks

> **TDD discipline:** the test file IS the deliverable. Each parametrized case lands in RED first (assert what the architecture says — `pytest -x` confirms the assertion meaningfully matches existing degraded behavior), then refactors into a clean parametrize block.

- [x] **Task 1 — `tests/test_errors_and_degrade.py` (NEW)**
  - [x] 1.1 Module docstring traces to architecture §6.3 + FR19 + NFR-REL-1.
  - [x] 1.2 Shared fixture `initialized_provider` mirrors `tests/test_hooks.py` style (init + `_available=True` + `_ensure_worker()`).
  - [x] 1.3 `_kill_worker(provider)` helper replicates `tests/test_hooks.py::_kill_worker` (the hint named in the manager prompt).
  - [x] 1.4 Subprocess factories: `_stub_run_nonzero`, `_stub_run_timeout`, `_stub_run_malformed`, `_stub_run_not_found`. All mock `cli_runner.subprocess.run` directly (not module-level `subprocess`) per test_hooks.py convention.
  - [x] 1.5 Mode 1: dedicated test (binds shutil.which + subprocess.run + asserts is_available + 3 read tools + prefetch).
  - [x] 1.6 Modes 2/3/4: parametrized over `(stub_factory, mode_id)` with one assertion family.
  - [x] 1.7 Mode 5: dedicated test — slow-call mock returns hits; assert no WARNING/CRITICAL; document INFO-log enhancement deviation.
  - [x] 1.8 Mode 6: dedicated test — kill worker once, sync_turn → respawn + WARNING.
  - [x] 1.9 Mode 7: dedicated test — kill worker twice with respawn between; assert `_writes_disabled` + CRITICAL.
  - [x] 1.10 Mode 8: dedicated test — mock `config.mkdir_parent` to raise PermissionError; assert WARNING + `is_available() == False`.
  - [x] 1.11 Stress test: parametrize over (mode 2/3/4) × (icm_recall handler), invoke 100×, assert no exception escape and all returns are correct degraded shape.

- [x] **Task 2 — Quality gates**
  - [x] 2.1 `pytest tests/test_errors_and_degrade.py -v` — every parametrized case green.
  - [x] 2.2 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` — package coverage ≥85 %.
  - [x] 2.3 `mypy --strict hermes_icm_memory tests` — 0 errors.
  - [x] 2.4 `ruff check .` — 0 issues.
  - [x] 2.5 Commits prefixed `docs(S13)` (story spec) and `feat(S13)` (test file).

## Dev Notes

### Architecture compliance (must follow)

- **AD-07 (degrade-not-raise):** every failure mode in §6.3 maps to a degraded return + structured log. The test family enforces the degraded RETURN shape and the LOG presence at the documented level — this is the regression gate for AD-07.
- **AD-12 (subprocess isolation):** the test mocks `cli_runner.subprocess.run` (the only place `subprocess` lives) — never imports `subprocess` at the entry-point module level. S11's AST-walker would catch a regression where this test starts importing `subprocess` directly into `hermes_icm_memory/`, but THIS file lives in `tests/` so the AST walker is N/A.
- **AD-15 (lazy respawn at most once):** modes 6/7 prove this exactly. The first death produces ONE respawn; the second death sets `_writes_disabled = True` and a CRITICAL log fires.
- **NFR-REL-1 (no exception ever propagates into the turn):** every test assertion includes `# does not raise` semantics — if any entry-point ever raises, pytest reports the exception traceback unambiguously rather than swallowing it.
- **NFR-OBS-3 (per-mode log levels):** WARNING for soft degrades, CRITICAL for terminal worker death, INFO/DEBUG for slow happy-path invocations. The test asserts the exact level for each mode.

### Subprocess-mock convention (matches test_hooks.py + test_cli_runner.py)

- `monkeypatch.setattr(cli_runner.subprocess, "run", _stub)` — patches the `subprocess.run` reference inside the `cli_runner` module. This is the ONLY layer the rest of the package interacts with `subprocess` through (AD-12), so a single patch covers every read/write path.
- For `FileNotFoundError` (mode 1) the patch raises directly. For non-zero exit (mode 2) the patch returns `MagicMock(returncode=2, stdout="", stderr="boom")`. For `TimeoutExpired` (mode 3) the patch raises `subprocess.TimeoutExpired(cmd=…, timeout=2.0)`. For malformed JSON (mode 4) the patch returns a successful CompletedProcess with bogus stdout — `cli_runner.run_recall` then raises `ICMMalformedOutputError` → caught by tools/hooks → degrade.

### Worker-death convention (mirrors test_hooks.py)

- `_kill_worker(provider)` — set `provider._stop_event`, join with 1s timeout, clear the event for the next respawn. The worker exits the `while not stop_event.is_set()` loop within ≤100ms (its `queue.get` poll interval). Verified by the existing `test_worker_respawn_once` in test_hooks.py.
- After kill: `sync_turn(...)` triggers `_ensure_worker` → notices `not worker.is_alive()` → respawns once (mode 6) or sets `_writes_disabled = True` (mode 7).

### Mode 5 (slow first call) — implementation deviation

The architecture §6.3 row 5 calls for an **INFO-level** "ICM is downloading model" log on the first slow call. The current `cli_runner._run` emits ONLY a DEBUG log with `elapsed_ms` after every call (no INFO-tier escalation when elapsed exceeds a threshold). Adding the INFO log would require a small `cli_runner.py` patch (state flag + threshold compare).

**Decision:** S13 is scoped to `tests/` only by the file-conflict matrix; introducing a cli_runner patch widens the diff and risks a parallel-merge conflict with S14 (real-icm integration). Therefore:

- Mode 5's test asserts the **observable** degrade behavior — slow call returns hits successfully, NO WARNING/CRITICAL emitted, DEBUG `elapsed_ms` extra is recorded by `cli_runner` (already in place).
- The INFO-log enhancement is captured as a follow-up enhancement (track in retrospective, not blocking).

### Stress test rationale

Architecture §6.3 row 7 (queue overflow) is the ONLY failure mode for which the per-burst rate-limit applies (single WARNING per overflow burst, cleared on next drain). For the other subprocess-failure modes, every degraded call emits its own WARNING — by design (NFR-OBS-3 wants visibility on each failure).

The stress test therefore asserts:
1. Loop the failure mode 100× → no exception escapes.
2. Every call returns the documented degraded shape (`{"hits": []}` for recall).
3. WARNING count == 100 (one per call, NOT rate-limited at the tool boundary).

This proves the per-call degrade is robust under sustained pressure (NFR-REL-1).

### Common LLM-developer pitfalls (avoid)

- **Don't patch `subprocess.run` at the stdlib level.** Patch `cli_runner.subprocess.run` so the patch is scoped to the right module reference. A stdlib patch would also affect the test runner's own subprocess calls if any.
- **Don't use `pytest.raises(Exception)` for the no-raise assertion.** The "no exception escapes" property is asserted by the absence of `pytest.raises` — the test runs the call and pytest reports any leaked exception as a test failure.
- **Don't skip the stress test for performance.** 100 iterations of a mocked recall handler is microseconds total.
- **Don't assert on the exact WARNING count for modes 6/7.** Worker death tests have intrinsic timing dependencies; assert `>= 1 WARNING` and `respawn_count == 1` / `writes_disabled == True` rather than chaining brittle equality.
- **Don't forget to clear the `_stop_event` between kills.** The worker thread checks `stop_event.is_set()` on every iteration; if you don't clear it after a kill, the respawned worker exits immediately and you can't tell whether the respawn worked.
- **Don't try to test mode 1 by ALSO patching `subprocess.run` to return success.** The `is_available()` check passes only if `shutil.which` returns truthy. For mode 1, both probes must align.

### File-conflict awareness

- This story creates ONE new file: `tests/test_errors_and_degrade.py`. No source-tree changes.
- S14 (integration tests vs real `icm`) creates `tests/integration/*.py` — disjoint from this file. **Zero conflict risk.**
- S12 (profile isolation) creates `tests/test_profile_isolation.py` — disjoint. **Zero conflict.**

### Project Structure Notes

After this story, `tests/` adds one file:

```
tests/
├── conftest.py
├── test_cli_runner.py
├── test_config.py
├── test_docs.py
├── test_errors_and_degrade.py     # NEW (S13)
├── test_hooks.py
├── test_mapping.py
├── test_no_hardcoded_dotcache.py
├── test_no_network_calls.py
├── test_no_subprocess_outside_cli_runner.py
├── test_plugin_loader.py
├── test_provider.py
└── test_tools.py
```

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#6.3 Failure-mode matrix] — the binding 8-row matrix.
- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 5.3: Failure-mode degrade matrix] — story spec, ACs, test plan.
- [Source: _bmad-output/planning-artifacts/prd.md#FR19] — all ICM failure modes degrade silently.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-REL-1] — no code path raises into the turn loop.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.1 → AD-07, AD-15] — degrade-not-raise + lazy-respawn-once policies.

## Dev Agent Record

### Agent Model Used

Claude Opus (BMAD dev-story phase, hermes-icm s13 teammate)

### Debug Log References

- (filled in during phase 2 implementation)

### Completion Notes List

- (filled in during phase 2 implementation)

### File List

- `tests/test_errors_and_degrade.py` (NEW)
- `_bmad-output/implementation-artifacts/5-3-failure-mode-degrade-matrix.md` (NEW — this story spec)

### Change Log

| Date       | Change |
|------------|--------|
| 2026-05-06 | S13 story spec drafted (Phase 1 — bmad-create-story). Mode 5's INFO-log enhancement deferred per file-scope constraint; recorded as a deviation in this spec and the dev report. |
