---
stepsCompleted:
  - step-01-init
  - step-02-context
  - step-03-starter
  - step-04-decisions
  - step-05-patterns
  - step-06-structure
  - step-07-validation
  - step-08-complete
inputDocuments:
  - _bmad-output/planning-artifacts/product-brief.md
  - _bmad-output/planning-artifacts/prd.md
workflowType: 'architecture'
project_name: hermes-icm-memory
user_name: Nikos
date: '2026-05-05'
---

# Architecture Decision Document — hermes-icm-memory

**Author:** Planner (BMAD Phase 2) · **Date:** 2026-05-05 · **Status:** Approved · **License:** Apache-2.0

This document operationalizes the approved [Product Brief](./product-brief.md) and [PRD](./prd.md). Locked decisions from those upstream artifacts (Python 3.11+, ICM CLI shellouts, profile isolation via `kwargs['hermes_home']`, non-blocking `sync_turn`, log+degrade failure policy, ≥85 % pytest coverage, ruff + `mypy --strict`) are carried forward and not relitigated. The reference scaffold this architecture mirrors is the local `hermes-rtk-hook` plugin (`/home/nikos/.hermes/plugins/hermes-rtk-hook/`), adapted from "hooks-only" to "memory provider".

---

## 1. Project Context

### 1.1 Product summary

A Hermes Agent **memory provider** plugin (`name = "icm"`) that delegates every storage operation to the local `icm` CLI binary via `subprocess`. No daemon, no embedded SQLite, no embedding service: ICM owns all of that. The plugin is the thinnest possible adapter.

### 1.2 Project type & scale

- **Type:** Greenfield Python OSS plugin (single repo, single PyPI package).
- **Scale:** Single-user local-AI tooling. Not multi-tenant. Single Hermes process per Hermes profile; profile isolation derives from `kwargs['hermes_home']`.
- **Distribution:** PyPI (entry-point group `hermes_agent.plugins`) + project-local drop-in at `~/.hermes/plugins/hermes-icm-memory/`.

### 1.3 Cross-cutting concerns

- **Performance ceiling:** p95 added latency from the plugin must be `< 50 ms` (PRD NFR-PERF-2). `sync_turn` returns within 5 ms p95 (NFR-PERF-1).
- **Reliability floor:** No code path raises into the agent's turn loop (NFR-REL-1). Every subprocess / JSON parse / queue op is try/except'd with WARNING-level logging on failure.
- **Security floor:** Zero network I/O originated by this plugin (NFR-SEC-1). All paths derive from `kwargs['hermes_home']` (NFR-SEC-2).
- **Maintainability floor:** Public API surface (class name, four tool names, config keys) is frozen post-v1; only the `cli_runner` module is allowed to swap shape for v2's MCP transport (NFR-MAINT-1, NFR-MAINT-2).

---

## 2. Starter Template & Reference Scaffold

### 2.1 Reference template

We do **not** start from a third-party generator. We mirror the structure of the locally-known-good Hermes plugin `hermes-rtk-hook` (path: `/home/nikos/.hermes/plugins/hermes-rtk-hook/`), which has the following shape:

```
hermes-rtk-hook/
├── plugin.yaml           # manifest
├── pyproject.toml        # PEP 621 metadata, py-modules, pytest config
├── __init__.py           # register(ctx) entry point
├── hook.py               # one hook callback
├── filter_map.py         # supporting pure logic (testable in isolation)
├── README.md
├── LICENSE
└── tests/
    ├── test_hook.py            # mock subprocess, fail-open behavior
    ├── test_filter_map.py      # pure-logic unit tests
    ├── test_integration.py     # real binary, end-to-end
    └── test_plugin_loader.py   # plugin.yaml + register(ctx) shape
```

Adaptations for our case (memory provider rather than hooks-only):

- Add a `provider.py` that subclasses `agent.memory_provider.MemoryProvider` and is registered via `ctx.register_memory_provider(...)`.
- Replace the single `hook.py` with a `hooks.py` module exposing four hook callbacks (`prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end`). All four are bound to provider methods (provider owns state).
- Add a `tools.py` module that implements four LLM-facing tools (`icm_recall`, `icm_store`, `icm_topics`, `icm_health`) per the PRD's tool-surface table.
- Add a `cli_runner.py` module that owns every `subprocess` call (NFR-MAINT-2: nothing else imports `subprocess`).
- Add `config.py`, `errors.py`, and `mapping.py` for separation of concerns.

### 2.2 Tooling choices (verified against current ecosystem, 2026-05)

| Tool                | Choice                  | Why                                                                                              |
|---------------------|-------------------------|--------------------------------------------------------------------------------------------------|
| Build backend       | `setuptools >= 68`      | Mirrors hermes-rtk-hook; no reason to deviate. PEP 621 metadata in `pyproject.toml`.             |
| Test runner         | `pytest >= 8`           | Already specified in PRD; matches reference scaffold.                                            |
| Coverage            | `pytest-cov` + `coverage` | Standard. Threshold gate at 85 % (line + branch) enforced by CI.                                |
| Lint                | `ruff` (project default ruleset) | PRD mandate.                                                                            |
| Type check          | `mypy --strict`         | PRD mandate.                                                                                     |
| Subprocess mocking  | `unittest.mock` (stdlib) | Matches reference scaffold's `test_hook.py` style.                                              |
| CI                  | GitHub Actions, Python 3.11 + 3.12 matrix | PRD mandate.                                                                  |
| License             | Apache-2.0              | Brief lock.                                                                                      |

---

## 3. Core Architectural Decisions

### 3.1 Critical decisions (block implementation)

