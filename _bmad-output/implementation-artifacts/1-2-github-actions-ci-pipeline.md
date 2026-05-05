# Story 1.2: GitHub Actions CI pipeline

Status: draft
Story ID: S02 · Epic: 1 (Plugin foundation) · Effort: S · Dependencies: S01

## Story

As a maintainer,
I want CI that lints, type-checks, and tests on every push and PR across Python 3.11 + 3.12,
so that quality regressions are caught before merge and Sprint-2 deliverables (NFR-REL-3 / NFR-REL-4) are continuously enforced.

## Acceptance Criteria

**AC1 — Lint + type-check + test on Python 3.11 and 3.12 / `ubuntu-latest`**

- **Given** a PR is opened
- **When** the workflow runs
- **Then** it executes `ruff check .`, `mypy --strict hermes_icm_memory`, and `pytest` with the 85 % coverage gate, on both Python 3.11 and Python 3.12, on `ubuntu-latest`.

**AC2 — Failure blocks merge (status check semantics)**

- **Given** any of those steps fails
- **When** GitHub evaluates the PR
- **Then** the workflow status is failure (the required-status-check enforcement is configured at the repo level, out of code scope — but the workflow MUST surface failures via non-zero step exits, not `continue-on-error: true` for the gate steps).

**AC3 — Triggers**

- **Given** the workflow file
- **When** read
- **Then** `on:` includes both `push` and `pull_request` (no branch filters: any branch push triggers; any PR triggers).

**AC4 — Install icm before installing the package**

- **Given** the workflow file
- **When** read
- **Then** there is a step named exactly `Install icm` that installs `icm` from upstream and surfaces `icm --version`. This step appears **before** the package-install step (named `Install package`). The install step is `continue-on-error: true` so a transient ICM build failure does not block the unit tests / lint / mypy from running. Manager directive (binding): install via `cargo install --git https://github.com/rtk-ai/icm.git icm`; integration tests under S14 self-skip if `icm` is unavailable on PATH.

**AC5 — Coverage gate is invoked**

- **Given** the workflow file
- **When** read
- **Then** the test step's command either contains `--cov-fail-under=85` explicitly **or** invokes `pytest` plain (the gate is wired in `pyproject.toml`'s `[tool.pytest.ini_options].addopts` from S01). Both forms are equivalent; the test asserts at least one of them holds.

**AC6 — YAML is valid and parses cleanly**

- **Given** `.github/workflows/ci.yml`
- **When** parsed via `yaml.safe_load`
- **Then** parsing succeeds (no `YAMLError`); the resulting structure has the keys `name`, `on`, `jobs`; `jobs` contains exactly one job named `test` with a `strategy.matrix.python-version` list of `["3.11", "3.12"]` and `runs-on: ubuntu-latest`.

## Tasks / Subtasks

> **TDD discipline (mandatory):** every code task is preceded by writing the failing test for it. Run `pytest tests/test_ci_workflow.py -q --no-cov` after writing tests but before writing impl, and confirm all 3 FAIL. Only then write `.github/workflows/ci.yml`.

- [ ] **Task 1 — Write the 3 failing tests (AC1, AC3, AC4, AC5, AC6)**
  - [ ] 1.1 Create `tests/test_ci_workflow.py` containing the 3 tests from §Test Plan (verbatim names). Use `yaml.safe_load` on the workflow file; locate the file via `pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"`.
  - [ ] 1.2 Confirm `pytest tests/test_ci_workflow.py -q --no-cov` reports 3 collected, 3 failed (file missing). Capture in Dev Agent Record.

- [ ] **Task 2 — Implement `.github/workflows/ci.yml` (AC1–AC6)**
  - [ ] 2.1 Create the workflow per §File Spec.
  - [ ] 2.2 Re-run `pytest tests/test_ci_workflow.py -q --no-cov`. All three pass.

- [ ] **Task 3 — Quality gates (all six ACs)**
  - [ ] 3.1 `ruff check .` → 0 issues (no source change here, but a regression check).
  - [ ] 3.2 `mypy --strict hermes_icm_memory tests` → 0 errors (the new test file is type-checked too).
  - [ ] 3.3 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → all green; coverage stays ≥ 85 %.
  - [ ] 3.4 Commit on branch `s02`. Conventional message: `feat(S02): GitHub Actions CI pipeline (3.11 + 3.12, ruff + mypy + pytest)`.

## File Spec (authoritative — copy-paste boilerplate intent, not literal)

### `.github/workflows/ci.yml` (NEW)

