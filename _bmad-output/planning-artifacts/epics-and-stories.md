---
stepsCompleted:
  - step-01-validate-prerequisites
  - step-02-design-epics
  - step-03-create-stories
  - step-04-final-validation
inputDocuments:
  - _bmad-output/planning-artifacts/product-brief.md
  - _bmad-output/planning-artifacts/prd.md
  - _bmad-output/planning-artifacts/architecture.md
project_name: hermes-icm-memory
date: '2026-05-05'
---

# hermes-icm-memory — Epic Breakdown

## Overview

This document decomposes the approved [Product Brief](./product-brief.md), [PRD](./prd.md), and [Architecture](./architecture.md) into 5 epics and 14 stories. Stories are designed to be **file-disjoint** wherever possible so Phase 3 implementation can run in parallel waves. Every story carries: ID, title, user-story, acceptance criteria (Given/When/Then), files touched, test plan (TDD: tests written first), effort (S/M/L), and dependencies on other stories.

The user-story persona is **the Hermes plugin author / local-AI hobbyist** (the v1 primary user from the Brief). For internal-quality stories the persona is **the maintainer**, since those stories deliver developer-facing value (CI green, invariants enforced).

---

## Requirements Inventory

### Functional Requirements

- **FR1**: Plugin installable via `pip install hermes-icm-memory` and via `~/.hermes/plugins/hermes-icm-memory/`; registers a memory provider named `icm`.
- **FR2**: Plugin derives DB path from `kwargs['hermes_home']`; different `hermes_home` values produce different DB files; no hardcoded `~/.hermes`.
- **FR3**: `is_available()` reports availability without network I/O; returns `True` only if `icm` is on PATH and executable.
- **FR4**: Initialization is idempotent — running `initialize` twice does not corrupt state nor invoke setup redundantly.
- **FR5**: `on_session_end` flushes in-flight non-blocking writes within a bounded grace period; does not block teardown beyond that.
- **FR6**: User can override default importance, default topic prefix, recall limit, prefetch on/off, sync-write queue capacity, and ICM command timeout via the config schema.
- **FR7**: Plugin validates user-supplied config and rejects invalid values structurally (no raise into the agent turn).
- **FR8**: LLM can call `icm_recall` with `{query, topic?, limit?, project?}` and receive JSON-encoded list of hits.
- **FR9**: Plugin performs `prefetch` recall against recent-turn content; results cached for the immediately following `system_prompt_block` injection.
- **FR10**: Plugin returns a `system_prompt_block` string composed of top-K recalled memories + compact project-context summary.
- **FR11**: LLM can call `icm_topics` and receive the list of ICM topics.
- **FR12**: Recall paths can read memories stored by any other ICM client on the same machine, given the same DB path.
- **FR13**: LLM can call `icm_store` with `{topic, content, importance?, keywords?, raw?}`; returns immediately, never blocks the turn.
- **FR14**: `sync_turn` detects the five mandatory triggers (errors-resolved, decisions-{project}, preferences, context-{project}, periodic progress) and enqueues corresponding writes without blocking.
- **FR15**: Writes drained in FIFO by a single daemon worker; bounded queue overflow drops new writes with one WARNING per overflow burst; the agent turn is never throttled.
- **FR16**: Hermes-side categories map to ICM `(topic, importance)` per the documented matrix.
- **FR17**: LLM can call `icm_health` and receive ICM's staleness/consolidation report.
- **FR18**: Structured logs emitted under the `hermes_icm_memory` namespace at appropriate levels (DEBUG/INFO/WARNING).
- **FR19**: All ICM failure modes degrade silently — log once at WARNING, return empty for reads / drop for writes, never raise into the turn.

### NonFunctional Requirements

- **NFR-PERF-1**: `sync_turn` returns within 5 ms p95 under nominal load.
- **NFR-PERF-2**: End-to-end p95 added latency from the plugin is < 50 ms on the Hermes turn benchmark.
- **NFR-PERF-3**: ICM subprocess calls carry configurable hard timeouts (default 2000 ms read / 5000 ms write).
- **NFR-PERF-4**: `prefetch` results cached for the immediately-following `system_prompt_block` call.
- **NFR-REL-1**: No code path raises an exception into the agent turn loop.
- **NFR-REL-2**: Single daemon worker thread; lazy-respawn at most once per process; degrade-to-drop on second death.
- **NFR-REL-3**: Test coverage ≥ 85 % (line + branch) for `hermes_icm_memory`; CI fails below.
- **NFR-REL-4**: Zero `ruff check` warnings; zero `mypy --strict` errors; CI fails on any.
- **NFR-REL-5**: `initialize`, queue startup, and DB-dir creation are idempotent.
- **NFR-SEC-1**: Zero network I/O originated by this plugin.
- **NFR-SEC-2**: Profile isolation: every DB path derives from `kwargs['hermes_home']`; no cross-profile leakage.
- **NFR-SEC-3**: Subprocess uses list-form `subprocess.run([...], shell=False)` — no string concatenation.
- **NFR-SEC-4**: No secrets handled, stored, or logged; no chmod/chown of user files.
- **NFR-OBS-1**: All logging via Python `logging` under `hermes_icm_memory` namespace; no `print()` in non-test code.
- **NFR-OBS-2**: Subprocess invocations log at DEBUG with redacted argv + elapsed time.
- **NFR-OBS-3**: Trigger detections / queue overflows / worker restarts log at INFO or WARNING per the matrix.
- **NFR-OBS-4**: `icm_health` surfaces ICM's report; plugin maintains no parallel health system.
- **NFR-MAINT-1**: Public API surface (class name, four tool names, ten config keys) frozen post-v1.
- **NFR-MAINT-2**: Only `cli_runner.py` imports `subprocess`; v2 may swap it for MCP transport without breaking the public API.
- **NFR-MAINT-3**: Type hints on every public function and class; `mypy --strict` clean.

### Additional Requirements (Architecture)

- Mirror the `hermes-rtk-hook` reference scaffold layout (committed locally at `/home/nikos/.hermes/plugins/hermes-rtk-hook/`).
- Component map locked: `provider.py`, `cli_runner.py`, `tools.py`, `hooks.py`, `config.py`, `mapping.py`, `errors.py`, `__init__.py`, `_version.py`.
- AST/grep invariant tests required: only `cli_runner.py` imports `subprocess`; the literal `"~/.hermes"` does not appear anywhere; no socket created during lifecycle methods.
- ICM CLI verified surface (2026-05-05): `icm --db <path> recall <q> --limit K --format json [-t topic] [-p project]`; `icm store -t topic -c content -i importance [-k keywords] [-r raw]`; `icm topics`; `icm health [-t topic]`. **Do not call `icm init`** — that subcommand configures Claude-Code integration; the SQLite DB auto-creates on first `--db <path>` call.
- GitHub Actions CI matrix: Python 3.11 + 3.12 on `ubuntu-latest`; install `icm`, then ruff → mypy → pytest with `--cov-fail-under=85 --cov-branch`.
- Distribution: PyPI entry-point group `hermes_agent.plugins`, plus drop-in install at `~/.hermes/plugins/hermes-icm-memory/`.

