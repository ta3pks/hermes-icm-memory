# Story 1.1: Package skeleton and entry point

Status: ready-for-dev
Story ID: S01 · Epic: 1 (Plugin foundation) · Effort: M · Dependencies: none

## Story

As a maintainer,
I want the package skeleton in place with `pyproject.toml`, `plugin.yaml`, a stub `register(ctx)`, and a passing baseline test,
so that every subsequent story (S02–S14) has a CI-validated foundation to build on.

## Acceptance Criteria

**AC1 — Fresh-checkout install + baseline test**

- **Given** a fresh checkout
- **When** I run `pip install -e ".[dev]"` then `pytest`
- **Then** installation succeeds, the test suite runs, and `tests/test_plugin_loader.py` passes asserting `register(ctx)` calls `ctx.register_memory_provider` exactly once with an object that has `name == "icm"`.

**AC2 — Version single-source-of-truth**

- **Given** the package is installed
- **When** Python imports `hermes_icm_memory`
- **Then** `hermes_icm_memory.__version__` equals the value in `_version.py` and matches `pyproject.toml`'s version field (`0.1.0`).

**AC3 — `pyproject.toml` shape**

- **Given** `pyproject.toml`
- **When** examined
- **Then** it declares:
  - `requires-python = ">=3.11"`
  - `[project.entry-points."hermes_agent.plugins"]` with `hermes-icm-memory = "hermes_icm_memory:register"`
  - dev optional deps including: `pytest`, `pytest-cov`, `coverage`, `ruff`, `mypy`
  - pytest config `addopts` containing `--cov=hermes_icm_memory --cov-branch --cov-fail-under=85`
  - `[tool.ruff] target-version = "py311"`
  - `[tool.mypy] strict = true`

**AC4 — `plugin.yaml` shape**

- **Given** `plugin.yaml`
- **When** read
- **Then** it lists:
  - `name: hermes-icm-memory`
  - `version: 0.1.0`
  - `description:` (one-line description from PRD Executive Summary; see Dev Notes for canonical text)
  - `hooks:` containing exactly these four names: `prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`

## Tasks / Subtasks

> **TDD discipline (mandatory):** every code task is preceded by writing the failing test for it. Run `pytest tests/test_plugin_loader.py -q` after writing tests but before writing impl, and confirm all four FAIL. Only then write the impl.

- [ ] **Task 1 — Write failing tests first (AC1, AC2, AC4)**
  - [ ] 1.1 Create `tests/__init__.py` (empty file).
  - [ ] 1.2 Create `tests/conftest.py` (empty placeholder; later stories will add fixtures — keep file present so pytest collection is stable).
  - [ ] 1.3 Create `tests/test_plugin_loader.py` containing the four tests from §Test Plan (verbatim names). Use `unittest.mock.MagicMock` for the fake `ctx`. Use `tomllib` (stdlib, 3.11+) to parse `pyproject.toml`. Use `pathlib.Path(__file__).resolve().parent.parent` to locate the repo root from the test file. For YAML, parse via `yaml.safe_load(...)` — add `pyyaml` to `[project.optional-dependencies].dev`.
  - [ ] 1.4 Confirm `pytest tests/test_plugin_loader.py -q` reports 4 collected, 4 failed (impl missing).

