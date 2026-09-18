from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from open_brain_engine.engine.contracts import (
    PublicProvenance,
    RetrievalResult,
    project_public_result_text,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.local_operations import search_result
from open_brain.services.mcp_protocol import McpCallError
from open_brain.services.plugin_bridge import PluginBridgeFailure, _read_request
from open_brain.services.t03_adapters import T03AppAdapter

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "tests/fixtures/new-user-t03"
SPEC = importlib.util.spec_from_file_location("t03_validator", FIXTURES / "validator.py")
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
CORPUS = json.loads((FIXTURES / "strict-cases.json").read_text())
COMPAT = json.loads((FIXTURES / "compatibility.json").read_text())


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_t03_strict_contract(case: dict[str, Any]) -> None:
    if case["accept"]:
        VALIDATOR.validate(case["schema"], VALIDATOR.parse(case["raw"]))
    else:
        with pytest.raises(ValueError):
            VALIDATOR.validate(case["schema"], VALIDATOR.parse(case["raw"]))


@pytest.mark.parametrize("case", COMPAT["baseline_bridge_cases"], ids=lambda case: case["id"])
def test_t03_actual_legacy_bridge(case: dict[str, Any]) -> None:
    raw = (
        str(case["raw_request"]).encode()
        if "raw_request" in case
        else json.dumps(case["request"]).encode()
    )
    if case["expected"] == "accept":
        assert _read_request(raw) == case["request"]
    elif case["id"] == "C04":
        # The frozen receipt records that the historical bridge did not know this
        # operation. The current bridge does, but the malformed legacy arguments
        # still fail typed validation before an engine callback can run.
        assert case["expected"] == "unknown_operation"
        assert _read_request(raw) == case["request"]
        calls = 0

        class Retrieval:
            def read_record(self, _request: object, *, authority: object) -> object:
                nonlocal calls
                calls += 1
                return {}

        authority = EffectiveAuthority(
            "compatibility-test",
            "compatibility-session",
            frozenset({"content-read"}),
            None,
        )
        granted = LocalMcpAdapter(
            negotiated=T03AppAdapter(
                SimpleNamespace(retrieval=Retrieval()),
                authority,
                frozenset({"content-read"}),
            )
        )
        with pytest.raises(McpCallError, match="^invalid_arguments$"):
            granted.call_tool("brain_read", case["request"]["arguments"])
        assert calls == 0

        denied = LocalMcpAdapter(
            negotiated=T03AppAdapter(
                SimpleNamespace(retrieval=Retrieval()), authority, frozenset()
            )
        )
        with pytest.raises(McpCallError, match="^unsupported_capability$"):
            denied.call_tool(
                "brain_read",
                {
                    "dto_version": 1,
                    "record_id": "capture_123e4567-e89b-42d3-a456-426614174100",
                    "expected_revision_id": "capture_123e4567-e89b-42d3-a456-426614174100",
                },
            )
        assert calls == 0
    else:
        with pytest.raises(PluginBridgeFailure, match=str(case["expected"])):
            _read_request(raw)


@pytest.mark.parametrize("case", COMPAT["baseline_mcp_cases"], ids=lambda case: case["id"])
def test_t03_actual_legacy_mcp(case: dict[str, Any]) -> None:
    adapter = LocalMcpAdapter(search=lambda _query, _limit: ())
    if case["expected"] == "accept":
        assert adapter.call_tool(case["tool"], case["arguments"]) is not None
    else:
        with pytest.raises(McpCallError, match=str(case["expected"])):
            adapter.call_tool(case["tool"], case["arguments"])


@pytest.mark.parametrize("case", COMPAT["baseline_provenance_cases"], ids=lambda case: case["id"])
def test_t03_actual_ordered_provenance(case: dict[str, Any]) -> None:
    def parse() -> PublicProvenance:
        return PublicProvenance(
            source_origin="third_party",
            capture_id=case["capture_id"],
            capture_ids=tuple(case["capture_ids"]),
        )

    if case["expected"] == "accept":
        assert parse().capture_ids == tuple(case["capture_ids"])
    else:
        with pytest.raises(ValueError):
            parse()


def test_t03_legacy_protocol_never_coerces_fractional_versions() -> None:
    request = json.dumps(COMPAT["baseline_bridge_cases"][0]["request"])
    for value in ["1.0", "1e0"]:
        raw = request.replace('"protocol_version": 1', '"protocol_version": ' + value)
        with pytest.raises(PluginBridgeFailure, match="incompatible_protocol"):
            _read_request(raw.encode())


def test_t03_actual_legacy_search_producer_retains_exact_shape() -> None:
    fixture = COMPAT["baseline_search_serialization"]
    result = RetrievalResult(
        **fixture["input"],
        provenance=PublicProvenance(
            source_origin="third_party",
            capture_id=fixture["input"]["capture_id"],
            capture_ids=(fixture["input"]["capture_id"],),
        ),
    )
    assert search_result((result,)) == fixture["expected"]


def test_t03_actual_projection_preserves_secondary_source_redaction() -> None:
    security = json.loads((FIXTURES / "security-boundaries.json").read_text())
    for case in security["baseline_projection_cases"]:
        projected = project_public_result_text(
            case["text"], protected_literals=tuple(case["protected_literals"])
        )
        for literal in case["protected_literals"]:
            assert literal not in projected


def test_t03_projected_vocabulary_matches_actual_engine_producers() -> None:
    import sqlite3

    from open_brain_engine.engine.search_projection import _canonical_trust, source_trust

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE review_page_heads(page_id TEXT);"
        "CREATE TABLE decisions(page_id TEXT, outcome TEXT, publication_id TEXT);"
        "INSERT INTO decisions VALUES('page', 'approved', 'publication');"
    )
    capture = connection.execute("SELECT 'quick' AS action, NULL AS canonical_path").fetchone()
    try:
        for case in CORPUS["cases"]:
            if not case["id"].startswith("projection."):
                continue
            response = VALIDATOR.validate(case["schema"], VALIDATOR.parse(case["raw"]))
            summary = response["results"][0]
            provenance = summary["provenance"]
            PublicProvenance(
                capture_id=provenance["representative_capture_id"],
                capture_ids=tuple(provenance["capture_ids"]),
                source_origin=provenance["source_origin"],
            )
            if summary["record_type"] == "source":
                trust = source_trust(provenance["source_origin"])
            else:
                trust, _ = _canonical_trust(
                    connection,
                    capture=capture,
                    origin=provenance["source_origin"],
                    result_id="page",
                    canonical_path=None,
                )
            assert summary["trust"] == trust
    finally:
        connection.close()


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_t03_byte_boundaries_are_utf8_only(encoding: str) -> None:
    raw = CORPUS["cases"][0]["raw"].encode(encoding)
    with pytest.raises(ValueError):
        VALIDATOR.validate(CORPUS["cases"][0]["schema"], VALIDATOR.parse(raw))
    legacy = json.dumps(COMPAT["baseline_bridge_cases"][0]["request"]).encode(encoding)
    with pytest.raises(PluginBridgeFailure, match="invalid_request"):
        _read_request(legacy)


def test_t03_invalid_utf8_is_rejected_before_dto_construction() -> None:
    with pytest.raises(ValueError):
        VALIDATOR.parse(b'{"query":"\xff"}')
    with pytest.raises(PluginBridgeFailure, match="invalid_request"):
        _read_request(b'{"query":"\xff"}')


def test_t03_raw_byte_corpus_matches_consumers() -> None:
    corpus = json.loads((FIXTURES / "raw-byte-cases.json").read_text())
    for case in corpus["cases"]:
        if case["accept"]:
            VALIDATOR.validate(case["schema"], VALIDATOR.parse(bytes(case["bytes"])))
        else:
            with pytest.raises(ValueError):
                VALIDATOR.validate(case["schema"], VALIDATOR.parse(bytes(case["bytes"])))
