---
stepsCompleted:
  - step-01-init
  - step-02-discovery
  - step-02b-vision
  - step-02c-executive-summary
  - step-03-success
  - step-04-journeys
  - step-05-domain
  - step-06-innovation
  - step-07-project-type
  - step-08-scoping
  - step-09-functional
  - step-10-nonfunctional
  - step-11-polish
  - step-12-complete
inputDocuments:
  - _bmad-output/planning-artifacts/product-brief.md
workflowType: 'prd'
project_name: hermes-icm-memory
projectType: greenfield-oss-plugin
license: Apache-2.0
---

# Product Requirements Document — hermes-icm-memory

**Author:** Nikos (BMAD Phase 2 — Planner) · **Date:** 2026-05-05 · **Status:** Approved · **License:** Apache-2.0

This PRD operationalizes the approved [Product Brief](./product-brief.md). The Brief's locked decisions (name, license, language, integration approach via ICM CLI shellouts, profile isolation contract, non-blocking `sync_turn`, five mandatory store triggers, ≥85% pytest coverage, ruff + mypy --strict) are carried forward verbatim and are not relitigated here.

---

## 1. Executive Summary

`hermes-icm-memory` is a [Hermes Agent](https://hermes-agent.nousresearch.com/) memory provider plugin that uses [ICM](https://github.com/rtk-ai/icm) (Infinite Context Memory) as its backing store. It registers a `MemoryProvider` named `icm` via `ctx.register_memory_provider(...)`, satisfies the full Hermes memory-provider lifecycle contract (`save_config` / `get_tool_schemas` / `handle_tool_call`, plus `prefetch` / `system_prompt_block` / `sync_turn` / `on_session_end` hooks), and shells out to the local `icm` CLI via `subprocess` for every memory operation. There is no daemon, no embedded SQLite client, no embedding service to manage — `icm` already owns all of that.

The plugin closes the editor-agent memory island problem: a decision a user records in Claude Code via `/remember`, or that ICM auto-extracts from a coding session, becomes immediately recallable in the next Hermes session — same SQLite database, no sync layer, no service to run. Conversely, anything Hermes captures via the five mandatory store triggers (errors-resolved, decisions-{project}, preferences, context-{project}, periodic progress) is searchable from every editor that ICM already integrates with (Claude Code, Cursor, OpenCode, Codex CLI, Copilot CLI, Gemini, Windsurf).

**Primary differentiators** (vs. mem0, Letta, Honcho, holographic, in-process default):

- **Shared memory across editors and agents** — not a parallel silo per tool.
- **No service to operate** — ICM CLI is a single binary; the plugin shells out.
- **Decay model + hybrid recall + consolidation built in** — inherited from ICM, not reimplemented.
- **Cross-platform by inheritance** — ICM ships native binaries for x86_64 + aarch64 (Pi 4 verified), Linux + macOS.
- **Apache-2.0, no vendor lock-in.** Hermes-side adapter is thin and replaceable; ICM is open source upstream.

The honest moat is execution discipline: hooks must be wired exactly to the Hermes contract, `sync_turn` must never block a turn, `is_available()` must never make a network call, profile isolation must derive every path from `kwargs['hermes_home']`, and the topic/type mapping must produce real ICM entries with the right importance — not JSON dumps of conversation turns.

---

## 2. Success Criteria

These criteria map 1-to-1 to the Brief's Success Criteria and become the acceptance bar for v1 release.

### 2.1 Functional acceptance (must-pass)

- **SM1 — Setup works on a real machine.** `hermes memory setup icm` succeeds end-to-end on a host with `icm` on PATH; `is_available()` returns `False` (with a helpful, actionable message) when `icm` is not installed or not on PATH. Maps to FR1, FR3.
- **SM2 — Cross-session recall.** A Hermes session that runs, exits, and restarts can recall a fact stored in the previous session via (a) the LLM's `icm_recall` tool call AND (b) auto-injected `system_prompt_block` content. Maps to FR8, FR12, FR15.
- **SM3 — Cross-tool recall.** A decision recorded by the user in Claude Code (`/remember` or auto-extract) is recallable in a fresh Hermes session — same ICM SQLite DB, no sync layer, no migration step. Maps to FR8, FR12.
- **SM4 — Non-blocking sync_turn.** `sync_turn` never blocks the agent turn. p95 added latency from the plugin (turn-with-plugin minus turn-without-plugin) is **< 50 ms** measured on the Hermes turn benchmark. Maps to NFR-PERF-1, NFR-PERF-2.
- **SM5 — Profile isolation.** Two Hermes profiles produce two distinct ICM databases under `hermes_home/icm/<profile>.db`. No reads or writes leak between them under any code path. Maps to FR2, NFR-SEC-2.

### 2.2 Quality (release gate)

- **SM6 — Test coverage.** Pytest coverage ≥ **85 %** for the `hermes_icm_memory` package. CI fails the PR if coverage drops below this floor. Maps to NFR-REL-3.
- **SM7 — Static-analysis clean.** Zero warnings on `ruff check` (project ruleset) and zero errors on `mypy --strict`. Documented `# noqa` / `# type: ignore` accepted only with an inline reason and an issue link. Maps to NFR-REL-4.
- **SM8 — Quickstart.** README ships a 3-step quickstart. A new user can install, register the provider, and verify recall in **< 5 minutes** measured on a clean machine with `icm` already installed.

### 2.3 Adoption (post-launch, v1 → v1.1)

- **SM9 — External engagement.** At least one external user opens an issue or PR within 30 days of the v1 release tag.
- **SM10 — Listed in the Hermes plugin docs / community plugin index.

---

## 3. User Journeys

Three journeys frame the user-facing capability surface and back-feed the FRs.

### 3.1 Journey A — First-time setup (the local-AI hobbyist)

1. User has Hermes installed and `icm` on PATH. User runs `pip install hermes-icm-memory` (or drops the plugin into `~/.hermes/plugins/hermes-icm-memory/`).
2. User runs `hermes plugins enable hermes-icm-memory`, then `hermes memory setup icm`.
3. Plugin's `is_available()` returns true; `initialize(session_id, hermes_home=...)` derives the DB path under `<hermes_home>/icm/<profile>.db` and calls `icm init` if the DB doesn't exist yet.
4. User starts a Hermes session. The system prompt block now includes a "Memory" section seeded from `wake-up`-style top-K recall against the project. Session feels warmer than cold-start.
5. **Verification path.** User asks the agent "what do you remember about my Hermes setup?" → tool call `icm_recall` returns hits → session demonstrates continuity.

### 3.2 Journey B — Cross-session continuity (the plugin author)

1. Day 1, Hermes session A: agent resolves a tricky import error. `sync_turn` fires the `errors-resolved` trigger → daemon thread runs `icm store -t errors-resolved -c "..." -i high -k "..."` non-blockingly.
2. Day 1, evening: user is back in Claude Code; via the editor-side ICM hooks, asks "did I fix that import error?" → recall returns the Hermes-stored memory.
3. Day 2, fresh Hermes session B: `prefetch` runs against the recent user turn, `system_prompt_block` injects the matching memory before the LLM sees the turn. Agent acts informed without an explicit tool call.

### 3.3 Journey C — Explicit memory drive (any user)

1. User asks the agent to remember a preference: "always use bun, never npm". Agent calls `icm_store` with topic `preferences` and importance `critical`.
2. User asks for a topic dump: agent calls `icm_topics` → returns the topic list → optionally narrows with `icm_recall(topic="preferences")`.
3. User asks for a memory health check: agent calls `icm_health` → returns staleness/consolidation report → agent surfaces actionable suggestions.

---

## 4. Domain & Compliance

Single-user local-AI tooling. **No GDPR / HIPAA / PCI-DSS surface.** No data leaves the host: every byte the plugin writes lives in `hermes_home/icm/<profile>.db` under the user's home (or wherever Hermes points). No telemetry. No analytics call-outs. No network I/O on any code path that this plugin owns; if `icm` itself ever performs a network call (e.g. for embeddings model download), that is `icm`'s contract with the user, not ours, and the plugin must not assume a network is available at runtime.

The only legal constraint we own is **license compatibility**: the plugin is Apache-2.0; `icm` is open source upstream; Hermes Agent is open source. No proprietary dependencies admitted.

---

## 5. Innovation Patterns

The novel patterns this product introduces or operationalizes:

- **Editor-agent shared memory via a CLI-backed adapter.** Other Hermes memory providers either are in-process (lose state on exit), service-backed (extra ops surface), or vendor-locked (mem0, Letta). None share a single SQLite store with the user's editor sessions today.
- **Non-blocking write commit pattern with bounded queue + daemon thread.** The plugin commits to never adding turn-perceptible latency. The pattern: a single daemon-mode `threading.Thread` consumes a bounded `queue.Queue` of write tasks; producers (the `sync_turn` hook + the `icm_store` tool handler) enqueue with a non-blocking put and degrade-on-full (drop-with-log) rather than apply backpressure to the agent.
- **Topic/type/importance heuristic mapping** that mirrors the editor-side ICM rules so a Hermes "decision" becomes a real `decisions-{project}` ICM entry (not a JSON conversation dump). Five mandatory triggers, all driven by `sync_turn` content sniffing + explicit tool-call paths.
- **Failure-degrades-silently policy.** If `icm` is not on PATH, exits non-zero, hangs past timeout, or emits malformed stdout, the plugin logs once at WARNING, returns an empty result (recall) or drops the work (write), and **never raises into the agent's turn**. A crashing memory provider would disable itself per Hermes contract; this is worse for the user than degraded memory, so we degrade explicitly.

---

## 6. Project-Type Requirements

**Project type:** Greenfield Python OSS plugin, single repository, single package.

- **Language / runtime:** Python 3.11+ (matches Hermes plugin runtime; no Python 3.10 backport).
- **Package layout:** `pip install hermes-icm-memory`. Distribution via PyPI entry-point group `hermes_agent.plugins`. Also installable as a project-local `~/.hermes/plugins/hermes-icm-memory/` drop-in.
- **Source layout (capability surface, full structure decided in Architecture):** `hermes_icm_memory/{__init__.py, provider.py, cli_runner.py, tools.py, hooks.py, config.py, errors.py}` plus `tests/` (unit + integration).
- **Test stack:** pytest, pytest-cov, `unittest.mock` for subprocess mocking, real `icm` invocation for integration tests against `tmp_path`.
- **Static analysis:** ruff (project default ruleset), mypy `--strict`. Pre-commit not mandated for v1 but recommended.
- **CI:** GitHub Actions on every PR. Matrix: Python 3.11, 3.12, on `ubuntu-latest`. Steps: install, ruff, mypy, pytest with coverage threshold gate.
- **Repo / hosting:** GitHub user `ta3pks`; repo `hermes-icm-memory`; default branch `main`; Apache-2.0 LICENSE; CONTRIBUTING.md minimal; CODE_OF_CONDUCT optional for v1.
- **Distribution channels:** PyPI (primary), GitHub Releases (tagged), Hermes plugin community index entry (post-launch).
- **External dependencies:** `icm` CLI on PATH (runtime). No Python package depends on `icm` directly. Python deps: only what Hermes Agent already requires for plugins (no new runtime deps if avoidable; `subprocess` and `threading` are stdlib).

---

## 7. Scope

### 7.1 In scope (v1)

- `IcmMemoryProvider(MemoryProvider)` class registered via `ctx.register_memory_provider(...)`.
- `is_available()` — checks `icm` on PATH via `shutil.which`. Pure local check, no network.
- `initialize(session_id, hermes_home, **kwargs)` — derives `<hermes_home>/icm/<profile>.db` (profile from `hermes_home`/session context per Hermes contract); idempotently runs `icm init --db <path>` once if the DB doesn't exist.
- `get_tool_schemas()` + `handle_tool_call(name, arguments)` — exposes four LLM-callable tools: `icm_recall`, `icm_store`, `icm_topics`, `icm_health`. Handlers always return `json.dumps(...)`, never raise, never return a dict.
- `get_config_schema()` + `save_config()` — user-tunable config (importance default, topic prefix, recall limit, prefetch on/off, sync-write queue size, command timeout).
- Hooks (declared in `plugin.yaml`):
  - `prefetch` — semantic recall against the recent turn content (uses `icm recall <query> --limit K --format json`).
  - `system_prompt_block` — injects top-K recalled memories + a project-context summary (uses `icm wake-up` style or `icm recall-context`).
  - `sync_turn` — non-blocking enqueue of detected store triggers; daemon thread drains the queue and runs `icm store ...`.
  - `on_session_end` — flush the queue with a bounded grace period; optional lightweight `icm consolidate` on configured topics.
- Trigger detection: errors-resolved, decisions-{project}, preferences, context-{project}, periodic progress (~ every N turns). Identical heuristics to the editor-side ICM rules.
- Apache-2.0 LICENSE, README with 3-step quickstart, minimal CONTRIBUTING.md, GitHub Actions CI.
- Test suite: unit tests with subprocess mocked (`unittest.mock`), integration tests with a real `icm` binary against `tmp_path`.

### 7.2 Out of scope (v1)

- ICM MCP-server transport (`icm serve`). v2 concern.
- Multi-user / shared-DB / multi-tenant deployments. ICM is single-user by design.
- Web UI / dashboard for browsing memories. ICM's own CLI suffices.
- Custom embedding models. ICM's default (`multilingual-e5-base`) is fine; user configures ICM separately if they want a different one.
- Migration tooling from other providers (mem0, Letta, Honcho).
- Synchronous turn writes. We are committed to non-blocking from day 1.
- Cross-machine sync. Same machine only; if the user wants sync, that is an ICM-level concern.

---

## 8. Functional Requirements

The capability contract for v1. Every story in the Epics & Stories doc must trace back to one or more FRs here. Capabilities are stated implementation-agnostically; the Architecture document picks the implementation.

### 8.1 Plugin lifecycle & registration

- **FR1.** The plugin can be installed via `pip install hermes-icm-memory` and via project-local `~/.hermes/plugins/hermes-icm-memory/` drop-in. It registers a memory provider named `icm` when Hermes loads it.
- **FR2.** The plugin can derive its database path from `kwargs['hermes_home']` on `initialize(session_id, hermes_home, **kwargs)`. Different `hermes_home` values produce different database files; no hardcoded `~/.hermes` references exist anywhere in the codebase.
- **FR3.** The plugin can report availability via `is_available()` without any network I/O. It returns `True` only if the `icm` binary is on PATH and is executable.
- **FR4.** The plugin can initialize a fresh ICM database idempotently — running `initialize` twice does not corrupt or duplicate state, and does not run `icm init` redundantly when the DB already exists.
- **FR5.** The plugin can shut down cleanly on `on_session_end`, flushing any in-flight non-blocking writes within a bounded grace period and not blocking session teardown beyond that.

### 8.2 Configuration

- **FR6.** The user can override default importance, default topic prefix, recall limit (top-K for prefetch / system prompt block), prefetch on/off, sync-write queue capacity, and ICM command timeout via `get_config_schema` / `save_config`.
- **FR7.** The plugin can validate user-supplied config and reject invalid values with an actionable error returned from `save_config` (no raise into the agent turn).

### 8.3 Memory recall (read path)

- **FR8.** The LLM can call the `icm_recall` tool with `{query, topic?, limit?, project?}` and receive a JSON-encoded list of memory hits (id, score, topic, importance, summary).
- **FR9.** The plugin can perform a `prefetch` recall at the appropriate Hermes hook point against recent-turn content; results are cached for the immediately following `system_prompt_block` injection.
- **FR10.** The plugin can return a `system_prompt_block` string composed of (a) top-K prefetched memories formatted for prompt injection and (b) a compact project-context summary, suitable for direct concatenation into Hermes's system prompt.
- **FR11.** The LLM can call the `icm_topics` tool with no arguments and receive the current list of ICM topics (useful for discovery and narrowing follow-up recalls).
- **FR12.** Recall paths can read memories that were stored by any other ICM client (Claude Code, Cursor, etc.) on the same machine, provided they share the same DB path.

### 8.4 Memory write (store path)

- **FR13.** The LLM can call the `icm_store` tool with `{topic, content, importance?, keywords?, raw?}` to record a memory; the call returns immediately with an acknowledgement and never blocks the turn.
- **FR14.** The plugin can detect the five mandatory triggers in `sync_turn` (errors-resolved, decisions-{project}, preferences, context-{project}, periodic progress) and enqueue a corresponding `icm store` task without blocking.
- **FR15.** Writes enqueued under load are committed to ICM in FIFO order by a single daemon worker. When the bounded queue is full, new writes are dropped (with a single WARNING log per overflow burst); the agent turn is never throttled.
- **FR16.** The plugin can map Hermes-side memory categories to ICM `topic` + `importance` per the documented matrix (decisions↔`decisions-{project}` high, errors↔`errors-resolved` high, preferences↔`preferences` critical, context↔`context-{project}` high, learnings↔`learnings` high).

### 8.5 Health & observability

- **FR17.** The LLM can call the `icm_health` tool and receive the ICM staleness/consolidation report scoped to the current DB.
- **FR18.** The plugin can emit structured logs (Python `logging`, namespaced under `hermes_icm_memory`) for every subprocess invocation, every trigger detection, every queue overflow, and every degraded path, at appropriate levels (DEBUG / INFO / WARNING).
- **FR19.** The plugin can degrade silently on any ICM failure mode (`icm` not on PATH, non-zero exit, timeout, malformed stdout): log once at WARNING, return empty result for reads / drop the work for writes, never raise into the turn.

### 8.6 LLM tool surface (summary)

| Tool         | Direction | Args                                              | Returns (JSON-encoded string)                    |
|--------------|-----------|---------------------------------------------------|--------------------------------------------------|
| `icm_recall` | LLM→plugin | `query: str, topic?: str, limit?: int, project?: str` | `{hits: [{id, score, topic, importance, summary, ...}]}` |
| `icm_store`  | LLM→plugin | `topic, content, importance?, keywords?, raw?`     | `{accepted: true, queued_at: <iso>}`             |
| `icm_topics` | LLM→plugin | `(none)`                                          | `{topics: [...]}`                                |
| `icm_health` | LLM→plugin | `topic?: str`                                     | `{report: {...}}`                                |

---

## 9. Non-Functional Requirements

Selective — only the categories that materially apply to this product are listed.

### 9.1 Performance (NFR-PERF)

- **NFR-PERF-1.** `sync_turn` is non-blocking. The hook function returns within **5 ms (p95)** under nominal load (queue not full); enqueue uses non-blocking `queue.Queue.put_nowait` with overflow-drop.
- **NFR-PERF-2.** End-to-end p95 added latency from the plugin (turn-with-plugin minus turn-without-plugin) is **< 50 ms** on the Hermes turn benchmark. Includes any synchronous read paths (e.g. `prefetch`).
- **NFR-PERF-3.** Subprocess invocations to `icm` carry a configurable hard timeout (default **2000 ms** for reads, **5000 ms` for writes). Timeout triggers the degrade path (FR19) without blocking the caller beyond the timeout.
- **NFR-PERF-4.** `prefetch` results are cached for the immediately-following `system_prompt_block` call so the same recall is not run twice per turn.

### 9.2 Reliability (NFR-REL)

- **NFR-REL-1.** No code path raises an exception into the agent's turn loop. Every subprocess call, every JSON parse, every queue operation is wrapped in try/except with WARNING-level logging on the failure branch.
- **NFR-REL-2.** The non-blocking write daemon is a single `threading.Thread(daemon=True)` thread (one per provider instance). On worker death, the next enqueue attempt restarts it (lazy-respawn) at most once per process lifetime; subsequent failures degrade to drop-with-log.
- **NFR-REL-3.** Test coverage ≥ **85 %** for the `hermes_icm_memory` package (line + branch). CI fails if coverage drops below this floor on any PR.
- **NFR-REL-4.** Static analysis: zero `ruff check` warnings, zero `mypy --strict` errors. CI fails on any.
- **NFR-REL-5.** Idempotency: `initialize()`, queue startup, and `icm init` are all idempotent — calling them twice in the same process is safe and observable as a no-op on the second call.

### 9.3 Security (NFR-SEC)

- **NFR-SEC-1.** Zero network I/O originated by the plugin itself, on any code path including `is_available`. Verified by a unit test that asserts no socket calls during plugin lifecycle methods.
- **NFR-SEC-2.** Profile isolation: every database path derives from `kwargs['hermes_home']`. A unit + integration test pair proves no cross-profile leakage by running two profiles in the same process and asserting database paths and contents are disjoint.
- **NFR-SEC-3.** Subprocess argument passing uses list-form `subprocess.run([...])` with `shell=False`. No string concatenation into shell commands. User-supplied values (query, topic, content) are passed as discrete argv elements.
- **NFR-SEC-4.** No secrets handled, stored, or logged by the plugin. ICM databases are user-owned files; the plugin does not chmod, chown, or otherwise touch permissions.

### 9.4 Observability (NFR-OBS)

- **NFR-OBS-1.** All logging goes through Python's `logging` module under the `hermes_icm_memory` namespace. No `print()` calls in non-test code.
- **NFR-OBS-2.** Subprocess invocations log at DEBUG with command, redacted args, and elapsed time.
- **NFR-OBS-3.** Trigger detections, queue overflows, worker restarts, and degraded paths log at WARNING (overflows / restarts) or INFO (trigger detections).
- **NFR-OBS-4.** Health: the `icm_health` tool surfaces ICM's own staleness/consolidation report; the plugin does not maintain a parallel health system.

### 9.5 Maintainability (NFR-MAINT)

- **NFR-MAINT-1.** Public API surface frozen post-v1 release: `IcmMemoryProvider` class, four tool names (`icm_recall`, `icm_store`, `icm_topics`, `icm_health`), config schema keys. Breaking changes require a major version bump.
- **NFR-MAINT-2.** Architecture v2 is allowed to swap the `cli_runner` module to talk to ICM's MCP server (`icm serve`) without changing the public API surface in NFR-MAINT-1. The `provider.py` / `tools.py` / `hooks.py` modules MUST NOT import `subprocess` directly — only `cli_runner` does.
- **NFR-MAINT-3.** Type hints on every public function and class. mypy `--strict` clean (NFR-REL-4 is the test; this is the design requirement).

---

## 10. Traceability — Success Metrics → FRs / NFRs

| Success Metric | Maps to                                                         |
|----------------|-----------------------------------------------------------------|
| SM1 (setup)    | FR1, FR3, FR4                                                   |
| SM2 (cross-session recall) | FR8, FR9, FR10, FR12, FR15                          |
| SM3 (cross-tool recall)    | FR8, FR12, FR2, NFR-SEC-2                           |
| SM4 (non-blocking)         | FR13, FR14, FR15, NFR-PERF-1, NFR-PERF-2, NFR-REL-2 |
| SM5 (profile isolation)    | FR2, NFR-SEC-2                                      |
| SM6 (coverage ≥85%)        | NFR-REL-3                                           |
| SM7 (lint/type clean)      | NFR-REL-4, NFR-MAINT-3                              |
| SM8 (3-step quickstart)    | FR1, FR3 (covered by README story)                  |
| SM9, SM10 (adoption)       | Out-of-band — release & docs work                   |

---

## 11. Acceptance Criteria (per FR — testable bullets)

Acceptance criteria below are stated as testable bullets. They become the source for the Epics & Stories document's per-story test plans.

- **FR1.** `pip install` from a built sdist/wheel succeeds; `hermes plugins enable hermes-icm-memory` lists the plugin; project-local drop-in path also works (covered by an integration test that runs Hermes plugin discovery against a fixture dir).
- **FR2.** Two `initialize` calls with different `hermes_home` produce different DB paths, both under `<hermes_home>/icm/...`. No code path references `~/.hermes` literally — enforced by an AST/grep test.
- **FR3.** `is_available()` returns `False` and a usable message when `icm` is removed from PATH (test monkeypatches PATH); returns `True` when present. No socket created during the call (verified by a `socket.socket` patch).
- **FR4.** Calling `initialize` twice in the same process: second call is a no-op (no second `icm init` invocation), verified via subprocess mock call count.
- **FR5.** `on_session_end`: queue drains within configured grace window; if items remain, they are dropped with a WARNING log; method returns within `grace_window + ε`.
- **FR6 / FR7.** `save_config` accepts every documented key with valid values; rejects out-of-range / wrong-type values with a structured error response; never raises.
- **FR8.** `handle_tool_call("icm_recall", {...})` returns `json.dumps(...)` of the parsed `icm recall --format json` output; on subprocess failure returns `json.dumps({"hits": []})` and logs WARNING.
- **FR9 / FR10.** `prefetch` populates a per-turn cache; `system_prompt_block` reads from cache (no second subprocess call) and returns a string containing both recalled memories and a project context summary.
- **FR11.** `icm_topics` returns the list ICM emits.
- **FR12.** Integration test: write a memory via `icm store ...` directly (simulating Claude Code), then read via the plugin's `icm_recall` against the same DB — hit returned.
- **FR13.** `icm_store` tool handler enqueues and returns within p95 < 5 ms; the actual `icm store` subprocess runs on the daemon thread.
- **FR14.** Per-trigger unit tests assert `sync_turn` enqueues the right `(topic, importance, content)` for each of the five triggers given representative turn content.
- **FR15.** Stress test: enqueue 2× queue capacity rapidly; assert FIFO order on processed items, single WARNING per overflow burst, no exception, no lost work below capacity.
- **FR16.** Mapping unit test: each of the five Hermes-side categories produces the documented ICM `(topic, importance)` pair.
- **FR17.** `icm_health` returns the ICM health report JSON unchanged (or the empty `{}` on failure with WARNING).
- **FR18.** Log assertions: subprocess invocations, trigger detections, queue overflows, and degraded paths emit at the documented levels.
- **FR19.** Failure-mode matrix test: `icm` not on PATH / non-zero exit / hang past timeout / malformed stdout — for each, plugin returns the documented degraded result, logs WARNING once, and does not raise.

---

## 12. Risks & Mitigations

| Risk                                           | Mitigation                                                                                                  |
|------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| ICM CLI shape changes between releases         | Pin / minimum-version check at `is_available`; integration tests run against a known-good `icm` in CI.       |
| Subprocess overhead bloats p95 latency         | Cache prefetch results across hooks in same turn (FR9); read-path timeout 2 s; offload writes to daemon.     |
| Daemon thread silently dies                    | Lazy-respawn once per process (NFR-REL-2); WARNING log; degrade-with-drop after second death.                |
| User runs two Hermes profiles simultaneously and we accidentally share a DB | Profile-isolation test in CI (NFR-SEC-2); AST grep for hardcoded `~/.hermes`.       |
| `icm init` slow on first run (embedding model download) | Document in README; surface a INFO log; do not gate `is_available()` on it (model download is an `icm` concern). |
| MCP transport (v2) breaks the public API       | NFR-MAINT-1 freezes the API; `cli_runner` module is the only swap surface (NFR-MAINT-2).                     |

---

## 13. Out-of-Scope, Captured for Future

- ICM MCP transport via `icm serve` — v2 candidate. Public API stays stable per NFR-MAINT-1/2.
- Bidirectional `learn` / `feedback-record` integration so Hermes turns feed ICM's prediction-correction loop.
- A `hermes-icm-recall-only` lite / read-only mode for audit deployments.
- A small companion CLI `hermes-icm` so the user can ask "what did my Hermes agent remember from yesterday?" from a plain shell.

---

## 14. References

- Approved Product Brief: `_bmad-output/planning-artifacts/product-brief.md`
- Hermes plugin docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>
- Hermes memory provider docs: <https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin>
- ICM upstream: <https://github.com/rtk-ai/icm>
- ICM CLI command surface (v1 baseline used in this PRD): `icm store / recall / topics / health / init / wake-up / recall-context`.
