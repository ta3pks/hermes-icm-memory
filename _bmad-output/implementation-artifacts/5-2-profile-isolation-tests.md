# Story 5.2: Profile isolation tests

Status: draft
Story ID: S12 · Epic: 5 (Quality guardrails & integration) · Effort: S · Dependencies: S05 (config / `resolve_db_path`), S07 (`IcmMemoryProvider`)

## Story

As a Hermes user with multiple profiles,
I want a test suite that proves two profiles get two distinct DB paths and never read each other's data,
so that I can trust my "work" and "personal" memories don't bleed (FR2, NFR-SEC-2, SM5).

## Acceptance Criteria

**AC1 — Two distinct `hermes_home` values yield two distinct DB paths (FR2)**

- **Given** two providers initialised with `hermes_home="/tmp/hh-A"` and `hermes_home="/tmp/hh-B"` (per-test `tmp_path`-scoped paths in practice)
- **When** their `_db_path` attributes are compared
- **Then** the two paths are non-equal *and* each is contained inside its respective `hermes_home` directory.

**AC2 — Two distinct `profile` values under one shared `hermes_home` yield two distinct DB paths (FR2 / SM5)**

- **Given** two providers initialised with `profile="work"` and `profile="personal"` against the same `hermes_home`
- **When** their `_db_path` attributes are compared
- **Then** the paths are exactly `<hh>/icm/work.db` and `<hh>/icm/personal.db` and are non-equal.

**AC3 — Cross-profile recall does not leak written data (NFR-SEC-2)**

- **Given** a working `icm` binary on `PATH` (test is skipped via `pytest.mark.skipif(shutil.which("icm") is None, ...)` otherwise) and two providers `A` (profile `"alpha"`) and `B` (profile `"beta"`) under the same `hermes_home`
- **When** a memory is written through provider A's DB and then `recall` is run against provider B's DB for the same query
- **Then** B observes zero hits for that memory.

**AC4 — Resolved DB paths are always inside `hermes_home` (FR2 / NFR-SEC-2)**

- **Given** a provider initialised with any `hermes_home` (and any non-`None` profile name)
- **When** `_db_path` is examined
- **Then** `db_path.is_relative_to(Path(hermes_home).resolve())` is `True`.

## Tasks / Subtasks

> **TDD discipline:** tests first. Each test must be observed RED on a real provider that doesn't yet honour the invariant — but in this case, S05 + S07 already implement the path-resolution + initialize contract. The test suite locks the contract against future regressions; RED-confirm is achieved by temporarily breaking `resolve_db_path` (e.g. dropping the `profile` term) before reverting.

- [ ] **Task 1 — `tests/test_profile_isolation.py` (NEW; AC1–AC4)**
  - [ ] 1.1 Module docstring naming FR2, NFR-SEC-2, SM5.
  - [ ] 1.2 `test_two_hermes_homes_two_dbs` — instantiate two providers with two `tmp_path`-scoped `hermes_home`s; assert `_db_path` values distinct AND each contained inside its own `hermes_home`.
  - [ ] 1.3 `test_two_profiles_one_hermes_home_two_dbs` — instantiate two providers under one `hermes_home` with `profile="work"` and `profile="personal"`; assert `_db_path == hh / "icm" / "work.db"` and `hh / "icm" / "personal.db"` and they are distinct.
  - [ ] 1.4 `test_no_cross_profile_recall_leak` — gated by `@pytest.mark.skipif(shutil.which("icm") is None)`. Two providers A (profile `"alpha"`) and B (profile `"beta"`) under one `hermes_home`. Write a unique-token memory through `cli_runner.run_store(... db_path=A._db_path ...)`, then `cli_runner.run_recall(query=token, db_path=B._db_path, ...)`. Assert zero hits. Use `--no-embeddings` via env var or by writing without embedding model trigger if the CLI supports it (manager note: integration test 3 needs `--no-embeddings` to avoid model download in CI).
  - [ ] 1.5 `test_db_path_inside_hermes_home_only` — assert `provider._db_path.is_relative_to(Path(hermes_home).resolve())` for at least two profile names (`"default"` and an explicit name).

