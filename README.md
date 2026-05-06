# hermes-icm-memory

A [Hermes Agent](https://hermes-agent.nousresearch.com/) memory provider plugin backed by [ICM](https://github.com/rtk-ai/icm) — semantic, cross-session, cross-editor recall via the local `icm` CLI.

## Why

Hermes forgets between sessions unless you wire up persistence yourself. Service-backed providers (mem0, Letta) need a daemon and a separate database. Meanwhile, you may already run ICM for editor memory: a single SQLite file with semantic search, hybrid recall, decay, and hooks into Claude Code, Cursor, OpenCode, Codex CLI, Copilot CLI, and Gemini. This plugin closes the loop — Hermes recalls from and writes to the *same* ICM database your editors use, with no service to run and no extra ops surface.

## Quickstart

Three steps, under five minutes on a machine where `icm` is already installed.

1. **Verify `icm` is on your PATH.**

   ```bash
   icm --version
   ```

   Expected: a version string (e.g. `icm 0.x.y`). If the command is not found, install ICM first from <https://github.com/rtk-ai/icm>.

2. **Install the plugin.**

   ```bash
   pip install hermes-icm-memory
   ```

   Until the first PyPI release, install from source instead:

   ```bash
   pip install git+https://github.com/ta3pks/hermes-icm-memory.git
   ```

   Expected: pip reports `Successfully installed hermes-icm-memory-<version>`.

3. **Enable and activate inside Hermes.**

   ```bash
   hermes plugins enable hermes-icm-memory && hermes memory setup icm
   ```

   Then add `mcp_servers.icm:` to `~/.hermes/config.yaml` so the LLM can
   call `icm_memory_recall`, `icm_memory_store`, `icm_memory_list_topics`,
   and `icm_memory_health` tools natively (auto-discovered from
   `icm serve`):

   ```yaml
   mcp_servers:
     icm:
       command: icm
       args: [serve, --no-embeddings]   # or omit --no-embeddings on fast hosts
       timeout: 120
       connect_timeout: 30
   ```

   Expected: Hermes reports the plugin enabled and the `icm` memory provider configured. New sessions will now recall from ICM at startup and write decisions, errors-resolved, preferences, and task summaries back to ICM after every turn — non-blockingly. The plugin auto-injects recalled memories into the system prompt every turn (via the `prefetch` lifecycle hook); the LLM gets `icm_memory_*` tools on demand from the hermes-native MCP server.

## Architecture (v0.3)

```
┌─────────────────────────────────────────────────────────────────┐
│                       hermes-gateway                            │
│                                                                 │
│  ┌──────────────────────┐         ┌──────────────────────────┐  │
│  │ memory_manager       │         │ mcp_servers.icm          │  │
│  │ (lifecycle hooks)    │         │ (long-lived ``icm serve``)│  │
│  │                      │         │                          │  │
│  │ ┌──────────────────┐ │         │ ┌──────────────────────┐ │  │
│  │ │hermes-icm-memory │ │         │ │ icm_memory_recall    │ │  │
│  │ │  (this plugin)   │ │         │ │ icm_memory_store     │ │  │
│  │ │                  │ │         │ │ icm_memory_list_topics│ │  │
│  │ │ • prefetch()     │ │         │ │ icm_memory_health    │ │  │
│  │ │ • system_prompt_ │ │         │ └──────────────────────┘ │  │
│  │ │   block()        │ │         │     ▲ JSON-RPC           │  │
│  │ │ • sync_turn()    │ │         │     │ (LLM tool calls)   │  │
│  │ │ • shutdown()     │ │         └─────┼────────────────────┘  │
│  │ └────────┬─────────┘ │               │                       │
│  └───────────┼──────────┘               │                       │
│              │ icm subprocess           │                       │
│              │ (keyword recall, store)  │                       │
└──────────────┼──────────────────────────┼───────────────────────┘
               ▼                          ▼
         ┌──────────────────────────────────┐
         │   icm SQLite DB (shared)         │
         │   ~/.local/share/icm/memories.db │
         └──────────────────────────────────┘
```

The plugin owns **lifecycle**: auto-prefetch on prompt-submit
(`prefetch()` → cached hits → `system_prompt_block()` prepended into
every turn's system prompt) and auto-store on triggered turns
(`sync_turn()` → bounded queue → daemon worker → fresh `icm` subprocess
per write). Hermes-native `mcp_servers.icm:` owns **tool exposure**:
the LLM calls `icm_memory_*` directly through hermes' MCP client, with
the embedding model warm-cached in the long-lived `icm serve` daemon.

## Features

- **Shared memory with editors, not a parallel silo.** Anything Hermes learns is immediately searchable from Claude Code, OpenCode, Codex, etc., and vice versa.
- **No service to run.** `icm` is a CLI; the plugin shells out. Nothing to systemctl, no port to defend, no Docker.
- **Decay model + hybrid recall built in.** Temporal decay, semantic + keyword fusion, importance levels, and consolidation come from ICM upstream — the plugin inherits all of it.
- **Cross-platform by inheritance.** Works wherever ICM does (x86_64 + aarch64 on Fedora, Debian, macOS).
- **Profile-isolated.** All paths derive from `kwargs['hermes_home']`; multiple Hermes profiles get their own ICM database with no leakage.
- **Non-blocking writes.** `sync_turn` returns within milliseconds; the agent's turn loop never waits on disk or subprocess I/O.
- **Apache-2.0, no vendor lock-in.** Thin replaceable adapter on the Hermes side; ICM is open source upstream.

## Configuration

User-tunable keys (importance default, recall limit, queue size, timeouts, prefetch toggle, etc.) are documented in [`_bmad-output/planning-artifacts/architecture.md`](./_bmad-output/planning-artifacts/architecture.md) §10.1. Configure via `hermes memory setup icm` or by editing the Hermes memory provider config for your profile.

### v0.1.1 — sharing vs. isolation, embeddings vs. keyword-only

Two keys control the trade-offs surfaced by the Pi 4 deploy on 2026-05-06; both default to the safer-for-most-users option:

- **`isolated`** *(bool, default `false`)* — when `false`, the plugin omits `--db` and lets `icm` use its canonical OS-default database (the same SQLite file Claude Code, Cursor, OpenCode, etc. already share). This is the brief's "shared memory with editors" value prop. Set to `true` to opt into the v0.1.0 behaviour: a per-profile silo at `<hermes_home>/icm/<profile>.db`. Profile isolation requires `isolated: true`.
- **`use_embeddings`** *(bool, default `true`)* — when `true`, `icm recall` uses semantic search via the configured icm embedding model (the Brief's value prop). Set to `false` on Pi-class hardware or anywhere the multilingual-e5-base ONNX model load (~50 s per subprocess invocation on a 4 GB Pi 4) blows past `command_timeout_read_ms`. Desktop / cloud hosts handle the default fine.

### v0.3 — hermes-native MCP for tools, lifecycle-only plugin

The v0.2 `transport: mcp` config was removed. Hermes-Agent v0.3.0+
ships first-class `mcp_servers.<name>:` config — hermes itself spawns
`icm serve`, completes the JSON-RPC handshake, auto-discovers tools,
and registers them alongside built-ins. The plugin's responsibility
shrank to lifecycle hooks (which only it can do):

- **`prefetch` + `system_prompt_block`** — auto-injection of recalled
  memories into the system prompt every turn. Uses fresh `icm` keyword
  subprocesses (sub-100 ms on Pi when `use_embeddings: false`).
- **`sync_turn`** — non-blocking trigger detection + bounded-queue daemon
  writer (FIFO, drop-on-full per AD-04, lazy-respawn AD-15).
- **`shutdown`** — explicit no-op so hermes' `memory_manager` stops
  logging "object has no attribute 'shutdown'" on every gateway
  restart.

LLM-side semantic recall is handled by `mcp_servers.icm:`: the
embedding model loads once at hermes startup, every subsequent
`icm_memory_recall` call is sub-second. See the migration note in
[CHANGELOG.md](./CHANGELOG.md) for upgrading from v0.2.

**Pi recipe.** Add this to your `~/.hermes/config.yaml`:

```yaml
plugins:
  hermes-icm-memory:
    use_embeddings: false        # plugin's prefetch hot-path: keyword-only

mcp_servers:
  icm:
    command: icm
    args: [serve]                # LLM tool path: warm semantic cache
    timeout: 120
    connect_timeout: 30
```

The plugin's prefetch fires keyword recall (fast on Pi); the LLM's
on-demand semantic recall flows through the warm-cache hermes daemon.
Best of both worlds, zero plugin-side daemon management.

**Limitation.** Writes (`sync_turn` → bounded queue → daemon worker)
still require a concrete `_db_path`, so under the default
`isolated: false` the worker no-ops and writes are dropped silently.
Operators who need plugin-side writes today should set `isolated: true`.
Shared-DB writes against the canonical icm file (concurrent-writer
semantics with editors) is a v0.4 concern.

## Development

Contributions welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the dev install, the lint / type-check / test loop, the coverage gate, and the TDD policy.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

## Links

- ICM (upstream memory store): <https://github.com/rtk-ai/icm>
- Hermes plugin docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>
- Hermes memory-provider docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers>