| ID    | Decision                                                                 | Rationale                                                                                      |
|-------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| AD-01 | **Integration: ICM CLI shellouts via `subprocess.run([...], shell=False)`** | Battle-tested, no daemon, no MCP runtime dep, simple to mock. v2 may swap to MCP via `icm serve` without breaking AD-12 (cli_runner is the only swap point). |
| AD-02 | **Language / runtime: Python 3.11+**                                      | Hermes plugin runtime baseline. No 3.10 backport — we use `tomllib`, `Self`, and PEP 695 freely. |
| AD-03 | **Concurrency model for writes: single daemon `threading.Thread` + bounded `queue.Queue`** | Simplest non-blocking model that satisfies "never block a turn" and "FIFO order" simultaneously. `asyncio.create_task` rejected because Hermes's runtime model does not guarantee an active event loop in `sync_turn` callers. |
| AD-04 | **Bounded queue policy: drop-with-log on overflow (`put_nowait` + `queue.Full`)** | Backpressure into the agent turn would violate NFR-PERF-1. Single WARNING per overflow burst (rate-limited via a "burst flag" the worker clears after drain). |
| AD-05 | **Profile isolation: DB path = `<hermes_home>/icm/<profile>.db`**         | Hermes contract; satisfies FR2 + NFR-SEC-2. Profile name resolves from `kwargs['profile']` if Hermes provides it, else `"default"`. Path lives inside `hermes_home` so two profiles cannot collide. |
| AD-06 | **DB initialization: idempotent `Path(db).parent.mkdir(parents=True, exist_ok=True)` + first call to ICM creates the SQLite file** | `icm init` configures *Claude-Code integration*, not the DB itself; the DB auto-creates on first `icm store` / `icm recall` against `--db <path>` — verified against `icm --help`. We do **not** call `icm init` from this plugin. (Clarification of PRD FR4 wording.) |
| AD-07 | **Failure policy: log-once-at-WARNING, return empty for reads, drop the work for writes, never raise into the turn** | NFR-REL-1. Crashing memory providers get disabled by Hermes; degraded memory beats no memory. |
| AD-08 | **Subprocess timeout: configurable, default 2000 ms reads / 5000 ms writes** | NFR-PERF-3. Timeout triggers the AD-07 path. Read timeout is tighter so prefetch never bloats turn latency. |
| AD-09 | **Output format from `icm` is `--format json`** for `recall`               | Stable, machine-parseable. `toon` (the new default) is shorter but parser-fragile; `detail` is human-only. We pin `--format json` for reads we parse. |
| AD-10 | **Tool handler return contract: `json.dumps(...)` always, never raise, never return a dict** | Hermes plugin author rule; reference scaffold convention. |
| AD-11 | **Hook signatures all accept `**kwargs`** | Reference scaffold convention; survives Hermes adding new arg names. |
| AD-12 | **Only `cli_runner.py` imports `subprocess`** (NFR-MAINT-2 design lock)    | Lets v2 swap to MCP transport (`icm serve`) by replacing one module without touching `provider.py`, `tools.py`, `hooks.py`. Enforced by an AST/grep test in CI. |
| AD-13 | **Logging namespace: `hermes_icm_memory` (Python `logging`)**, no `print()` in non-test code | NFR-OBS-1. |

### 3.2 Important decisions (shape architecture)

| ID    | Decision                                                                                                            |
|-------|---------------------------------------------------------------------------------------------------------------------|
| AD-14 | **Per-turn prefetch cache** (in-memory dict keyed by recent-turn-hash, LRU bounded at 32 entries) so `system_prompt_block` reads from cache after `prefetch` populated it. (FR9, NFR-PERF-4.) |
| AD-15 | **Worker lazy-respawn at most once per process lifetime** on daemon-thread death; second death → degrade-with-drop forever. (NFR-REL-2.) |
| AD-16 | **`on_session_end` grace window default 1500 ms**, configurable. Items remaining at deadline are dropped with one WARNING; method returns within `grace_window + ε`. (FR5.) |
| AD-17 | **Mapping module is data-driven** (a single `MAPPING` dict in `mapping.py`, plus a `detect_triggers(user_text, assistant_text)` pure function). Lets the heuristics be unit-tested in isolation against fixture turns. |
| AD-18 | **Config schema rejects invalid values structurally** (returning `{"error": "..."}` from `save_config`); never raises. (FR7.) |
| AD-19 | **Subprocess args always list-form (`["icm", "store", "-t", topic, ...]`), never string concat** | NFR-SEC-3. |
| AD-20 | **Periodic-progress trigger fires every N turns** (default `N=20`, configurable), counted per-session by the provider instance. Resets on `on_session_end`. |

### 3.3 Deferred (post-v1)

| ID    | Decision                                                                  | Trigger to revisit                                          |
|-------|---------------------------------------------------------------------------|-------------------------------------------------------------|
| AD-D1 | MCP transport via `icm serve` (replaces `cli_runner.py` internals)         | v2 release; no public API change required (NFR-MAINT-1/2).  |
| AD-D2 | Bidirectional `icm learn` / `icm feedback-record` integration              | After v1 adoption; needs UX design.                         |
| AD-D3 | Read-only / audit mode (`icm-recall-only` lite)                            | Demand-driven.                                              |
| AD-D4 | Companion shell CLI (`hermes-icm`) for out-of-session recall queries       | Demand-driven.                                              |

### 3.4 Cascading implications

- AD-01 → AD-12 (cli_runner isolation) → enables AD-D1 (MCP swap) without breaking NFR-MAINT-1.
- AD-03 + AD-04 → NFR-PERF-1 (`sync_turn` < 5 ms p95) and NFR-REL-2 (lazy-respawn).
- AD-05 → all path construction goes through a single `paths.py`-like helper inside `config.py`; no other module joins paths from `hermes_home` directly.
- AD-09 → integration tests can assert on stable JSON shape; unit tests mock `subprocess.run` to return canned `--format json` strings.

---

## 4. Component Map

The Python module layout and per-module responsibilities. Mirror-style of `hermes-rtk-hook`, expanded for the memory-provider role.

