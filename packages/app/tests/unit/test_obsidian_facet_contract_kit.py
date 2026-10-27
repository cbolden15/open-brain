"""O0 codec parity and real publication producer proof, not live bridge proof."""

from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine.facet_validation import CONTRACT
from open_brain_engine.portable.review_binding import validate_review_binding

from open_brain.services.plugin_bridge import _frozen_publication_result
from open_brain.services.plugin_facet_contracts import (
    OPERATIONS,
    PROTOCOL,
    RESOLVE_OPERATION,
    capabilities,
    decode_request,
    encode_response,
    limits,
    managed_request,
    response_size,
)
from open_brain.services.review_publication import ReviewPublicationService
from packages.engine.tests.contract.test_facets_acceptance import FacetHarness

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "packages/obsidian-plugin/tests/fixtures/facets"
REQUEST_ID = "plugin_00000000-0000-4000-8000-000000000001"


def actual_inspection(tmp_path: Path) -> dict[str, object]:
    """Use real Stage 1 publication and the frozen plugin inspection shape."""
    brain = FacetHarness(tmp_path)
    value = brain.value(brain.dimension())
    capture = brain.capture()
    brain.patch(capture, add=(value,))
    query = brain.write(
        "facet.query.create", name="O0 fixture", expected_version=0,
        selector={"all_value_ids": [value]},
    )
    proposed = brain.call(
        "review.propose_from_facet_query", query_id=query["subject_id"],
        expected_query_version=1, destination_space_id=brain.engine.inbox.spaces()[0].space_id,
        target_page_id=None, expected_page_revision_id=None,
        title="Synthetic O0 publication", markdown="Synthetic evidence: café.\n",
        operation_id="o0-real-producer",
    )
    shown = ReviewPublicationService(brain.engine.review, authority=brain.authority).show(
        {"proposal_id": proposed["proposal_id"]}
    )
    # Plugin sees JSON arrays, not dataclass tuples.
    return cast(dict[str, object], json.loads(json.dumps(_frozen_publication_result(shown))))


def test_real_publication_inspection_producer(tmp_path: Path) -> None:
    value = actual_inspection(tmp_path)
    expected = json.loads((FIXTURES / "o0-cases.json").read_text())["publication_inspections"][0]
    assert set(value) == set(expected)
    binding = validate_review_binding(value["facet_selection"])
    assert value["review_token"] == binding["review_digest"]
    assert value["selected_capture_ids"] == binding["selected_capture_ids"]
    assert value["capture_ids"] == binding["provenance"]
    assert value["page_id"] == binding["page_id"]
    assert value["space_id"] == binding["destination_space_id"]


def test_generated_types_and_frozen_inputs() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "tools/generate_obsidian_facet_types.py"), "--check"],
        check=True, cwd=ROOT,
    )
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["contract_revision"] == "obsidian-o0-v1"
    assert manifest["stage_one_pin"]["contract_revision"] == CONTRACT["contract_revision"]
    for path, expected in manifest["files"].items():
        assert sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    c0 = ROOT / "packages/engine/tests/fixtures/facets/manifest.json"
    assert sha256(c0.read_bytes()).hexdigest() == manifest["stage_one_pin"][
        "fixture_manifest_sha256"
    ]


def test_descriptor_is_explicit_intersection_and_matches_ts_fixture() -> None:
    assert capabilities([]) == {"version": 1, "operations": []}
    assert capabilities(["unknown", "facet.query.page"])["operations"] == [
        {"operation": "facet.query.page", "dto_versions": [1],
         "request_bytes": 65536, "response_bytes": 250000},
    ]
    fixture = json.loads((FIXTURES / "o0-cases.json").read_text())
    assert capabilities(OPERATIONS) == fixture["handshake"]["facet_capabilities"]


def frame(operation: str, arguments: Any) -> bytes:
    return json.dumps({**PROTOCOL, "request_id": REQUEST_ID, "operation": operation,
                       "arguments": arguments}, ensure_ascii=False).encode() + b"\n"


@pytest.mark.parametrize("bad", [
    b'{"protocol_version":1,"protocol_version":1}\n', b"{}\n{}\n", b"{}", b"\xff\n",
    b'{"protocol_version":1e0}\n', b'{"protocol_version":9007199254740992}\n',
])
def test_rejects_bad_raw_frames(bad: bytes) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        decode_request(bad, available_operations=OPERATIONS)


def test_exact_utf8_frame_bound_and_availability() -> None:
    operation = "facet.dimension.create"
    raw = frame(operation, {"dto_version": 1, "operation_id": "café", "name": "Café",
                            "expected_version": 0})
    maximum = limits(operation)[0]
    at_limit = raw[:-1] + b" " * (maximum - len(raw)) + b"\n"
    assert len(at_limit) == maximum
    assert decode_request(at_limit, available_operations=[operation])["arguments"]["name"] == "Café"
    rejected_frames = (
        at_limit[:-1] + b" \n", raw.replace(b'"dto_version": 1', b'"dto_version": 1.0'),
    )
    for rejected in rejected_frames:
        with pytest.raises(ValueError):
            decode_request(rejected, available_operations=[operation])
    with pytest.raises(ValueError, match="unsupported_capability"):
        decode_request(raw, available_operations=[])


@pytest.mark.parametrize("path", ["/x.md", "../x.md", "a/../x.md", "a//x.md", "a/./x.md",
                                      "C:/x.md", "a\\x.md", "x.md\x00", "\ud800.md"])
def test_managed_request_closed_path(path: str) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        managed_request({"dto_version": 1,
                         "workspace_id": "workspace_00000000-0000-4000-8000-000000000002",
                         "relative_path": path})


def test_python_frames_consumed_by_ts_fixture() -> None:
    cases = json.loads((FIXTURES / "o0-cases.json").read_text())
    for case in cases["frames"]:
        operation, result = case["operation"], case["result"]
        encoded = encode_response(operation, REQUEST_ID, result)
        assert encoded.decode() == case["frame"]
        assert len(encoded) == response_size(REQUEST_ID, result)
        assert len(encoded) <= limits(operation)[1]
    resolver = cases["managed_request"]
    assert decode_request(frame(RESOLVE_OPERATION, resolver),
                          available_operations=[RESOLVE_OPERATION])["arguments"] == resolver


def test_invalid_response_never_encodes() -> None:
    with pytest.raises(ValueError):
        encode_response("facet.dimension.list", REQUEST_ID, {"status": "ok", "owner": True})
