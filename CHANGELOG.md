# Changelog

All notable changes to this project are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [0.4.1] — 2026-05-16

**Bug fix — auto-store no longer silently dropped in default-shared mode.**

### Fixed

- **`provider._ensure_worker` no longer short-circuits when `_db_path is
  None`.** The v0.1.1 guard against a missing `_db_path` predated the v0.4
  MCP migration; once `initialize()` started spawning a warm `icm serve`
  daemon (which owns its own DB at startup) the guard turned into dead
  code that silently no-op'd every `sync_turn` for users on the
  recommended `isolated: false` config. The worker now spawns regardless
  of `_db_path`, and `cli_runner.run_store` routes writes through the
  daemon. Regression guard: `tests/test_hooks.py::
  test_sync_turn_enqueues_in_default_shared_mode`.

### Removed

- **README "Known limitations" caveat** about writes needing a concrete
  `_db_path` — invalidated by this fix.

## [0.3.1] — 2026-05-07

**License change — Apache-2.0 → BSD 3-Clause "New" or "Revised" License.**

### Changed

- **LICENSE** replaced with the canonical BSD 3-Clause text (filled with
  `Copyright (c) 2026, Nikos Efthias`). GitHub auto-detects as
  `BSD-3-Clause`.
- **README**, **CONTRIBUTING.md**, project metadata updated to reflect
  the new license. Badge, "Features" bullet, "License" section, and
  contributor agreement all point at BSD 3-Clause.

### Removed

- **NOTICE file** — Apache-2.0–specific convention; not used by BSD 3-Clause.

### Migration

This is a license change, not a code change — no behavioural impact.
Downstream users who pinned hermes-icm-memory under Apache-2.0 should
review whether BSD 3-Clause is acceptable for their project. The two
licenses are similar in spirit (permissive, attribution-required), but
BSD 3-Clause adds the no-endorsement clause and drops the Apache patent
grant — consult counsel if patent posture matters to your use case.

## [0.3.0] — 2026-05-07

**Architecture pivot — hermes-native MCP for tools, lifecycle-only plugin.**

Hermes-Agent v0.3.0 (March 2026) shipped first-class `mcp_servers.<name>:` config. This release deletes the plugin's duplicate `transport: mcp` machinery so hermes is the single source of truth for `icm` tool exposure, and the plugin keeps only what it alone can do: auto-injection of recalled memories on prompt-submit (`prefetch()` → `system_prompt_block()`) and auto-store on triggered turns (`sync_turn()`).

Net diff: **−2484 lines** of code. Auto-injection contract preserved bit-for-bit. The LLM now sees ~30 native `icm_memory_*` / `icm_memoir_*` / `icm_feedback_*` / `icm_transcript_*` / `icm_learn` / `icm_consolidate` tools instead of the plugin's previous 4-tool wrapper surface.

### Removed

- **`transport` config key** — the v0.2 enum (`cli` / `mcp`) is gone.
  v0.2-era configs that still carry `transport: ...` validate as a pass-
  through unknown key (forward-compat); the runtime ignores it.
- **MCP transport in `cli_runner`** — `mcp_start`, `mcp_stop`, `_McpDaemon`,
  `_mcp_call`, `_mcp_recall` / `_mcp_store` / `_mcp_topics` / `_mcp_health`
  and the JSON-RPC plumbing (`_MCP_PROTOCOL_VERSION`, `_MCP_TOOL_*`,
  `_MCP_MAX_RESPONSE_LINES`, the lifecycle lock, the `atexit` backstop)
  are deleted. `cli_runner` now only uses `subprocess.run` for one-shot
  CLI invocations (no `subprocess.Popen`). The `transport` kwarg on
  `run_recall` / `run_store` / `run_topics` / `run_health` is removed.
- **LLM-tool surface (`tools.py`)** — `IcmMemoryProvider.handle_tool_call`
  and `IcmMemoryProvider.get_tool_schemas` are removed; the entire
  `hermes_icm_memory/tools.py` module is deleted along with
  `tests/test_tools.py` and `tests/test_cli_runner_mcp.py`. Tool exposure
  to the LLM is now hermes-native via `mcp_servers.icm:` (auto-discovers
  `icm_memory_recall`, `icm_memory_store`, `icm_memory_list_topics`,
  `icm_memory_health`).
