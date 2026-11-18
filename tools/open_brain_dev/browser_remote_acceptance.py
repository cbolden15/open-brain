"""Exercise the remote popup against a second disposable Brain through local process pipes."""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import json
import os
import shlex
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.open_brain_dev.browser_acceptance import ROOT, Run, command, environment, until  # noqa: E402

DESTINATION = "owner@canonical.invalid"
KEY = (
    "ssh-ed25519 "
    + base64.b64encode(
        bytes.fromhex("0000000b") + b"ssh-ed25519" + bytes.fromhex("00000020") + bytes(32)
    ).decode()
)


def local_transport(canonical: Path, policy: object, pin: Path) -> list[str]:
    """Replace only the SSH executable in the synthetic laptop process."""
    assert pin.read_text() == "open-brain-canonical " + KEY + "\n"
    return [sys.executable, __file__, "_canonical", str(canonical)]


def fixture_host(laptop: Path, canonical: Path, origin: str) -> int:
    from functools import partial

    from open_brain_browser_host import native, remote

    os.environ["HOME"] = str(laptop)
    from unittest.mock import patch

    with patch.object(remote, "ssh_command", partial(local_transport, canonical)):
        sys.argv = [sys.argv[0], origin]
        return native.main()


def canonical_host(home: Path) -> int:
    from open_brain_browser_host import remote

    if (home / "offline").exists():
        return 255
    os.environ["HOME"] = str(home)
    return remote.handler(sys.stdin.buffer, sys.stdout.buffer)


async def exercise(laptop: Run, canonical: Run, pw: Any) -> dict[str, object]:
    laptop.setup()
    canonical.setup()
    # Use the public host description to identify the configured canonical workflow.
    origin = f"chrome-extension://{canonical.id}/"
    message = json.dumps(
        {"version": "browser.v1", "operation": "binding.describe", "args": {}}
    ).encode()
    reply = subprocess.run(
        [canonical.host, origin],
        input=struct.pack("=I", len(message)) + message,
        env=environment(canonical.home),
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    binding = json.loads(reply[4:])["result"]["binding_id"]
    command(
        laptop.host,
        "setup-remote",
        "--extension-id",
        laptop.id,
        "--destination",
        DESTINATION,
        "--host-key",
        KEY,
        "--identity-file",
        str(laptop.home / "fixture-key"),
        "--canonical-id",
        binding,
        home=laptop.home,
    )
    bridge = laptop.home / "fixture-native-host"
    args = [
        str(laptop.host.parent / "python"),
        __file__,
        "_laptop",
        str(laptop.home),
        str(canonical.home),
    ]
    bridge.write_text("#!/bin/sh\nexec " + shlex.join(args) + ' "$@"\n')
    bridge.chmod(0o700)
    laptop.host = bridge
    laptop.insert_relay()
    reason = "remotefinchnest"
    async with laptop.browser(pw):
        await laptop.page_at("remote")
        (canonical.home / "offline").touch()
        popup = await laptop.toolbar()

        async def disconnected() -> bool:
            return "Cannot reach " + DESTINATION in (
                await popup.evaluate("document.querySelector('#status')?.textContent") or ""
            )

        await until(disconnected, "named remote destination while disconnected")
        assert await popup.evaluate("document.querySelector('#page-form').hidden")
        assert not laptop.hits(reason) and not canonical.hits(reason)
        await laptop.close_popup()
        (canonical.home / "offline").unlink()
        popup = await laptop.toolbar()
        await popup.ready()
        assert DESTINATION in await popup.evaluate(
            "document.querySelector('#destination').textContent"
        )
        assert not await popup.evaluate("document.querySelector('#page-form').hidden"), (
            laptop.events("page.lookup")
        )
        (laptop.home / "drop").touch()
        await popup.click_save(reason)
        try:
            capture = await canonical.wait_hit(reason)
        except TimeoutError as error:
            raise RuntimeError(
                {
                    "lookup": laptop.events("page.lookup"),
                    "save": laptop.events("page.save"),
                    "ui": await popup.evaluate("document.querySelector('#status').textContent"),
                }
            ) from error

        async def lost() -> bool:
            return bool(laptop.events("page.save")) and not (laptop.home / "drop").exists()

        await until(lost, "withheld native acknowledgement")
        [event] = laptop.events("page.save")
        operation = event["request"]["operation_id"]
        assert event["response"]["result"]["capture_id"] == capture
    async with laptop.browser(pw):
        await laptop.page_at("remote")
        popup = await laptop.toolbar()
        await popup.ready()

        async def reconciled() -> bool:
            return any(
                e["request"]["operation_id"] == operation
                and e["response"].get("result", {}).get("capture_id") == capture
                for e in laptop.events("operation.status")
            )

        await until(reconciled, "canonical reconciliation after browser restart")
        assert len(laptop.events("page.save")) == len(canonical.hits(reason)) == 1
        assert not laptop.hits(reason) and not laptop.blocked
    return {
        "browser": laptop.version,
        "extension_id": laptop.id,
        "transport": "local process pipes",
        "canonical_captures": 1,
        "laptop_captures": 0,
        "status_reconciled": True,
    }


async def main() -> None:
    if not __debug__:
        raise RuntimeError("Run acceptance with assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--host", type=Path, default=ROOT / ".venv/bin/open-brain-browser-host")
    parser.add_argument("--brain-cli", type=Path, default=ROOT / ".venv/bin/open-brain")
    args = parser.parse_args()
    playwright = importlib.import_module("playwright.async_api")
    with tempfile.TemporaryDirectory(prefix="open-brain-remote-fixture-") as tmp:
        runs = []
        for label in ("laptop", "canonical"):
            home = Path(tmp).resolve() / label
            home.mkdir(mode=0o700)
            runs.append(
                Run(
                    home,
                    args.chrome.resolve(strict=True),
                    args.host.resolve(strict=True),
                    args.brain_cli.resolve(strict=True),
                )
            )
        async with playwright.async_playwright() as pw:
            print(json.dumps(await exercise(runs[0], runs[1], pw), indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "_laptop":
        sys.exit(fixture_host(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]))
    if len(sys.argv) == 3 and sys.argv[1] == "_canonical":
        sys.exit(canonical_host(Path(sys.argv[2])))
    asyncio.run(main())
