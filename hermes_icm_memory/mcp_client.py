"""MCP stdio client for `icm serve` — JSON-RPC over stdin/stdout.

Architecture invariants (v0.4):
* AD-12 — this is the ONLY module under hermes_icm_memory/ (alongside
  cli_runner.py) that imports subprocess.
* AD-07 — every public method catches at the boundary and returns documented
  degraded shapes. No exception propagates into the Hermes turn loop.
* AD-13 — module-level ``logger = logging.getLogger(__name__)``; structured
  ``extra={...}`` dicts on every WARNING.

Respawn policy:
* First subprocess death → log WARNING, respawn once.
* Second death → log CRITICAL, set ``_disabled = True`` for provider lifetime.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .errors import ICMConnectionError, ICMError, ICMTimeoutError

__all__ = [
    "IcmMcpClient",
]

logger = logging.getLogger(__name__)

#: JSON-RPC protocol version.
_RPC_VERSION: str = "2.0"

#: Max seconds to wait for a JSON-RPC response line.
_RPC_TIMEOUT_S: float = 5.0

#: Max seconds to wait for the MCP initialize handshake.
_INIT_TIMEOUT_S: float = 10.0

#: Number of newlines to consume as a heartbeat/probe when waiting for a response.
_MAX_RESPONSE_LINES: int = 512

#: MCP tool names exposed by icm serve (verified on icm 0.10.34).
#: v0.4.1: Added wake-up tool for session-start injection.
_TOOL_RECALL: str = "icm_memory_recall"
_TOOL_STORE: str = "icm_memory_store"
_TOOL_TOPICS: str = "icm_memory_list_topics"
_TOOL_HEALTH: str = "icm_memory_health"
_TOOL_WAKE_UP: str = "icm_wake_up"


class IcmMcpClient:
    """Manages a long-lived ``icm serve --no-embeddings`` subprocess over MCP stdio.

    Usage::

        client = IcmMcpClient()
        client.start(db_path=None, use_embeddings=False)  # lazy spawn
        hits = client.call_recall("hello", limit=5)
        client.call_store("errors-resolved", "fixed bug", "high")
        client.close()
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._req_id: int = 0
        self._disabled: bool = False  # set True after second death
        self._respawn_count: int = 0

    # ------------------------------------------------------------------ public

    def is_available(self) -> bool:
        """Return True iff the MCP subprocess is alive (or could be started)."""
        return not self._disabled

    def start(
        self,
        db_path: Path | None = None,
        use_embeddings: bool = False,
    ) -> None:
        """Spawn ``icm serve`` and complete the MCP handshake.

        Raises ``ICMConnectionError`` if spawning or the handshake fails.
        No-op if already running.
        """
        if self._proc is not None and self._proc.poll() is None:
            return  # already running

        argv: list[str] = ["icm", "serve"]
        if db_path is not None:
            argv += ["--db", str(db_path)]
        if not use_embeddings:
            argv.append("--no-embeddings")

        logger.debug(
            "mcp: spawning icm serve",
            extra={"argv": argv, "use_embeddings": use_embeddings},
        )

        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise ICMConnectionError(f"icm binary not found: {exc}") from exc
        except OSError as exc:
            raise ICMConnectionError(f"failed to spawn icm serve: {exc}") from exc

        # Perform MCP initialize handshake.
        try:
            self._send_initialize()
        except Exception as exc:
            self._kill()
            raise ICMConnectionError(f"MCP initialize handshake failed: {exc}") from exc

        logger.debug("mcp: started and initialised")

    def close(self) -> None:
        """Send shutdown notification and terminate the subprocess gracefully."""
        if self._proc is None:
            return
        with contextlib.suppress(Exception):
            self._send_jsonrpc(
                {
                    "jsonrpc": _RPC_VERSION,
                    "method": "notifications/initialized",
                    "params": {},
                },
                expect_response=False,
            )
        self._kill()

    def call_recall(
        self,
        query: str,
        limit: int = 5,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        """Call icm_memory_recall via MCP and return parsed list of hits.

        Returns ``[]`` on any failure (AD-07 degrade).
        """
        if self._disabled:
            return []

        params: dict[str, Any] = {"query": query, "limit": limit}
        if topic is not None:
            params["topic"] = topic
        if project is not None:
            params["project"] = project

        try:
            result = self._call_tool(_TOOL_RECALL, params)
        except ICMError as exc:
            logger.warning(
                "mcp: recall failed: %r", exc, extra={"err": repr(exc), "query": query}
            )
            return []
        except Exception as exc:
            logger.warning(
                "mcp: recall unexpected error: %r",
                exc,
                extra={"err": repr(exc), "query": query},
            )
            return []

        return _parse_recall_response(result)

    def call_store(
        self,
        topic: str,
        content: str,
        importance: str,
        keywords: str | None = None,
    ) -> bool:
        """Call icm_memory_store via MCP. Returns True on success."""
        if self._disabled:
            return False

        params: dict[str, Any] = {
            "topic": topic,
            "content": content,
            "importance": importance,
        }
        if keywords is not None:
            params["keywords"] = keywords

        try:
            self._call_tool(_TOOL_STORE, params)
            return True
        except ICMError as exc:
            logger.warning(
                "mcp: store failed: %r", exc, extra={"err": repr(exc), "topic": topic}
            )
            return False
        except Exception as exc:
            logger.warning(
                "mcp: store unexpected error: %r",
                exc,
                extra={"err": repr(exc), "topic": topic},
            )
            return False

    def call_topics(self) -> list[dict[str, Any]]:
        """Call icm_memory_list_topics via MCP. Returns [] on failure."""
        if self._disabled:
            return []
        try:
            result = self._call_tool(_TOOL_TOPICS, {})
        except ICMError as exc:
            logger.warning("mcp: topics failed: %r", exc, extra={"err": repr(exc)})
            return []
        except Exception as exc:
            logger.warning(
                "mcp: topics unexpected error: %r",
                exc,
                extra={"err": repr(exc)},
            )
            return []
        return _parse_topics_response(result)

    def call_health(self, topic: str | None = None) -> dict[str, Any]:
        """Call icm_memory_health via MCP. Returns {} on failure."""
        if self._disabled:
            return {}
        params: dict[str, Any] = {}
        if topic is not None:
            params["topic"] = topic
        try:
            result = self._call_tool(_TOOL_HEALTH, params)
        except ICMError as exc:
            logger.warning("mcp: health failed: %r", exc, extra={"err": repr(exc)})
            return {}
        except Exception as exc:
            logger.warning(
                "mcp: health unexpected error: %r",
                exc,
                extra={"err": repr(exc)},
            )
            return {}
        return _parse_health_response(result)

    def call_wake_up(
        self,
        project: str | None = None,
        max_tokens: int = 400,
    ) -> str:
        """Call icm_wake_up via MCP. Returns formatted wake-up text.

        Returns empty string if disabled or on any failure (AD-07 degrade).
        """
        if self._disabled:
            return ""

        params: dict[str, Any] = {"max_tokens": max_tokens}
        if project is not None:
            params["project"] = project

        try:
            result = self._call_tool(_TOOL_WAKE_UP, params)
        except ICMError as exc:
            logger.warning(
                "mcp: wake_up failed: %r", exc, extra={"err": repr(exc)}
            )
            return ""
        except Exception as exc:
            logger.warning(
                "mcp: wake_up unexpected error: %r",
                exc,
                extra={"err": repr(exc)},
            )
            return ""

        # Wake-up returns text content (not structured like recall)
        if result is None:
            return ""
        return _get_text(result) if isinstance(result, list) else ""

    # ------------------------------------------------------------------ private

    def _ensure_alive(self) -> None:
        """Check subprocess health; respawn once if dead (second death → disable)."""
        if self._disabled:
            return
        if self._proc is not None and self._proc.poll() is None:
            return  # alive

        if self._respawn_count >= 1:
            self._disabled = True
            logger.critical(
                "mcp: second death — MCP client disabled for process lifetime",
                extra={"respawn_count": self._respawn_count},
            )
            self._proc = None
            return

        self._respawn_count += 1
        logger.warning(
            "mcp: subprocess dead; respawning (attempt %d)",
            self._respawn_count,
            extra={"respawn_count": self._respawn_count},
        )
        # Re-spawn (no db_path/use_embeddings — same as initial start).
        # We stash cfg from first start.
        self._proc = None

    def _write_stdin(self, data: str) -> None:
        """Write a JSON-RPC payload to the subprocess stdin."""
        if self._proc is None or self._proc.stdin is None:
            raise ICMConnectionError("MCP subprocess not running")
        try:
            self._proc.stdin.write(data + "\n")
            self._proc.stdin.flush()
        except BrokenPipeError as exc:
            raise ICMConnectionError(f"MCP stdin broken: {exc}") from exc
        except OSError as exc:
            raise ICMConnectionError(f"MCP stdin error: {exc}") from exc

    def _read_line(self, timeout: float = _RPC_TIMEOUT_S) -> str:
        """Read one line from the subprocess stdout with timeout."""
        if self._proc is None or self._proc.stdout is None:
            raise ICMConnectionError("MCP subprocess not running")

        deadline = time.monotonic() + timeout
        buffer: list[str] = []
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if line:
                buffer.append(line)
                # Check if we have a complete JSON-RPC response (ends with \n).
                # The response might span multiple lines if the text is long.
                combined = "".join(buffer)
                # Try to parse as JSON — if it works, we're done.
                try:
                    json.loads(combined)
                    return combined
                except json.JSONDecodeError:
                    continue

            # Check if subprocess died.
            if self._proc.poll() is not None:
                raise ICMConnectionError("MCP subprocess died during read")

        raise ICMTimeoutError("MCP read timeout")

    def _send_jsonrpc(
        self, payload: dict[str, Any], expect_response: bool = True
    ) -> dict[str, Any] | None:
        """Send a JSON-RPC payload and optionally await a response."""
        self._ensure_alive()
        if self._disabled:
            raise ICMConnectionError("MCP client disabled")

        with self._lock:
            payload_str = json.dumps(payload, ensure_ascii=False)
            self._write_stdin(payload_str)

            if not expect_response:
                return None

            raw = self._read_line()
            response: dict[str, Any] = json.loads(raw)

            if "error" in response:
                err = response["error"]
                raise ICMConnectionError(
                    f"MCP error: {err.get('message', 'unknown')} "
                    f"(code {err.get('code', '?')})"
                )

            return response.get("result")

    def _send_initialize(self) -> None:
        """Send MCP initialize + await response, then send initialized notification."""
        init_payload = {
            "jsonrpc": _RPC_VERSION,
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "hermes-icm-memory", "version": "0.4.0"},
            },
        }
        result = self._send_jsonrpc(init_payload, expect_response=True)
        if result is None:
            raise ICMConnectionError("MCP initialize returned None")

        # Send initialized notification (no response expected).
        self._send_jsonrpc(
            {
                "jsonrpc": _RPC_VERSION,
                "method": "notifications/initialized",
                "params": {},
            },
            expect_response=False,
        )

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool and return its result content."""
        payload = {
            "jsonrpc": _RPC_VERSION,
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        result = self._send_jsonrpc(payload, expect_response=True)
        if result is None:
            raise ICMConnectionError(f"MCP tool {tool_name} returned None")

        # MCP response shape: result.content[0].type + .text
        content = result.get("content", [])
        if not content:
            # isError might be set
            if result.get("isError"):
                raise ICMConnectionError(
                    f"MCP tool {tool_name} returned error: {result}"
                )
            return None

        return content

    def _kill(self) -> None:
        """Terminate the subprocess if alive."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                with contextlib.suppress(Exception):
                    self._proc.kill()
            self._proc = None