- **`hooks.WorkerState.transport` field** — single CLI write path; no
  branch in `worker_loop` / `ensure_worker` / `run_prefetch`.
- **`provider.initialize` MCP startup branch** and **`provider.on_session_end`
  `cli_runner.mcp_stop()` call** are gone.

### Added

- **`IcmMemoryProvider.shutdown()`** — Hermes lifecycle hook (no-op in
  v0.3, no daemon to manage). Defined explicitly so hermes-agent's
  `memory_manager` no longer logs
  `'IcmMemoryProvider' object has no attribute 'shutdown'` on every
  gateway restart.
- **Inline `%r` in WARNING log messages.** Every public boundary that
  catches and degrades (`hooks.run_prefetch`, `hooks.submit_triggers`,
  `hooks.worker_loop`, `provider.prefetch`, `provider.sync_turn`,
  `provider.on_session_end`, `provider.shutdown`,
  `provider.initialize`, `provider.save_config`, `provider.is_available`)
  now includes the exception text in the format string itself (e.g.
  `"prefetch failed: %r"` with `exc` as positional arg) **in addition to**
  the existing `extra={"err": repr(exc), ...}`. The default Python
  logging formatter does not render `extra={...}`, which made silent-
  degrade incidents undiagnosable in the field. AD-13's structured logs
  stay (for operators using JSON log formatters), but the human-readable
  exception text is now also present.
- **New invariant tests** —
  `tests/test_no_tool_surface.py` pins that the provider has no
  `handle_tool_call` / `get_tool_schemas` and `tools.py` is deleted from
  the package; `tests/test_cli_only_transport.py` pins that none of the
  `run_*` helpers carries a `transport=` kwarg, no `mcp_*` symbols
  remain, and `subprocess.Popen` is absent from the source.

### Changed

- **`config.get_default_schema()` returns twelve entries** (down from 13);
  the v0.2 `transport` enum is removed (AC2).
- **`hooks.run_prefetch` always uses CLI subprocess.** With
  `use_embeddings: false` (the recommended Pi-class setting for the
  prefetch hot-path) each call is < 100 ms — fine for the prompt-prepend
  hot path. Semantic recall on demand is delivered by hermes-native
  `mcp_servers.icm:` when the LLM calls `icm_memory_recall`.

### Migration from v0.2

1. **Remove `transport` from `plugins.hermes-icm-memory:`** in
   `~/.hermes/config.yaml` (it's now ignored; passes through as an
   unknown key, no error).

2. **Add `mcp_servers.icm:`** to `~/.hermes/config.yaml` if not already
   present:

   ```yaml
   mcp_servers:
     icm:
       command: icm
       args: [serve, --no-embeddings]   # or omit --no-embeddings if your hardware has fast model load
       timeout: 120
       connect_timeout: 30
   ```

   Hermes auto-discovers `icm_memory_recall` / `icm_memory_store` /
   `icm_memory_list_topics` / `icm_memory_health` and registers them
   alongside built-ins.

3. **Restart hermes-gateway.** The LLM now uses the hermes-native
   `icm_memory_*` tools (prefixed with `icm_memory_` per hermes
   convention). Auto-injection on prompt-submit continues unchanged via
   the plugin's lifecycle hooks (`prefetch` → `system_prompt_block`).

### Limitations / Out of scope

- **Plugin-side writes still require a concrete `_db_path`.** Under the recommended `isolated: false` (shared DB) the worker no-ops and `sync_turn` writes are silently dropped — same v0.1.1 limitation. Set `isolated: true` to restore plugin writes today; the LLM can still write via `icm_memory_store` over hermes-native MCP. Concurrent-writer semantics against the canonical icm SQLite file is a v0.4 problem.
- **Honcho memory provider integration** (unrelated; hermes 0.3.0 ships its own).
- **Reusing hermes' MCP-managed daemon for plugin-side prefetch** (would couple the plugin to hermes internals; rejected — keyword-only CLI is fast enough on Pi).
- **Replacing the bounded-queue worker** with hermes' async write infrastructure (potential v0.4).

## [0.2.0] — 2026-05-06

`icm-serve` MCP transport — amortize the embedding-model load across calls.
Pi-class hosts can now run semantic recall: first call ~50 s (warmup),
every subsequent recall ~50 ms.

### Added

