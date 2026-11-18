"""A real folder import and Chrome host contend for one source-scoped page."""

from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

ORIGIN = "chrome-extension://" + "a" * 32 + "/"
URL = "https://fixture.example/items/race"

# Only the child test process installs the checkpoint. Both writers keep their
# ordinary executable, argument handling, storage and receipt paths.
PAUSE = """
import os, runpy, sys, time
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.contracts import CaptureFault
from open_brain_collector.custody import CustodyStore
point, descriptor, *sys.argv = sys.argv[1:]
def stop():
    os.write(int(descriptor), b"!")
    time.sleep(25)
    raise RuntimeError("test parent did not terminate paused process")
old_fault = BrainEngine._fault
def fault(self, kind):
    if point == "reserved" and kind == CaptureFault.AFTER_JOURNAL_COMMIT:
        stop()
    return old_fault(self, kind)
BrainEngine._fault = fault
old_outcome = CustodyStore.outcome
def outcome(self, *args, **kwargs):
    if point == "before_ack":
        stop()
    result = old_outcome(self, *args, **kwargs)
    if point == "after_ack":
        stop()
    return result
CustodyStore.outcome = outcome
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _run(args: list[str], home: Path, data: bytes = b"") -> bytes:
    return subprocess.run(
        args,
        env={**os.environ, "HOME": str(home)},
        input=data,
        capture_output=True,
        check=True,
        timeout=25,
    ).stdout


def _json(args: list[str], home: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(_run(args, home))
    return value


def _wire(operation: str, operation_id: str) -> bytes:
    body = json.dumps(
        {
            "version": "browser.v1",
            "operation": operation,
            "operation_id": operation_id,
            "args": {"url": URL, "reason": "Browserlark fixture"}
            if operation == "page.save"
            else {},
        }
    ).encode()
    return struct.pack("=I", len(body)) + body


def _decode(raw: bytes) -> dict[str, Any]:
    assert struct.unpack("=I", raw[:4]) == (len(raw) - 4,)
    value: dict[str, Any] = json.loads(raw[4:])
    assert "result" in value, value
    return dict(value["result"])


@pytest.mark.parametrize(
    ("winner", "point"),
    [
        ("browser", "reserved"),
        ("folder", "reserved"),
        ("folder", "before_ack"),
        ("folder", "after_ack"),
    ],
)
def test_handoff_after_writer_crash(tmp_path: Path, winner: str, point: str) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    root = tmp_path / "brain"
    bin_dir = Path(sys.executable).parent
    _json([str(bin_dir / "open-brain"), "init", "--data-dir", str(root), "--json"], home)
    collector = [
        str(bin_dir / "open-brain-collector"),
        "sources",
        "--foreground",
        "--state",
        str(tmp_path / "collector" / "state.json"),
        "--brain-root",
        str(root),
    ]
    host = [str(bin_dir / "open-brain-browser-host")]
    selected = _json([*collector, "saved-item-select", "--folder-path", str(folder)], home)
    config = tmp_path / "selection.json"
    config.write_text(json.dumps(selected))
    source = _json([*collector, "configure", "--arguments-file", str(config)], home)["source_id"]

    def preview(filename: str, url: str) -> list[str]:
        (folder / filename).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "url": url,
                    "why": "Folderfinch fixture" if url == URL else "seed",
                    "received_at": "2026-10-26T14:00:00Z",
                    "fetch_status": "not_attempted",
                }
            )
        )
        result = _json([*collector, "preview", "--source-id", source], home)
        assert result["count"] == 1
        return [*collector, "import", "--source-id", source, "--preview-id", result["preview_id"]]

    assert _json(preview("seed.json", URL + "/seed"), home)["captured_count"] == 1
    [(_, connection, resource)] = _json([*host, "sources", "--data-dir", str(root)], home)[
        "sources"
    ]
    _json(
        [
            *host,
            "setup",
            "--data-dir",
            str(root),
            "--extension-id",
            "a" * 32,
            "--source",
            connection,
            resource,
            "--operation",
            "save",
            "--operation",
            "status",
            "--operation",
            "lookup",
            "--tier",
            "public",
            "--tier",
            "personal",
            "--tier",
            "work",
            "--unassigned",
            "--complete-domain",
        ],
        home,
    )
    apply = preview("target.json", URL)
    operation_id = str(uuid4())
    save = _wire("page.save", operation_id)
    status = _wire("operation.status", operation_id)
    browser = [*host, ORIGIN]
    first, second = (browser, apply) if winner == "browser" else (apply, browser)
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb") as ready,
        os.fdopen(write_fd, "wb") as signal,
        subprocess.Popen(
            [sys.executable, "-c", PAUSE, point, str(write_fd), *first],
            env={**os.environ, "HOME": str(home)},
            pass_fds=(write_fd,),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as original,
    ):
        signal.close()
        try:
            assert original.stdin
            if winner == "browser":
                original.stdin.write(save)
            original.stdin.close()
            assert select.select([ready], [], [], 15)[0], "writer did not reach its checkpoint"
            assert ready.read(1) == b"!"
            if winner == "browser":
                assert _decode(_run(browser, home, status))["state"] == "pending"
            # Keep the first writer alive until the second one has reached durable
            # custody. This exercises real contention without scheduler assumptions.
            other_output = _run(second, home, save if winner == "folder" else b"")
        finally:
            if original.poll() is None:
                original.kill()
    if winner == "browser":
        assert json.loads(other_output)["quarantined_count"] == 0
    else:
        assert _decode(other_output)["state"] in {"pending", "accepted"}
    confirmed = _decode(_run(browser, home, save))
    assert confirmed["state"] == "accepted"
    assert _decode(_run(browser, home, status)) == confirmed
    imported = _json(apply, home)
    assert imported["quarantined_count"] == 0
    assert imported["captured_count"] + imported["duplicate_count"] == 1
    for producer, word in (("browser", "Browserlark"), ("folder", "Folderfinch")):
        results = _json(
            [str(bin_dir / "open-brain"), "search", word, "--data-dir", str(root), "--json"], home
        )["results"]
        assert [row["capture_id"] for row in results] == (
            [confirmed["capture_id"]] if producer == winner else []
        )

    if winner == "folder" and point == "after_ack":
        # A new collector has no scan index to suppress an edited delivery.
        # The Brain still enforces the original folder payload contract.
        target = folder / "target.json"
        changed = json.loads(target.read_bytes())
        changed["why"] = "Changedfennel fixture"
        target.write_text(json.dumps(changed))
        fresh = [
            str(tmp_path / "fresh" / "state.json")
            if item == str(tmp_path / "collector" / "state.json")
            else item
            for item in collector
        ]
        new_source = _json([*fresh, "configure", "--arguments-file", str(config)], home)[
            "source_id"
        ]
        new_preview = _json([*fresh, "preview", "--source-id", new_source], home)
        replay = _json(
            [
                *fresh,
                "import",
                "--source-id",
                new_source,
                "--preview-id",
                new_preview["preview_id"],
            ],
            home,
        )
        assert replay["quarantined_count"] == 1 and replay["captured_count"] == 0
        assert _decode(_run(browser, home, status)) == confirmed
