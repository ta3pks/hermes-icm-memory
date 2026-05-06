"""Unit tests for the v0.2 MCP transport inside ``cli_runner``.

The MCP client is a private section of ``cli_runner.py`` (AD-12 — only this
module imports ``subprocess``). Tests mock ``subprocess.Popen`` and supply
fake stdin/stdout pipes so no real ``icm serve`` is spawned.

Coverage matrix:

* ``mcp_start`` argv shape (with / without ``--db``, with / without
  ``--no-embeddings``)
* ``mcp_start`` handshake — sends ``initialize`` + ``notifications/initialized``
  exactly once
* ``_mcp_recall`` JSON-RPC request shape: id increments, name + arguments,
  empty-string project to disable cwd filter
* ``_mcp_recall`` text-response parsing into ``[{"topic", "summary"}, ...]``
* ``_mcp_topics`` / ``_mcp_health`` parsers
* lifecycle: ``mcp_stop`` closes stdin and terminates Popen; subsequent
  ``mcp_start`` re-spawns
* respawn-once policy: first death → respawn; second death → ``_mcp_disabled``
  set, ``ICMNotFoundError`` raised, no third spawn
* timeout: response never arrives → ``ICMTimeoutError``
* lock serializes concurrent calls
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.errors import ICMNotFoundError, ICMTimeoutError

POPEN_TARGET = "hermes_icm_memory.cli_runner.subprocess.Popen"


# ---------------------------------------------------------------------------
# Test plumbing — fake bidirectional pipe that records writes and serves
# canned responses keyed by the JSON-RPC ``id`` in each incoming request.
# ---------------------------------------------------------------------------


class _FakeStdin:
    """Captures every line written by the MCP client for later inspection."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, data: str) -> int:
        if self.closed:
            raise BrokenPipeError("fake stdin closed")
        self.lines.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    """Yields canned response lines via ``readline``; ``b''``/``''`` on EOF."""

    def __init__(self, responses: list[str]) -> None:
        self._buffer = io.StringIO("".join(responses))

    def readline(self) -> str:
        return self._buffer.readline()

    def close(self) -> None:
        pass


def _initialize_response(req_id: int = 1) -> str:
    """Canned response for the MCP ``initialize`` handshake call."""
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "icm", "version": "0.10.43"},
                },
            }
        )
        + "\n"
    )


def _tools_call_response(req_id: int, text: str) -> str:
    """Canned ``tools/call`` response wrapping the given text in MCP shape."""
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        )
        + "\n"
    )


def _make_popen_mock(stdout_lines: list[str]) -> tuple[MagicMock, _FakeStdin, _FakeStdout]:
    """Build a Popen mock + the fake pipes it serves up."""
    fake_stdin = _FakeStdin()
    fake_stdout = _FakeStdout(stdout_lines)
    popen = MagicMock()
    popen.stdin = fake_stdin
    popen.stdout = fake_stdout
    popen.poll.return_value = None  # alive
    popen.terminate = MagicMock()
    popen.kill = MagicMock()
    popen.wait = MagicMock()
    return popen, fake_stdin, fake_stdout


@pytest.fixture(autouse=True)
def _reset_mcp_state() -> Any:
    """Ensure each test starts with no daemon held in module state."""
    cli_runner._mcp_reset_state_for_tests()
    yield
    cli_runner._mcp_reset_state_for_tests()


# ---------------------------------------------------------------------------
# AC3 — argv shape + handshake
# ---------------------------------------------------------------------------


def test_mcp_start_argv_with_db_and_embeddings_on() -> None:
    """``--db`` is forwarded; no ``--no-embeddings`` when use_embeddings=True."""
    popen, _stdin, _stdout = _make_popen_mock([_initialize_response()])
    db = Path("/tmp/x.db")
    with patch(POPEN_TARGET, return_value=popen) as p:
        cli_runner.mcp_start(db_path=db, use_embeddings=True)
    argv = p.call_args.args[0]
    assert argv[0] == "icm"
    assert argv[-1] == "serve"
    assert "--db" in argv
    assert str(db) in argv
    assert "--no-embeddings" not in argv


