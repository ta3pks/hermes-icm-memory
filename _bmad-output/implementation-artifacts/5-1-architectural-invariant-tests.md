# Story 5.1: Architectural invariant tests (subprocess, dot-cache, network)

Status: review
Story ID: S11 · Epic: 5 (Quality guardrails & integration) · Effort: S · Dependencies: S01

## Story

As a maintainer,
I want CI to fail if anyone (a) imports `subprocess` outside `cli_runner.py`, (b) hardcodes `~/.hermes`, or (c) opens a socket during plugin lifecycle methods,
so that NFR-MAINT-2 (subprocess isolation), FR2 (path injection), and NFR-SEC-1 (zero plugin network I/O) cannot regress silently.

## Acceptance Criteria

**AC1 — Subprocess isolation (NFR-MAINT-2 / AD-12)**

- **Given** any source file under `hermes_icm_memory/`
- **When** the test scans every `.py` file using the stdlib `ast` module (NOT regex), parsing each `Import` and `ImportFrom` node
- **Then** the only file allowed to declare `import subprocess` or `from subprocess import ...` is `hermes_icm_memory/cli_runner.py`. The test asserts the offending list is empty.

**AC2 — No hardcoded `~/.hermes` literal (FR2)**

- **Given** any source file under `hermes_icm_memory/`
- **When** the test reads each `.py` file as text (UTF-8) and scans for the literal string `"~/.hermes"`
- **Then** no occurrence is found. Test files are excluded from this scan (the test itself must reference the literal in order to assert against it).

**AC3 — Provider lifecycle methods make zero socket calls (NFR-SEC-1)**

- **Given** the registered provider's lifecycle methods `is_available()`, `get_config_schema()`, and `save_config(...)`
- **When** invoked under a `socket.socket` patch that raises `RuntimeError` on construction
- **Then** none of the three methods raises — proving no socket is created during the plugin lifecycle entry points (NFR-SEC-1 invariant).

**AC4 — Forward-compat skip when lifecycle methods are absent**

- **Given** the S11 branch is parallel to S07 (the story that lands `IcmMemoryProvider` with the three lifecycle methods)
- **When** the registered provider does not yet implement `is_available` / `get_config_schema` / `save_config`
- **Then** the three socket-patch tests are explicitly skipped via `pytest.mark.skipif`, with a skip reason that names S07 as the unblocker. After S07 merges, the skip predicate flips to `False` automatically and the tests run.

## Tasks / Subtasks

> **TDD discipline:** these stories ARE tests. The tests are simultaneously the impl and the spec. Run each test once with the invariant intentionally violated (introduce a temporary `import subprocess` somewhere, or a stray `"~/.hermes"`) to confirm RED before reverting; then confirm GREEN.

- [x] **Task 1 — `tests/test_no_subprocess_outside_cli_runner.py` (AC1)**
  - [x] 1.1 Walk `hermes_icm_memory/` for every `*.py` file (use `pathlib.Path.rglob`).
  - [x] 1.2 For each file, parse with `ast.parse(path.read_text(encoding="utf-8"))`.
  - [x] 1.3 Walk the AST and collect any `ast.Import` whose alias name is `subprocess` OR any `ast.ImportFrom` whose module is `subprocess`.
  - [x] 1.4 Assert the offending list is empty *unless* the file is `cli_runner.py`.
  - [x] 1.5 Negative-control sub-test: helper `_imports_subprocess(source)` asserted True for crafted offending sources and False for benign code.

- [x] **Task 2 — `tests/test_no_hardcoded_dotcache.py` (AC2)**
  - [x] 2.1 Walk `hermes_icm_memory/` for every `*.py` file.
  - [x] 2.2 Read each file as text with `encoding="utf-8"`.
  - [x] 2.3 Assert `"~/.hermes"` not in file content; collect violations with helpful failure message.
  - [x] 2.4 Negative-control sub-test: synthetic source string contains literal → caught; benign string passes.

- [x] **Task 3 — `tests/test_no_network_calls.py` (AC3 + AC4)**
  - [x] 3.1 `_CapturingCtx` + `_register_and_capture()` helper.
  - [x] 3.2 Module-level `_HAS_LIFECYCLE` predicate computed once at import; skipif gate references it.
  - [x] 3.3 `test_register_returns_provider` — always runs.
  - [x] 3.4 `test_lifecycle_predicate_smoke` — always runs.
  - [x] 3.5 `test_is_available_no_socket` — skipif until S07.
  - [x] 3.6 `test_get_config_schema_no_socket` — skipif until S07.
  - [x] 3.7 `test_save_config_no_socket` — skipif until S07.

