# Story 3.1: IcmMemoryProvider class

Status: done
Story ID: S07 · Epic: 3 (Memory provider lifecycle) · Effort: L · Dependencies: S04 (cli_runner, errors), S05 (config), S01 (register stub being upgraded later by S10)

## Story

As a Hermes runtime,
I want a `MemoryProvider` subclass that implements every required Hermes-side lifecycle method (`name`, `is_available`, `initialize`, `get_config_schema`, `save_config`, `get_tool_schemas`, `handle_tool_call`),
so that `hermes memory setup icm` runs end-to-end and the provider is wired correctly under FR1, FR3, FR4, FR7, AD-12, AD-13, AD-18.

## Acceptance Criteria

**AC1 — `name` is the literal `"icm"`**

- **Given** an `IcmMemoryProvider()` instance
- **When** `provider.name` is read
- **Then** the value is the literal string `"icm"` (matches plugin manifest + frozen public-API surface §11.8).

**AC2 — `is_available()` returns `True` when `icm` is on PATH**

- **Given** `shutil.which("icm")` returns a truthy string
- **When** `is_available()` is called
- **Then** it returns `True`.

**AC3 — `is_available()` returns `False` when `icm` is missing**

- **Given** `shutil.which("icm")` returns `None`
- **When** `is_available()` is called
- **Then** it returns `False`.

**AC4 — `is_available()` caches the first result**

- **Given** the provider has called `is_available()` once
- **When** it is called again in the same process
- **Then** `shutil.which` is **not** invoked a second time (verified by patching `shutil.which` and counting calls).

**AC5 — `is_available()` performs no network I/O (NFR-SEC-1)**

- **Given** `socket.socket` is patched to raise on construction
- **When** `is_available()` is called
- **Then** the call does not raise.

**AC6 — `initialize` resolves the per-profile DB path**

- **Given** `provider.initialize(session_id="s1", hermes_home="/tmp/hh", profile="work")`
- **When** examined afterward
- **Then** `provider._db_path` equals `config.resolve_db_path("/tmp/hh", "work")` and `provider._session_id == "s1"`.

**AC7 — `initialize` creates the parent directory (mkdir_parent)**

- **Given** a fresh `tmp_path` as `hermes_home`
- **When** `initialize(session_id, hermes_home=tmp_path, profile="default")` is called
- **Then** `<hermes_home>/icm/` exists (and `<hermes_home>/icm/default.db` does **not** — ICM auto-creates the SQLite file on first write; the plugin never runs `icm init`).

**AC8 — `initialize` is idempotent on the same args (FR4, NFR-REL-5)**

- **Given** an initialized provider
- **When** `initialize` is called a second time with the same `(session_id, hermes_home, profile)`
- **Then** the second call is a no-op: it does not invoke `Path.mkdir` again (verified via a counting mock around `mkdir_parent` or `Path.mkdir`).

**AC9 — `initialize` against an unwritable `hermes_home` self-disables instead of raising**

- **Given** a `hermes_home` whose `<hermes_home>/icm/` cannot be created (read-only parent → `OSError` from `mkdir`)
- **When** `initialize` is called
- **Then** it does **not** raise; a WARNING is logged via `logging.getLogger("hermes_icm_memory.provider")`; subsequent calls to `is_available()` return `False` (the provider self-disables — failure-mode matrix §6.3 row 8).

**AC10 — `get_config_schema()` returns `config.get_default_schema()` verbatim**

- **Given** an `IcmMemoryProvider()`
- **When** `provider.get_config_schema()` is called
- **Then** it returns a list deep-equal to `config.get_default_schema()` (a fresh defensive copy each call — caller mutation cannot poison the next call).

**AC11 — `save_config` persists valid values and returns `None`**

- **Given** `provider.save_config({"recall_limit": 7, "default_importance": "high"}, hermes_home=tmp_path)` with valid values
- **When** called
- **Then** it returns `None`; the file `<hermes_home>/icm/config.json` exists and contains the normalized values; `provider._config` reflects the merged values.

**AC12 — `save_config` rejects invalid values with an error dict (FR7)**