def test_mcp_start_argv_without_db_and_no_embeddings() -> None:
    """``db_path=None`` omits ``--db``; ``use_embeddings=False`` adds ``--no-embeddings``."""
    popen, _stdin, _stdout = _make_popen_mock([_initialize_response()])
    with patch(POPEN_TARGET, return_value=popen) as p:
        cli_runner.mcp_start(db_path=None, use_embeddings=False)
    argv = p.call_args.args[0]
    assert "--db" not in argv
    assert "--no-embeddings" in argv
    assert argv[-1] == "serve"


def test_mcp_start_sends_handshake() -> None:
    """The handshake writes one ``initialize`` and one ``notifications/initialized``."""
    popen, fake_stdin, _stdout = _make_popen_mock([_initialize_response()])
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
    # Each line is a complete JSON-RPC frame ending in '\n'.
    assert all(line.endswith("\n") for line in fake_stdin.lines)
    parsed = [json.loads(line) for line in fake_stdin.lines]
    methods = [msg.get("method") for msg in parsed]
    assert "initialize" in methods
    assert "notifications/initialized" in methods


# ---------------------------------------------------------------------------
# AC2 — JSON-RPC request shape + parsed response for recall
# ---------------------------------------------------------------------------


def test_mcp_recall_request_shape_and_response_parsing() -> None:
    """``run_recall(transport='mcp')`` sends one ``tools/call`` and parses the text blob."""
    text = (
        "[hubs] **Title One**\n\nFirst body content.\n\n"
        "[decisions-x] **Title Two**\n\nSecond body content.\n"
    )
    responses = [
        _initialize_response(req_id=1),
        _tools_call_response(req_id=2, text=text),
    ]
    popen, fake_stdin, _stdout = _make_popen_mock(responses)
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        hits = cli_runner.run_recall(
            query="hello",
            limit=3,
            db_path=None,
            timeout_ms=2000,
            transport="mcp",
            project="hermes",
        )

    # Last-written line is the recall request.
    request = json.loads(fake_stdin.lines[-1])
    assert request["jsonrpc"] == "2.0"
    assert isinstance(request["id"], int)
    assert request["method"] == "tools/call"
    params = request["params"]
    assert params["name"] == "icm_memory_recall"
    args = params["arguments"]
    assert args["query"] == "hello"
    assert args["limit"] == 3
    # AC5 — project is forwarded; if caller passed a value, use it; if None, the
    # adapter forces ``""`` to disable the cwd-based filter.
    assert args["project"] == "hermes"

    # Response text parsed into hit-shaped dicts.
    assert isinstance(hits, list)
    assert len(hits) == 2
    assert hits[0]["topic"] == "hubs"
    assert "First body content" in hits[0]["summary"]
    assert hits[1]["topic"] == "decisions-x"
    assert "Second body content" in hits[1]["summary"]


def test_mcp_recall_with_no_project_passes_empty_string() -> None:
    """``project=None`` from the caller normalizes to ``""`` to defeat cwd filter."""
    text = "[topic-a] **t**\n\ncontent.\n"
    responses = [
        _initialize_response(req_id=1),
        _tools_call_response(req_id=2, text=text),
    ]
    popen, fake_stdin, _stdout = _make_popen_mock(responses)
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        cli_runner.run_recall(
            query="q", limit=2, db_path=None, timeout_ms=2000, transport="mcp"
        )
    request = json.loads(fake_stdin.lines[-1])
    args = request["params"]["arguments"]
    assert args["project"] == ""


