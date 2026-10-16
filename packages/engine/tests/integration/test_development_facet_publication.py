"""Development confirmations retain facet selection and portable history semantics."""

import json
from pathlib import Path

from open_brain_engine.engine.contracts import DecisionOutcome
from open_brain_engine.portable.versioned import validate_portable_root

from packages.engine.tests.contract.test_portable_brain_v10 import (
    _real_facet_proposal,
    _real_publication_brain,
)


def test_development_facet_confirmation_publishes_and_exports(tmp_path: Path) -> None:
    engine, owner, query, _value, captures, destination = _real_publication_brain(tmp_path)
    first, second = captures[:2]
    engine.developments.bind(
        first, (second,), operation_id="development.bind", authority=owner
    )
    proposal = _real_facet_proposal(engine, owner, query, destination)
    inspection = engine.review.show(proposal["proposal_id"])
    assert inspection.facet_binding_json is not None
    binding = json.loads(inspection.facet_binding_json)
    assert binding["schema_version"] == 6
    assert binding["base_schema_version"] == 5
    assert len(engine.review.list(facet_query_id=query)) == 1
    engine.review.decide(
        inspection.proposal_id, DecisionOutcome.APPROVED, delivery_id="approve",
        expected_review_digest=inspection.review_digest,
    )
    exported = tmp_path / "exported"
    engine.portability.export(
        exported, export_id="export_2b5f2d12-59ee-4f04-9a8b-7f6bbd9c1bd4"
    )
    assert validate_portable_root(exported)["schema_version"] == 14
