from __future__ import annotations

import pytest
from open_brain_engine.engine import ContentOrigin, PrivacyDecision, Provenance, ReferencePayload

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey


def test_source_record_intake_derives_stable_third_party_capture_kwargs() -> None:
    record = SourceRecordIntake(
        key=SourceRecordKey(
            connector_name="github",
            connection_id="account:open-brain-test",
            resource_id="repo:cbolden15/open-brain-fixture",
            external_id="issue:42",
            revision_id="updated_at:2026-09-14T12:00:00Z",
        ),
        url="HTTPS://GitHub.com/cbolden15/open-brain-fixture/issues/42",
        title="  Import fixture issue  ",
        text="Synthetic issue body from the disposable D2 fixture.",
        privacy=_privacy(),
    )

    kwargs = record.capture_kwargs()

    assert kwargs["delivery_id"] == record.key.delivery_id()
    assert kwargs["delivery_id"].startswith("connector.github.")
    assert len(kwargs["delivery_id"]) == len("connector.github.") + 64
    assert kwargs["source_origin"] is ContentOrigin.THIRD_PARTY
    assert kwargs["source_reference"] == "https://github.com/cbolden15/open-brain-fixture/issues/42"
    assert kwargs["intent"] == "reference"
    assert kwargs["title"] == "Import fixture issue"
    assert kwargs["privacy"] == record.privacy
    assert isinstance(kwargs["payload"], ReferencePayload)
    assert kwargs["payload"].search_text().endswith(
        "Synthetic issue body from the disposable D2 fixture."
    )
    assert isinstance(kwargs["provenance"], Provenance)
    assert kwargs["provenance"].source_ref == kwargs["source_reference"]
    assert kwargs["provenance"].content_origin is ContentOrigin.THIRD_PARTY


def test_delivery_key_is_stable_while_revision_identity_changes() -> None:
    original = SourceRecordKey(
        connector_name="github",
        connection_id="account:fixture",
        resource_id="repo:fixture/project",
        external_id="pull:7",
        revision_id="sha:aaa",
    )
    replay = SourceRecordKey(
        connector_name="github",
        connection_id="account:fixture",
        resource_id="repo:fixture/project",
        external_id="pull:7",
        revision_id="sha:aaa",
    )
    changed = SourceRecordKey(
        connector_name="github",
        connection_id="account:fixture",
        resource_id="repo:fixture/project",
        external_id="pull:7",
        revision_id="sha:bbb",
    )

    assert replay.delivery_id() == original.delivery_id()
    assert changed.delivery_id() == original.delivery_id()
    assert changed.revision_identity() != original.revision_identity()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("connector_name", "GitHub"),
        ("connection_id", ""),
        ("resource_id", " repo with leading space"),
        ("external_id", "issue\x00bad"),
        ("revision_id", "revision with spaces"),
    ),
)
def test_source_record_key_rejects_unbounded_or_ambiguous_identity(
    field: str, value: str
) -> None:
    parts = {
        "connector_name": "github",
        "connection_id": "account:fixture",
        "resource_id": "repo:fixture/project",
        "external_id": "issue:1",
        "revision_id": "updated:1",
    }
    parts[field] = value

    with pytest.raises(ConnectorContractError):
        SourceRecordKey(**parts)


def test_source_record_intake_rejects_invalid_payload_content() -> None:
    with pytest.raises(ConnectorContractError, match="invalid source record"):
        SourceRecordIntake(
            key=SourceRecordKey(
                connector_name="github",
                connection_id="account:fixture",
                resource_id="repo:fixture/project",
                external_id="issue:1",
                revision_id="updated:1",
            ),
            url="https://github.com/fixture/project/issues/1",
            text="",
            privacy=_privacy(),
        )


def test_source_record_intake_rejects_malformed_optional_title() -> None:
    with pytest.raises(ConnectorContractError, match="invalid source record"):
        SourceRecordIntake(
            key=SourceRecordKey(
                connector_name="github",
                connection_id="account:fixture",
                resource_id="repo:fixture/project",
                external_id="issue:1",
                revision_id="updated:1",
            ),
            url="https://github.com/fixture/project/issues/1",
            text="Synthetic issue body.",
            privacy=_privacy(),
            title=object(),  # type: ignore[arg-type]
        )


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }
    )