- **Given** `provider.save_config({"recall_limit": -1}, hermes_home=tmp_path)`
- **When** called with a value that fails `config.validate`
- **Then** it returns a dict with an `"error"` key and a string value naming the offending key; never raises; no JSON sidecar file is written; `provider._config` is unchanged.

**AC13 — `handle_tool_call` returns the `tool unavailable` placeholder JSON**

- **Given** a fresh `IcmMemoryProvider()` (S09 has not yet wired tools)
- **When** `provider.handle_tool_call("icm_recall", {"query": "x"})` (or any other name) is called
- **Then** it returns the literal `json.dumps({"error": "tool unavailable"})`. No subprocess is spawned (S07 has zero `subprocess` import — enforced by S11 AST test).

**AC14 — `get_tool_schemas()` returns an empty list for now**

- **Given** an `IcmMemoryProvider()`
- **When** `get_tool_schemas()` is called
- **Then** it returns `[]`. (S09 will replace this stub with the four real tool schemas; the docstring explicitly notes the stub status.)

**AC15 — Provider does not import `subprocess` (AD-12)**

- **Given** `hermes_icm_memory/provider.py`
- **When** parsed by the S11 AST invariant test (`tests/test_no_subprocess_outside_cli_runner.py`)
- **Then** the test still passes (no `import subprocess`, no `from subprocess import …`).

**AC16 — Lifecycle invariants light up the previously-skipped S11 tests**

- **Given** S11's `tests/test_no_network_calls.py` skips three lifecycle tests via `@pytest.mark.skipif(not _HAS_LIFECYCLE, …)`
- **When** S07 lands `is_available`, `get_config_schema`, `save_config` on the registered provider
- **Then** the three previously-skipped tests (`test_is_available_no_socket`, `test_get_config_schema_no_socket`, `test_save_config_no_socket`) automatically execute and pass — pytest collection moves from `… passed, 3 skipped` to `… passed, 0 skipped` without editing the S11 file.

## Tasks / Subtasks

- [x] **Task 1 — Story spec (Phase 1 / `/bmad-create-story`)**
  - Capture sixteen ACs above, map each to a test in the test plan, and freeze the file spec.
- [ ] **Task 2 — Phase 2 / `/bmad-dev-story` (TDD)**
  - RED: write `tests/test_provider.py` with thirteen S07-spec tests (AC1–AC14 above).
  - GREEN: implement `hermes_icm_memory/provider.py` with the seven public methods + private state.
  - Confirm S11's three lifecycle tests light up automatically.
- [ ] **Task 3 — Phase 3 / `/bmad-code-review`**
  - Adversarial pass (Blind Hunter + Edge Case Hunter + Acceptance Auditor).
- [ ] **Task 4 — Phase 4 / `/simplify`**
  - Reuse / quality / efficiency review on the new code.

## File Spec

### `hermes_icm_memory/provider.py` (NEW)

Public surface:

```python
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)  # AD-13 — module-level logger

_TOOL_UNAVAILABLE_JSON: str = json.dumps({"error": "tool unavailable"})


class IcmMemoryProvider:
    name: str = "icm"

    def __init__(self) -> None: ...
    def is_available(self) -> bool: ...
    def initialize(
        self,
        session_id: str,
        hermes_home: str | os.PathLike[str],
        profile: str | None = None,
        **kwargs: Any,
    ) -> None: ...
    def get_config_schema(self) -> list[dict[str, Any]]: ...
    def save_config(
        self,
        values: dict[str, Any],
        hermes_home: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any] | None: ...
    def get_tool_schemas(self) -> list[dict[str, Any]]: ...
    def handle_tool_call(self, name: str, args: dict[str, Any]) -> str: ...
```

State holders set in `__init__`:

- `self._db_path: Path | None = None`
- `self._available: bool | None = None`  (cache for `is_available`)
- `self._config: dict[str, Any] = {}`
- `self._session_id: str | None = None`
- `self._initialized: bool = False`
- `self._init_args: tuple[str, str, str | None] | None = None`  (for idempotency check)

### `tests/test_provider.py` (NEW)

Thirteen TDD cases (one per behaviour AC1–AC14, except AC15/AC16 which are validated by the existing S11 invariants):

