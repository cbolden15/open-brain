"""Two isolated synthetic installations communicating through process pipes only."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from open_brain_browser_host import remote, setup
from open_brain_browser_host.policy import Refused, RemotePolicy, load_installation

pytestmark = pytest.mark.integration
EXT = "a" * 32
URL = "https://fixture.invalid/cedar"
KEY = "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + b"x" * 32).decode()


class FaultStream:
    def __init__(self, fault: str):
        self.fault = fault
        self.count = 0

    def write(self, data: bytes) -> int:
        self.count += 1
        if self.count == 1 and self.fault == "wrong_brain":
            data = remote._frame({"binding": "0" * 64, "extension": EXT})
        if self.count == 2:
            if self.fault == "drop":
                return len(data)
            if self.fault == "truncate":
                data = data[:-2]
            if self.fault == "extra":
                data += b"x"
            if self.fault == "identity":
                reply = json.loads(data[8:])
                reply["nonce"] = "different"
                data = remote._frame(reply)
            if self.fault in {"unrecognized_error", "wrong_result"}:
                reply = json.loads(data[8:])
                if self.fault == "unrecognized_error":
                    del reply["result"]
                    reply["error"] = "unexpected_short_code"
                else:
                    reply["result"]["annotation_id"] = str(uuid4())
                data = remote._frame(reply)
            if self.fault == "marker":
                data = b"BAD!" + data[4:]
            if self.fault == "size":
                data = struct.pack("!4sI", b"OBR1", 2**32 - 1)
        return sys.stdout.buffer.write(data)

    def flush(self) -> None:
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    os.environ["HOME"] = sys.argv[1]
    mode = sys.argv[2]
    if mode in {"refused", "unknown_host_key"}:
        sys.exit(255)
    if mode == "stall":
        time.sleep(8)
    sys.exit(remote.handler(sys.stdin.buffer, FaultStream(mode)))  # type: ignore[arg-type]


def count(root: Path) -> int:
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        return int(db.execute("SELECT COUNT(*) FROM captures").fetchone()[0])


def test_remote_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    laptop, canonical = (tmp_path / name for name in ("laptop", "canonical"))
    for home in (laptop, canonical):
        home.mkdir(mode=0o700)
        monkeypatch.setenv("HOME", str(home))
        assert (
            setup.main(
                [
                    "setup",
                    "--data-dir",
                    str(home / "brain"),
                    "--extension-id",
                    EXT,
                    "--initialize-brain",
                    "--new-source",
                    "--complete-domain",
                    "--unassigned",
                    "--tier",
                    "personal",
                    "--tier",
                    "public",
                    "--tier",
                    "work",
                    "--operation",
                    "save",
                    "--operation",
                    "lookup",
                    "--operation",
                    "status",
                ]
            )
            == 0
        )
        binding = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("HOME", str(laptop))
    assert (
        setup.main(
            [
                "setup-remote",
                "--extension-id",
                EXT,
                "--destination",
                "fixture@canonical.invalid",
                "--host-key",
                KEY,
                "--identity-file",
                "/fixture/unused",
                "--canonical-id",
                binding["binding_id"],
            ]
        )
        == 0
    )
    policy = load_installation()
    assert isinstance(policy, RemotePolicy)
    mode = "normal"

    def adapter(_: RemotePolicy, hosts: Path) -> list[str]:
        assert hosts.read_text() == "open-brain-canonical " + KEY + "\n"
        return [sys.executable, __file__, str(canonical), mode]

    argv = remote.ssh_command(policy, Path("/fixture/pin"))
    assert argv[0] == "/usr/bin/ssh"
    assert argv[-2:] == [policy.destination, "open-brain-browser-host remote-handler"]
    assert {
        "-oStrictHostKeyChecking=yes",
        "-oIdentityAgent=none",
        "-oBatchMode=yes",
        "-oConnectTimeout=2",
        "-oHostKeyAlias=open-brain-canonical",
    } <= set(argv)
    monkeypatch.setattr(remote, "ssh_command", adapter)
    describe = remote.forward(
        policy, {"version": "browser.v1", "operation": "binding.describe", "args": {}}
    )
    assert describe["destination"] == policy.destination
    base = {"version": "browser.v1", "binding_id": describe["binding_id"]}
    operation = str(uuid4())
    save = {
        **base,
        "operation": "page.save",
        "operation_id": operation,
        "args": {"url": URL, "reason": "cedar"},
    }
    status = {**base, "operation": "operation.status", "operation_id": operation, "args": {}}
    lookup = {**base, "operation": "page.lookup", "args": {"url": URL}}
    assert remote.forward(policy, lookup)["state"] == "absent"
    for mode in ("refused", "unknown_host_key", "stall", "wrong_brain"):
        code = "binding_changed" if mode == "wrong_brain" else "remote_unavailable"
        with pytest.raises(Refused, match="^" + code + "$"):
            remote.forward(policy, save)
        assert count(canonical / "brain") == count(laptop / "brain") == 0
    for fault in (
        "drop",
        "truncate",
        "extra",
        "identity",
        "marker",
        "size",
        "unrecognized_error",
        "wrong_result",
    ):
        mode = fault
        with pytest.raises(Refused, match="^outcome_unknown$"):
            remote.forward(policy, save)
        assert count(canonical / "brain") == 1
        assert count(laptop / "brain") == 0
    mode = "normal"
    saved = remote.forward(policy, status)
    assert saved["state"] == "accepted"
    assert remote.forward(policy, save) == saved
    assert remote.forward(policy, lookup)["capture_id"] == saved["capture_id"]
    assert count(canonical / "brain") == 1 and count(laptop / "brain") == 0
    monkeypatch.setenv("HOME", str(canonical))
    assert (
        setup.main(
            [
                "setup",
                "--extension-id",
                EXT,
                "--data-dir",
                str(canonical / "brain"),
                "--source",
                *binding["source"][1:],
                "--workflow-principal",
                binding["workflow_principal"],
                "--operation",
                "lookup",
                "--tier",
                "public",
                "--unassigned",
            ]
        )
        == 0
    )
    monkeypatch.setenv("HOME", str(laptop))
    assert remote.forward(policy, lookup)["state"] == "absent"
    for request in (save, status):
        with pytest.raises(Refused, match="^operation_denied$"):
            remote.forward(policy, request)
    assert count(canonical / "brain") == 1 and count(laptop / "brain") == 0


def test_terminal_results_match_the_operation_and_arguments() -> None:
    annotation = str(uuid4())
    parent = "capture_opaque"
    args = {"capture_id": parent, "annotation_id": annotation}
    accepted = {"state": "accepted", "capture_id": parent, "annotation_id": annotation}
    row = {"annotation_id": annotation, "quote": "café", "prefix": "", "suffix": "", "note": None}
    assert remote.validate_result("annotation.add", args, accepted)
    assert remote.validate_result("operation.status", {}, accepted)
    assert not remote.validate_result("page.save", {}, accepted)
    assert not remote.validate_result("annotation.add", args, {**accepted, "capture_id": "other"})
    assert not remote.validate_result(
        "annotation.add", args, {"state": "accepted", "capture_id": parent}
    )
    page = {"annotations": [row], "next_offset": 1}
    paging = {"limit": 1, "offset": 0}
    assert remote.validate_result("annotation.list", paging, page)
    assert not remote.validate_result("annotation.list", paging, {**page, "next_offset": True})
    for bad in ({**row, "note": 1}, {**row, "prefix": "x" * 129}, {**row, "quote": "\ud800"}):
        assert not remote.validate_result("annotation.list", paging, {**page, "annotations": [bad]})
    assert not remote.validate_result("unregistered.method", {}, {"state": "accepted"})