```
hermes_icm_memory/
├── __init__.py        # register(ctx) — only thing Hermes loads. Constructs IcmMemoryProvider, calls ctx.register_memory_provider(provider).
├── provider.py        # IcmMemoryProvider(MemoryProvider) — class that owns state (config, queue, worker, prefetch cache).
├── cli_runner.py      # The ONLY module that imports subprocess. Functions: run_recall, run_store, run_topics, run_health. Each takes argv list parts + optional db path + timeout, returns parsed result or raises a typed exception caught by the caller.
├── tools.py           # Four pure handlers (icm_recall, icm_store, icm_topics, icm_health). Each: validate args → call cli_runner OR enqueue → return json.dumps(...). Never raise.
├── hooks.py           # Four hook callbacks: prefetch, system_prompt_block, sync_turn, on_session_end. All bound methods on the provider — receive **kwargs.
├── config.py          # Config schema, default values, validation, db-path derivation from hermes_home.
├── mapping.py         # Pure heuristics: detect_triggers(...) returns list of (topic, importance, content, keywords) tuples. MAPPING dict literal lives here.
├── errors.py          # Typed exceptions raised inside cli_runner (ICMNotFoundError, ICMTimeoutError, ICMNonZeroExitError, ICMMalformedOutputError). Caught at the cli_runner boundary; never escape it.
└── _version.py        # __version__ string (single-source-of-truth for pyproject.toml + plugin.yaml).
```

```
tests/
├── conftest.py                      # Shared fixtures: tmp_hermes_home, mock_icm_subprocess, real_icm_db, capture_logs.
├── test_provider.py                 # IcmMemoryProvider lifecycle: is_available, initialize idempotency, get_config_schema, save_config validation.
├── test_cli_runner.py               # Subprocess mocked: argv shape, timeout values, JSON parse success, every failure mode in §6.
├── test_tools.py                    # Each tool handler: returns json.dumps(...), validates args, never raises.
├── test_hooks.py                    # Hook callbacks: prefetch populates cache, system_prompt_block reads cache, sync_turn enqueues without blocking, on_session_end flushes within grace.
├── test_mapping.py                  # Pure: detect_triggers fixtures → expected (topic, importance) tuples.
├── test_errors_and_degrade.py       # Failure-mode matrix from §6: every error → WARNING log, no exception escape.
├── test_profile_isolation.py        # Two distinct hermes_home → two distinct DB paths, no cross-leak.
├── test_no_subprocess_outside_cli_runner.py  # AST/grep test — only cli_runner.py imports subprocess.
├── test_no_hardcoded_dotcache.py    # AST/grep test — no module references "~/.hermes" literally.
├── test_no_network_calls.py         # Patch socket.socket; assert lifecycle methods make no socket.
├── test_plugin_loader.py            # plugin.yaml shape; register(ctx) calls ctx.register_memory_provider once with the provider instance.
└── integration/
    ├── test_real_icm_recall.py      # Real icm binary, tmp_path DB. Write via subprocess, read via plugin → hit returned.
    ├── test_real_icm_cross_tool.py  # Simulates Claude Code writer + Hermes reader against same DB.
    └── test_sync_turn_stress.py     # Enqueue 2× capacity rapidly; assert FIFO, single WARNING, no exception.
```

### 4.1 Module dependency graph (compile-time)

```
__init__.py ──► provider.py ──► hooks.py
                  │                │
                  ▼                ▼
              tools.py ─────► cli_runner.py ──► errors.py
                  │              │
                  ▼              ▼
              mapping.py     subprocess (stdlib)
                  │
                  ▼
              config.py
```

**Invariants** (enforced by tests):

1. `subprocess` is imported only by `cli_runner.py`. (AD-12.)
2. `mapping.py` and `config.py` import nothing from this package — they are pure.
3. `errors.py` imports nothing from this package.
4. The string literal `"~/.hermes"` does not appear anywhere except documentation tests.

---

## 5. Plugin Lifecycle Wiring

Maps each Hermes hook + lifecycle method to: when it fires, what data flows, what side effects occur.

### 5.1 Lifecycle methods (synchronous, called by Hermes core)

| Method                           | When                                       | Inputs                                | Returns / Side effect                                                                                  |
|----------------------------------|--------------------------------------------|---------------------------------------|--------------------------------------------------------------------------------------------------------|
| `register(ctx)`                  | Plugin load                                 | `ctx`                                 | Constructs `IcmMemoryProvider()`, calls `ctx.register_memory_provider(provider)`.                      |
| `provider.is_available()`        | Hermes "should we activate this provider?"  | (none)                                | `True` iff `shutil.which("icm")` is truthy. **No subprocess, no network.** Caches first result.        |
| `provider.initialize(session_id, **kwargs)` | Once per session                | `session_id`, `kwargs['hermes_home']`, optional `kwargs['profile']` | Resolves DB path, ensures `hermes_home/icm/` exists, starts daemon worker (if not already), sets self._session_id. Idempotent. |
| `provider.get_config_schema()`   | Hermes runs `hermes memory setup icm`       | (none)                                | Returns the config schema list per FR6. Pure.                                                          |
| `provider.save_config(values, hermes_home)` | After interactive setup           | values dict                           | Validates per AD-18 ; writes to a tiny JSON sidecar at `<hermes_home>/icm/config.json` ; returns `None` on success or `{"error": ...}` on validation fail. Never raises. |
| `provider.get_tool_schemas()`    | LLM tool registration                       | (none)                                | Returns the four tool schemas from `tools.py`.                                                         |
| `provider.handle_tool_call(name, args)` | LLM invokes a tool                  | `name`, `args` dict                   | Dispatches to `tools.icm_recall / icm_store / icm_topics / icm_health`. Always returns `json.dumps(...)`. |
| `provider.shutdown()` (optional) | Process exit                                | (none)                                | Calls `on_session_end` if not already, joins worker thread within grace, drops remainder.              |

### 5.2 Optional hooks (declared in `plugin.yaml`)

| Hook                | When                                       | Behavior                                                                                                                                                              |
|---------------------|--------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `prefetch(query, **kwargs)` | Before each LLM call                  | Synchronous read: `cli_runner.run_recall(query, limit=K, format=json, db=...)` with read-timeout. Stores result in the per-turn cache (AD-14) keyed by `hash(query)`. Returns the recalled string for Hermes to inject. |
| `system_prompt_block(**kwargs)` | System prompt assembly             | Reads from prefetch cache (no second subprocess). Composes a formatted block: top-K hits + a one-paragraph project-context summary derived from the hits' topics. Returns string. |
| `sync_turn(user_content, assistant_content, **kwargs)` | After every completed turn | **Non-blocking.** Calls `mapping.detect_triggers(user_content, assistant_content)` → list of write tasks. For each task, `queue.put_nowait(task)`. On `queue.Full` → WARNING (rate-limited). Returns `None` immediately. p95 < 5 ms. |
| `on_session_end(messages, **kwargs)` | Conversation ends                  | Sets a "drain deadline" (now + grace_window). Worker drains queue until empty or deadline. Items remaining → drop with single WARNING. Method returns within `grace_window + ε`. Optionally fires one `icm consolidate` per configured topic if enabled. |

