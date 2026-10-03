from __future__ import annotations

import json
import os
import unicodedata
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime import saved_markdown as saved_markdown_module
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.saved_markdown import (
    SavedMarkdownIdentity,
    SavedMarkdownLimits,
    SavedMarkdownRootAdapter,
    SavedMarkdownScanEpoch,
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


@pytest.mark.parametrize("opening,closing", [("```markdown", "```"), ("~~~~markdown", "~~~~")])
@pytest.mark.parametrize("heading", ["## Example", "Example\n-------"])
def test_owner_context_fenced_headings_cannot_end_the_stripped_section(
    opening: str, closing: str, heading: str,
) -> None:
    raw = (
        "# Source\nVisible before\n## Why Saved\nowner-start\n"
        f"{opening}\n{heading}\nowner-in-fence\n{closing}\n"
        "owner-after-fence\n## Public section\nVisible after\n"
    ).encode()
    assert normalize_saved_markdown(raw) == (
        "# Source\nVisible before\n## Public section\nVisible after\n"
    )


@pytest.mark.parametrize("false_close", ["```", "~~~~", "````not-a-close"])
def test_owner_context_requires_a_matching_complete_fence_close(false_close: str) -> None:
    raw = (
        "# Source\nVisible before\n## Why Saved\n````markdown\n"
        f"{false_close}\n## Still fenced\nowner-context\n````\n"
        "owner-after-fence\n## Public section\nVisible after\n"
    ).encode()
    assert normalize_saved_markdown(raw) == (
        "# Source\nVisible before\n## Public section\nVisible after\n"
    )


@pytest.mark.parametrize("heading", [
    " ## Why Saved", "  ## Why Saved", "   ## Why Saved",
    "Why Saved\n-", "Why Saved\n=", "   Why Saved\n   ---",
])
def test_valid_indented_or_short_setext_owner_heading_is_stripped(heading: str) -> None:
    # Setext text must start a new paragraph, not extend the preceding body.
    prefix = "# Source\nVisible before\n" + ("\n" if "\n" in heading else "")
    raw = f"{prefix}{heading}\nowner-context\n# Public\nVisible after\n".encode()
    assert normalize_saved_markdown(raw) == f"{prefix}# Public\nVisible after\n"


@pytest.mark.parametrize("heading", ["## **Why Saved**", "## Why&#32;Saved", "Why\nSaved\n---"])
def test_semantic_owner_heading_is_stripped_without_rendering_retained_body(heading: str) -> None:
    raw = f"# Source\nVisible **body**\n\n{heading}\nowner-context\n# Public\nAfter\n".encode()
    assert normalize_saved_markdown(raw) == "# Source\nVisible **body**\n\n# Public\nAfter\n"


@pytest.mark.parametrize("block", [
    "\n---", "- owner-list-item\n---", "> owner-quote\n---",
    "\n    owner-code\n---", "\n\towner-code\n---",
])
def test_non_heading_blocks_do_not_end_owner_context(block: str) -> None:
    raw = (
        f"# Source\nVisible before\n## Why Saved\nowner-before\n{block}\n"
        "owner-after\n## Public\nVisible after\n"
    ).encode()
    assert normalize_saved_markdown(raw) == "# Source\nVisible before\n## Public\nVisible after\n"


@pytest.mark.parametrize("opener", ["\t```", "    ```", "```lang`invalid"])
def test_non_fence_openers_do_not_hide_real_owner_heading(opener: str) -> None:
    raw = f"# Source\n{opener}\n## Why Saved\nowner-context\n## Public\nVisible after\n".encode()
    assert normalize_saved_markdown(raw) == f"# Source\n{opener}\n## Public\nVisible after\n"


@pytest.mark.parametrize("heading,definition", [
    ("## [Why Saved][owner]", "[owner]: https://example.test"),
    ("## [Why Saved][]", "[Why Saved]: https://example.test"),
    ("## [Why Saved]", "[Why Saved]: https://example.test"),
    ("## ![**Why Saved**](https://example.test/image)", ""),
    ("## <em>Why Saved</em>", ""),
    ("## Why<!-- owner label --> Saved", ""),
    ("## Why<br>Saved", ""),
    ("## Why<br />Saved", ""),
])
@pytest.mark.parametrize("definition_first", [False, True])
def test_owner_heading_semantics_preserve_reference_context_and_visible_labels(
    heading: str, definition: str, definition_first: bool,
) -> None:
    reference = definition + "\n\n" if definition else ""
    # A reference definition cannot interrupt the preceding paragraph.
    prefix = "# Source\nVisible **body**\n" + ("\n" + reference if definition_first else "")
    suffix = "## Public\nVisible after\n" + ("\n" + reference if not definition_first else "")
    raw = f"{prefix}{heading}\nowner-canary\n{suffix}".encode()
    assert normalize_saved_markdown(raw) == (prefix + suffix).strip() + "\n"


@pytest.mark.parametrize("heading", [
    "## <span class=owner>Why Saved</span>",
    "## <em style=display:none>Why Saved</em>",
    "## <script>Why Saved</script>",
    "## <custom>Why Saved</custom>",
    "## <?label Why Saved?>",
    "## Public <!FOO bar>",
    "## Why<!FOO bar> Saved",
    "## Why<!DOCTYPE html> Saved",
    "## Why<![CDATA[hidden]]> Saved",
])
def test_ambiguous_html_heading_is_refused_without_intake(heading: str) -> None:
    with pytest.raises(ConnectorContractError, match="saved_markdown_unsupported_heading_markup"):
        normalize_saved_markdown(f"# Source\n{heading}\nowner-canary\n".encode())


def test_public_marked_up_heading_and_fenced_owner_example_are_preserved() -> None:
    raw = (
        "# Source\n## <em>Public details</em>\nRetained **body**\n"
        "```markdown\n## [Why Saved][owner]\npublic-example\n```\n"
        "\n[owner]: https://example.test\n"
    )
    assert normalize_saved_markdown(raw.encode()) == raw


@pytest.mark.parametrize("heading", [
    "## <span class=owner>Why Saved</span>", "## Why<!FOO bar> Saved",
])
def test_ambiguous_heading_adapter_has_no_identity_or_intake(
    tmp_path: Path, heading: str,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "owner.md").write_text(
        f"# Source\n{heading}\nowner-canary\n", encoding="utf-8",
    )
    candidate, = _adapter(root).dry_run().candidates
    assert candidate.refusal_code == "saved_markdown_unsupported_heading_markup"
    assert candidate.identity is None and candidate.intake is None


def test_unresolved_reference_heading_is_not_invented_as_a_link() -> None:
    raw = "# Source\n## [Why Saved][missing]\nLiteral reference example\n"
    assert normalize_saved_markdown(raw.encode()) == raw


def test_non_ascii_whitespace_does_not_close_owner_section_fence() -> None:
    raw = (
        "# Source\nVisible before\n## Why Saved\n```\n```\u00a0\n"
        "## Still fenced\nowner-context\n```\n## Public\nVisible after\n"
    ).encode()
    assert normalize_saved_markdown(raw) == "# Source\nVisible before\n## Public\nVisible after\n"


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


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_scan_epoch_resumes_exact_decomposed_paths_after_restart(
    tmp_path: Path, kind: str,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    for index in range(30):
        (root / f"item-{index:03}.md").write_text(f"# Synthetic item {index}\n", encoding="utf-8")
    decomposed = "z-cafe\u0301"
    relative = decomposed + ".md" if kind == "file" else decomposed + "/saved.md"
    if kind == "directory":
        (root / decomposed).mkdir()
    (root / relative).write_text("# Decomposed filename\nsynthetic body\n", encoding="utf-8")
    adapter = _adapter(root)
    page = adapter.scan_page(adapter.begin_epoch())
    assert len(page.candidates) == 25
    assert not page.complete
    if kind == "file":
        assert relative not in [candidate.relative_path for candidate in page.candidates]
    candidates = list(page.candidates)
    for _ in range(3):
        # JSON custody and a new adapter model a process restart without renaming any file.
        saved = json.loads(json.dumps(page.epoch.value()))
        epoch = SavedMarkdownScanEpoch.from_value(saved)
        assert epoch.value() == saved
        page = _adapter(root).scan_page(epoch)
        candidates.extend(page.candidates)
        if page.complete:
            break
    else:
        pytest.fail("synthetic resumed scan did not complete within three pages")
    assert len(candidates) == len({candidate.relative_path for candidate in candidates}) == 31
    assert all(candidate.accepted for candidate in candidates)
    candidate = next(candidate for candidate in candidates if candidate.relative_path == relative)
    assert candidate.identity is not None
    logical = unicodedata.normalize("NFC", relative)
    assert logical != relative
    assert candidate.identity.relative_item_identity == logical
    assert candidate.identity.item_id == adapter.item_identity(logical)
    files = page.epoch.state["files"]
    assert isinstance(files, dict) and relative in files
    assert (root / relative).read_text(encoding="utf-8") == (
        "# Decomposed filename\nsynthetic body\n"
    )


def test_completed_scan_epoch_deserializes_decomposed_file_and_directory(tmp_path: Path) -> None:
    root = tmp_path / "selected"
    relative = "cafe\u0301/entre\u0301e.md"
    (root / "cafe\u0301").mkdir(parents=True)
    (root / relative).write_text("# Synthetic decomposed path\n", encoding="utf-8")
    adapter = _adapter(root)
    page = adapter.scan_page(adapter.begin_epoch())
    assert page.complete and len(page.candidates) == 1
    assert page.candidates[0].accepted
    saved = json.loads(json.dumps(page.epoch.value()))
    restored = SavedMarkdownScanEpoch.from_value(saved)
    assert restored.value() == saved
    files, directories = restored.state["files"], restored.state["directories"]
    assert isinstance(files, dict) and relative in files
    assert isinstance(directories, dict) and "cafe\u0301" in directories
    resumed = _adapter(root).scan_page(restored)
    assert resumed.complete and resumed.candidates == ()


@pytest.mark.parametrize("field", ["work", "directories", "files"])
@pytest.mark.parametrize("unsafe", ["/outside.md", "../outside.md", "cafe\u0301/../x.md",
                                    "cafe\u0301\n.md"])
def test_scan_epoch_rejects_unsafe_physical_paths(
    tmp_path: Path, field: str, unsafe: str,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    epoch = _adapter(root).begin_epoch()
    if field == "work":
        epoch.state[field] = [["file", unsafe]]
    else:
        epoch.state[field] = {unsafe: []}
    with pytest.raises(ConnectorContractError, match="invalid saved markdown"):
        SavedMarkdownScanEpoch.from_value(epoch.value())


def test_scan_epoch_refuses_normalized_filename_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    decomposed, composed = "cafe\u0301.md", "caf\u00e9.md"
    (root / decomposed).write_text("# Synthetic collision\n", encoding="utf-8")
    entries = []
    for name in (decomposed, composed):
        entry = Mock(spec=os.DirEntry)
        entry.name = name
        entry.is_dir.return_value = False
        entries.append(entry)
    # Some filesystems alias NFC/NFD names. Supply both raw directory entries
    # deterministically so their shared logical identity must refuse either body.
    snapshot = MagicMock()
    snapshot.__enter__.return_value = iter(entries)
    monkeypatch.setattr(os, "scandir", Mock(return_value=snapshot))
    adapter = _adapter(root)
    page = adapter.scan_page(adapter.begin_epoch())
    assert not page.complete
    assert {candidate.relative_path for candidate in page.candidates} == {decomposed, composed}
    assert all(candidate.refusal_code == "identity_collision" for candidate in page.candidates)
    assert all(candidate.intake is None and candidate.identity is None
               for candidate in page.candidates)
    restored = SavedMarkdownScanEpoch.from_value(json.loads(json.dumps(page.epoch.value())))
    assert restored.value() == page.epoch.value()


def test_saved_markdown_observation_retains_raw_transformed_and_admitted_hashes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    raw = ("---\r\nowner: synthetic\r\n---\r\n# Cafe\u0301\r\nbody\r\n"
           "\r\n# Why Saved\r\nprivate\r\n").encode()
    (root / "saved.md").write_bytes(raw)
    candidate = _adapter(root).dry_run().candidates[0]
    assert candidate.intake is not None
    assert candidate.identity is not None
    intake = candidate.intake
    observation = intake.observation
    assert observation is not None
    transformed = normalize_saved_markdown(raw)
    assert "\r" not in transformed and "private" not in transformed
    assert transformed != intake.text
    assert intake.text == unicodedata.normalize("NFC", transformed)
    assert observation.value() == {
        "dto_version": 1,
        "original_sha256": sha256(raw).hexdigest(),
        "transformed_sha256": sha256(transformed.encode()).hexdigest(),
        "normalization_version": candidate.identity.normalization_version,
        "privacy_policy_version": intake.privacy.policy_version,
        "privacy_policy_sha256": sha256(
            portable_canonical_json_bytes(intake.privacy.to_dict())).hexdigest(),
        "admitted_payload_sha256": sha256(
            portable_canonical_json_bytes(intake.payload().to_dict())).hexdigest(),
    }
    assert len({observation.original_sha256, observation.transformed_sha256,
                observation.admitted_payload_sha256}) == 3


def test_complete_privacy_change_same_version_changes_revision_not_logical_item(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "saved.md").write_text("# Synthetic policy binding\n", encoding="utf-8")
    first = _adapter(root).dry_run().candidates[0]
    privacy = PrivacyDecision.from_dict({
        "tier": "work", "reason": "policy_work", "policy_version": "policy-v1",
        "authority": {"cloud": False, "external_egress": False}, "confirmation_ref": None,
    })
    second = _adapter(root, privacy=privacy).dry_run().candidates[0]
    assert first.identity is not None and second.identity is not None
    assert first.intake is not None and second.intake is not None
    assert first.identity.privacy_policy_version == second.identity.privacy_policy_version
    assert first.identity.item_id == second.identity.item_id
    assert first.intake.key.external_id == second.intake.key.external_id
    assert first.identity.revision_id != second.identity.revision_id
    assert first.identity.delivery_id != second.identity.delivery_id
    assert first.intake.observation is not None and second.intake.observation is not None
    assert first.intake.observation.privacy_policy_sha256 != (
        second.intake.observation.privacy_policy_sha256
    )


def test_saved_markdown_legacy_explicit_identity_fingerprints_remain_v1() -> None:
    identity = SavedMarkdownIdentity("destination:synthetic", "source:synthetic", "saved.md",
                                     "a" * 64, "b" * 64, "normalization-v1", "policy-v1")
    assert identity.item_id == "239cc19addc59e6eee24bb523feebc8bc84813568724dde65577e711bbb8d56d"
    assert identity.revision_id == (
        "bad21123951e29516cc9928283710763f33f21a8567db0da658cd7fa010aaf67"
    )
    assert identity.delivery_id == (
        "saved-markdown.34be5cf59984a0f8811e2c01cba47e2308d6d837e2bfbf114df3f4a5a1d66655"
    )


@pytest.mark.parametrize("count", [4096, 4097])
def test_scan_epoch_directory_inventory_quota_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    entries = []
    for index in range(count):
        entry = Mock(spec=os.DirEntry)
        entry.name = f"item-{index:04}.md"
        entry.is_dir.return_value = False
        entries.append(entry)
    snapshot = MagicMock()
    snapshot.__enter__.return_value = iter(entries)
    monkeypatch.setattr(os, "scandir", Mock(return_value=snapshot))
    adapter = _adapter(root)
    page = adapter.scan_page(adapter.begin_epoch(), max_entries=1)
    assert not page.complete and page.evidence_sha256 is None
    assert page.candidates == ()
    work = page.epoch.state["work"]
    assert isinstance(work, list)
    if count == 4096:
        assert len(work) == 4096 and page.epoch.state["errors"] == []
    else:
        assert work == [] and page.epoch.state["errors"] == ["enumeration_failed"]


def _adapter(
    root: Path,
    *,
    destination: str = "destination:synthetic",
    policy_version: str = "policy-v1",
    limits: SavedMarkdownLimits | None = None,
    privacy: PrivacyDecision | None = None,
) -> SavedMarkdownRootAdapter:
    return SavedMarkdownRootAdapter(
        root,
        destination_identity=destination,
        accepted_source_identity="source:synthetic",
        source_reference="https://saved.example.test/library",
        privacy=privacy or PrivacyDecision.from_dict(
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