```yaml
name: ci

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip

      - name: Set up Rust toolchain
        uses: dtolnay/rust-toolchain@stable

      - name: Cache cargo build
        uses: Swatinem/rust-cache@v2

      - name: Install icm
        continue-on-error: true
        run: |
          cargo install --git https://github.com/rtk-ai/icm.git icm
          icm --version

      - name: Install package
        run: |
          python -m pip install -U pip
          pip install -e ".[dev]"

      - name: Lint (ruff)
        run: ruff check .

      - name: Type check (mypy --strict)
        run: mypy --strict hermes_icm_memory

      - name: Test (pytest + coverage gate)
        run: pytest --cov-fail-under=85
```

Notes on the spec:

- `cache: pip` on `setup-python@v5` is the modern caching mechanism (no separate `actions/cache` step needed for pip).
- `Swatinem/rust-cache@v2` caches `~/.cargo/registry`, `~/.cargo/git`, and `target/` keyed on the workflow run, drastically reducing the cold ~5–10 min `cargo install` to ~10–30 s on subsequent runs.
- `continue-on-error: true` is on `Install icm` only — every gate step (Lint / Type / Test) MUST fail the job on non-zero exit. The integration tests under S14 use `pytest.mark.skipif(shutil.which("icm") is None, ...)` to self-skip if the cargo build failed transiently; unit tests + lint + mypy still gate.
- `fail-fast: false` so a 3.11 failure doesn't prematurely cancel the 3.12 job (and vice versa) — easier debugging.
- `pytest --cov-fail-under=85` is explicit (AC5 acceptable form #1). The gate is also set in `pyproject.toml`, so the explicit flag is redundant-but-explicit; this keeps the workflow self-documenting.

### `tests/test_ci_workflow.py` (NEW)

3 tests, names per the test plan below. Each test loads the workflow YAML and asserts on its parsed structure. No subprocess — pure YAML parsing. mypy `--strict` clean (use `dict[str, Any]` annotations; cast where needed).

## Dev Notes

### Architecture compliance (must follow)

- **Architecture §12.4 (CI shape):** the planner gave a literal `ci.yml` shape with `<icm-install-url>` as a placeholder. Manager directive resolves the placeholder to `cargo install --git https://github.com/rtk-ai/icm.git icm`. Add caching (Swatinem/rust-cache + setup-python's pip cache) which were not in the planner's sketch — performance improvement, no semantic change.
- **PRD NFR-REL-3 / NFR-REL-4:** coverage ≥ 85 % and ruff + mypy clean are CI-enforced. The test step's flag (`--cov-fail-under=85`) makes this self-evident in the workflow even though pyproject.toml also enforces it.
- **PRD CI matrix locked:** Python 3.11 + 3.12 on `ubuntu-latest` only (no Windows / macOS in v1).
- **AD-13 (logging namespace):** N/A — no Python source changes here.
- **No new package dependencies:** `pyyaml` is already in `[project.optional-dependencies].dev` from S01; the test file uses it directly.

### TDD execution log expected (mirror S01 / S04)

- **RED phase:** write the 3 tests first → run `pytest tests/test_ci_workflow.py -q --no-cov` → 3 failures (the workflow file does not yet exist; `FileNotFoundError` from `Path.read_text()` propagates as test errors).
- **GREEN phase:** create `.github/workflows/ci.yml` → re-run pytest → 3/3 pass.
- **No refactor phase needed** if the GREEN code is already clean — verify ruff + mypy gates.

### Test plan (TDD; tests-first; mirrors AC1, AC3, AC4, AC5, AC6 1:1)

| # | Test name                                                | AC ref      | What it asserts |
|---|----------------------------------------------------------|-------------|-----------------|
| 1 | `test_workflow_yaml_shape`                               | AC1, AC3, AC6 | YAML parses; `name == "ci"`; `on` contains both `push` and `pull_request`; `jobs.test.runs-on == "ubuntu-latest"`; `jobs.test.strategy.matrix.python-version == ["3.11", "3.12"]`; the steps' `run`/`name` fields (joined as one searchable string) include `ruff check`, `mypy`, and `pytest`. |
| 2 | `test_workflow_installs_icm`                             | AC4         | A step with `name == "Install icm"` exists; its index in the steps list is **less than** the index of the step named `Install package`. |
| 3 | `test_workflow_runs_pytest_with_coverage_gate`           | AC5         | The pytest step's `run` field contains `--cov-fail-under=85` **or** the project's `pyproject.toml` `[tool.pytest.ini_options].addopts` contains `--cov-fail-under=85` (either form satisfies the gate; both are acceptable per the planner). |

### Hard quality gates (must all pass before story is "done")

1. `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → 0 failures, coverage ≥ 85 %. (Coverage stays unchanged from S04 baseline since this story adds zero source lines under `hermes_icm_memory/`.)
2. `ruff check .` → 0 issues.
3. `mypy --strict hermes_icm_memory tests` → 0 errors. (The new test file participates in the strict mypy pass.)
4. The new files (`.github/workflows/ci.yml`, `tests/test_ci_workflow.py`) plus the story doc are committed on branch `s02`.
5. `git log --oneline` shows `feat(S02): ...` as the latest commit.

### Common LLM-developer pitfalls (avoid)

- **`on: [push, pull_request]` vs `on: { push: {}, pull_request: {} }`.** Both are valid GitHub Actions syntax. The expanded form is easier to extend later (e.g. branch filters); use the expanded form. The test asserts membership in the `on` mapping/list — both shapes pass the assertion (use `"push" in on_field` semantics).
- **YAML's `on` key parses as Python `True`** in PyYAML's default loader because `on` is a YAML 1.1 boolean alias. Use `yaml.safe_load` and check both `True` and `"on"` keys when reading the parsed `on:` field — or quote `"on":` in the YAML source. Quoting is uglier but avoids the boolean-key footgun. The cleanest fix: probe both keys defensively in the test (`workflow.get("on") or workflow.get(True)`). The workflow file itself can stay un-quoted because GitHub Actions parses correctly.
- **Don't omit `cache: pip` on setup-python.** Cold pip install is slow; the cache shaves ~30 s/run.
- **Don't forget `fail-fast: false`.** Default `fail-fast: true` cancels the 3.12 job the instant 3.11 fails (or vice versa), which obscures the actual failure on the second Python version.
- **Don't make `Install icm` a hard gate.** The manager directive explicitly says `continue-on-error: true` so unit tests + lint + mypy still run if the cargo build hiccups. Integration tests (S14) self-skip when `icm` is missing.
- **Don't add `--no-cov` to the pytest step.** The whole point is that CI enforces the coverage gate. The S01 `addopts` in `pyproject.toml` activates coverage automatically; the explicit `--cov-fail-under=85` in the workflow is for self-documentation.
- **Don't commit a `.github/workflows/ci.yml` that GitHub itself can't parse.** While the test asserts `yaml.safe_load` succeeds, that's a structural check. If you have any doubt, paste the file into https://rhysd.github.io/actionlint/ or run `actionlint` locally.

### Project Structure Notes

After this story the tree gains exactly one workflow file and one test file:

```
.github/
└── workflows/
    └── ci.yml                # NEW

tests/
├── ...                       # unchanged
└── test_ci_workflow.py       # NEW — 3 YAML-parse tests
```

No conflict with any other story. S02 only depends on S01 (pyproject.toml exists; addopts contain the coverage gate). It is parallel-safe with S03 (README/CONTRIBUTING), S11 (invariant tests), S12/S13/S14 (test-only stories).

### References

- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 1.2: GitHub Actions CI pipeline] — story spec, ACs, the 3-test plan, files-touched.
- [Source: _bmad-output/planning-artifacts/architecture.md#12.4 GitHub Actions CI (`ci.yml` shape)] — workflow shape; planner's `<icm-install-url>` placeholder resolved by manager directive to `cargo install --git`.
- [Source: _bmad-output/planning-artifacts/prd.md] — NFR-REL-3 (coverage ≥ 85 %), NFR-REL-4 (ruff + mypy clean), CI matrix lock (3.11 + 3.12 on ubuntu-latest).
- [Source: pyproject.toml] — `[tool.pytest.ini_options].addopts = "-q --cov=hermes_icm_memory --cov-branch --cov-fail-under=85"` (S01 baseline).

## Dev Agent Record

### Agent Model Used

Claude Opus (BMAD dev-story phase, S02 lane).

### Debug Log References

(populated in Phase 2)

### Completion Notes List

(populated in Phase 2)

### File List

(populated in Phase 2)

### Change Log

| Date       | Change                                                                                                  |
|------------|---------------------------------------------------------------------------------------------------------|
| 2026-05-06 | S02 story spec drafted from epics-and-stories.md §Story 1.2 + manager directive (cargo install + caching + continue-on-error). Status: draft → ready-for-dev. |
