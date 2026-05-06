# Story 4.2: LLM-facing tools (`icm_recall`, `icm_store`, `icm_topics`, `icm_health`)

Status: in-progress
Story ID: S09 · Epic: 4 (Tool surface) · Effort: M · Dependencies: S04 (cli_runner, errors), S05 (config), S07 (provider — supplies `_db_path`, `_config`, `_write_queue`)

## Story

As an LLM running inside Hermes,
I want four tools (`icm_recall`, `icm_store`, `icm_topics`, `icm_health`) that I can call from inside a turn,
So that I can drive memory operations explicitly when heuristics alone aren't enough — closing FR8 (recall), FR11 (topics), FR13 (store), and FR17 (health) and satisfying the AD-10 / FR19 contract that **every handler returns `json.dumps(...)`, never a dict, and never propagates an exception into the agent turn**.

## Acceptance Criteria

**AC1 — `get_tool_schemas()` returns four schemas with the canonical names**

- **Given** an `IcmMemoryProvider()` instance
- **When** `provider.get_tool_schemas()` is called
- **Then** the result is a list of length 4. The `name` of each entry is exactly one of `icm_recall`, `icm_store`, `icm_topics`, `icm_health` (no other names admitted, no duplicates). Order matches the PRD §8.6 table.

**AC2 — every schema has `name`, `description`, `parameters` with `type`/`properties`/`required`**

- **Given** any of the four schemas from AC1
- **When** inspected
- **Then** it has the keys `name` (str), `description` (str, non-empty), and `parameters` (dict). The `parameters` dict has `type == "object"`, a `properties` mapping, and a `required` list. The `properties` shape matches PRD §8.6: `icm_recall.required = ["query"]`; `icm_store.required = ["topic", "content"]`; `icm_topics.required = []`; `icm_health.required = []`.

**AC3 — `icm_recall` returns `json.dumps({"hits": [...]})` on success**

- **Given** `cli_runner.run_recall(...)` returns a parsed list of hit dicts
- **When** `provider.handle_tool_call("icm_recall", {"query": "what does Nikos prefer for package managers?"})` is invoked
- **Then** the return is a string equal to `json.dumps({"hits": <list>})`. `isinstance(result, str)` is `True`.

**AC4 — `icm_recall` degrades to `json.dumps({"hits": []})` + WARNING log on any cli_runner failure**

- **Given** `cli_runner.run_recall(...)` raises `ICMNotFoundError` / `ICMTimeoutError` / `ICMNonZeroExitError` / `ICMMalformedOutputError`
- **When** the handler is invoked
- **Then** it returns `json.dumps({"hits": []})` and emits exactly one `logging.WARNING` record on the `hermes_icm_memory.tools` logger. No exception escapes.

**AC5 — `icm_store` enqueues a write task and returns immediately**

- **Given** `provider._write_queue` (the bounded `queue.Queue` owned by S08) is wired to a queue with capacity ≥ 1
- **When** `provider.handle_tool_call("icm_store", {"topic": "preferences", "content": "Always use bun"})` is invoked
- **Then** exactly one task is appended via `_write_queue.put_nowait(...)`; the return is `json.dumps({"accepted": True, "queued_at": "<iso8601>"})` (the `queued_at` value parses with `datetime.fromisoformat`); the call's wall-clock time is < 5 ms p95 across 200 invocations.

**AC6 — `icm_store` shape of the queued task: `(topic, importance, content, keywords_list)`**

- **Given** `_write_queue.put_nowait(...)` is called by `icm_store`
- **When** the queued tuple is inspected
- **Then** it is a 4-tuple `(topic: str, importance: str, content: str, keywords: list[str])`. The `importance` field defaults to the provider's `default_importance` config or `"high"` when neither caller arg nor config supplies one. `keywords` defaults to `[]`.

**AC7 — `icm_store` rejects missing required args with `{"error": "..."}` JSON**

