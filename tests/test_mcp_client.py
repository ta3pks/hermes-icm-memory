"""Unit tests for ``hermes_icm_memory.mcp_client``.

Parsing helpers + IcmMcpClient lifecycle via mocked _send_jsonrpc / subprocess."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_icm_memory.errors import ICMConnectionError, ICMTimeoutError
from hermes_icm_memory.mcp_client import (
    IcmMcpClient,
    _get_text,
    _normalize_key,
    _parse_health_response,
    _parse_recall_response,
    _parse_topics_response,
)

# ---------------------------------------------------------------------------
# _get_text — extract text from MCP content list
# ---------------------------------------------------------------------------


def test_get_text_single_entry() -> None:
    content = [{"type": "text", "text": "hello world"}]
    assert _get_text(content) == "hello world"


def test_get_text_multiple_entries() -> None:
    content = [
        {"type": "text", "text": "line1"},
        {"type": "text", "text": "line2"},
    ]
    assert _get_text(content) == "line1\nline2"


def test_get_text_skips_non_text() -> None:
    content = [{"type": "resource", "text": "skip"}, {"type": "text", "text": "keep"}]
    assert _get_text(content) == "keep"


def test_get_text_empty() -> None:
    assert _get_text([]) == ""


# ---------------------------------------------------------------------------
# _parse_recall_response
# ---------------------------------------------------------------------------


def test_parse_recall_single_hit() -> None:
    content = [{"type": "text", "text": "[preferences] Hello world"}]
    result = _parse_recall_response(content)
    assert result == [{"topic": "preferences", "summary": "Hello world"}]


def test_parse_recall_multiple_hits() -> None:
    content = [
        {
            "type": "text",
            "text": "[preferences] Luna is daughter | [errors-resolved] fixed the bug",
        }
    ]
    result = _parse_recall_response(content)
    assert result == [
        {"topic": "preferences", "summary": "Luna is daughter"},
        {"topic": "errors-resolved", "summary": "fixed the bug"},
    ]


def test_parse_recall_no_topic_prefix() -> None:
    content = [{"type": "text", "text": "just a note"}]
    result = _parse_recall_response(content)
    assert result == [{"topic": "", "summary": "just a note"}]


def test_parse_recall_empty() -> None:
    assert _parse_recall_response([]) == []
    assert _parse_recall_response([{"type": "text", "text": ""}]) == []


def test_parse_recall_skips_empty_parts() -> None:
    """Trailing `` | `` separator produces an empty part that is skipped."""
    content = [{"type": "text", "text": "[topic] A | [topic] B | "}]
    result = _parse_recall_response(content)
    assert result == [
        {"topic": "topic", "summary": "A"},
        {"topic": "topic", "summary": "B"},
    ]


# ---------------------------------------------------------------------------
# _parse_topics_response
# ---------------------------------------------------------------------------


def test_parse_topics_table() -> None:
    content = [
        {
            "type": "text",
            "text": "Topic            Count\nerrors-resolved  3\ndecisions-x      7\n",
        }
    ]
    result = _parse_topics_response(content)
    assert {"topic": "errors-resolved", "count": "3"} in result
    assert {"topic": "decisions-x", "count": "7"} in result


def test_parse_topics_empty() -> None:
    assert _parse_topics_response([]) == []
    assert _parse_topics_response([{"type": "text", "text": ""}]) == []


def test_parse_topics_single_column_line() -> None:
    """A line that doesn't split into 2+ cols returns the bare line as topic."""
    content = [{"type": "text", "text": "Topic  Count\nbare-line\nanother  3\n"}]
    result = _parse_topics_response(content)
    assert {"topic": "bare-line"} in result
    assert {"topic": "another", "count": "3"} in result


# ---------------------------------------------------------------------------
# _parse_health_response
# ---------------------------------------------------------------------------


def test_parse_health_key_value() -> None:
    content = [
        {
            "type": "text",
            "text": "Total memories: 42\nStale: 0\nLast consolidation: 2026-05-05\n",
        }
    ]
    result = _parse_health_response(content)
    assert result["total_memories"] == "42"
    assert result["stale"] == "0"
    assert result["last_consolidation"] == "2026-05-05"


def test_parse_health_empty() -> None:
    assert _parse_health_response([]) == {}