def test_mcp_recall_id_counter_monotonic() -> None:
    """Successive recall calls use strictly-increasing JSON-RPC ids."""
    responses = [
        _initialize_response(req_id=1),
        _tools_call_response(req_id=2, text="[t] **x**\n\nbody.\n"),
        _tools_call_response(req_id=3, text="[t] **y**\n\nbody.\n"),
    ]
    popen, fake_stdin, _stdout = _make_popen_mock(responses)
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        cli_runner.run_recall(
            query="q1", limit=1, db_path=None, timeout_ms=2000, transport="mcp"
        )
        cli_runner.run_recall(
            query="q2", limit=1, db_path=None, timeout_ms=2000, transport="mcp"
        )
    ids = [
        json.loads(line)["id"]
        for line in fake_stdin.lines
        if "id" in json.loads(line)
    ]
    # initialize id, recall1 id, recall2 id — all distinct and increasing.
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# AC2 — topics + health parsers
# ---------------------------------------------------------------------------


def test_mcp_topics_parses_text_into_list_of_dicts() -> None:
    text = (
        "Topics:\n"
        "  errors-resolved: 3 memories\n"
        "  decisions-x: 7 memories\n"
    )
    responses = [
        _initialize_response(req_id=1),
        _tools_call_response(req_id=2, text=text),
    ]
    popen, _stdin, _stdout = _make_popen_mock(responses)
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        result = cli_runner.run_topics(
            db_path=None, timeout_ms=2000, transport="mcp"
        )
    assert {"topic": "errors-resolved", "count": "3"} in result
    assert {"topic": "decisions-x", "count": "7"} in result


def test_mcp_health_wraps_text_in_raw_field() -> None:
    text = "Memory Health Report:\n  topic-a: ok healthy\n    entries: 1\n"
    responses = [
        _initialize_response(req_id=1),
        _tools_call_response(req_id=2, text=text),
    ]
    popen, _stdin, _stdout = _make_popen_mock(responses)
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        result = cli_runner.run_health(
            db_path=None, timeout_ms=2000, transport="mcp"
        )
    assert "raw" in result
    assert "topic-a" in result["raw"]


# ---------------------------------------------------------------------------
# AC3 — lifecycle (mcp_stop closes stdin + terminates Popen)
# ---------------------------------------------------------------------------


def test_mcp_stop_closes_pipe_and_terminates_subprocess() -> None:
    popen, fake_stdin, _stdout = _make_popen_mock([_initialize_response()])
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        cli_runner.mcp_stop()
    assert fake_stdin.closed is True
    popen.terminate.assert_called_once()
    popen.wait.assert_called_once()


def test_mcp_stop_when_not_started_is_noop() -> None:
    """Calling stop with no live daemon is safe and silent."""
    cli_runner.mcp_stop()  # must not raise


def test_mcp_start_after_stop_respawns() -> None:
    """A second ``mcp_start`` after ``mcp_stop`` produces a fresh Popen call."""
    responses_a = [_initialize_response(req_id=1)]
    responses_b = [_initialize_response(req_id=1)]
    popen_a, _, _ = _make_popen_mock(responses_a)
    popen_b, _, _ = _make_popen_mock(responses_b)
    with patch(POPEN_TARGET, side_effect=[popen_a, popen_b]) as p:
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        cli_runner.mcp_stop()
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
    assert p.call_count == 2


# ---------------------------------------------------------------------------
# AC4 — respawn-once policy + degrade sentinel
# ---------------------------------------------------------------------------


def test_mcp_recall_respawns_once_after_broken_pipe() -> None:
    """First broken-pipe → respawn once → retry → succeed."""
    text = "[t] **ok**\n\nbody.\n"
    # Daemon A: handshake OK, then broken pipe on the recall write.
    popen_a, fake_stdin_a, _ = _make_popen_mock([_initialize_response(req_id=1)])
    # Force the recall write to raise BrokenPipeError once the handshake is done.
    real_write_a = fake_stdin_a.write
    write_count_a = {"calls": 0}

    def _fail_after_handshake(data: str) -> int:
        write_count_a["calls"] += 1
        if write_count_a["calls"] > 2:  # past initialize + initialized notification
            raise BrokenPipeError("daemon A died")
        return real_write_a(data)

    fake_stdin_a.write = _fail_after_handshake  # type: ignore[method-assign]

    # Daemon B: handshake OK, recall returns hits.
    popen_b, _stdin_b, _stdout_b = _make_popen_mock(
        [_initialize_response(req_id=1), _tools_call_response(req_id=2, text=text)]
    )

    with patch(POPEN_TARGET, side_effect=[popen_a, popen_b]) as p:
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        hits = cli_runner.run_recall(
            query="q",
            limit=2,
            db_path=None,
            timeout_ms=2000,
            transport="mcp",
        )
    assert p.call_count == 2  # original + respawn
    assert isinstance(hits, list)
    assert hits and hits[0]["topic"] == "t"


