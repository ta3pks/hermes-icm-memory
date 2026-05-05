# Story 2.3: Trigger detection mapping

Status: in-progress
Story ID: S06 · Epic: 2 (ICM adapter core) · Effort: S · Dependencies: S01

## Story

As a Hermes user,
I want `sync_turn` to detect the five mandatory store triggers and emit ICM-shaped write tasks with the right topic + importance,
so that decisions, errors-resolved, preferences, context, and periodic progress are captured automatically (FR14, FR16, AD-17, AD-20).

## Acceptance Criteria

**AC1 — `MAPPING` dict shape (FR16)**

- **Given** `mapping.MAPPING`
- **When** read
- **Then** it is a `dict` with exactly five keys: `decisions`, `errors-resolved`, `preferences`, `context`, `learnings`. Each value is a `dict` (or equivalent typed structure) carrying:
  - `topic_template` — string with optional `{project}` placeholder.
  - `importance` — one of `critical / high / medium / low`.
- The matrix matches FR16 exactly:
  - `decisions` → `topic_template="decisions-{project}"`, `importance="high"`.
  - `errors-resolved` → `topic_template="errors-resolved"`, `importance="high"`.
  - `preferences` → `topic_template="preferences"`, `importance="critical"`.
  - `context` → `topic_template="context-{project}"`, `importance="high"`.
  - `learnings` → `topic_template="learnings"`, `importance="high"`.

**AC2 — `detect_triggers` errors-resolved pattern**

- **Given** `mapping.detect_triggers(user_text, assistant_text, project=None, turn_index=0, every_n_turns=20)`
- **When** the assistant text contains a fix-it pattern (e.g. `"fixed"`, `"resolved"`, `"the bug was"`, `"root cause"`, `"fix it"`)
- **Then** the result list contains a tuple `(topic="errors-resolved", importance="high", content=<assistant snippet>, keywords=[...])`.

**AC3 — `detect_triggers` decisions pattern**

- **Given** assistant text containing decision phrasing (`"decided to"`, `"going with"`, `"we'll use"`, `"let's use"`, `"chose to"`)
- **When** called with `project="hermes-icm-memory"`
- **Then** the result includes a tuple `(topic="decisions-hermes-icm-memory", importance="high", content=..., keywords=[...])`.

**AC4 — `detect_triggers` preferences pattern**

- **Given** user text containing preference phrasing (`"always use"`, `"never use"`, `"prefer"`, `"always do"`, `"never do"`)
- **When** called
- **Then** the result includes a tuple `(topic="preferences", importance="critical", content=<user snippet>, keywords=[...])`.

**AC5 — Periodic context emission (AD-20)**

- **Given** `turn_index == every_n_turns` (i.e. `turn_index % every_n_turns == 0` AND `turn_index > 0`)
- **When** called
- **Then** the result includes a periodic-progress tuple `(topic="context-<project or default>", importance="high", content=..., keywords=[...])`.
- **Given** `turn_index == 0`
- **When** called
- **Then** no periodic tuple is emitted (boundary check: zeroth turn is not periodic).

**AC6 — Empty result on no match**

- **Given** neutral user/assistant text and a non-periodic `turn_index`
- **When** called
- **Then** the result is `[]` (empty list, not `None`).

**AC7 — Multiple triggers from one turn (independence)**

- **Given** assistant text matching both errors-resolved AND decisions phrases in a single turn
- **When** called
- **Then** the result contains both tuples (independent triggers; one turn yields ≥1 ICM write tasks).

**AC8 — Default-project fallback**

- **Given** `project=None`
- **When** a periodic-context or decisions trigger fires
- **Then** the topic uses `"default"` as the project segment, i.e. `"context-default"` and `"decisions-default"` — never literal `"context-{project}"` or `"decisions-{project}"`.

**AC9 — Learnings pattern**

