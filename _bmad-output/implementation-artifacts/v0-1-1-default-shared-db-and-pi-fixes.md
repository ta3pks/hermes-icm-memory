# Story v0.1.1: Default-shared DB + opt-in isolation + opt-in embeddings + Pi fixes

Status: ready-for-dev
Story ID: v0.1.1 · Epic: post-v0.1.0 hardening · Effort: M · Dependencies: v0.1.0 (S01–S14 shipped)

## Story

As a Hermes user (especially on Pi-class hardware) running ICM alongside Claude Code, Cursor, OpenCode, etc.,
I want the plugin to share my canonical ICM database by default and skip embeddings on the hot path,
so that Hermes restores the brief's "shared memory with editors" promise *and* stays inside the 2-second read-timeout budget on a 4 GB Raspberry Pi.

## Why this story exists

Live Pi 4GB deploy on 2026-05-06 surfaced two regressions in v0.1.0:

1. **Architecture broke the brief's value prop.** AD-05 mandated `<hermes_home>/icm/<profile>.db` as the unconditional default. Result: every Hermes session created a fresh empty DB at `~/.hermes/icm/default.db` instead of using ICM's canonical `~/.local/share/icm/memories.db` — the file Claude Code, Cursor, OpenCode all share. Brief §"What Makes This Different" → "Shared memory with editors, not a parallel silo" was silently inverted.
2. **Embeddings cost ~50s per `icm recall` subprocess on Pi.** The `multilingual-e5-base` ONNX model loads from scratch every invocation. Default 2000ms read timeout fires every time. Prefetch always returned empty → degraded gracefully but the brief's recall promise was unobservable.

Manager hot-patched the deployed copy at `~/.hermes/plugins/hermes-icm-memory/` end-to-end (`prefetch` returns 2206 chars from the canonical DB). This story backports those fixes properly, with config plumbing + tests.

## Acceptance Criteria

### AC1 — Two new config keys appended to `_SCHEMA_ENTRIES`

- **Given** `config.get_default_schema()`
- **When** called
- **Then** the returned list contains exactly **twelve** entries (the original ten plus two new keys), each carrying the standard `key/type/default/secret/required/description` fields.
- The two new keys are:
  - `isolated` (`bool`, default `False`) — when `True`, plugin uses `<hermes_home>/icm/<profile>.db`; when `False`, plugin omits `--db` so `icm` resolves its OS-canonical default (shared with editors).
  - `use_embeddings` (`bool`, default `False`) — when `True`, `icm recall` runs with semantic search; when `False`, plugin appends `--no-embeddings` so recall is keyword-only and instant.
- Defaults are chosen for the brief + Pi: shared DB, no embeddings.

### AC2 — `cli_runner.run_recall` accepts `use_embeddings` kwarg

- **Given** `cli_runner.run_recall(query, limit, db_path, timeout_ms, *, use_embeddings: bool = False, topic=None, project=None)`
- **When** `use_embeddings=False` (default)
- **Then** `--no-embeddings` is appended to argv.
- **And when** `use_embeddings=True`
- **Then** argv contains no `--no-embeddings` flag (icm uses its configured embedding model).
- The hot-patch hardcode of `--no-embeddings` is removed; behavior is now config-driven.
- `run_topics` / `run_health` argv shape is unchanged — `--no-embeddings` is recall-only on icm 0.10.43.

### AC3 — `provider.initialize` is shared-DB by default, isolated-DB on opt-in

- **Given** `provider.initialize(session_id, hermes_home, profile=None)` with default config (`isolated=False`)
- **When** called
- **Then** `provider._db_path` stays `None` (no path resolution, no `mkdir_parent`, no filesystem touch under `<hermes_home>/icm/`).
- **And given** `provider._config["isolated"] = True` *before* `initialize` is invoked
- **When** `initialize` is called
- **Then** it resolves `<hermes_home>/icm/<profile>.db`, calls `mkdir_parent`, and stores the path as `_db_path`. OSError still triggers self-disable (failure-mode §6.3 row 8 unchanged).
- The idempotent re-init guard (same `args_key`) still works in both modes.