- New config key `transport` (enum, default `"cli"`, choices
  `["cli", "mcp"]`). `cli` keeps the v0.1.x fresh-subprocess path;
  `mcp` spawns one long-lived `icm serve` subprocess per provider
  lifetime and reuses it via JSON-RPC over stdin/stdout.
- New module-level helpers `cli_runner.mcp_start(db_path, use_embeddings)`
  and `cli_runner.mcp_stop()`. Provider's `initialize` calls
  `mcp_start` when `transport: mcp`; `on_session_end` always calls
  `mcp_stop` (no-op when transport is `cli`). An `atexit` hook is a
  belt-and-braces backstop so torn-down sessions never leak orphan
  `icm serve` processes.
- New integration test `tests/integration/test_real_icm_serve.py` —
  spawns a real `icm serve` daemon and asserts two consecutive recalls
  reuse the same subprocess (gated on `shutil.which("icm")`).
- `cli_runner.run_recall` / `run_topics` / `run_health` / `run_store`
  accept a `transport: str = "cli"` keyword. The MCP path internally
  dispatches to `_mcp_recall` / `_mcp_topics` / `_mcp_health` /
  `_mcp_store` (all private to `cli_runner.py`, AD-12 unchanged).

### Changed

- `provider.initialize` now branches on `_config_str("transport")`. If
  `"mcp"`, it spawns the daemon during initialize so the embedding-model
  warmup happens once at startup rather than on the first recall. On
  `mcp_start` failure the provider logs a WARNING and flips
  `_config["transport"]` to `"cli"` for the rest of the lifetime —
  graceful degrade-to-cli, never degrade-to-empty.
- `hooks.WorkerState` gained a `transport: str = "cli"` field captured at
  worker-spawn time so the daemon worker forwards the transport to
  `cli_runner.run_store`. Worker re-reads happen at spawn, not per-task,
  so a config edit mid-session won't race the worker.

### Failure-mode policy (MCP transport)

- **Daemon dies mid-call** → `cli_runner` logs a WARNING, respawns once
  with the cached args, retries the request.
- **Second consecutive death** → `_mcp_disabled` sentinel set, every
  subsequent `_mcp_*` call short-circuits to `ICMNotFoundError`. Upstream
  `tools._run_read` already catches `ICMError` and degrades to the
  documented empty-payload shape; `provider.prefetch` returns `""`.
- **`mcp_start` fails at initialize time** → provider falls back to
  `transport: cli` and continues operating. Operators see one WARNING
  per session; no exception escapes the provider boundary (AD-07
  invariant preserved).
- **JSON-RPC response never arrives** → `ICMTimeoutError` after the
  per-call `timeout_ms` budget elapses; same upstream degrade as a
  CLI-path timeout.

### Pi-friendly recipe

Add this to your Hermes memory-provider config to get fast semantic
recall on Pi-class hardware (4 GB Raspberry Pi 4 or similar):

```yaml
transport: mcp
use_embeddings: true
```

First recall: ~50 s (model cold-start). Every subsequent recall: <1 s.
Operators on desktop / cloud can leave both settings at default —
`transport: cli` + `use_embeddings: true` already gets them semantic
recall with no behavioural change from v0.1.1.

### Migration from v0.1.1

No action required. Default settings (`transport: cli`,
`use_embeddings: true`, `isolated: false`) preserve v0.1.1 behaviour
bit-for-bit.

### Limitations / Out of scope

- `transport: mcp` works for both reads (recall / topics / health /
  prefetch) and writes (`icm_memory_store` over MCP). However, in the
  default-shared mode (`isolated: false`) the worker still no-ops
  because `_ensure_worker` short-circuits when `_db_path is None` —
  carry-over from v0.1.1's "shared-DB writes need a v0.3 review"
  position. Operators wanting MCP-mediated writes today should set
  `isolated: true`.
- Auto-detection of Pi-class hardware (to default to `mcp` there) is a
  v0.3 concern.
- Windows is unsupported. `icm serve` spawning + signal handling tested
  on Linux + macOS only.

## [0.1.1] — 2026-05-06

Pi-deployment fixes + restoration of the brief's "shared memory with editors"
value prop.

### Changed (default-flip — see Migration below)

- **DB sharing is now opt-out instead of opt-in.** `provider.initialize` no
  longer eagerly resolves `<hermes_home>/icm/<profile>.db`. By default
  (`isolated=false`) the plugin omits `--db` so the `icm` CLI uses its
  OS-canonical default DB — the same SQLite file Claude Code, Cursor,
  OpenCode, Codex CLI, etc. already share. Recovers the original brief's
  promise: "Shared memory with editors, not a parallel silo."
