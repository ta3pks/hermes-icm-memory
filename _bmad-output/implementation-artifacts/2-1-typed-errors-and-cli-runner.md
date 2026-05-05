# Story 2.1: Typed errors + CLI runner (read & write paths)

Status: draft
Story ID: S04 · Epic: 2 (ICM adapter core) · Effort: M · Dependencies: S01

## Story

As a maintainer,
I want `cli_runner.py` to be the only module that imports `subprocess` and to wrap every `icm` invocation behind typed exceptions,
so that v2's MCP-transport swap touches one file (NFR-MAINT-2) and every failure mode is centrally translatable into the AD-07 degrade response.

## Acceptance Criteria

**AC1 — `run_recall` builds the documented argv and returns parsed JSON on success**

- **Given** `cli_runner.run_recall(query, limit, db_path, timeout_ms, topic=None, project=None)`
- **When** called with `subprocess.run` mocked to return `returncode=0` and a JSON-list stdout
- **Then** the argv passed to `subprocess.run` is exactly
  `["icm", "--db", str(db_path), "recall", query, "--limit", str(limit), "--format", "json"]`
  with `["-t", topic]` and `["-p", project]` appended in that order only when supplied,
  the call is invoked with `shell=False`, `check=False`, `capture_output=True`, `text=True`, and `timeout=timeout_ms / 1000`,
  and the function returns the parsed `list[dict]`.

**AC2 — `run_recall` raises typed exceptions for every documented failure mode**

- **Given** `subprocess.run` raising `FileNotFoundError` → `cli_runner` raises `ICMNotFoundError`.
- **Given** `subprocess.run` raising `subprocess.TimeoutExpired` → raises `ICMTimeoutError`.
- **Given** `returncode != 0` with non-empty stderr → raises `ICMNonZeroExitError` with the stderr payload included in the message.
- **Given** `returncode == 0` but `stdout` is not valid JSON → raises `ICMMalformedOutputError` with the first 200 chars of stdout in the message.
- **Then** every typed exception is a subclass of `ICMError` defined in `errors.py`, and the original exception (where applicable) is chained via `raise ... from exc`.

**AC3 — `run_store` builds list-form argv and ignores stdout**

- **Given** `cli_runner.run_store(topic, content, importance, db_path, timeout_ms, keywords=None, raw=None)`
- **When** called with the mock returning `returncode=0`
- **Then** the argv contains
  `["icm", "--db", str(db_path), "store", "-t", topic, "-c", content, "-i", importance]`
  with `["-k", keywords]` and `["-r", raw]` appended in that order only when supplied,
  the function returns `None` regardless of stdout content,
  and the same four typed exceptions are raised on the matching failure modes (no JSON parse path — `ICMMalformedOutputError` does not apply to writes).

**AC4 — `run_topics` and `run_health` invoke their JSON subcommands**

- **Given** `run_topics(db_path, timeout_ms)` → argv is `["icm", "--db", str(db_path), "topics", "--format", "json"]`; on success returns the parsed list.
- **Given** `run_health(db_path, timeout_ms, topic=None)` → argv is `["icm", "--db", str(db_path), "health", "--format", "json"]` with `["-t", topic]` appended only when supplied; on success returns the parsed dict.
- Failure modes (not-found / timeout / nonzero / malformed) match AC2.

> **Reality-check note (does NOT change the spec for S04):** the team-lead briefing flagged that `icm 0.10.43` does not actually support `--format json` for `topics` / `health`; line-split table parsing is required in production. **This story builds and tests the `--format json` argv shape** because the architecture spec (§6.1) and the binding S04 test plan in epics-and-stories.md both lock that argv shape, and the AST/integration tests in S11/S14 are what will surface the real-CLI mismatch. A follow-up bridge (table-parse fallback) is explicitly **out of scope** for S04 and will be filed as a separate change against `cli_runner.py` once S11/S14 land. See "Deviations" in the Dev Agent Record after implementation.

**AC5 — DEBUG log emitted with redacted argv + elapsed milliseconds**