- [x] **Task 4 — Quality gates**
  - [x] 4.1 `pytest` → 10 passed, 3 skipped (S11: 4 active + 3 skipif). Coverage 100 %.
  - [x] 4.2 `ruff check .` → 0 issues.
  - [x] 4.3 `mypy --strict hermes_icm_memory tests` → 0 errors.
  - [x] 4.4 Committed with `feat(S11)` prefix.

## File Spec (authoritative)

### `tests/test_no_subprocess_outside_cli_runner.py` (NEW)

Walks every `.py` file under `hermes_icm_memory/`, parses AST, collects offenders. The only file allowed to import `subprocess` is `cli_runner.py` (which does not exist on this branch — S04 introduces it). On this branch, the test passes trivially because no file imports `subprocess`.

### `tests/test_no_hardcoded_dotcache.py` (NEW)

Reads every `.py` file under `hermes_icm_memory/` as UTF-8 text, asserts `"~/.hermes"` not in content. Excludes test files from the scan (tests must reference the literal in order to enforce against it).

### `tests/test_no_network_calls.py` (NEW)

Five tests:
1. `test_register_returns_provider` — sanity (always runs).
2. `test_lifecycle_predicate_smoke` — documents skip-gate behaviour (always runs).
3. `test_is_available_no_socket` — skipif when `_HAS_LIFECYCLE` is False.
4. `test_get_config_schema_no_socket` — same skip.
5. `test_save_config_no_socket` — same skip.

The skip predicate flips automatically when S07 lands `IcmMemoryProvider` with `is_available` / `get_config_schema` / `save_config` defined. No follow-up edit to this file is required from S07 — the skipif disappears by predicate evaluation.

## Dev Notes

### Architecture compliance (must follow)

- **AD-12 (subprocess isolation):** the AST walker is the *teeth* of AD-12. Catches both `import subprocess` and `from subprocess import run` in a single pass. Regex would miss aliasing (`import subprocess as sp`) which AST handles natively.
- **NFR-MAINT-2:** mirrors AD-12; this test family is the regression gate.
- **NFR-SEC-1:** zero plugin network I/O. Mocking `socket.socket` to raise on construction is sufficient because every higher-level Python network call (urllib, http.client, requests) ultimately constructs a socket. If any lifecycle method were to dial home, the patched `socket.socket` would raise during construction and the test would fail.
- **AD-13 (logging namespace):** N/A for this story.

### Forward-compat design (THIS BRANCH IS PARALLEL TO S04/S07)

This branch starts from `main` at commit `3cb35bb` — **before** S04 (cli_runner) and S07 (IcmMemoryProvider) merge. Concretely:

- `hermes_icm_memory/` contains only `__init__.py` (S01 stub) and `_version.py`. Neither imports `subprocess`. **AC1 passes trivially.** Once S04 lands `cli_runner.py`, the test continues to pass (allow-listed file).
- Neither file contains `"~/.hermes"`. **AC2 passes trivially.** Once S05 lands `config.py` (which uses `pathlib.Path.home()` / env vars instead of literals), the test continues to pass.
- The S01 stub `_StubProvider` has only `name = "icm"` and **does not** implement `is_available` / `get_config_schema` / `save_config`. So tests 3, 4, 5 would error with `AttributeError` if they ran. **Solution:** module-level `_HAS_LIFECYCLE` predicate + `@pytest.mark.skipif`. Skip reason explicitly names S07.

**Why skipif (Option A) over getattr-with-fallback (Option B):** clearer signal in CI output. A skipped test is visible in pytest's summary; a `getattr(..., None)` fallback inside the test body silently passes when the method is absent, which masks the regression we *want* to enforce. Skipif also makes the dependency on S07 unambiguous: the skip reason becomes self-documenting changelog.

### Test plan (TDD; tests-first; mirrors AC1/AC2/AC3 1:1)

