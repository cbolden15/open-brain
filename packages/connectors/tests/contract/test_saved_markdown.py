from __future__ import annotations

import os
from pathlib import Path

import pytest
from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime import saved_markdown as saved_markdown_module
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.saved_markdown import (
    SavedMarkdownLimits,
    SavedMarkdownRootAdapter,
    normalize_saved_markdown,
)


def test_normalizer_removes_owner_context_but_keeps_fenced_and_later_content() -> None:
    transformed = normalize_saved_markdown(
        b"---\nprivate_note: synthetic\n---\n# Public title\n\n```md\n# Why Saved\n"
        b"fenced\n```\n\nWhy Saved\n---------\nowner-only context\n\n## Details\nkept body\n"
    )

    assert "private_note" not in transformed
    assert "owner-only context" not in transformed
    assert "# Why Saved\nfenced" in transformed
    assert "## Details\nkept body" in transformed


@pytest.mark.parametrize(
    "payload, code",
    (
        (b"---\nnot valid\n# body\n", "saved_markdown_malformed_frontmatter"),
        (b"\xff", "saved_markdown_invalid_utf8"),
        (b"# title\naccess_token = synthetic\n", "saved_markdown_secret_bearing"),
    ),
)
def test_normalizer_refuses_malformed_or_secret_content(payload: bytes, code: str) -> None:
    with pytest.raises(ConnectorContractError, match=code):
        normalize_saved_markdown(payload)


def test_selected_root_binds_destination_policy_and_bytes(tmp_path: Path) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "entry.md").write_text(
        "---\nowner: synthetic\n---\n# Saved item\n\nBody kept.\n\n# Why Saved\nprivate.\n",
        encoding="utf-8",
    )
    (root / ".hidden.md").write_text("# hidden\n", encoding="utf-8")
    (root / "draft.tmp").write_text("# temporary\n", encoding="utf-8")
    (root / "unsupported.txt").write_text("synthetic", encoding="utf-8")
    adapter = _adapter(root)

    scan = adapter.dry_run()
    accepted = [candidate for candidate in scan.candidates if candidate.accepted]
    refused = {
        candidate.relative_path: candidate.refusal_code
        for candidate in scan.candidates
        if not candidate.accepted
    }

    assert scan.complete is True
    assert len(accepted) == 1
    assert accepted[0].intake is not None
    assert accepted[0].identity is not None
    assert "private" not in accepted[0].intake.text
    assert accepted[0].identity.delivery_id.startswith("saved-markdown.")
    assert refused == {"unsupported.txt": "unsupported_format"}

    changed_destination = _adapter(root, destination="destination:other").dry_run().candidates[0]
    changed_policy = _adapter(root, policy_version="policy-v2").dry_run().candidates[0]
    assert changed_destination.identity is not None
    assert changed_policy.identity is not None
    assert changed_destination.identity.delivery_id != accepted[0].identity.delivery_id
    assert changed_policy.identity.delivery_id != accepted[0].identity.delivery_id


def test_selected_root_refuses_symlinks_escapes_limits_and_unstable_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "selected"
    outside = tmp_path / "outside.md"
    root.mkdir()
    outside.write_text("# outside\n", encoding="utf-8")
    (root / "link.md").symlink_to(outside)
    (root / "large.md").write_text("# " + "x" * 80, encoding="utf-8")
    (root / "changing.md").write_text("# changing\n", encoding="utf-8")
    adapter = _adapter(root, limits=SavedMarkdownLimits(max_file_bytes=64))

    scan = adapter.dry_run()
    refused = {candidate.relative_path: candidate.refusal_code for candidate in scan.candidates}
    assert refused["link.md"] == "symlink"
    assert refused["large.md"] == "file_too_large"
    assert adapter._candidate(root, outside, "escape.md").refusal_code == "root_escape"

    original = saved_markdown_module._fingerprint
    calls = 0

    def unstable(value: os.stat_result) -> tuple[int, int, int, int, int]:
        nonlocal calls
        calls += 1
        result = original(value)
        return result if calls % 2 else (*result[:2], result[2] + 1, *result[3:])

    monkeypatch.setattr(saved_markdown_module, "_fingerprint", unstable)
    unstable_scan = _adapter(root).dry_run()
    unstable_refusals = {
        candidate.relative_path: candidate.refusal_code for candidate in unstable_scan.candidates
    }
    assert unstable_refusals["changing.md"] == "unstable_read"
    assert unstable_scan.complete is False


def test_selected_root_enforces_transformed_and_core_byte_limits(tmp_path: Path) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "characters.md").write_text(
        "# Title\n\none two three four five six seven eight nine ten eleven twelve\n",
        encoding="utf-8",
    )
    (root / "bytes.md").write_text(
        "# Title\n\nå å å å å å å å å å å å å å å å å å å å\n", encoding="utf-8"
    )

    character_scan = _adapter(
        root, limits=SavedMarkdownLimits(max_transformed_characters=32)
    ).dry_run()
    assert {
        item.relative_path: item.refusal_code for item in character_scan.candidates
    } == {"bytes.md": "transformed_too_large", "characters.md": "transformed_too_large"}
    byte_scan = _adapter(root, limits=SavedMarkdownLimits(max_core_payload_bytes=32)).dry_run()
    assert {
        item.relative_path: item.refusal_code for item in byte_scan.candidates
    } == {"bytes.md": "core_payload_too_large", "characters.md": "core_payload_too_large"}


def _adapter(
    root: Path,
    *,
    destination: str = "destination:synthetic",
    policy_version: str = "policy-v1",
    limits: SavedMarkdownLimits | None = None,
) -> SavedMarkdownRootAdapter:
    return SavedMarkdownRootAdapter(
        root,
        destination_identity=destination,
        accepted_source_identity="source:synthetic",
        source_reference="https://saved.example.test/library",
        privacy=PrivacyDecision.from_dict(
            {
                "authority": {"cloud": False, "external_egress": False},
                "confirmation_ref": None,
                "policy_version": policy_version,
                "reason": "policy_public",
                "tier": "public",
            }
        ),
        limits=limits,
    )
