from __future__ import annotations

import json
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureFault,
    DecisionOutcome,
    EnrichmentRequest,
    EnrichmentUnavailable,
    EventPayload,
    FilePayload,
    InjectedFault,
    MeasurementPayload,
    ProposalDraft,
    ReferencePayload,
    TextPayload,
)
from open_brain_engine.providers.base import ProviderMode
from open_brain_engine.storage.filesystem import RootConfinementError
from open_brain_engine.storage.locks import FileLease, LockBusyError
from open_brain_engine.storage.markdown import parse_markdown
from referencing import Registry, Resource

from open_brain.profile import compile_single_user_local

SCHEMA_ROOT = files("open_brain_engine.portable").joinpath("schemas/v1")
_FORMAT_CHECKER = FormatChecker()


def _validator(name: str) -> Draft202012Validator:
    schemas = {
        path.name: cast(dict[str, object], json.loads(path.read_bytes()))
        for path in sorted(SCHEMA_ROOT.iterdir(), key=lambda item: item.name)
        if path.name.endswith(".json")
    }
    registry = Registry().with_resources(
        (str(schema["$id"]), Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Draft202012Validator(schemas[name], registry=registry, format_checker=_FORMAT_CHECKER)


def _engine(
    root: Path,
    *,
    faults: set[CaptureFault] | None = None,
    starter_spaces: tuple[str, ...] = (),
) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root, starter_spaces=starter_spaces),
        faults=faults or set(),
    )