- **Given** `provider.handle_tool_call("icm_store", {"content": "hi"})` (missing `topic`) — or `{"topic": "x"}` (missing `content`)
- **When** invoked
- **Then** the return is `json.dumps({"error": "<msg naming the missing key>"})`. **No item is enqueued.** The handler does not raise.

**AC8 — `icm_topics` returns `json.dumps({"topics": [...]})` on success**

- **Given** `cli_runner.run_topics(...)` returns a parsed list of topic dicts
- **When** `provider.handle_tool_call("icm_topics", {})` is invoked
- **Then** the return is `json.dumps({"topics": <list>})`.

**AC9 — `icm_topics` degrades to `json.dumps({"topics": []})` on cli_runner failure**

- **Given** `cli_runner.run_topics(...)` raises any `ICMError` subtype
- **When** the handler is invoked
- **Then** the return is `json.dumps({"topics": []})` and exactly one WARNING log is emitted. No exception escapes.

**AC10 — `icm_health` (no topic) returns `json.dumps({"report": {...}})`**

- **Given** `cli_runner.run_health(db_path, timeout_ms)` returns a parsed health dict
- **When** `provider.handle_tool_call("icm_health", {})` is invoked
- **Then** the return is `json.dumps({"report": <dict>})`.

**AC11 — `icm_health` accepts an optional `topic` arg and forwards it**

- **Given** `provider.handle_tool_call("icm_health", {"topic": "preferences"})`
- **When** invoked
- **Then** `cli_runner.run_health` is called with `topic="preferences"` and the return is `json.dumps({"report": <dict>})`.

**AC12 — `icm_health` degrades to `json.dumps({"report": {}})` on failure**

- **Given** `cli_runner.run_health(...)` raises any `ICMError` subtype
- **When** invoked
- **Then** the return is `json.dumps({"report": {}})` plus one WARNING log. No exception escapes.

**AC13 — Unknown tool name returns `json.dumps({"error": "unknown tool: ..."})`**

- **Given** `provider.handle_tool_call("anything-at-all", {})`
- **When** invoked
- **Then** the return is `json.dumps({"error": "unknown tool: anything-at-all"})` (or equivalent message that names the offending name). The dispatch never raises.

**AC14 — Every handler return is `str` (`isinstance(result, str)` is True)**

- **Given** every code path through every handler (success, validation failure, cli_runner failure, unknown name)
- **When** the result is asserted with `isinstance(result, str)`
- **Then** the assertion passes for every return. No handler ever returns a `dict`.

**AC15 — No exception escapes any handler boundary**

- **Given** every code path through every handler — including pathological args (`None`, wrong type, deeply nested junk)
- **When** invoked
- **Then** no exception escapes. (FR19 / NFR-REL-1 / AD-07.)

**AC16 — `tools.py` does not import `subprocess` (AD-12)**

- **Given** `hermes_icm_memory/tools.py`
- **When** parsed by `tests/test_no_subprocess_outside_cli_runner.py`
- **Then** the test still passes (no `import subprocess`, no `from subprocess import …`).

## Tasks / Subtasks

- [x] **Task 1 — Story spec (Phase 1 / `/bmad-create-story`)**
  - 16 ACs, 16-test plan, file spec, dev notes locked.
- [ ] **Task 2 — Phase 2 / `/bmad-dev-story` (TDD)**
  - RED: write `tests/test_tools.py` with 16 cases; modify the two AC-13/AC-14 cases in `tests/test_provider.py` to align with the now-real dispatch.
  - GREEN: implement `hermes_icm_memory/tools.py`; rewrite `provider.get_tool_schemas` and `provider.handle_tool_call` to delegate.
- [ ] **Task 3 — Phase 3 / `/bmad-code-review`**
  - Adversarial pass (Blind Hunter + Edge Case Hunter + Acceptance Auditor).
- [ ] **Task 4 — Phase 4 / `/simplify`**
  - Reuse / quality / efficiency review on the new code.

## File Spec

### `hermes_icm_memory/tools.py` (NEW)

Public surface (pure dispatch — provider passes itself in for state access):

