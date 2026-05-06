# Changelog

All notable changes to this project are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

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
