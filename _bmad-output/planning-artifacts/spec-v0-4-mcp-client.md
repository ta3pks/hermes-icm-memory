# Spec - v0.4 MCP client transport for hermes-icm-memory

## Overview
Replace CLI subprocess calls in `cli_runner.py` with a warm MCP daemon via `icm serve --no-embeddings` over stdio. Fix prefetch to pass actual user queries. Expand trigger detection patterns.

## Why not reuse v0.2's `transport: mcp`?
v0.2 had MCP transport inside `cli_runner.py` with a config toggle. v0.3 removed it because hermes-native `mcp_servers.icm:` already handles LLM-facing MCP tools. This is different — we're replacing the *plugin-internal* recall/store path (prefetch, worker writes) with MCP instead of subprocess-per-call. The plugin's internal calls get a warm daemon, no subprocess overhead.

## Architecture Decision
- **New module:** `mcp_client.py` — owns the `icm serve` subprocess lifecycle and JSON-RPC communication
- **No config toggle** (unlike v0.2's `transport: cli/mcp`) — always MCP if icm is available, degrade to empty results if not
- **`cli_runner.py` shrinks** — loses `_run`, `run_recall`, `run_store`, `run_topics`, `run_health`. Those become MCP calls
- **AD-12 preserved** — only `mcp_client.py` imports `subprocess` (AST test updated)
- **`errors.py`** — new `ICMConnectionError` for MCP transport failures (timeout, subprocess death, JSON-RPC errors)

## Implementation Plan
1. Create `mcp_client.py` — `IcmMcpClient` class with `start()`, `call_recall()`, `call_store()`, `call_topics()`, `call_health()`, `close()`
2. Refactor `cli_runner.py` — remove subprocess code, delegate to `mcp_client.py`
3. Update `hooks.py` — `run_prefetch` and `worker_loop` use new MCP calls
4. Fix `provider.py` — `prefetch()` passes actual query from kwargs, `initialize()` starts MCP client
5. Expand `mapping.py` patterns
6. Update tests

## Risks
- `icm serve` subprocess death mid-session → respawn once, then degrade
- JSON-RPC parsing from `icm 0.10.34` — we tested and it works
- Response shapes from MCP tools are text blobs, not JSON — need parsing adapters