- [ ] **Task 2 — Quality gates**
  - [ ] 2.1 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` passes.
  - [ ] 2.2 `ruff check .` passes.
  - [ ] 2.3 `mypy --strict hermes_icm_memory tests` passes.
  - [ ] 2.4 Commits prefixed `docs(S12)/feat(S12)/review(S12)/simplify(S12)`.

## File Spec (authoritative)

### `tests/test_profile_isolation.py` (NEW)

Single test module, **four tests**, all using the existing `tmp_path` pytest fixture (no `tmp_hermes_home` because each test instantiates its own multiple homes/profiles).

**Imports:**

```python
import shutil
from pathlib import Path
import pytest
from hermes_icm_memory.provider import IcmMemoryProvider
```

**Tests 1, 2, 4** are pure-Python contract checks (no subprocess) — they depend only on `provider.initialize` populating `_db_path` correctly.

**Test 3** (integration) imports `cli_runner` lazily inside the test body to avoid penalising the unit-tests-only run when `icm` is absent. Skipif on `shutil.which("icm") is None`. Uses `--no-embeddings` (or equivalent) so no embedding model download occurs in CI.

## Dev Notes

### Architecture compliance (must follow)

- **FR2 (path injection):** every DB path derives from `kwargs['hermes_home']`. This story's tests *prove* it. AC1 + AC4 are the active enforcers.
- **NFR-SEC-2 (profile isolation):** AC2 + AC3 are the enforcers. AC3 catches the *runtime* leak (subprocess-level) and AC2 catches the *static* leak (resolved paths overlap).
- **SM5 (success metric — profile isolation):** the entire test family is the SM5 regression gate.
- **AD-05 / AD-06:** `<hermes_home>/icm/<profile>.db`; `mkdir(parents=True, exist_ok=True)`. AC2 pins the exact path layout.
- **AD-12 (subprocess isolation):** test 3 uses `cli_runner` (the only allowed subprocess module). Tests 1/2/4 do not import `subprocess` at all.
- **NFR-SEC-1:** test 3 hits the network only via the embedding model in ICM proper; we pass `--no-embeddings` so even that channel is closed in CI.

### Why use `cli_runner.run_store` / `cli_runner.run_recall` rather than spawning `icm` directly?

`cli_runner` is the canonical way every other test invokes `icm`; its argv layout is the contract; reusing it keeps the test resilient to argv refactors and enforces AD-12 by construction. The `--no-embeddings` requirement is fulfilled by the underlying `icm` CLI flag — `cli_runner` does not currently surface it, so test 3 either (a) sets an env var ICM honours, or (b) constructs argv inline at the test level. Approach (b) keeps the test self-contained and avoids polluting the production module surface for a test-only switch. **Decision: build the argv at the test level** using a small `subprocess.run` shim contained inside the test module (this is a *test* file, not package source, so AD-12's allow-list does not gate it).

Alternative considered: extend `cli_runner.run_store` with a `no_embeddings: bool = False` flag. **Rejected** — pollutes the public CLI surface for a test-only concern.

### Test plan (TDD; tests-first; mirrors AC1–AC4 1:1)

| #   | Test name                                       | Assertion                                                                                            | Gate                                  |
|-----|-------------------------------------------------|------------------------------------------------------------------------------------------------------|---------------------------------------|
| 1   | `test_two_hermes_homes_two_dbs`                 | Two `hermes_home`s ⇒ two distinct `_db_path`s, each inside its own `hermes_home`.                    | always runs                           |
| 2   | `test_two_profiles_one_hermes_home_two_dbs`     | One `hermes_home` + two profiles ⇒ `<hh>/icm/work.db` and `<hh>/icm/personal.db`, non-equal.          | always runs                           |
| 3   | `test_no_cross_profile_recall_leak`             | Write to A; recall from B; expect zero hits.                                                          | skipif `shutil.which("icm") is None`  |
| 4   | `test_db_path_inside_hermes_home_only`          | `provider._db_path.is_relative_to(Path(hermes_home).resolve())` is True for default + explicit name.  | always runs                           |

### Hard quality gates

1. `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → 0 failures; ≥ 3 active tests on this branch (test 3 may skip in CI without `icm`).
2. `ruff check .` → 0 issues.
3. `mypy --strict hermes_icm_memory tests` → 0 errors.
4. `git status` clean.
5. All work on branch `s12`.

### Common LLM-developer pitfalls (avoid)

- **Don't forget `Path.resolve()`.** `tmp_path` from pytest may resolve through symlinks (`/private/tmp` on macOS, `/var/folders/...`). `is_relative_to` requires both operands at the same realpath layer; `resolve_db_path` already calls `.resolve()`, so the assertion side must too.
- **Don't assume `profile=None` ≡ `"default"` at the test layer.** The provider stores `_init_args[2]` as the literal `None` while `_db_path` resolves through `"default"`. AC2/AC4 keep the two views consistent.
- **Don't write integration test 3 in a way that downloads the embedding model.** ICM downloads on first use without `--no-embeddings`. Either (a) construct argv with `--no-embeddings` directly, or (b) skip if the env signals embeddings cannot be disabled.
- **Don't reuse `tmp_hermes_home`** — the conftest fixture creates a single `hermes_home`, but tests 1 and 3 need *two*. Use `tmp_path` directly and create child directories.
- **Don't import `subprocess` at module top-level in test 3.** Lazy-import inside the test body to keep the no-`icm` path zero-cost.

### File-conflict awareness

- This story creates ONE new file: `tests/test_profile_isolation.py`. No edits to any existing file. **Zero conflict risk** with S13 / S14 (sibling tests-only stories).
- No source-tree changes.

### Project Structure Notes

After this story, `tests/` adds one file:

```
tests/
├── __init__.py                                    # pre-existing
├── conftest.py                                    # pre-existing
├── test_profile_isolation.py                      # NEW (S12)
└── ... (other tests untouched)
```

### References

- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 5.2: Profile isolation tests] — story spec, ACs, test plan, files-touched.
- [Source: _bmad-output/planning-artifacts/architecture.md#9. Profile Isolation] — `<hermes_home>/icm/<profile>.db` layout, first-run behaviour, leakage tests.
- [Source: _bmad-output/planning-artifacts/prd.md#FR2] — path injection.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-SEC-2] — profile isolation.
- [Source: _bmad-output/planning-artifacts/prd.md#SM5] — profile isolation success metric.

## Dev Agent Record

### Agent Model Used

Claude Opus (BMAD dev-story phase, hermes-icm s12 teammate)

### Debug Log References

- (to be filled in during Phase 2)

### Completion Notes List

- (to be filled in during Phase 2 / Phase 3 / Phase 4)

### File List

- `tests/test_profile_isolation.py` (NEW) — AC1–AC4, four tests.
- `_bmad-output/implementation-artifacts/5-2-profile-isolation-tests.md` (NEW) — this story spec.

### Change Log

| Date       | Change |
|------------|--------|
| 2026-05-06 | S12 story spec drafted (Phase 1 — bmad-create-story). |