### AC4 — `provider.prefetch` no longer guards on `_db_path is None`

- **Given** a default-mode provider (`_db_path is None`)
- **When** `prefetch` is called and `is_available()` is `True`
- **Then** `prefetch` proceeds (passes `db_path=None` through `hooks.run_prefetch` → `cli_runner.run_recall` → `_db_args(None)` → no `--db` argv element → icm uses canonical default).
- The legacy `or self._db_path is None` short-circuit is removed.

### AC5 — `tools._run_read` guards on `_init_args is None`, not `_db_path is None`

- **Given** a freshly-constructed provider (no `initialize` called)
- **When** any read tool (`icm_recall`, `icm_topics`, `icm_health`) is invoked
- **Then** the read short-circuits to the documented degrade payload + WARNING (`"…: provider not initialized"`).
- **And given** an `initialize`-d default-mode provider (`_init_args` set, `_db_path` still None)
- **When** the same read tools are invoked
- **Then** the call proceeds to `cli_runner` (with `db_path=None`), not the not-initialized branch.

### AC6 — `use_embeddings` flows through provider → tools → hooks → cli_runner

- The provider exposes its config-bool reader.
- `tools._run_read`/`_handle_recall` reads `provider._config_bool("use_embeddings")` and forwards it to `cli_runner.run_recall(..., use_embeddings=...)`.
- `hooks.run_prefetch` accepts `use_embeddings: bool` and forwards it identically.
- `provider.prefetch` reads `_config_bool("use_embeddings")` and passes it into `hooks.run_prefetch`.
- All four flow paths preserve the default `False`.

### AC7 — `db_path: Path | None` typing across hooks helpers

- `hooks.run_prefetch`, `hooks.worker_loop`, `hooks.ensure_worker`, `hooks._spawn_worker`, `hooks.WriteTask` consumers, and provider write paths accept `Path | None`.
- mypy `--strict` (full scope: `hermes_icm_memory tests`) reports zero errors.
- Note: writes in default-shared mode are out of scope for v0.1.1 (the worker still requires a concrete `db_path`); the typing relaxation is for read-path symmetry. `provider._ensure_worker` continues to skip worker spawn when `_db_path is None`.

### AC8 — Test alignment for behavior flip

- `tests/conftest.py` ships an `isolated_provider` fixture (calls `save_config({"isolated": True})` then `initialize`, returns the provider with concrete `_db_path`).
- Tests that depend on a concrete `_db_path` (write-path coverage in `test_hooks.py` / `test_tools.py`) flip to either the new `isolated_provider` fixture or set `_config["isolated"] = True` before `initialize`.
- `test_profile_isolation.py` enables `isolated=True` for every test — profile isolation is the explicit opt-in being tested.
- `test_provider.py::test_initialize_resolves_db_path` is split into `test_initialize_default_shared_db_path_stays_none` and `test_initialize_isolated_resolves_db_path`. `test_initialize_creates_parent_dir` and `test_initialize_with_unwritable_hermes_home_self_disables` only fire under `isolated=True`.

### AC9 — `test_default_schema_has_twelve_keys`

- `tests/test_config.py::test_default_schema_has_ten_keys` is renamed to `test_default_schema_has_twelve_keys` (or made count-agnostic) with the new key set documented.
- New validation tests exercise both bool keys (`isolated`, `use_embeddings`) — coercion from `"true"/"false"`, rejection of arbitrary strings, rejection of non-bool/non-string values.

### AC10 — Version bump + CHANGELOG + README

- `hermes_icm_memory/_version.py` → `__version__ = "0.1.1"`.
- `pyproject.toml` `version = "0.1.1"`.
- `plugin.yaml` `version: 0.1.1`.
- `CHANGELOG.md` (new file) documents v0.1.1 changes:
  - **Behavior change (default flip):** `_db_path` defaults to None (shared-with-editors); enable `isolated=true` to restore the v0.1.0 silo behavior.
  - **Behavior change:** `icm recall` defaults to keyword-only; enable `use_embeddings=true` to restore semantic search.
  - **Migration note:** users who relied on the v0.1.0 default (parallel silo per profile) must explicitly set `isolated=true`.
