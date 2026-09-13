from __future__ import annotations

import importlib
import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "packages" / "legacy" / "tests" / "fixtures" / "phase0" / "current_records.json"


def _fixture() -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("current record characterization must be an object")
    return cast(dict[str, object], value)


def test_current_typed_record_fields_match_characterization() -> None:
    records = _fixture()["records"]
    assert isinstance(records, list) and records
    for raw_record in records:
        assert isinstance(raw_record, dict)
        symbol = raw_record["symbol"]
        expected_fields = raw_record["fields"]
        assert isinstance(symbol, str)
        assert isinstance(expected_fields, list)
        module_name, class_name = symbol.rsplit(".", 1)
        record_type = getattr(importlib.import_module(module_name), class_name)
        assert is_dataclass(record_type)
        assert [field.name for field in fields(record_type)] == expected_fields
        assert raw_record["portable_v1_status"] == "legacy-characterized-not-portable-v1"


def test_non_json_authorities_are_explicit_and_source_backed() -> None:
    representations = _fixture()["other_representations"]
    assert isinstance(representations, list)
    assert {item["name"] for item in representations if isinstance(item, dict)} == {
        "canonical_markdown",
        "operational_sqlite",
    }
    for item in representations:
        assert isinstance(item, dict)
        source = item["source"]
        assert isinstance(source, str) and (ROOT / source).is_file()
