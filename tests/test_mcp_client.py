"""Unit tests for ``hermes_icm_memory.mcp_client`` — parsing helpers.

Does NOT test the subprocess/lifecycle (those need integration or heavy mocking).
Focuses on the response-parsing logic which is pure text processing.
"""

from __future__ import annotations

from hermes_icm_memory.mcp_client import (
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


# ---------------------------------------------------------------------------
# _normalize_key
# ---------------------------------------------------------------------------


def test_normalize_key() -> None:
    assert _normalize_key("Total memories") == "total_memories"
    assert _normalize_key("  key  ") == "key"
    assert _normalize_key("") == ""