### UX Design Requirements

Not applicable — this is a backend memory provider plugin with no UI surface. The "interaction surface" is the four LLM-facing tools (`icm_recall`, `icm_store`, `icm_topics`, `icm_health`); their schemas / descriptions are part of the functional contract (FR8–FR17) rather than a separate UX concern.

### FR Coverage Map

| FR    | Story owner(s)        | Module(s)                          |
|-------|-----------------------|-------------------------------------|
| FR1   | S01, S03, S10         | `pyproject.toml`, `plugin.yaml`, `__init__.py`, `README.md` |
| FR2   | S05, S07, S12         | `config.py`, `provider.py`, `tests/test_profile_isolation.py` |
| FR3   | S07, S11              | `provider.py`, `tests/test_no_network_calls.py` |
| FR4   | S07                   | `provider.py`                       |
| FR5   | S08                   | `hooks.py`                          |
| FR6   | S05                   | `config.py`                         |
| FR7   | S05, S07              | `config.py`, `provider.py`          |
| FR8   | S09                   | `tools.py` (`icm_recall`)           |
| FR9   | S08                   | `hooks.py` (`prefetch`)             |
| FR10  | S08                   | `hooks.py` (`system_prompt_block`)  |
| FR11  | S09                   | `tools.py` (`icm_topics`)           |
| FR12  | S14                   | `tests/integration/test_real_icm_cross_tool.py` |
| FR13  | S08, S09              | `hooks.py` (queue), `tools.py` (`icm_store`) |
| FR14  | S06, S08              | `mapping.py`, `hooks.py` (`sync_turn`) |
| FR15  | S08, S14              | `hooks.py` (worker), `tests/integration/test_sync_turn_stress.py` |
| FR16  | S06                   | `mapping.py`                        |
| FR17  | S09                   | `tools.py` (`icm_health`)           |
| FR18  | S04, S07–S10          | logging across all modules          |
| FR19  | S04, S13              | `cli_runner.py`, `tests/test_errors_and_degrade.py` |

---

## Epic List

### Epic 1: Plugin foundation — installable, CI-green, documented

Stand up the package skeleton, CI pipeline, and OSS docs so subsequent work has a place to land and turn green on every push.

**FRs covered:** FR1 (installability + registration shape).
**NFRs covered:** NFR-REL-3 (coverage gate wired), NFR-REL-4 (lint/type gates wired).
**Stories:** S01, S02, S03.

### Epic 2: ICM adapter core — safe, mockable, configurable

Build the three pure modules that everything else depends on: typed errors, the `subprocess`-isolated CLI runner, the config schema with path resolution, and the trigger-detection mapping module.

**FRs covered:** FR2, FR6, FR7, FR16, FR19 (in part — degrade behavior in `cli_runner`).
**NFRs covered:** NFR-PERF-3, NFR-SEC-3, NFR-MAINT-2 (`subprocess` isolation), NFR-OBS-2.
**Stories:** S04, S05, S06.

### Epic 3: Memory provider lifecycle — Hermes can register, configure, tear down

Implement the `IcmMemoryProvider` class + `register(ctx)` wiring so Hermes recognizes us as a provider, configures cleanly, and shuts down without leaking work.

**FRs covered:** FR1 (real registration), FR3, FR4, FR7 (provider-side validation).
**NFRs covered:** NFR-REL-5 (idempotency), NFR-SEC-1 (no network in `is_available`), NFR-MAINT-1 (frozen API).
**Stories:** S07, S10.

### Epic 4: Memory operations — recall in the prompt, store in the background

The hot-path features: hooks that inject memory into every turn, tools that let the LLM drive recall/store/health directly, and the bounded-queue daemon worker that keeps writes off the agent's critical path.

**FRs covered:** FR5, FR8, FR9, FR10, FR11, FR13, FR14, FR15, FR17.
**NFRs covered:** NFR-PERF-1, NFR-PERF-2, NFR-PERF-4, NFR-REL-1, NFR-REL-2, NFR-OBS-3, NFR-OBS-4.
**Stories:** S08, S09.

### Epic 5: Quality guardrails & integration — invariants enforced, cross-tool verified

Architectural invariants enforced by AST/grep tests, profile-isolation guarantees, full failure-mode matrix, and integration tests against a real `icm` binary including the cross-tool sharing demo.

**FRs covered:** FR12 (cross-tool integration), FR18, FR19 (full degrade matrix).
**NFRs covered:** NFR-SEC-1 (no socket), NFR-SEC-2 (profile isolation), NFR-MAINT-2 (subprocess isolation), NFR-REL-1 (no-raise matrix).
**Stories:** S11, S12, S13, S14.

---

## Story Dependency Graph

```
S01 ────┬──► S02 ──► (CI green)
        ├──► S03
        ├──► S04 ──┬──────────► S07 ──┬──► S08 ──┬──► S10 ──► S14
        ├──► S05 ──┤             ▲     │          │
        ├──► S06 ──┘             │     ├──► S09 ──┤
        ├──► S11                 │     │          │
        │                        │     │          ├──► S13
        │                        │     │          │
        │                        └─────┴──► S12 ──┘
```

**Parallel wave plan:**

- Wave 0 (sequential): S01.
- Wave 1 (parallel after S01): S02, S03, S04, S05, S06, S11. **Six lanes**.
- Wave 2 (parallel after S04 & S05): S07.
- Wave 3 (parallel after S04, S06, S07): S08; (after S04, S07): S09; (after S05, S07): S12.
- Wave 4 (sequential after S07, S08, S09): S10. **Touches `__init__.py` again — single lane**.
- Wave 5 (parallel after S10): S13, S14.

---

## Epic 1: Plugin foundation

**Goal:** A new contributor can `pip install -e ".[dev]"`, run `pytest` + `ruff` + `mypy --strict`, and get a green pipeline. The plugin is discoverable by Hermes via `register(ctx)` even though no real provider logic is wired yet.

### Story 1.1: Package skeleton and entry point

**ID:** S01 · **Effort:** M · **Dependencies:** none.

As a maintainer,
I want the package skeleton in place with `pyproject.toml`, `plugin.yaml`, a stub `register(ctx)`, and a passing baseline test,
So that every subsequent story has a CI-validated foundation to build on.

**Acceptance Criteria:**

**Given** a fresh checkout
**When** I run `pip install -e ".[dev]"` then `pytest`
**Then** installation succeeds, the test suite runs, and `tests/test_plugin_loader.py` passes asserting `register(ctx)` calls `ctx.register_memory_provider` exactly once with an object that has `name == "icm"`.

