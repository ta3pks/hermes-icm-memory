"""Tests for ``hermes_icm_memory.classifier`` — ``ClassifyTask``,
``ClassifierResult``, and ``classify_exchange``.

Achieves near-100 % line/branch coverage of classifier.py.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
import urllib.error
import urllib.request

from hermes_icm_memory.classifier import (
    ClassifierResult,
    ClassifyTask,
    classify_exchange,
)

# ===================================================================
# Shared constants
# ===================================================================

KW = {
    "endpoint": "https://api.openai.com/v1/chat/completions",
    "model": "gpt-4",
}

# ===================================================================
# Helper factories  (use patch + MagicMock for reliable urlopen mocking)
# ===================================================================


def _patch_urlopen(body: str):
    """Return a context manager that patches ``urllib.request.urlopen``
    to return a response yielding *body* as UTF-8 bytes."""

    def _start(mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = body.encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

    return patch("urllib.request.urlopen", autospec=True), _start


def _patch_urlopen_error(exc: Exception):
    """Return a context manager that patches ``urllib.request.urlopen``
    to raise *exc*."""

    def _start(mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = exc

    return patch("urllib.request.urlopen", autospec=True), _start


def _patch_urlopen_capture(captured: list):
    """Return a context manager that captures Request objects and
    returns a successful response."""

    def _start(mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "store": {"topic": "preferences", "importance": "high",
                           "content": "remember this", "keywords": ["kw1"]}
            })}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # Side-effect: capture the Request
        original = mock_urlopen.side_effect
        mock_urlopen.side_effect = lambda req, **kw: (
            captured.append(req) or mock_urlopen.return_value
        )

    return patch("urllib.request.urlopen", autospec=True), _start


def _llm_response(content: str) -> str:
    """Build an OpenAI chat-completions JSON response with *content* as the
    assistant message content."""
    return json.dumps({
        "choices": [
            {"message": {"content": content}},
        ],
    })


def _store_response(**overrides) -> str:
    """Build a complete OpenAI response whose LLM content is
    ``{\"store\": {...}}`` with *overrides* applied to the store dict."""
    store = {
        "topic": "preferences",
        "importance": "high",
        "content": "remember this",
        "keywords": ["kw1", "kw2"],
    }
    store.update(overrides)
    return _llm_response(json.dumps({"store": store}))


# ===================================================================
# ClassifyTask dataclass
# ===================================================================


class TestClassifyTask:
    def test_fields(self) -> None:
        t = ClassifyTask(user_text="u", assistant_text="a", project="p")
        assert t.user_text == "u"
        assert t.assistant_text == "a"
        assert t.project == "p"

    def test_project_none(self) -> None:
        t = ClassifyTask(user_text="u", assistant_text="a", project=None)
        assert t.project is None

    def test_frozen(self) -> None:
        t = ClassifyTask(user_text="u", assistant_text="a", project="p")
        with pytest.raises(AttributeError):
            t.user_text = "x"  # type: ignore[misc]


# ===================================================================
# ClassifierResult dataclass
# ===================================================================


class TestClassifierResult:
    def test_fields(self) -> None:
        r = ClassifierResult(
            topic="t", importance="i", content="c", keywords=("k",),
        )
        assert r.topic == "t"
        assert r.importance == "i"
        assert r.content == "c"
        assert r.keywords == ("k",)

    def test_default_keywords(self) -> None:
        r = ClassifierResult(topic="t", importance="i", content="c")
        assert r.keywords == ()

    def test_frozen(self) -> None:
        r = ClassifierResult(topic="t", importance="i", content="c")
        with pytest.raises(AttributeError):
            r.topic = "x"  # type: ignore[misc]


# ===================================================================
# classify_exchange — success paths
# ===================================================================


class TestClassifyExchangeSuccess:
    """Happy-path LLM returns a valid ``store`` object."""

    def test_basic(self) -> None:
        patch_cm, start = _patch_urlopen(_store_response())
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("hello", "world", **KW)
        assert isinstance(result, ClassifierResult)
        assert result.topic == "preferences"
        assert result.importance == "high"
        assert result.content == "remember this"
        assert result.keywords == ("kw1", "kw2")

    def test_with_api_key(self) -> None:
        """``api_key`` is set → ``Authorization: Bearer ...`` header added."""
        captured_reqs: list = []

        patch_cm, _start = _patch_urlopen_capture(captured_reqs)
        with patch_cm as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a", api_key="sk-test-123", **KW)
        assert len(captured_reqs) == 1
        assert captured_reqs[0].headers.get("Authorization") == "Bearer sk-test-123"

    def test_without_api_key(self) -> None:
        """No ``api_key`` → no ``Authorization`` header."""
        captured_reqs: list = []

        patch_cm, _start = _patch_urlopen_capture(captured_reqs)
        with patch_cm as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a", **KW)
        assert len(captured_reqs) == 1
        assert "Authorization" not in captured_reqs[0].headers

    def test_markdown_fence_with_lang(self) -> None:
        """LLM wraps JSON in ```json … ``` fences."""
        store = json.dumps({
            "store": {
                "topic": "decisions",
                "importance": "critical",
                "content": "use fastapi",
                "keywords": ["fastapi"],
            }
        })
        body = _llm_response(f"```json\n{store}\n```")
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert isinstance(result, ClassifierResult)
        assert result.topic == "decisions"
        assert result.importance == "critical"

    def test_markdown_fence_no_lang(self) -> None:
        """LLM wraps JSON in ``` … ``` without a language tag."""
        store = json.dumps({
            "store": {"topic": "learnings", "importance": "low",
                       "content": "pi has limited ram"}
        })
        body = _llm_response(f"```\n{store}\n```")
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert isinstance(result, ClassifierResult)
        assert result.topic == "learnings"

    def test_markdown_fence_malformed_close(self) -> None:
        """LLM wraps JSON in ``` … `` (double-backtick close) — fallback path."""
        store = json.dumps({
            "store": {"topic": "context", "importance": "medium",
                       "content": "malformed fence"}
        })
        body = _llm_response(f"```\n{store}\n``")
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert isinstance(result, ClassifierResult)
        assert result.topic == "context"
        assert result.importance == "medium"

    def test_empty_keywords(self) -> None:
        """No ``keywords`` key in store → empty tuple."""
        body = _store_response(keywords=[])
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is not None
        assert result.keywords == ()

    def test_content_capped(self) -> None:
        """Content longer than 500 chars → truncated to 500."""
        body = _store_response(content="x" * 1000)
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is not None
        assert len(result.content) == 500
        assert result.content == "x" * 500

    def test_keywords_capped(self) -> None:
        """More than 5 keywords → only first 5 kept."""
        many = [str(i) for i in range(10)]
        body = _store_response(keywords=many)
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is not None
        assert result.keywords == tuple(str(i) for i in range(5))

    def test_user_text_truncated(self) -> None:
        """Long user_text truncated to 1500 chars in the prompt."""
        captured_reqs: list = []

        def _start(mock_urlopen: MagicMock) -> None:
            mock_resp = MagicMock()
            mock_resp.read.return_value = _store_response().encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = lambda req, **kw: (
                captured_reqs.append(req) or mock_urlopen.return_value
            )

        with patch("urllib.request.urlopen", autospec=True) as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u" * 2000, "a", **KW)
        payload = json.loads(captured_reqs[0].data)
        user_msg = payload["messages"][1]["content"]
        assert "u" * 1500 in user_msg
        assert "u" * 2000 not in user_msg

    def test_assistant_text_truncated(self) -> None:
        """Long assistant_text truncated to 2000 chars in the prompt."""
        captured_reqs: list = []

        def _start(mock_urlopen: MagicMock) -> None:
            mock_resp = MagicMock()
            mock_resp.read.return_value = _store_response().encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = lambda req, **kw: (
                captured_reqs.append(req) or mock_urlopen.return_value
            )

        with patch("urllib.request.urlopen", autospec=True) as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a" * 3000, **KW)
        payload = json.loads(captured_reqs[0].data)
        user_msg = payload["messages"][1]["content"]
        assert "a" * 2000 in user_msg
        assert "a" * 3000 not in user_msg

    def test_fence_no_newline(self) -> None:
        """Edge case: response starts with ``` but has no newline (single line).
        Since the entire string including ``` prefix is passed to json.loads,
        this fails JSON parse → None."""
        store = json.dumps({
            "store": {"topic": "context", "importance": "low",
                       "content": "no-newline fence"}
        })
        body = _llm_response(f"```{store}```")
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is None


# ===================================================================
# classify_exchange — store is null
# ===================================================================


class TestClassifyExchangeNoStore:
    def test_store_null(self) -> None:
        """LLM returns ``{\"store\": null, \"reason\": \"...\"}`` → ``None``."""
        body = _llm_response(
            json.dumps({"store": None, "reason": "nothing to remember"})
        )
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is None


# ===================================================================
# classify_exchange — network errors
# ===================================================================


class TestClassifyExchangeNetworkErrors:
    def test_http_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        exc = urllib.error.HTTPError(
            url="http://example.com",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=None,
        )
        patch_cm, start = _patch_urlopen_error(exc)
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("HTTP 503" in r.message for r in caplog.records)

    def test_url_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        exc = urllib.error.URLError("connection refused")
        patch_cm, start = _patch_urlopen_error(exc)
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("endpoint unreachable" in r.message for r in caplog.records)

    def test_timeout(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        exc = TimeoutError("timed out")
        patch_cm, start = _patch_urlopen_error(exc)
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("timeout after" in r.message for r in caplog.records)

    def test_generic_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        exc = RuntimeError("something broke unexpectedly")
        patch_cm, start = _patch_urlopen_error(exc)
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("request failed" in r.message for r in caplog.records)


# ===================================================================
# classify_exchange — response parsing errors
# ===================================================================


class TestClassifyExchangeResponseParsing:
    def test_non_json_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Response body is not valid JSON."""
        patch_cm, start = _patch_urlopen("not json at all")
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("unparseable response" in r.message for r in caplog.records)

    def test_api_error_in_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Response JSON contains an ``error`` key."""
        body = json.dumps({"error": "rate limited"})
        patch_cm, start = _patch_urlopen(body)
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("API error" in r.message for r in caplog.records)

    def test_empty_choices(self) -> None:
        """Response has empty ``choices`` list."""
        body = json.dumps({"choices": []})
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is None

    def test_choices_missing(self) -> None:
        """Response has no ``choices`` key at all."""
        body = json.dumps({})
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is None

    def test_missing_message_content(self) -> None:
        """Choice has no ``message.content``."""
        body = json.dumps({"choices": [{"message": {}}]})
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is None

    def test_empty_message_content(self) -> None:
        """Choice has empty ``message.content``."""
        body = json.dumps({"choices": [{"message": {"content": ""}}]})
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is None

    def test_missing_message_key(self) -> None:
        """Choice exists but has no ``message`` key → falls through to empty
        dict ``{}.get(\"content\", \"\") → \"\" → returns None."""
        body = json.dumps({"choices": [{"not_message": "foo"}]})
        patch_cm, start = _patch_urlopen(body)
        with patch_cm as mock_urlopen:
            start(mock_urlopen)
            result = classify_exchange("u", "a", **KW)
        assert result is None


# ===================================================================
# classify_exchange — LLM output decoding errors
# ===================================================================


class TestClassifyExchangeLLMOutput:
    def test_llm_output_not_json(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """LLM returned plain text, not JSON."""
        body = _llm_response("I don't think there's anything to remember here.")
        patch_cm, start = _patch_urlopen(body)
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("LLM output not JSON" in r.message for r in caplog.records)

    def test_llm_output_json_decode_error_after_fence_strip(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """After stripping ``` fences, the remaining text is not valid JSON.

        This covers the fence-stripping code path and then the
        ``json.JSONDecodeError`` at line ~191.
        """
        body = _llm_response("```\nnot-json-content\n```")
        patch_cm, start = _patch_urlopen(body)
        with caplog.at_level(logging.DEBUG, logger="hermes_icm_memory.classifier"):
            with patch_cm as mock_urlopen:
                start(mock_urlopen)
                result = classify_exchange("u", "a", **KW)
        assert result is None
        assert any("LLM output not JSON" in r.message for r in caplog.records)


# ===================================================================
# classify_exchange — verify payload structure
# ===================================================================


class TestClassifyExchangePayload:
    def test_system_prompt_included(self) -> None:
        """The request payload contains the system prompt as first message."""
        captured_reqs: list = []

        def _start(mock_urlopen: MagicMock) -> None:
            mock_resp = MagicMock()
            mock_resp.read.return_value = _store_response().encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = lambda req, **kw: (
                captured_reqs.append(req) or mock_urlopen.return_value
            )

        with patch("urllib.request.urlopen", autospec=True) as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a", **KW)
        payload = json.loads(captured_reqs[0].data)
        assert payload["model"] == "gpt-4"
        assert payload["stream"] is False
        assert payload["messages"][0]["role"] == "system"
        assert "You are a memory classifier" in payload["messages"][0]["content"]

    def test_post_method(self) -> None:
        """The request uses the POST method."""
        captured_reqs: list = []

        def _start(mock_urlopen: MagicMock) -> None:
            mock_resp = MagicMock()
            mock_resp.read.return_value = _store_response().encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = lambda req, **kw: (
                captured_reqs.append(req) or mock_urlopen.return_value
            )

        with patch("urllib.request.urlopen", autospec=True) as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a", **KW)
        assert captured_reqs[0].method == "POST"
        assert captured_reqs[0].full_url == KW["endpoint"]

    def test_content_type_header(self) -> None:
        """Content-Type and User-Agent headers are set."""
        captured_reqs: list = []

        def _start(mock_urlopen: MagicMock) -> None:
            mock_resp = MagicMock()
            mock_resp.read.return_value = _store_response().encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = lambda req, **kw: (
                captured_reqs.append(req) or mock_urlopen.return_value
            )

        with patch("urllib.request.urlopen", autospec=True) as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a", **KW)
        # urllib.request.Request stores headers in a dict that is
        # accessible via .headers (or .header_items()).
        headers = dict(captured_reqs[0].headers.items())
        # urllib.request.Request lowercases header names internally.
        assert headers.get("Content-type") == "application/json"
        assert headers.get("User-agent") == "HermesICM/0.4"


# ===================================================================
# classify_exchange — default timeout_s
# ===================================================================


class TestClassifyExchangeTimeout:
    def test_default_timeout(self) -> None:
        """``timeout_s`` defaults to 8.0 seconds."""
        captured_timeouts: list = []

        def _start(mock_urlopen: MagicMock) -> None:
            mock_resp = MagicMock()
            mock_resp.read.return_value = _store_response().encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = lambda req, *, timeout=8.0: (
                captured_timeouts.append(timeout) or mock_urlopen.return_value
            )

        with patch("urllib.request.urlopen", autospec=True) as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a", **KW)
        assert captured_timeouts == [8.0]

    def test_custom_timeout(self) -> None:
        """``timeout_s`` can be overridden."""
        captured_timeouts: list = []

        def _start(mock_urlopen: MagicMock) -> None:
            mock_resp = MagicMock()
            mock_resp.read.return_value = _store_response().encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            mock_urlopen.side_effect = lambda req, *, timeout=8.0: (
                captured_timeouts.append(timeout) or mock_urlopen.return_value
            )

        with patch("urllib.request.urlopen", autospec=True) as mock_urlopen:
            _start(mock_urlopen)
            classify_exchange("u", "a", timeout_s=15.0, **KW)
        assert captured_timeouts == [15.0]


# ===================================================================
# classify_exchange — missing keyword arguments
# ===================================================================


class TestClassifyExchangeRequiredArgs:
    def test_missing_endpoint_raises(self) -> None:
        """``endpoint`` is required (keyword-only after first two args)."""
        with pytest.raises(TypeError, match=r"endpoint"):
            classify_exchange("u", "a", model="gpt-4")  # type: ignore[call-arg]

    def test_missing_model_raises(self) -> None:
        """``model`` is required."""
        with pytest.raises(TypeError, match=r"model"):
            classify_exchange("u", "a", endpoint="http://example.com")  # type: ignore[call-arg]