```python
from __future__ import annotations

import datetime as _dt
import json
import logging
import queue
from typing import TYPE_CHECKING, Any

from .cli_runner import run_health, run_recall, run_topics
from .errors import ICMError

if TYPE_CHECKING:
    from .provider import IcmMemoryProvider

logger = logging.getLogger(__name__)

def get_tool_schemas() -> list[dict[str, Any]]: ...
def handle_tool_call(provider: "IcmMemoryProvider", name: str, args: dict[str, Any]) -> str: ...
```

Module-level constants:

- `_TOOL_SCHEMAS: Final[list[dict[str, Any]]]` — the four schemas, frozen at import time. `get_tool_schemas` returns a deep copy so callers can't mutate the source.
- `_DEFAULT_IMPORTANCE: Final[str] = "high"` — fallback when both arg and config omit it.
- `_DEFAULT_RECALL_LIMIT: Final[int] = 5` — fallback when caller omits `limit`.
- `_DEFAULT_READ_TIMEOUT_MS: Final[int] = 2000` — fallback (provider config wins).

Handlers (private, dispatched by name):

- `_handle_recall(provider, args) -> str`
- `_handle_store(provider, args) -> str`
- `_handle_topics(provider, args) -> str`
- `_handle_health(provider, args) -> str`

### `tests/test_tools.py` (NEW)

16 TDD cases (one per behaviour AC1–AC16, except AC16 which is enforced by the existing S11 AST test):

1. `test_get_tool_schemas_has_four_with_correct_names` — names are exactly `icm_recall`, `icm_store`, `icm_topics`, `icm_health`; no duplicates; list length 4 (AC1).
2. `test_each_schema_has_required_keys` — every schema has `name`, `description`, `parameters{type,properties,required}` (AC2).
3. `test_recall_returns_json_string_with_hits_key` — `cli_runner.run_recall` mocked → list of hits; assert handler returns `json.dumps({"hits": [...]})` (AC3).
4. `test_recall_failure_returns_empty_hits_and_warns` — `cli_runner.run_recall` raises `ICMTimeoutError`; handler returns `json.dumps({"hits": []})`; one WARNING captured (AC4).
5. `test_store_enqueues_and_returns_immediately` — provider `_write_queue` mocked; handler enqueues exactly one `(topic, importance, content, keywords_list)` task; returns `json.dumps({"accepted": True, "queued_at": "<iso>"})` (AC5, AC6).
6. `test_store_p95_under_5ms` — call handler 200×; assert sorted-95th-percentile elapsed < 5 ms (AC5 latency clause).
7. `test_store_returns_accepted_true_with_iso_timestamp` — parse `queued_at` with `datetime.fromisoformat`; assert no `ValueError` (AC5).
8. `test_store_invalid_args_returns_error_json` — missing `topic` (and separately missing `content`); handler returns `json.dumps({"error": "..."})`; **no enqueue**; no raise (AC7, AC15).
9. `test_topics_returns_topics_key` — `cli_runner.run_topics` returns a list; handler returns `json.dumps({"topics": [...]})` (AC8).
10. `test_topics_failure_returns_empty_topics` — raise `ICMNonZeroExitError`; handler returns `json.dumps({"topics": []})`; one WARNING (AC9).
11. `test_health_no_topic` — `args={}`; `cli_runner.run_health` called with `topic=None`; handler returns `json.dumps({"report": {...}})` (AC10).
12. `test_health_with_topic_arg` — `args={"topic": "preferences"}`; `cli_runner.run_health` called with `topic="preferences"`; handler returns `json.dumps({"report": {...}})` (AC11).
13. `test_health_failure_returns_empty_report` — raise any `ICMError`; handler returns `json.dumps({"report": {}})`; one WARNING (AC12).
14. `test_unknown_tool_name_returns_error_json` — `handle_tool_call(provider, "anything-at-all", {})`; return `json.dumps({"error": "unknown tool: ..."})` (AC13).
15. `test_no_tool_returns_dict` — across every test above, `isinstance(result, str)` (AC14). Single parametrized check that exercises every code path and asserts the type contract.
16. `test_no_tool_raises` — pass garbage args (None, list, scalar) to every dispatch; handler never raises (AC15).

