"""Pure append-only rendering and merge for receipt-bound ledger pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from open_brain_engine.core.models import ContentOrigin

from .sanitize import LedgerSection, SanitizedLeaf
from .stage import LedgerStage


class MergeCode(StrEnum):
    CREATED = "created"
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    CITATION_APPENDED = "citation_appended"
    INVALID_INPUT = "invalid_input"
    INVALID_SECTION = "invalid_section"
    INVALID_CITATION = "invalid_citation"
    PROVENANCE_NOT_ELIGIBLE = "provenance_not_eligible"


_CITATION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_HEADINGS: tuple[tuple[LedgerSection, str], ...] = (
    (LedgerSection.SUMMARY, "Summary"),
    (LedgerSection.KEY_POINTS, "Key points"),
    (LedgerSection.CONTEXT, "Context"),
    (LedgerSection.QUESTIONS, "Questions"),
    (LedgerSection.REFERENCES, "References"),
)


@dataclass(frozen=True, slots=True)
class TrustedCitation:
    citation_id: str
    destination: str

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def create(cls, *, citation_id: object, destination: object) -> TrustedCitation:
        return cls(citation_id=citation_id, destination=destination)  # type: ignore[arg-type]

    def validate(self) -> None:
        if (
            not isinstance(self.citation_id, str)
            or not _CITATION_ID.fullmatch(self.citation_id)
            or not isinstance(self.destination, str)
            or not self.destination
            or len(self.destination) > 1024
            or "\\" in self.destination
        ):
            raise ValueError("invalid trusted citation")
        path = PurePosixPath(self.destination)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or any(not _safe_destination_part(part) for part in path.parts)
            or path.as_posix() != self.destination
        ):
            raise ValueError("invalid trusted citation")


@dataclass(frozen=True, slots=True)
class _RenderedLeaf:
    leaf: SanitizedLeaf
    citations: tuple[TrustedCitation, ...]

    def __post_init__(self) -> None:
        self.leaf.validate()
        if not isinstance(self.citations, tuple):
            raise ValueError("invalid rendered leaf")
        for citation in self.citations:
            if not isinstance(citation, TrustedCitation):
                raise ValueError("invalid rendered leaf")
            citation.validate()


@dataclass(frozen=True, slots=True)
class LedgerPage:
    topic_label: str
    provenance_label: str
    sections: tuple[tuple[LedgerSection, tuple[_RenderedLeaf, ...]], ...]

    def render(self) -> str:
        heading_for = dict(_HEADINGS)
        blocks = ["---", f"provenance: {self.provenance_label}", "---", "", f"# {self.topic_label}"]
        for section, leaves in self.sections:
            blocks.extend(("", f"## {heading_for[section]}"))
            blocks.extend(_render_leaf(item) for item in leaves)
        return "\n".join(blocks) + "\n"


@dataclass(frozen=True, slots=True)
class LedgerPageResult:
    page: LedgerPage | None
    code: MergeCode


def create_ledger_page(*, stage: object) -> LedgerPageResult:
    if not isinstance(stage, LedgerStage):
        return LedgerPageResult(page=None, code=MergeCode.INVALID_INPUT)
    stage.validate()
    if stage.binding.provenance.content_origin is not ContentOrigin.THIRD_PARTY:
        return LedgerPageResult(page=None, code=MergeCode.PROVENANCE_NOT_ELIGIBLE)
    if not stage.binding.topic_label:
        return LedgerPageResult(page=None, code=MergeCode.INVALID_INPUT)
    return LedgerPageResult(
        page=LedgerPage(
            topic_label=stage.binding.topic_label,
            provenance_label="unreviewed-third-party",
            sections=tuple((section, ()) for section, _ in _HEADINGS),
        ),
        code=MergeCode.CREATED,
    )


def merge_leaf(
    *, page: object, section: object, leaf: object, citation: object
) -> LedgerPageResult:
    if not isinstance(page, LedgerPage) or not isinstance(leaf, SanitizedLeaf):
        return LedgerPageResult(page=None, code=MergeCode.INVALID_INPUT)
    if not isinstance(section, LedgerSection):
        return LedgerPageResult(page=None, code=MergeCode.INVALID_SECTION)
    if not isinstance(citation, TrustedCitation):
        return LedgerPageResult(page=None, code=MergeCode.INVALID_CITATION)
    try:
        leaf.validate()
    except ValueError:
        return LedgerPageResult(page=None, code=MergeCode.INVALID_INPUT)
    try:
        citation.validate()
    except ValueError:
        return LedgerPageResult(page=None, code=MergeCode.INVALID_CITATION)
    changed_sections: list[tuple[LedgerSection, tuple[_RenderedLeaf, ...]]] = []
    for current_section, items in page.sections:
        if current_section is not section:
            changed_sections.append((current_section, items))
            continue
        for index, item in enumerate(items):
            if item.leaf.normalized_key != leaf.normalized_key:
                continue
            if citation in item.citations:
                return LedgerPageResult(page=page, code=MergeCode.DUPLICATE)
            updated = item.citations + (citation,)
            changed_items = (
                items[:index] + (_RenderedLeaf(item.leaf, updated),) + items[index + 1 :]
            )
            changed_sections.append((current_section, changed_items))
            changed_sections.extend(page.sections[len(changed_sections) :])
            return LedgerPageResult(
                page=LedgerPage(page.topic_label, page.provenance_label, tuple(changed_sections)),
                code=MergeCode.CITATION_APPENDED,
            )
        changed_sections.append((current_section, items + (_RenderedLeaf(leaf, (citation,)),)))
    return LedgerPageResult(
        page=LedgerPage(page.topic_label, page.provenance_label, tuple(changed_sections)),
        code=MergeCode.APPLIED,
    )


def _render_leaf(value: _RenderedLeaf) -> str:
    value.__post_init__()
    citations = "".join(
        f" [{citation.citation_id}](<{citation.destination}>)" for citation in value.citations
    )
    return "- " + value.leaf.text + citations


def _safe_destination_part(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "._-")
            for character in value
        )
    )