- [ ] **Task 2 — Minimal `pyproject.toml` enabling install (AC3)**
  - [ ] 2.1 Create `pyproject.toml` per §File Spec → `pyproject.toml`.
  - [ ] 2.2 In a fresh venv: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`. If `pip` missing, fall back to `python3 -m pip install -e ".[dev]"`.
  - [ ] 2.3 Verify `python -c "import hermes_icm_memory"` raises `ModuleNotFoundError` (the package dir doesn't exist yet — this is correct).

- [ ] **Task 3 — Implementation files (AC1, AC2, AC4)**
  - [ ] 3.1 Create `hermes_icm_memory/_version.py` with `__version__ = "0.1.0"`.
  - [ ] 3.2 Create `hermes_icm_memory/__init__.py` with the stub `register(ctx)` per §File Spec.
  - [ ] 3.3 Create `plugin.yaml` per §File Spec.
  - [ ] 3.4 Re-run `pytest tests/test_plugin_loader.py -q`. All four pass.

- [ ] **Task 4 — Quality gates (all four ACs)**
  - [ ] 4.1 `ruff check .` → 0 issues.
  - [ ] 4.2 `mypy --strict hermes_icm_memory tests` → 0 errors.
  - [ ] 4.3 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → passes (coverage will be 100 % at this story's scope; the gate only enforces the floor).
  - [ ] 4.4 `git status` clean → commit.

## File Spec (authoritative — copy-paste boilerplate)

### `pyproject.toml` (NEW)

```toml
[project]
name = "hermes-icm-memory"
version = "0.1.0"
description = "Hermes Agent memory provider plugin backed by ICM (Infinite Context Memory) — semantic, cross-session, cross-editor recall via the local icm CLI."
readme = "README.md"
license = { file = "LICENSE" }
authors = [{ name = "Nikos Efthias" }]
requires-python = ">=3.11"
dependencies = [
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "coverage>=7.4",
    "ruff>=0.5",
    "mypy>=1.10",
    "types-pyyaml",
]

[project.entry-points."hermes_agent.plugins"]
hermes-icm-memory = "hermes_icm_memory:register"

[project.urls]
Homepage = "https://github.com/ta3pks/hermes-icm-memory"
Repository = "https://github.com/ta3pks/hermes-icm-memory"
Issues = "https://github.com/ta3pks/hermes-icm-memory/issues"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["hermes_icm_memory*"]
exclude = ["tests*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --cov=hermes_icm_memory --cov-branch --cov-fail-under=85"
pythonpath = ["."]

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "SIM"]

[tool.mypy]
strict = true
python_version = "3.11"
files = ["hermes_icm_memory", "tests"]

[tool.coverage.run]
branch = true
source = ["hermes_icm_memory"]
```

> **Note on `dependencies`:** `pyyaml` is included because `plugin.yaml` is parsed in `test_plugin_loader.py` (and later by Hermes itself when it loads the plugin manifest). `tomllib` is stdlib in 3.11+ so no dep is needed for the version-consistency test.

### `plugin.yaml` (NEW)

```yaml
name: hermes-icm-memory
version: 0.1.0
description: "Hermes Agent memory provider plugin backed by ICM (Infinite Context Memory) — semantic, cross-session, cross-editor recall via the local icm CLI."
author: nikos efthias
hooks:
  - prefetch
  - system_prompt_block
  - sync_turn
  - on_session_end
```

> **Field-name note:** the epic spec and AC4 require the key `hooks` (not `provides_hooks` as the `hermes-rtk-hook` reference uses). Follow the epic spec; do NOT rename to `provides_hooks`. (Hermes parses both, but our test asserts `hooks`.)

### `hermes_icm_memory/_version.py` (NEW)

```python
"""Single source of truth for the package version.

Imported by hermes_icm_memory.__init__ and asserted equal to pyproject.toml's
[project].version by tests/test_plugin_loader.py::test_version_is_consistent.
"""

__version__ = "0.1.0"
```

### `hermes_icm_memory/__init__.py` (NEW — stub for S01; S10 will replace with the real `IcmMemoryProvider`)

```python
"""hermes-icm-memory — Hermes Agent memory provider plugin backed by ICM.

Hermes calls `register(ctx)` after loading plugin.yaml. For S01 we register
a placeholder provider so the entry-point + plugin-manifest plumbing is
exercised end-to-end with a passing baseline test. S10 replaces _StubProvider
with the real IcmMemoryProvider from provider.py.
"""

from __future__ import annotations

from typing import Any

from ._version import __version__

