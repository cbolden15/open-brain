from __future__ import annotations

import json
import sqlite3

import pytest
from open_brain_engine.engine.harness_memory import (
    HarnessCitation,
    HarnessMemory,
    decode_memory,
    encode_memory,
    recognized_memory,
)
from open_brain_engine.engine.search_projection import (
    SearchDocumentProjection,
    project_search_document,
    public_harness_record_text,
)

REFERENCE = "urn:open-brain:harness:v1:synthetic-turn-1"


def memory() -> HarnessMemory:
    return HarnessMemory(
        kind="material",
        text="Lentil soup\n" + "Simmer lentils for 25 minutes.\n" * 80,
        source_context="Requested a complete soup recipe; contextualword.",
        citation=HarnessCitation(
            harness="codex", client="fixture", project_id="project-logical",
            session_id="session-synthetic", message_ordinal=4, authorship="assistant",
            timestamp=None,
        ),
    )


def test_complete_material_round_trip_and_recognition() -> None:
    original = memory()
    encoded = encode_memory(original)
    assert decode_memory(encoded) == original
    assert recognized_memory(encoded, source_reference=REFERENCE) == original
    assert recognized_memory(encoded, source_reference="urn:untrusted") is None


@pytest.mark.parametrize("field,value", [("version", 2), ("kind", "guess"), ("extra", True)])
def test_exact_envelope_schema(field: str, value: object) -> None:
    raw = json.loads(encode_memory(memory()))
    raw[field] = value
    with pytest.raises(ValueError):
        decode_memory(json.dumps(raw))


@pytest.mark.parametrize("field,value", [
    ("project_id", "/private/project"), ("message_ordinal", True),
    ("authorship", "system"), ("timestamp", "invented"), ("session_id", "x" * 257),
])
def test_citation_validation(field: str, value: object) -> None:
    raw = json.loads(encode_memory(memory()))
    raw["citation"][field] = value
    with pytest.raises(ValueError):
        decode_memory(json.dumps(raw))


def test_duplicate_json_keys_rejected() -> None:
    with pytest.raises(ValueError):
        decode_memory('{"version":1,"version":1}')


def projection(reference: str, body: str) -> SearchDocumentProjection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE captures (capture_id,source_origin,source_reference,"
                       "provenance_json,action,canonical_path)")
    connection.execute("INSERT INTO captures VALUES(?,?,?,?,?,?)", (
        "capture-test", "third_party", reference, json.dumps({
            "content_origin": "third_party", "owner_context": "automation_absent",
            "source_ref": reference,
        }), "quick", None,
    ))
    try:
        return project_search_document(connection, result_id="capture-test",
            capture_id="capture-test", record_type="source", title=body[:200],
            body=body, canonical_path=None)
    finally:
        connection.close()


def test_index_includes_only_selected_material_and_derived_title() -> None:
    original = memory()
    result = projection(REFERENCE, encode_memory(original))
    assert result.body == original.text
    assert result.title == "Lentil soup"
    assert "contextualword" not in result.body
    assert "session-synthetic" not in result.body
    assert "project-logical" not in result.body
    assert result.trust == "third_party"


def test_unrecognized_text_unchanged_and_malformed_recognized_fails_closed() -> None:
    assert projection("urn:untrusted", "ordinary text").body == "ordinary text"
    assert "session-synthetic" in projection("urn:untrusted", encode_memory(memory())).body
    assert projection(REFERENCE, '{"version":2}').body == ""


def test_existing_redaction_applies_after_decoding() -> None:
    raw = json.loads(encode_memory(memory()))
    raw["text"] = "Use /private/synthetic/source.txt for the recipe"
    assert "/private/synthetic" not in projection(REFERENCE, json.dumps(raw)).body


def test_record_read_preserves_complete_envelope_and_citation() -> None:
    original = memory()
    projected, indexed = public_harness_record_text(
        encode_memory(original), source_reference=REFERENCE,
    )
    assert decode_memory(projected) == original
    assert indexed == original.text


def test_record_read_redacts_context_without_invalidating_json() -> None:
    original = memory()
    altered = HarnessMemory(
        original.kind, original.text, "/private/synthetic/context.txt", original.citation,
    )
    projected, _ = public_harness_record_text(encode_memory(altered), source_reference=REFERENCE)
    assert "/private/synthetic" not in decode_memory(projected).source_context
