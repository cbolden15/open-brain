"""The revision gate is a table keyed by connector; Slack attestation bytes do not move."""

from __future__ import annotations

import hashlib
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.source_observation import SourceRevisionObservation

from open_brain_collector import capture_policy
from open_brain_collector.capture_policy import (
    _REVISION_CONNECTORS,
    observe_revision,
    revision_store_dirname,
)
from open_brain_connectors.runtime.live_common import bounded_json, local_source_privacy
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey

ACCOUNT = "account:synthetic"
DRIVE_REVISION = "revision:" + "a" * 64


def _intake(
    connector: str,
    resource_id: str,
    external_id: str,
    revision: str,
    text: str = "Synthetic body",
) -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey(connector, ACCOUNT, resource_id, external_id, revision),
        "https://docs.google.com/document/d/doc-1/edit",
        text,
        local_source_privacy(),
        title="Synthetic title",
    )


def _slack() -> SourceRecordIntake:
    return _intake("slack", "channel:CSYNTHETIC", "message:1791900000.000001", "edited:1")


def _expected(intake: SourceRecordIntake, normalization_version: str) -> SourceRevisionObservation:
    digest = hashlib.sha256(intake.text.encode()).hexdigest()
    privacy = intake.privacy.to_dict()
    payload = intake.payload().to_dict()
    return SourceRevisionObservation(
        original_sha256=digest,
        transformed_sha256=digest,
        normalization_version=normalization_version,
        privacy_policy_version=cast(str, privacy["policy_version"]),
        privacy_policy_sha256=hashlib.sha256(bounded_json(privacy)).hexdigest(),
        admitted_payload_sha256=hashlib.sha256(portable_canonical_json_bytes(payload)).hexdigest(),
    )


def test_table_holds_exactly_slack_and_drive_with_their_directories() -> None:
    assert _REVISION_CONNECTORS == {
        "slack": ("slack-source-v1", "slack-revisions"),
        "google_drive": ("drive-source-v1", "drive-revisions"),
    }
    assert revision_store_dirname("slack") == "slack-revisions"
    assert revision_store_dirname("google_drive") == "drive-revisions"
    assert revision_store_dirname("gmail") is None
    assert revision_store_dirname("agent_session") is None
    assert not hasattr(capture_policy, "observe_slack")


def test_slack_attestation_bytes_are_unchanged() -> None:
    intake = _slack()
    observed = observe_revision(intake)
    assert observed.observation == _expected(intake, "slack-source-v1")
    assert observed.observation is not None
    assert observed.observation.normalization_version == "slack-source-v1"
    assert observed.observation.privacy_policy_version == "priority-capture-v1"
    assert observed.observation.original_sha256 == hashlib.sha256(b"Synthetic body").hexdigest()
    assert observed.observation.transformed_sha256 == observed.observation.original_sha256
    assert (observed.key, observed.url, observed.text, observed.privacy, observed.title) == (
        intake.key,
        intake.url,
        intake.text,
        intake.privacy,
        intake.title,
    )
    assert observe_revision(observed) is observed


def test_drive_set_shapes_are_attested_with_the_drive_version() -> None:
    folder = _intake("google_drive", "drive_folder:folder-1", "file:doc-1", DRIVE_REVISION)
    account = _intake("google_drive", "drive_account:all", "file:doc-1#part:2", DRIVE_REVISION)
    for intake in (folder, account):
        observed = observe_revision(intake)
        assert observed.observation == _expected(intake, "drive-source-v1")
        assert observed.key == intake.key and observed.text == intake.text
        assert observe_revision(observed) is observed


def test_drive_file_and_connectors_outside_the_table_pass_through() -> None:
    single = _intake("google_drive", "drive_file:doc-1", "file:doc-1", DRIVE_REVISION)
    assert observe_revision(single) is single
    assert single.observation is None
    gmail = _intake("gmail", "label:INBOX", "message1", "r1")
    assert observe_revision(gmail) is gmail
    session = _intake("agent_session", "local_project:synthetic", "turn:1", "r1")
    assert observe_revision(session) is session