__all__ = ["__version__", "register"]


class _StubProvider:
    """Placeholder memory provider. Replaced in S10 by IcmMemoryProvider."""

    name = "icm"


def register(ctx: Any) -> None:
    """Plugin entry point invoked by Hermes after loading plugin.yaml.

    Constructs a memory provider and registers it with the Hermes context
    exactly once. S01 ships a stub; S10 swaps in the real provider.
    """
    provider = _StubProvider()
    ctx.register_memory_provider(provider)
```

> **Why `Any` for `ctx`:** Hermes does not publish a typed `Context` protocol on PyPI. Using `Any` keeps `mypy --strict` happy without depending on `agent.*` import-time. S10 may tighten this if a typing stub becomes available.

### `tests/__init__.py` (NEW — empty)

```python
```

### `tests/conftest.py` (NEW — empty placeholder for shared fixtures)

```python
"""Shared pytest fixtures. Empty in S01; populated by later stories
(tmp_hermes_home, mock_icm_subprocess, real_icm_db, capture_logs)."""
```

### `tests/test_plugin_loader.py` (NEW)

```python
"""Plugin loader baseline tests (S01).

These are the only tests that exist after S01. They prove:
  - register(ctx) wires a provider into Hermes correctly,
  - the registered provider is the icm provider,
  - version is single-source-of-truth across __init__/_version/pyproject,
  - plugin.yaml has the manifest shape Hermes (and S10) expect.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import yaml

import hermes_icm_memory
from hermes_icm_memory import _version

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_register_calls_register_memory_provider_once() -> None:
    """register(ctx) must call ctx.register_memory_provider exactly once."""
    ctx = MagicMock()
    hermes_icm_memory.register(ctx)
    assert ctx.register_memory_provider.call_count == 1


def test_registered_provider_name_is_icm() -> None:
    """The registered provider's name attribute must equal 'icm'."""
    ctx = MagicMock()
    hermes_icm_memory.register(ctx)
    (provider,) = ctx.register_memory_provider.call_args.args
    assert provider.name == "icm"


