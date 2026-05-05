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

## Development

Contributions welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the dev install, the lint / type-check / test loop, the coverage gate, and the TDD policy.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

## Links

- ICM (upstream memory store): <https://github.com/rtk-ai/icm>
- Hermes plugin docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>
- Hermes memory-provider docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers>
