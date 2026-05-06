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

   Expected: Hermes reports the plugin enabled and the `icm` memory provider configured. New sessions will now recall from ICM at startup and write decisions, errors-resolved, preferences, and task summaries back to ICM after every turn — non-blockingly.

### Want fast semantic recall on Pi? (v0.2)

The default (`transport: cli`) spawns a fresh `icm` subprocess per call. On a 4 GB Raspberry Pi the multilingual-e5-base ONNX model takes ~50 s to load every time, so Pi users had to fall back to `use_embeddings: false` (keyword-only). v0.2 lets you keep semantic recall on Pi by amortizing the model load across one long-lived `icm serve` daemon:

```yaml
transport: mcp
use_embeddings: true
```

First recall after startup: ~50 s (model cold-start). Every subsequent recall: <1 s. The daemon lives for the duration of the Hermes provider; `on_session_end` tears it down (with an `atexit` backstop so SIGTERM-style shutdowns don't leak orphans). On desktop / cloud, the default `transport: cli` is fine — there's nothing to amortize.

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

### v0.2 — `transport: cli` vs. `transport: mcp`

- **`transport`** *(enum, default `"cli"`, choices `["cli", "mcp"]`)* — controls how `cli_runner` talks to `icm`.
  - `cli` (default) spawns a fresh subprocess per call. Simple, no daemon, no extra ops surface. Fine for desktop / cloud, where the embedding-model cold-start is filesystem-cached and effectively free.
  - `mcp` spawns one long-lived `icm serve` subprocess per provider lifetime and reuses it via newline-delimited JSON-RPC over stdin/stdout. The first recall pays the model cold-start (~50 s on Pi); every subsequent recall is sub-second. **Pair with `use_embeddings: true` for fast semantic recall on Pi.**

When `transport: mcp` fails to start (`icm` missing, handshake timeout), the provider logs a WARNING and falls back to `transport: cli` for the rest of its lifetime — graceful degrade, never a hard fail.

**Limitation.** Writes (`sync_turn` → bounded queue → daemon worker) still require a concrete `_db_path`, so under the default `isolated: false` the worker no-ops and writes are dropped silently. Operators who need writes today should set `isolated: true`. Shared-DB writes against the canonical icm file (concurrent-writer semantics with editors) is a v0.2 concern. See [CHANGELOG.md](./CHANGELOG.md) for the full migration note.

## Development

Contributions welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the dev install, the lint / type-check / test loop, the coverage gate, and the TDD policy.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

## Links

- ICM (upstream memory store): <https://github.com/rtk-ai/icm>
- Hermes plugin docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>
- Hermes memory-provider docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers>
