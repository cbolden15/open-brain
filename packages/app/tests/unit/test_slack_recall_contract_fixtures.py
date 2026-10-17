"""Pin W0's proposed bytes without admitting them as existing sharing evidence."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.sharing_contracts import SharingError, SharingPreviewRequest


def _fixture() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "slack-recall-contracts.v2.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("name", "domain"),
    [
        ("saved_markdown_v1_request", b"open-brain-sharing-request.v1\0"),
        ("slack_v2_request", b"open-brain-sharing-request.v2\0"),
        ("envelope", b"open-brain-slack-recall-evidence.v1\0"),
        ("selection", b"open-brain-slack-recall-selection.v1\0"),
        ("read_selection", None),
    ],
)
def test_contract_canonical_bytes_and_digests_are_frozen(name: str, domain: bytes | None) -> None:
    fixture = _fixture()
    raw = portable_canonical_json_bytes(fixture["examples"][name])
    golden = fixture["goldens"][name]
    assert raw.decode("utf-8") == golden["canonical_utf8"]
    assert sha256(raw).hexdigest() == golden["sha256"]
    if domain is not None:
        assert sha256(domain + raw).hexdigest() == golden["bound_sha256"]


def test_saved_markdown_v1_bytes_stay_unchanged_and_do_not_admit_v2() -> None:
    fixture = _fixture()
    value = fixture["examples"]["saved_markdown_v1_request"]
    request = SharingPreviewRequest(**value)
    golden = fixture["goldens"]["saved_markdown_v1_request"]
    canonical = portable_canonical_json_bytes(request.value()).decode("utf-8")
    assert canonical == golden["canonical_utf8"]
    assert request.request_sha256 == golden["bound_sha256"]
    with pytest.raises(SharingError, match="^invalid_arguments$"):
        SharingPreviewRequest(**{**value, "dto_version": 2})
