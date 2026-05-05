# hermes-icm-memory

A [Hermes Agent](https://hermes-agent.nousresearch.com/) memory provider plugin backed by [ICM](https://github.com/rtk-ai/icm) (Infinite Context Memory).

> Status: **early development** — BMAD planning artifacts being authored. v1 ships once stories complete and tests pass.

## What it does (v1, planned)

- Persists agent context across sessions to ICM (cross-editor SQLite memory store with semantic recall and decay).
- Exposes `recall` / `store` tools to the LLM via the Hermes plugin protocol.
- Auto-extracts decisions, errors-resolved, user preferences, and task summaries via the standard memory triggers.
- Ships an `is_available()` check that requires a working `icm` CLI on PATH.
- Profile-isolated: all paths derive from `kwargs['hermes_home']` per the Hermes memory provider contract.

## Why

Hermes's default memory providers either don't persist long-term or require running services. ICM gives you a single SQLite file with semantic search and a CLI that already integrates with Claude Code, Cursor, and other editors via hooks. This plugin lets the *same memory* be shared between your Hermes agent and your editor sessions.

## Status

| Phase | State |
|---|---|
| Product Brief | pending |
| PRD | pending |
| Architecture | pending |
| Epics + Stories | pending |
| Implementation | pending |
| OSS publish | pending |

## License

Apache-2.0 — see [LICENSE](./LICENSE).