- **Given** any of the four `run_*` functions
- **When** invoked (success path)
- **Then** a single `DEBUG`-level log entry is emitted on `logging.getLogger("hermes_icm_memory.cli_runner")` containing:
  - the redacted argv (each `query` / `content` argument truncated to ≤ 80 chars with a `…` marker if truncated),
  - the elapsed wall-clock time in milliseconds (integer ms, measured around the `subprocess.run` call).
- The redaction is observable from `caplog`; an 81-char query/content string is truncated, an 80-char one is not.

**AC6 — Subprocess invocation contract is uniform**

- Every call to `subprocess.run` from `cli_runner.py` passes `shell=False`, `check=False`, `capture_output=True`, `text=True`, `timeout=timeout_seconds` (a positional `timeout_ms` divided by 1000 — never a hard-coded number).
- `cli_runner.py` is the **only** module under `hermes_icm_memory/` that imports `subprocess` (NFR-MAINT-2; the AST gate in S11 will assert this once it lands).

## Tasks / Subtasks

> **TDD discipline (mandatory):** every code task is preceded by writing the failing test for it. Run `pytest tests/test_cli_runner.py -q --no-cov` after writing tests but before writing impl, and confirm all 13 FAIL. Only then write `errors.py` + `cli_runner.py`.

- [ ] **Task 1 — Write the 13 failing tests (AC1–AC6, all matched 1:1 by test name)**
  - [ ] 1.1 Create `tests/test_cli_runner.py` containing the 13 tests from §Test Plan (verbatim names).
  - [ ] 1.2 All tests must mock `subprocess.run` (patch target: `hermes_icm_memory.cli_runner.subprocess.run`).
  - [ ] 1.3 Confirm `pytest tests/test_cli_runner.py -q --no-cov` reports 13 collected, 13 errors/failures (impl missing). Capture the failure log for the Dev Agent Record.

- [ ] **Task 2 — Implement `errors.py` (AC2, AC4)**
  - [ ] 2.1 Create `hermes_icm_memory/errors.py` with `ICMError` (base) and four subclasses: `ICMNotFoundError`, `ICMTimeoutError`, `ICMNonZeroExitError`, `ICMMalformedOutputError`.
  - [ ] 2.2 No `__init__` overrides — the four subclasses inherit `Exception` semantics from `ICMError(Exception)`. Module docstring + per-class one-line docstring.
  - [ ] 2.3 Import nothing from this package (`errors.py` is leaf-pure per architecture §4.1 invariant 3).

- [ ] **Task 3 — Implement `cli_runner.py` (AC1, AC3, AC4, AC5, AC6)**
  - [ ] 3.1 Create `hermes_icm_memory/cli_runner.py` with the four public functions: `run_recall`, `run_store`, `run_topics`, `run_health`.
  - [ ] 3.2 Single private helper `_run(argv, timeout_ms)` runs `subprocess.run` with the locked kwargs (AC6), measures elapsed_ms, emits the DEBUG log with redacted argv, and returns `(returncode, stdout, stderr)` — translating `FileNotFoundError` / `TimeoutExpired` / `OSError` into typed errors at this single boundary.
  - [ ] 3.3 Public functions build argv lists, call `_run`, raise `ICMNonZeroExitError` if `returncode != 0`, parse JSON where applicable (raise `ICMMalformedOutputError` on `JSONDecodeError`).
  - [ ] 3.4 Re-run `pytest tests/test_cli_runner.py -q --no-cov`. All 13 pass.