### `hermes_icm_memory/provider.py` (MODIFY — minimal)

Replace the two stub method bodies to delegate to `tools.py`. Imports added at file top: `from . import tools`. The `_TOOL_UNAVAILABLE_JSON: Final[str]` constant is removed (no longer reachable).

```python
def get_tool_schemas(self) -> list[dict[str, Any]]:
    """Return the four LLM-facing tool schemas (S09 dispatch)."""
    return tools.get_tool_schemas()

def handle_tool_call(self, name: str, args: dict[str, Any]) -> str:
    """Dispatch an LLM tool call to ``tools.py``. Always returns a JSON string."""
    return tools.handle_tool_call(self, name, args)
```

### `tests/test_provider.py` (MODIFY — two assertions only)

- `test_handle_tool_call_unknown_tool_returns_error_json` — drop the `"icm_recall"` line (now a real tool); keep the `"anything-at-all"` line but rewrite the assertion to match the new "unknown tool: ..." message shape.
- `test_get_tool_schemas_is_empty_list` — rename to `test_get_tool_schemas_returns_four_schemas`; assert `len(...) == 4` and the four canonical names. Note that the deeper schema-shape contract lives in `tests/test_tools.py` (AC2).

### Provider `_write_queue` contract (S08 coordination)

S08 owns `_write_queue: queue.Queue[tuple[str, str, str, list[str]]]` on the provider. Until S08 lands, S09's tests **mock** `provider._write_queue` directly with a `queue.Queue(maxsize=N)` (or `unittest.mock.Mock()` where shape inspection is enough). The contract assumed:

```python
Trigger = tuple[str, str, str, list[str]]   # (topic, importance, content, keywords)
provider._write_queue: queue.Queue[Trigger]
provider._write_queue.put_nowait(task)      # raises queue.Full on overflow
```

If S08's actual surface differs at merge time, the manager mediates — handler will adjust to the real tuple shape; the contract here is the assumption recorded for reviewer validation.

## Dev Notes

### Architecture compliance

- **AD-10** — every handler returns `json.dumps(...)`. **Never** a dict. Caller-facing assertion: `isinstance(result, str)`.
- **AD-12** — `tools.py` MUST NOT `import subprocess`. Enforced by `tests/test_no_subprocess_outside_cli_runner.py`.
- **AD-13** — `logger = logging.getLogger(__name__)` at module top.
- **AD-07 / NFR-REL-1 / FR19** — every handler boundary is `try/except Exception` at the outermost level. On any failure: WARNING with `extra={...}` (no f-string), return the documented degrade JSON. No exception escapes.
- **NFR-PERF-1 / FR13** — `icm_store` is non-blocking: validate, enqueue via `put_nowait`, return immediately (no subprocess on the hot path; the worker thread runs `cli_runner.run_store` later).

### Schema shapes (PRD §8.6)

```python
{
    "name": "icm_recall",
    "description": "Search ICM memory for hits matching the query.",
    "parameters": {
        "type": "object",
        "properties": {
            "query":   {"type": "string", "description": "Natural-language search query."},
            "topic":   {"type": "string", "description": "Optional topic filter (e.g. 'preferences')."},
            "limit":   {"type": "integer", "description": "Max number of hits.", "default": 5},
            "project": {"type": "string", "description": "Optional project scope."},
        },
        "required": ["query"],
    },
},
{
    "name": "icm_store",
    "description": "Record a memory in ICM. Non-blocking — returns as soon as the task is queued.",
    "parameters": {
        "type": "object",
        "properties": {
            "topic":      {"type": "string"},
            "content":    {"type": "string"},
            "importance": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low"],
                "default": "high",
            },
            "keywords": {"type": "array", "items": {"type": "string"}, "default": []},
            "raw":      {"type": "string", "description": "Optional raw payload retained alongside content."},
        },
        "required": ["topic", "content"],
    },
},
{
    "name": "icm_topics",
    "description": "List the ICM topics currently populated in the local DB.",
    "parameters": {"type": "object", "properties": {}, "required": []},
},
{
    "name": "icm_health",
    "description": "Return ICM's staleness/consolidation report (optionally scoped to a topic).",
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Optional topic filter."},
        },
        "required": [],
    },
},
```