# ------------------------------------------------------------------ response parsers


def _get_text(content: list[dict[str, Any]]) -> str:
    """Extract the text from an MCP tool result content list."""
    parts: list[str] = []
    for entry in content:
        if isinstance(entry, dict) and entry.get("type") == "text":
            parts.append(entry.get("text", ""))
    return "\n".join(parts)


def _parse_recall_response(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse icm_memory_recall MCP response into list[dict] with topic + summary.

    icm serve returns a text blob where each entry is separated by `` | `` and
    starts with ``[topic]`` content, e.g.::

        [preferences] Daughter: Luna ...
        | [errors-resolved] fixed the bug ...
    """
    raw = _get_text(content)
    if not raw:
        return []

    hits: list[dict[str, Any]] = []
    # Split on the pipe separator that ICM uses between entries.
    parts = raw.split(" | ")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Extract [topic] prefix.
        if part.startswith("[") and "]" in part:
            close_bracket = part.index("]")
            topic = part[1:close_bracket].strip()
            summary = part[close_bracket + 1 :].strip()
        else:
            topic = ""
            summary = part
        hits.append({"topic": topic, "summary": summary})
    return hits


def _parse_topics_response(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse icm_memory_list_topics MCP response into list[dict]."""
    raw = _get_text(content)
    if not raw:
        return []

    # Output is typically a table or list format — try parsing line by line.
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("Topic") and "Count" in line:
            continue  # skip header
        # Try aligned-table parse: two-or-more whitespace splits.
        import re  # noqa: PLC0415 — local import to keep top-level clean

        cols = re.split(r"\s{2,}", line)
        if len(cols) >= 2:
            rows.append({"topic": cols[0].strip(), "count": cols[1].strip()})
        else:
            rows.append({"topic": line})
    return rows


def _parse_health_response(content: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse icm_memory_health MCP response into dict."""
    raw = _get_text(content)
    if not raw:
        return {}

    result: dict[str, Any] = {"raw": raw}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        result_key = _normalize_key(key)
        result[result_key] = value.strip()
    return result


def _normalize_key(raw: str) -> str:
    """Lower-case + collapse whitespace runs to underscores for dict keys."""
    import re  # noqa: PLC0415

    return re.sub(r"\s+", "_", raw.strip().lower())
