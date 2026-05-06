# Story v0.2: icm-serve MCP transport — amortize the embedding-model load

Status: ready-for-dev
Story ID: v0.2 · Epic: post-v0.1.0 hardening · Effort: M · Dependencies: v0.1.1 (`isolated`, `use_embeddings`, default-shared DB shipped)

## Story

As a Hermes user on Pi-class hardware (4 GB RPi 4) running ICM alongside Claude Code,
I want one long-lived `icm serve` subprocess per Hermes provider lifetime,
so that the multilingual-e5-base ONNX model loads **once** (~50 s on the first recall) and every subsequent recall is sub-second — restoring the brief's "fast semantic recall" promise on Pi.

## Why this story exists

v0.1.1 introduced `use_embeddings` as a config toggle, but the only viable default on Pi is `use_embeddings: false` (keyword-only) because each `icm recall` subprocess re-loads the ONNX model from scratch (~50 s, blowing past `command_timeout_read_ms: 2000`). The Brief promised semantic recall as the value prop. This story closes the gap by introducing a transport abstraction:

- **`transport: cli` (default, today's behavior)** — fresh `icm` subprocess per call. Simple, no daemon, model loads every time. Perfectly fine for desktop / cloud where the cold start is cached and irrelevant.
- **`transport: mcp` (new)** — one long-lived `icm serve` subprocess per Hermes provider lifetime. Model loads ONCE on the first call (~50 s), every subsequent recall sends a JSON-RPC request over the persistent stdin/stdout pipe (~50 ms). This is what the Brief promised on Pi.

`icm serve` is the canonical icm 0.10.43+ subcommand for MCP stdio transport (verified: `icm serve --help` exposes `--db`, `--no-embeddings`, `--compact`).

## Locked decisions (carried + new)

- **AD-12 stays.** All subprocess work continues to live in `cli_runner.py`. The MCP client implementation goes **inside** that file as a private section (no new files importing `subprocess`). The `tests/test_no_subprocess_outside_cli_runner.py` AST-walker test stays green.
- **AD-07 stays.** When MCP fails (subprocess died, JSON-RPC malformed), `cli_runner` raises typed errors from `errors.py`; upstream callers (`tools.py` / `hooks.py`) catch and degrade per the documented degrade matrix. No exception escapes the provider boundary.
- **AD-13 stays.** `logging.getLogger(__name__)` + structured `extra={...}` on every WARNING / DEBUG.
- **Default `transport: "cli"`** — existing users see no behavior change. Pi users opt into MCP via config. Pi-friendly combo: `transport: mcp` + `use_embeddings: true`.
- **Single `cli_runner` module, internal branch.** Inside `run_recall` / `run_topics` / `run_health`, branch on `transport`: `cli` keeps the v0.1.1 path; `mcp` calls a new private `_mcp_*` helper. Public surface unchanged.
- **MCP daemon lifecycle.** Lazy-spawn on first `_mcp_*` call. One process per `IcmMemoryProvider` lifetime (module-level state in `cli_runner` keyed by db_path is fine for v0.2 — Hermes runs one provider per service). `provider.initialize` calls `cli_runner.mcp_start(...)` only if `transport == "mcp"`; `provider.on_session_end` calls `cli_runner.mcp_stop()`. `atexit` is a belt-and-braces backstop.
- **Respawn policy.** Subprocess died mid-call → spawn-once retry; second death → set the daemon-disabled sentinel, raise `ICMNotFoundError`, callers degrade exactly as for "icm missing". No third spawn for the rest of the provider lifetime.
- **JSON-RPC framing.** Newline-delimited JSON-RPC 2.0 over stdin/stdout (the `icm serve` stdio transport). One `tools/call` per request keyed by a monotonic id; reads parse line-by-line until `{"id": <expected>}` arrives. `initialize` / `notifications/initialized` are sent once on spawn.
- **MCP tool name mapping.** `icm serve`'s tool names use the `icm_memory_*` prefix (verified by `tools/list` probe): `icm_memory_recall`, `icm_memory_list_topics`, `icm_memory_health`, `icm_memory_store`. The plugin's external surface stays `icm_recall` / `icm_store` / `icm_topics` / `icm_health`; the mapping is internal to `cli_runner._mcp_*`.
- **Response-shape adapter.** `icm_memory_recall` over MCP returns `result.content[0].text` — a formatted text blob (`[topic] **title**\n\ncontent\n\n`-separated). `cli_runner._mcp_recall` parses that into `list[{"topic": ..., "summary": ...}]` so `hooks.format_block` (which keys on `topic` + `summary`) continues to work without churn. `icm_memory_list_topics` text → `list[{"topic": ..., "count": "..."}]`. `icm_memory_health` text is wrapped in `{"raw": <text>}` (the existing health payload is opaque JSON downstream).

## Acceptance Criteria

### AC1 — New config key `transport`

- **Given** `config.get_default_schema()`
- **When** called
- **Then** the returned list contains exactly **thirteen** entries (twelve from v0.1.1 + one new `transport`).
- The new entry: `transport` (`enum`, default `"cli"`, choices `["cli", "mcp"]`), with the standard `key/type/default/secret/required/description` fields.
- The description names the trade-off ("`cli` spawns fresh subprocess per call; `mcp` spawns one long-lived `icm serve` daemon and reuses it via JSON-RPC over stdin/stdout, amortizing the embedding-model cold start").
- Validation: unknown transport names rejected; case-sensitive (`"MCP"` rejected; tests for both directions).

### AC2 — `cli_runner.run_recall` accepts `transport` kwarg

- **Given** `cli_runner.run_recall(query, limit, db_path, timeout_ms, *, use_embeddings=True, transport="cli", topic=None, project=None)`
- **When** `transport="cli"` (default)
- **Then** the implementation is the v0.1.1 fresh-subprocess path; argv shape and parsed return value are identical to v0.1.1.
- **And when** `transport="mcp"`
- **Then** `cli_runner._mcp_recall` is dispatched: lazy-spawn-or-reuse the daemon, JSON-RPC `tools/call` with `name="icm_memory_recall"` + arguments shaped from the kwargs, parse `result.content[0].text` into `list[dict]` with `{"topic", "summary"}` keys, return that list.
- `run_topics` / `run_health` / `run_store` get the same `transport` kwarg with the same branch shape (mapped to `icm_memory_list_topics` / `icm_memory_health` / `icm_memory_store`).

### AC3 — MCP daemon lifecycle: lazy spawn + reuse + stop

- **Given** `cli_runner.mcp_start(db_path, use_embeddings)` and `cli_runner.mcp_stop()` are public module-level functions
- **When** `mcp_start` is called from `provider.initialize`
- **Then** it spawns `icm serve` (with `--db` if `db_path` is not None, `--no-embeddings` if `use_embeddings` is False, otherwise no flags), sends the MCP `initialize` + `notifications/initialized` handshake, and stashes the `(Popen, stdin, stdout, lock, request_id_counter)` tuple in module state.
- **And when** the first `_mcp_recall` (or `_mcp_topics` / `_mcp_health`) is invoked
- **Then** it reuses the stashed daemon — no new spawn.
- **And when** `mcp_stop` is called from `provider.on_session_end`
- **Then** stdin is closed and the subprocess is terminated (with a short wait + `kill` fallback); module state is cleared so the next `mcp_start` spawns fresh.
- **And** `atexit` registers `mcp_stop` so a torn-down session leaves no orphan `icm serve` process.

### AC4 — Respawn-once policy + degrade-to-cli sentinel

- **Given** the daemon died mid-call (broken pipe / unexpected EOF / `Popen.poll()` non-None)
- **When** `_mcp_recall` is invoked
- **Then** it logs a WARNING, calls `_mcp_respawn()` (re-runs `mcp_start` with the cached args), retries the request once, and returns the result.
- **And given** the respawned daemon also dies
- **When** the same call is retried
- **Then** the module-level `_mcp_disabled` flag is set, `ICMNotFoundError` is raised (so upstream `_run_read` degrades to the documented empty-payload shape), and subsequent `_mcp_*` calls short-circuit to the same `ICMNotFoundError` without spawning. The flag is cleared on the next explicit `mcp_start`.

### AC5 — JSON-RPC request shape + concurrency

- **Given** the MCP daemon is alive
- **When** `_mcp_recall` is called
- **Then** it sends a single line: `{"jsonrpc": "2.0", "id": <N>, "method": "tools/call", "params": {"name": "icm_memory_recall", "arguments": {"query": ..., "limit": ..., "project": ""}}}\n` and reads response lines until it sees one matching the same `id`. (The empty-string `project` disables the implicit cwd-based project filter, which we don't want for Hermes.)
- **And** the request-id counter is monotonic per provider lifetime (no reset across calls).
- **And** access to stdin/stdout + the id counter is serialized by a `threading.Lock` (Hermes may call multiple read tools concurrently from prefetch + the agent turn loop).
- **And** unparseable response lines (no JSON, missing `id`) are skipped (keep reading) until a matching response or 2× `timeout_ms` total elapsed → raise `ICMTimeoutError`.

### AC6 — `provider` wires `transport` end-to-end

- `provider.initialize` reads `_config_str("transport")`; if `"mcp"`, calls `cli_runner.mcp_start(db_path=self._db_path, use_embeddings=self._config_bool("use_embeddings"))`. Failure (Popen raised, handshake timed out) → log WARNING, fall back to `transport: cli` for the rest of the lifetime (set `self._config["transport"] = "cli"` so subsequent reads use the CLI path).
- `provider.on_session_end` calls `cli_runner.mcp_stop()` after the existing queue drain. Safe to call when transport is `cli` (no-op).
- `provider.prefetch` and `tools._handle_recall/_handle_topics/_handle_health/_handle_store` thread `transport=provider._config_str("transport")` into `cli_runner.run_*` calls.
- New `provider._config_str(key) -> str` helper, mirror-shape with `_config_int` / `_config_bool`.

### AC7 — Hooks pass `transport` through

- `hooks.run_prefetch` accepts `transport: str = "cli"` and forwards to `cli_runner.run_recall`.
- `hooks.worker_loop` reads the cached transport (passed at thread spawn time) and forwards into `cli_runner.run_store`.
- `hooks.ensure_worker` / `_spawn_worker` accept and store `transport: str` so the worker thread can forward it without rebinding state.

### AC8 — AST invariant test stays green

- `tests/test_no_subprocess_outside_cli_runner.py` continues to pass: only `cli_runner.py` imports `subprocess` (the new MCP client lives inside that file).

### AC9 — Unit tests for the MCP transport

- `tests/test_cli_runner_mcp.py` (NEW): mocks `subprocess.Popen` + stdin/stdout `BytesIO`-style pipes; covers
  - `mcp_start` argv shape (with / without `--db`, with / without `--no-embeddings`)
  - `mcp_start` handshake: writes `initialize` + `notifications/initialized` exactly once
  - `_mcp_recall` JSON-RPC request shape (id increments, name + arguments, project="")
  - `_mcp_recall` text-response parsing into `[{"topic", "summary"}, ...]`
  - `_mcp_topics` / `_mcp_health` parsers
  - lifecycle: `mcp_stop` closes stdin and terminates Popen; subsequent `mcp_start` re-spawns
  - respawn-once policy: first death → respawn; second death → `_mcp_disabled` set, `ICMNotFoundError` raised, no third spawn
  - timeout: response never arrives → `ICMTimeoutError`
  - lock serializes concurrent calls
- `tests/test_cli_runner.py`: at least one new parameterized case proving `transport="cli"` keeps the v0.1.1 argv shape (regression guard).

### AC10 — Provider tests for transport wiring

- `tests/test_provider.py`:
  - `test_initialize_starts_mcp_when_transport_mcp` — patches `cli_runner.mcp_start` to a recorder, asserts called with the resolved `db_path` + `use_embeddings`.
  - `test_initialize_no_mcp_when_transport_cli` — recorder NOT called when `transport="cli"` (default).
  - `test_initialize_falls_back_to_cli_on_mcp_start_failure` — `mcp_start` raises → log WARNING, `_config["transport"]` flipped to `"cli"` for the lifetime.
  - `test_on_session_end_stops_mcp` — `cli_runner.mcp_stop` recorder is called, even when `transport="cli"` (it's a no-op there but the call is unconditional).

### AC11 — Integration test on real `icm serve`

- `tests/integration/test_real_icm_serve.py` (NEW), gated on `shutil.which("icm")`:
  - Spawns a real `icm serve --no-embeddings --db <tmp>` via `cli_runner.mcp_start`.
  - Stores one memory via the CLI path (or seeds the DB via `subprocess.run(["icm", ...])` — keeps the daemon read-only).
  - Calls `cli_runner._mcp_recall` twice; asserts both return non-empty hits and both round-trips complete (no latency assertion in the unit-gated suite — the `<1000 ms` reuse claim is checked in the Pi smoke).

### AC12 — README + CHANGELOG

- README "Configuration" — add the `transport` key with the trade-off table.
- README "Quickstart" — new "Want fast semantic recall?" subsection: "Pi users: set `transport: mcp` + `use_embeddings: true`. First recall ~50 s (warmup), every subsequent recall <1 s."
- CHANGELOG — v0.2 entry covering the transport abstraction, the MCP daemon lifecycle, the failure-mode policy, and a migration note ("no behavior change at default settings").

### AC13 — Version bump

- `hermes_icm_memory/_version.py` → `__version__ = "0.2.0"`.
- `pyproject.toml` `version = "0.2.0"`.
- `plugin.yaml` `version: 0.2.0`.

### AC14 — Quality gates pass on full scope

- `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → green (target: ≥ 90% on touched files; floor 85%).
- `ruff check .` → zero findings.
- `mypy --strict hermes_icm_memory tests` → zero findings.

### AC15 — Pi smoke test passes end-to-end

After `git checkout v0.2` is applied to the deployed plugin dir at `~/.hermes/plugins/hermes-icm-memory/` and `hermes-gateway.service` is restarted, the smoke probe:

```python
from hermes_icm_memory import IcmMemoryProvider
p = IcmMemoryProvider()
p._config = {"transport": "mcp", "use_embeddings": True, "isolated": False}
p.initialize(session_id="v02-smoke", hermes_home="/tmp/hh")
# first call ~50 s (model warmup)
# second call < 1000 ms (model reused via daemon)
```

Asserts: `second_call_ms < 1000` and `len(hits) > 0` on both calls (proves daemon is reused, not respawned).

## Implementation Tasks (ordered)

1. **Tests-first (RED).** Write `tests/test_cli_runner_mcp.py` with mocked Popen. Add `test_default_schema_has_thirteen_keys` + `transport` validation cases to `tests/test_config.py`. Add the four `test_initialize_*_mcp*` + `test_on_session_end_stops_mcp` cases to `tests/test_provider.py`. Add `tests/integration/test_real_icm_serve.py` (gated on `which("icm")`).
2. **GREEN — config.** Append `transport` enum entry to `_SCHEMA_ENTRIES`.
3. **GREEN — cli_runner MCP client.** New private section: `_McpDaemon` dataclass holding `Popen / stdin / stdout / lock / id_counter / cached_args / disabled`. Module-level `_mcp_state` holder. Public `mcp_start(db_path, use_embeddings)` / `mcp_stop()`. Private `_mcp_recall` / `_mcp_topics` / `_mcp_health` / `_mcp_store` / `_mcp_call(method, params, timeout_ms)` / `_parse_recall_text` / `_parse_topics_text`.
4. **GREEN — cli_runner.run_*.** Add `transport: str = "cli"` kwarg to each public function; if `"mcp"`, dispatch to the `_mcp_*` helper; else current `_cli_*` body (rename current bodies into `_cli_*` private helpers for symmetry, OR leave them inlined behind a top-of-function check — pick the lower-noise diff).
5. **GREEN — hooks.** Thread `transport: str = "cli"` through `run_prefetch`, `worker_loop`, `_spawn_worker`, `ensure_worker`. Worker reads it from `WorkerState` (new `transport: str = "cli"` field).
6. **GREEN — provider.** Add `_config_str` helper. `initialize` calls `cli_runner.mcp_start` when `transport == "mcp"`; falls back to `cli` on failure. `on_session_end` calls `cli_runner.mcp_stop` unconditionally. `prefetch` / `_ensure_worker` thread `transport` through.
7. **GREEN — tools.** `_handle_recall/_handle_topics/_handle_health` read `provider._config_str("transport")` and forward to `cli_runner.run_*`.
8. **Bump version + write CHANGELOG + README update.**
9. **Verify.** Full gates (pytest cov, ruff, mypy --strict on full scope).
10. **Pi smoke test.** Reinstall worktree branch into `~/.hermes/plugins/hermes-icm-memory/`, restart gateway, run the two-recall latency probe.

## Out of scope

- v0.2 keeps shared-DB writes off the table for `transport: cli` (carried from v0.1.1: writes need a concrete `_db_path`). Under `transport: mcp`, `_mcp_store` works with `_db_path=None` because the daemon owns the DB selection — but the `_ensure_worker` short-circuit on `_db_path is None` keeps writes off in shared-mode for parity with v0.1.1. Lifting that restriction is a v0.3 concern (concurrent-writer review on the canonical SQLite file).
- No Windows support: `icm serve` spawning + signal handling are tested on Linux + macOS only.
- No automatic transport selection: operators choose explicitly via config. Future work: auto-detect Pi-class hardware and default to `mcp`.

## Risks / breaking changes

- **None at default.** `transport: cli` keeps the v0.1.1 behavior bit-for-bit. Existing users see zero diff.
- **MCP-mode shape change.** Recall hits under `transport: mcp` carry only `{topic, summary}` (parsed from MCP text) — fewer fields than the CLI path's full JSON dicts. Downstream `format_block` only reads those two, so this is invisible to operators. If a future caller starts reading other fields, document at that time.
- **Daemon process leakage.** If `on_session_end` is never called and `atexit` is bypassed (SIGKILL), an `icm serve` process leaks. Hermes lifecycle normally calls `on_session_end`; the `atexit` registration covers normal interpreter exit. Operators on tightly-supervised systemd units should use `KillMode=mixed` so the child gets SIGTERM with the parent.

## Definition of Done

- All 15 ACs pass.
- 4-phase chain executed end-to-end on a single commit train (`docs(v0.2)/feat(v0.2)/review(v0.2)/simplify(v0.2)`).
- Pi smoke test green: second recall < 1 s.
- Status report sent to team-lead.
