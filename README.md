# hermes-icm-memory

A [Hermes Agent](https://hermes-agent.nousresearch.com/) memory plugin backed by [ICM](https://github.com/rtk-ai/icm) — semantic, cross-session, cross-editor recall via the same SQLite database your editors already use.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE) ![Coverage: 95%](https://img.shields.io/badge/coverage-95%25-brightgreen) ![Version: 0.3.0](https://img.shields.io/badge/version-0.3.0-blue)

## Why

Hermes forgets between sessions unless you wire up persistence yourself. Service-backed providers (mem0, Letta) want a daemon and a separate database. If you already run ICM for your editors — Claude Code, Cursor, OpenCode, Codex CLI, Gemini, Copilot — your agent should be reading and writing to that same store, not a parallel silo.

This plugin closes the loop. **One SQLite file. Shared with your editors. No service to run.**

## Quickstart

Three steps, under five minutes if `icm` is already on your `$PATH`.

**1. Verify ICM is installed.**

```bash
icm --version    # expected: e.g. "icm 0.10.43"
```

If not found, install from <https://github.com/rtk-ai/icm>.

**2. Install the plugin.**

```bash
pip install hermes-icm-memory
```

PyPI release pending; until then install from source instead:

```bash
pip install git+https://github.com/ta3pks/hermes-icm-memory.git
```

**3. Wire it into Hermes.**

The `hermes memory setup icm` wizard walks you through it, or you can do it by hand — add the following to `~/.hermes/config.yaml`:

```yaml
memory:
  provider: hermes-icm-memory          # auto-prefetch + auto-store lifecycle hooks

mcp_servers:
  icm:
    command: icm                       # gives the LLM ~30 native icm_memory_* tools
    args: [serve]                      # add --no-embeddings on Pi-class hardware
    timeout: 120
    connect_timeout: 30
```

Then enable the plugin and restart hermes-gateway:

```bash
hermes plugins enable hermes-icm-memory
systemctl --user restart hermes-gateway   # or however you run it
```

You should see in the gateway log:

```
INFO tools.mcp_tool: MCP server 'icm' (stdio): registered N tool(s): mcp_icm_icm_memory_recall, mcp_icm_icm_memory_store, ...
INFO agent.memory_manager: Memory provider 'icm' registered
```

That's it. Every new turn auto-prefetches recalled memories into the system prompt; every triggered turn auto-writes back to ICM in the background; the LLM can call `icm_memory_recall` / `icm_memory_store` / `icm_memory_*` directly when it wants explicit control.

## Architecture (v0.3)

The plugin owns the **lifecycle hooks** — auto-injection on prompt-submit, auto-store on triggered turns. Hermes-native `mcp_servers.icm:` owns **tool exposure** — the LLM gets the full `icm` MCP surface (recall, store, memoirs, feedback, transcripts, learn, consolidate, ~30 tools total) with the embedding model warm-cached in a long-lived `icm serve` daemon.

```
┌────────────────────────────────────────────────────────────────┐
│                       hermes-gateway                           │
│                                                                │
│  ┌────────────────────────┐      ┌─────────────────────────┐   │
│  │ memory_manager         │      │ mcp_servers.icm         │   │
│  │ (lifecycle hooks)      │      │ (long-lived icm serve)  │   │
│  │                        │      │                         │   │
│  │ ┌────────────────────┐ │      │  • icm_memory_recall    │   │
│  │ │ hermes-icm-memory  │ │      │  • icm_memory_store     │   │
│  │ │  (this plugin)     │ │      │  • icm_memoir_*         │   │
│  │ │                    │ │      │  • icm_feedback_*       │   │
│  │ │ • prefetch()       │ │      │  • icm_transcript_*     │   │
│  │ │ • system_prompt_   │ │      │  • icm_learn            │   │
│  │ │     block()        │ │      │  • …~30 tools           │   │
│  │ │ • sync_turn()      │ │      │                         │   │
│  │ │ • shutdown()       │ │      │     ▲ JSON-RPC          │   │
│  │ └────────┬───────────┘ │      │     │ (LLM tool calls)  │   │
│  └──────────┼─────────────┘      └─────┼───────────────────┘   │
│             │ icm subprocess           │                       │
│             │ (keyword recall, store)  │                       │
└─────────────┼──────────────────────────┼───────────────────────┘
              ▼                          ▼
        ┌────────────────────────────────────┐
        │   icm SQLite DB (shared)           │
        │   ~/.local/share/icm/memories.db   │
        └────────────────────────────────────┘
```

Two paths into the same database:

- **Plugin-side (auto-injection).** `prefetch()` runs a fresh keyword `icm recall` per turn, caches the hits, and `system_prompt_block()` formats them into the prompt prepend. Sub-100 ms on a 4 GB Raspberry Pi 4 with `use_embeddings: false`. The LLM sees the recalled memories without ever calling a tool.
- **LLM-side (on-demand).** When the LLM wants to search semantically, fetch a memoir, or consolidate explicitly, it calls `icm_memory_*` directly through hermes' MCP client. The embedding model lives in the persistent `icm serve` daemon, so warm calls return in 100–250 ms even on Pi-class hardware.

## Features

- **Shared memory with editors, not a parallel silo.** Anything Hermes learns is searchable from Claude Code, OpenCode, Codex CLI, Gemini, etc., and vice versa.
- **No service to run.** `icm` is a CLI; the plugin shells out for hot-path writes/reads, hermes manages the long-lived MCP daemon. Nothing to systemctl yourself, no port to defend, no Docker.
- **Decay model + hybrid recall built in.** Temporal decay, semantic + keyword fusion, importance levels, and consolidation come from ICM upstream — the plugin inherits all of it.
- **Cross-platform by inheritance.** Linux + macOS, x86_64 + aarch64. Tested on Debian, Fedora, and the 4 GB Raspberry Pi 4.
- **Profile-isolated when you want it.** Default is shared with editors (`isolated: false`); flip on `isolated: true` for per-profile silos at `<hermes_home>/icm/<profile>.db`.
- **Non-blocking writes.** `sync_turn` returns within milliseconds; the agent's turn loop never waits on disk or subprocess I/O. Bounded queue + drop-on-full + lazy-respawn.
- **Apache-2.0, no vendor lock-in.** Thin replaceable adapter on the Hermes side; ICM is open source upstream.

## Configuration

Plugin config goes under `plugins.hermes-icm-memory:` in `~/.hermes/config.yaml`. All keys have defaults; you only set what you want to override.

### Hot-path config

| Key                  | Type    | Default  | What it does                                                       |
| -------------------- | ------- | -------- | ------------------------------------------------------------------ |
| `prefetch_enabled`   | bool    | `true`   | Auto-inject recalled memories into the system prompt every turn.    |
| `recall_limit`       | int     | `5`      | Max hits for prefetch.                                             |
| `use_embeddings`     | bool    | `true`   | Semantic recall on the plugin's CLI subprocess path. **Set `false` on Pi-class hardware** — the ONNX model cold-loads per fresh subprocess (~50 s on a 4 GB Pi 4), which blows past most read timeouts. The prefetch hot path then runs keyword-only and stays well under 100 ms. |
| `isolated`           | bool    | `false`  | `false` = share `icm` canonical DB with editors. `true` = per-profile silo at `<hermes_home>/icm/<profile>.db`. |

### Worker / write-path config

| Key                          | Type | Default | What it does                              |
| ---------------------------- | ---- | ------- | ----------------------------------------- |
| `default_importance`         | enum | `high`  | Importance for sync_turn-triggered writes (`critical` / `high` / `medium` / `low`). |
| `topic_prefix`               | str  | `""`    | Prefix prepended to all auto-stored topics (e.g., `"hermes-"`). |
| `sync_write_queue_size`      | int  | `64`    | Bounded queue capacity. Producer drops with one WARNING per overflow burst. |
| `command_timeout_read_ms`    | int  | `2000`  | Read timeout for `icm recall` subprocess. |
| `command_timeout_write_ms`   | int  | `5000`  | Write timeout for `icm store` subprocess. |
| `session_end_grace_ms`       | int  | `1500`  | Grace period for the worker to drain on session end. |
| `periodic_progress_every_n_turns` | int | `12` | Heartbeat-store cadence. |
| `consolidate_on_session_end` | bool | `false` | Run `icm consolidate` on session end. |

### Recipes

**Desktop / cloud (no resource constraint).** Defaults are fine; just register `mcp_servers.icm:` so the LLM has the icm tools:

```yaml
mcp_servers:
  icm:
    command: icm
    args: [serve]              # long-lived daemon, warm embedding cache
    timeout: 120
    connect_timeout: 30
```

**Pi 4 (4 GB) or other Pi-class hardware.** Disable embeddings on both paths so every recall stays keyword-only and fits inside a sane read timeout:

```yaml
plugins:
  hermes-icm-memory:
    use_embeddings: false      # plugin's prefetch hot-path: keyword-only, sub-100 ms

mcp_servers:
  icm:
    command: icm
    args: [serve, --no-embeddings]
    timeout: 120
    connect_timeout: 30
```

If you want semantic recall on a Pi, run `icm embed` on a faster machine against the same SQLite file (rsync / SSHFS / shared NFS) so the embeddings get computed off the hot path.

## Migrating from v0.2

The v0.2 `transport: mcp` config key was removed in v0.3 — hermes' first-class `mcp_servers.<name>:` (added in hermes-agent v0.3.0) replaces the plugin's duplicate transport. Three steps:

1. **Delete** `plugins.hermes-icm-memory.transport` from `~/.hermes/config.yaml`. (The plugin now ignores this key as a forward-compat passthrough; deleting it just removes a dead line.)
2. **Add** `mcp_servers.icm:` per the recipes above if it's not already there.
3. **Restart** hermes-gateway. The LLM now calls hermes-native `icm_memory_*` tools instead of the plugin's old `icm_*` wrappers. Auto-injection on prompt-submit is unchanged.

See [CHANGELOG.md](./CHANGELOG.md) for the full diff.

## Known limitations

- **Writes need a concrete `_db_path` until v0.4.** Under the recommended `isolated: false` (shared DB), `_db_path` is `None` and the write worker no-ops. Auto-store via `sync_turn` is silently dropped. If you need plugin-side writes today, set `isolated: true` to restore the v0.1.0 per-profile silo. The LLM can still write via `icm_memory_store` (hermes-native MCP) — that path works in shared mode. Concurrent-writer semantics with editors against the canonical SQLite file is the v0.4 design problem.
- **Windows is unsupported.** `icm serve` spawning + signal handling tested on Linux + macOS only.

## Development

Contributions welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the dev install, the lint / type-check / test loop, and the TDD policy.

```bash
git clone https://github.com/ta3pks/hermes-icm-memory
cd hermes-icm-memory
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q --cov=hermes_icm_memory --cov-branch --cov-fail-under=85
ruff check .
mypy --strict hermes_icm_memory tests
```

The project follows BMAD ceremony for non-trivial changes: every story runs `/bmad-create-story` → `/bmad-dev-story` → `/bmad-code-review` → `/simplify`. Story specs live under `_bmad-output/implementation-artifacts/`.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

## Links

- ICM (upstream memory store): <https://github.com/rtk-ai/icm>
- Hermes Agent: <https://hermes-agent.nousresearch.com/>
- Hermes plugin docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>
- Hermes memory-provider docs: <https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin>