**Given** the package is installed
**When** Python imports `hermes_icm_memory`
**Then** `hermes_icm_memory.__version__` equals the value in `_version.py` and matches `pyproject.toml`'s version field.

**Given** `pyproject.toml`
**When** examined
**Then** it declares `requires-python = ">=3.11"`, `[project.entry-points."hermes_agent.plugins"] hermes-icm-memory = "hermes_icm_memory:register"`, optional dev deps (pytest, pytest-cov, coverage, ruff, mypy), pytest config `--cov=hermes_icm_memory --cov-branch --cov-fail-under=85`, ruff `target-version = "py311"`, and `mypy strict = true`.

**Given** `plugin.yaml`
**When** read
**Then** it lists `name: hermes-icm-memory`, `version: 0.1.0`, four hooks (`prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`), and the description from the PRD's Executive Summary (one line).

**Files touched:**

- `pyproject.toml` (NEW)
- `plugin.yaml` (NEW)
- `hermes_icm_memory/__init__.py` (NEW — stub `register(ctx)` that constructs a placeholder `_StubProvider` with `name = "icm"` and calls `ctx.register_memory_provider(provider)`)
- `hermes_icm_memory/_version.py` (NEW — `__version__ = "0.1.0"`)
- `tests/__init__.py` (NEW — empty)
- `tests/conftest.py` (NEW — empty fixture file; later stories add fixtures)
- `tests/test_plugin_loader.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_register_calls_register_memory_provider_once` — fake `ctx` with a `register_memory_provider(p)` mock; assert called exactly once after `register(ctx)`.
2. `test_registered_provider_name_is_icm` — capture the argument; assert `provider.name == "icm"`.
3. `test_version_is_consistent` — `hermes_icm_memory.__version__` equals `_version.__version__` and equals the version string in `pyproject.toml` (parsed via `tomllib`).
4. `test_plugin_yaml_shape` — load `plugin.yaml`, assert keys `name`, `version`, `description`, `hooks` exist; `hooks` contains the four expected hook names.

### Story 1.2: GitHub Actions CI pipeline

**ID:** S02 · **Effort:** S · **Dependencies:** S01.

As a maintainer,
I want CI that lints, type-checks, and tests on every push and PR across Python 3.11 + 3.12,
So that quality regressions are caught before merge.

**Acceptance Criteria:**

**Given** a PR is opened
**When** the workflow runs
**Then** it executes `ruff check .`, `mypy --strict hermes_icm_memory`, and `pytest` with the 85 % coverage gate, on both Python 3.11 and Python 3.12, on `ubuntu-latest`.

**Given** any of those steps fails
**When** GitHub evaluates the PR
**Then** the workflow status is failure and merging is blocked by required-status-check (configured at the repo level, out of code scope).

**Given** the workflow file
**When** read
**Then** it installs `icm` from upstream (so integration tests can run) before installing the package; the install step is named `Install icm` and surfaces `icm --version`.

**Files touched:**

- `.github/workflows/ci.yml` (NEW)

**Test plan (TDD; tests first):**

1. `tests/test_ci_workflow.py::test_workflow_yaml_shape` — parse `.github/workflows/ci.yml`, assert: matrix has both `"3.11"` and `"3.12"`; steps include `ruff check`, `mypy`, `pytest`; runs on `push` and `pull_request`.
2. `tests/test_ci_workflow.py::test_workflow_installs_icm` — assert a step named `Install icm` exists before the `Install package` step.
3. `tests/test_ci_workflow.py::test_workflow_runs_pytest_with_coverage_gate` — assert the test step's command contains `--cov-fail-under=85` (or the gate is set in `pyproject.toml` and pytest is invoked plain).

### Story 1.3: README quickstart and CONTRIBUTING

**ID:** S03 · **Effort:** S · **Dependencies:** S01.

As a new user,
I want a README with a 3-step quickstart and a CONTRIBUTING file with the dev-loop commands,
So that I can install + verify in under 5 minutes (SM8) and a PR contributor knows how to run the gates locally.

**Acceptance Criteria:**

**Given** the README
**When** read
**Then** it contains: project tagline, 3-step quickstart (verify `icm`, `pip install`, `hermes plugins enable && hermes memory setup icm`), a feature bullet list aligned with the PRD differentiators, and links to the ICM and Hermes upstreams.

**Given** CONTRIBUTING.md
**When** read
**Then** it documents: how to set up dev (`pip install -e ".[dev]"`), how to run lint (`ruff check .`), type-check (`mypy hermes_icm_memory`), tests (`pytest`), the 85 % coverage requirement, the TDD-required policy, and a one-line note on commit message style (short imperative; no `Co-Authored-By` line).

**Files touched:**

- `README.md` (REPLACES the placeholder)
- `CONTRIBUTING.md` (NEW)

**Test plan (TDD; tests first):**

1. `tests/test_docs.py::test_readme_has_quickstart` — readme text contains `"## Quickstart"` and references `pip install hermes-icm-memory`, `hermes plugins enable hermes-icm-memory`, and `hermes memory setup icm`.
2. `tests/test_docs.py::test_contributing_has_dev_loop` — contributing text mentions `ruff check`, `mypy`, `pytest`, and the 85 % coverage threshold.
3. `tests/test_docs.py::test_readme_links_upstreams` — readme links to `https://github.com/rtk-ai/icm` and the Hermes plugin docs URL.

---

## Epic 2: ICM adapter core

**Goal:** Three pure modules — typed errors, the only `subprocess`-importing module, and the config + mapping helpers — give the rest of the codebase a clean, testable foundation that Hermes runtime concerns never need to enter.

### Story 2.1: Typed errors + CLI runner (read & write paths)

**ID:** S04 · **Effort:** M · **Dependencies:** S01.

As a maintainer,
I want `cli_runner.py` to be the only module that imports `subprocess` and to wrap every `icm` invocation behind typed exceptions,
So that v2's MCP-transport swap touches one file (NFR-MAINT-2) and every failure mode is centrally translatable.

**Acceptance Criteria:**

**Given** `cli_runner.run_recall(query, limit, db_path, timeout_ms, topic=None, project=None)`
**When** called
**Then** it builds argv `["icm", "--db", str(db_path), "recall", query, "--limit", str(limit), "--format", "json", ...]`, invokes `subprocess.run(argv, capture_output=True, text=True, timeout=timeout_ms/1000, check=False, shell=False)`, returns the parsed JSON list on success.

**Given** an `icm` invocation
**When** `subprocess.run` raises `FileNotFoundError`, `subprocess.TimeoutExpired`, or returns non-zero
**Then** `cli_runner` raises `ICMNotFoundError`, `ICMTimeoutError`, or `ICMNonZeroExitError` respectively, all from `errors.py`, with the original message attached.