### 5.3 LLM tool handlers (called via `handle_tool_call`)

| Tool          | Args                                              | Path                                                                                                                                                  |
|---------------|---------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------|
| `icm_recall`  | `query`, optional `topic`, `limit`, `project`     | `cli_runner.run_recall(...)` with read-timeout → JSON parse → `json.dumps({"hits": [...]})` ; on any failure → `json.dumps({"hits": []})` + WARNING.   |
| `icm_store`   | `topic`, `content`, optional `importance`, `keywords`, `raw` | Build write task → `queue.put_nowait(task)` → return `json.dumps({"accepted": True, "queued_at": iso_now})`. **Never blocks.**                  |
| `icm_topics`  | (none)                                            | `cli_runner.run_topics(db=...)` → JSON parse → `json.dumps({"topics": [...]})` ; on failure → `json.dumps({"topics": []})` + WARNING.                  |
| `icm_health`  | optional `topic`                                  | `cli_runner.run_health(topic=topic, db=...)` → JSON parse → `json.dumps({"report": {...}})` ; on failure → `json.dumps({"report": {}})` + WARNING.    |

---

## 6. ICM CLI Shellout Strategy

### 6.1 Subcommand surface (verified against `icm --help` on 2026-05-05)

| Plugin operation     | `icm` invocation                                                                                                            | Timeout (default) | Output handling                                                                                       |
|----------------------|-----------------------------------------------------------------------------------------------------------------------------|-------------------|-------------------------------------------------------------------------------------------------------|
| `run_recall`         | `icm --db <path> recall <query> --limit <K> --format json [-t <topic>] [-p <project>]`                                       | 2000 ms (read)    | `json.loads(stdout)` ; expects array of memory dicts; on `JSONDecodeError` → `ICMMalformedOutputError`. |
| `run_store`          | `icm --db <path> store -t <topic> -c <content> -i <importance> [-k <keywords>] [-r <raw>]`                                   | 5000 ms (write)   | Discard stdout; only return-code matters.                                                              |
| `run_topics`         | `icm --db <path> topics --format json` (fall back to plain-text parsing if `--format json` not supported in installed `icm`) | 2000 ms (read)    | Parse JSON list or split lines; degrade-on-fail.                                                       |
| `run_health`         | `icm --db <path> health [-t <topic>] --format json` (same fallback)                                                          | 2000 ms (read)    | Parse JSON dict; degrade-on-fail.                                                                      |

### 6.2 Subprocess invocation contract (every call)

```python
subprocess.run(
    argv,                       # AD-19: list form, no shell
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=timeout,
    check=False,                # we inspect returncode ourselves
    shell=False,                # NFR-SEC-3
)
```

Wrapped in `try/except (FileNotFoundError, subprocess.TimeoutExpired, OSError)` → translated into typed exceptions in `errors.py`, which the caller (in `tools.py` / `hooks.py`) catches and converts to the AD-07 degrade response.

### 6.3 Failure-mode matrix

| Failure mode                                          | Detected by                              | Plugin behavior                                                                          | Test                                                       |
|-------------------------------------------------------|------------------------------------------|------------------------------------------------------------------------------------------|------------------------------------------------------------|
| `icm` not on PATH                                     | `shutil.which("icm")` returns `None`     | `is_available()` → `False`; tool handlers short-circuit to `{"hits": []}` / drop write.  | `test_provider.py::test_is_available_false_when_missing`   |
| `icm` exits non-zero                                  | `proc.returncode != 0`                   | Log WARNING with stderr; reads → empty result, writes → drop.                            | `test_errors_and_degrade.py::test_nonzero_exit_*`          |
| `icm` hangs past timeout                              | `subprocess.TimeoutExpired`              | Log WARNING; reads → empty, writes → drop. Worker thread keeps running.                  | `test_errors_and_degrade.py::test_timeout_*`               |
| `icm` stdout is malformed JSON                        | `json.JSONDecodeError`                   | Log WARNING; reads → empty result.                                                       | `test_cli_runner.py::test_recall_malformed_json`           |
| Embedding model not yet downloaded (first `icm` call) | First call slow but eventually succeeds  | Surface INFO log "ICM is downloading model"; do not gate `is_available`. Let timeout fire if it actually hangs. | `test_errors_and_degrade.py::test_first_call_slow_path`    |
| Worker thread dies                                    | Next `put_nowait` sees thread not alive  | Lazy-respawn once per process (AD-15); if it dies again, degrade-to-drop.                | `test_hooks.py::test_worker_respawn_once`                  |
| Bounded queue full                                    | `queue.Full` from `put_nowait`           | Drop the new task; single WARNING per overflow burst (rate-limited via a flag).          | `integration/test_sync_turn_stress.py`                     |
| `hermes_home` path not writable                       | `OSError` from `mkdir` in `initialize`   | Log WARNING; provider self-disables (`is_available` flips to False after init failure).  | `test_provider.py::test_initialize_unwritable`             |

### 6.4 Timeout policy

- **Read path** (recall, topics, health): 2000 ms default. Bounded so prefetch cannot bloat turn latency past NFR-PERF-2.
- **Write path** (store): 5000 ms default. ICM writes that take > 5 s are degenerate; we drop and log.
- Both configurable via `save_config({"command_timeout_read_ms": ..., "command_timeout_write_ms": ...})`.

---

## 7. Non-Blocking `sync_turn` Design

### 7.1 Components