| #   | Test name                              | File                                                | Assertion                                                                                              | Active on this branch? |
|-----|----------------------------------------|-----------------------------------------------------|--------------------------------------------------------------------------------------------------------|------------------------|
| 1   | `test_only_cli_runner_imports_subprocess` | `test_no_subprocess_outside_cli_runner.py`         | AST walk: offending list (files importing `subprocess` outside `cli_runner.py`) is empty.              | YES                    |
| 1b  | `test_ast_walker_detects_subprocess_imports` | same file                                       | Negative control: helper returns True for crafted offending sources, False for benign sources.         | YES                    |
| 2   | `test_no_dotcache_literal_in_source`   | `test_no_hardcoded_dotcache.py`                     | Text scan: `"~/.hermes"` not in any source file content under `hermes_icm_memory/`.                    | YES                    |
| 2b  | `test_dotcache_scanner_detects_literal` | same file                                          | Negative control: scanner finds the literal in a synthetic source string.                              | YES                    |
| 3a  | `test_register_returns_provider`        | `test_no_network_calls.py`                          | Sanity: `_register_and_capture()` yields a non-None provider with a `name` attribute.                   | YES                    |
| 3b  | `test_lifecycle_predicate_smoke`        | same file                                           | `_HAS_LIFECYCLE` is a bool.                                                                             | YES                    |
| 4   | `test_is_available_no_socket`           | same file                                           | `socket.socket` patched to raise; `provider.is_available()` does not raise.                             | NO (skipif until S07)  |
| 5   | `test_get_config_schema_no_socket`      | same file                                           | Same patch; `provider.get_config_schema()` does not raise.                                              | NO (skipif until S07)  |
| 6   | `test_save_config_no_socket`            | same file                                           | Same patch; `provider.save_config({})` does not raise.                                                  | NO (skipif until S07)  |

### Hard quality gates