1. `test_name_is_icm` — `IcmMemoryProvider().name == "icm"` (AC1).
2. `test_is_available_true_when_icm_on_path` — patch `shutil.which` → `"/usr/local/bin/icm"`; assert `True` (AC2).
3. `test_is_available_false_when_missing` — patch `shutil.which` → `None`; assert `False` (AC3).
4. `test_is_available_caches_result` — patch returns same value; call twice; assert `shutil.which` called exactly once (AC4).
5. `test_is_available_no_socket` — patch `socket.socket` to raise; `is_available()` does not raise (AC5; mirror of S11 test).
6. `test_initialize_resolves_db_path` — `_db_path == resolve_db_path(hermes_home, "work")`; `_session_id == "s1"` (AC6).
7. `test_initialize_creates_parent_dir` — `<hermes_home>/icm/` exists post-init; the `.db` file does not (AC7).
8. `test_initialize_idempotent` — patch `Path.mkdir`; init twice with same args; assert `mkdir` call count is exactly one (AC8).
9. `test_initialize_with_unwritable_hermes_home_self_disables` — patch `mkdir_parent` to raise `OSError`; init does not raise; `caplog` captures a WARNING; `is_available()` returns `False` (AC9).
10. `test_get_config_schema_matches_defaults` — equal to `config.get_default_schema()`; second call returns a separate object (defensive copy) (AC10).
11. `test_save_config_accepts_valid` — returns `None`; `<hermes_home>/icm/config.json` is valid JSON deep-equal to the normalized values; `provider._config` reflects them (AC11).
12. `test_save_config_rejects_invalid_returns_error_dict` — returns `{"error": …}`; never raises; no sidecar file created (AC12).
13. `test_handle_tool_call_unknown_tool_returns_error_json` — return value `== json.dumps({"error": "tool unavailable"})` for any tool name (AC13).

Plus one extra sanity case — AC14 piggy-backs on the same suite:

14. `test_get_tool_schemas_is_empty_list` — `provider.get_tool_schemas() == []` (AC14).

## Dev Notes

### Architecture compliance (must follow)

- **AD-12 (no subprocess outside cli_runner)** — `provider.py` MUST NOT `import subprocess` (or `from subprocess import …`). Enforced by `tests/test_no_subprocess_outside_cli_runner.py`.
- **AD-13 (named logger)** — `logger = logging.getLogger(__name__)`; never `logging.getLogger()` (root) and never `print()`.
- **AD-07 (degrade, never raise into a turn) / NFR-REL-1** — `is_available`, `initialize`, `get_config_schema`, `save_config`, `get_tool_schemas`, `handle_tool_call` all catch broadly at their public boundary and return the documented degraded shape (False / None / [] / `{"error": …}` / `json.dumps({"error": …})`). They never propagate exceptions to the Hermes turn loop.
- **AD-18 (validation never raises)** — `save_config` delegates to `config.validate` (already AD-18-compliant) and additionally wraps the disk-write in `try/except OSError` so a read-only filesystem returns `{"error": "could not persist config: …"}` rather than crashing the setup.
- **AD-05 / AD-06 (db path + idempotent mkdir)** — `initialize` calls `config.resolve_db_path(hermes_home, profile)` and `config.mkdir_parent(self._db_path)`. The plugin **never** invokes `icm init` (SQLite auto-creates).
- **NFR-SEC-1 (no network I/O during lifecycle)** — `is_available` uses `shutil.which` only; nothing in the module opens a socket.

### `is_available` caching

- Initialize `self._available: bool | None = None` in `__init__`.
- On call: if `self._available is not None`, return it; else compute `bool(shutil.which("icm"))`, cache, return.
- The cache is process-scoped — Hermes `register(ctx)` constructs one provider per process.
- `initialize` may **flip the cache to `False`** when the filesystem is unwritable (AC9). Once flipped to `False` by self-disable, it stays `False` for the rest of the session (no recovery — the operator has to fix `hermes_home` and restart Hermes).

### `initialize` idempotency

- Guard via `self._initialized` plus `self._init_args == (session_id, str(hermes_home), profile)`. Same args + already initialized → return early. Different args → re-resolve and re-mkdir (cheap; idempotent itself).
- Wrap the `mkdir_parent` call in `try/except OSError`. On failure: WARN log, set `self._available = False`, set `self._initialized = True` so the broken state is sticky, and return without raising.