def test_version_is_consistent() -> None:
    """__version__ must match _version.__version__ and pyproject.toml's version."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    pyproject_version = pyproject["project"]["version"]
    assert hermes_icm_memory.__version__ == _version.__version__
    assert hermes_icm_memory.__version__ == pyproject_version


def test_plugin_yaml_shape() -> None:
    """plugin.yaml must declare name/version/description/hooks with the four expected hooks."""
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())
    for key in ("name", "version", "description", "hooks"):
        assert key in manifest, f"plugin.yaml missing required key: {key}"
    assert manifest["name"] == "hermes-icm-memory"
    assert manifest["version"] == hermes_icm_memory.__version__
    assert set(manifest["hooks"]) == {
        "prefetch",
        "system_prompt_block",
        "sync_turn",
        "on_session_end",
    }
```

> **Why `tomllib` (no extra dep):** stdlib in 3.11+; matches AD-02 (Python 3.11 minimum).
> **Why `MagicMock` not a hand-rolled fake:** `call_count` and `call_args.args` give us both AC1 and AC2 in two lines per test — no maintenance surface in a stub.
> **Why the explicit `import sys` line if unused:** it isn't — remove it. (Mypy `--strict` + ruff `F401` will catch it; if your IDE auto-inserts it, just delete.)

## Dev Notes

### Architecture compliance (must follow)

- **AD-02 (Python 3.11+):** use `tomllib` (stdlib), `from __future__ import annotations`, `Self` / `Any` typing freely. Do not add 3.10 fallbacks.
- **AD-12 (subprocess isolation):** S01 ships **no** `subprocess` import anywhere — `__init__.py` is just `register(ctx)`, nothing more. The S11 test that polices "only `cli_runner.py` imports `subprocess`" will pass trivially because `cli_runner.py` doesn't exist yet (S04 adds it).
- **AD-13 (logging namespace `hermes_icm_memory`):** S01 has nothing to log. Do not add a `logging.getLogger(...)` call — YAGNI; S04+ introduce it where used.
- **NFR-MAINT-1 (frozen public API):** `register(ctx)`, the entry-point string `hermes_icm_memory:register`, and the package import name `hermes_icm_memory` are public surface from this story onward. Don't rename later.
- **AC4 specifically requires `hooks` as the manifest key**, even though the reference scaffold (`hermes-rtk-hook/plugin.yaml`) uses `provides_hooks`. Follow AC4.

### Reference scaffold (file shape only, NOT semantics)

Path: `/home/nikos/.hermes/plugins/hermes-rtk-hook/`. That plugin is a *hook plugin* (different plugin type from a memory provider). Mirror its **layout** (`plugin.yaml`, `pyproject.toml`, `__init__.py` exposing `register(ctx)`, a `tests/` dir alongside source). Do not copy its `provides_hooks` key, `requires-python = ">=3.10"`, or `[tool.setuptools].py-modules` (we are a package, not a flat module set).

### File-conflict awareness for downstream stories

- **S10 modifies `hermes_icm_memory/__init__.py`** — replaces `_StubProvider` with `IcmMemoryProvider` from `provider.py`. S01 → S10 are sequential by the file-conflict matrix (epics-and-stories.md §"Story-to-story File Conflict Matrix"). Don't pre-empt S10 — the stub is the contract.
- **S02 (CI workflow)**, **S03 (README/CONTRIBUTING)**, and **S04–S09** depend on S01 but do not modify any S01 files; they only add new ones.

### Test plan (TDD; tests-first; mirrors AC1/AC2/AC4 1:1)

| #   | Test name                                              | Assertion                                                                                                       | AC mapping |
|-----|--------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|------------|
| 1   | `test_register_calls_register_memory_provider_once`    | `ctx.register_memory_provider.call_count == 1` after `register(ctx)`.                                            | AC1        |
| 2   | `test_registered_provider_name_is_icm`                 | Captured argument's `.name == "icm"`.                                                                            | AC1        |
| 3   | `test_version_is_consistent`                           | `hermes_icm_memory.__version__ == _version.__version__ == pyproject["project"]["version"]` (parsed via tomllib). | AC2        |
| 4   | `test_plugin_yaml_shape`                               | `name`/`version`/`description`/`hooks` keys present; `hooks` set equals the four expected names.                 | AC4        |

> AC3 (pyproject.toml shape) is implicitly enforced: if `pyproject.toml` doesn't declare the entry point or coverage gate correctly, `pip install -e ".[dev]"` or `pytest` will fail before tests even run, and the developer will see it.

### Hard quality gates (must all pass before story is "done")

1. `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → 0 failures, coverage ≥ 85 %.
2. `ruff check .` → 0 issues.
3. `mypy --strict hermes_icm_memory tests` → 0 errors.
4. `pip install -e ".[dev]"` works in a fresh venv.
5. `git status` clean (everything committed).
6. All work committed on `main` (lone-wave story; no worktree, no branch).

### Common LLM-developer pitfalls (avoid)

- **Don't pre-implement `IcmMemoryProvider`.** That's S07. S01 ships a `_StubProvider` with `.name = "icm"` and nothing else. Resist the urge.
- **Don't add `subprocess` calls.** S04 introduces the only file allowed to import `subprocess`.
- **Don't add `logging.getLogger(...)` to `__init__.py`.** Nothing logs in S01. Adding it adds an untested branch and risks dropping coverage below 85 %.
- **Don't use `from typing import TYPE_CHECKING` to import a Hermes `Context` type.** Hermes doesn't publish one. Type `ctx` as `Any`.
- **Don't add `__init__.py` to the *project root*.** Tests live in `tests/`; the package lives in `hermes_icm_memory/`. The root has neither.
- **Don't include unused imports.** `mypy --strict` will pass on them but `ruff check .` (rule F401) will fail. The boilerplate above intentionally omits them.
- **Don't bump the version yet.** `0.1.0` is correct; S10 / release stories handle bumps.
- **Don't call `icm init` from `register(ctx)`.** AD-06: the plugin never calls `icm init`. The DB auto-creates on first store/recall against `--db <path>`.

### Rationale for design choices already locked

- **`pyyaml` as a runtime dep, not just dev:** Hermes loads `plugin.yaml` itself, but at test time we parse it from inside our own test suite. Easier to keep one YAML library across runtime + tests. (Reference scaffold doesn't ship YAML because it has no test that loads its own manifest.)
- **`pythonpath = ["."]` in pytest config:** lets `tests/test_plugin_loader.py` find `hermes_icm_memory` even if the `pip install -e .` step is skipped during local quick-iteration. Matches reference scaffold.
- **`[tool.setuptools.packages.find]` (not `py-modules`):** we are a package (a directory with `__init__.py`), not a flat module set. The reference scaffold is the latter; we deviate intentionally.

### Project Structure Notes

After this story the tree looks like:

```
hermes-icm-memory/
├── _bmad/                     # bmad config (gitignored)
├── _bmad-output/              # planning + implementation artifacts (kept in repo)
├── docs/                      # repo docs (pre-existing)
├── hermes_icm_memory/         # NEW — package dir
│   ├── __init__.py            # NEW — register(ctx) stub
│   └── _version.py            # NEW — __version__ = "0.1.0"
├── tests/                     # NEW — pytest tree
│   ├── __init__.py            # NEW — empty
│   ├── conftest.py            # NEW — empty placeholder
│   └── test_plugin_loader.py  # NEW — four baseline tests
├── plugin.yaml                # NEW — Hermes manifest
├── pyproject.toml             # NEW — PEP 621 metadata + tool config
├── LICENSE                    # pre-existing (Apache-2.0)
├── README.md                  # pre-existing placeholder (S03 replaces)
└── .gitignore                 # pre-existing
```

No conflicts with the existing tree; everything is greenfield additions. The `.venv/` directory created during install is already covered by `.gitignore` (verify: it should match `*.venv*` or `.venv/`). If not, the dev should add `.venv/` to `.gitignore` as part of this story.

### References

- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 1.1: Package skeleton and entry point] — story spec, ACs, test plan, files-touched.
- [Source: _bmad-output/planning-artifacts/architecture.md#4. Component Map] — package layout (`hermes_icm_memory/__init__.py`, `_version.py`).
- [Source: _bmad-output/planning-artifacts/architecture.md#2.2 Tooling choices] — setuptools/pytest/pytest-cov/ruff/mypy version floors.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.1 Critical decisions] — AD-02 (Python 3.11+), AD-12 (subprocess isolation), AD-13 (logging namespace).
- [Source: _bmad-output/planning-artifacts/prd.md#Executive Summary] — canonical one-line description for `plugin.yaml` and `pyproject.toml`.
- [Source: _bmad-output/planning-artifacts/product-brief.md#Success Criteria] — coverage ≥ 85 %, ruff/mypy clean, Apache-2.0 license.
- [Reference scaffold (file shape only, different plugin type): /home/nikos/.hermes/plugins/hermes-rtk-hook/]

## Dev Agent Record

### Agent Model Used

Claude Opus (BMAD dev-story phase)

### Debug Log References

(Populated during dev-story execution.)

### Completion Notes List

(Populated during dev-story execution.)

### File List

Expected after this story (will be confirmed during dev-story):

- `pyproject.toml` (NEW)
- `plugin.yaml` (NEW)
- `hermes_icm_memory/__init__.py` (NEW)
- `hermes_icm_memory/_version.py` (NEW)
- `tests/__init__.py` (NEW)
- `tests/conftest.py` (NEW)
- `tests/test_plugin_loader.py` (NEW)