- **`icm recall` runs semantic search by default; Pi users opt out.**
  The new `use_embeddings` config key defaults to `true` (the Brief's
  value prop — semantic recall via the multilingual-e5-base ONNX model).
  Set to `false` to fall back to keyword-only recall. The Pi 4 deploy
  surfaced the trade-off: the ONNX model loads from scratch on every
  subprocess invocation (~50 s on a 4 GB Pi 4), which blows past the
  default 2000 ms read timeout. Pi-class operators should set
  `use_embeddings: false` in their hermes config until v0.2's
  `icm-serve` MCP transport amortizes the model load. Desktop / cloud
  hosts are fine with the default.

### Added

- New config key `isolated` (bool, default `false`). Set to `true` to
  restore the v0.1.0 silo behaviour (`<hermes_home>/icm/<profile>.db`
  per-profile DB path, `--db` forwarded, profile isolation enforced).
- New config key `use_embeddings` (bool, default `true`). Set to `false`
  on Pi-class hardware (or any host that can't sustain the ONNX cold
  start inside `command_timeout_read_ms`) to fall back to keyword-only
  recall.
- `cli_runner.run_recall` accepts `use_embeddings: bool = True` kwarg
  (keyword-only) and conditionally appends `--no-embeddings` when
  ``False``.
- Default-shared mode flows `db_path=None` end-to-end: `cli_runner` omits
  `--db`, `hooks.run_prefetch` and `hooks.worker_loop` accept
  `Path | None`, and `tools._run_read` passes the same `None` through to
  `cli_runner`.
- `tests/conftest.py` ships an `isolated_provider` fixture for tests that
  need a concrete `_db_path` (write-path coverage, profile-isolation tests).

### Fixed

- `tools._run_read`'s "provider not initialized" guard now keys off
  `_init_args` instead of `_db_path`. Default-shared mode legitimately has
  `_db_path is None` after a successful `initialize`; the previous guard
  short-circuited every read tool to the empty-payload degrade shape.
- `provider.prefetch` no longer rejects `_db_path is None`. The
  `or self._db_path is None` clause was the read-path counterpart of the
  same regression and is removed.
- `hooks.{run_prefetch, worker_loop, ensure_worker, _spawn_worker}` and
  `cli_runner.{run_recall, run_topics}` typings allow `Path | None` so
  `mypy --strict` passes on the full `hermes_icm_memory tests` scope.

### Limitations / Out of Scope

- Default-shared mode supports **reads** (recall / topics / health /
  prefetch / system_prompt_block) end-to-end. **Writes** (sync_turn →
  bounded queue → worker) still require a concrete `_db_path`; in
  default-shared mode `_ensure_worker` short-circuits and writes silently
  no-op. Operators who need writes today must set `isolated: true`.
  Shared-DB writes against the canonical SQLite file (concurrent-writer
  semantics, schema-version coordination with Claude Code et al.) are a
  v0.2 concern.

### Migration from v0.1.0

If you relied on the v0.1.0 default behaviour (per-profile parallel silo
under `<hermes_home>/icm/<profile>.db`), set `isolated: true` in your
Hermes memory-provider config to restore it:

```yaml
# Restores v0.1.0 silo behaviour
isolated: true
```

If you're on Pi-class hardware (4 GB Raspberry Pi 4 or similar where the
ONNX model load blows past `command_timeout_read_ms`), additionally set:

```yaml
# Pi-class escape hatch — keyword-only recall
use_embeddings: false
```

Desktop / cloud hosts are fine with the default `use_embeddings: true`
and gain the Brief's semantic-recall value prop out of the box.

## [0.1.0] — 2026-05-05

Initial release. 14-story BMAD sprint (S01–S14) shipping a
`MemoryProvider` plugin for Hermes Agent backed by the local `icm` CLI.
Provides `prefetch` / `system_prompt_block` / `sync_turn` /
`on_session_end` hooks, four LLM-facing tools (`icm_recall`, `icm_store`,
`icm_topics`, `icm_health`), bounded-queue daemon writer, profile
isolation, full failure-mode degrade matrix, and integration tests against
a real `icm` binary.
