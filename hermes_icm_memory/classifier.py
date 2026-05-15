"""LLM-based exchange classifier — async memory-trigger detection (v0.4+).

Calls a configurable LLM endpoint using the OpenAI-compatible chat completions
format (``POST /chat/completions``). The endpoint, model, and API key are resolved
from the Hermes provider config — no separate auth setup needed.

Runs in the background classifier worker thread — never blocks the turn loop.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

__all__ = [
    "ClassifyTask",
    "ClassifierResult",
    "classify_exchange",
]

logger = logging.getLogger(__name__)

#: System prompt sent to the classifier LLM. Instructs JSON-only output.
_SYSTEM_PROMPT: str = """\
You are a memory classifier. Given a conversation exchange, determine if \
there is anything worth remembering long-term.

An exchange is worth remembering when it contains:
- A user preference ("I prefer dark mode", "Always use port 8080")
- A resolved error or root cause ("the bug was the port was already in use")
- A decision ("we decided to use FastAPI")
- A learning or insight ("turns out the Pi doesn't have enough RAM")

Respond with a JSON object. If nothing worth storing:
{{"store": null, "reason": "<brief reason>"}}

If something worth storing:
{{"store": {{"topic": "<topic>", "importance": "<importance>",
       "content": "<short summary>", "keywords": ["kw1", "kw2"]}}}}

Topic must be one of: preferences, decisions, errors-resolved, learnings, context
Importance must be one of: critical, high, medium, low
Content should be concise (max 200 chars)."""


@dataclass(frozen=True, slots=True)
class ClassifyTask:
    """A single exchange awaiting classification by the classifier worker.

    Enqueued by ``hooks.submit_triggers`` when ``classifier_enabled`` is true.
    """

    user_text: str
    assistant_text: str
    project: str | None


@dataclass(frozen=True, slots=True)
class ClassifierResult:
    """Parsed output from the classifier LLM."""

    topic: str
    importance: str
    content: str
    keywords: tuple[str, ...] = field(default_factory=tuple)


def classify_exchange(
    user_text: str,
    assistant_text: str,
    *,
    endpoint: str,
    model: str,
    api_key: str = "",
    timeout_s: float = 8.0,
) -> ClassifierResult | None:
    """Call the LLM endpoint and parse the classification result.

    Uses the OpenAI-compatible chat completions format. ``endpoint`` should be
    the full URL (e.g. ``https://api.openai.com/v1/chat/completions``).

    Returns ``None`` when the LLM decides nothing is worth storing, or on
    any network/parse failure (degrade gracefully — never raises).
    """
    user_message = (
        f"Exchange:\n"
        f"User: {user_text[:1500]}\n"
        f"Assistant: {assistant_text[:2000]}"
    )

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "HermesICM/0.4",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        logger.debug(
            "classifier: HTTP %d: %s",
            exc.code,
            exc.reason,
            extra={"endpoint": endpoint, "model": model, "err": repr(exc)},
        )
        return None
    except urllib.error.URLError as exc:
        logger.debug(
            "classifier: endpoint unreachable: %r",
            exc,
            extra={"endpoint": endpoint, "err": repr(exc)},
        )
        return None
    except TimeoutError as exc:
        logger.debug(
            "classifier: timeout after %ss: %r",
            timeout_s,
            exc,
            extra={"endpoint": endpoint, "timeout_s": timeout_s, "err": repr(exc)},
        )
        return None
    except Exception as exc:
        logger.debug(
            "classifier: request failed: %r",
            exc,
            extra={"endpoint": endpoint, "err": repr(exc)},
        )
        return None

    # Parse the OpenAI chat completions response.
    try:
        body = json.loads(raw)
        if "error" in body:
            logger.debug(
                "classifier: API error: %s",
                body["error"],
                extra={"endpoint": endpoint, "model": model},
            )
            return None
        choices = body.get("choices") or []
        if not choices:
            return None
        response_text = (choices[0].get("message") or {}).get("content", "")
        if not response_text:
            return None
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.debug(
            "classifier: unparseable response: %r",
            exc,
            extra={"raw_preview": raw[:200]},
        )
        return None

    # Parse the structured JSON from the LLM's response text.
    # Strip markdown code fences if present.
    clean = response_text.strip()
    if clean.startswith("```"):
        # Remove opening fence (```json, ```, etc.) and closing fence
        first_newline = clean.find("\n")
        if first_newline != -1:
            clean = clean[first_newline + 1 :]
        if clean.endswith("```"):
            clean = clean[:-3].strip()
        elif clean.endswith("``"):
            clean = clean[:-2].strip()

    try:
        decision = json.loads(clean)
    except json.JSONDecodeError:
        logger.debug(
            "classifier: LLM output not JSON: %.200s",
            response_text,
            extra={"raw_preview": response_text[:200]},
        )
        return None

    store = decision.get("store")
    if store is None:
        return None  # LLM decided nothing to store

    topic = store.get("topic", "")
    importance = store.get("importance", "medium")
    content = store.get("content", "")
    keywords = tuple(store.get("keywords", []))

    return ClassifierResult(
        topic=topic,
        importance=importance,
        content=content[:500],  # cap content length
        keywords=keywords[:5],  # cap keywords
    )