- **Given** assistant text containing learnings phrasing (`"learned"`, `"turns out"`, `"TIL"`, `"now I understand"`)
- **When** called
- **Then** the result includes a tuple `(topic="learnings", importance="high", content=..., keywords=[...])`.

## Tasks / Subtasks

> **TDD discipline (mandatory):** every code task is preceded by writing the failing test for it. Run `pytest tests/test_mapping.py -q --no-cov` after writing the tests but before any impl exists, confirm RED, then implement to GREEN.

- [ ] **Task 1 — Write failing tests first (AC1–AC9)**
  - [ ] 1.1 Create `tests/test_mapping.py` containing the nine tests from §Test Plan (verbatim names).
  - [ ] 1.2 Confirm `pytest tests/test_mapping.py --no-cov -q` reports nine collected, nine failed/errored (impl missing).

- [ ] **Task 2 — `mapping.py` implementation (AC1, AC2–AC9)**
  - [ ] 2.1 Create `hermes_icm_memory/mapping.py`.
  - [ ] 2.2 Define a `Trigger = tuple[str, str, str, list[str]]` type alias (topic, importance, content, keywords) for clarity. Or document the tuple shape in the docstring of `detect_triggers`.
  - [ ] 2.3 Define `MAPPING: dict[str, dict[str, str]]` literal with the five categories per AC1.
  - [ ] 2.4 Define `detect_triggers(user_text, assistant_text, project=None, turn_index=0, every_n_turns=20) -> list[Trigger]` per ACs.
    - Use compiled `re.Pattern` objects, case-insensitive, with `\b` word boundaries — keeps the heuristics readable and unit-testable.
    - Each category has its own pattern; iteration order is stable (use a list-of-tuples or rely on `dict` insertion order).
    - Periodic-context handling sits at the top of the function so the sequence of emitted tuples is deterministic across tests.
    - All public surface is type-hinted; pass `mypy --strict`.
  - [ ] 2.5 Re-run `pytest tests/test_mapping.py --no-cov -q`. All nine pass.

- [ ] **Task 3 — Quality gates**
  - [ ] 3.1 `ruff check .` → 0 issues.
  - [ ] 3.2 `mypy --strict hermes_icm_memory tests` → 0 errors.
  - [ ] 3.3 `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → passes; coverage on `mapping.py` ≥ 85 % (line + branch).
  - [ ] 3.4 `git status` clean → commit.

## File Spec

### `hermes_icm_memory/mapping.py` (NEW)

Contract sketch (do NOT copy verbatim — write to satisfy tests):

```python
"""Trigger detection for sync_turn (FR14, FR16, AD-17, AD-20).

Pure heuristics — no I/O, no logging, no dependencies on cli_runner / provider.
The MAPPING dict literal locks the FR16 matrix (category → topic, importance).
detect_triggers(...) is the single entry point hooks.sync_turn calls.
"""

from __future__ import annotations

import re
from typing import Final

# (topic, importance, content, keywords)
Trigger = tuple[str, str, str, list[str]]

MAPPING: Final[dict[str, dict[str, str]]] = {
    "decisions":       {"topic_template": "decisions-{project}",  "importance": "high"},
    "errors-resolved": {"topic_template": "errors-resolved",      "importance": "high"},
    "preferences":     {"topic_template": "preferences",          "importance": "critical"},
    "context":         {"topic_template": "context-{project}",    "importance": "high"},
    "learnings":       {"topic_template": "learnings",            "importance": "high"},
}

_DEFAULT_PROJECT: Final[str] = "default"

# Compiled patterns (case-insensitive). Word boundaries keep "fixedly" out.
_ERRORS_RESOLVED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(fixed|resolved|the bug was|root cause|fix(?:ed)? it)\b", re.IGNORECASE,
)
_DECISIONS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(decided to|going with|we'll use|let's use|chose to)\b", re.IGNORECASE,
)
_PREFERENCES_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(always (?:use|do)|never (?:use|do)|prefer)\b", re.IGNORECASE,
)
_LEARNINGS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(learned|turns out|TIL|now I understand)\b", re.IGNORECASE,
)