### Handler dispatch shape

```python
_DISPATCH: dict[str, Callable[[Provider, dict[str, Any]], str]] = {
    "icm_recall": _handle_recall,
    "icm_store":  _handle_store,
    "icm_topics": _handle_topics,
    "icm_health": _handle_health,
}

def handle_tool_call(provider, name, args):
    handler = _DISPATCH.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        return handler(provider, args or {})
    except Exception as exc:  # AD-07: outermost net
        logger.warning("tool handler crashed", extra={"tool": name, "err": repr(exc)})
        return json.dumps({"error": "tool handler crashed"})
```

### `_handle_store` semantics

- Validate `args["topic"]` and `args["content"]` are non-empty strings → on missing/wrong-type return `json.dumps({"error": "missing required arg: <key>"})`.
- Resolve `importance`: `args.get("importance") or provider._config.get("default_importance") or "high"`.
- Normalize `keywords`: accept `list[str]`, comma-separated string, or absent → coerce to `list[str]`. Empty list when absent.
- Build `task = (topic, importance, content, keywords_list)`. Type-pin via the `Trigger` alias.
- Enqueue via `provider._write_queue.put_nowait(task)`. Catch `queue.Full` → WARNING + return `json.dumps({"error": "store queue full"})`.
- Catch `AttributeError` (queue not yet initialized — defensive against S08 not-yet-merged) → WARNING + return `json.dumps({"error": "store queue unavailable"})`.
- Compute `queued_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")`. Return `json.dumps({"accepted": True, "queued_at": queued_at})`.

### `_handle_recall` semantics

- Required: `args["query"]` (non-empty str). Optional: `topic`, `limit`, `project`.
- `limit = int(args.get("limit") or provider._config.get("recall_limit") or 5)`.
- `db_path = provider._db_path`. If `None` (provider not initialized), degrade.
- `timeout_ms = provider._config.get("command_timeout_read_ms") or 2000`.
- Call `cli_runner.run_recall(query, limit, db_path, timeout_ms, topic=topic, project=project)`.
- On success: `json.dumps({"hits": parsed})`. On `ICMError` / `Exception`: WARNING + `json.dumps({"hits": []})`.

### `_handle_topics` and `_handle_health` semantics

- `_handle_topics(provider, args)`: ignore args; call `cli_runner.run_topics(provider._db_path, timeout_ms)`. Success → `json.dumps({"topics": parsed})`. Failure → `json.dumps({"topics": []})` + WARNING.
- `_handle_health(provider, args)`: optional `topic`; call `cli_runner.run_health(db_path, timeout_ms, topic=topic)`. Success → `json.dumps({"report": parsed})`. Failure → `json.dumps({"report": {}})` + WARNING.

### Common LLM-developer pitfalls (avoid)

- Do **not** return a dict from any handler — `json.dumps(...)` always (AD-10).
- Do **not** call `cli_runner.run_store` from `_handle_store` — that is the worker thread's job; the handler enqueues only (FR13, NFR-PERF-1).
- Do **not** import `subprocess` in `tools.py` — `cli_runner` is the only allowed importer (AD-12).
- Do **not** raise on bad args — return `{"error": ...}` JSON (FR19).
- Do **not** use f-string interpolation in `logger.warning` — pass `extra={...}` instead.
- Do **not** keep the `_TOOL_UNAVAILABLE_JSON` Final constant in `provider.py` — it becomes unreachable once dispatch lands. Remove it.

### Hard quality gates

