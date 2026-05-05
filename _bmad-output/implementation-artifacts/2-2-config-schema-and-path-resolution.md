# Story 2.2: Config schema + path resolution

Status: review
Story ID: S05 · Epic: 2 (ICM adapter core) · Effort: M · Dependencies: S01

## Story

As a Hermes user,
I want a tunable config (importance default, recall limit, queue size, timeouts, etc.) and per-profile DB-path resolution,
so that I can fit the plugin to my workflow and run multiple Hermes profiles without DB collision (FR2, FR6, FR7, AD-05, AD-06, AD-18).

## Acceptance Criteria

**AC1 — `get_default_schema()` returns the ten architecture §10.1 keys**

- **Given** `config.get_default_schema()`
- **When** called
- **Then** it returns a `list` of exactly ten entries, one per architecture §10.1 key. Each entry is a `dict` carrying:
  - `key` — the config key name (string).
  - `description` — short human-readable description.
  - `secret` — `False` (no secrets in this plugin's config).
  - `required` — `bool` flag. All ten defaults are usable, so all `required` flags are `False` for v1.
  - `type` — one of `"int"`, `"bool"`, `"string"`, `"enum"`.
  - `default` — the architecture-locked default value.
  - `choices` — present **only** for `type="enum"` (i.e. the `default_importance` key); a list of the legal enum values.
- The exact key set must match the architecture matrix verbatim:
  `default_importance`, `topic_prefix`, `recall_limit`, `prefetch_enabled`, `sync_write_queue_size`, `command_timeout_read_ms`, `command_timeout_write_ms`, `session_end_grace_ms`, `periodic_progress_every_n_turns`, `consolidate_on_session_end`.

**AC2 — `validate(values: dict)` returns `(True, normalized)` for valid input**

- **Given** `config.validate(values)` where every key present is valid
- **When** called
- **Then** the result is a `(True, normalized_values)` tuple where `normalized_values` is a `dict` with:
  - integer-typed keys coerced from `str` to `int` (e.g. `"5"` → `5`).
  - boolean-typed keys coerced from `"true"`/`"false"` (case-insensitive) to `True`/`False`.
  - string-typed keys returned as-is.
  - enum-typed keys returned as-is (already a string, but checked against `choices`).
- Unknown keys are passed through unchanged in `normalized_values` (no schema lock-out — the plugin tolerates forward-compatible extras).

**AC3 — `validate(values)` rejects invalid input with `(False, {"error": ...})`**

- **Given** `config.validate(values)` with at least one bad value (out-of-range int, unknown enum, wrong type)
- **When** called
- **Then** the result is a `(False, {"error": "<actionable message that names the bad key>"})` tuple. The error message contains the offending key name so the operator can fix it. The function never raises (AD-18, FR7).

**AC4 — `validate(values)` never raises on garbage input**

- **Given** `validate(garbage)` where `garbage` is `None`, a `list`, a string, a nested dict, or any other shape that is not a flat `dict`
- **When** called
- **Then** it returns a `(False, {"error": ...})` tuple. No `TypeError` / `AttributeError` escapes.

**AC5 — `resolve_db_path(hermes_home, profile=None)` default profile**

- **Given** `config.resolve_db_path(hermes_home="/tmp/hh-A")` with `profile=None`
- **When** called
- **Then** it returns `Path("/tmp/hh-A/icm/default.db")` (absolute, resolved).

**AC6 — `resolve_db_path(hermes_home, profile=...)` named profile**

- **Given** `config.resolve_db_path(hermes_home="/tmp/hh-A", profile="work")`
- **When** called
- **Then** it returns `Path("/tmp/hh-A/icm/work.db")`.

**AC7 — `resolve_db_path` expands `~`**

- **Given** `hermes_home="~/foo"` and `monkeypatch.setenv("HOME", "/tmp/fakehome")`
- **When** `resolve_db_path` is called
- **Then** the returned path is `Path("/tmp/fakehome/foo/icm/default.db")` (absolute, `~` expanded). The function uses `Path(...).expanduser().resolve()`.

**AC8 — `resolve_db_path` accepts `os.PathLike`**

- **Given** a `Path` (or any `os.PathLike`) passed for `hermes_home`
- **When** `resolve_db_path` is called
- **Then** the function works identically to the `str` form — no `TypeError`.

**AC9 — `mkdir_parent(db_path)` is idempotent**

- **Given** `config.mkdir_parent(db_path)` where `db_path = "<tmp>/icm/default.db"`
- **When** called twice in succession
- **Then** the parent directory `<tmp>/icm` exists, no exception is raised, and the second call is a no-op (architecture §9.2: `mkdir(parents=True, exist_ok=True)`).

**AC10 — `validate` rejects negative queue size with the offending key in the message**

- **Given** `validate({"sync_write_queue_size": -1})`
- **When** called
- **Then** the result is `(False, {"error": "...sync_write_queue_size..."})`. The error message names the bad key by its exact identifier.

## Tasks / Subtasks

> **TDD discipline (mandatory):** every code task is preceded by writing the failing test for it. Run `pytest tests/test_config.py -q --no-cov` after writing tests but before any implementation, confirm RED, then implement to GREEN.

- [x] **Task 1 — Write failing tests first (AC1–AC10)**
  - [x] 1.1 Created `tests/test_config.py` with the ten named tests from §Test Plan plus five focused coverage tests (bool-as-int, unparseable int, non-string string-key, arbitrary string for bool key, non-{str,bool} for bool key, unknown-key passthrough).
  - [x] 1.2 RED phase confirmed — `pytest tests/test_config.py --no-cov -q` reported `ImportError: cannot import name 'config' from 'hermes_icm_memory'`.

- [x] **Task 2 — `config.py` implementation (AC1–AC10)**
  - [x] 2.1 Created `hermes_icm_memory/config.py`.
  - [x] 2.2 Defined `_SCHEMA_ENTRIES: Final[list[dict[str, Any]]]` literal mirroring architecture §10.1 verbatim (10 entries, all `secret=False`, all `required=False`, `choices` only on the enum entry).
  - [x] 2.3 `get_default_schema()` returns `copy.deepcopy(_SCHEMA_ENTRIES)` — verified by mutation test in `test_default_schema_has_ten_keys`.
  - [x] 2.4 `validate(values)` — flat-dict check, per-key coercion via `_coerce_int` / `_coerce_bool` (bool explicitly refused for int keys), range gate via `_INT_MIN`, enum membership check, unknown-key passthrough, defensive `try/except Exception` outermost layer.
  - [x] 2.5 `resolve_db_path(...)` accepts `str` or `os.PathLike`, expands `~`, calls `.resolve()`, returns `<hermes_home>/icm/<profile>.db`. Empty profile string also falls back to `"default"` (defensive).
  - [x] 2.6 `mkdir_parent(db_path)` — `db_path.parent.mkdir(parents=True, exist_ok=True)`.
  - [x] 2.7 GREEN phase: 15 of 15 collected cases pass (10 named tests; the garbage-input test is parametrized over 6 cases). Full suite 34 passed, 3 skipped (integration).

- [x] **Task 3 — Quality gates**
  - [x] 3.1 `ruff check .` → All checks passed!
  - [x] 3.2 `mypy --strict hermes_icm_memory tests` → Success: no issues found in 12 source files.
  - [x] 3.3 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → passes; `config.py` **100 %** (79 stmts, 0 miss, 38 branches, 0 partial). Total package coverage 98.38 %.
  - [x] 3.4 Story spec + impl committed on branch `s05`.

## File Spec

### `hermes_icm_memory/config.py` (NEW)

Contract sketch (do NOT copy verbatim — write to satisfy tests):

```python
"""Config schema, validation, and DB-path resolution (FR2, FR6, FR7, AD-05, AD-06, AD-18).

Pure module: no I/O beyond filesystem path construction in resolve_db_path /
mkdir_parent. No subprocess, no logging, no network. Public surface is
get_default_schema(), validate(values), resolve_db_path(hermes_home, profile),
and mkdir_parent(db_path). All four are frozen by NFR-MAINT-1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

_DEFAULT_PROFILE: Final[str] = "default"

_IMPORTANCE_CHOICES: Final[tuple[str, ...]] = ("critical", "high", "medium", "low")

# Architecture §10.1 — ten frozen keys.
_SCHEMA_ENTRIES: Final[list[dict[str, Any]]] = [
    {"key": "default_importance", "type": "enum", "default": "high",
     "choices": list(_IMPORTANCE_CHOICES), "secret": False, "required": False,
     "description": "..."},
    # ... nine more entries, mirroring §10.1 exactly.
]


def get_default_schema() -> list[dict[str, Any]]:
    """Return a defensive copy of the schema (callers must not mutate module state)."""
    ...


def validate(values: Any) -> tuple[bool, dict[str, Any]]:
    """Structural validation. Returns (True, normalized) or (False, {"error": ...})."""
    ...


def resolve_db_path(
    hermes_home: str | os.PathLike[str],
    profile: str | None = None,
) -> Path:
    """Compute <hermes_home>/icm/<profile>.db. Expands `~`, returns absolute path."""
    ...


def mkdir_parent(db_path: Path) -> None:
    """Idempotently ensure db_path.parent exists (parents=True, exist_ok=True)."""
    ...
```

### `tests/test_config.py` (NEW)

Test plan — exact names, mapped to ACs:

| #   | Test name                                              | AC mapping |
|-----|--------------------------------------------------------|------------|
| 1   | `test_default_schema_has_ten_keys`                     | AC1        |
| 2   | `test_validate_accepts_default_values`                 | AC2        |
| 3   | `test_validate_rejects_negative_queue_size`            | AC3, AC10  |
| 4   | `test_validate_rejects_unknown_importance`             | AC3        |
| 5   | `test_validate_coerces_strings_to_ints`                | AC2        |
| 6   | `test_validate_never_raises_on_garbage_input`          | AC4        |
| 7   | `test_resolve_db_path_default_profile`                 | AC5        |
| 8   | `test_resolve_db_path_named_profile`                   | AC6        |
| 9   | `test_resolve_db_path_expands_tilde`                   | AC7, AC8   |
| 10  | `test_resolve_db_path_makes_parent_idempotent`         | AC9        |

## Dev Notes

### Architecture compliance (must follow)

- **AD-02 (Python 3.11+):** `from __future__ import annotations`, `Final`, PEP 604 union syntax (`str | None`), type hints throughout.
- **AD-05 (profile isolation):** DB path is `<hermes_home>/icm/<profile>.db`. Profile defaults to `"default"`.
- **AD-06 (idempotent mkdir; no `icm init`):** parent directory creation is `parents=True, exist_ok=True`. SQLite file itself auto-creates on first ICM call.
- **AD-12 (subprocess isolation):** `config.py` MUST NOT import `subprocess`. It is a pure module.
- **AD-13 (logging discipline):** `config.py` does NOT log. Pure validation; if invalid input arrives, the caller (`provider.save_config`) handles logging.
- **AD-18 (validation never raises):** every invalid value path returns `(False, {"error": ...})`. The function catches its own `TypeError` / `AttributeError` defensively.
- **NFR-MAINT-1 (frozen public API):** `get_default_schema`, `validate`, `resolve_db_path`, `mkdir_parent`, and the ten config keys are frozen post-v1.
- **NFR-MAINT-3 (mypy --strict):** every public function fully type-hinted.
- **NFR-SEC-2 (no hardcoded `~/.hermes`):** the literal `"~/.hermes"` does not appear anywhere in this module. All paths derive from `hermes_home`.

### Schema entry shape (architecture §10.1 verbatim)

| Key | type | default | choices | description (short) |
|-----|------|---------|---------|---------------------|
| `default_importance` | `enum` | `"high"` | `["critical","high","medium","low"]` | Importance applied when `icm_store` omits it. |
| `topic_prefix` | `string` | `""` | — | Optional prefix prepended to every stored topic, e.g. `"hermes/"`. |
| `recall_limit` | `int` | `5` | — | Top-K for prefetch + `system_prompt_block`. |
| `prefetch_enabled` | `bool` | `True` | — | If `False`, prefetch no-ops and `system_prompt_block` returns `""`. |
| `sync_write_queue_size` | `int` | `64` | — | Bounded write queue capacity. |
| `command_timeout_read_ms` | `int` | `2000` | — | Timeout for read-path `icm` calls. |
| `command_timeout_write_ms` | `int` | `5000` | — | Timeout for write-path `icm` calls. |
| `session_end_grace_ms` | `int` | `1500` | — | `on_session_end` drain window. |
| `periodic_progress_every_n_turns` | `int` | `20` | — | How often the periodic-progress trigger fires. |
| `consolidate_on_session_end` | `bool` | `False` | — | If `True`, fire `icm consolidate` on configured topics at session end. |

All ten use `secret: False` and `required: False`.

### Validation rules

- **int keys** (`recall_limit`, `sync_write_queue_size`, `command_timeout_read_ms`, `command_timeout_write_ms`, `session_end_grace_ms`, `periodic_progress_every_n_turns`):
  - Accept `int` directly, or a `str` parseable via `int(...)`.
  - Reject `bool` (since `bool` is a subclass of `int`, special-case to refuse `True`/`False` for int keys).
  - Range: must be `>= 0`. (`recall_limit` and queue size of `0` are degenerate but not invalid — let provider semantics handle the no-op case if they want. The spec only excludes negative values.) Actually: tighter guard. `recall_limit`, `sync_write_queue_size`, and the timeouts must be `>= 1` (zero would break recall and stall the worker). `periodic_progress_every_n_turns` must be `>= 1` to avoid div-by-zero. `session_end_grace_ms` may be `0` (means no grace; drop everything pending — supported by AD-16's "items remaining at deadline are dropped").
- **bool keys** (`prefetch_enabled`, `consolidate_on_session_end`):
  - Accept `bool` directly.
  - Accept the strings `"true"`/`"false"` (case-insensitive), coerce to `bool`.
  - Reject anything else.
- **string keys** (`topic_prefix`):
  - Accept any `str` (including empty `""`).
  - Reject non-`str`.
- **enum keys** (`default_importance`):
  - Must be a `str` exactly matching one of `("critical","high","medium","low")`.
- **Unknown keys**: pass through (forward-compat). Don't reject.

### Defensive copy semantics

`get_default_schema()` returns a **deep-ish copy** — the outer list and each inner dict are fresh copies so callers can mutate them without affecting `_SCHEMA_ENTRIES`. The `choices` list inside the enum entry is also copied. Easiest impl: `[dict(e) for e in _SCHEMA_ENTRIES]` plus `entry["choices"] = list(entry["choices"])` when present, OR a simple `copy.deepcopy(_SCHEMA_ENTRIES)`.

### `resolve_db_path` semantics

```python
def resolve_db_path(
    hermes_home: str | os.PathLike[str],
    profile: str | None = None,
) -> Path:
    base = Path(os.fspath(hermes_home)).expanduser().resolve()
    profile_name = profile if profile else _DEFAULT_PROFILE
    return base / "icm" / f"{profile_name}.db"
```

Notes:
- `os.fspath()` accepts both `str` and `os.PathLike` (AC8).
- `expanduser()` resolves `~` against the current `$HOME` (AC7 — `monkeypatch.setenv` works).
- `.resolve()` makes the path absolute even when the user passes a relative `hermes_home`.
- `profile or _DEFAULT_PROFILE` covers both `None` and `""` — empty profile names fall back to `"default"` (defensive; the spec doesn't strictly require it, but it prevents `"icm/.db"`).

### `mkdir_parent` semantics

```python
def mkdir_parent(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
```

Idempotent because `exist_ok=True`. AC9's "twice" assertion just confirms no second-call exception.

### Test fixtures (concrete strings)

- For `test_resolve_db_path_expands_tilde`: use `monkeypatch.setenv("HOME", str(tmp_path))` then call `resolve_db_path("~/foo")`; assert against `tmp_path / "foo" / "icm" / "default.db"`. Note: on Linux, `Path.resolve()` requires the path to exist for full resolution in *strict* mode but defaults to *non-strict* in 3.11+ — non-existent paths still resolve symbolically. Tests should work with `tmp_path` (which exists) for the home base.
- For `test_resolve_db_path_makes_parent_idempotent`: use `tmp_path` to construct a `db_path = tmp_path / "icm" / "default.db"`; call `mkdir_parent(db_path)` twice; assert `db_path.parent.is_dir()` and no exception.

### Common LLM-developer pitfalls (avoid)

- **Don't `import subprocess`.** Pure module (AD-12, S11 invariant test).
- **Don't `import logging`** in this module. Pure (AD-13). The provider logs config-related events.
- **Don't return mutable module-level state from `get_default_schema`.** Callers will mutate it. Defensive copy.
- **Don't accept `bool` as an int.** `True` is `1` in Python — the validator must explicitly reject `True`/`False` for int-typed keys.
- **Don't raise on garbage input.** AC4 requires graceful `(False, {"error": ...})`. Wrap the validator body in a try/except `Exception` at the outermost layer if needed (catch only at the boundary, AD-07 / §11.3).
- **Don't hardcode `"~/.hermes"`.** S11 invariant test will fail. All paths derive from `hermes_home` argument.
- **Don't drop unknown keys.** Pass them through `normalized_values` so future config keys (added in v1.x) are forward-compatible.
- **Don't conflate `0` with negative for `session_end_grace_ms`.** Zero is valid (means "drop instantly"); negative is not.

### Hard quality gates

1. `pytest tests/test_config.py --no-cov -q` → 10 pass, 0 fail.
2. `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → coverage gate passes; `config.py` line+branch ≥ 85 %.
3. `ruff check .` → 0 issues.
4. `mypy --strict hermes_icm_memory tests` → 0 errors.
5. All work committed on branch `s05`. Commits prefixed `docs(S05)/feat(S05)/review(S05)/simplify(S05)`.

### References

- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 2.2: Config schema + path resolution] — story spec, ACs, files-touched.
- [Source: _bmad-output/planning-artifacts/architecture.md#10. Configuration Surface] — schema table (§10.1) + validation rule (§10.2).
- [Source: _bmad-output/planning-artifacts/architecture.md#9. Profile Isolation] — `resolve_db_path` contract + `mkdir(parents=True, exist_ok=True)` semantics.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.1 Critical decisions / AD-05, AD-06] — profile isolation + idempotent mkdir, no `icm init`.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.2 Important decisions / AD-18] — config rejection returns `{"error": ...}`, never raises.
- [Source: _bmad-output/planning-artifacts/prd.md#FR2, FR6, FR7] — db path derivation + config schema + non-raising validation.

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (BMAD dev-story phase, S05).

### Debug Log References

- RED phase: `pytest tests/test_config.py --no-cov -q` → `ImportError: cannot import name 'config' from 'hermes_icm_memory'`. Tests correctly failed before any impl.
- GREEN phase: created `hermes_icm_memory/config.py` (`_SCHEMA_ENTRIES` + four public functions + `_coerce_int`/`_coerce_bool`/`_validate_one` helpers + `_INT_MIN` table). Re-ran pytest → 15/15 cases pass.
- Coverage: `config.py` **100 %** line+branch (79 stmts, 38 branches). Total package 98.38 %.
- ruff: `All checks passed!` after one auto-fix (import sort order in tests/test_config.py).
- mypy --strict: `Success: no issues found in 12 source files`.

### Completion Notes List

- All ten ACs satisfied. Coverage well above the 85 % gate (config.py = 100 %).
- Strict TDD followed: 15-case RED → impl → GREEN, no refactor needed.
- Pure module — no `subprocess`, no `logging`, no class. AD-12 / AD-13 / AD-17-style discipline honored. S11 invariant tests still green.
- Defensive copy via `copy.deepcopy` covers the nested `choices` list inside the enum entry — caller mutation is contained.
- `_coerce_int` explicitly rejects `bool` (Python quirk: `True` is `1`); test `test_validate_rejects_bool_for_int_key` locks this in.
- Unknown keys pass through to `normalized_values` (forward-compat) — `test_validate_passes_through_unknown_keys` locks this in.
- `resolve_db_path` accepts both `str` and `os.PathLike` via `os.fspath()`. `~` expansion verified via `monkeypatch.setenv("HOME", ...)`.
- `mkdir_parent` is the only filesystem-mutating call in the module; idempotent via `parents=True, exist_ok=True`.
- One `# pragma: no cover` on the `unknown schema type` defensive return path — unreachable from any current schema entry, no test data can construct it.

### File List

- `hermes_icm_memory/config.py` (NEW) — `get_default_schema`, `validate`, `resolve_db_path`, `mkdir_parent`.
- `tests/test_config.py` (NEW) — ten TDD tests covering AC1–AC10.

### Change Log

| Date       | Change                                                                                                          |
|------------|-----------------------------------------------------------------------------------------------------------------|
| 2026-05-06 | Story drafted (Phase 1 / `/bmad-create-story`): ACs, test plan, file spec, dev notes locked.                    |
| 2026-05-06 | Phase 2 dev-story: TDD RED → GREEN. 15 cases pass, config.py 100 % line+branch, ruff + mypy --strict clean.       |
| 2026-05-06 | Phase 3 code-review (Blind Hunter + Edge Case Hunter + Acceptance Auditor): Acceptance Auditor PASS — all ten ACs traced to a passing test. Blind Hunter clean: schema insertion order stable (3.7+ guarantee); deep-copy covers nested `choices` list; `_INT_MIN.get(key, 0)` default-floor never reached because every int key is in the table; `_coerce_bool` returns `None` on str-but-not-true-false (line 158) covered by test_validate_rejects_arbitrary_string_for_bool_key. Edge Case Hunter clean: empty profile → `"default"` (defensive but unspec'd; documented); `recall_limit=0` deliberately rejected (min=1; only `session_end_grace_ms=0` is permitted, supported by AD-16); tilde expansion against `$HOME` verified via `monkeypatch.setenv`; `os.PathLike` accepted via `os.fspath()`. **One LOW finding** for simplify: the `schema_by_key` dict is rebuilt inside `validate()` on every call; hoisting to a module-level constant saves 10 dict allocs per call without changing behavior. Status: review → Phase 4. |
