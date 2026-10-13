"""Pure, conservative extraction from authored harness messages.

Selection is evidence of authorship, not factual verification, privacy classification,
endorsement, or a current-decision projection. Callers retain message context and apply
policy and credential screening before persistence. Unsupported wording is omitted.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

MAX_MESSAGES = 256
MAX_INPUT_BYTES = 128 * 1024
MAX_MESSAGE_BYTES = 16 * 1024
MAX_CANDIDATE_BYTES = 8 * 1024
MAX_TOTAL_BYTES = 16 * 1024
MAX_CANDIDATES = 32

_PREFERENCE = re.compile(
    r"^(?:I (?:prefer|dislike)\b|My preference is\b"
    r"|My (?:preferred|favorite) [\w -]{1,60} (?:is|are)\b)",
    re.I,
)
_DECISION = re.compile(
    r"^(?:(?:I|We) (?:have )?(?:decided|chose|changed (?:my|our) decision)\b"
    r"|(?:My|Our|The) decision is\b)",
    re.I,
)
_FACT = re.compile(
    r"^(?:I (?:live (?:in|at)|work (?:at|in|as|for)|am allergic to|have an? )\b"
    r"|My [\w -]{1,60} (?:is|are)\b"
    r"|(?:The |Our )[\w -]{1,60} (?:uses|requires|supports|runs on)\b)",
    re.I,
)
_UNCERTAIN = re.compile(r"\b(?:maybe|might|probably|perhaps|guess|hypothetically)\b", re.I)
_RECIPE_REQUEST = re.compile(
    r"^(?:please\s+)?(?:give me|write|create|provide|suggest|show me)\b.*\brecipe\b"
    r"|^(?:can|could|would) you\b.*\b(?:give|write|create|provide|suggest|show)\b"
    r".*\brecipe\b",
    re.I,
)
_INGREDIENTS = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?ingredients(?:\*\*)?\s*:?\s*$", re.I)
_DIRECTIONS = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:directions|instructions|method|steps)"
    r"(?:\*\*)?\s*:?\s*$",
    re.I,
)
_QUANTITY = re.compile(r"(?:\d|[¼½¾])\s*(?:[\d/.,]+\s*)?\w")
_STEP = re.compile(r"^\s*\d+[.)]\s+\S")


@dataclass(frozen=True, slots=True)
class MemoryMessage:
    role: str
    text: str
    ordinal: int
    authored: bool = True
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    kind: Literal["fact", "decision", "preference", "material"]
    text: str
    ordinal: int
    role: str
    source_excerpt: str
    source_id: str | None = None


def select_memories(messages: Sequence[MemoryMessage]) -> tuple[MemoryCandidate, ...]:
    """Extract conservative English statements and explicitly requested recipes.

    Input order is chronological; ordinal is the caller's original message locator.
    All selected messages are examined, up to the public input bounds above (not a
    recent-message window). Oversized messages/candidates are omitted, never truncated.
    Quoted lines and fenced code are excluded. Recipe recognition requires ingredients
    with quantities and numbered directions, and retains the entire response.

    This deliberately misses unsupported syntax/languages and unstructured material.
    Changed decisions remain historical statements; a caller must resolve currentness
    using retained context. No privacy grant, secret-safety guarantee, or verification
    is inferred. Generated/injected messages must be marked authored=False by adapters.
    """
    candidates: list[MemoryCandidate] = []
    input_bytes = 0
    output_bytes = 0
    recipe_requested = False
    excerpts: list[tuple[Literal["fact", "decision", "preference", "material"], str]]
    for message in messages[:MAX_MESSAGES]:
        size = len(message.text.encode("utf-8"))
        input_bytes += size
        if input_bytes > MAX_INPUT_BYTES:
            break
        if not message.authored:
            continue
        if message.role == "user":
            # Any new authored user turn replaces the preceding material request.
            recipe_requested = bool(_RECIPE_REQUEST.search(" ".join(message.text.split())))
        if size > MAX_MESSAGE_BYTES:
            recipe_requested = False
            continue
        if message.role == "user":
            excerpts = (
                [("material", message.text)]
                if _complete_recipe(message.text)
                else _user_statements(message.text)
            )
        elif message.role == "assistant" and recipe_requested:
            excerpts = [("material", message.text)] if _complete_recipe(message.text) else []
            recipe_requested = False
        else:
            continue
        for kind, excerpt in excerpts:
            text = excerpt.strip() if kind == "material" else " ".join(excerpt.split())
            candidate_bytes = len(text.encode("utf-8"))
            if (
                candidate_bytes > MAX_CANDIDATE_BYTES
                or len(excerpt.encode("utf-8")) > MAX_CANDIDATE_BYTES
            ):
                continue
            if (
                len(candidates) >= MAX_CANDIDATES
                or output_bytes + candidate_bytes > MAX_TOTAL_BYTES
            ):
                return tuple(candidates)
            candidates.append(
                MemoryCandidate(
                    kind, text, message.ordinal, message.role, excerpt, message.source_id
                )
            )
            output_bytes += candidate_bytes
    return tuple(candidates)


def _user_statements(
    text: str,
) -> list[tuple[Literal["fact", "decision", "preference", "material"], str]]:
    statements: list[tuple[Literal["fact", "decision", "preference", "material"], str]] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith((">", '"', "'")):
            continue
        for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            excerpt = part.strip()
            if not excerpt or "?" in excerpt or _UNCERTAIN.search(excerpt):
                continue
            normalized = " ".join(excerpt.split())
            if _has_value(_PREFERENCE, normalized):
                statements.append(("preference", excerpt))
            elif _has_value(_DECISION, normalized):
                statements.append(("decision", excerpt))
            elif _has_value(_FACT, normalized):
                statements.append(("fact", excerpt))
    return statements


def _has_value(pattern: re.Pattern[str], text: str) -> bool:
    match = pattern.match(text)
    return match is not None and any(
        word.lower() not in {"a", "an", "the", "to", "is", "are", "on", "in", "at", "for"}
        for word in re.findall(r"\w+", text[match.end() :])
    )


def _complete_recipe(text: str) -> bool:
    lines = text.splitlines()
    ingredients = next((i for i, line in enumerate(lines) if _INGREDIENTS.fullmatch(line)), None)
    directions = next((i for i, line in enumerate(lines) if _DIRECTIONS.fullmatch(line)), None)
    if ingredients is None or directions is None or ingredients >= directions:
        return False
    return (
        sum(bool(_QUANTITY.search(line)) for line in lines[ingredients + 1 : directions]) >= 2
        and sum(bool(_STEP.search(line)) for line in lines[directions + 1 :]) >= 2
    )
