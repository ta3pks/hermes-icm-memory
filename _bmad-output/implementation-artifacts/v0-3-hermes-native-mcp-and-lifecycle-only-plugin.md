# Story v0.3: Hermes-native MCP + lifecycle-only plugin

Status: ready-for-dev
Story ID: v0.3 · Epic: post-v0.1.0 hardening · Effort: L · Dependencies: v0.2 (`transport: mcp` + `icm serve` integration)

## Story

As a Hermes-Agent operator running `hermes-icm-memory` on Pi-class hardware,
I want the plugin to **stop** spawning and managing its own `icm serve` subprocess,
so that:

1. Hermes' first-class `mcp_servers:` config (added in hermes-agent v0.3.0) is the single source of truth for `icm` tool exposure to the LLM, and
2. The plugin's value-add is reduced to what only it can do — **lifecycle hooks** (auto-prefetch on prompt-submit, auto-store on triggered turns) — eliminating the duplicate transport, the silent-degrade bug we hit on Pi (mcp_start hung on embedding-model cold-load → fell back to CLI → CLI timed out at 2 s every recall), and ~600 LOC of plugin code that hermes already does natively and better.

The non-negotiable: **auto-injection of recalled memories into the system-prompt prepend MUST be preserved** — that's what `prefetch()` + `system_prompt_block()` already do, and they stay.

## Why this story exists

v0.2 introduced `transport: mcp` so the plugin could spawn one long-lived `icm serve` per provider lifetime and amortize the embedding-model cold-load. That worked in isolation. But:

1. **Hermes-Agent v0.3.0 (March 17, 2026) shipped `mcp_servers.<name>:` as a first-class config-driven extension surface** — hermes itself spawns the MCP subprocess, completes the JSON-RPC handshake, auto-discovers tools, and registers them alongside built-ins. The user's gateway already has `mcp_servers.icm.command: icm` + `args: [serve, --no-embeddings]` in `~/.hermes/config.yaml`, and hermes successfully spawns `icm serve` from that config. The plugin's `transport: mcp` machinery is now duplicate work running *inside* the same Hermes process that already does the same thing better.

2. **The duplicate transport caused a real Pi outage.** On 2026-05-06, the plugin's `mcp_start` raised silently in initialize() (cause hidden behind `extra={"err": ...}` formatter that the default logger drops), the design's degrade-to-cli fallback fired correctly, and then every CLI recall timed out at 2 s because `use_embeddings: true` re-loaded the ONNX model per fresh subprocess. Hermes-side semantic recall was effectively dead for hours before the silent log was traced. Removing the duplicate transport eliminates the entire failure class.

