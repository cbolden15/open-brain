from __future__ import annotations

import shutil
import stat
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from tools.open_brain_dev.release_audit import _path_rules, audit, main


def write_safe_tree(root: Path) -> Path:
    root.mkdir()
    (root / "LICENSE").write_text("synthetic license", encoding="utf-8")
    (root / "NOTICE").write_text("synthetic notice", encoding="utf-8")
    (root / "README.md").write_text("synthetic public fixture", encoding="utf-8")
    denylist = root.parent / "denylist.txt"
    denylist.write_text("owner-private-canary\n", encoding="utf-8")
    return denylist


def test_safe_tree_passes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)

    assert audit(root, denylist) == []


def test_loopback_address_is_public_safe_configuration(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    (root / "bind.txt").write_text("127.0.0.1", encoding="utf-8")

    assert audit(root, denylist) == []


def test_dynamic_credential_option_name_is_public_safe(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    (root / "provider.py").write_text(
        'client = constructor(**{"api" + "_key": credential})\n',
        encoding="utf-8",
    )

    assert audit(root, denylist) == []


@pytest.mark.parametrize(
    ("relative_path", "body", "rule"),
    [
        ("content/note.md", "synthetic", "forbidden-path-family"),
        ("example.env", "synthetic", "forbidden-file-type"),
        (
            "example.md",
            "/" + "/".join(["Users", "example", "private", "file"]),
            "absolute-home-path",
        ),
        (
            "example.md",
            "http://" + ".".join(["192", "168", "1", "10"]) + "/item",
            "private-ip-address",
        ),
        ("example.md", "owner-" + "private-canary", "private-denylist-term"),
        ("example.md", "api" + "_key=" + "abcdefghijklmnop", "credential-assignment"),
    ],
)
def test_tree_canaries_fail(tmp_path: Path, relative_path: str, body: str, rule: str) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    assert rule in {finding.rule for finding in audit(root, denylist)}


def test_public_portable_fixture_paths_are_narrowly_allowed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    fixture = root / "tests/fixtures/portable-brain/v1/brain-root/content/spaces/demo/_space.md"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("synthetic public conformance fixture", encoding="utf-8")

    assert audit(root, denylist) == []

    private_content = root / "other/content/private.md"
    private_content.parent.mkdir(parents=True)
    private_content.write_text("synthetic", encoding="utf-8")
    assert "forbidden-path-family" in {finding.rule for finding in audit(root, denylist)}


def test_committed_markdown_import_fixture_is_regular_and_public_safe(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "examples/markdown-fixture"
    for path in source.rglob("*"):
        mode = path.lstat().st_mode
        assert not stat.S_ISLNK(mode)
        assert stat.S_ISDIR(mode) or stat.S_ISREG(mode)

    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    shutil.copytree(source, root / "examples/markdown-fixture")

    assert audit(root, denylist) == []


def test_engine_source_portable_fixture_paths_are_narrowly_allowed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    fixture = (
        root
        / "packages/engine/src/open_brain_engine/portable/conformance/v1/brain-root"
        / "sources/captures/capture.json"
    )
    fixture.parent.mkdir(parents=True)
    fixture.write_text("synthetic public conformance fixture", encoding="utf-8")

    assert audit(root, denylist) == []


def test_legacy_synthetic_vault_exception_suppresses_only_its_vault_path_part(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    allowed = root / "examples/synthetic-vault/vault/note.md"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("synthetic public fixture", encoding="utf-8")

    assert audit(root, denylist) == []

    adjacent = root / "examples/customer-vault/vault/note.md"
    adjacent.parent.mkdir(parents=True)
    adjacent.write_text("synthetic", encoding="utf-8")
    forbidden_type = root / "examples/synthetic-vault/vault/state.sqlite3"
    forbidden_type.write_text("synthetic", encoding="utf-8")
    sensitive = root / "examples/synthetic-vault/vault/sensitive.md"
    sensitive.write_text("api" + "_key=" + "abcdefghijklmnop", encoding="utf-8")

    findings = {(finding.location, finding.rule) for finding in audit(root, denylist)}
    assert ("examples/customer-vault/vault/note.md", "forbidden-path-family") in findings
    assert "forbidden-path-family" in {
        finding.rule for finding in _path_rules("EXAMPLES/SYNTHETIC-VAULT/vault/note.md")
    }
    assert ("examples/synthetic-vault/vault/state.sqlite3", "forbidden-file-type") in findings
    assert ("examples/synthetic-vault/vault/sensitive.md", "credential-assignment") in findings


def test_oversized_content_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    (root / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    assert "content-scan-limit-exceeded" in {finding.rule for finding in audit(root, denylist)}


def test_denylist_matches_unicode_case_and_normalization(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    denylist.write_text("caf" + chr(0x00E9) + "\n", encoding="utf-8")
    (root / "example.md").write_text("CAFE" + chr(0x0301), encoding="utf-8")

    assert "private-denylist-term" in {finding.rule for finding in audit(root, denylist)}


def test_missing_or_empty_denylist_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    denylist.unlink()

    assert main(["--root", str(root), "--private-denylist", str(denylist)]) == 2

    denylist.write_text("# no terms\n", encoding="utf-8")
    assert main(["--root", str(root), "--private-denylist", str(denylist)]) == 2


def test_explicit_no_additional_project_terms_marker_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    denylist.write_text("# no additional project terms\n", encoding="utf-8")

    assert audit(root, denylist) == []
    assert main(["--root", str(root), "--private-denylist", str(denylist)]) == 0


def test_tree_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    target = tmp_path / "outside.md"
    target.write_text("synthetic", encoding="utf-8")
    (root / "linked.md").symlink_to(target)

    assert "symlink-not-allowed" in {finding.rule for finding in audit(root, denylist)}


def test_zip_member_name_and_content_are_scanned(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    artifact = tmp_path / "release.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("package/content/private.md", "owner-private-canary")

    rules = {finding.rule for finding in audit(root, denylist, [artifact])}
    assert "forbidden-path-family" in rules
    assert "private-denylist-term" in rules


def test_packaged_portable_fixture_paths_are_narrowly_allowed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    artifact = tmp_path / "release.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "open_brain_engine/portable/conformance/v1/brain-root/content/spaces/demo/_space.md",
            "synthetic public conformance fixture",
        )
        archive.writestr(
            "open_brain_engine/portable/conformance/v1/brain-root/sources/captures/capture.json",
            "synthetic public conformance fixture",
        )

    assert audit(root, denylist, [artifact]) == []

    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr(
            "open_brain_engine/portable/conformance/v2/brain-root/content/private.md",
            "synthetic",
        )
    assert "forbidden-path-family" in {
        finding.rule for finding in audit(root, denylist, [artifact])
    }


def test_packaged_legacy_synthetic_vault_exception_is_exact(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    artifact = tmp_path / "release.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "examples/synthetic-vault/vault/note.md",
            "synthetic public fixture",
        )

    assert audit(root, denylist, [artifact]) == []

    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("examples/customer-vault/vault/note.md", "synthetic")
        archive.writestr("EXAMPLES/SYNTHETIC-VAULT/vault/note.md", "synthetic")
        archive.writestr("examples/synthetic-vault/vault/state.sqlite3", "synthetic")
        archive.writestr(
            "examples/synthetic-vault/vault/sensitive.md",
            "api" + "_key=" + "abcdefghijklmnop",
        )
    findings = {(finding.location, finding.rule) for finding in audit(root, denylist, [artifact])}
    assert (
        "release.whl:examples/customer-vault/vault/note.md",
        "forbidden-path-family",
    ) in findings
    assert (
        "release.whl:EXAMPLES/SYNTHETIC-VAULT/vault/note.md",
        "forbidden-path-family",
    ) in findings
    assert (
        "release.whl:examples/synthetic-vault/vault/state.sqlite3",
        "forbidden-file-type",
    ) in findings
    assert (
        "release.whl:examples/synthetic-vault/vault/sensitive.md",
        "credential-assignment",
    ) in findings


def test_zip_traversal_name_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    artifact = tmp_path / "release.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("../outside.md", "synthetic")

    assert "unsafe-archive-path" in {finding.rule for finding in audit(root, denylist, [artifact])}


def test_tar_member_content_is_scanned(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    payload = tmp_path / "payload.md"
    payload.write_text("http://" + ".".join(["10", "0", "0", "1"]) + "/item", encoding="utf-8")
    artifact = tmp_path / "release.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(payload, arcname="package/payload.md")

    rules = {finding.rule for finding in audit(root, denylist, [artifact])}
    assert "private-ip-address" in rules


def test_tar_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    denylist = write_safe_tree(root)
    artifact = tmp_path / "release.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        member = tarfile.TarInfo("package/linked.md")
        member.type = tarfile.SYMTYPE
        member.linkname = "../outside.md"
        archive.addfile(member, BytesIO())

    assert "symlink-not-allowed" in {finding.rule for finding in audit(root, denylist, [artifact])}
