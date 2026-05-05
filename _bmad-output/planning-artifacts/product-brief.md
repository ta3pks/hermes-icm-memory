# Product Brief: hermes-icm-memory

**Status:** Approved (BMAD Phase 1, 2026-05-05) · **Author:** Manager (BMAD greenfield kickoff) · **License:** Apache-2.0

## Executive Summary

`hermes-icm-memory` is a [Hermes Agent](https://hermes-agent.nousresearch.com/) memory provider plugin that uses [ICM](https://github.com/rtk-ai/icm) (Infinite Context Memory) as its backing store. It gives Hermes agents long-term, semantically-recallable memory that persists across sessions and is shareable with editor sessions (Claude Code, Cursor, OpenCode, Copilot CLI, Gemini, Windsurf — every editor ICM already integrates with via hooks).

The plugin is a thin, well-tested adapter: Hermes calls the standard memory-provider lifecycle (`save_config`, `get_tool_schemas`, `handle_tool_call`, plus optional `prefetch`, `system_prompt_block`, `sync_turn`, `on_session_end` hooks) and the plugin shells out to the local `icm` CLI. No long-running daemon, no separate database, no embedding service to manage — `icm` already owns all of that.

It exists because today the Hermes ecosystem has no first-class persistent memory provider that survives a process restart and can be shared between an agent and a human's editor sessions. The plugin author's daily reality already runs ICM across Claude Code + Hermes; this plugin closes the loop so Hermes is a first-class participant in that shared memory.

## The Problem

Hermes is a capable local agent runtime, but it forgets between sessions unless the user wires up their own persistence. Today's options force an unhappy choice:

- **Default in-process memory** — vanishes at process exit, no semantic recall, no cross-session continuity.
- **Build-your-own provider** — hours of plumbing per user, custom schema, hand-rolled embeddings, no reuse across editors.
- **Service-backed providers** (e.g. mem0, Letta) — require a running server, extra ops surface, and don't share storage with the user's editor memory.

Meanwhile, the user already runs ICM on their machine for editor memory: it's a single SQLite file with semantic recall, decay, and hybrid-fused search; it's wired into Claude Code, Cursor, OpenCode, Codex CLI, Copilot CLI, and Gemini via auto-installed hooks. ICM remembers what was decided, what failed, what the user prefers — across editors. But Hermes can't see any of that, and Hermes's own context evaporates each session. The two worlds don't talk.

The cost: every Hermes conversation starts cold. Repeated explanations. Re-discovered gotchas. Decisions that the user already made elsewhere, made again. Memory islands that should be one continent.

## The Solution

A drop-in Hermes plugin that registers a `MemoryProvider` named `icm`. After `hermes memory setup icm`, every Hermes session:

1. **Recalls** relevant prior context at session start (via `prefetch` and `system_prompt_block` hooks), using ICM's semantic search.
2. **Stores** decisions, errors-resolved, user preferences, and significant task summaries automatically (via `sync_turn` and explicit tool calls), following the same five mandatory triggers ICM already enforces in editor sessions.
3. **Exposes tools** `icm_recall`, `icm_store`, `icm_topics`, `icm_health` so the LLM can drive memory directly when the heuristics aren't enough.
4. **Stays quiet** — non-blocking writes (daemon thread) so it never adds latency to a turn.

All paths derive from `kwargs['hermes_home']` (per the Hermes memory provider contract), so multiple Hermes profiles each get their own ICM database without collision.

## What Makes This Different

- **Shared memory with editors, not a parallel silo.** Anything Hermes learns is immediately searchable from Claude Code, OpenCode, Codex, etc. (and vice versa). No other Hermes provider does this today.
- **No service to run.** `icm` is a CLI; the plugin shells out. There's nothing to systemctl, no port to defend, no Docker.
- **Decay model + hybrid recall built in.** ICM already gives temporal decay, semantic + keyword fusion, importance levels, and consolidation — the plugin inherits all of that for free. Mem0/Letta-style providers reinvent these pieces.
- **Cross-platform by inheritance.** ICM ships native binaries for x86_64 + aarch64 (we built one for the Pi 4 last week) and runs on Fedora, Debian, macOS. The plugin works wherever ICM does.
- **Apache-2.0, no vendor lock-in.** The Hermes-side adapter is thin and replaceable; ICM is open source upstream.

The honest moat is execution: getting the lifecycle hooks right (especially `sync_turn` non-blocking + `is_available` gating + profile isolation), and getting the topic/type mapping right so a Hermes "decision" becomes an ICM `decisions-{project}` entry with the right importance — not just a JSON dump of conversation turns.

## Who This Serves

**Primary user — the local-AI hobbyist/builder.** Runs Hermes on their own machine, often alongside Claude Code or Cursor. Already has opinions about memory. Wants their agents to actually remember things between Tuesday and Thursday. Cares about ops simplicity and self-hostability. Today maintains hand-rolled persistence or has given up.

**Secondary — the Hermes plugin author.** Building agent workflows that span sessions (research agents, on-call helpers, long-horizon coding loops). Needs a memory backend they can trust without standing up infrastructure.

**Not the target (v1):** team/multi-tenant deployments. ICM is single-user by design; we're not contorting the plugin to fit shared deployments. v2 may revisit this if demand exists.

## Success Criteria

**Functional (acceptance):**

- ✅ `hermes memory setup icm` succeeds end-to-end on a machine with `icm` on PATH; `is_available()` returns false (with a helpful message) when it's not.
- ✅ A Hermes session that runs, exits, and restarts can recall facts stored in the previous session via the LLM's `icm_recall` tool call AND via auto-injected `system_prompt_block`.
- ✅ A decision recorded by the user in Claude Code (`/remember` or auto-extract) is recallable in a fresh Hermes session — same SQLite DB, no sync layer.
- ✅ `sync_turn` never blocks the turn; latency overhead at p95 is < 50ms (measured by a Hermes turn benchmark with the plugin enabled vs disabled).
- ✅ Profile isolation verified: two Hermes profiles produce two ICM databases, no leakage between them.

**Quality:**

- ✅ Test coverage ≥ 85% for the plugin module (pytest); CI runs on every PR.
- ✅ Zero warnings on `ruff check` + `mypy --strict` (or accepted reasons documented).
- ✅ README has a 3-step quickstart; a new user can install and verify in < 5 minutes.

**Adoption (post-launch, v1 → v1.1):**

- ⛳ At least one external user opens an issue or PR within 30 days of release (signal of real-world usage).
- ⛳ Listed in the Hermes plugin docs / community plugin index.

## Scope

### In (v1)

- `MemoryProvider` subclass `IcmMemoryProvider` registered via `ctx.register_memory_provider(...)`.
- `is_available()` — checks `icm` on PATH, no network calls.
- `initialize(session_id, hermes_home)` — derives DB path under `hermes_home/icm/`, calls `icm init` if needed.
- `get_tool_schemas()` + `handle_tool_call()` — exposes `icm_recall`, `icm_store`, `icm_topics`, `icm_health` to the LLM.
- Hooks: `prefetch` (semantic recall by recent turn content), `system_prompt_block` (inject top-K recalled memories + project context summary), `sync_turn` (non-blocking write of detected triggers via daemon thread), `on_session_end` (flush + lightweight consolidation).
- Importance + topic mapping: errors-resolved/preferences/decisions/learnings/context-{project} — same five triggers as the editor-side rules.
- `get_config_schema()` + `save_config()` — user-tunable: importance default, topic prefix, recall limit, prefetch on/off.
- Apache-2.0 LICENSE + README + minimal CONTRIBUTING + GitHub Actions CI (lint + tests on Python 3.11/3.12).
- pip-installable package (`pyproject.toml`, entry point `hermes_agent.plugins`) plus instructions for project-local `.hermes/plugins/` install.
- Test suite: unit tests (mock `icm` subprocess), integration tests (real `icm` against a temp DB).

### Out (v1)

- ICM MCP server transport. CLI shellout only for v1; MCP integration is a v2 concern.
- Multi-user / shared-DB deployments.
- A web UI / dashboard for browsing memories (ICM's own CLI suffices).
- Custom embedding models. ICM's default (multilingual-e5-base) is fine; user can configure ICM separately.
- Migration tooling from other memory providers (mem0, Letta). Out of scope.
- Synchronous turn writes. We're committed to non-blocking from day one.
- Cross-machine sync. Same machine only; if user wants sync, that's an ICM-level concern.

## Technical Approach (high-level)

```
┌──────────────────┐                ┌──────────────────────┐
│ Hermes Agent     │                │ ICM (CLI on PATH)    │
│ (LLM turn loop)  │                │  - SQLite DB         │
│                  │  shell out     │  - embeddings        │
│  ┌──────────────┐│   subprocess   │  - semantic recall   │
│  │ icm provider │├───────────────►│  - hybrid search     │
│  │  (this plug) ││                │  - decay + prune     │
│  └──────────────┘│                │                      │
│                  │                │ Same DB also used by │
│   prefetch ─────►│ recall         │ Claude Code, Cursor, │
│   system_prompt◄─│ ←context       │ OpenCode, etc.       │
│   sync_turn ────►│ store (async)  │                      │
└──────────────────┘                └──────────────────────┘
```

- **Language:** Python 3.11+ (matches Hermes plugin runtime).
- **Process model:** Synchronous reads (recall) on the hot path (with cache); writes off-thread via daemon (`threading.Thread(daemon=True)` or `asyncio.create_task` depending on Hermes runtime model — to be confirmed in architecture phase).
- **Failure modes:** If `icm` subprocess exits non-zero, log + degrade (return empty recall, drop write); never raise into the agent's turn.
- **Test strategy:** subprocess mocking via `unittest.mock` for unit tests; a `pytest` fixture that spawns a real `icm` against `tmp_path` for integration. CI installs `icm` from the upstream prebuilt or builds it.

Detailed component design lives in the Architecture document (next BMAD phase).

## Vision

In 2-3 years: `hermes-icm-memory` is the default-recommended memory provider for Hermes users who already run ICM, and a reference implementation for anyone authoring memory providers against other backends. The shared editor-agent memory pattern becomes obvious in retrospect: agents that don't remember what their human collaborator already learned look broken.

Stretch v2+ ideas (out of v1 scope, captured for context):

- ICM MCP transport (skip subprocess overhead).
- Bidirectional `learn` / `feedback-record` integration (Hermes turns inform ICM's prediction-correction loop).
- A `hermes-icm-recall-only` lite mode for read-only/audit deployments.
- A small companion CLI `hermes-icm` that lets the user ask, from a regular shell, "what did my Hermes agent remember from yesterday?".

## Out-of-band notes captured during brief

- BMAD execution model: this brief was authored by the manager directly (full context + locked decisions). Downstream PRD/Architecture/Stories will be authored by a Planner teammate in fresh context, then implementation via parallel Story teammates with the mandatory 4-phase chain.
- Repo already initialized at `/home/nikos/projects/hermes-icm-memory/` with Apache-2.0 LICENSE and README placeholder; first commit `6cd0214`.
- Memory of decisions already written to ICM (`01KQWQZCVFJJW28RBKW2WHDJVG`) and the claude-memory plugin (`decision-hermes-icm-memory-project-kickoff`) per the dual-write policy.
