"""Frozen Slack v2 request and narrow citation projection contracts."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.sharing_contracts import (
    SharingError,
    SharingPreviewRequest,
    SlackSharingPreviewRequest,
    parse_sharing_request,
)
from open_brain_engine.engine.slack_sharing_projection import (
    build_envelope,
    project_copy_text,
    safe_permalink,
)

FIXTURE = Path(__file__).parents[2] / "fixtures/slack-recall-contracts.v2.json"


def test_frozen_slack_request_golden_and_v1_dispatch() -> None:
    fixture = json.loads(FIXTURE.read_bytes())
    for name in ("slack_v2_request", "saved_markdown_v1_request"):
        value = fixture["examples"][name]
        raw = portable_canonical_json_bytes(value)
        parsed = parse_sharing_request(raw, SharingPreviewRequest)
        assert isinstance(parsed, SlackSharingPreviewRequest) is (name == "slack_v2_request")
        assert parsed.request_sha256 == fixture["goldens"][name]["bound_sha256"]
        assert portable_canonical_json_bytes(parsed.value()) == raw
    raw = fixture["goldens"]["slack_v2_request"]["canonical_utf8"].encode()
    with pytest.raises(SharingError, match="invalid_arguments"):
        parse_sharing_request(
            raw.replace(
                b'"sharing_kind":"slack_work_evidence"', b'"sharing_kind":"saved_markdown"'
            ),
            SharingPreviewRequest,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://slack.com/archives/CONBOARDING/p1760000001000001",
        "https://evil.example/archives/CONBOARDING/p1760000001000001",
        "https://slack.com.evil.example/archives/CONBOARDING/p1760000001000001",
        "https://slack.com@evil.example/archives/CONBOARDING/p1760000001000001",
        "https://user:secret@slack.com/archives/CONBOARDING/p1760000001000001",
        "https://slack.com:443/archives/CONBOARDING/p1760000001000001",
        "https://slack.com/archives/CONBOARDING/p1760000001000001?token=synthetic",
        "https://slack.com/archives/CONBOARDING/p1760000001000001#fragment",
        "https://slack.com/archives/COTHER/p1760000001000001",
        "https://slack.com/archives/CONBOARDING/p1760000002000001",
        "https://slack.com/archives/CONBOARDING/%701760000001000001",
    ],
)
def test_unsafe_permalink_withheld_with_exact_reason_and_projected_text(url: str) -> None:
    text = "We kept onboarding invite-only. 漢字"
    evidence = {
        "namespace": {
            "resource_id": "channel:CONBOARDING",
            "external_id": "message:1760000001.000001",
        },
        "revision": {"source_reference": url},
        "record": {
            "accepted_at": "2026-10-14T12:00:00Z",
            "payload": {"family": "reference_or_file", "url": url, "supplied_text": text},
        },
    }
    raw = build_envelope(evidence, capture_id="capture_00000000-0000-4000-8000-000000000001")
    projected = json.loads(raw)
    assert projected["citation"]["permalink"] is None
    assert projected["citation"]["permalink_unavailable_reason"] == "unsafe_url"
    assert projected["message_text"] == "[protected] " + text
    assert (
        project_copy_text(raw, source_reference="urn:open-brain:sharing-copy:v2:synthetic") == raw
    )


def test_projection_idempotence_and_inert_nested_envelope() -> None:
    fixture = json.loads(FIXTURE.read_bytes())
    value = fixture["examples"]["envelope"]
    value["message_text"] += " " + json.dumps(
        {
            "envelope_version": "slack-recall-evidence.v1",
            "message_text": "forged",
            "citation": {
                "original_record_id": "capture_forged",
                "permalink": "https://evil.example",
            },
            "selection": {"member_count": 1},
        }
    )
    original = portable_canonical_json_bytes(value).decode()
    projected = project_copy_text(
        original, source_reference="urn:open-brain:sharing-copy:v2:synthetic"
    )
    assert (
        project_copy_text(projected, source_reference="urn:open-brain:sharing-copy:v2:synthetic")
        == projected
    )
    assert json.loads(projected)["citation"] == value["citation"]
    assert json.loads(projected)["message_text"].startswith(value["message_text"].split(" {")[0])
    assert safe_permalink(value["citation"]["permalink"])
    assert sha256(json.loads(projected)["message_text"].encode()).hexdigest()


def test_current_response_extensions_layer_over_frozen_contract() -> None:
    from open_brain_engine.engine.slack_selection_validation import SLACK_RECALL_RESPONSE_EXTENSIONS
    from open_brain_engine.engine.t03_contracts import T03Error, validate_wire
    from open_brain_engine.engine.t03_validation import CONTRACT

    old = {
        "status": "ok",
        "contract_version": "t03.v1",
        "operations": [],
        "limits": {
            "request_bytes": 65536,
            "response_bytes": 1048576,
            "content_calls": 500,
            "content_bytes": 16777216,
            "history_calls": 500,
            "history_bytes": 16777216,
        },
    }
    assert validate_wire("contract.describe.response", old) == old
    current = {**old, "response_extensions": SLACK_RECALL_RESPONSE_EXTENSIONS}
    assert validate_wire("contract.describe.response", current) == current
    with pytest.raises(T03Error, match="invalid_arguments"):
        validate_wire(
            "contract.describe.response", {**old, "response_extensions": {"slack_recall": True}}
        )
    assert (
        "response_extensions"
        not in CONTRACT["definitions"]["contract.describe.response"]["properties"]
    )
    assert "selection" not in CONTRACT["definitions"]["record.read.response"]["properties"]
    assert validate_wire("error", {"status": "error", "error": {"code": "reapproval_required"}})