**Given** stdout that is not valid JSON
**When** `json.loads` fails
**Then** `cli_runner` raises `ICMMalformedOutputError` with the first 200 chars of stdout in the message.

**Given** `run_store(topic, content, importance, db_path, timeout_ms, keywords=None, raw=None)`
**When** called
**Then** it builds list-form argv with `-t`, `-c`, `-i`, optional `-k`, optional `-r`; ignores stdout; raises the same typed exceptions on failure.

**Given** `run_topics(db_path, timeout_ms)` and `run_health(db_path, timeout_ms, topic=None)`
**When** called
**Then** they invoke `icm topics --format json` and `icm health [-t topic] --format json` respectively, parse JSON, and return list / dict.

**Given** any of the four `run_*` functions
**When** invoked
**Then** a DEBUG log entry is emitted with the redacted argv (content / query truncated to 80 chars) and elapsed milliseconds.

**Files touched:**

- `hermes_icm_memory/errors.py` (NEW — `ICMError` base + four subtypes)
- `hermes_icm_memory/cli_runner.py` (NEW)
- `tests/test_cli_runner.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_run_recall_argv_shape_default` — mock `subprocess.run`; assert argv includes `--db <path>`, `recall`, `<query>`, `--limit`, `--format json`.
2. `test_run_recall_argv_shape_with_topic_and_project` — same with optional flags appended in stable order.
3. `test_run_recall_returns_parsed_list` — mock returns JSON `[{"id":"x"}]`; assert function returns Python list.
4. `test_run_recall_raises_not_found` — `subprocess.run` raises `FileNotFoundError`; assert `ICMNotFoundError`.
5. `test_run_recall_raises_timeout` — `subprocess.run` raises `TimeoutExpired`; assert `ICMTimeoutError`.
6. `test_run_recall_raises_nonzero` — returncode 2 with stderr; assert `ICMNonZeroExitError` with stderr in message.
7. `test_run_recall_raises_malformed` — stdout is `"not json"`; assert `ICMMalformedOutputError`.
8. `test_run_store_argv_shape` — argv includes `store`, `-t`, `-c`, `-i`; optional `-k`, `-r` only when supplied.
9. `test_run_store_does_not_parse_stdout` — stdout content irrelevant, only returncode matters.
10. `test_run_topics_argv_and_parse` — argv includes `topics --format json`; returns list.
11. `test_run_health_argv_with_topic` — `-t <topic>` appears when supplied; returns dict.
12. `test_debug_log_emits_redacted_argv` — caplog at DEBUG; assert content > 80 chars is truncated.
13. `test_subprocess_invoked_with_shell_false_and_timeout` — kwargs assertion (NFR-SEC-3, NFR-PERF-3).

### Story 2.2: Config schema + path resolution

**ID:** S05 · **Effort:** M · **Dependencies:** S01.

As a Hermes user,
I want a tunable config (importance default, recall limit, queue size, timeouts, etc.) and per-profile DB-path resolution,
So that I can fit the plugin to my workflow and run multiple Hermes profiles without DB collision (FR2, FR6, FR7).

**Acceptance Criteria:**

**Given** `config.get_default_schema()`
**When** called
**Then** it returns a list of 10 entries covering every key in the architecture's §10.1 table, each with `key`, `description`, `secret: false`, `required` flag, type, default, and (where applicable) `choices`.

**Given** `config.validate(values: dict)`
**When** passed valid values
**Then** it returns `(True, normalized_values)`; ints are coerced from strings, bools from `"true"/"false"`.

**Given** `config.validate(values)`
**When** passed an invalid value (out-of-range int, unknown enum, wrong type)
**Then** it returns `(False, {"error": "<actionable message naming the bad key>"})` and never raises (AD-18, FR7).

**Given** `config.resolve_db_path(hermes_home, profile=None)`
**When** called with `hermes_home="/tmp/hh-A"`
**Then** it returns `Path("/tmp/hh-A/icm/default.db")`.

**Given** the same with `profile="work"`
**When** called
**Then** it returns `Path("/tmp/hh-A/icm/work.db")`.

**Given** `hermes_home="~/foo"`
**When** resolved
**Then** the result is the expanded absolute path under the user home, with `~` resolved.

**Files touched:**

- `hermes_icm_memory/config.py` (NEW)
- `tests/test_config.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_default_schema_has_ten_keys` — assert exact key set per architecture §10.1.
2. `test_validate_accepts_default_values` — every default value passes `validate`.
3. `test_validate_rejects_negative_queue_size` — returns `(False, {"error": ...})` mentioning `sync_write_queue_size`.
4. `test_validate_rejects_unknown_importance` — value `"weak"` rejected.
5. `test_validate_coerces_strings_to_ints` — `"5"` for `recall_limit` → int `5`.
6. `test_validate_never_raises_on_garbage_input` — pass a list / None / nested dict; method returns `(False, ...)`.
7. `test_resolve_db_path_default_profile` — `<hh>/icm/default.db`.
8. `test_resolve_db_path_named_profile` — `<hh>/icm/<name>.db`.
9. `test_resolve_db_path_expands_tilde` — `~/foo` → `/home/<user>/foo` (use `monkeypatch.setenv("HOME", ...)`).
10. `test_resolve_db_path_makes_parent_idempotent` — the parent-dir creation helper called twice yields no error and only one mkdir-equivalent observable side effect.

### Story 2.3: Trigger detection mapping

**ID:** S06 · **Effort:** S · **Dependencies:** S01.

As a Hermes user,
I want `sync_turn` to detect the five mandatory store triggers and emit ICM-shaped write tasks with the right topic + importance,
So that decisions, errors-resolved, preferences, context, and periodic progress are captured automatically (FR14, FR16).

**Acceptance Criteria:**

**Given** `mapping.MAPPING`
**When** read
**Then** it is a dict with keys `decisions`, `errors-resolved`, `preferences`, `context`, `learnings`; each value has `topic_template` (with optional `{project}` placeholder) and `importance` per the architecture matrix (decisions↔`decisions-{project}` high; errors↔`errors-resolved` high; preferences↔`preferences` critical; context↔`context-{project}` high; learnings↔`learnings` high).

**Given** `mapping.detect_triggers(user_text, assistant_text, project=None, turn_index=0, every_n_turns=20)`
**When** called with a turn whose assistant text contains a fix-it pattern (e.g. "fixed", "resolved", "the bug was")
**Then** the result includes a tuple `(topic="errors-resolved", importance="high", content=<summary>, keywords=[...])`.

**Given** the same with assistant text containing decision phrasing ("decided to", "going with", "we'll use X")
**When** called
**Then** the result includes a tuple with topic `"decisions-<project or default>"` and importance `"high"`.