3. **Architectural clarity.** The plugin's unique surface is the **lifecycle hooks** that no MCP server can provide: `prefetch(query)` → cached hits → `system_prompt_block()` → prepended into the LLM's system prompt every turn (auto-injection, the user's hard requirement); `sync_turn(user, assistant)` → trigger detection → enqueue `icm store` (auto-store on important turns). The `icm_recall` / `icm_store` / `icm_topics` / `icm_health` tools the plugin exposes via `tools.py` are **already** exposed natively by `icm serve` under names `icm_memory_recall` / `icm_memory_store` / `icm_memory_list_topics` / `icm_memory_health`. The plugin's `tools.py` is a wrapper providing nothing the LLM doesn't get from hermes-native MCP.

## Locked decisions (carried + new)

### Carried from v0.1.x / v0.2

- **AD-07 stays.** Failure-mode policy unchanged: every public boundary catches and returns the documented degraded shape. No exception propagates into a Hermes turn.
- **AD-12 stays.** Only `cli_runner.py` imports `subprocess`. `tests/test_no_subprocess_outside_cli_runner.py` AST-walker test stays green.
- **AD-13 stays.** `logging.getLogger(__name__)` at module scope; structured `extra={...}` for context. **NEW (this story):** also include the exception text inline in the WARNING message body via `%r` so it survives the default logger formatter. The Pi outage was diagnosed only by hand-patching this — `extra={}` alone is not debuggable in production.
- **Default-shared DB stays** (`isolated: false` is the default — `_db_path = None` lets `icm` use its OS-canonical default DB).
- **Bounded-queue worker stays** for `sync_turn` non-blocking writes (NFR-PERF-1).

### New for v0.3

- **AD-19 (new): hermes-native MCP is the only tool surface.** The plugin no longer exposes `icm_recall` / `icm_store` / `icm_topics` / `icm_health` to the LLM. Operators register `icm serve` under `mcp_servers.icm:` in `~/.hermes/config.yaml`. Hermes auto-discovers `icm_memory_*` tools and registers them alongside built-ins. The plugin documents this in README; deployment in S04 updates the user's `~/.hermes/config.yaml`.
- **AD-20 (new): plugin-internal recall uses CLI subprocess only.** `prefetch()` calls `cli_runner.run_recall()` via fresh `icm` subprocess per turn. With `use_embeddings: false` (the keyword-only mode), each call is < 100 ms — fine for the prompt-prepend hot path. Semantic recall is on-demand: when the LLM calls `icm_memory_recall` (the hermes-native MCP tool), it goes through hermes' long-lived `icm serve` and gets warm-cache speeds. Best of both worlds, zero plugin-side daemon management.
- **`transport` config field is removed.** No more `cli` / `mcp` branching. Single transport: CLI subprocess for plugin-internal calls.
- **`shutdown()` method added** to `IcmMemoryProvider` (Hermes' optional lifecycle hook). It's now a no-op (no daemon to manage) but its presence stops the spurious `'IcmMemoryProvider' object has no attribute 'shutdown'` WARNING that fires on every gateway restart under hermes' memory_manager.
- **Provider's `handle_tool_call` and `get_tool_schemas` are removed.** No tools = no schemas to advertise. Hermes' memory_manager calls these only when the provider opts in via the LLM-tool surface; removing them is a clean boundary.
- **Worker writes branch on transport is removed** in `hooks.ensure_worker` / `worker_loop`. CLI subprocess only. Keyword-only writes on Pi (no embeddings on this hardware path; embeddings are batched on Fedora via the existing icm-embed-pi.sh cron pattern).

## Acceptance Criteria

### AC1 — Plugin no longer spawns `icm serve`

- **Given** a Hermes gateway with the v0.3 plugin installed and any combination of `~/.hermes/config.yaml` plugin config,
- **When** the gateway initializes the provider (`provider.initialize(...)`) and processes any number of user turns,
- **Then** `pgrep -af "icm serve"` shows **zero** subprocesses parented by the gateway PID that were spawned by the plugin (any `icm serve` running is owned by hermes-native `mcp_servers:` config, parented by gateway directly via hermes' MCP client).

### AC2 — `transport` config field is gone

- **Given** `config.get_default_schema()`,
- **When** called,
- **Then** the returned list contains exactly **twelve** entries (the v0.1.1 set: no `transport`).
- **And** any v0.2-era config carrying `transport: mcp` or `transport: cli` validates with that key rejected as unknown (per existing schema strict-mode behavior — see existing tests on unknown keys).

### AC3 — `tools.py` is deleted; provider exposes no LLM tool surface

- **Given** `provider = IcmMemoryProvider()`,
- **When** the operator inspects the provider,
- **Then** `IcmMemoryProvider` has **no** `handle_tool_call`, no `get_tool_schemas` methods.
- **And** the file `hermes_icm_memory/tools.py` does not exist in the package.
- **And** `tests/test_tools.py` does not exist.
- **And** `__init__.py` exports unchanged: still exports `IcmMemoryProvider` (the lifecycle class).

### AC4 — `cli_runner.py` MCP transport section is deleted

- **Given** `cli_runner.py`,
- **When** grepped for `_mcp_*`, `mcp_start`, `mcp_stop`, `_McpDaemon`, `_MCP_TOOL_*`, `_MCP_PROTOCOL_VERSION`,
- **Then** zero matches.
- **And** `subprocess.Popen` appears at zero call sites (only `subprocess.run` remains, used by `_run` for one-shot CLI invocations).
- **And** `tests/test_cli_runner_mcp.py` does not exist.

### AC5 — `prefetch()` works keyword-only via fresh CLI subprocess

- **Given** the v0.3 provider with `use_embeddings: false`,
- **When** `provider.prefetch(query="obsidian")` runs,
- **Then** a single fresh `icm recall obsidian --limit 5 --format json --no-embeddings` subprocess fires (no `--db` since `_db_path is None` in default-shared mode), completes in < 1 s on Pi-class hardware,
- **And** `provider.system_prompt_block()` returns the formatted "Recalled memories:" block (cached from prefetch).
- **And** the auto-injection contract is preserved: `system_prompt_block()` reads from cache only, never invokes a subprocess.

### AC6 — `sync_turn` writes via CLI subprocess

- **Given** the v0.3 provider after a turn whose user / assistant content fires a trigger,
- **When** `provider.sync_turn(user, assistant)` runs,
- **Then** `put_nowait` enqueues a write task on the bounded queue, the daemon worker drains it, and the worker spawns one fresh `icm store -t <topic> -c <content> -i <importance> -k <keywords>` subprocess per task (no `--db` in default-shared mode).
- **And** the worker has no `transport` branch — there is exactly one code path for writes (CLI subprocess).

### AC7 — `shutdown()` exists and is a no-op

- **Given** `provider = IcmMemoryProvider()` (any state — initialised or not),
- **When** `provider.shutdown()` is called,
- **Then** it returns `None` without raising.
- **And** the gateway's `agent.memory_manager` no longer logs `'IcmMemoryProvider' object has no attribute 'shutdown'` on shutdown.

### AC8 — WARNING logs include exception text inline

- **Given** any boundary that catches and degrades (`hooks.run_prefetch`, `tools.py` removed → moved to provider boundary, `provider.prefetch`, `provider.sync_turn`, `provider.on_session_end`, `provider.shutdown`),
- **When** an `ICMError` or unexpected `Exception` is caught,
- **Then** the `logger.warning(...)` call includes the exception text in the format string itself (e.g., `"prefetch failed: %r"` with `exc` as positional arg), in addition to (not replacing) the existing `extra={"err": repr(exc), ...}` for structured logs.
- **Why:** the default Python logging formatter does not render `extra={}`. The Pi outage was undebuggable until this was hand-patched. AD-13's structured logs stay (for operators using JSON log formatters), but the human-readable exception text is now also present.

### AC9 — Test gates clean

- `pytest -q --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` — passes with no fewer than 85% branch coverage. With ~600 LOC removed, coverage % may drop slightly for the surviving modules; the gate stays at 85%.
- `ruff check .` — clean.
- `mypy --strict hermes_icm_memory tests` — clean across the (smaller) source tree.
- `tests/test_no_subprocess_outside_cli_runner.py` — still green (AD-12 invariant).
- New test: `tests/test_no_tool_surface.py` — asserts `IcmMemoryProvider` does NOT have `handle_tool_call` or `get_tool_schemas`, and that `hermes_icm_memory.tools` does not import.
- New test: `tests/test_cli_only_transport.py` — asserts `run_recall(...)`/`run_store(...)`/`run_topics(...)`/`run_health(...)` have no `transport` parameter, and `cli_runner` exports do not include `mcp_start`/`mcp_stop`.

### AC10 — README + Brief + CHANGELOG updated; version bumped to 0.3.0

- README: lead with the new architecture diagram (lifecycle hooks in plugin; tool exposure via `mcp_servers.icm`). Update the install snippet to include the `mcp_servers:` block. Remove all references to `transport: mcp` / `transport: cli`.
- CHANGELOG: `[0.3.0] - 2026-05-XX` entry: removed (transport config, MCP client, tools.py, handle_tool_call), added (`shutdown()` method, inline `%r` log format), changed (architecture pivot to hermes-native MCP for tools).
- `_bmad-output/planning-artifacts/architecture.md`: AD-19 + AD-20 added. Sections on duplicate transport / tools.py removed.
- `_bmad-output/planning-artifacts/product-brief.md`: scope updated — plugin = lifecycle, not tools.
- `_version.py`: `0.3.0`.

## Migration notes (for README / CHANGELOG)

For users upgrading from v0.2:

1. Remove `transport` from `plugins.hermes-icm-memory:` in `~/.hermes/config.yaml` (it's now ignored; will warn as unknown key).
2. Add `mcp_servers.icm:` if not already present:
   ```yaml
   mcp_servers:
     icm:
       command: icm
       args: [serve, --no-embeddings]   # or omit --no-embeddings if your hardware has fast model load
       timeout: 120
       connect_timeout: 30
   ```
3. Restart hermes-gateway. The LLM now uses `icm_memory_recall` / `icm_memory_store` / `icm_memory_list_topics` / `icm_memory_health` (hermes-native, prefixed). Auto-injection on prompt-submit continues unchanged via the plugin's lifecycle hooks.

## Out of scope

- Honcho memory provider integration (unrelated; hermes 0.3.0 ships its own).
- Replacing the bounded-queue worker with hermes' own async write infrastructure (potential v0.4).
- A "shared icm serve" mode where the plugin reuses hermes' MCP-managed daemon for prefetch (would couple plugin to hermes internals; rejected — keyword-only CLI is fast enough on Pi).

## Implementation hint (non-binding, for the dev teammate)

The deletion is significant but mechanical:

1. **Delete-pass:** `cli_runner.py` lines ~345–700 (the entire "MCP transport (v0.2)" private section + its module-level state); `tools.py` whole file; `tests/test_cli_runner_mcp.py`; `tests/test_tools.py`; the `transport` schema entry; the `transport` column from the config validation matrix.
2. **Trim-pass:** `provider.py` — remove `handle_tool_call`, `get_tool_schemas`, the `mcp_start` block in `initialize`, the `cli_runner.mcp_stop()` call in `on_session_end`. Add `shutdown()` no-op. Inline `%r` in WARNING messages.
3. **Trim-pass:** `hooks.py` — drop the `transport` parameter from `ensure_worker`/`worker_loop`/`run_prefetch`. Inline `%r` in WARNING messages. Drop the worker's mcp branch.
4. **Trim-pass:** `cli_runner.py` `run_recall` / `run_store` / `run_topics` / `run_health` — drop the `transport` parameter (CLI path is the only path).
5. **Tests:** delete obsolete tests as above; update remaining tests that still pass `transport=...` kwarg; add the two new invariant tests in AC9.
6. **Docs + version + changelog.**

The dev teammate runs `/bmad-dev-story` then `/bmad-code-review` then `/simplify` per the mandatory ceremony floor. Manager owns: branch + worktree setup, monitoring, merge, push, deploy, smoke-test on Pi.
