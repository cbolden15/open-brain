from __future__ import annotations

import fcntl
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

ORIGIN = "chrome-extension://" + "a" * 32 + "/"
URL = "https://pages.example.org/synthetic/browser"
REASON = "Synthetic copperbird browser protocol acceptance"
VERSION = "browser.v1"


def _host(home: Path) -> tuple[list[str], dict[str, str]]:
    executable = Path(sys.executable).with_name("open-brain-browser-host")
    assert executable.is_file(), "optional browser host entry point is missing"
    return [str(executable)], {**os.environ, "HOME": str(home)}


def _call(home: Path, args: list[str], *, frame: bytes | None = None) -> bytes:
    executable, env = _host(home)
    result = subprocess.run(
        [*executable, *args], env=env, input=frame, capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _frame(value: object) -> bytes:
    body = json.dumps(value).encode()
    return struct.pack("=I", len(body)) + body


def _decode(out: bytes) -> dict[str, object]:
    assert len(out) >= 4
    assert struct.unpack("=I", out[:4]) == (len(out) - 4,)
    response = cast(dict[str, object], json.loads(out[4:]))
    assert response.pop("version") == VERSION
    return response


def _request(
    home: Path, operation: str, args: dict[str, str], operation_id: str | None = None
) -> dict[str, object]:
    envelope: dict[str, object] = {"version": VERSION, "operation": operation, "args": args}
    if operation_id:
        envelope["operation_id"] = operation_id
    return _decode(_call(home, [ORIGIN], frame=_frame(envelope)))


def _setup(home: Path, root: Path, *extra: str) -> dict[str, str]:
    out = _call(
        home,
        [
            "setup",
            "--extension-id",
            "a" * 32,
            "--data-dir",
            str(root),
            "--operation",
            "lookup",
            "--operation",
            "save",
            "--operation",
            "status",
            "--tier",
            "personal",
            "--unassigned",
            *extra,
        ],
    )
    return cast(dict[str, str], json.loads(out))


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = tmp_path / "brain"
    _setup(
        home,
        root,
        "--initialize-brain",
        "--new-source",
        "--complete-domain",
        "--tier",
        "public",
        "--tier",
        "work",
    )
    return home, root


def test_accepted_stdio_save_replays_and_is_searchable(installation: tuple[Path, Path]) -> None:
    home, root = installation
    assert _request(home, "page.lookup", {"url": URL}) == {"result": {"state": "absent"}}
    operation = str(uuid4())
    saved = _request(home, "page.save", {"url": URL, "reason": REASON}, operation)
    assert "result" in saved, saved
    receipt = cast(dict[str, object], saved["result"])
    assert receipt["state"] == "accepted"
    assert _request(home, "operation.status", {}, operation) == saved
    assert _request(home, "page.save", {"url": URL, "reason": REASON}, operation) == saved
    lookup = _request(home, "page.lookup", {"url": URL})
    assert lookup == {
        "result": {"state": "saved", "capture_id": receipt["capture_id"], "space_id": None}
    }
    search = subprocess.run(
        [
            str(Path(sys.executable).with_name("open-brain")),
            "search",
            "copperbird",
            "--data-dir",
            str(root),
            "--json",
        ],
        capture_output=True,
        timeout=20,
        check=True,
    )
    hits = json.loads(search.stdout)["results"]
    assert [(hit["capture_id"], hit["trust"]) for hit in hits] == [
        (receipt["capture_id"], "third_party")
    ]
    assert REASON in hits[0]["excerpt"].replace("[copperbird]", "copperbird")


@pytest.mark.parametrize(
    ("origin", "payload", "code"),
    [
        (
            "chrome-extension://" + "b" * 32 + "/",
            _frame({"version": VERSION, "operation": "page.lookup", "args": {"url": URL}}),
            "origin_denied",
        ),
        (ORIGIN, b"bad", "invalid_frame"),
        (ORIGIN, struct.pack("=I", 262145), "frame_too_large"),
        (
            ORIGIN,
            _frame({"version": "browser.v2", "operation": "page.lookup", "args": {"url": URL}}),
            "unsupported_version",
        ),
        (
            ORIGIN,
            _frame(
                {
                    "version": VERSION,
                    "operation": "page.lookup",
                    "args": {"url": URL, "target": "/tmp"},
                }
            ),
            "invalid_request",
        ),
        (
            ORIGIN,
            _frame({"version": VERSION, "operation": "capture", "args": {}}),
            "operation_denied",
        ),
    ],
)
def test_refuses_invalid_native_request(
    installation: tuple[Path, Path], origin: str, payload: bytes, code: str
) -> None:
    home, _ = installation
    assert _decode(_call(home, [origin], frame=payload)) == {"error": {"code": code}}


def test_stalled_input_hits_the_foreground_deadline(installation: tuple[Path, Path]) -> None:
    home, _ = installation
    command, env = _host(home)
    with subprocess.Popen(
        [*command, ORIGIN], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as child:
        assert child.stdin is not None and child.stdout is not None
        child.stdin.write(struct.pack("=I", 24))
        child.stdin.flush()
        try:
            child.wait(timeout=13)
        except subprocess.TimeoutExpired:
            child.kill()
            raise
        assert _decode(child.stdout.read()) == {"error": {"code": "deadline_exceeded"}}


def test_revocation_between_lookup_and_save(installation: tuple[Path, Path]) -> None:
    home, _ = installation
    assert _request(home, "page.lookup", {"url": URL}) == {"result": {"state": "absent"}}
    _call(home, ["revoke"])
    assert _request(home, "page.save", {"url": URL, "reason": REASON}, str(uuid4())) == {
        "error": {"code": "browser_unbound"}
    }


def test_missing_unsafe_and_symlink_policy_are_refused(
    installation: tuple[Path, Path],
) -> None:
    home, _ = installation
    policy = home / ".open-brain-browser" / "policy.json"
    policy.chmod(0o644)
    assert _request(home, "page.lookup", {"url": URL}) == {"error": {"code": "browser_unbound"}}
    policy.chmod(0o600)
    backup = policy.with_name("backup")
    policy.rename(backup)
    policy.symlink_to(backup)
    assert _request(home, "page.lookup", {"url": URL}) == {"error": {"code": "browser_unbound"}}
    policy.unlink()
    assert _request(home, "page.lookup", {"url": URL}) == {"error": {"code": "browser_unbound"}}


def test_partial_domain_has_identical_public_results_for_hidden_and_absent(
    installation: tuple[Path, Path],
) -> None:
    home, root = installation
    operation = str(uuid4())
    original = _request(home, "page.save", {"url": URL, "reason": REASON}, operation)
    assert cast(dict[str, object], original["result"])["state"] == "accepted"
    metadata = json.loads((home / ".open-brain-browser" / "policy.json").read_bytes())["binding"]
    _, connection, resource = metadata["source"]
    restricted = home.parent / "partial-home"
    restricted.mkdir(mode=0o700)
    _call(
        restricted,
        [
            "setup",
            "--extension-id",
            "a" * 32,
            "--data-dir",
            str(root),
            "--operation",
            "lookup",
            "--operation",
            "save",
            "--operation",
            "status",
            "--tier",
            "public",
            "--unassigned",
            "--source",
            connection,
            resource,
            "--workflow-principal",
            metadata["workflow_principal"],
        ],
    )
    absent_url = URL + "/absent"
    assert (
        _request(restricted, "page.lookup", {"url": URL})
        == _request(restricted, "page.lookup", {"url": absent_url})
        == {"result": {"state": "absent"}}
    )
    hidden_save = _request(restricted, "page.save", {"url": URL, "reason": REASON}, str(uuid4()))
    absent_save = _request(
        restricted, "page.save", {"url": absent_url, "reason": REASON}, str(uuid4())
    )
    assert hidden_save == absent_save
    assert hidden_save != original
    assert _request(restricted, "operation.status", {}, operation) == _request(
        restricted, "operation.status", {}, str(uuid4())
    )


def test_registration_contains_one_allowed_extension(installation: tuple[Path, Path]) -> None:
    home, _ = installation
    result = json.loads(_call(home, ["install"]))
    manifest = Path(result["manifest"])
    assert manifest.is_relative_to(home)
    value = json.loads(manifest.read_bytes())
    assert value == {
        "allowed_origins": [ORIGIN],
        "description": "Open Brain saved items",
        "name": "com.open_brain.saved_items",
        "path": str(Path(sys.executable).with_name("open-brain-browser-host").resolve()),
        "type": "stdio",
    }


def test_policy_binding_mismatch_fails_before_capture(installation: tuple[Path, Path]) -> None:
    home, _ = installation
    policy = home / ".open-brain-browser" / "policy.json"
    stale = json.loads(policy.read_bytes())
    stale["binding"]["issuer_epoch"] += 1
    policy.write_text(json.dumps(stale), encoding="utf-8")
    assert _request(home, "page.save", {"url": URL, "reason": REASON}, str(uuid4())) == {
        "error": {"code": "browser_unbound"}
    }


def test_save_uses_validated_passage_and_rejects_changed_replay(
    installation: tuple[Path, Path],
) -> None:
    home, root = installation
    operation = str(uuid4())
    args = {
        "url": URL + "#synthetic-marker",
        "reason": REASON,
        "title": "Synthetic browser title",
        "passage": "A synthetic dragonfly passage",
    }
    saved = _request(home, "page.save", args, operation)
    assert "result" in saved, saved
    receipt = cast(dict[str, object], saved["result"])
    assert receipt["state"] == "accepted"
    assert _request(home, "page.save", {**args, "passage": "Changed passage"}, operation) == {
        "error": {"code": "browser_operation_conflict"}
    }
    search = subprocess.run(
        [
            str(Path(sys.executable).with_name("open-brain")),
            "search",
            "dragonfly",
            "--data-dir",
            str(root),
            "--json",
        ],
        capture_output=True,
        timeout=20,
        check=True,
    )
    hits = json.loads(search.stdout)["results"]
    assert [hit["capture_id"] for hit in hits] == [receipt["capture_id"]]
    assert args["passage"] in hits[0]["excerpt"].replace("[dragonfly]", "dragonfly")
    assert b"synthetic-marker" not in search.stdout


def test_narrowing_installation_revokes_old_grant_only(
    installation: tuple[Path, Path],
) -> None:
    home, root = installation
    policy = home / ".open-brain-browser" / "policy.json"
    previous = policy.read_bytes()
    _, connection, resource = json.loads(previous)["binding"]["source"]
    independent = home.parent / "independent-home"
    independent.mkdir(mode=0o700)
    _setup(
        independent,
        root,
        "--source",
        connection,
        resource,
        "--complete-domain",
        "--tier",
        "public",
        "--tier",
        "work",
    )
    independent_save = _request(
        independent, "page.save", {"url": URL, "reason": REASON}, str(uuid4())
    )
    assert cast(dict[str, object], independent_save["result"])["state"] == "accepted"

    _call(
        home,
        [
            "setup",
            "--extension-id",
            "a" * 32,
            "--data-dir",
            str(root),
            "--operation",
            "lookup",
            "--tier",
            "public",
            "--unassigned",
            "--source",
            connection,
            resource,
        ],
    )
    assert _request(home, "page.lookup", {"url": URL}) == {"result": {"state": "absent"}}
    policy.write_bytes(previous)
    assert _request(home, "page.lookup", {"url": URL}) == {"error": {"code": "browser_unbound"}}
    independent_lookup = _request(independent, "page.lookup", {"url": URL})
    assert cast(dict[str, object], independent_lookup["result"])["state"] == "saved"


def test_interrupted_reconfiguration_leaves_old_policy_unbound(
    installation: tuple[Path, Path],
) -> None:
    home, root = installation
    policy = home / ".open-brain-browser" / "policy.json"
    previous = policy.read_bytes()
    binding = json.loads(previous)["binding"]
    _, connection, resource = binding["source"]
    operation = str(uuid4())
    saved = _request(home, "page.save", {"url": URL, "reason": REASON}, operation)
    assert cast(dict[str, object], saved["result"])["state"] == "accepted"
    arguments = [
        "setup",
        "--extension-id",
        "a" * 32,
        "--data-dir",
        str(root),
        "--operation",
        "lookup",
        "--operation",
        "status",
        "--tier",
        "personal",
        "--unassigned",
        "--source",
        connection,
        resource,
        "--workflow-principal",
        binding["workflow_principal"],
    ]
    _, env = _host(home)
    crash_before_policy_replacement = """import os
import sys
from open_brain_browser_host import setup

def crash(directory, policy):
    os._exit(97)

setup.save = crash
raise SystemExit(setup.main(sys.argv[1:]))
"""
    child = subprocess.run(
        [sys.executable, "-c", crash_before_policy_replacement, *arguments],
        env=env,
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 97, child.stderr
    assert policy.read_bytes() == previous
    assert _request(home, "page.lookup", {"url": URL}) == {"error": {"code": "browser_unbound"}}
    _call(home, arguments)
    assert _request(home, "operation.status", {}, operation) == saved


def test_owner_mutations_serialize_per_installation(installation: tuple[Path, Path]) -> None:
    home, root = installation
    executable, env = _host(home)
    directory = os.open(home / ".open-brain-browser", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for arguments in (
            ["revoke"],
            [
                "setup",
                "--extension-id",
                "a" * 32,
                "--data-dir",
                str(root),
                "--operation",
                "lookup",
                "--tier",
                "public",
                "--unassigned",
                "--new-source",
            ],
        ):
            child = subprocess.run(
                [*executable, *arguments], env=env, capture_output=True, timeout=5
            )
            assert child.returncode == 2
        assert _request(home, "page.lookup", {"url": URL}) == {"result": {"state": "absent"}}
    finally:
        os.close(directory)
    _call(home, ["revoke"])
    assert _request(home, "page.lookup", {"url": URL}) == {"error": {"code": "browser_unbound"}}