```
producer side                                consumer side
─────────────                                ─────────────
sync_turn(user, assistant)        ┌───────┐  worker thread (daemon=True)
   │                              │       │       │
   │ mapping.detect_triggers ───► │ Queue │ ────► │ while True:
   │                              │ (max  │       │   task = queue.get()
   │ for task in tasks:           │  N=64)│       │   try: cli_runner.run_store(...)
   │   queue.put_nowait(task) ──► │       │       │   except: logger.warning(...)
   │   except queue.Full:         └───────┘
   │     warn_once_per_burst()
   │
   └─► return None (turn unblocked)
```

### 7.2 Concrete contract

- **Queue:** `queue.Queue(maxsize=N)` where `N` defaults to **64**, configurable via `sync_write_queue_size`.
- **Worker:** Single `threading.Thread(target=worker_loop, daemon=True)`. Started lazily on first enqueue (or in `initialize`).
- **Producer policy:** `queue.put_nowait(task)`. On `queue.Full` → call `_warn_overflow_once()`, return immediately. The "burst flag" in `_warn_overflow_once()` is cleared by the worker after the next successful drain, so each overflow burst gets exactly one WARNING.
- **Consumer policy:** Blocking `queue.get()`; on each task, `cli_runner.run_store(...)` with write-timeout; failures logged at WARNING, never propagated.
- **Worker death:** If `worker_loop` raises (it shouldn't — every `try/except`), the next `put_nowait` notices `not worker.is_alive()` → respawn once (AD-15). After the second death, set `self._writes_disabled = True` and drop everything with a single CRITICAL log.

### 7.3 Why this exact shape

- **`threading.Thread` over `asyncio.create_task`:** Hermes calls `sync_turn` synchronously without guarantee of an active event loop. Threads are universal.
- **Single worker over multiple workers:** Preserves FIFO ordering (FR15) without locking. `icm store` is fast enough on local SQLite that one worker at 5 s timeout handles ≫ realistic write rate.
- **Bounded queue over unbounded:** Unbounded would let a runaway producer eat memory; bounded with drop-with-log is the explicit "degrade" choice.
- **Drop-on-full over backpressure:** Backpressure into `sync_turn` would violate NFR-PERF-1. Memory loss on overflow is the explicit trade-off, signalled to the user via WARNING.

### 7.4 Sequence diagram — `sync_turn` non-blocking write

```
User Hermes turn loop      provider.sync_turn       Queue          Worker thread       icm subprocess
       │                          │                    │                 │                   │
       │── turn done ────────────►│                    │                 │                   │
       │                          │                    │                 │                   │
       │                          │ detect_triggers──► (returns tasks)   │                   │
       │                          │                    │                 │                   │
       │                          │── put_nowait(t1)──►│                 │                   │
       │                          │── put_nowait(t2)──►│                 │                   │
       │                          │                    │                 │                   │
       │ ◄── return None ─────────│  (sync_turn done, < 5 ms)            │                   │
       │                          │                    │                 │                   │
       │ next turn starts...      │                    │                 │                   │
       │                          │                    │── get() ───────►│                   │
       │                          │                    │                 │── run_store(t1)─►│
       │                          │                    │                 │ ◄── ok ───────────│
       │                          │                    │── get() ───────►│                   │
       │                          │                    │                 │── run_store(t2)─►│
       │                          │                    │                 │ ◄── ok ───────────│
```

---

## 8. Recall Flow — Sequence Diagram

```
Hermes pre-LLM hook        provider.prefetch        cli_runner       icm subprocess     prefetch cache
       │                          │                     │                 │                  │
       │── query="..." ──────────►│                     │                 │                  │
       │                          │── run_recall(q,K)──►│                 │                  │
       │                          │                     │── icm recall...►│                  │
       │                          │                     │ ◄── JSON hits ──│                  │
       │                          │ ◄── parsed list ────│                 │                  │
       │                          │── cache.put(hash(q), hits) ────────────────────────────►│
       │ ◄── recall string ───────│                     │                 │                  │
       │                          │                     │                 │                  │
Hermes system prompt assembly                           │                 │                  │
       │── system_prompt_block ──►│                     │                 │                  │
       │                          │── cache.get(...) ──────────────────────────────────────►│
       │                          │ ◄── cached hits ─────────────────────────────────────────│
       │                          │ format(top-K + project-context-summary)                  │
       │ ◄── prompt block ────────│                     │                 │                  │
       │                          │                     │                 │                  │
LLM call proceeds with injected memory context.
```

Failure variant: if `cli_runner.run_recall` raises (timeout / not-found / malformed JSON), `prefetch` catches → logs WARNING → returns empty string AND writes `[]` into the cache so `system_prompt_block` doesn't re-attempt.

---

## 9. Profile Isolation

### 9.1 Path resolution

```python
# config.py
def resolve_db_path(hermes_home: str, profile: str | None = None) -> Path:
    base = Path(hermes_home).expanduser().resolve()
    profile_name = profile or "default"
    return base / "icm" / f"{profile_name}.db"
```

### 9.2 First-run behavior

1. `initialize(session_id, hermes_home=..., profile=...)` is called.
2. Plugin computes `db_path = resolve_db_path(hermes_home, profile)`.
3. Plugin runs `db_path.parent.mkdir(parents=True, exist_ok=True)`.
4. Plugin **does not** call `icm init` (that subcommand configures Claude-Code integration, not the DB). The first `icm recall` / `icm store` against `--db <path>` creates the SQLite file.
5. If the user has never run ICM at all, the embedding model download happens on the first real call. We surface a one-time INFO log "ICM is initializing on first call; this may take a few seconds." We do **not** block `is_available`.

### 9.3 Cross-profile leakage tests

- `test_profile_isolation.py::test_two_profiles_two_dbs` — instantiate two providers with two different `hermes_home` values; assert they resolve to two distinct paths.
- `test_profile_isolation.py::test_no_writes_cross_leak` — write via provider A, read via provider B against B's DB → no hit.
- `test_profile_isolation.py::test_no_hardcoded_dotcache` — AST/grep test asserts the string `"~/.hermes"` does not appear outside doc tests.

---

## 10. Configuration Surface

### 10.1 Schema (FR6)

| Key                          | Type   | Default                       | Description                                                              |
|------------------------------|--------|-------------------------------|--------------------------------------------------------------------------|
| `default_importance`         | enum   | `"high"`                      | Importance applied when an `icm_store` call omits it. `critical/high/medium/low`. |
| `topic_prefix`               | string | `""` (empty)                  | Optional prefix prepended to every stored topic, e.g. `"hermes/"`.       |
| `recall_limit`               | int    | `5`                           | Top-K for prefetch + system_prompt_block.                                |
| `prefetch_enabled`           | bool   | `true`                        | If false, `prefetch` no-ops and `system_prompt_block` returns `""`.       |
| `sync_write_queue_size`      | int    | `64`                          | Bounded write queue capacity.                                            |
| `command_timeout_read_ms`    | int    | `2000`                        | Timeout for read-path `icm` calls.                                       |
| `command_timeout_write_ms`   | int    | `5000`                        | Timeout for write-path `icm` calls.                                      |
| `session_end_grace_ms`       | int    | `1500`                        | `on_session_end` drain window.                                            |
| `periodic_progress_every_n_turns` | int | `20`                         | How often the periodic-progress trigger fires.                            |
| `consolidate_on_session_end` | bool   | `false`                       | If true, fire `icm consolidate` on configured topics at session end.      |

### 10.2 Validation (AD-18)

`save_config` performs structural validation: int-range checks, enum-membership checks, bool coercion. On invalid input → returns `{"error": "<actionable message>"}`. Never raises.

---

## 11. Implementation Patterns & Consistency Rules

(Pattern decisions that prevent AI-agent implementation drift across stories.)

### 11.1 Code naming

| Surface           | Convention                                                                                           |
|-------------------|------------------------------------------------------------------------------------------------------|
| Module names      | `snake_case.py`                                                                                      |
| Class names       | `PascalCase` (only the provider class is exported).                                                  |
| Function / method names | `snake_case`. Private helpers prefixed `_`.                                                    |
| Constants         | `UPPER_SNAKE_CASE`. Module-level only.                                                               |
| Type aliases      | `PascalCase`, defined with `type` keyword (PEP 695, Python 3.12+) where targeting 3.12+; otherwise `TypeAlias`. |
| Tool names (LLM-facing) | `icm_recall`, `icm_store`, `icm_topics`, `icm_health` — frozen by NFR-MAINT-1.                  |
| Topic names       | Hyphen-form for ICM compatibility: `errors-resolved`, `decisions-{project}`, `preferences`, `context-{project}`, `learnings`. |
| Importance values | Lowercase exactly as ICM expects: `"critical" | "high" | "medium" | "low"`.                          |

### 11.2 Logging

- Logger: `logger = logging.getLogger("hermes_icm_memory")` at module top.
- Submodule loggers: `logging.getLogger("hermes_icm_memory.cli_runner")`, etc.
- Levels:
  - `DEBUG` — every subprocess invocation with redacted argv + elapsed time.
  - `INFO` — trigger detections (`"detected trigger: errors-resolved"`).
  - `WARNING` — every degraded path, every overflow burst, every worker death.
- Format: structured key-value (`logger.warning("recall failed", extra={"err": ..., "elapsed_ms": ...})`). No f-string interpolation in log calls.
- **No `print()` calls** anywhere outside `tests/`.

### 11.3 Error handling

- `cli_runner.py` is the only place that raises typed exceptions from `errors.py`.
- `tools.py`, `hooks.py`, `provider.py` all catch broadly at the boundary, log WARNING, return the documented degraded shape.
- `try/except` blocks must be tight: catch the specific typed exception or `Exception` only at the outermost boundary of a tool handler / hook.

### 11.4 Subprocess invocation

- Always list-form argv, never a shell string.
- Always pass `--db <path>` as the second + third tokens (after `icm`).
- Always pass `--format json` where the subcommand supports it.
- Always set `timeout=` (never call `subprocess.run` without a timeout).

### 11.5 Tests

- Unit tests **always** mock `subprocess.run` (and any other side effect) — never invoke `icm` itself.
- Integration tests live under `tests/integration/` and require a real `icm` on PATH; CI installs it. They use a `tmp_path`-bound DB.
- Each fixture is named after what it provides (`tmp_hermes_home`, `mock_icm_subprocess`, etc.) and lives in `tests/conftest.py`.
- Test names follow `test_<unit_under_test>_<condition>_<expected>` (e.g. `test_recall_malformed_json_returns_empty_hits`).

### 11.6 Type hints

- Every public function and class has full type hints (NFR-MAINT-3).
- `mypy --strict` clean.
- No `Any` unless documented with an inline reason; `# type: ignore[code]` only with both an issue link and an inline comment.

### 11.7 Imports

- Standard library first, third-party second, local last (ruff-isort default).
- No wildcard imports.
- `cli_runner.py` is the only module allowed to `import subprocess`. Enforced by an AST test.

### 11.8 Public API surface (frozen post-v1)

- Class: `IcmMemoryProvider`.
- Tool names: exactly the four above.
- Config keys: exactly the ten above.
- Plugin name: `"icm"`.

Breaking changes require a major version bump (semver).

---

## 12. Project Structure & Boundaries

### 12.1 Complete directory tree

```
hermes-icm-memory/
├── README.md                     # 3-step quickstart + features + links to ICM/Hermes docs
├── LICENSE                       # Apache-2.0 (already present; verify wording)
├── CONTRIBUTING.md               # Minimal: bun-style not applicable; how to run tests, ruff, mypy; PR conventions
├── CODE_OF_CONDUCT.md            # OPTIONAL for v1 (Contributor Covenant if added)
├── pyproject.toml                # PEP 621; entry-point hermes_agent.plugins; pytest + ruff + mypy config
├── plugin.yaml                   # Manifest: name=hermes-icm-memory, hooks: [prefetch, system_prompt_block, sync_turn, on_session_end]
├── .gitignore                    # Already present (BMAD additions committed)
├── .github/
│   └── workflows/
│       └── ci.yml                # Matrix: Python 3.11 / 3.12 on ubuntu-latest. Steps: checkout → install icm → install package + dev deps → ruff → mypy → pytest --cov (fail < 85 %)
├── hermes_icm_memory/
│   ├── __init__.py               # register(ctx)
│   ├── _version.py               # __version__ = "0.1.0"
│   ├── provider.py               # IcmMemoryProvider class
│   ├── cli_runner.py             # subprocess wrapper (only file importing subprocess)
│   ├── tools.py                  # 4 LLM tool handlers
│   ├── hooks.py                  # 4 lifecycle hook callbacks (provider-bound)
│   ├── config.py                 # Schema + validation + path resolution
│   ├── mapping.py                # detect_triggers + MAPPING dict
│   └── errors.py                 # Typed exceptions
├── tests/
│   ├── conftest.py
│   ├── test_provider.py
│   ├── test_cli_runner.py
│   ├── test_tools.py
│   ├── test_hooks.py
│   ├── test_mapping.py
│   ├── test_errors_and_degrade.py
│   ├── test_profile_isolation.py
│   ├── test_no_subprocess_outside_cli_runner.py
│   ├── test_no_hardcoded_dotcache.py
│   ├── test_no_network_calls.py
│   ├── test_plugin_loader.py
│   └── integration/
│       ├── __init__.py
│       ├── test_real_icm_recall.py
│       ├── test_real_icm_cross_tool.py
│       └── test_sync_turn_stress.py
└── _bmad-output/                 # Planning artifacts (committed; not packaged)
    └── planning-artifacts/
        ├── product-brief.md
        ├── prd.md
        ├── architecture.md      # this file
        ├── epics-and-stories.md  # next BMAD step
        └── implementation-readiness.md  # final BMAD step
```

### 12.2 `pyproject.toml` key sections (target shape)

```toml
[project]
name = "hermes-icm-memory"
version = "0.1.0"
description = "Hermes Agent memory provider plugin backed by ICM (Infinite Context Memory)."
readme = "README.md"
license = { file = "LICENSE" }
requires-python = ">=3.11"
authors = [{ name = "ta3pks" }]
dependencies = []  # No runtime Python deps beyond what hermes-agent provides.

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-cov",
    "coverage[toml]",
    "ruff",
    "mypy",
]

[project.entry-points."hermes_agent.plugins"]
hermes-icm-memory = "hermes_icm_memory:register"

[project.urls]
Homepage = "https://github.com/ta3pks/hermes-icm-memory"
Repository = "https://github.com/ta3pks/hermes-icm-memory"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --strict-markers --cov=hermes_icm_memory --cov-branch --cov-fail-under=85"

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.mypy]
strict = true
python_version = "3.11"
```

### 12.3 `plugin.yaml` (target shape)

```yaml
name: hermes-icm-memory
version: 0.1.0
description: "Hermes memory provider backed by ICM (cross-editor SQLite memory with semantic recall)."
author: ta3pks
hooks:
  - prefetch
  - system_prompt_block
  - sync_turn
  - on_session_end
```

### 12.4 GitHub Actions CI (`ci.yml` shape)

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ matrix.python-version }} }
      - name: Install icm
        run: |
          curl -fsSL <icm-install-url> | bash
          icm --version
      - name: Install package
        run: |
          python -m pip install -U pip
          pip install -e ".[dev]"
      - name: Lint
        run: ruff check .
      - name: Type check
        run: mypy hermes_icm_memory
      - name: Test
        run: pytest
