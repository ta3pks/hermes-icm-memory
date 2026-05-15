"""Trigger detection for ``sync_turn`` (FR14, FR16, AD-17, AD-20).

Pure heuristics — no I/O, no logging, no dependencies on cli_runner / provider.
The :data:`MAPPING` dict literal locks the FR16 matrix (category → topic, importance);
:func:`detect_triggers` is the single entry point that ``hooks.sync_turn`` (S08)
calls each turn to decide which ICM writes to enqueue.

A "trigger" is a 4-tuple ``(topic, importance, content, keywords)`` ready to be
shaped into a write task by the caller. Multiple triggers may fire from one turn
(e.g. an assistant message that both fixes a bug and records a decision); the
caller is responsible for deduplication if any.
"""

from __future__ import annotations

import re
from typing import Final

# (topic, importance, content, keywords)
Trigger = tuple[str, str, str, list[str]]

#: FR16 topic ↔ importance matrix. Frozen public surface (NFR-MAINT-1).
MAPPING: Final[dict[str, dict[str, str]]] = {
    "decisions": {"topic_template": "decisions-{project}", "importance": "high"},
    "errors-resolved": {"topic_template": "errors-resolved", "importance": "high"},
    "preferences": {"topic_template": "preferences", "importance": "critical"},
    "context": {"topic_template": "context-{project}", "importance": "high"},
    "learnings": {"topic_template": "learnings", "importance": "high"},
}

_DEFAULT_PROJECT: Final[str] = "default"
_CONTENT_LIMIT: Final[int] = 500
_KEYWORDS_LIMIT: Final[int] = 5

# Compiled patterns. Word boundaries (\b) keep partial matches like "fixedly" out.
# All searched case-insensitively.
_ERRORS_RESOLVED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(fixed|resolved|the bug was|root cause|fix(?:ed)? it|"
    r"the issue was|that explains|the problem was|caused by|"
    r"turns? out the|was due to|reason it (?:failed|broke|didn'?t))",
    re.IGNORECASE,
)
_DECISIONS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(decided to|going with|we'll use|let's use|chose to|"
    r"let'?s go with|we should use|we should go|"
    r"I'm going to use|the approach is)",
    re.IGNORECASE,
)
_PREFERENCES_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(always (?:use|do)|never (?:use|do)|prefer|"
    r"I (?:like|love|hate|can'?t stand) when|"
    r"my (?:favourite|favorite|preferred)|"
    r"don'?t (?:like|use|do)|won'?t use)",
    re.IGNORECASE,
)
_LEARNINGS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(learned|turns out|TIL|now I understand|"
    r"interestingly|actually it (?:works|seems|turns)|"
    r"the key insight|what I didn'?t know|"
    r"it (?:turns|turned) out that)",
    re.IGNORECASE,
)

# Word tokenizer for keywords extraction. Lowercase a-z plus digits, length ≥ 3.
_WORD_TOKEN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]{3,}")


def _resolve_topic(category: str, project: str | None) -> str:
    """Format the category's topic_template, substituting 'default' for None."""
    template = MAPPING[category]["topic_template"]
    return template.format(project=project or _DEFAULT_PROJECT)


def _extract_keywords(text: str) -> list[str]:
    """Lower-cased word tokens, deduped, first-seen order, capped at ``_KEYWORDS_LIMIT``."""
    seen: set[str] = set()
    out: list[str] = []
    for token in _WORD_TOKEN.findall(text.lower()):
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= _KEYWORDS_LIMIT:
            break
    return out


def detect_triggers(
    user_text: str,
    assistant_text: str,
    project: str | None = None,
    turn_index: int = 0,
    every_n_turns: int = 20,
) -> list[Trigger]:
    """Detect store triggers in a single turn.

    :param user_text: User message that opened the turn.
    :param assistant_text: Assistant message that closed the turn.
    :param project: Project name for ``{project}``-templated topics. ``None`` →
        ``"default"`` (per AC8).
    :param turn_index: Per-session turn counter, owned by the provider.
    :param every_n_turns: Periodic-progress cadence (AD-20). Default 20.
    :returns: List of ``(topic, importance, content, keywords)`` tuples. Empty
        list when no pattern matches and no periodic boundary is hit. Never
        returns ``None``.

    Order of emitted tuples (deterministic for tests / FIFO writes):

    1. periodic context (if applicable)
    2. errors-resolved
    3. decisions
    4. learnings
    5. preferences
    """
    out: list[Trigger] = []

    # 1. Periodic context — fires every N turns, never on turn_index == 0.
    if every_n_turns > 0 and turn_index > 0 and turn_index % every_n_turns == 0:
        topic = _resolve_topic("context", project)
        content = f"periodic progress checkpoint: turn {turn_index}"
        out.append((topic, MAPPING["context"]["importance"], content, []))

    # 2. errors-resolved (assistant_text).
    if _ERRORS_RESOLVED_PATTERN.search(assistant_text):
        out.append(
            (
                _resolve_topic("errors-resolved", project),
                MAPPING["errors-resolved"]["importance"],
                assistant_text[:_CONTENT_LIMIT],
                _extract_keywords(assistant_text),
            )
        )

    # 3. decisions (assistant_text).
    if _DECISIONS_PATTERN.search(assistant_text):
        out.append(
            (
                _resolve_topic("decisions", project),
                MAPPING["decisions"]["importance"],
                assistant_text[:_CONTENT_LIMIT],
                _extract_keywords(assistant_text),
            )
        )

    # 4. learnings (assistant_text).
    if _LEARNINGS_PATTERN.search(assistant_text):
        out.append(
            (
                _resolve_topic("learnings", project),
                MAPPING["learnings"]["importance"],
                assistant_text[:_CONTENT_LIMIT],
                _extract_keywords(assistant_text),
            )
        )

    # 5. preferences (user_text only — see story spec rationale).
    if _PREFERENCES_PATTERN.search(user_text):
        out.append(
            (
                _resolve_topic("preferences", project),
                MAPPING["preferences"]["importance"],
                user_text[:_CONTENT_LIMIT],
                _extract_keywords(user_text),
            )
        )

    return out
