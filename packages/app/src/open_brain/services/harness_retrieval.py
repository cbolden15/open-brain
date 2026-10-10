"""Bounded local task planning and source-grounded decision currentness.

This is deliberately lexical English retrieval, not inference or semantic search.
Aliases identify task domains, never add facts or broaden a serving authority.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from open_brain_engine.engine.harness_memory import HarnessMemory

_WORDS = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "should",
        "will",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "from",
        "with",
        "and",
        "or",
        "as",
        "by",
        "what",
        "which",
        "who",
        "where",
        "when",
        "how",
        "why",
        "please",
        "help",
        "tell",
        "explain",
        "something",
        "up",
        "set",
        "plan",
        "planning",
        "use",
        "using",
        "need",
        "want",
        "make",
    ]
)
_ALIASES = (
    (
        frozenset(
            [
                "meal",
                "meals",
                "dinner",
                "lunch",
                "breakfast",
                "supper",
                "cook",
                "cooking",
                "recipe",
                "recipes",
            ]
        ),
        ("ingredients", "directions", "meal", "meals", "allergic"),
    ),
    (frozenset(["editor", "editors", "editing"]), ("editor",)),
    (frozenset(["reside", "residing", "residence", "hometown", "home", "living"]), ("live",)),
    (frozenset(["employment", "employer", "occupation", "job", "workplace"]), ("work",)),
    (
        frozenset(
            ["choose", "choice", "chosen", "chose", "decided", "decision", "database", "storage"]
        ),
        ("decided", "decision"),
    ),
    (
        frozenset(["favorite", "favourite", "preference", "preferences", "prefer", "preferred"]),
        ("prefer", "preferred"),
    ),
)
_CHANGE = re.compile(
    r"^(?:(?:I|We) changed (?:my|our) decision\b"
    r"|(?:My|Our|The) decision is now\b"
    r"|(?:(?:I|We) (?:have )?(?:decided|chose)|(?:My|Our|The) decision is)\b"
    r".*\binstead of\b)",
    re.I,
)
_SUBJECT = re.compile(r"\b(?:for|about|regarding)\s+(.+?)[.!]?\s*$", re.I)


def words(text: str) -> tuple[str, ...]:
    return tuple(word.casefold() for word in _WORDS.findall(text))


def is_explicit_change(text: str) -> bool:
    return _CHANGE.match(text) is not None


@dataclass(frozen=True, slots=True)
class TaskQuery:
    primary: tuple[str, ...]
    aliases: tuple[str, ...]

    @property
    def search_text(self) -> str:
        return " ".join(dict.fromkeys((*self.primary, *self.aliases)))

    def relevance(self, memory: HarnessMemory) -> int:
        tokens = frozenset(words(memory.text))
        return 4 * len(tokens.intersection(self.primary)) + len(tokens.intersection(self.aliases))


def plan_task_query(query: str) -> TaskQuery:
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("invalid_query")
    tokens = words(query)
    primary = tuple(dict.fromkeys(word for word in tokens if word not in _STOP))[:32]
    token_set = frozenset(tokens)
    alias_groups = [
        expansions for triggers, expansions in _ALIASES if token_set.intersection(triggers)
    ]
    if any(group != _ALIASES[-1][1] for group in alias_groups):
        alias_groups = [group for group in alias_groups if group != _ALIASES[-1][1]]
    aliases = tuple(dict.fromkeys(alias for expansions in alias_groups for alias in expansions))
    result = TaskQuery(primary, aliases)
    if len(result.search_text) > 500:
        raise ValueError("invalid_query")
    return result


def _subject(memory: HarnessMemory) -> tuple[str, ...]:
    match = _SUBJECT.search(memory.text)
    return tuple(word for word in words(match[1]) if word not in _STOP) if match else ()


def decision_state(memory: HarnessMemory, changes: Sequence[HarnessMemory]) -> str:
    """Withhold superseded/ambiguous decisions, preserving immutable source reads.

    Only an explicit authored change in the same project is evidence. Same-session
    ordinals or timezone-aware source timestamps prove ordering. Named subjects must
    agree; an unnamed same-session change conservatively withholds earlier decisions.
    Across sessions without timestamps a matching named change makes currentness
    ambiguous rather than inventing an ordering from delivery time.
    """
    if memory.kind != "decision":
        return "current"
    subject = _subject(memory)
    uncertain = False
    for change in changes:
        if change == memory or change.kind != "decision" or not is_explicit_change(change.text):
            continue
        if change.citation.project_id != memory.citation.project_id:
            continue
        changed_subject = _subject(change)
        same_session = (
            change.citation.harness == memory.citation.harness
            and change.citation.session_id == memory.citation.session_id
        )
        if subject and changed_subject and subject != changed_subject:
            continue
        if not same_session and (not subject or not changed_subject):
            continue
        if same_session:
            later = change.citation.message_ordinal > memory.citation.message_ordinal
        elif change.citation.timestamp is not None and memory.citation.timestamp is not None:
            later = datetime.fromisoformat(change.citation.timestamp) > datetime.fromisoformat(
                memory.citation.timestamp
            )
        else:
            uncertain = True
            continue
        if later:
            return "superseded"
    return "currentness_unknown" if uncertain else "current"