def test_parse_health_skips_lines_without_colon() -> None:
    """Lines without ``:`` are skipped but not lost — raw captures everything."""
    content = [{"type": "text", "text": "Total memories: 42\n---separator---\nStale: 0\n"}]
    result = _parse_health_response(content)
    assert result["total_memories"] == "42"
    assert result["stale"] == "0"
    # The separator line is only in the raw key
    assert "---separator---" in result["raw"]


# ---------------------------------------------------------------------------
# _normalize_key
# ---------------------------------------------------------------------------


def test_normalize_key() -> None:
    assert _normalize_key("Total memories") == "total_memories"
    assert _normalize_key("  key  ") == "key"
    assert _normalize_key("") == ""


# ---------------------------------------------------------------------------
# IcmMcpClient — lifecycle and state
# ---------------------------------------------------------------------------


class TestIcmMcpClientInit:
    """IcmMcpClient.__init__"""

    def test_init_defaults(self) -> None:
        client = IcmMcpClient()
        assert client._proc is None
        assert client._disabled is False
        assert client._respawn_count == 0
        assert client._req_id == 0
        assert client._lock is not None


class TestIcmMcpClientIsAvailable:
    """IcmMcpClient.is_available"""

    def test_available_when_not_disabled(self) -> None:
        client = IcmMcpClient()
        assert client.is_available() is True

    def test_unavailable_when_disabled(self) -> None:
        client = IcmMcpClient()
        client._disabled = True
        assert client.is_available() is False


class TestIcmMcpClientNextId:
    """IcmMcpClient._next_id"""

    def test_next_id_increments(self) -> None:
        client = IcmMcpClient()
        assert client._next_id() == 1
        assert client._next_id() == 2
        assert client._next_id() == 3
        assert client._req_id == 3


# ---------------------------------------------------------------------------
# IcmMcpClient — _kill
# ---------------------------------------------------------------------------