```

### 12.5 README.md quickstart shape

```
## Quickstart (3 steps)

1. Ensure `icm` is on your PATH: `icm --version`. (If not, install from https://github.com/rtk-ai/icm.)
2. Install the plugin: `pip install hermes-icm-memory` (or drop into `~/.hermes/plugins/hermes-icm-memory/`).
3. Enable + activate: `hermes plugins enable hermes-icm-memory && hermes memory setup icm`.

You're done. Hermes will now recall from ICM at session start and write decisions / errors-resolved / preferences / context / progress back to ICM after every turn — non-blockingly.
```

### 12.6 CONTRIBUTING.md scope

- How to run the test suite (`pytest`, `pytest tests/integration` for real-`icm` tests).
- How to run lint + type-check (`ruff check .` and `mypy hermes_icm_memory`).
- PR conventions: TDD-required (write tests first), ≥85 % coverage, both ruff + mypy clean.
- Commit message style: short imperative, no `Co-Authored-By` line per project convention.

---

## 13. Test Strategy

### 13.1 Unit tests (mock subprocess)

- **Files:** `test_provider.py`, `test_cli_runner.py`, `test_tools.py`, `test_hooks.py`, `test_mapping.py`, `test_errors_and_degrade.py`, `test_profile_isolation.py`, `test_plugin_loader.py`, plus the three AST/grep tests.
- **Subprocess mocking:** `unittest.mock.patch("hermes_icm_memory.cli_runner.subprocess.run")` returning a `MagicMock(returncode=0, stdout=..., stderr=...)`. The reference scaffold's `test_hook.py` is the style template.
- **Fixtures (in `tests/conftest.py`):**
  - `tmp_hermes_home(tmp_path) -> Path` — creates `tmp_path/.hermes` and yields it.
  - `mock_icm_on_path(monkeypatch)` — patches `shutil.which("icm")` → `/usr/local/bin/icm`.
  - `mock_icm_missing(monkeypatch)` — patches `shutil.which("icm")` → `None`.
  - `mock_subprocess_run(monkeypatch)` — patches `subprocess.run` in `cli_runner` module; returns a configurable `MagicMock`.
  - `provider(tmp_hermes_home, mock_icm_on_path)` — fully initialized `IcmMemoryProvider` for tests.
  - `capture_logs(caplog)` — convenience wrapper to assert WARNING emissions.

### 13.2 Integration tests (real `icm`)

- **Files:** `tests/integration/test_real_icm_recall.py`, `test_real_icm_cross_tool.py`, `test_sync_turn_stress.py`.
- **Skip condition:** `pytest.importorskip` analog — at module import, if `shutil.which("icm")` is `None`, skip the whole file with a clear message. CI installs `icm` so these always run there.
- **DB path:** Always `tmp_path / ".hermes" / "icm" / "default.db"`. Never the user's real DB.
- **Use `--no-embeddings`** for tests where embedding download would be slow; we still verify hybrid keyword search.

### 13.3 Coverage gate

`pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` is wired into CI. Gate fails the PR if coverage drops below 85 %.

### 13.4 Static analysis gates

- `ruff check .` — zero warnings.
- `mypy --strict hermes_icm_memory` — zero errors.
- Both run in CI; both block the PR.

---

## 14. Architecture Validation

### 14.1 Coherence

- All 13 critical decisions (AD-01–AD-13) are mutually compatible: subprocess + threading + logging + Python 3.11 are all stdlib; no version conflicts.
- AD-12 (cli_runner isolation) is the linchpin enabling AD-D1 (MCP swap) without violating NFR-MAINT-1.
- AD-03/AD-04/AD-15 jointly satisfy NFR-PERF-1 (`< 5 ms p95`) + NFR-REL-2 (lazy-respawn) + FR15 (FIFO + bounded).

### 14.2 Requirements coverage

Every PRD FR maps to a module + test file:

| FR    | Module owner              | Test file(s)                                                            |
|-------|---------------------------|-------------------------------------------------------------------------|
| FR1   | `pyproject.toml`, `__init__.py` | `test_plugin_loader.py`                                          |
| FR2   | `config.py::resolve_db_path` | `test_profile_isolation.py`, `test_no_hardcoded_dotcache.py`         |
| FR3   | `provider.py::is_available` | `test_provider.py`, `test_no_network_calls.py`                       |
| FR4   | `provider.py::initialize`   | `test_provider.py::test_initialize_idempotent`                        |
| FR5   | `hooks.py::on_session_end` + worker | `test_hooks.py::test_on_session_end_*`                          |
| FR6,7 | `config.py::save_config`    | `test_provider.py::test_save_config_*`                                |
| FR8   | `tools.py::icm_recall` + `cli_runner.run_recall` | `test_tools.py::test_recall_*`                  |
| FR9,10| `hooks.py::prefetch` + `system_prompt_block` | `test_hooks.py::test_prefetch_caches_for_block`     |
| FR11  | `tools.py::icm_topics`      | `test_tools.py::test_topics_*`                                        |
| FR12  | (cross-tool integration)    | `tests/integration/test_real_icm_cross_tool.py`                       |
| FR13  | `tools.py::icm_store` + queue | `test_tools.py::test_store_returns_immediately`                     |
| FR14  | `mapping.py::detect_triggers` | `test_mapping.py`                                                   |
| FR15  | worker + queue              | `tests/integration/test_sync_turn_stress.py`                          |
| FR16  | `mapping.py::MAPPING`       | `test_mapping.py::test_category_to_topic_importance`                  |
| FR17  | `tools.py::icm_health`      | `test_tools.py::test_health_*`                                        |
| FR18  | logging across all modules  | `test_*` (caplog assertions)                                          |
| FR19  | `errors.py` + every degrade path | `test_errors_and_degrade.py`                                     |

NFRs covered:

- NFR-PERF-1/2 → AD-03/AD-04 design + `test_sync_turn_stress`.
- NFR-PERF-3 → AD-08 + `test_cli_runner` timeout cases.
- NFR-PERF-4 → AD-14 cache + `test_hooks::prefetch_caches_for_block`.
- NFR-REL-1/2/3/4/5 → §11 patterns + CI gates.
- NFR-SEC-1/2/3/4 → AD-19, AD-05, `test_no_network_calls`, `test_no_hardcoded_dotcache`.
- NFR-OBS-1–4 → §11.2 logging discipline.
- NFR-MAINT-1/2/3 → §11.8 frozen API + AD-12 + `mypy --strict`.

### 14.3 Implementation readiness checklist

- [x] Component map specifies every file + responsibility.
- [x] Plugin lifecycle wiring covers all 8 lifecycle methods + 4 hooks.
- [x] CLI shellout strategy enumerates every subcommand, flag, and timeout.
- [x] Non-blocking sync_turn design decided and locked (thread + bounded queue + drop policy).
- [x] Profile isolation scheme is mechanical (`<hermes_home>/icm/<profile>.db`) and enforced by tests.
- [x] Failure-mode matrix covers 8 failure modes, each with detection method + behavior + test.
- [x] Test strategy specifies fixtures, mocking style, and coverage gate.
- [x] Two sequence diagrams provided (recall flow §8, sync_turn flow §7.4).
- [x] OSS deliverables enumerated: README quickstart, CONTRIBUTING scope, GitHub Actions CI matrix.

### 14.4 Open architectural questions

None blocking. The PRD already locked every load-bearing decision; this document operationalized them. Remaining unknowns are story-level (e.g. exact prompt format inside `system_prompt_block`) and will be settled by the dev story acceptance criteria.

### 14.5 Deviations / clarifications from PRD

- **PRD FR4 wording vs `icm init`** — PRD says "calls `icm init` if needed". Verified that `icm init` actually configures Claude-Code integration; the SQLite DB itself auto-creates on first call against `--db <path>`. AD-06 records the clarification: we do not call `icm init`; we ensure the parent dir exists and let ICM create the DB.

---

## 15. Implementation Handoff

### 15.1 First implementation step

Story 1 (per the next BMAD phase) creates the package skeleton: `pyproject.toml`, `plugin.yaml`, `hermes_icm_memory/__init__.py` with a stub `register(ctx)`, `LICENSE` (Apache-2.0 already present), and an empty test that asserts `register(ctx)` calls `ctx.register_memory_provider` exactly once. Everything else builds atop this.

### 15.2 AI-agent guidelines

- Follow this document's component map exactly. Do not collapse modules; the module split is intentional (AD-12 enforces it).
- TDD is mandatory: write the test first, watch it fail, implement until it passes.
- Every PR runs ruff + mypy + pytest with the 85 % coverage gate. No exceptions.
- All hook callbacks accept `**kwargs`. All tool handlers return `json.dumps(...)`. Always.
- The string `"~/.hermes"` does not appear in source. Use `kwargs['hermes_home']`.

### 15.3 Architecture readiness

**Overall status: READY FOR IMPLEMENTATION.**

Confidence: high. Every decision traces to a PRD requirement; every requirement has a test target.