def test_mcp_recall_second_death_disables_and_raises() -> None:
    """Two deaths in a row → ``_mcp_disabled`` set, ``ICMNotFoundError`` raised."""
    popen_a, fake_stdin_a, _ = _make_popen_mock([_initialize_response(req_id=1)])
    popen_b, fake_stdin_b, _ = _make_popen_mock([_initialize_response(req_id=1)])

    def _kill_after_handshake(stdin: _FakeStdin) -> None:
        original = stdin.write
        count = {"n": 0}

        def _w(data: str) -> int:
            count["n"] += 1
            if count["n"] > 2:
                raise BrokenPipeError("dead")
            return original(data)

        stdin.write = _w  # type: ignore[method-assign]

    _kill_after_handshake(fake_stdin_a)
    _kill_after_handshake(fake_stdin_b)

    with patch(POPEN_TARGET, side_effect=[popen_a, popen_b]) as p:
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        with pytest.raises(ICMNotFoundError):
            cli_runner.run_recall(
                query="q",
                limit=2,
                db_path=None,
                timeout_ms=2000,
                transport="mcp",
            )
    assert p.call_count == 2  # original + one respawn, no third
    # Subsequent calls short-circuit without spawning.
    with patch(POPEN_TARGET) as p_third:
        with pytest.raises(ICMNotFoundError):
            cli_runner.run_recall(
                query="q2",
                limit=2,
                db_path=None,
                timeout_ms=2000,
                transport="mcp",
            )
        p_third.assert_not_called()


# ---------------------------------------------------------------------------
# AC5 — timeout
# ---------------------------------------------------------------------------


def test_mcp_recall_timeout_raises_when_response_never_arrives() -> None:
    """Empty stdout (no matching id) eventually surfaces ``ICMTimeoutError``."""
    # Handshake succeeds; the recall response is missing.
    popen, _stdin, _stdout = _make_popen_mock(
        [_initialize_response(req_id=1)]
    )
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        with pytest.raises(ICMTimeoutError):
            cli_runner.run_recall(
                query="q",
                limit=1,
                db_path=None,
                timeout_ms=20,  # tiny budget; test runs fast
                transport="mcp",
            )


# ---------------------------------------------------------------------------
# AC5 — concurrency (lock serializes calls)
# ---------------------------------------------------------------------------


def test_mcp_recall_lock_serializes_concurrent_calls() -> None:
    """Two threaded recalls don't interleave writes to stdin."""
    text = "[t] **x**\n\nbody.\n"
    responses = [
        _initialize_response(req_id=1),
        _tools_call_response(req_id=2, text=text),
        _tools_call_response(req_id=3, text=text),
    ]
    popen, fake_stdin, _stdout = _make_popen_mock(responses)
    with patch(POPEN_TARGET, return_value=popen):
        cli_runner.mcp_start(db_path=None, use_embeddings=True)

        def _call() -> None:
            cli_runner.run_recall(
                query="q",
                limit=1,
                db_path=None,
                timeout_ms=2000,
                transport="mcp",
            )

        t1 = threading.Thread(target=_call)
        t2 = threading.Thread(target=_call)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    # Each line should be a complete JSON object — no interleaved garbage.
    for line in fake_stdin.lines:
        json.loads(line)  # raises if torn write happened


# ---------------------------------------------------------------------------
# Regression — transport='cli' still uses the v0.1.1 fresh-subprocess path
# ---------------------------------------------------------------------------