class TestIcmMcpClientKill:
    """IcmMcpClient._kill"""

    def test_kill_no_proc(self) -> None:
        client = IcmMcpClient()
        client._kill()  # should not raise
        assert client._proc is None

    def test_kill_terminate_success(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        client._proc = mock_proc
        client._kill()
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=3)
        assert client._proc is None

    def test_kill_terminate_raises_falls_back_to_kill(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.terminate.side_effect = OSError("terminate failed")
        client._proc = mock_proc
        client._kill()
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        assert client._proc is None

    def test_kill_wait_timeout_falls_back_to_kill(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.wait.side_effect = TimeoutError("timed out")
        client._proc = mock_proc
        client._kill()
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        assert client._proc is None


# ---------------------------------------------------------------------------
# IcmMcpClient — _write_stdin
# ---------------------------------------------------------------------------


class TestIcmMcpClientWriteStdin:
    """IcmMcpClient._write_stdin"""

    def test_write_stdin_no_proc(self) -> None:
        client = IcmMcpClient()
        client._proc = None
        with pytest.raises(ICMConnectionError, match="not running"):
            client._write_stdin("data")

    def test_write_stdin_stdin_is_none(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.stdin = None
        client._proc = mock_proc
        with pytest.raises(ICMConnectionError, match="not running"):
            client._write_stdin("data")

    def test_write_stdin_success(self) -> None:
        client = IcmMcpClient()
        mock_stdin = Mock()
        mock_proc = Mock()
        mock_proc.stdin = mock_stdin
        client._proc = mock_proc
        client._write_stdin('{"test": "data"}')
        mock_stdin.write.assert_called_once_with('{"test": "data"}\n')
        mock_stdin.flush.assert_called_once()

    def test_write_stdin_broken_pipe(self) -> None:
        client = IcmMcpClient()
        mock_stdin = Mock()
        mock_stdin.write.side_effect = BrokenPipeError("pipe broken")
        mock_proc = Mock()
        mock_proc.stdin = mock_stdin
        client._proc = mock_proc
        with pytest.raises(ICMConnectionError, match="stdin broken"):
            client._write_stdin("data")

    def test_write_stdin_os_error(self) -> None:
        client = IcmMcpClient()
        mock_stdin = Mock()
        mock_stdin.write.side_effect = OSError("OS error")
        mock_proc = Mock()
        mock_proc.stdin = mock_stdin
        client._proc = mock_proc
        with pytest.raises(ICMConnectionError, match="stdin error"):
            client._write_stdin("data")


# ---------------------------------------------------------------------------
# IcmMcpClient — _read_line
# ---------------------------------------------------------------------------


class TestIcmMcpClientReadLine:
    """IcmMcpClient._read_line"""

    def test_read_line_no_proc(self) -> None:
        client = IcmMcpClient()
        client._proc = None
        with pytest.raises(ICMConnectionError, match="not running"):
            client._read_line(timeout=0.01)

    def test_read_line_stdout_is_none(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.stdout = None
        client._proc = mock_proc
        with pytest.raises(ICMConnectionError, match="not running"):
            client._read_line(timeout=0.01)

    def test_read_line_success(self) -> None:
        client = IcmMcpClient()
        mock_stdout = Mock()
        response = '{"jsonrpc":"2.0","result":{"ok":true}}\n'
        mock_stdout.readline.side_effect = [response]
        mock_proc = Mock()
        mock_proc.stdout = mock_stdout
        mock_proc.poll.return_value = None
        client._proc = mock_proc
        result = client._read_line(timeout=1.0)
        assert json.loads(result)["result"]["ok"] is True

    def test_read_line_multiline_json(self) -> None:
        """Response spans two readline calls (incomplete JSON on first)."""
        client = IcmMcpClient()
        mock_stdout = Mock()
        mock_stdout.readline.side_effect = [
            '{"jsonrpc":"2.0","result":{\n',
            '"ok":true}}\n',
        ]
        mock_proc = Mock()
        mock_proc.stdout = mock_stdout
        mock_proc.poll.return_value = None
        client._proc = mock_proc
        result = client._read_line(timeout=1.0)
        parsed = json.loads(result)
        assert parsed["result"]["ok"] is True

    def test_read_line_subprocess_dies_during_read(self) -> None:
        client = IcmMcpClient()
        mock_stdout = Mock()
        mock_stdout.readline.return_value = ""  # EOF
        mock_proc = Mock()
        mock_proc.stdout = mock_stdout
        mock_proc.poll.return_value = 0  # dead
        client._proc = mock_proc
        with pytest.raises(ICMConnectionError, match="died"):
            client._read_line(timeout=1.0)

    def test_read_line_timeout(self) -> None:
        client = IcmMcpClient()
        mock_stdout = Mock()
        mock_stdout.readline.return_value = ""  # no data
        mock_proc = Mock()
        mock_proc.stdout = mock_stdout
        mock_proc.poll.return_value = None  # alive, just slow
        client._proc = mock_proc
        with pytest.raises(ICMTimeoutError, match="timeout"):
            client._read_line(timeout=0.01)


# ---------------------------------------------------------------------------
# IcmMcpClient — _ensure_alive
# ---------------------------------------------------------------------------


class TestIcmMcpClientEnsureAlive:
    """IcmMcpClient._ensure_alive"""

    def test_alive_when_disabled(self) -> None:
        client = IcmMcpClient()
        client._disabled = True
        client._ensure_alive()
        assert client._disabled is True
        assert client._respawn_count == 0

    def test_alive_proc_running(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.poll.return_value = None  # still running
        client._proc = mock_proc
        client._ensure_alive()
        assert client._disabled is False
        assert client._respawn_count == 0

    def test_alive_proc_none_triggers_respawn(self) -> None:
        client = IcmMcpClient()
        client._proc = None  # never started
        client._respawn_count = 0
        client._ensure_alive()
        assert client._respawn_count == 1
        assert client._proc is None
        assert client._disabled is False

    def test_alive_dead_second_death_disables(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.poll.return_value = 0  # dead
        client._proc = mock_proc
        client._respawn_count = 1  # already respawned once
        client._ensure_alive()
        assert client._disabled is True
        assert client._proc is None

    def test_alive_dead_first_death_respawns(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.poll.return_value = 0  # dead
        client._proc = mock_proc
        client._respawn_count = 0
        client._ensure_alive()
        assert client._disabled is False
        assert client._respawn_count == 1
        assert client._proc is None


# ---------------------------------------------------------------------------
# IcmMcpClient — _send_jsonrpc
# ---------------------------------------------------------------------------


class TestIcmMcpClientSendJsonRpc:
    """IcmMcpClient._send_jsonrpc"""

    def test_send_jsonrpc_disabled_after_ensure_alive(self) -> None:
        """When _ensure_alive sets _disabled, _send_jsonrpc raises."""
        client = IcmMcpClient()
        client._disabled = False
        client._respawn_count = 1
        mock_proc = Mock()
        mock_proc.poll.return_value = 0  # dead, and already respawned → disabled
        client._proc = mock_proc
        with pytest.raises(ICMConnectionError, match="disabled"):
            client._send_jsonrpc({"method": "test"}, expect_response=True)

    def test_send_jsonrpc_expect_false_returns_none(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.poll.return_value = None  # alive
        mock_stdin = Mock()
        mock_proc.stdin = mock_stdin
        client._proc = mock_proc
        result = client._send_jsonrpc(
            {"jsonrpc": "2.0", "method": "test", "params": {}},
            expect_response=False,
        )
        assert result is None
        mock_stdin.write.assert_called_once()

    def test_send_jsonrpc_with_response(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_stdin = Mock()
        mock_proc.stdin = mock_stdin
        mock_stdout = Mock()
        response = '{"jsonrpc":"2.0","result":{"answer":42}}\n'
        mock_stdout.readline.return_value = response
        mock_proc.stdout = mock_stdout
        client._proc = mock_proc
        result = client._send_jsonrpc(
            {"jsonrpc": "2.0", "method": "test", "params": {}},
            expect_response=True,
        )
        assert result == {"answer": 42}
        mock_stdin.write.assert_called_once()

    def test_send_jsonrpc_error_in_response(self) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.poll.return_value = None
        mock_stdin = Mock()
        mock_proc.stdin = mock_stdin
        mock_stdout = Mock()
        error_response = (
            '{"jsonrpc":"2.0","error":{"code":-32601,"message":"Method not found"}}\n'
        )
        mock_stdout.readline.return_value = error_response
        mock_proc.stdout = mock_stdout
        client._proc = mock_proc
        with pytest.raises(ICMConnectionError, match="MCP error"):
            client._send_jsonrpc(
                {"jsonrpc": "2.0", "method": "test", "params": {}},
                expect_response=True,
            )
        mock_stdin.write.assert_called_once()


# ---------------------------------------------------------------------------
# IcmMcpClient — _call_tool
# ---------------------------------------------------------------------------


class TestIcmMcpClientCallTool:
    """IcmMcpClient._call_tool"""

    def test_call_tool_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        result_content = [{"type": "text", "text": "ok"}]
        mock_send = Mock(return_value={"content": result_content})
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client._call_tool("my_tool", {"arg": 1})
        assert result == result_content
        sent_payload = mock_send.call_args[0][0]
        assert sent_payload["method"] == "tools/call"
        assert sent_payload["params"]["name"] == "my_tool"
        assert sent_payload["params"]["arguments"] == {"arg": 1}
        assert "id" in sent_payload

    def test_call_tool_none_result_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        monkeypatch.setattr(client, "_send_jsonrpc", Mock(return_value=None))
        with pytest.raises(ICMConnectionError, match="returned None"):
            client._call_tool("my_tool", {})

    def test_call_tool_is_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        monkeypatch.setattr(
            client,
            "_send_jsonrpc",
            Mock(return_value={"isError": True, "content": []}),
        )
        with pytest.raises(ICMConnectionError, match="returned error"):
            client._call_tool("my_tool", {})

    def test_call_tool_empty_content_no_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        monkeypatch.setattr(
            client,
            "_send_jsonrpc",
            Mock(return_value={"content": []}),
        )
        result = client._call_tool("my_tool", {})
        assert result is None


# ---------------------------------------------------------------------------
# IcmMcpClient — _send_initialize
# ---------------------------------------------------------------------------


class TestIcmMcpClientSendInitialize:
    """IcmMcpClient._send_initialize"""

    def test_send_initialize_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_send = Mock(return_value={"serverInfo": {"name": "icm"}})
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        client._send_initialize()
        assert mock_send.call_count == 2
        # First call: initialize
        init_call = mock_send.call_args_list[0]
        assert init_call[0][0]["method"] == "initialize"
        assert init_call[1]["expect_response"] is True
        # Second call: initialized notification
        notif_call = mock_send.call_args_list[1]
        assert notif_call[0][0]["method"] == "notifications/initialized"
        assert notif_call[1]["expect_response"] is False

    def test_send_initialize_result_none_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        monkeypatch.setattr(client, "_send_jsonrpc", Mock(return_value=None))
        with pytest.raises(ICMConnectionError, match="returned None"):
            client._send_initialize()


# ---------------------------------------------------------------------------
# IcmMcpClient — start
# ---------------------------------------------------------------------------


class TestIcmMcpClientStart:
    """IcmMcpClient.start"""

    def test_start_already_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_proc = Mock()
        mock_proc.poll.return_value = None  # alive
        client._proc = mock_proc
        mock_popen = Mock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        client.start()
        mock_popen.assert_not_called()

    def test_start_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_proc_obj = Mock()
        mock_proc_obj.poll.return_value = None
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=mock_proc_obj))
        monkeypatch.setattr(client, "_send_initialize", Mock())
        client.start()
        assert client._proc is mock_proc_obj

    def test_start_with_db_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_proc_obj = Mock()
        mock_proc_obj.poll.return_value = None
        mock_popen = Mock(return_value=mock_proc_obj)
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(client, "_send_initialize", Mock())
        client.start(db_path=Path("/tmp/test.db"))
        argv = mock_popen.call_args[0][0]
        assert "--db" in argv
        assert "/tmp/test.db" in argv
        assert "--no-embeddings" in argv

    def test_start_with_embeddings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_proc_obj = Mock()
        mock_proc_obj.poll.return_value = None
        mock_popen = Mock(return_value=mock_proc_obj)
        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(client, "_send_initialize", Mock())
        client.start(use_embeddings=True)
        argv = mock_popen.call_args[0][0]
        assert "--no-embeddings" not in argv

    def test_start_file_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        monkeypatch.setattr(
            subprocess,
            "Popen",
            Mock(side_effect=FileNotFoundError("icm not found")),
        )
        with pytest.raises(ICMConnectionError, match="icm binary not found"):
            client.start()

    def test_start_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        monkeypatch.setattr(
            subprocess,
            "Popen",
            Mock(side_effect=OSError("permission denied")),
        )
        with pytest.raises(ICMConnectionError, match="failed to spawn"):
            client.start()

    def test_start_initialize_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_proc_obj = Mock()
        mock_proc_obj.poll.return_value = None
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=mock_proc_obj))
        monkeypatch.setattr(
            client,
            "_send_initialize",
            Mock(side_effect=ValueError("init failed")),
        )
        mock_kill = Mock()
        monkeypatch.setattr(client, "_kill", mock_kill)
        with pytest.raises(ICMConnectionError, match="initialize handshake failed"):
            client.start()
        mock_kill.assert_called_once()


# ---------------------------------------------------------------------------
# IcmMcpClient — close
# ---------------------------------------------------------------------------


class TestIcmMcpClientClose:
    """IcmMcpClient.close"""

    def test_close_no_proc(self) -> None:
        client = IcmMcpClient()
        client._proc = None
        client.close()  # should not raise

    def test_close_sends_notification_and_kills(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        client._proc = Mock()  # just so it's not None
        mock_send = Mock(return_value=None)
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        mock_kill = Mock()
        monkeypatch.setattr(client, "_kill", mock_kill)
        client.close()
        mock_send.assert_called_once()
        payload = mock_send.call_args[0][0]
        assert payload["method"] == "notifications/initialized"
        assert mock_send.call_args[1]["expect_response"] is False
        mock_kill.assert_called_once()

    def test_close_send_exception_suppressed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception from _send_jsonrpc is suppressed; _kill still called."""
        client = IcmMcpClient()
        client._proc = Mock()
        mock_send = Mock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        mock_kill = Mock()
        monkeypatch.setattr(client, "_kill", mock_kill)
        client.close()  # should not raise
        mock_send.assert_called_once()
        mock_kill.assert_called_once()


# ---------------------------------------------------------------------------
# IcmMcpClient — call_recall
# ---------------------------------------------------------------------------


class TestIcmMcpClientCallRecall:
    """IcmMcpClient.call_recall"""

    def test_recall_disabled_returns_empty_list(self) -> None:
        client = IcmMcpClient()
        client._disabled = True
        assert client.call_recall("hello") == []

    def test_recall_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_send = Mock(
            return_value={
                "content": [
                    {"type": "text", "text": "[preferences] Hello world"}
                ]
            }
        )
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_recall("hello", limit=5)
        assert result == [{"topic": "preferences", "summary": "Hello world"}]
        args = mock_send.call_args[0][0]["params"]["arguments"]
        assert args["query"] == "hello"
        assert args["limit"] == 5

    def test_recall_with_topic_and_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(
            return_value={
                "content": [{"type": "text", "text": "[prefs] Hello"}]
            }
        )
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_recall(
            "hello", limit=3, topic="preferences", project="myapp"
        )
        assert result == [{"topic": "prefs", "summary": "Hello"}]
        args = mock_send.call_args[0][0]["params"]["arguments"]
        assert args["topic"] == "preferences"
        assert args["project"] == "myapp"

    def test_recall_icm_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_icm_memory.mcp_client import _TOOL_RECALL

        client = IcmMcpClient()
        # Make _call_tool raise ICMError by having _send_jsonrpc return error
        mock_send = Mock(
            return_value={
                "isError": True,
                "content": [],
            }
        )
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_recall("hello")
        assert result == []

    def test_recall_unexpected_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(side_effect=RuntimeError("unexpected"))
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_recall("hello")
        assert result == []


# ---------------------------------------------------------------------------
# IcmMcpClient — call_store
# ---------------------------------------------------------------------------


class TestIcmMcpClientCallStore:
    """IcmMcpClient.call_store"""

    def test_store_disabled_returns_false(self) -> None:
        client = IcmMcpClient()
        client._disabled = True
        assert client.call_store("topic", "content", "high") is False

    def test_store_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_send = Mock(return_value={"content": [{"type": "text", "text": "ok"}]})
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_store("my_topic", "my content", "high")
        assert result is True
        args = mock_send.call_args[0][0]["params"]["arguments"]
        assert args["topic"] == "my_topic"
        assert args["content"] == "my content"
        assert args["importance"] == "high"
        assert "keywords" not in args

    def test_store_with_keywords(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_send = Mock(return_value={"content": [{"type": "text", "text": "ok"}]})
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_store(
            "my_topic", "my content", "high", keywords="kw1,kw2"
        )
        assert result is True
        args = mock_send.call_args[0][0]["params"]["arguments"]
        assert args["keywords"] == "kw1,kw2"

    def test_store_icm_error_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(
            return_value={"isError": True, "content": []}
        )
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_store("topic", "content", "high")
        assert result is False

    def test_store_unexpected_error_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(side_effect=RuntimeError("unexpected"))
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_store("topic", "content", "high")
        assert result is False


# ---------------------------------------------------------------------------
# IcmMcpClient — call_topics
# ---------------------------------------------------------------------------


class TestIcmMcpClientCallTopics:
    """IcmMcpClient.call_topics"""

    def test_topics_disabled_returns_empty_list(self) -> None:
        client = IcmMcpClient()
        client._disabled = True
        assert client.call_topics() == []

    def test_topics_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_send = Mock(
            return_value={
                "content": [
                    {
                        "type": "text",
                        "text": "Topic            Count\nerrors-resolved  3\n",
                    }
                ]
            }
        )
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_topics()
        assert {"topic": "errors-resolved", "count": "3"} in result

    def test_topics_icm_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(return_value={"isError": True, "content": []})
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_topics()
        assert result == []

    def test_topics_unexpected_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(side_effect=RuntimeError("unexpected"))
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_topics()
        assert result == []


# ---------------------------------------------------------------------------
# IcmMcpClient — call_health
# ---------------------------------------------------------------------------


class TestIcmMcpClientCallHealth:
    """IcmMcpClient.call_health"""

    def test_health_disabled_returns_empty_dict(self) -> None:
        client = IcmMcpClient()
        client._disabled = True
        assert client.call_health() == {}

    def test_health_success_no_topic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_send = Mock(
            return_value={
                "content": [
                    {
                        "type": "text",
                        "text": "Total memories: 42\nStale: 0\n",
                    }
                ]
            }
        )
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_health()
        assert result["total_memories"] == "42"
        assert result["stale"] == "0"
        # No topic param sent
        args = mock_send.call_args[0][0]["params"]["arguments"]
        assert "topic" not in args

    def test_health_with_topic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = IcmMcpClient()
        mock_send = Mock(
            return_value={
                "content": [
                    {"type": "text", "text": "Total memories: 5\nStale: 0\n"}
                ]
            }
        )
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_health(topic="preferences")
        assert result["total_memories"] == "5"
        args = mock_send.call_args[0][0]["params"]["arguments"]
        assert args["topic"] == "preferences"

    def test_health_icm_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(return_value={"isError": True, "content": []})
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_health()
        assert result == {}

    def test_health_unexpected_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = IcmMcpClient()
        mock_send = Mock(side_effect=RuntimeError("unexpected"))
        monkeypatch.setattr(client, "_send_jsonrpc", mock_send)
        result = client.call_health()
        assert result == {}