def _topic(category: str, project: str | None) -> str:
    template = MAPPING[category]["topic_template"]
    return template.format(project=project or _DEFAULT_PROJECT)


def _keywords(text: str, limit: int = 5) -> list[str]:
    """Return up to `limit` lower-cased word tokens, deduped, in first-seen order."""
    ...


def detect_triggers(
    user_text: str,
    assistant_text: str,
    project: str | None = None,
    turn_index: int = 0,
    every_n_turns: int = 20,
) -> list[Trigger]:
    """Pure heuristic. See module docstring + tests/test_mapping.py."""
    ...
```

### `tests/test_mapping.py` (NEW)

Test plan — exact names, mapped to ACs:

| #   | Test name                                              | AC mapping |
|-----|--------------------------------------------------------|------------|
| 1   | `test_mapping_dict_has_five_categories`                | AC1        |
| 2   | `test_mapping_topic_and_importance_for_each_category`  | AC1        |
| 3   | `test_detect_errors_resolved_pattern`                  | AC2        |
| 4   | `test_detect_decisions_pattern`                        | AC3        |
| 5   | `test_detect_preferences_critical`                     | AC4        |
| 6   | `test_detect_context_periodic`                         | AC5        |
| 7   | `test_detect_no_match_returns_empty`                   | AC6        |
| 8   | `test_detect_multiple_triggers_in_one_turn`            | AC7        |
| 9   | `test_topic_template_with_default_project`             | AC8        |

Note: AC9 (learnings) is covered by extending tests 3/8 with learnings phrases or a small dedicated assertion inside test 3-style block — follow whichever shape keeps the suite ≤ 9 tests as locked by epic spec, while still covering all ACs (test 8 is a natural place to verify learnings + decisions co-fire).

## Dev Notes

### Architecture compliance (must follow)

- **AD-02 (Python 3.11+):** `from __future__ import annotations`, `Final`, PEP 604 union syntax (`str | None`).
- **AD-12 (subprocess isolation):** `mapping.py` MUST NOT import `subprocess`. It is a pure function module.
- **AD-13 (logging namespace):** `mapping.py` does NOT log. Trigger-detection logs (INFO `"detected trigger: <category>"`) belong to S08 / `hooks.sync_turn` — that's where I/O context lives. Adding a logger here would introduce an untested branch and tightly couple a pure module to logging infrastructure.
- **AD-17 (mapping module is data-driven):** `MAPPING` is a literal dict; `detect_triggers` is a pure function. Both unit-testable in isolation.
- **AD-20 (periodic trigger every N turns, default 20):** `every_n_turns` parameter defaults to 20. The provider tracks `turn_index` per-session; this module just consumes it.
- **NFR-MAINT-1 (frozen public API):** `MAPPING`, the keys (`decisions`, `errors-resolved`, `preferences`, `context`, `learnings`), and the `detect_triggers` signature are public surface. Don't rename.
- **NFR-MAINT-3 (mypy --strict):** type-hint everything public.

### Pattern catalogue (seed; extend if a test demands it)

| Category          | Pattern (case-insensitive, `\b`-bounded)                                | Searched in        |
|-------------------|-------------------------------------------------------------------------|--------------------|
| errors-resolved   | `fixed | resolved | the bug was | root cause | fixed it / fix it`        | `assistant_text`   |
| decisions         | `decided to | going with | we'll use | let's use | chose to`             | `assistant_text`   |
| preferences       | `always use/do | never use/do | prefer`                                  | `user_text`        |
| learnings         | `learned | turns out | TIL | now I understand`                          | `assistant_text`   |
| context (period)  | (no pattern; fires when `turn_index % every_n_turns == 0 AND > 0`)       | (turn metadata)    |

Why search preferences in `user_text` only: preferences are user assertions about how *they* want to work. The assistant restating "you prefer bun" should not re-fire the trigger every turn the assistant mentions the preference.

### Trigger tuple ordering (deterministic for tests)

Order of emitted tuples in `detect_triggers` (when multiple match):

1. periodic context (if applicable) — first because it's metadata-driven, not text-driven.
2. errors-resolved.
3. decisions.
4. learnings.
5. preferences.

Rationale: the order is locked so AC7 ("multiple triggers in one turn") can use index-based assertions OR `set` comparisons; either works. Most tests should use `set`/membership checks on the `(topic, importance)` pair — robust to future reordering.

### Content + keywords extraction

- `content`: a short summary string. Simplest sound choice — pass through the matched text source (assistant_text for errors/decisions/learnings, user_text for preferences, a synthesized `"periodic progress: turn N"` for context). Truncate to 500 chars to keep ICM payloads lean.
- `keywords`: lowercased words from the matched text, deduped, first-seen order, limit 5. Used by `icm store -k`. The exact keyword extraction algorithm is **not** spec-locked beyond "must be `list[str]`"; pick the simplest implementation that passes tests and avoids logging dead code.

### Test fixtures (concrete strings)

Suggested literals:

- errors-resolved: `assistant_text = "Fixed the import error — root cause was the missing __init__.py."`
- decisions:      `assistant_text = "We decided to go with the bounded queue + drop-on-full strategy."`
- preferences:    `user_text = "I always use bun, never npm. Please prefer bun."`
- learnings:      `assistant_text = "Turns out subprocess.run with shell=False is the only safe form."`
- multiple:       `assistant_text = "Fixed the bug. We decided to go with bun instead of npm."`
- neutral:        `user_text = "What time is it?", assistant_text = "I don't know."`

### Common LLM-developer pitfalls (avoid)

- **Don't log inside `mapping.py`.** Pure module. Logging belongs in `hooks.sync_turn` (S08).
- **Don't add a class.** A module-level dict + module-level function is sufficient. AD-17 said "data-driven" — `MAPPING` is the data.
- **Don't import `subprocess`** here or anywhere outside `cli_runner.py` (AD-12, S11 invariant test).
- **Don't return `None` for empty result.** Return `[]`. The hooks loop expects a list.
- **Don't fire periodic context at `turn_index == 0`.** AC5 boundary explicitly: `> 0` requirement.
- **Don't make patterns greedy or unbounded.** `\b` word boundaries prevent `"fixedly"` from matching `"fixed"`.
- **Don't store the literal `{project}` in the topic.** AC8: substitute `"default"` if `project is None`.

### Hard quality gates

1. `pytest tests/test_mapping.py --no-cov -q` → 9 pass, 0 fail.
2. `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → coverage gate passes; `mapping.py` line+branch ≥ 85 %.
3. `ruff check .` → 0 issues.
4. `mypy --strict hermes_icm_memory tests` → 0 errors.
5. All work committed on branch `s06`. Commits prefixed `docs(S06)/feat(S06)/review(S06)/simplify(S06)`.

### References

- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 2.3: Trigger detection mapping] — story spec, ACs, files-touched.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.1 Critical decisions / AD-17] — mapping is data-driven.
- [Source: _bmad-output/planning-artifacts/architecture.md#3.2 Important decisions / AD-20] — periodic-progress every-N-turns.
- [Source: _bmad-output/planning-artifacts/architecture.md#5.2 Hook callbacks] — `sync_turn` consumes detect_triggers.
- [Source: _bmad-output/planning-artifacts/prd.md#FR14, FR16] — five mandatory triggers + topic↔importance matrix.

## Dev Agent Record

### Agent Model Used

_filled by dev phase_

### Debug Log References

_filled by dev phase_

### Completion Notes List

_filled by dev phase_

### File List

_filled by dev phase_

### Change Log

| Date       | Change                                                                                                          |
|------------|-----------------------------------------------------------------------------------------------------------------|
| 2026-05-06 | Story drafted (Phase 1 / `/bmad-create-story`): ACs, test plan, file spec, dev notes locked.                    |
