# Story 3.2: Wire `register(ctx)` to the real provider

Status: in-progress
Story ID: S10 · Epic: 3 (Memory provider lifecycle) · Effort: S · Dependencies: S07 (provider), S08 (hooks), S09 (tools)

## Story

As a Hermes runtime,
I want `hermes_icm_memory.__init__.register(ctx)` to construct a real `IcmMemoryProvider` (replacing the S01 `_StubProvider`) and register it with the Hermes context so that the four `plugin.yaml`-declared hook callbacks (`prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`) are actually bound on the registered object,
so that the plugin transitions from "stub registered" to "fully functional" — completing FR1's "registers a memory provider named `icm`" with the real provider rather than a placeholder.

## Acceptance Criteria

**AC1 — `register(ctx)` constructs a real `IcmMemoryProvider`**

- **Given** `register(ctx)` is invoked with a fake `ctx` exposing `register_memory_provider(provider)`
- **When** the call returns
- **Then** the captured argument satisfies `isinstance(arg, IcmMemoryProvider)` (not `_StubProvider`, not a generic mock).

**AC2 — All four `plugin.yaml` hook methods are bound on the registered provider**

- **Given** the provider captured from `register(ctx)`
- **When** `getattr(provider, hook_name)` is read for each of `prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`
- **Then** each lookup succeeds (no `AttributeError`) and the resolved attribute is `callable(...)`.

**AC3 — `register` is the only entry point that calls `ctx.register_memory_provider`; module import has no side effects**

- **Given** a fresh interpreter
- **When** `import hermes_icm_memory` runs (without invoking `register`)
- **Then** no `register_memory_provider` call has been emitted; the side-effect happens only inside the explicit `register(ctx)` call. Re-importing the package (cached module) likewise does not re-fire registration.

**AC4 — S01 baseline tests still pass (no regression)**

- **Given** the four pre-existing `test_plugin_loader.py` tests from S01 (`test_register_calls_register_memory_provider_once`, `test_registered_provider_name_is_icm`, `test_version_is_consistent`, `test_plugin_yaml_shape`)
- **When** rerun against the S10 wiring
- **Then** all four continue to pass — `provider.name == "icm"` because `IcmMemoryProvider` carries the same class attribute as the stub did.

**AC5 — S11 NFR-SEC-1 lifecycle tests light up**

- **Given** the three `@pytest.mark.skipif(not _HAS_LIFECYCLE)` tests in `tests/test_no_network_calls.py` (`test_is_available_no_socket`, `test_get_config_schema_no_socket`, `test_save_config_no_socket`)
- **When** S10 lands and the registered provider exposes `is_available`, `get_config_schema`, `save_config`
- **Then** the skip predicate flips and the three tests execute (and pass) — verifying zero plugin-originated network I/O during lifecycle calls.

## Tasks / Subtasks

- [ ] Task 1: Write three new tests in `tests/test_plugin_loader.py` (TDD — tests first, expected red) (AC1, AC2, AC3)
  - [ ] `test_register_constructs_real_provider` — capture argument; assert `isinstance(arg, IcmMemoryProvider)`
  - [ ] `test_provider_hook_methods_bound` — assert each of the four hook names resolves and is callable
  - [ ] `test_register_called_once_idempotent_module_import` — re-import module via `importlib.reload`; assert capturing ctx records zero side-effect calls (only the explicit `register(ctx)` triggers registration)
- [ ] Task 2: Run pytest; confirm the three new tests fail with the stub (TDD red)
- [ ] Task 3: Implement the wiring in `hermes_icm_memory/__init__.py` (AC1, AC2)
  - [ ] Drop `_StubProvider`, drop `from typing import Any`
  - [ ] Add `from .provider import IcmMemoryProvider`
  - [ ] Replace `register` body with the canonical 2-line shape
  - [ ] Update `__all__` to export `IcmMemoryProvider` alongside `__version__` and `register`
  - [ ] Update module docstring to reflect "real provider" status
- [ ] Task 4: Run full test suite; confirm (AC4, AC5)
  - [ ] All 4 S01 baseline tests still pass
  - [ ] All 3 new S10 tests pass
  - [ ] The 3 S11 lifecycle skips flip to pass (total now ~150 passed, 0 skipped)
- [ ] Task 5: Quality gates
  - [ ] `ruff check .` clean
  - [ ] `mypy --strict hermes_icm_memory tests` clean (full scope — both packages)
  - [ ] `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` passes
- [ ] Task 6: Commit (`docs(S10)` for story, `feat(S10)` for tests + wiring)

## Dev Notes