- 16 new tests in `tests/test_tools.py` pass.
- Existing 75 tests + 3 skips remain stable (the two AC-13/AC-14 tests in `test_provider.py` are rewritten in lockstep — net delta still 75+ passed).
- Coverage: package ≥ 85 %; `tools.py` ≥ 95 %.
- ruff clean; mypy --strict clean.
- `tests/test_no_subprocess_outside_cli_runner.py` still passes (`tools.py` has no subprocess import).

### References

- [Source: _bmad-output/planning-artifacts/prd.md#8.6 LLM tool surface] — canonical tool-surface table.
- [Source: _bmad-output/planning-artifacts/prd.md#FR8, FR11, FR13, FR17, FR19] — read/write/topics/health + degrade contract.
- [Source: _bmad-output/planning-artifacts/architecture.md#5.3 LLM tool handlers] — dispatch shape per tool.
- [Source: _bmad-output/planning-artifacts/architecture.md#AD-10, AD-12, AD-13] — JSON-string return contract; subprocess isolation; logging namespace.
- [Source: _bmad-output/planning-artifacts/epics-and-stories.md#Story 4.2] — verbatim ACs + 16-test plan.

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (BMAD dev-story phase, S09).

### Debug Log References

(Filled during Phase 2.)

### Completion Notes List

(Filled during Phase 4.)

### File List

- `hermes_icm_memory/tools.py` (NEW)
- `tests/test_tools.py` (NEW)
- `hermes_icm_memory/provider.py` (MODIFY) — replace `get_tool_schemas` and `handle_tool_call` stubs with dispatch into `tools.py`; drop unreachable `_TOOL_UNAVAILABLE_JSON` constant.
- `tests/test_provider.py` (MODIFY) — rewrite two assertions (AC13/AC14) to match the new behaviour.

### Change Log

| Date       | Change |
|------------|--------|
| 2026-05-06 | Story drafted (Phase 1 / `/bmad-create-story`): 16 ACs, 16-test plan, file spec, dev notes locked. |
| 2026-05-06 | Phase 2 / `/bmad-dev-story` (TDD): RED with `tools` ImportError, then GREEN. 16 ACs implemented across `tools.py` + dispatch wiring on `provider.py`. 30 tests pass; tools.py 100% line+branch; package 97.35%. ruff + mypy --strict clean. |
| 2026-05-06 | Phase 3 / `/bmad-code-review` (Acceptance Auditor + Blind Hunter + Edge Case Hunter, three subagents in parallel). Findings actioned in code+tests: (a) inner `except ICMError` → `except Exception` in all three read handlers so untyped errors still produce per-tool degrade shape (closes Edge F7/F8); (b) `_read_timeout_ms` made defensive against non-dict `_config` and `_recall_limit`/`_importance_for` extracted (closes Edge F5/F6); (c) non-dict `args` coerced to `{}` at the dispatch boundary (closes Edge F11); (d) `importance` validated against the schema enum, bogus values fall back rather than reach the worker (closes Edge F3 / Blind 3); (e) every WARNING `extra={...}` now includes the `tool` name for parity with the dispatch crash log (closes Blind 1); (f) test for AC6 config-default-importance branch added (closes Auditor 1); (g) AC9/AC12 "exactly one WARNING" tightened from `any` to `len == 1` (closes Auditor 2); (h) defense-in-depth test for the outer dispatch crash net pinning AD-07. Eleven follow-up tests added; tools.py stays 100% line+branch; suite 121 passed / 3 skipped; ruff + mypy --strict clean. Skipped: keyword-type-mismatch warning (F1, documented), bool-as-int (F2, harmless), misleading "missing" message (F4, cosmetic), Python 3.11 compat (F9, AD-02 floor verified), name=None stringification (F10, intended), `multiprocessing.Queue` swap (F12, S08-side concern), `Protocol` for provider state (Blind 5, deferred), keyword shape vs `cli_runner.run_store` signature (Blind 4, S08-side reformat). |