1. `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → 0 failures; ≥ 6 active tests on this branch (the original 4 from S01 + the new active S11 tests). Coverage gate met (S01 already gives 100 %; S11 adds tests, not source).
2. `ruff check .` → 0 issues.
3. `mypy --strict hermes_icm_memory tests` → 0 errors.
4. `git status` clean.
5. All work on branch `s11`.

### Common LLM-developer pitfalls (avoid)

- **Don't write the subprocess test as a regex.** AC1 explicitly mandates `ast`. Regex misses `import subprocess as sp` cleanly; AST does not.
- **Don't exclude `cli_runner.py` by checking the import path string.** Compare the file's path *relative to the package directory* — `path.name == "cli_runner.py"` is a one-liner that survives refactors.
- **Don't skip the negative-control tests.** A test that always passes (because the predicate vacuously holds) is worse than no test — it gives false confidence. The negative-control tests prove the scanner has teeth.
- **Don't mock `urllib.request` or `requests`.** Mocking at `socket.socket` catches *all* network paths in one place. Anything higher-level would miss raw-socket users.
- **Don't import the lifecycle methods at module load time and `try/except ImportError` around them.** Use `hasattr(provider, "is_available")` etc. inside `_HAS_LIFECYCLE` — survives refactors and keeps the predicate readable.
- **Don't add type stubs for the fake `ctx`.** `Any` is sufficient; `mypy --strict` will accept a `class _Ctx:` with a single `register_memory_provider` method.
- **Don't forget the `__init__` files.** `tests/__init__.py` already exists from S01; no new package needed.
- **Don't widen `socket` patch to module-level fixtures.** Per-test `monkeypatch` keeps blast radius minimal.

### Rationale for design choices

- **AST over regex** for AC1: aliasing (`import subprocess as sp`) and conditional imports inside functions are natively handled. Regex requires escalating complexity for diminishing return.
- **Text-scan for AC2** (not AST): `"~/.hermes"` may appear in docstrings, comments, or string literals — AST is overkill and would miss comments. A simple `in` check on file content is correct and minimal.
- **`socket.socket` patch** for AC3: the lowest common denominator for outbound network in CPython. Patching it forces any actual network call to raise during socket construction, which is the moment we want to detect.
- **Skipif over getattr-fallback**: visible in test summary, self-documenting, automatic light-up on S07 merge.

### File-conflict awareness

- This story creates THREE new files in `tests/`. None of S04/S05/S06/S07 modify these files. **Zero conflict risk.**
- S04 will introduce `hermes_icm_memory/cli_runner.py` — the AST walker explicitly allow-lists this filename.
- S07 will introduce `is_available` / `get_config_schema` / `save_config` on the real provider, at which point the three skipif tests light up. **No edit to this story's files required from S07.**

### Project Structure Notes

After this story, `tests/` adds three files:

```
tests/
├── __init__.py                                    # pre-existing (S01)
├── conftest.py                                    # pre-existing (S01)
├── test_plugin_loader.py                          # pre-existing (S01)
├── test_no_subprocess_outside_cli_runner.py       # NEW (S11)
├── test_no_hardcoded_dotcache.py                  # NEW (S11)
└── test_no_network_calls.py                       # NEW (S11)
```

No source-tree changes; tests-only story.

### References

- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 5.1: Architectural invariant tests (subprocess, dot-cache, network)] — story spec, ACs, test plan, files-touched.
- [Source: _bmad-output/planning-artifacts/architecture.md#4. Component Map → Invariants] — the four invariants enumerated; AC1/AC2 are #1/#4.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.1 Critical decisions → AD-12] — subprocess isolation rationale.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-MAINT-2] — architecture-v2 swap allowed only because AD-12 holds.
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-SEC-1] — zero plugin network I/O.
- [Source: _bmad-output/planning-artifacts/prd.md#FR2] — path injection (no hardcoded `~/.hermes`).

## Dev Agent Record

### Agent Model Used

Claude Opus (BMAD dev-story phase, hermes-icm s11 teammate)

### Debug Log References

- RED-confirm phase: temporarily injected `import subprocess` and `"~/.hermes/..."` into `hermes_icm_memory/__init__.py`. Both invariant tests failed loudly with file-naming offender messages. Reverted; both green.
- Skipif-flip phase: temporarily added `is_available` / `get_config_schema` / `save_config` (each a no-op) to `_StubProvider`. The three skipif-gated tests lit up automatically with no test-file edit and all 5 socket tests passed. Reverted.
- Final run: `pytest -v` → 10 passed, 3 skipped (S01: 4, S11 active: 6, S11 skipped: 3). Coverage 100 %, gate ≥85 % met.
- `ruff check .` → All checks passed.
- `mypy --strict hermes_icm_memory tests` → Success: no issues found in 8 source files.

### Completion Notes List

- All 4 acceptance criteria satisfied (AC1 subprocess AST-walk, AC2 dot-cache literal text-scan, AC3 socket-patch lifecycle tests, AC4 forward-compat skipif gate).
- Strict TDD: RED phase confirmed by temporarily violating each invariant; GREEN phase confirmed by reverting. Skipif-flip behaviour verified by temporarily adding lifecycle methods to the S01 stub.
- Forward-compat design choice = Option A (skipif). Rationale: visible in pytest summary, self-documenting skip reason names S07, automatic light-up on S07 merge with no follow-up edit.
- Negative-control tests included for both AST walker and text scanner — proves the scanners actually have teeth, not just vacuously-passing predicates.
- `socket.socket` patched at the lowest common denominator: every higher-level Python network call ultimately constructs a socket, so this single patch catches all paths (urllib, http.client, requests, raw sockets).
- AST-not-regex enforced for AC1 (per spec): handles `import subprocess as sp` cleanly; regex would require escalating complexity.
- Text-scan-not-AST chosen for AC2: literal must be absent from comments/docstrings/string-literals alike, so simple `in` check on file content is correct and minimal.

### File List

- `tests/test_no_subprocess_outside_cli_runner.py` (NEW) — AC1, AST walker + negative control.
- `tests/test_no_hardcoded_dotcache.py` (NEW) — AC2, text scan + negative control.
- `tests/test_no_network_calls.py` (NEW) — AC3, socket-patch lifecycle tests + AC4 skipif gate.
- `_bmad-output/implementation-artifacts/5-1-architectural-invariant-tests.md` (NEW) — this story spec.

### Change Log

| Date       | Change |
|------------|--------|
| 2026-05-06 | S11 story spec drafted (Phase 1 — bmad-create-story). Forward-compat design: skipif Option A. |
| 2026-05-06 | S11 implementation (Phase 2 — bmad-dev-story): 3 new test files, 9 new tests (6 active + 3 skipif). RED-confirm via temporary invariant violation. Skipif-flip verified by temporarily adding lifecycle methods. 10 passed, 3 skipped, ruff + mypy --strict clean, coverage 100 %. |
| 2026-05-06 | Phase 3 code-review (Blind Hunter + Edge Case Hunter + Acceptance Auditor): Acceptance Auditor PASS, no AC violations. Patches applied: (1) `_raise_on_socket` return-type changed from `socket.socket` to `NoReturn` (function always raises — type hint now matches reality); (2) AST-walker negative-control extended to `from subprocess import *` and `import subprocess.constants` so the catch-coverage of submodule + star-import is asserted explicitly. No deferrals. |
| 2026-05-06 | Phase 4 simplify pass: no-op (reviewed 3 test files across reuse / quality / efficiency layers, all three reviewers converged on "ship as-is, appropriately minimal"). Considered factoring `_iter_package_py_files()` into a shared helper module — rejected because the duplication is 3 lines and a shared module would couple independent invariants whose failure outputs should remain unambiguous about which invariant tripped. No simplify commit needed. |
