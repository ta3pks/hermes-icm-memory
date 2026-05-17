---
stepsCompleted:
  - step-01-clarify
  - step-02-spec
inputDocuments:
  - _bmad-output/planning-artifacts/architecture.md
  - _bmad-output/planning-artifacts/epics-and-stories.md
  - _bmad-output/implementation-artifacts/v0-3-hermes-native-mcp-and-lifecycle-only-plugin.md
project_name: hermes-icm-memory
workflowType: 'spec'
date: '2026-05-15'
version: 0.4.0
---

# Spec v0.4 — MCP-native transports + fixed trigger detection

## Overview

Replace the plugin's internal `subprocess`-based ICM operations (`cli_runner.py`) with MCP client calls to the already-running `icm serve` daemon managed by Hermes' `mcp_servers.icm` config. Then fix the trigger detection and prefetch quality on top of that foundation.

### Why MCP instead of CLI subprocess

| Concern | CLI subprocess (current) | MCP (proposed) |
|---------|--------------------------|----------------|
| Model load | **Every** call cold-loads ONNX (50s+ on Pi) | Loaded once in daemon; all calls < 200ms |
| Semantic recall | Not available with `--no-embeddings` | Full vector search even with `--no-embeddings` (icm serve keeps model warm) |
| Empty query recall | Returns `[]` (no keywords to match) | Returns semantically relevant memories with empty query |
| Process overhead | One `fork+exec` per call (2-5ms + memory) | Zero overhead (single HTTP/MCP call) |
| DB connections | New SQLite connection per call | Single warm connection in daemon |
| Failure surface | subprocess.TimeoutExpired, FileNotFoundError, non-zero exits, malformed JSON | Single HTTP connection timeout |

### The three fixes

1. **MCP transport layer** — new `mcp_client.py` module that speaks MCP JSON-RPC to `icm serve` over HTTP StreamableHTTP. Replaces `cli_runner.py` for all ICM operations.

2. **Fixed prefetch** — `prefetch()` uses MCP-based `icm_memory_recall` with the user's actual message as query (instead of empty string). Semantic search returns relevant memories even with `--no-embeddings` because the daemon keeps embeddings warm.

3. **Better trigger detection** — expanded `mapping.py` with broader regex patterns + LLM-assisted detection (the agent itself can flag important content via MCP `icm_memory_store`).

## Requirements

### Functional
- FR-MCP1: Plugin connects to `icm serve` via MCP HTTP StreamableTransport at `http://pi.hole:4178/mcp`
- FR-MCP2: `prefetch(query)` calls `icm_memory_recall` via MCP with the actual `query` string from Hermes (not `""`), including optional topic filter
- FR-MCP3: Worker thread stores via MCP `icm_memory_store` instead of CLI `icm store`
- FR-MCP4: Plugin falls back to empty results silently if MCP connection fails (degrade-to-empty, not degrade-to-CLI)
- FR-MCP5: Trigger detection patterns expanded to catch more natural language patterns
- FR-MCP6: Plugin exposes `shutdown()` that closes the MCP session gracefully

### Non-functional
- NFR-MCP1: MCP connection established lazily on first use, not in `initialize()`
- NFR-MCP2: Each MCP call has a configurable timeout (default 5000ms)
- NFR-MCP3: `sync_turn` must still return within 5ms p95 (MCP write is queued just like CLI write was)
- NFR-MCP4: Plugin never blocks a Hermes turn on MCP connection issues

## Technical Approach

### Module changes