def test_mcp_call_respawn_does_not_cascade_under_concurrency() -> None:
    """v0.2 review fix C2 — concurrent broken-pipe must trigger AT MOST one respawn.

    Before the lifecycle-lock fix, two threads racing through ``_mcp_call``
    against a dying daemon would each independently trigger ``_mcp_respawn``,
    cascading into N spawns for N concurrent callers (each paying the
    embedding-model cold start). With the module-level lock, only one of
    them does the respawn; the second picks up the fresh ``_mcp_state`` on
    its retry without triggering a second respawn.
    """
    text = "[errors-resolved] ok.\n"
    # Daemon A: dies on the recall write; daemon B: handshake + two recalls.
    popen_a, fake_stdin_a, _ = _make_popen_mock([_initialize_response(req_id=1)])
    real_a = fake_stdin_a.write
    a_writes = {"n": 0}

    def _a_die(data: str) -> int:
        a_writes["n"] += 1
        if a_writes["n"] > 2:
            raise BrokenPipeError("A died")
        return real_a(data)

    fake_stdin_a.write = _a_die  # type: ignore[method-assign]

    popen_b, _stdin_b, _stdout_b = _make_popen_mock(
        [
            _initialize_response(req_id=1),
            _tools_call_response(req_id=2, text=text),
            _tools_call_response(req_id=3, text=text),
            _tools_call_response(req_id=4, text=text),
            _tools_call_response(req_id=5, text=text),
        ]
    )

    with patch(POPEN_TARGET, side_effect=[popen_a, popen_b]) as p:
        cli_runner.mcp_start(db_path=None, use_embeddings=True)
        results: list[Any] = []
        errors: list[BaseException] = []

        def _call() -> None:
            try:
                hits = cli_runner.run_recall(
                    query="q",
                    limit=1,
                    db_path=None,
                    timeout_ms=2000,
                    transport="mcp",
                )
                results.append(hits)
            except BaseException as exc:  # noqa: BLE001 — record for assert
                errors.append(exc)

        threads = [threading.Thread(target=_call) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # Exactly two Popen calls — one initial spawn + ONE respawn — even
    # though four concurrent callers raced through ``_mcp_call``.
    assert p.call_count == 2, (
        f"expected exactly 2 spawns (start+1 respawn), got {p.call_count}"
    )
    assert not errors, f"unexpected errors: {errors!r}"
    # Every concurrent caller eventually got a non-empty hit list.
    assert len(results) == 4
    assert all(r and r[0]["topic"] == "errors-resolved" for r in results)


def test_parse_recall_text_ignores_markdown_link_syntax() -> None:
    """v0.2 review fix H2 — non-slug bracketed text inside a body must not split.

    A memory body containing ``[issue tracker]`` (space) or ``[Co-Authored-By]``
    (uppercase) used to be parsed as a phantom topic boundary, fabricating
    extra hits with bogus topic names. The slug-restricted regex must keep
    this content inside the original hit's summary.
    """
    text = (
        "[errors-resolved] Fixed import bug.\n"
        "Refs: [issue tracker], [Co-Authored-By], [1].\n"
        "[decisions-x] Locked the migration plan.\n"
    )
    hits = cli_runner._parse_mcp_recall_text(text)
    assert [h["topic"] for h in hits] == ["errors-resolved", "decisions-x"]
    # The first hit's summary keeps the markdown links inside the body.
    assert "issue tracker" in hits[0]["summary"]
    assert "Co-Authored-By" in hits[0]["summary"]


def test_run_recall_transport_cli_uses_subprocess_run() -> None:
    """Default ``transport='cli'`` keeps the v0.1.1 fresh-subprocess argv shape."""
    with patch(
        "hermes_icm_memory.cli_runner.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="[]", stderr=""),
    ) as run, patch(POPEN_TARGET) as popen_mock:
        cli_runner.run_recall(
            query="q",
            limit=2,
            db_path=Path("/tmp/x.db"),
            timeout_ms=2000,
            transport="cli",
        )
    run.assert_called_once()
    popen_mock.assert_not_called()
