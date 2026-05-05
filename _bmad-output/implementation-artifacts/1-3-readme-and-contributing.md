# Story 1.3: README quickstart and CONTRIBUTING

Status: review
Story ID: S03 · Epic: 1 (Plugin foundation) · Effort: S · Dependencies: S01

## Story

As a new user,
I want a `README.md` with a 3-step quickstart and a `CONTRIBUTING.md` with the dev-loop commands,
so that I can install + verify in under 5 minutes (PRD SM8) and a PR contributor knows how to run the gates locally.

## Acceptance Criteria

**AC1 — README quickstart shape**

- **Given** the new `README.md`
- **When** read
- **Then** it contains:
  - a project tagline,
  - a `## Quickstart` section with a 3-step list (verify `icm` on PATH, install the package, enable + activate via Hermes),
  - the literal commands `pip install hermes-icm-memory`, `hermes plugins enable hermes-icm-memory`, `hermes memory setup icm`,
  - a feature bullet list aligned with the Brief's "What Makes This Different",
  - links to `https://github.com/rtk-ai/icm` and the Hermes plugin docs URL `https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins`,
  - a note that, until the first PyPI release, install is via `pip install git+https://github.com/ta3pks/hermes-icm-memory.git`.

**AC2 — CONTRIBUTING dev-loop**

- **Given** the new `CONTRIBUTING.md`
- **When** read
- **Then** it documents:
  - dev install via `pip install -e ".[dev]"`,
  - lint via `ruff check .`,
  - type-check via `mypy --strict hermes_icm_memory`,
  - tests via `pytest`,
  - the **85 %** coverage threshold,
  - the TDD-required policy (write failing tests first),
  - commit message style: short imperative; **no `Co-Authored-By` line**.

**AC3 — Tests pass + gates green**

- **Given** the three new tests in `tests/test_docs.py`
- **When** the suite runs
- **Then** all three pass, the existing suite still passes, branch coverage stays ≥ 85 %, `ruff check .` is clean, and `mypy --strict hermes_icm_memory` is clean.

## Tasks / Subtasks

> **TDD discipline (mandatory):** write the three failing tests in `tests/test_docs.py` before authoring the docs. Run `pytest tests/test_docs.py -q` and confirm 3 collected, 3 failed (no docs of the right shape exist yet — the placeholder `README.md` from S01 lacks `## Quickstart` and the upstream links).

- [x] **Task 1 — Write failing tests first (AC1, AC2)**
  - [x] 1.1 Create `tests/test_docs.py` with the three tests from §Test Plan (verbatim names).
  - [x] 1.2 Confirm `pytest tests/test_docs.py -q` reports 3 collected, 3 failed.

- [x] **Task 2 — Author `README.md` (REPLACES placeholder) (AC1)**
  - [x] 2.1 Replace the placeholder `README.md` from S01 with the canonical quickstart README per §File Spec → `README.md`.
  - [x] 2.2 Re-run `pytest tests/test_docs.py::test_readme_has_quickstart tests/test_docs.py::test_readme_links_upstreams -q`. Both pass.

- [x] **Task 3 — Author `CONTRIBUTING.md` (NEW) (AC2)**
  - [x] 3.1 Create `CONTRIBUTING.md` per §File Spec → `CONTRIBUTING.md`.
  - [x] 3.2 Re-run `pytest tests/test_docs.py::test_contributing_has_dev_loop -q`. Passes.

- [x] **Task 4 — Quality gates (all ACs)**
  - [x] 4.1 `ruff check .` → 0 issues.
  - [x] 4.2 `mypy --strict hermes_icm_memory` → 0 errors.
  - [x] 4.3 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → passes.
  - [x] 4.4 `git status` clean → commit.

## File Spec (authoritative)

### `tests/test_docs.py` (NEW)

Three pure read-and-assert tests over the repo's docs. No subprocess, no network.

- `test_readme_has_quickstart` — read `README.md`, assert it contains the heading `"## Quickstart"` and references the three install commands.
- `test_contributing_has_dev_loop` — read `CONTRIBUTING.md`, assert it mentions `ruff check`, `mypy`, `pytest`, and the `85` coverage threshold.
- `test_readme_links_upstreams` — read `README.md`, assert it contains `https://github.com/rtk-ai/icm` and the Hermes plugin docs URL.

### `README.md` (REPLACES placeholder)

Sections (in order):

1. Title + one-line tagline.
2. Why (one short paragraph from Brief §The Problem / §The Solution).
3. `## Quickstart` (3 numbered steps with verifiable output line).
4. `## Features` (bulleted; mirrors Brief §What Makes This Different).
5. `## Configuration` (link to `architecture.md` §10.1).
6. `## Development` (link to `CONTRIBUTING.md`).
7. `## License` (Apache-2.0).
8. `## Links` (ICM upstream + Hermes plugin docs + Hermes memory-providers docs).

### `CONTRIBUTING.md` (NEW)

Sections: dev install, dev loop (lint / type-check / tests), coverage gate, TDD policy, commit message style.

## Test Plan

| # | Test                                              | Asserts                                                                                       |
|---|---------------------------------------------------|-----------------------------------------------------------------------------------------------|
| 1 | `test_readme_has_quickstart`                      | `## Quickstart` + `pip install hermes-icm-memory` + `hermes plugins enable hermes-icm-memory` + `hermes memory setup icm` |
| 2 | `test_contributing_has_dev_loop`                  | `ruff check` + `mypy` + `pytest` + `85`                                                       |
| 3 | `test_readme_links_upstreams`                     | `https://github.com/rtk-ai/icm` + `https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins` |

## Dev Notes

- **License** locked to Apache-2.0 (Brief).
- **Python** 3.11+ (PRD).
- **Until first PyPI release**, README must mention the `pip install git+https://github.com/ta3pks/hermes-icm-memory.git` fallback.
- **No emojis in committed files** per Nikos's global preferences.
- **No `Co-Authored-By` lines** in any commit on this branch.

## Change Log

- 2026-05-06 — Story created, tests authored (TDD), README + CONTRIBUTING authored, 4-phase chain run.
- 2026-05-06 — Code review: pass (no findings of substance, see SendMessage summary).
- 2026-05-06 — Simplify: pass (no-op — diff is markdown + 3 read-only tests; nothing to dedupe).
