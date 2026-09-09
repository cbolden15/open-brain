from __future__ import annotations

import json
from dataclasses import fields
from hashlib import sha256
from pathlib import Path
from subprocess import run
from typing import Any

import pytest
from pytest import CaptureFixture

from tools.open_brain_dev.public_history_audit import HistoryFinding, audit_history, main
from tools.open_brain_dev.release_audit import audit


def git(repository: Path, *args: str) -> str:
    result = run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def write_history_allowlist(
    repository: Path,
    *,
    blob_sha256: str,
    path: str,
    rule: str,
) -> None:
    release = repository / "release"
    release.mkdir(exist_ok=True)
    (release / "public-history-allowlist.json").write_text(
        json.dumps(
            {
                "policy_version": 1,
                "entries": [
                    {
                        "blob_sha256": blob_sha256,
                        "path": path,
                        "reason": "reviewed-public-fixture-redacted-in-current-tree",
                        "rule": rule,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_history_audit_reports_only_commit_path_and_rule_for_removed_content(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    canary = "synthetic-private-canary"
    denylist.write_text(f"{canary}\n", encoding="utf-8")
    removed_path = repository / "removed.txt"
    removed_path.write_text(canary, encoding="utf-8")
    git(repository, "add", "removed.txt")
    git(repository, "commit", "-m", "add synthetic fixture")
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "rm", "removed.txt")
    git(repository, "commit", "-m", "remove synthetic fixture")

    findings = audit_history(repository, denylist)

    assert findings == [
        HistoryFinding(commit=commit, path="removed.txt", rule="private-denylist-term")
    ]
    assert [field.name for field in fields(HistoryFinding)] == ["commit", "path", "rule"]
    assert main(["--repository", str(repository), "--private-denylist", str(denylist)]) == 1
    assert canary not in capsys.readouterr().out


def test_history_audit_redacts_denylisted_path_components(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    canary = "synthetic-owner-name"
    denylist.write_text(f"{canary}\n", encoding="utf-8")
    secret_path = repository / f"{canary}-notes.txt"
    secret_path.write_text(canary, encoding="utf-8")
    git(repository, "add", secret_path.name)
    git(repository, "commit", "-m", "add redaction fixture")

    findings = audit_history(repository, denylist)

    assert findings and all(canary not in finding.path for finding in findings)
    assert all(finding.path.startswith("<redacted-path:") for finding in findings)
    assert main(["--repository", str(repository), "--private-denylist", str(denylist)]) == 1
    assert canary not in capsys.readouterr().out


def test_history_audit_fails_closed_for_oversized_and_binary_content(tmp_path: Path) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    canary = "synthetic-private-canary"
    denylist.write_text(f"{canary}\n", encoding="utf-8")
    (repository / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (repository / "binary.bin").write_bytes(b"prefix\x00" + canary.encode("utf-8"))
    git(repository, "add", "large.bin", "binary.bin")
    git(repository, "commit", "-m", "add bounded scan fixtures")

    rules = {finding.rule for finding in audit_history(repository, denylist)}

    assert "content-scan-limit-exceeded" in rules
    assert "private-denylist-term" in rules


def test_history_audit_accepts_explicit_no_additional_project_terms_marker(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("# no additional project terms\n", encoding="utf-8")
    (repository / "safe.txt").write_text("synthetic public fixture", encoding="utf-8")
    git(repository, "add", "safe.txt")
    git(repository, "commit", "-m", "add synthetic fixture")

    assert audit_history(repository, denylist) == []
    assert main(["--repository", str(repository), "--private-denylist", str(denylist)]) == 0


def test_history_audit_limits_fail_closed(tmp_path: Path) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("synthetic-private-canary\n", encoding="utf-8")
    (repository / "one.txt").write_text("synthetic", encoding="utf-8")
    git(repository, "add", "one.txt")
    git(repository, "commit", "-m", "add limit fixture")

    with pytest.raises(ValueError, match="commit limit"):
        audit_history(repository, denylist, maximum_commits=0)
    with pytest.raises(ValueError, match="blob limit"):
        audit_history(repository, denylist, maximum_blobs=0)
    with pytest.raises(ValueError, match="byte limit"):
        audit_history(repository, denylist, maximum_bytes=0)


def test_history_allowlist_suppresses_only_the_exact_reviewed_blob(tmp_path: Path) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("unrelated-history-canary\n", encoding="utf-8")
    private_ip = ".".join(("192", "168", "1", "10"))
    path = "reviewed.txt"
    (repository / path).write_text(private_ip, encoding="utf-8")
    git(repository, "add", path)
    git(repository, "commit", "-m", "add reviewed fixture")
    git(repository, "rm", path)
    git(repository, "commit", "-m", "remove reviewed fixture")
    digest = sha256(private_ip.encode()).hexdigest()
    write_history_allowlist(
        repository,
        blob_sha256=digest,
        path=path,
        rule="private-ip-address",
    )

    assert audit_history(repository, denylist) == []

    write_history_allowlist(
        repository,
        blob_sha256="0" * 64,
        path=path,
        rule="private-ip-address",
    )

    assert {finding.rule for finding in audit_history(repository, denylist)} == {
        "private-ip-address"
    }

    write_history_allowlist(
        repository,
        blob_sha256=digest,
        path=path,
        rule="private-denylist-term",
    )

    with pytest.raises(ValueError, match="allowlist rule"):
        audit_history(repository, denylist)


def test_history_allowlist_does_not_cover_the_same_blob_at_another_path(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("unrelated-history-canary\n", encoding="utf-8")
    private_ip = ".".join(("192", "168", "1", "10"))
    for path in ("reviewed.txt", "unreviewed.txt"):
        (repository / path).write_text(private_ip, encoding="utf-8")
    git(repository, "add", "reviewed.txt", "unreviewed.txt")
    git(repository, "commit", "-m", "add path-bound fixtures")
    git(repository, "rm", "reviewed.txt", "unreviewed.txt")
    git(repository, "commit", "-m", "remove path-bound fixtures")
    write_history_allowlist(
        repository,
        blob_sha256=sha256(private_ip.encode()).hexdigest(),
        path="reviewed.txt",
        rule="private-ip-address",
    )

    findings = audit_history(repository, denylist)

    assert {finding.path for finding in findings} == {"unreviewed.txt"}
    assert {finding.rule for finding in findings} == {"private-ip-address"}


def test_history_allowlist_rejects_inexact_or_unsafe_entries(tmp_path: Path) -> None:
    repository = tmp_path / "synthetic-history"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("synthetic-history-canary\n", encoding="utf-8")
    (repository / "safe.txt").write_text("synthetic", encoding="utf-8")
    git(repository, "add", "safe.txt")
    git(repository, "commit", "-m", "add safe fixture")
    write_history_allowlist(
        repository,
        blob_sha256="0" * 64,
        path="../safe.txt",
        rule="absolute-home-path",
    )

    with pytest.raises(ValueError, match="allowlist path"):
        audit_history(repository, denylist)


def private_history(
    tmp_path: Path, *, extra: str = ""
) -> tuple[Path, Path, str, dict[str, Any]]:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Synthetic Test")
    git(repository, "config", "user.email", "synthetic@example.invalid")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("synthetic-history-token\ncafé\n")
    payload = "synthetic-history-token" + extra
    (repository / "reviewed.txt").write_text(payload)
    for name in ("LICENSE", "NOTICE"):
        (repository / name).write_text("Synthetic fixture")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "record synthetic historical fixture")
    reviewed = git(repository, "rev-parse", "HEAD")
    git(repository, "rm", "reviewed.txt")
    git(repository, "commit", "-m", "remove synthetic historical fixture")
    policy = {
        "policy_version": 2,
        "entries": [
            {
                "blob_sha256": sha256(payload.encode()).hexdigest(),
                "path": "reviewed.txt",
                "rule": "private-denylist-term",
                "reason": "owner-reviewed-synthetic-history",
                "reviewed_commits": [reviewed],
                "normalized_denylist_sha256": sha256(
                    b'["caf\\u00e9","synthetic-history-token"]'
                ).hexdigest(),
            }
        ],
    }
    (repository / "release").mkdir()
    save_private_policy(repository, policy)
    return repository, denylist, payload, policy


def save_private_policy(repository: Path, policy: dict[str, Any]) -> None:
    (repository / "release/public-history-allowlist.json").write_text(json.dumps(policy))


def test_reviewed_private_history_is_exception_free_in_current_tree_and_archive(
    tmp_path: Path,
) -> None:
    import zipfile

    repository, denylist, payload, _ = private_history(tmp_path)
    assert audit_history(repository, denylist) == []
    assert audit(repository, denylist) == []
    (repository / "reviewed.txt").write_text(payload)
    assert {finding.rule for finding in audit(repository, denylist)} == {"private-denylist-term"}
    (repository / "reviewed.txt").unlink()
    archive = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("reviewed.txt", payload)
    assert {finding.rule for finding in audit(repository, denylist, [archive])} == {
        "private-denylist-term"
    }


@pytest.mark.parametrize("field", ("blob_sha256", "path", "reviewed_commits"))
def test_private_history_approval_requires_exact_blob_path_and_commit(
    tmp_path: Path, field: str
) -> None:
    repository, denylist, _, policy = private_history(tmp_path)
    entry = policy["entries"][0]
    entry[field] = {
        "blob_sha256": "0" * 64,
        "path": "another.txt",
        "reviewed_commits": ["0" * 40],
    }[field]
    save_private_policy(repository, policy)
    findings = audit_history(repository, denylist)
    assert len(findings) == 1
    assert findings[0].rule == "private-denylist-term"


@pytest.mark.parametrize("new_path", ("reviewed.txt", "copied.txt"))
def test_later_reintroduction_of_identical_private_blob_is_not_approved(
    tmp_path: Path, new_path: str
) -> None:
    repository, denylist, payload, _ = private_history(tmp_path)
    (repository / new_path).write_text(payload)
    git(repository, "add", new_path)
    git(repository, "commit", "-m", "reintroduce synthetic fixture")
    later = git(repository, "rev-parse", "HEAD")
    git(repository, "rm", new_path)
    git(repository, "commit", "-m", "remove reintroduced fixture")
    assert audit_history(repository, denylist) == [
        HistoryFinding(later, new_path, "private-denylist-term")
    ]


def test_private_history_approval_does_not_suppress_other_rules(tmp_path: Path) -> None:
    repository, denylist, _, _ = private_history(
        tmp_path, extra=" " + ".".join(("192", "168", "1", "10"))
    )
    findings = audit_history(repository, denylist)
    assert len(findings) == 1
    assert findings[0].rule == "private-ip-address"


def test_private_history_fingerprint_uses_semantic_normalized_term_set(tmp_path: Path) -> None:
    repository, denylist, _, _ = private_history(tmp_path)
    denylist.write_text("# comment\nCAFE\u0301\nSYNTHETIC-HISTORY-TOKEN\ncafé\n\n")
    assert audit_history(repository, denylist) == []


@pytest.mark.parametrize(
    "terms", ("café\n", "café\nsynthetic-history-token\nadded-term\n", "changed-term\n")
)
def test_semantic_denylist_changes_invalidate_history_approval(tmp_path: Path, terms: str) -> None:
    repository, denylist, _, _ = private_history(tmp_path)
    denylist.write_text(terms)
    with pytest.raises(ValueError, match="approval fingerprint"):
        audit_history(repository, denylist)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_fingerprint",
        "bad_fingerprint",
        "missing_commits",
        "empty_commits",
        "bad_commit",
        "duplicate_commit",
        "too_many_commits",
        "unknown_field",
        "unsupported_rule",
        "version_one",
        "missing_version",
    ),
)
def test_private_history_policy_fails_closed_on_invalid_metadata(
    tmp_path: Path, mutation: str
) -> None:
    repository, denylist, _, policy = private_history(tmp_path)
    entry = policy["entries"][0]
    if mutation == "missing_fingerprint":
        del entry["normalized_denylist_sha256"]
    elif mutation == "bad_fingerprint":
        entry["normalized_denylist_sha256"] = "0" * 64
    elif mutation == "missing_commits":
        del entry["reviewed_commits"]
    elif mutation == "empty_commits":
        entry["reviewed_commits"] = []
    elif mutation == "bad_commit":
        entry["reviewed_commits"] = ["HEAD"]
    elif mutation == "duplicate_commit":
        entry["reviewed_commits"] *= 2
    elif mutation == "too_many_commits":
        entry["reviewed_commits"] *= 257
    elif mutation == "unknown_field":
        entry["unrecognized"] = True
    elif mutation == "unsupported_rule":
        entry["rule"] = "credential-assignment"
    elif mutation == "version_one":
        policy["policy_version"] = 1
    else:
        del policy["policy_version"]
    save_private_policy(repository, policy)
    with pytest.raises(ValueError):
        audit_history(repository, denylist)