- `README.md` § Configuration mentions the two new keys + the trade-off (isolation vs. shared, instant keyword-only vs. semantic-with-cold-start).

### AC11 — Quality gates pass on full scope

- `pytest --cov=hermes_icm_memory --cov-branch --cov-fail-under=85` → green (target: ≥ 96% on touched modules, matching the v0.1.0 floor).
- `ruff check .` → zero findings.
- `mypy --strict hermes_icm_memory tests` → zero findings.

### AC12 — Pi smoke test passes end-to-end

After `git checkout v0.1.1` is applied to the deployed plugin dir and `hermes-gateway.service` restarted, the smoke probe imports `IcmMemoryProvider`, calls `initialize` with default config, and asserts:

- `provider._db_path is None` (default shared-mode).
- `provider.is_available() is True`.
- `provider.handle_tool_call("icm_recall", {"query": "<known-canonical-token>"})` returns `len(hits) > 0` (proves recall hits the canonical DB).

## Implementation Tasks (ordered)

1. **Tests-first (RED).** Update `tests/test_config.py` with the twelve-key expectation + two new bool validation tests; rewrite `tests/test_provider.py::test_initialize_*` per AC3+AC8; update `tests/test_profile_isolation.py` to enable `isolated=True`; add `isolated_provider` fixture in `conftest.py`; extend `tests/test_cli_runner.py` with `--no-embeddings` argv shape (default + opt-out).
2. **GREEN — config.** Append `isolated` + `use_embeddings` entries to `_SCHEMA_ENTRIES`.
3. **GREEN — cli_runner.** Add `use_embeddings: bool = False` kwarg to `run_recall`; conditionally append `--no-embeddings`.
4. **GREEN — provider.** Branch `initialize` on `_config_bool("isolated")`; remove `or self._db_path is None` from `prefetch`; thread `use_embeddings` into `hooks.run_prefetch`.
5. **GREEN — hooks.** Relax `db_path: Path | None`; thread `use_embeddings` into `cli_runner.run_recall`. Provider `_ensure_worker` keeps the `_db_path is None` short-circuit (writes don't run in shared-mode for v0.1.1 — documented limitation).
6. **GREEN — tools.** Replace `if db_path is None` guard with `if provider._init_args is None`; thread `use_embeddings` into `cli_runner.run_recall`.
7. **Bump version + write CHANGELOG + README update.**
8. **Verify.** Full gates (pytest cov, ruff, mypy --strict on full scope).
9. **Pi smoke test.** Reinstall worktree branch into `~/.hermes/plugins/hermes-icm-memory/`, restart gateway, run probe.

## Out of scope

- v0.2 will introduce `icm-serve` MCP transport that amortizes the embedding-model load — making `use_embeddings=True` viable on the hot path. This story stops at the config flag.
- Default-mode writes (sync_turn → worker) remain isolation-aware — running writes against the canonical shared DB is a v0.2 concern (shared-DB write semantics + concurrent-writer safety on SQLite need a separate review).

## Risks / breaking changes

- **Breaking:** users who relied on the v0.1.0 default (parallel silo) get the shared canonical DB. The CHANGELOG migration note + the new `isolated` key restore the old behavior in one config edit.
- **Worker behavior:** in default-mode (`_db_path is None`), `_ensure_worker` returns False → writes silently no-op. This matches the brief's prefetch-first intent and is documented in CHANGELOG. Operators who want writes today must set `isolated=true`.

## Definition of Done

- All 12 ACs pass.
- 4-phase chain executed end-to-end on a single commit train (`docs(v0.1.1)/feat(v0.1.1)/review(v0.1.1)/simplify(v0.1.1)`).
- Pi smoke test green.
- Status report sent to team-lead.