def test_no_model_accepts_every_payload_and_separates_quick_from_canonical(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain", starter_spaces=("Notes",))
    space = engine.inbox.spaces()[0]
    requests = (
        ("text", TextPayload("Synthetic notebook phrase")),
        (
            "reference",
            ReferencePayload("https://example.test/reference", "Synthetic reference"),
        ),
        ("file", FilePayload("sample.txt", "text/plain", b"Synthetic file evidence")),
        (
            "event",
            EventPayload(
                "synthetic.event",
                "2026-08-30T12:00:00Z",
                {"label": "Synthetic"},
            ),
        ),
        (
            "measurement",
            MeasurementPayload(
                "42",
                "count",
                "2026-08-30T12:00:00Z",
                {"label": "Synthetic"},
            ),
        ),
    )

    receipts = [
        engine.capture.accept(payload, delivery_id=f"delivery.{name}") for name, payload in requests
    ]
    canonical = engine.capture.accept(
        TextPayload("Synthetic notebook phrase"),
        delivery_id="delivery.canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )

    assert {item.capture_id for item in engine.inbox.list()} == {
        receipt.capture_id for receipt in receipts
    }
    assert all(receipt.enrichment_state == "pending_enrichment" for receipt in receipts)
    assert canonical.capture_id not in {item.capture_id for item in engine.inbox.list()}
    results = engine.retrieval.search("Synthetic")
    assert results[0].record_type == "canonical"
    assert {result.payload_family for result in results} >= {
        "text",
        "reference_or_file",
        "event",
        "measurement",
    }
    assert len(tuple((tmp_path / "brain" / "sources" / "captures").rglob("*.json"))) == 6


def test_canonical_note_requires_a_portable_space_without_partial_acceptance(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain")

    with pytest.raises(ValueError, match="requires a space"):
        engine.capture.accept(
            TextPayload("Synthetic canonical note"),
            delivery_id="delivery.canonical.unassigned",
            action=CaptureAction.CANONICAL_NOTE,
        )

    assert engine.inbox.list() == ()
    assert tuple((tmp_path / "brain" / "sources" / "captures").rglob("*.json")) == ()


def test_mutation_fails_closed_while_another_canonical_writer_holds_the_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)

    with (
        FileLease(root / ".open-brain", "competing-writer").acquire_shared_writer(),
        pytest.raises(LockBusyError, match="lease already held"),
    ):
        engine.capture.accept(
            TextPayload("Synthetic blocked mutation"),
            delivery_id="delivery.writer.conflict",
        )

    assert engine.inbox.list() == ()


def test_open_engine_rejects_runtime_root_replacement_before_io(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    replacement = tmp_path / "replacement"
    engine = _engine(selected)
    compile_single_user_local(replacement)
    displaced = tmp_path / "displaced-selected"
    selected.rename(displaced)
    replacement.rename(selected)

    with pytest.raises(RootConfinementError, match="identity changed"):
        engine.capture.accept(
            TextPayload("Must not reach replacement root"),
            delivery_id="delivery.root-replaced",
        )

    assert not (selected / ".open-brain/state/phase1.sqlite3").exists()
    assert (displaced / ".open-brain/state/phase1.sqlite3").exists()


def test_third_party_reference_is_retrievable_source_until_approved(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    receipt = engine.capture.accept(
        ReferencePayload("https://example.test/third-party", "Synthetic external article"),
        delivery_id="delivery.third-party",
    )

    results = engine.retrieval.search("external article")

    assert receipt.canonical_path is None
    assert [(result.record_type, result.trust) for result in results] == [("source", "third_party")]
    assert results[0].provenance["capture_id"] == receipt.capture_id


@pytest.mark.parametrize(
    "fault",
    [
        CaptureFault.AFTER_CAPTURE_RESERVATION,
        CaptureFault.AFTER_SOURCE_WRITE,
        CaptureFault.AFTER_AUTOMATIC_PROPOSAL_WRITE,
        CaptureFault.AFTER_AUTOMATIC_DECISION_WRITE,
        CaptureFault.AFTER_CANONICAL_PAGE_WRITE,
        CaptureFault.AFTER_PUBLICATION_WRITE,
        CaptureFault.AFTER_INDEX_UPDATE,
    ],
)
def test_capture_restart_replays_each_durable_transition_once(
    tmp_path: Path, fault: CaptureFault
) -> None:
    root = tmp_path / fault.value
    engine = _engine(root, faults={fault}, starter_spaces=("Notes",))
    space_id = engine.inbox.spaces()[0].space_id

    with pytest.raises(InjectedFault):
        engine.capture.accept(
            TextPayload("Synthetic recovery note"),
            delivery_id="delivery.recovery",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=space_id,
        )

    reopened = _engine(root)
    duplicate = reopened.capture.accept(
        TextPayload("Synthetic recovery note"),
        delivery_id="delivery.recovery",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
    )

    assert duplicate.duplicate is True
    assert duplicate.canonical_path is not None
    assert len(tuple((root / "sources" / "captures").rglob("*.json"))) == 1
    assert len(tuple((root / "history" / "proposals").rglob("*.json"))) == 1
    assert len(tuple((root / "history" / "decisions").rglob("*.json"))) == 1
    assert len(tuple((root / "history" / "publications").rglob("*.json"))) == 1
    assert len(tuple((root / "content" / "spaces").rglob("page_*.md"))) == 1


def test_file_blob_restart_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, faults={CaptureFault.AFTER_BLOB_WRITE})
    payload = FilePayload("fixture.txt", "text/plain", b"Synthetic blob")

    with pytest.raises(InjectedFault):
        engine.capture.accept(payload, delivery_id="delivery.file")

    duplicate = _engine(root).capture.accept(payload, delivery_id="delivery.file")
    assert duplicate.duplicate is True
    assert len(tuple((root / "sources" / "blobs" / "sha256").rglob("*"))) >= 2
    assert len(tuple((root / "sources" / "captures").rglob("*.json"))) == 1


def test_duplicate_delivery_conflict_is_quarantined_without_content(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    engine.capture.accept(TextPayload("Synthetic first"), delivery_id="delivery.conflict")

    with pytest.raises(ValueError, match="conflicting delivery"):
        engine.capture.accept(TextPayload("Synthetic second"), delivery_id="delivery.conflict")

    quarantine = tuple((root / ".open-brain" / "quarantine").glob("*.json"))
    assert len(quarantine) == 1
    rendered = quarantine[0].read_text(encoding="utf-8")
    assert "Synthetic first" not in rendered
    assert "Synthetic second" not in rendered


def test_spaces_rename_and_route_without_changing_identities(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    capture = engine.capture.accept(
        TextPayload("Synthetic space note"), delivery_id="delivery.space.capture"
    )
    space = engine.inbox.create_space("Projects", delivery_id="delivery.space.create")
    renamed = engine.inbox.rename_space(
        space.space_id, "Renamed", delivery_id="delivery.space.rename"
    )

    routed = engine.inbox.route(
        capture.capture_id, space.space_id, delivery_id="delivery.space.route"
    )

    assert capture.capture_id == routed.capture_id
    assert renamed.space_id == space.space_id
    assert renamed.slug == space.slug
    assert engine.inbox.list(unassigned_only=True) == ()
    assert engine.retrieval.search("space note", space_id=space.space_id)[0].capture_id == (
        capture.capture_id
    )
    assert engine.retrieval.search("space note")[0].space_id == space.space_id
    assert len(tuple((root / "content" / "spaces").rglob("_space.md"))) == 1


@pytest.mark.parametrize(
    "fault",
    [CaptureFault.AFTER_SPACE_RESERVATION, CaptureFault.AFTER_SPACE_WRITE],
)
def test_space_creation_recovers_one_stable_identity(tmp_path: Path, fault: CaptureFault) -> None:
    root = tmp_path / fault.value
    engine = _engine(root, faults={fault})

    with pytest.raises(InjectedFault):
        engine.inbox.create_space("Recovery", delivery_id="delivery.space.recovery")

    reopened = _engine(root)
    recovered = reopened.inbox.create_space("Recovery", delivery_id="delivery.space.recovery")
    assert reopened.inbox.spaces() == (recovered,)
    assert len(tuple((root / "content" / "spaces").rglob("_space.md"))) == 1


@pytest.mark.parametrize(
    "fault",
    [CaptureFault.AFTER_ROUTE_RESERVATION, CaptureFault.AFTER_ROUTE_SOURCE_WRITE],
)
def test_space_routing_recovers_without_changing_capture_identity(
    tmp_path: Path, fault: CaptureFault
) -> None:
    root = tmp_path / fault.value
    setup = _engine(root, starter_spaces=("Recovery",))
    space_id = setup.inbox.spaces()[0].space_id
    capture = setup.capture.accept(
        TextPayload("Synthetic routed recovery"), delivery_id="delivery.route.capture"
    )
    engine = _engine(root, faults={fault})

    with pytest.raises(InjectedFault):
        engine.inbox.route(capture.capture_id, space_id, delivery_id="delivery.route.recovery")

    reopened = _engine(root)
    routed = reopened.inbox.route(
        capture.capture_id, space_id, delivery_id="delivery.route.recovery"
    )
    source = next((root / "sources" / "captures").rglob("*.json"))
    routing = json.loads(next((root / "history" / "routes").rglob("*.json")).read_bytes())
    assert routed.capture_id == capture.capture_id
    assert json.loads(source.read_bytes())["space_id"] is None
    assert routing["capture_id"] == capture.capture_id
    assert routing["space_id"] == space_id
    assert routing["receipt"]["kind"] == "routing"
    assert reopened.retrieval.search("routed recovery")[0].space_id == space_id


def test_provider_retry_enriches_once_without_duplicate_capture_or_proposals(
    tmp_path: Path,
) -> None:
    class RecoveringProvider:
        def __init__(self) -> None:
            self.calls = 0

        def enrich(self, request: EnrichmentRequest) -> tuple[ProposalDraft, ...]:
            self.calls += 1
            assert request.payload_family == "text"
            assert request.source_text == "Synthetic pending provider note"
            if self.calls == 1:
                raise RuntimeError("synthetic outage")
            return (ProposalDraft("Provider result", "Synthetic enriched proposal"),)

    class NeverCalledProvider:
        def __init__(self) -> None:
            self.calls = 0

        def enrich(self, _request: EnrichmentRequest) -> tuple[ProposalDraft, ...]:
            self.calls += 1
            raise AssertionError("completed enrichment must not call the provider")

    root = tmp_path / "brain"
    profile = replace(
        compile_single_user_local(root, starter_spaces=("Notes",)),
        provider_mode=ProviderMode.LOCAL,
    )
    provider = RecoveringProvider()
    engine = BrainEngine.open(profile, enrichment_provider=provider)
    space_id = engine.inbox.spaces()[0].space_id
    capture = engine.capture.accept(
        TextPayload("Synthetic pending provider note"),
        delivery_id="delivery.provider.capture",
        space_id=space_id,
    )

    with pytest.raises(EnrichmentUnavailable, match="provider unavailable"):
        engine.capture.retry_enrichment(
            capture.capture_id,
            delivery_id="delivery.provider.retry",
        )
    assert engine.capture.get(capture.capture_id).enrichment_state == "pending_enrichment"  # type: ignore[union-attr]
    assert tuple((root / "history" / "proposals").rglob("*.json")) == ()
    first = engine.capture.retry_enrichment(
        capture.capture_id,
        delivery_id="delivery.provider.retry",
    )
    never_called = NeverCalledProvider()
    replay = BrainEngine.open(profile, enrichment_provider=never_called).capture.retry_enrichment(
        capture.capture_id,
        delivery_id="delivery.provider.retry",
    )

    assert first == replay
    assert provider.calls == 2
    assert never_called.calls == 0
    assert engine.capture.get(capture.capture_id).enrichment_state == "enriched"  # type: ignore[union-attr]
    assert len(tuple((root / "history" / "proposals").rglob("*.json"))) == 1
    assert len(tuple((root / "sources" / "captures").rglob("*.json"))) == 1


@pytest.mark.parametrize(
    "fault",
    [CaptureFault.AFTER_PROPOSAL_RESERVATION, CaptureFault.AFTER_PROPOSAL_WRITE],
)
def test_sibling_proposal_set_recovers_without_duplicate_outputs(
    tmp_path: Path, fault: CaptureFault
) -> None:
    root = tmp_path / fault.value
    setup = _engine(root, starter_spaces=("Recovery",))
    capture = setup.capture.accept(
        TextPayload("Synthetic proposal recovery"),
        delivery_id="delivery.proposal.capture",
        space_id=setup.inbox.spaces()[0].space_id,
    )
    drafts = (
        ProposalDraft("One", "Synthetic one"),
        ProposalDraft("Two", "Synthetic two"),
    )
    engine = _engine(root, faults={fault})

    with pytest.raises(InjectedFault):
        engine.review.propose(capture.capture_id, drafts, delivery_id="delivery.proposal.recovery")

    reopened = _engine(root)
    recovered = reopened.review.propose(
        capture.capture_id, drafts, delivery_id="delivery.proposal.recovery"
    )
    assert len(recovered) == 2
    assert len({proposal.proposal_id for proposal in recovered}) == 2
    assert len(tuple((root / "history" / "proposals").rglob("*.json"))) == 2


@pytest.mark.parametrize(
    "fault",
    [
        CaptureFault.AFTER_DECISION_RESERVATION,
        CaptureFault.AFTER_DECISION_WRITE,
        CaptureFault.AFTER_REVIEW_PAGE_WRITE,
        CaptureFault.AFTER_REVIEW_PUBLICATION_WRITE,
    ],
)
def test_terminal_decision_recovers_one_publication(tmp_path: Path, fault: CaptureFault) -> None:
    root = tmp_path / fault.value
    setup = _engine(root, starter_spaces=("Recovery",))
    capture = setup.capture.accept(
        TextPayload("Synthetic decision recovery"),
        delivery_id="delivery.decision.capture",
        space_id=setup.inbox.spaces()[0].space_id,
    )
    proposal = setup.review.propose(
        capture.capture_id,
        (ProposalDraft("Recovery", "Synthetic decision output"),),
        delivery_id="delivery.decision.proposal",
    )[0]
    engine = _engine(root, faults={fault})

    with pytest.raises(InjectedFault):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.decision.recovery",
        )

    reopened = _engine(root)
    recovered = reopened.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.decision.recovery",
    )
    assert recovered.duplicate is True
    assert recovered.publication_id is not None
    assert len(tuple((root / "history" / "decisions").rglob("*.json"))) == 1
    assert len(tuple((root / "history" / "publications").rglob("*.json"))) == 1
    assert len(tuple((root / "content" / "spaces").rglob("page_*.md"))) == 1


def test_sibling_proposals_have_independent_terminal_results(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, starter_spaces=("Notes",))
    space_id = engine.inbox.spaces()[0].space_id
    capture = engine.capture.accept(
        TextPayload("Synthetic multi-meaning capture"),
        delivery_id="delivery.review.capture",
        space_id=space_id,
    )
    proposals = engine.review.propose(
        capture.capture_id,
        (
            ProposalDraft("First meaning", "Synthetic first meaning"),
            ProposalDraft("Second meaning", "Synthetic second meaning"),
            ProposalDraft("Third meaning", "Synthetic third meaning"),
        ),
        delivery_id="delivery.review.proposals",
    )

    approved = engine.review.decide(
        proposals[0].proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.review.approve",
    )
    rejected = engine.review.decide(
        proposals[1].proposal_id,
        DecisionOutcome.REJECTED,
        delivery_id="delivery.review.reject",
    )
    edited = engine.review.decide(
        proposals[2].proposal_id,
        DecisionOutcome.EDITED,
        delivery_id="delivery.review.edit",
        edited_markdown="Synthetic safely edited meaning",
    )
    replay = _engine(root).review.decide(
        proposals[0].proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.review.approve.replay",
    )

    assert approved.outcome is DecisionOutcome.APPROVED
    assert rejected.outcome is DecisionOutcome.REJECTED
    assert rejected.publication_id is None
    assert edited.outcome is DecisionOutcome.EDITED
    assert replay.decision_id == approved.decision_id
    assert replay.duplicate is True
    states = {item.proposal_id: item.status for item in engine.review.list()}
    assert states == {
        proposals[0].proposal_id: "approved",
        proposals[1].proposal_id: "rejected",
        proposals[2].proposal_id: "edited",
    }
    assert len(tuple((root / "history" / "decisions").rglob("*.json"))) == 3
    assert len(tuple((root / "history" / "publications").rglob("*.json"))) == 2
    canonical_results = engine.retrieval.search("meaning", record_type="canonical")
    assert {result.trust for result in canonical_results} == {"reviewed"}


def test_retrieval_is_exact_lexical_typed_space_scoped_and_fresh_after_reconciliation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, starter_spaces=("Alpha", "Beta"))
    alpha, beta = engine.inbox.spaces()
    first = engine.capture.accept(
        TextPayload("Exact synthetic phrase and lexical comet"),
        delivery_id="delivery.search.first",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=alpha.space_id,
    )
    engine.capture.accept(
        EventPayload("search.fixture", None, {"token": "lexical comet"}),
        delivery_id="delivery.search.event",
        space_id=beta.space_id,
    )

    exact = engine.retrieval.search("Exact synthetic phrase", record_type="canonical")
    lexical = engine.retrieval.search("lexical comet")
    typed = engine.retrieval.search("lexical", payload_family="event")
    scoped = engine.retrieval.search("lexical", space_id=beta.space_id)

    assert exact[0].capture_id == first.capture_id
    assert {result.record_type for result in lexical} == {"canonical", "source"}
    assert [result.payload_family for result in typed] == ["event"]
    assert {result.space_id for result in scoped} == {beta.space_id}
    assert all(result.provenance["capture_id"] for result in lexical)
    assert all(result.explanation for result in lexical)

    assert first.canonical_path == first.capture_id
    page = next((root / "content" / "spaces").rglob("page_*.md"))
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "Exact synthetic phrase and lexical comet", "Fresh owner edit token"
        ),
        encoding="utf-8",
    )
    assert engine.retrieval.search("Fresh owner edit token", record_type="canonical") == ()

    receipt = engine.reconciliation.reconcile()

    assert receipt.page_updates == 1
    assert engine.retrieval.search(
        "Fresh owner edit token", record_type="canonical"
    )[0].capture_id == first.capture_id
    assert engine.retrieval.search(
        "Exact synthetic phrase", record_type="canonical"
    ) == ()


