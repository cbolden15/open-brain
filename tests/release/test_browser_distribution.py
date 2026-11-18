"""The optional browser candidate owns only its private registration and artifacts."""

import json
import zipfile
from pathlib import Path

import pytest

from open_brain_browser_host import policy
from tools.open_brain_dev import browser_distribution

ROOT = Path(__file__).resolve().parents[2]


def test_store_candidate_is_reproducible_and_contains_only_release_files(tmp_path: Path) -> None:
    first, second = (tmp_path / name for name in ("first.zip", "second.zip"))
    assert browser_distribution.package_extension(
        ROOT, first
    ) == browser_distribution.package_extension(ROOT, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert browser_distribution.extension_id(manifest) == "gdeinollcipkdcfglknalgijfkhiojhn"
        assert set(archive.namelist()) == {
            p.name for p in (ROOT / "packages/browser-extension/extension").iterdir() if p.is_file()
        } | {"LICENSE", "NOTICE"}


def test_registration_owns_only_verified_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    installation = policy.Policy("a" * 32, tmp_path / "brain", "0" * 64, {})
    profile = tmp_path / "chrome"
    old, new = (tmp_path / name for name in ("v1", "v2"))
    for exe in (old, new):
        exe.write_text("fixture")
        exe.chmod(0o700)
    manifest = policy.install(installation, old, user_data_dir=profile)
    with policy.lock_setup(create=False) as fd:
        assert policy.registration_status(fd, profile)["state"] == "registered"
    assert policy.install(installation, new, user_data_dir=profile) == manifest
    assert json.loads(manifest.read_bytes())["path"] == str(new)
    manifest.write_text('{"name":"another app"}')
    with pytest.raises(ValueError), policy.lock_setup(create=False) as fd:
        policy.unregister(fd, profile)
    with pytest.raises(ValueError):
        policy.install(installation, new, user_data_dir=profile)
    assert json.loads(manifest.read_bytes()) == {"name": "another app"}


def test_removal_preserves_the_profile_and_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    installation = policy.Policy("a" * 32, tmp_path / "brain", "0" * 64, {})
    exe = tmp_path / "host"
    exe.touch(mode=0o700)
    profile = tmp_path / "chrome"
    manifest = policy.install(installation, exe, user_data_dir=profile)
    neighbor = manifest.with_name("other.json")
    neighbor.write_text("{}")
    with policy.lock_setup(create=False) as directory:
        assert policy.unregister(directory, profile)["state"] == "removed"
        assert policy.unregister(directory, profile)["state"] == "absent"
    assert not manifest.exists() and neighbor.is_file()


def test_reinstalled_host_recovers_lost_highlight_under_fresh_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from uuid import uuid4

    from open_brain_browser_host import setup
    from open_brain_browser_host.operations import Request, dispatch

    monkeypatch.setenv("HOME", str(tmp_path))
    args = [
        "setup",
        "--data-dir",
        str(tmp_path / "brain"),
        "--extension-id",
        "a" * 32,
        "--tier",
        "public",
        "--tier",
        "work",
        "--tier",
        "personal",
        "--unassigned",
        "--complete-domain",
        "--operation",
        "save",
        "--operation",
        "lookup",
        "--operation",
        "status",
    ]
    assert setup.main([*args, "--initialize-brain", "--new-source"]) == 0
    original = json.loads(capsys.readouterr().out)
    old = policy.load()
    op, child, saved = map(str, (uuid4(), uuid4(), uuid4()))
    url = "https://fixture.invalid/cedar"
    parent = dispatch(old, Request("save", saved, {"url": url, "reason": "synthetic cedar"}))
    add = Request(
        "annotation.add",
        op,
        {
            "url": url,
            "capture_id": parent["capture_id"],
            "annotation_id": child,
            "quote": "cedar bark",
            "prefix": "",
            "suffix": "",
        },
    )
    wanted = dispatch(old, add)
    executable = tmp_path / "native"
    executable.touch(mode=0o700)
    profile = tmp_path / "chrome"
    policy.install(old, executable, user_data_dir=profile)
    with policy.lock_setup(create=False) as directory:
        assert policy.unregister(directory, profile)["state"] == "removed"
    assert (
        setup.main(
            [
                *args,
                "--source",
                *original["source"][1:],
                "--workflow-principal",
                original["workflow_principal"],
            ]
        )
        == 0
    )
    fresh = policy.load()
    assert fresh.token != old.token
    recover = Request.parse(
        {
            "version": "browser.v1",
            "operation": "operation.list",
            "args": {"after": None, "limit": 1},
        }
    )
    with pytest.raises(PermissionError):
        dispatch(old, recover)
    first = dispatch(fresh, recover)
    second = dispatch(
        fresh, Request("operation.list", "", {"after": first["next_after"], "limit": 1})
    )
    assert second["next_after"] is None
    recovered = [*first["intents"], *second["intents"]]  # type: ignore[misc]
    assert {row["id"] for row in recovered if row["state"] == "accepted"} == {op, saved}
    assert dispatch(fresh, Request("status", op, {})) == wanted == dispatch(fresh, add)
    rows = dispatch(
        fresh,
        Request(
            "annotation.list",
            "",
            {"url": url, "capture_id": parent["capture_id"], "limit": 10, "offset": 0},
        ),
    )
    assert len(rows["annotations"]) == 1  # type: ignore[arg-type]
    assert setup.main([*args, "--new-source"]) == 0
    assert dispatch(policy.load(), recover) == {"intents": [], "next_after": None}