- [ ] **Task 4 — Quality gates (all six ACs)**
  - [ ] 4.1 `ruff check .` → 0 issues.
  - [ ] 4.2 `mypy --strict hermes_icm_memory tests` → 0 errors.
  - [ ] 4.3 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → passes (expect 100 % on the two new modules; S01's `__init__.py` + `_version.py` already at 100 %).
  - [ ] 4.4 `git status` clean → commit `feat(S04): typed errors + cli_runner with 4 run_* functions`.

## File Spec (authoritative — copy-paste boilerplate intent, not literal)

### `hermes_icm_memory/errors.py` (NEW)

```python
"""Typed exceptions raised inside ``cli_runner``.

Caught at the cli_runner boundary and translated into the AD-07 degrade
response by ``tools.py`` / ``hooks.py`` (S08, S09). ``cli_runner`` is the
only module allowed to raise these; downstream modules catch ``ICMError``
or its subtypes broadly at their public boundary.
"""

from __future__ import annotations

__all__ = [
    "ICMError",
    "ICMNotFoundError",
    "ICMTimeoutError",
    "ICMNonZeroExitError",
    "ICMMalformedOutputError",
]


class ICMError(Exception):
    """Base class for every typed error raised by ``cli_runner``."""


class ICMNotFoundError(ICMError):
    """Raised when the ``icm`` binary cannot be found on PATH."""


class ICMTimeoutError(ICMError):
    """Raised when an ``icm`` invocation exceeds its configured timeout."""


class ICMNonZeroExitError(ICMError):
    """Raised when ``icm`` exits with a non-zero return code."""


class ICMMalformedOutputError(ICMError):
    """Raised when ``icm`` stdout is not valid JSON for a JSON-format read."""
```

### `hermes_icm_memory/cli_runner.py` (NEW — shape, not literal)

The file MUST:

- Be the only module under `hermes_icm_memory/` to `import subprocess`.
- Use `logging.getLogger(__name__)` (resolves to `"hermes_icm_memory.cli_runner"`), never `logging.getLogger()` (root).
- Define a single private helper that owns the `subprocess.run` call and the DEBUG-log + redaction logic.
- Expose four public functions whose signatures match the AC text and the test plan exactly.

Argv construction rules:

- Always list-form, never f-strings into a shell.
- Order: `["icm", "--db", str(db_path), <subcommand>, ...positional..., "--format", "json"]` for read paths; `["icm", "--db", str(db_path), "store", "-t", topic, "-c", content, "-i", importance]` for the write path.
- Optional flags appended in the documented order only when their corresponding kwarg is not `None`.

Argv redaction (DEBUG log only — the real `subprocess.run` argv is unredacted):

- Walk a copy of the argv. Truncate any element longer than 80 chars to its first 80 chars + `"…"`. Tests assert the boundary at exactly 80/81 chars. Truncation policy is positional, not flag-aware (simplicity > cleverness).

### `tests/test_cli_runner.py` (NEW)

Mock target: `hermes_icm_memory.cli_runner.subprocess.run`. Use `unittest.mock.patch` as a context manager or `monkeypatch` per test author preference; either is fine. Use `MagicMock` for the returned object with `returncode`, `stdout`, `stderr` set per case. For the timeout / not-found tests, set `side_effect=...` to the exception instance.

The 13 tests:

| #   | Name                                                            | What it asserts                                                                                                                                       | AC mapping |
|-----|-----------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------|------------|
| 1   | `test_run_recall_argv_shape_default`                            | argv is `["icm", "--db", "<p>", "recall", "<q>", "--limit", "5", "--format", "json"]` when `topic` and `project` are `None`.                          | AC1        |
| 2   | `test_run_recall_argv_shape_with_topic_and_project`             | argv ends with `"-t", "<topic>", "-p", "<project>"` (in that order) when supplied.                                                                    | AC1        |
| 3   | `test_run_recall_returns_parsed_list`                           | mock stdout `'[{"id":"x"}]'` → return value `[{"id": "x"}]`.                                                                                          | AC1        |
| 4   | `test_run_recall_raises_not_found`                              | `subprocess.run.side_effect = FileNotFoundError` → `ICMNotFoundError`.                                                                                | AC2        |
| 5   | `test_run_recall_raises_timeout`                                | `side_effect = subprocess.TimeoutExpired(cmd=..., timeout=...)` → `ICMTimeoutError`.                                                                  | AC2        |
| 6   | `test_run_recall_raises_nonzero`                                | `returncode=2`, `stderr="boom"` → `ICMNonZeroExitError`; `"boom"` appears in `str(exc.value)`.                                                        | AC2        |
| 7   | `test_run_recall_raises_malformed`                              | `returncode=0`, `stdout="not json"` → `ICMMalformedOutputError`; first 200 chars of stdout appear in the message.                                     | AC2        |
| 8   | `test_run_store_argv_shape`                                     | argv is `["icm", "--db", "<p>", "store", "-t", "<topic>", "-c", "<content>", "-i", "<importance>"]`; `-k`/`-r` only appear when supplied.             | AC3        |
| 9   | `test_run_store_does_not_parse_stdout`                          | mock stdout is gibberish; `run_store` returns `None` and does not raise.                                                                              | AC3        |
| 10  | `test_run_topics_argv_and_parse`                                | argv is `["icm", "--db", "<p>", "topics", "--format", "json"]`; mock stdout `'["a","b"]'` → return `["a", "b"]`.                                       | AC4        |
| 11  | `test_run_health_argv_with_topic`                               | argv ends with `"-t", "<topic>"` when supplied; mock stdout `'{"status":"ok"}'` → return `{"status": "ok"}`.                                          | AC4        |
| 12  | `test_debug_log_emits_redacted_argv`                            | `caplog.set_level("DEBUG")`; query of length 81 → DEBUG log contains the truncated form ending in `"…"`; query of length 80 → no truncation marker.    | AC5        |
| 13  | `test_subprocess_invoked_with_shell_false_and_timeout`          | inspects `subprocess.run.call_args.kwargs`; asserts `shell=False`, `check=False`, `capture_output=True`, `text=True`, `timeout == timeout_ms / 1000`. | AC6        |

> **Why mock `cli_runner.subprocess.run` and not `subprocess.run` globally:** patching the locally-imported reference is robust against import-time aliasing and matches the reference scaffold's `test_hook.py`. (Patching `subprocess.run` on the stdlib module also works but couples to import order.)

## Dev Notes

### Architecture compliance (must follow)

- **AD-01 / AD-12 (subprocess isolation):** `cli_runner.py` is the **only** module under `hermes_icm_memory/` that imports `subprocess`. The AST/grep gate in S11 (`tests/test_no_subprocess_outside_cli_runner.py`) will enforce this once it lands. This story plants the seed: `errors.py` does **not** import `subprocess`.
- **AD-13 (logging namespace `hermes_icm_memory.*`):** use `logger = logging.getLogger(__name__)` at module top of `cli_runner.py`. Never `logging.getLogger()` (root). Never `print()`.
- **AD-19 (list-form argv, never shell-string):** every argv passed to `subprocess.run` is a list. No `shell=True`. No `" ".join(...)` of user input.
- **NFR-PERF-3 / NFR-SEC-3:** every `subprocess.run` call passes `timeout=` and `shell=False`. Both are tested by AC6 / test #13.
- **NFR-MAINT-1 (frozen API):** the four public function names + their parameter names are public surface from this story onward. Do not rename later; v2 may add kwargs but cannot remove or rename.
- **`run_recall` accepts `db_path`** as a `Path` or string and passes `str(db_path)` into argv. Tests use a `Path` to lock the conversion.

### TDD execution log expected (mirror S01)

- **RED phase:** write all 13 tests first → run `pytest tests/test_cli_runner.py -q --no-cov` → 13 failures (collection errors are acceptable since `cli_runner` and `errors` don't exist yet — convert to import errors counted as failures).
- **GREEN phase:** create `errors.py`, then `cli_runner.py`, re-run pytest → 13 passes.
- **No refactor phase needed** if the GREEN code is already clean — just verify ruff + mypy gates.

### Test plan (TDD; tests-first; mirrors AC1–AC6 1:1)

(See the 13-row table above; copied here verbatim for the test author.)

### Hard quality gates (must all pass before story is "done")

1. `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → 0 failures, coverage ≥ 85 %.
2. `ruff check .` → 0 issues.
3. `mypy --strict hermes_icm_memory tests` → 0 errors.
4. The two new files (`errors.py`, `cli_runner.py`) plus the test file are committed on branch `s04`.
5. `git log --oneline` shows `feat(S04): ...` as the latest commit.

### Common LLM-developer pitfalls (avoid)

- **Don't combine errors and cli_runner into one module.** Architecture §4.1 invariant 3 requires `errors.py` to import nothing from this package; merging would force a circular-import workaround later.
- **Don't add a `--format json` fallback / table parser** to `topics` / `health` in this story. The architecture spec locks the JSON-format argv shape; the line-split parser is a follow-up after S11/S14 surface the mismatch. See AC4 reality-check note.
- **Don't import `subprocess` in `errors.py` or anywhere else under `hermes_icm_memory/`.** S11's AST test will fail; even before S11 lands, we keep the invariant by hand.
- **Don't call `icm init` from `cli_runner.py`.** AD-06: the plugin never invokes `icm init`. The four `run_*` functions are read/write only.
- **Don't truncate flag values like `--limit`** in the DEBUG redactor. The redactor is positional / length-based: anything > 80 chars gets clipped, regardless of whether it's a flag value or a positional. Test #12 only exercises the long-`query` case; flag truncation is acceptable as a degenerate side effect (no PII-relevant flag value is > 80 chars in practice).
- **Don't catch `Exception` inside `cli_runner._run`** to avoid masking bugs. Catch `FileNotFoundError`, `subprocess.TimeoutExpired`, `OSError` explicitly — let everything else propagate (mypy / ruff will flag broad excepts via rule `BLE001`).
- **Don't bake the timeout-seconds conversion into the public signature.** Public params are `timeout_ms` (ms-int); the conversion to `timeout=ms/1000` happens once, inside `_run`. Test #13 asserts on the seconds form (`call_args.kwargs["timeout"] == 5.0` when `timeout_ms=5000`).
- **Don't decorate the four public functions with `@functools.cache`.** They're not pure (subprocess side effects).

### Project Structure Notes

After this story the tree gains exactly two source files and one test file:

```
hermes_icm_memory/
├── __init__.py            # unchanged from S01
├── _version.py            # unchanged from S01
├── cli_runner.py          # NEW — the only subprocess-importing module
└── errors.py              # NEW — typed exceptions

tests/
├── __init__.py            # unchanged from S01
├── conftest.py            # unchanged from S01 (still empty placeholder)
├── test_cli_runner.py     # NEW — 13 mocked-subprocess tests
└── test_plugin_loader.py  # unchanged from S01
```

No conflict with S05 (`config.py`), S06 (`mapping.py`), or S07–S10 (`provider.py`, `tools.py`, `hooks.py`) — each touches different files. S11 will add `tests/test_no_subprocess_outside_cli_runner.py` which polices this story's invariant; it must continue to pass.

### References

- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 2.1: Typed errors + CLI runner (read & write paths)] — story spec, ACs, the 13-test plan, files-touched.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.1 Critical decisions] — AD-01 (subprocess shellouts), AD-08 (timeouts), AD-09 (JSON format), AD-12 (subprocess isolation), AD-13 (logging namespace), AD-19 (list-form argv).
- [Source: _bmad-output/planning-artifacts/architecture.md#6.1 Subcommand surface] — argv shapes for all four `run_*` functions.
- [Source: _bmad-output/planning-artifacts/architecture.md#6.2 Subprocess invocation contract] — the locked `subprocess.run` kwargs.
- [Source: _bmad-output/planning-artifacts/architecture.md#6.3 Failure-mode matrix] — every typed exception's trigger and behavior.
- [Source: _bmad-output/planning-artifacts/architecture.md#11.2 Logging] — namespace + DEBUG-level redacted-argv requirement.
- [Source: _bmad-output/planning-artifacts/prd.md] — NFR-PERF-3, NFR-SEC-3, NFR-MAINT-1, NFR-MAINT-2.

## Dev Agent Record

### Agent Model Used

Claude Opus (BMAD dev-story phase, S04 lane).

### Debug Log References

(populated during Phase 2)

### Completion Notes List

(populated during Phase 2)

### File List

(populated during Phase 2)

### Change Log

| Date       | Change                                                                                                  |
|------------|---------------------------------------------------------------------------------------------------------|
| 2026-05-06 | S04 story spec drafted from epics-and-stories.md §Story 2.1.                                            |