**Architecture references:**
- `architecture.md` §3 (component map): `__init__.py` is the only thing Hermes loads; constructs `IcmMemoryProvider`, calls `ctx.register_memory_provider(...)`, no other logic.
- `architecture.md` §5 (`register(ctx)` lifecycle row): triggered on plugin load; data-flow is `IcmMemoryProvider() → ctx.register_memory_provider(provider)`; no side effects beyond construction + the registration call.
- `architecture.md` §11.8 (frozen surface): provider class name `IcmMemoryProvider`, `name = "icm"`.

**Locked decisions (manager prompt):**
- Python 3.11+, pytest ≥85% branch coverage, ruff + `mypy --strict` (full scope: `hermes_icm_memory tests`).
- AD-12: `__init__.py` MUST NOT import `subprocess`. The canonical shape only imports `IcmMemoryProvider` and `__version__` — trivially satisfied.
- The four hook methods (`prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`) are already bound on `IcmMemoryProvider` by S08 — S10 is purely a wiring change in `__init__.py`. No provider edits.
- `plugin.yaml` already declares the four hooks (S01) — no manifest edits.

**Canonical implementation shape (manager prompt, verbatim):**

```python
from __future__ import annotations

from ._version import __version__
from .provider import IcmMemoryProvider

__all__ = ["IcmMemoryProvider", "__version__", "register"]


def register(ctx) -> None:
    """Hermes plugin entry point. Construct the provider and hand it to ctx."""
    ctx.register_memory_provider(IcmMemoryProvider())
```

Keep it minimal. Don't add `try/except` — `IcmMemoryProvider.__init__` is documented as "no I/O, no subprocess, no network" (provider.py docstring) and any raise here legitimately surfaces as a plugin-load failure, not a turn-time error.

**Test plan (TDD; tests first):**

1. `test_register_constructs_real_provider` — uses `MagicMock` ctx, calls `register`, asserts `isinstance(ctx.register_memory_provider.call_args.args[0], IcmMemoryProvider)`. Imports `IcmMemoryProvider` from `hermes_icm_memory` (re-exported via `__all__`).
2. `test_provider_hook_methods_bound` — same capture pattern, then iterates `("prefetch", "system_prompt_block", "sync_turn", "on_session_end")` asserting `callable(getattr(provider, hook))`.
3. `test_register_called_once_idempotent_module_import` — uses `importlib.reload(hermes_icm_memory)`, then constructs a fresh `MagicMock` ctx, asserts `ctx.register_memory_provider.call_count == 0` *before* invoking `register(ctx)` (proves module-level reload has no side effects beyond defining the function). Then calls `register(ctx)` and asserts call count is exactly 1.

**Coverage delta expectation:** `__init__.py` is currently 9 stmts at 100%. After S10 it becomes ~6-7 stmts (one fewer class, one fewer import) still at 100% — the `register` body is exercised by every test that imports the module + calls `register`.

**S11 cross-story verification (manager prompt):**

The three `@pytest.mark.skipif(not _HAS_LIFECYCLE)` tests in `tests/test_no_network_calls.py` evaluate `_HAS_LIFECYCLE` at module-import time by calling `register(_CapturingCtx())` and probing `is_available`, `get_config_schema`, `save_config`. After S10 wires `IcmMemoryProvider`, those three attributes exist as bound methods (S07 added them), so `_HAS_LIFECYCLE` flips to `True` and the skips become passes — automatically. Phase 2 must verify total test count jumps from 147→~150 (3 skips light up + 3 new S10 tests = +3 passes net, since the 3 skips counted as `skipped` not `passed`).

Expected post-S10 pytest summary:
- Before: `147 passed, 3 skipped`
- After: `150 passed, 0 skipped` (147 baseline + 3 new S10 tests; minus 3 skips that are now in the passed total → net 147 baseline-pass + 3 new + 3 ex-skip = 153 passed total). Actual count to be verified empirically in Phase 2.

## Dev Agent Record

### Context Reference

- Manager prompt for S10 (this conversation, team-lead message)
- `_bmad-output/planning-artifacts/epics-and-stories.md` Story 3.2 (lines 526–557)
- `_bmad-output/planning-artifacts/architecture.md` §3, §5, §11.8

### Agent Model Used

claude-opus-4 (BMAD-aligned, Phase 1 of 4-phase chain)

### Debug Log References

n/a (Phase 1 — story creation only)

### Completion Notes List

(to be filled by Phase 2)

### File List

- `hermes_icm_memory/__init__.py` (MODIFY — replace stub wiring with real provider)
- `tests/test_plugin_loader.py` (MODIFY — add 3 new tests; keep existing 4)

### Change Log

- 2026-05-06 — S10 story drafted (Phase 1 of 4-phase chain).