def test_generated_source_records_are_canonical_json(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    engine.capture.accept(TextPayload("Synthetic canonical JSON"), delivery_id="delivery.json")

    source = next((root / "sources" / "captures").rglob("*.json"))
    value = json.loads(source.read_bytes())
    assert source.read_bytes() == json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def test_phase1_portable_records_validate_against_published_v1_schemas(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, starter_spaces=("Schema",))
    space_id = engine.inbox.spaces()[0].space_id
    canonical = engine.capture.accept(
        TextPayload("Synthetic schema publication"),
        delivery_id="delivery.schema.canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
    )
    source = engine.capture.accept(
        ReferencePayload("https://example.test/schema", "Synthetic schema source"),
        delivery_id="delivery.schema.source",
        space_id=space_id,
    )
    proposal = engine.review.propose(
        source.capture_id,
        (ProposalDraft("Reviewed schema", "Synthetic reviewed schema"),),
        delivery_id="delivery.schema.proposal",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.schema.decision",
    )

    for directory, schema_name in (
        (root / "sources" / "captures", "capture.json"),
        (root / "history" / "proposals", "proposal.json"),
        (root / "history" / "decisions", "decision.json"),
        (root / "history" / "publications", "publication.json"),
    ):
        validator = _validator(schema_name)
        for path in directory.rglob("*.json"):
            errors = sorted(validator.iter_errors(json.loads(path.read_bytes())), key=str)
            assert not errors, "\n".join(error.message for error in errors)

    space_validator = _validator("space-frontmatter.json")
    for path in (root / "content" / "spaces").rglob("_space.md"):
        fields = dict(parse_markdown(path.read_bytes()).fields)
        errors = sorted(space_validator.iter_errors(fields), key=str)
        assert not errors, "\n".join(error.message for error in errors)
    page_validator = _validator("canonical-page-frontmatter.json")
    for path in (root / "content" / "spaces").rglob("page_*.md"):
        fields = dict(parse_markdown(path.read_bytes()).fields)
        errors = sorted(page_validator.iter_errors(fields), key=str)
        assert not errors, "\n".join(error.message for error in errors)
    assert canonical.canonical_path is not None