### `save_config` semantics

- Signature: `save_config(values, hermes_home=None)`. `hermes_home=None` is supported because the S11 invariant test calls `provider.save_config({})` without a hermes_home — in that case validation runs but no JSON sidecar is written and `_config` still updates with the (empty) normalized dict.
- Validation: `ok, result = config.validate(values)`; on `ok=False`, return `result` (already shaped as `{"error": …}`). On `ok=True`, merge into `self._config`.
- Disk persistence (when `hermes_home` is provided): write `<hermes_home>/icm/config.json` via `Path.write_text(json.dumps(self._config, sort_keys=True, indent=2))`. Wrap in `try/except OSError` → return `{"error": f"could not persist config: {exc}"}` instead of raising.
- The architecture says "writes to a tiny JSON sidecar at `<hermes_home>/icm/config.json`" — implementation choice within the AC. Sort keys + indent for stable diffs.

### `handle_tool_call` placeholder

- S07 returns the literal `_TOOL_UNAVAILABLE_JSON` for every tool name. S09 will replace the body with a dispatch table to `tools.icm_recall / icm_store / icm_topics / icm_health`. The `args` parameter is intentionally unused at this stage; document with `_ = args` or in the docstring (no `# noqa` needed — ruff does not flag unused args by default for class methods unless `ARG002` is enabled, which it isn't).

### Test fixtures

- Add a `tmp_hermes_home` fixture to `tests/conftest.py` — `tmp_path / "hermes_home"` (parent created).
- Use `monkeypatch.setattr(shutil, "which", lambda _: …)` in `test_provider.py` (import `shutil` locally in the test file; the provider does `shutil.which("icm")` at call time, so patching the `shutil` module is sufficient).
- Use `caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider")` for AC9.

### S11 forward-compat — light up the three skipped lifecycle tests

The following tests in `tests/test_no_network_calls.py` are gated by `@pytest.mark.skipif(not _HAS_LIFECYCLE, …)`:

- `test_is_available_no_socket`
- `test_get_config_schema_no_socket`
- `test_save_config_no_socket`

Once `_StubProvider` has `is_available`, `get_config_schema`, `save_config` callable, the gate flips to `True` and the three tests run. **S07 does not yet replace `_StubProvider` with `IcmMemoryProvider` in `__init__.py`** — that's S10's job. So during S07 the three tests **stay skipped** (the stub still has only `name = "icm"`).

The team-lead briefing said "the 3 S11-skipped lifecycle tests should AUTOMATICALLY light up green when you run pytest." That's only true if S07 also wires the new provider into `register(ctx)`. Per the story DAG (S10 depends on S07 + S08 + S09), S07 alone does not light them up — S10 will. To make the lifecycle invariants exercise the **new** provider during S07 without prematurely doing S10's work, `tests/test_provider.py` includes its own `test_is_available_no_socket` case (test 5) that constructs the provider directly. The S11 trio will light up later when S10 lands.

If the team-lead expectation is wrong, the status report flags it; if it's right, the discrepancy here is documented for the reviewer.

### Common LLM-developer pitfalls (avoid)

- Do **not** import `subprocess` for an "availability ping" — `shutil.which` is the contract (AC2/AC3, NFR-SEC-1).
- Do **not** call `icm init` — SQLite auto-creates on first write; AD-05/AD-06 explicit decision.
- Do **not** use `logging.getLogger()` (root); use `logging.getLogger(__name__)` (AD-13).
- Do **not** raise on invalid config — return `{"error": …}` (AD-18, FR7).
- Do **not** flip `self._available` back to `True` after a self-disable — sticky `False` per failure-mode §6.3 row 8.
- Do **not** swallow exceptions silently — every degrade branch logs at WARNING with `logger.warning(..., extra={...})`, never f-string interpolation.

### Hard quality gates

- 14 new tests in `tests/test_provider.py` pass.
- Three S11 lifecycle tests **may stay skipped** during S07 (they light up in S10 when `register(ctx)` is rewired). Status report explicitly addresses this.
- pytest total: ≥ 70 passed (baseline 59 + 14 new — give-or-take fixture additions). 0 unintended skips.
- Coverage ≥ 85 % overall; aim ≥ 95 % for `provider.py`.
- ruff clean; mypy --strict clean.
- `tests/test_no_subprocess_outside_cli_runner.py` still passes (provider.py has no subprocess import).

### References

- [Source: _bmad-output/planning-artifacts/architecture.md#5.1 Lifecycle methods] — method signatures + behaviour matrix.
- [Source: _bmad-output/planning-artifacts/architecture.md#6.3 Failure-mode matrix] — row 8 (`hermes_home` not writable).
- [Source: _bmad-output/planning-artifacts/architecture.md#11.8 Public API surface] — frozen surface (class name, plugin name).
- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 3.1] — verbatim ACs + 13-test plan.
- [Source: _bmad-output/planning-artifacts/prd.md#FR1, FR3, FR4, FR7] — install/availability/idempotent-init/non-raising-validate.

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (BMAD dev-story phase, S07).

### Debug Log References

- RED phase: `pytest tests/test_provider.py --no-cov -q` → `ModuleNotFoundError: No module named 'hermes_icm_memory.provider'`. Tests correctly failed before any impl.
- GREEN phase: created `hermes_icm_memory/provider.py` (67 stmts, 8 branches; 6 public methods + `__init__` state holders + module logger + `_TOOL_UNAVAILABLE_JSON` Final constant). Re-ran pytest → 14/14 new cases pass.
- Coverage: `provider.py` **100 %** line+branch (67 stmts, 8 branches). Total package 96.43 %.
- ruff: `All checks passed!` after one auto-fix (import order in `tests/test_provider.py`) and one manual line-length wrap.
- mypy --strict: `Success: no issues found in 19 source files` after rewriting two `provider_mod.config.mkdir_parent` patches to use `config.mkdir_parent` directly (mypy strict rejects re-export through module namespace).
- Total suite: 75 passed, 3 skipped — the three skipped are S11's `test_is_available_no_socket` / `test_get_config_schema_no_socket` / `test_save_config_no_socket`. **Not S07's job** — they remain gated on `_HAS_LIFECYCLE`, which probes the **registered** provider; `register(ctx)` still constructs S01's `_StubProvider`. They will light up when S10 swaps `register` to construct `IcmMemoryProvider`. The story spec called this out pre-emptively; the team-lead briefing's expectation ("the 3 should AUTOMATICALLY light up") is incorrect for S07 alone.

### Completion Notes List

- All 14 behaviour ACs (AC1–AC14) satisfied; AC15 (no-subprocess invariant) verified by S11's existing AST test; AC16 (S11 lifecycle skips lighting up) deferred to S10 — documented above.
- Coverage well above the 85 % gate (provider.py = 100 % line+branch).
- Strict TDD followed: ModuleNotFoundError RED → impl → 14 GREEN, no refactor needed.
- AD-12 honored: `provider.py` does not `import subprocess`. S11's `tests/test_no_subprocess_outside_cli_runner.py` still passes.
- AD-13 honored: `logger = logging.getLogger(__name__)`, structured `extra={...}` dicts on every WARNING.
- AD-07 / NFR-REL-1 honored: every public method catches at the boundary; `is_available` wraps the (theoretically total) `shutil.which` in `try/except Exception` with a `pragma: no cover` defensive branch; `initialize` catches `OSError` only (the documented failure mode); `save_config` catches `OSError` on the disk write only (validation is already non-raising).
- Self-disable is sticky: once `_available = False` is set by `initialize` on `OSError`, neither the cache check in `is_available` nor a successful re-init resets it (per failure-mode matrix §6.3 row 8).
- Idempotency key: `(session_id, str(hermes_home), profile)` tuple. Unmixed Path-vs-str input forms are stable; mixed forms (e.g. `~/foo` once + expanded form once) would not collide — acceptable edge case.
- `save_config` writes the cumulative `_config` dict (sort_keys + indent) so the sidecar is stable across multiple `save_config` calls.
- The `_ = (name, args)` line in `handle_tool_call` is intentional documentation — preserves the S09-stable signature without `# noqa`.
- Phase 3 (Adversarial code review): **PASS, zero findings**.
  - **Acceptance Auditor**: all 14 behaviour ACs trace 1-to-1 to a passing test in `tests/test_provider.py`. AC15 traced to `tests/test_no_subprocess_outside_cli_runner.py`. AC16 deferred to S10 (documented).
  - **Blind Hunter**: cache-flip ordering safe (re-init with different args after self-disable does not retry: `_initialized=True`, `_init_args` matches the failed key → idempotent no-op; if args differ, mkdir might succeed but `_available` stays sticky-False — matches §6.3 row 8). Logging discipline correct (`extra=` not f-string). `Final[str]` constant for the tool-unavailable JSON computes once at import. `get_config_schema` returns a fresh deep copy each call (delegates to `config.get_default_schema`).
  - **Edge Case Hunter**: `save_config({})` with no `hermes_home` → validation succeeds, `_config.update({})` is no-op, returns None (covered by `test_save_config_without_hermes_home_skips_disk_write`). `save_config` with valid values + unwritable hermes_home → returns `{"error": "could not persist config: …"}` (covered by `test_save_config_returns_error_dict_on_oserror`); validation already updated `_config` before the write attempt — surfaced in the test. `handle_tool_call("", {})` returns the same error JSON (no name-validation needed at this stage). `initialize` with `profile=""` would create a `default.db` path but key the idempotency cache as `""` not `"default"`; documented edge but unrealistic at the Hermes call site.

### File List

- `hermes_icm_memory/provider.py` (NEW) — `IcmMemoryProvider` class.
- `tests/test_provider.py` (NEW) — fourteen TDD tests covering AC1–AC14.
- `tests/conftest.py` (MODIFY) — add `tmp_hermes_home` fixture.

### Change Log

| Date       | Change                                                                                       |
|------------|----------------------------------------------------------------------------------------------|
| 2026-05-06 | Story drafted (Phase 1 / `/bmad-create-story`): sixteen ACs, fourteen-test plan, file spec, dev notes locked. |
| 2026-05-06 | Phase 2 dev-story: TDD RED → GREEN. 14 cases pass + 2 extra coverage cases (no-hermes_home + sidecar-write OSError) → 16 cases total in `test_provider.py`. provider.py 100 % line+branch (gate 85 %), package 96.43 %, ruff + mypy --strict clean. Suite at 75 passed, 3 skipped (S11 lifecycle skips will light up in S10, not S07). |
| 2026-05-06 | Phase 3 code-review (Blind Hunter + Edge Case Hunter + Acceptance Auditor): **PASS, zero findings**. All 14 behaviour ACs trace to a passing test; cache-flip ordering safe; logging discipline correct; `_TOOL_UNAVAILABLE_JSON` is a `Final[str]` computed once at import. Edge cases (`save_config({})`, `save_config` + OSError on write, `handle_tool_call("")`, `initialize` with `profile=""`) all covered or explicitly documented as out-of-scope. |
| 2026-05-06 | Phase 4 simplify pass: applied three findings — (a) dropped `_initialized: bool` flag (derivable from `_init_args is not None`; idempotency guard now keys directly on `_init_args == args_key`); (b) trimmed module docstring from 38 lines to 11 (kept the AD-12 / AD-13 / AD-07 invariants; removed the AD-05/06/18 + S08/S09/S10 roadmap recap that duplicated per-method docs and the story file); (c) deleted the `# (AC8) (AC10) (AC11) S07 stub` story-tracking comments from production code (story file owns traceability) plus the `# `result` is already shaped …` narrate-what comment. Replaced `_ = (name, args)` line in `handle_tool_call` with method-level `# noqa: ARG002`. provider.py shrank 67 → 63 stmts; coverage stays 100 % line+branch; suite still 75 passed / 3 skipped; ruff + mypy --strict clean. Skipped two reviewer suggestions: stringly-typed sidecar error → constant (single-use, not worth a `Final`), and `_init_args` → NamedTuple (3 fields, comment is sufficient). Reuse-review optional finding (extract `_write_config_sidecar` to `config.py`) deferred to S08 if the helper actually gets a second caller. |
