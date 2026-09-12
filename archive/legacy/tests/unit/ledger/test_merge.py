from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from open_brain_engine.core.models import ContentOrigin, PrivacyTier

from open_brain_legacy.ledger.merge import (
    MergeCode,
    TrustedCitation,
    create_ledger_page,
    merge_leaf,
)
from open_brain_legacy.ledger.models import LedgerRoute, LedgerValidationError
from open_brain_legacy.ledger.sanitize import LedgerSection, SanitizedLeaf, sanitize_leaf
from open_brain_legacy.ledger.scan import scan_distillation_work_item
from open_brain_legacy.ledger.stage import LedgerStage, stage_scan_record

from .test_stage import _record


def _stage() -> LedgerStage:
    from .test_scan import _taxonomy

    return stage_scan_record(record=_record(), taxonomy=_taxonomy())


def _leaf(text: str, section: LedgerSection = LedgerSection.SUMMARY) -> SanitizedLeaf:
    result = sanitize_leaf(item_id="item-synthetic", section=section, text=text)
    assert result.leaf is not None
    return result.leaf


def test_merge_renders_only_the_fixed_five_headings_and_escapes_model_heading_text() -> None:
    page_result = create_ledger_page(stage=_stage())
    assert page_result.page is not None

    merge_result = merge_leaf(
        page=page_result.page,
        section=LedgerSection.SUMMARY,
        leaf=_leaf("# untrusted heading"),
        citation=TrustedCitation.create(
            citation_id="cite-synthetic", destination="references/synthetic.md"
        ),
    )

    assert merge_result.code is MergeCode.APPLIED
    assert merge_result.page is not None
    rendered = merge_result.page.render()
    assert rendered.count("\n## ") == 5
    assert "## untrusted heading" not in rendered
    assert "\\# untrusted heading" in rendered


def test_merge_deduplicates_normalized_leaves_within_one_section_only() -> None:
    page_result = create_ledger_page(stage=_stage())
    assert page_result.page is not None
    citation = TrustedCitation.create(
        citation_id="cite-synthetic", destination="references/synthetic.md"
    )

    first = merge_leaf(
        page=page_result.page,
        section=LedgerSection.SUMMARY,
        leaf=_leaf("Caf\u00e9 finding."),
        citation=citation,
    )
    assert first.page is not None
    duplicate = merge_leaf(
        page=first.page,
        section=LedgerSection.SUMMARY,
        leaf=_leaf("  CAFE\u0301   FINDING   "),
        citation=citation,
    )
    assert duplicate.code is MergeCode.DUPLICATE
    assert duplicate.page is not None
    cross_section = merge_leaf(
        page=duplicate.page,
        section=LedgerSection.QUESTIONS,
        leaf=_leaf("caf\u00e9 finding", LedgerSection.QUESTIONS),
        citation=citation,
    )

    assert cross_section.page is not None
    rendered = cross_section.page.render()
    assert rendered.count("Caf\u00e9 finding") == 1
    assert rendered.lower().count("caf\u00e9 finding") == 2


def test_merge_appends_each_new_trusted_citation_once_without_rendering_forged_paths() -> None:
    page_result = create_ledger_page(stage=_stage())
    assert page_result.page is not None
    leaf = _leaf("Synthetic finding")
    first_citation = TrustedCitation.create(citation_id="cite-one", destination="references/one.md")
    second_citation = TrustedCitation.create(
        citation_id="cite-two", destination="references/two.md"
    )
    first = merge_leaf(
        page=page_result.page,
        section=LedgerSection.SUMMARY,
        leaf=leaf,
        citation=first_citation,
    )
    assert first.page is not None
    second = merge_leaf(
        page=first.page,
        section=LedgerSection.SUMMARY,
        leaf=leaf,
        citation=second_citation,
    )
    assert second.code is MergeCode.CITATION_APPENDED
    assert second.page is not None
    repeat = merge_leaf(
        page=second.page,
        section=LedgerSection.SUMMARY,
        leaf=leaf,
        citation=second_citation,
    )

    assert repeat.code is MergeCode.DUPLICATE
    assert repeat.page is not None
    rendered = repeat.page.render()
    assert rendered.count("Synthetic finding") == 1
    assert rendered.count("<references/two.md>") == 1
    forged_destination = ".." + "/forged"
    with pytest.raises(ValueError, match="invalid trusted citation"):
        TrustedCitation.create(citation_id="cite-forged", destination=forged_destination)

    assert forged_destination not in rendered


@pytest.mark.parametrize(
    "origin",
    (ContentOrigin.OWNER_AUTHORED, ContentOrigin.MIXED, ContentOrigin.UNKNOWN),
)
def test_merge_rejects_non_third_party_provenance_with_a_closed_code(
    origin: ContentOrigin,
) -> None:
    from .test_scan import _event, _item, _taxonomy

    event = _event(origin=origin)
    ineligible = stage_scan_record(
        record=scan_distillation_work_item(
            item=_item(event),
            event=event,
            taxonomy=_taxonomy(),
            source_locator=PurePosixPath("professional/research/note"),
        ),
        taxonomy=_taxonomy(),
    )

    result = create_ledger_page(stage=ineligible)

    assert result.page is None
    assert result.code is MergeCode.PROVENANCE_NOT_ELIGIBLE


def test_merge_uses_the_exact_fixed_third_party_provenance_label() -> None:
    result = create_ledger_page(stage=_stage())

    assert result.page is not None
    assert "provenance: unreviewed-third-party" in result.page.render()


def test_direct_trusted_citation_construction_cannot_bypass_destination_validation() -> None:
    forged_destination = ".." + "/forged"

    with pytest.raises(ValueError, match="invalid trusted citation"):
        TrustedCitation(citation_id="cite-forged", destination=forged_destination)


def test_merge_revalidates_leaf_and_citation_at_its_boundary() -> None:
    page_result = create_ledger_page(stage=_stage())
    assert page_result.page is not None
    leaf = _leaf("Synthetic finding")
    citation = TrustedCitation.create(
        citation_id="cite-synthetic", destination="references/synthetic.md"
    )
    object.__setattr__(leaf, "text", "safe first line\n## forged heading")
    object.__setattr__(citation, "destination", ".." + "/forged")

    result = merge_leaf(
        page=page_result.page,
        section=LedgerSection.SUMMARY,
        leaf=leaf,
        citation=citation,
    )

    assert result.page is None
    assert result.code is MergeCode.INVALID_INPUT


@pytest.mark.parametrize(
    "unsafe_character",
    ("\u0085", "\u2028", "\u2029", "\u200b", "\ud800"),
)
def test_direct_taxonomy_value_cannot_forge_a_structural_heading(
    unsafe_character: str,
) -> None:
    with pytest.raises(LedgerValidationError, match="invalid ledger topic label"):
        LedgerRoute(
            path_prefix=("safe",),
            topic_id="research",
            topic_label=f"Safe{unsafe_character}forged",
            privacy_tier=PrivacyTier.WORK,
        )