| Module | Change |
|--------|--------|
| `mcp_client.py` | **NEW** — MCP client module. Connects to `icm serve` via HTTP StreamableTransport. Exports: `call_tool(name, args)`, `list_tools()`, `close()`. Uses `httpx` (or stdlib `urllib` to avoid dep). |
| `cli_runner.py` | **REMOVED** — replaced entirely by `mcp_client.py`. All subprocess-based ICM operations replaced. |
| `provider.py` | Initialize MCP client lazily in `prefetch()` and store worker. Add `connection_ref` to provider state. |
| `hooks.py` | `prefetch()` calls `mcp_client.call_tool("icm_memory_recall", ...)` instead of `cli_runner.run_recall(...)`. Worker calls `mcp_client.call_tool("icm_memory_store", ...)`. |
| `mapping.py` | **Expanded** — more detection patterns, broader matching, LLM-friendly content extraction. |
| `config.py` | Add `mcp_url` (default `http://pi.hole:4178/mcp`), `mcp_timeout_ms`. Remove CLI-specific configs (command_timeout_read_ms, command_timeout_write_ms replaced by mcp_timeout_ms). |
| `errors.py` | Add `MCPConnectionError`, `MCPToolCallError`. Remove CLI-specific error types (ICMNotFoundError, ICMTimeoutError, ICMMalformedOutputError, ICMNonZeroExitError) or keep as aliases. |
| `tests/` | Replace cli_runner tests with mcp_client tests (mock `httpx` or custom HTTP handler). Update all tests. |

### MCP Connection flow

```
provider.prefetch(query, **kwargs)
  ├─ if not self._mcp_client:
  │     self._mcp_client = McpClient(mcp_url, timeout)
  │     self._mcp_client.initialize()  # /mcp POST with initialize request
  │
  ├─ result = self._mcp_client.call_tool("icm_memory_recall", {
  │     "query": query or "recent",    # never pass empty string
  │     "limit": config.recall_limit,
  │     "topic": config.default_recall_topic,
  │   })
  │
  ├─ cache result
  └─ return formatted string
```

### MCP over HTTP StreamableTransport

The `icm serve --compact --no-embeddings` daemon (already running at `mcp_servers.icm` in Hermes config) exposes a StreamableHTTP MCP endpoint. The client sends JSON-RPC messages via HTTP POST to the `/mcp` endpoint.

The MCP protocol requires:
1. POST `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"hermes-icm-memory","version":"0.4.0"}}}` → receives capabilities
2. POST `{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}` → discover tools
3. POST `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"icm_memory_recall","arguments":{...}}}` → call tool
4. POST `{"jsonrpc":"2.0","id":4,"method":"notifications/close","params":{}}` → close

### Expanded trigger detection

Current patterns catch only very specific phrasings. New patterns add:

- **errors-resolved**: `fixed`, `resolved`, `root cause`, `the bug was`, `fix it`, `fixed it`, `found the issue`, `the problem was`, `solved`, `patched`, `hotfix`, `regression`
- **decisions**: `decided to`, `going with`, `we'll use`, `let's use`, `chose to`, `we should use`, `the best approach`, `let's go with`, `opted for`, `we'll go with`, `preferred`, `selected`, `chosen approach`
- **preferences** (user): `always use`, `never use`, `prefer`, `I like`, `I don't like`, `my convention`, `my habit`, `I usually`, `I generally`, `don't ever`, `always do`, `never do`, `important to note`, `remember that I`
- **context** (anything currently matching neither errors/decisions/preferences but non-trivial): broader topic capture, conversation summaries

Plus a new **periodic-synthesis** trigger: every N turns, save a summary of what was discussed using the actual conversation content rather than just "turn N completed".

### MCP URL config

The default MCP URL is `http://pi.hole:4178/mcp`. This is configurable via `save_config({"mcp_url": "..."})`. On systems where `icm serve` is not running as an MCP server, the user would need to configure the correct URL.

Actually — there's a subtlety. The `icm serve` instance managed by Hermes via `mcp_servers.icm` uses **stdio transport**, not HTTP. Hermes talks to it via stdin/stdout pipes. For the plugin to connect to it via HTTP, we'd need `icm serve` to also expose an HTTP endpoint.

Let me check what transport `icm serve` supports by default.

Actually, looking back at my earlier recollection of the system architecture — there's an ICM proxy running:
```
http://pi.hole:4178/sse and http://pi.hole:4178/mcp for ICM proxy URLs
```

Wait, let me check more carefully what's actually running.

Actually, re-reading: "Proxy works — SSE endpoint at http://100.95.64.83:4178/sse, icm serve spawned cleanly."

So there's an `icm serve` running with an SSE/HTTP endpoint. This might be the ICM proxy mentioned in the architecture. Let me verify what's actually running on the system.

Actually, I think I'm overcomplicating this. Let me just check what's running and what MCP transport is available.

But this is a planning/spec document, not implementation. Let me present the spec to the user first and let them guide the details.