**Given** a turn whose user text contains preference phrasing ("always use", "never use", "prefer")
**When** called
**Then** the result includes a tuple with topic `"preferences"` and importance `"critical"`.

**Given** `turn_index % every_n_turns == 0` (and > 0)
**When** called
**Then** the result includes a periodic-progress tuple with topic `"context-<project or default>"` and importance `"high"`.

**Given** none of the patterns match and it isn't a periodic boundary
**When** called
**Then** the result is an empty list.

**Files touched:**

- `hermes_icm_memory/mapping.py` (NEW)
- `tests/test_mapping.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_mapping_dict_has_five_categories` — exact key set.
2. `test_mapping_topic_and_importance_for_each_category` — five separate assertions matching FR16's matrix.
3. `test_detect_errors_resolved_pattern` — fixture user/assistant strings; result contains the expected tuple.
4. `test_detect_decisions_pattern` — same, with `project="hermes-icm-memory"` interpolated.
5. `test_detect_preferences_critical` — preference text → `"preferences"` + `"critical"`.
6. `test_detect_context_periodic` — `turn_index=20`, `every_n_turns=20` → context tuple emitted.
7. `test_detect_no_match_returns_empty` — neutral text → `[]`.
8. `test_detect_multiple_triggers_in_one_turn` — text matches both errors-resolved and decisions → both tuples emitted (independent triggers).
9. `test_topic_template_with_default_project` — `project=None` → `"context-default"` not `"context-{project}"`.

---

## Epic 3: Memory provider lifecycle

**Goal:** Hermes sees a real `IcmMemoryProvider` that satisfies the entire `MemoryProvider` contract (lifecycle methods + tool dispatch + config), uses the modules from Epic 2 under the hood, and ships a working `register(ctx)` entry point.

### Story 3.1: `IcmMemoryProvider` class

**ID:** S07 · **Effort:** L · **Dependencies:** S04 (cli_runner, errors), S05 (config).

As a Hermes runtime,
I want a `MemoryProvider` subclass that implements every required method (`name`, `is_available`, `initialize`, `get_config_schema`, `save_config`, `get_tool_schemas`, `handle_tool_call`),
So that `hermes memory setup icm` runs end-to-end and the provider is wired correctly (FR1, FR3, FR4, FR7).

**Acceptance Criteria:**

**Given** `IcmMemoryProvider()` constructed
**When** `provider.name` is read
**Then** the value is `"icm"`.

**Given** the provider
**When** `is_available()` is called and `shutil.which("icm")` returns truthy
**Then** the result is `True`; on subsequent calls within the same process the result is cached (no second `shutil.which` call) — verifiable by patching `shutil.which` and counting calls.

**Given** `is_available()` is called and `shutil.which("icm")` returns `None`
**When** evaluated
**Then** the result is `False`; no socket is created during the call.

**Given** `provider.initialize(session_id="s1", hermes_home="/tmp/hh", profile="work")` is called
**When** examined afterward
**Then** the provider has set `self._db_path` to `<hermes_home>/icm/work.db`, ensured `<hermes_home>/icm/` exists (parent mkdir), and recorded `session_id`. Calling `initialize` a second time with the same args is a no-op (FR4, NFR-REL-5).

**Given** `provider.get_config_schema()`
**When** called
**Then** it returns the list from `config.get_default_schema()` verbatim.

**Given** `provider.save_config({"recall_limit": 7, "default_importance": "high"}, hermes_home="/tmp/hh")`
**When** called with valid values
**Then** the values persist (writes a small JSON sidecar at `<hermes_home>/icm/config.json` or stores in memory — implementation choice, but loaded back by the next `get_config()`); returns `None`.

**Given** `provider.save_config({"recall_limit": -1}, ...)`
**When** called with invalid values
**Then** it returns `{"error": "..."}` and never raises (FR7).

**Given** `provider.get_tool_schemas()`
**When** called before tools.py is wired
**Then** it returns an empty list (or four placeholder schemas — coordinated with S09; ACs in S09 fill these in).

