from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    SearchPageRequest,
    T03Error,
    parse_wire,
    request_from_wire,
    response_to_wire,
    validate_wire,
)

FIXTURES = Path(__file__).resolve().parents[3] / "tests/fixtures/new-user-t03"
CASES = json.loads((FIXTURES / "strict-cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_production_values_match_frozen_grammar(case: dict[str, object]) -> None:
    def run() -> None:
        value = parse_wire(str(case["raw"]))
        validate_wire(str(case["schema"]), value)
        if case["schema"] == "error":
            return
        name, direction = str(case["schema"]).rsplit(".", 1)
        if direction == "request" and name != "contract.describe":
            request_from_wire(name, value)
        elif direction == "response":
            assert response_to_wire(name, value) == value

    if case["accept"]:
        run()
    else:
        with pytest.raises(T03Error):
            run()


def test_packaged_schema_matches_exact_frozen_grammar() -> None:
    from open_brain_engine.engine import t03_contracts

    packaged = Path(t03_contracts.__file__).with_name("t03_schema.json")
    assert packaged.read_bytes() == (FIXTURES / "strict-contract.schema.json").read_bytes()


def test_request_defaults_and_no_wire_authority() -> None:
    request = request_from_wire("search.page", {"dto_version": 1, "query": "hello"})
    assert isinstance(request, SearchPageRequest)
    assert request.limit == 10 and request.mode == "lexical" and request.cursor is None
    with pytest.raises(T03Error):
        request_from_wire("search.page", {"dto_version": 1, "query": "hello", "owner": True})
    authority = EffectiveAuthority("synthetic", "session", frozenset({"search"}), frozenset())
    authority.require("search")
    assert not authority.permits_space(None)
    with pytest.raises(T03Error):
        authority.require("content-read")