**Given** `provider.handle_tool_call("icm_recall", {"query": "x"})`
**When** S09 has not yet wired tools
**Then** it returns `json.dumps({"error": "tool unavailable"})` (a placeholder — overwritten in S09's ACs).

**Files touched:**

- `hermes_icm_memory/provider.py` (NEW)
- `tests/test_provider.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_name_is_icm` — class property literal.
2. `test_is_available_true_when_icm_on_path` — patch `shutil.which` → string; assert True.
3. `test_is_available_false_when_missing` — patch `shutil.which` → None; assert False.
4. `test_is_available_caches_result` — patch returns same value; call twice; assert `shutil.which` called once.
5. `test_is_available_no_socket` — patch `socket.socket` to raise on construction; assert `is_available()` does not raise.
6. `test_initialize_resolves_db_path` — assert `self._db_path` matches expected.
7. `test_initialize_creates_parent_dir` — assert `<hermes_home>/icm/` exists after init.
8. `test_initialize_idempotent` — call twice, assert second call produces no additional mkdir attempt (use a counting mock around `Path.mkdir`).
9. `test_initialize_with_unwritable_hermes_home_self_disables` — point `hermes_home` at a read-only path; assert `OSError` is caught, WARNING logged, `is_available()` returns False from then on.
10. `test_get_config_schema_matches_defaults` — equal to `config.get_default_schema()`.
11. `test_save_config_accepts_valid` — returns `None`; values readable.
12. `test_save_config_rejects_invalid_returns_error_dict` — never raises, returns `{"error": ...}`.
13. `test_handle_tool_call_unknown_tool_returns_error_json` — returns `json.dumps({"error": "..."})`.

### Story 3.2: Wire `register(ctx)` to the real provider

**ID:** S10 · **Effort:** S · **Dependencies:** S07, S08, S09.

As a Hermes runtime,
I want `hermes_icm_memory.__init__.register(ctx)` to register a real `IcmMemoryProvider` with hooks declared in `plugin.yaml` actually bound to the provider's hook methods (from S08),
So that the plugin transitions from "stub registered" to "fully functional".

**Acceptance Criteria:**

**Given** `register(ctx)`
**When** invoked with a fake `ctx` exposing `register_memory_provider(provider)`
**Then** it constructs a single `IcmMemoryProvider`, calls `ctx.register_memory_provider(provider)` exactly once, and that provider's `prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end` methods are bound (importable / callable) per Hermes contract.

**Given** the plugin loaded by Hermes (simulated)
**When** `plugin.yaml` declares the four hooks
**Then** Hermes can call them on the registered provider without an `AttributeError`.

**Given** the existing `tests/test_plugin_loader.py` from S01
**When** re-run after this story
**Then** the original assertions still hold (S01's test is upgraded — does not regress).

**Files touched:**

- `hermes_icm_memory/__init__.py` (MODIFY — replace stub with real wiring)

**Test plan (TDD; tests first):**

1. `test_plugin_loader.py::test_register_constructs_real_provider` — captured argument is an `IcmMemoryProvider` instance, not the placeholder.
2. `test_plugin_loader.py::test_provider_hook_methods_bound` — `getattr(provider, "prefetch")` is callable; same for the other three.
3. `test_plugin_loader.py::test_register_called_once_idempotent_module_import` — re-importing the package does not re-register (Python caches the module; just guard against importing side-effects beyond the function).

---

## Epic 4: Memory operations

**Goal:** The hot-path features. Hooks inject memory into prompts; tools let the LLM drive recall + store + health directly; the bounded-queue daemon worker keeps writes off the agent's critical path. No code path raises into a turn.

### Story 4.1: Hooks (`prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`) + bounded-queue worker

**ID:** S08 · **Effort:** L · **Dependencies:** S04 (cli_runner), S06 (mapping), S07 (provider).

As a Hermes user,
I want the plugin to recall memories before each LLM call, inject them into the system prompt, and persist new memories non-blockingly after each turn,
So that my agent acts informed and never pays turn-perceptible latency for memory writes (FR5, FR9, FR10, FR13, FR14, FR15, NFR-PERF-1, NFR-REL-1, NFR-REL-2).

**Acceptance Criteria:**

**Given** `provider.prefetch(query="...", **kwargs)` is called
**When** ICM is available
**Then** it calls `cli_runner.run_recall(query, limit=config.recall_limit, db_path=self._db_path, timeout_ms=config.command_timeout_read_ms)`, stores the result in `self._prefetch_cache[hash(query)]`, and returns a recalled-string suitable for prompt context.

**Given** `prefetch` and any failure raises (NotFound / Timeout / NonZeroExit / Malformed)
**When** the exception is raised
**Then** it is caught, a WARNING is logged with the exception type, the cache stores `[]` for that query hash, and the function returns `""`. **No exception escapes** (NFR-REL-1).

**Given** `provider.system_prompt_block(**kwargs)` is called
**When** the prefetch cache has an entry for the latest query
**Then** the block is composed of (a) top-K formatted hits + (b) a one-paragraph project-context summary derived from hit topics. **No second `cli_runner` call is made** (NFR-PERF-4); verified by counting `cli_runner.run_recall` invocations in tests.

**Given** `provider.sync_turn(user_content, assistant_content, **kwargs)`
**When** called
**Then** it invokes `mapping.detect_triggers`, then for each task calls `self._write_queue.put_nowait(task)`. On `queue.Full` it calls a rate-limited `_warn_overflow_once()` and drops. The function returns within p95 < 5 ms (verified by a benchmarking test that runs it 1000× and asserts the p95 measurement). **No exception escapes**.

**Given** the daemon worker is running
**When** a task is enqueued
**Then** the worker pulls it (FIFO) and calls `cli_runner.run_store(task.topic, task.content, task.importance, db_path, timeout_ms=config.command_timeout_write_ms, keywords=task.keywords)`. On any exception the worker logs WARNING and continues; **the worker does not die**.

**Given** the worker thread does die
**When** the next `put_nowait` finds `not worker.is_alive()`
**Then** the worker is respawned exactly once per process; if it dies again `self._writes_disabled = True` and a CRITICAL log fires and subsequent enqueues no-op (NFR-REL-2).

**Given** `provider.on_session_end(messages, **kwargs)` is called
**When** the queue has pending items
**Then** the method waits up to `config.session_end_grace_ms` for the worker to drain. Items remaining at the deadline are dropped with one WARNING, and the method returns within `grace + 100 ms` of being called (FR5).

**Files touched:**

- `hermes_icm_memory/hooks.py` (NEW — module hosts the four hook callables; `IcmMemoryProvider` from S07 binds them as methods, OR they live as standalone functions that the provider delegates to. Either pattern is acceptable as long as the public surface is `provider.prefetch(...)`, etc.)
- `tests/test_hooks.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_prefetch_calls_run_recall_with_config_limit_and_timeout`.
2. `test_prefetch_caches_result_for_block`.
3. `test_prefetch_swallows_icm_not_found_returns_empty`.
4. `test_prefetch_swallows_timeout_returns_empty`.
5. `test_prefetch_swallows_malformed_returns_empty`.
6. `test_system_prompt_block_reads_cache_no_second_subprocess`.
7. `test_system_prompt_block_formats_top_k_plus_summary`.
8. `test_sync_turn_enqueues_each_detected_trigger`.
9. `test_sync_turn_p95_under_5ms` — 1000 invocations with mocked queue.put_nowait; assert p95 latency < 5 ms.
10. `test_sync_turn_overflow_drops_with_one_warning_per_burst`.
11. `test_sync_turn_swallows_exceptions` — patch mapping to raise; assert sync_turn returns None and warns.
12. `test_worker_drains_fifo_order` — enqueue [A, B, C]; assert run_store called in that order.
13. `test_worker_survives_run_store_exception` — first run_store raises; second succeeds; worker still alive.
14. `test_worker_respawn_once` — kill worker; enqueue → respawn; kill again → degrade-disabled.
15. `test_on_session_end_drains_within_grace`.
16. `test_on_session_end_drops_remaining_with_warning`.

### Story 4.2: LLM-facing tools (`icm_recall`, `icm_store`, `icm_topics`, `icm_health`)

**ID:** S09 · **Effort:** M · **Dependencies:** S04 (cli_runner), S07 (provider — for queue access).

As an LLM running inside Hermes,
I want four tools (`icm_recall`, `icm_store`, `icm_topics`, `icm_health`) that I can call from inside a turn,
So that I can drive memory operations explicitly when heuristics alone aren't enough (FR8, FR11, FR13, FR17).

**Acceptance Criteria:**

**Given** `provider.get_tool_schemas()`
**When** called after this story
**Then** it returns four schemas with names exactly `icm_recall`, `icm_store`, `icm_topics`, `icm_health`; each has a clear description and a `parameters` JSON schema matching the PRD's tool-surface table (§8.6).

**Given** `provider.handle_tool_call("icm_recall", {"query": "what does Nikos prefer for package managers?"})`
**When** invoked
**Then** the handler validates the args, calls `cli_runner.run_recall(...)`, parses the result, returns `json.dumps({"hits": [...]})`. On any failure returns `json.dumps({"hits": []})` and logs WARNING.

**Given** `provider.handle_tool_call("icm_store", {"topic":"preferences","content":"Always use bun"})`
**When** invoked
**Then** the handler builds a write task, enqueues it via the provider's `_write_queue.put_nowait`, returns `json.dumps({"accepted": True, "queued_at": "<iso>"})` immediately. The handler completes p95 < 5 ms (verified the same way as S08's sync_turn benchmark).

**Given** `provider.handle_tool_call("icm_topics", {})`
**When** invoked
**Then** returns `json.dumps({"topics": [...]})` from `cli_runner.run_topics(...)`; on failure returns `json.dumps({"topics": []})` + WARNING.

**Given** `provider.handle_tool_call("icm_health", {})` or with `{"topic": "preferences"}`
**When** invoked
**Then** returns `json.dumps({"report": {...}})`; on failure returns `json.dumps({"report": {}})` + WARNING.

**Given** **any** tool handler
**When** anything goes wrong (bad args, subprocess failure, JSON parse fail)
**Then** the return is `json.dumps(...)` (string), never a dict, and **no exception escapes** (FR19).

**Files touched:**

- `hermes_icm_memory/tools.py` (NEW — pure functions; provider's `handle_tool_call` dispatches to them passing in the provider state needed)
- `tests/test_tools.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_get_tool_schemas_has_four_with_correct_names`.
2. `test_each_schema_has_required_keys` — `name`, `description`, `parameters` (with `type`, `properties`, `required`).
3. `test_recall_returns_json_string_with_hits_key`.
4. `test_recall_failure_returns_empty_hits_and_warns`.
5. `test_store_enqueues_and_returns_immediately`.
6. `test_store_p95_under_5ms`.
7. `test_store_returns_accepted_true_with_iso_timestamp`.
8. `test_store_invalid_args_returns_error_json` — missing `topic` → `json.dumps({"error": "..."})`, no enqueue.
9. `test_topics_returns_topics_key`.
10. `test_topics_failure_returns_empty_topics`.
11. `test_health_no_topic`.
12. `test_health_with_topic_arg`.
13. `test_health_failure_returns_empty_report`.
14. `test_unknown_tool_name_returns_error_json` — handler dispatches unknown name → `json.dumps({"error": "unknown tool ..."})`.
15. `test_no_tool_returns_dict` — every handler's return type is `str`, asserted by `isinstance`.
16. `test_no_tool_raises` — every test above also asserts no exception across the boundary.

---

## Epic 5: Quality guardrails & integration

**Goal:** Architectural invariants enforced by AST/grep tests, profile-isolation guarantees, complete failure-mode matrix, and integration tests that prove the cross-tool memory-sharing claim against a real `icm` binary.

### Story 5.1: Architectural invariant tests (subprocess, dot-cache, network)

**ID:** S11 · **Effort:** S · **Dependencies:** S01.

As a maintainer,
I want CI to fail if anyone (a) imports `subprocess` outside `cli_runner.py`, (b) hardcodes `~/.hermes`, or (c) opens a socket during plugin lifecycle methods,
So that NFR-MAINT-2, FR2, and NFR-SEC-1 cannot regress silently.

**Acceptance Criteria:**

**Given** any source file under `hermes_icm_memory/`
**When** the test scans every `.py` file
**Then** the only file that imports `subprocess` (via either `import subprocess` or `from subprocess import ...`) is `cli_runner.py`. The test parses with the `ast` module, not regex.

**Given** any source file under `hermes_icm_memory/`
**When** the test scans for the string literal `"~/.hermes"`
**Then** no occurrence is found (excluding the test files themselves).

**Given** the provider's lifecycle methods (`is_available`, `get_config_schema`, `save_config`)
**When** invoked under a `socket.socket` patch that raises on construction
**Then** none raises — proving no socket is created during these methods (NFR-SEC-1 invariant).

**Files touched:**

- `tests/test_no_subprocess_outside_cli_runner.py` (NEW)
- `tests/test_no_hardcoded_dotcache.py` (NEW)
- `tests/test_no_network_calls.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_only_cli_runner_imports_subprocess` — AST walk; assert offending files list is empty.
2. `test_no_dotcache_literal_in_source` — read every `*.py` under `hermes_icm_memory/`; assert `"~/.hermes"` not in content.
3. `test_is_available_no_socket` — patch `socket.socket` to raise; assert no exception.
4. `test_get_config_schema_no_socket` — same patch; assert no exception.
5. `test_save_config_no_socket` — same patch; assert no exception.

### Story 5.2: Profile isolation tests

**ID:** S12 · **Effort:** S · **Dependencies:** S05, S07.

As a Hermes user with multiple profiles,
I want a test that proves two profiles get two distinct DB paths and never read each other's data,
So that I can trust my "work" and "personal" memories don't bleed (FR2, NFR-SEC-2, SM5).

**Acceptance Criteria:**

**Given** two providers initialized with `hermes_home="/tmp/hh-A"` and `hermes_home="/tmp/hh-B"`
**When** examined
**Then** their `_db_path` values are distinct and live under their respective `hermes_home` directories.

**Given** the same with `profile="work"` and `profile="personal"` under one shared `hermes_home`
**When** examined
**Then** `_db_path` values are `<hh>/icm/work.db` and `<hh>/icm/personal.db` and are distinct.

**Given** an integration test (skipped if `icm` not on PATH) that writes a memory through provider A and then performs `recall` through provider B against the *other* DB
**When** run
**Then** provider B sees zero hits for that memory.

**Files touched:**

- `tests/test_profile_isolation.py` (NEW)

**Test plan (TDD; tests first):**

1. `test_two_hermes_homes_two_dbs`.
2. `test_two_profiles_one_hermes_home_two_dbs`.
3. `test_no_cross_profile_recall_leak` (integration; gated on real `icm`).
4. `test_db_path_inside_hermes_home_only` — assert `db_path.is_relative_to(Path(hermes_home).resolve())`.

### Story 5.3: Failure-mode degrade matrix

**ID:** S13 · **Effort:** M · **Dependencies:** S04, S07, S08, S09.

As a Hermes user,
I want every documented ICM failure mode to degrade silently (log + empty/drop) without raising into the agent turn,
So that a missing or misbehaving `icm` never crashes my session (FR19, NFR-REL-1).

**Acceptance Criteria:**

**Given** each of the eight failure modes from architecture §6.3
**When** simulated by mocking `subprocess.run` accordingly
**Then** the corresponding plugin entry-point (recall handler, store enqueue, topics handler, health handler, `prefetch`, `sync_turn`, `on_session_end`) returns the documented degraded result, logs WARNING **once** for that burst, and **does not raise**.

**Given** a stress sub-test
**When** the same failure is injected on 100 successive calls
**Then** the test asserts no exception escapes any of the 100 calls.

**Failure modes covered (one parametrized test family):**

1. `icm` not on PATH (`shutil.which` → None).
2. `icm` exits non-zero.
3. `icm` raises `TimeoutExpired`.
4. `icm` stdout malformed JSON.
5. `icm` first-call slow path (succeeds eventually) — no degrade, but elapsed-ms log emitted at INFO once.
6. Worker thread dies once (lazy respawn).
7. Worker thread dies twice (degrade-to-drop).
8. `hermes_home` parent unwritable.

**Files touched:**

- `tests/test_errors_and_degrade.py` (NEW)

**Test plan (TDD; tests first):**

- Eight parametrized test cases, one per failure mode, each asserting:
  - the documented return shape (e.g. `{"hits": []}`),
  - the documented log emission (WARNING / CRITICAL / INFO at the right level),
  - no exception escapes.
- One stress test: each failure injected 100× → no escape, single WARNING per burst.

### Story 5.4: Integration tests against a real `icm` binary

**ID:** S14 · **Effort:** M · **Dependencies:** S07, S08, S09, S10.

As a maintainer,
I want integration tests that exercise the real `icm` binary against a `tmp_path`-bound DB to verify cross-session recall, cross-tool sharing, and `sync_turn` stress behavior,
So that the unit-mocked claims are backed by end-to-end evidence (FR12, FR15, SM2, SM3).

**Acceptance Criteria:**

**Given** `tests/integration/test_real_icm_recall.py`
**When** the test writes a memory via the plugin's `icm_store` tool path (which enqueues; the test then drains the queue), then invokes the plugin's `icm_recall` tool
**Then** the recall returns a hit matching the stored content. The test uses `--no-embeddings` to avoid model-download flakiness in CI, and asserts on keyword-search hits.

**Given** `tests/integration/test_real_icm_cross_tool.py`
**When** the test invokes `icm` directly via `subprocess` (simulating a Claude Code write) against the *same* DB path the provider uses, then invokes `provider.handle_tool_call("icm_recall", ...)`
**Then** the plugin sees the externally-written memory.

**Given** `tests/integration/test_sync_turn_stress.py`
**When** the test enqueues 2× the queue capacity in rapid succession via `provider.sync_turn`
**Then** (a) FIFO order is preserved among accepted items, (b) exactly one WARNING is logged for the overflow burst, (c) no exception escapes any of the calls, (d) the eventually-drained `icm` DB contains exactly the accepted items.

**Given** any integration test
**When** run on a host without `icm` on PATH
**Then** the entire file is skipped at collection time with a clear reason.

**Files touched:**

- `tests/integration/__init__.py` (NEW — empty)
- `tests/integration/test_real_icm_recall.py` (NEW)
- `tests/integration/test_real_icm_cross_tool.py` (NEW)
- `tests/integration/test_sync_turn_stress.py` (NEW)

**Test plan (TDD; tests first; tests in this story ARE the implementation — they exercise existing code from earlier stories):**

1. `test_real_icm_recall.py::test_store_then_recall_returns_hit` — write via plugin → drain → recall via plugin → assert hit.
2. `test_real_icm_cross_tool.py::test_external_write_visible_to_plugin` — write directly with `subprocess.run(["icm", "store", ...])` → recall via plugin → assert hit (FR12).
3. `test_sync_turn_stress.py::test_overflow_fifo_warning_no_exception` — enqueue 2× capacity → assert FIFO + single WARNING + no exception escape + correct number of stored memories on drain.
4. Module-level skip logic: `pytestmark = pytest.mark.skipif(shutil.which("icm") is None, reason="icm not on PATH")` at top of each file.

---

## Story-to-story File Conflict Matrix

| Story | Modifies/creates files                                                                 | Conflicts with                                |
|-------|----------------------------------------------------------------------------------------|-----------------------------------------------|
| S01   | `pyproject.toml`, `plugin.yaml`, `__init__.py` (stub), `_version.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_plugin_loader.py` | (root)                                        |
| S02   | `.github/workflows/ci.yml`                                                             | none                                          |
| S03   | `README.md`, `CONTRIBUTING.md`                                                         | none                                          |
| S04   | `errors.py`, `cli_runner.py`, `tests/test_cli_runner.py`                               | none                                          |
| S05   | `config.py`, `tests/test_config.py`                                                    | none                                          |
| S06   | `mapping.py`, `tests/test_mapping.py`                                                  | none                                          |
| S07   | `provider.py`, `tests/test_provider.py`                                                | none                                          |
| S08   | `hooks.py`, `tests/test_hooks.py`                                                      | none                                          |
| S09   | `tools.py`, `tests/test_tools.py`                                                      | none                                          |
| S10   | `__init__.py` (MODIFY)                                                                 | **S01** (sequential)                          |
| S11   | `tests/test_no_subprocess_outside_cli_runner.py`, `tests/test_no_hardcoded_dotcache.py`, `tests/test_no_network_calls.py` | none                                          |
| S12   | `tests/test_profile_isolation.py`                                                      | none                                          |
| S13   | `tests/test_errors_and_degrade.py`                                                     | none                                          |
| S14   | `tests/integration/__init__.py`, three integration test files                          | none                                          |

**Only conflict:** S01 → S10 both touch `__init__.py`. They are already sequential by dep-graph.

---

## Sprint plan

### Sprint 1 — MVP (the spine)

**Stories:** S01, S04, S05, S06, S07, S08, S09, S10. Eight stories.
**Goal:** End-to-end: `register(ctx)` → real provider → real hooks → real tools → bounded-queue worker. Passing unit tests. The plugin works end-to-end against mocked `icm`.

**Wave order within Sprint 1:**

- Wave 0: S01 (sequential).
- Wave 1: parallel = { S04, S05, S06 }.
- Wave 2: S07 (depends on S04 + S05).
- Wave 3: parallel = { S08, S09 } (both depend on S04 + S07; S08 also on S06).
- Wave 4: S10 (touches `__init__.py`; sequential).

### Sprint 2 — Polish (CI + invariants + integration)

**Stories:** S02, S03, S11, S12, S13, S14. Six stories.
**Goal:** CI green on Python 3.11 + 3.12 with 85 % coverage + ruff + mypy gates; AST/grep invariants enforced; profile isolation proven; cross-tool sharing verified end-to-end against real `icm`; failure-mode matrix exhaustive.

**Wave order within Sprint 2:**

- Wave 1: parallel = { S02, S03, S11 } (all depend only on S01).
- Wave 2: parallel = { S12, S13, S14 } (after Sprint 1 spine is merged).

### Why this ordering

- Sprint 1 prioritizes the *user-visible* spine (a working plugin). Skeleton and CI scaffolding (S02, S03) are deferred to Sprint 2 because the existing local `pytest` invocation is sufficient to validate Sprint 1 stories — CI green is a Sprint-2 deliverable.
- Pulling integration + guardrail tests into Sprint 2 isolates "broad assertions about the codebase" (S11, S13, S14) from per-module unit work, so flakes and quirks are diagnosed at sprint boundaries rather than inside per-story dev cycles.
- All 14 stories together complete v1; SM1–SM8 (acceptance + quality) are all covered.
