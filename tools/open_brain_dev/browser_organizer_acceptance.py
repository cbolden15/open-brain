"""Run a real Chrome file chooser, bookmark preview, resumed import and saved-page filing."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import tempfile
from pathlib import Path
from typing import Any

from tools.open_brain_dev.browser_acceptance import ROOT, Run, Target, command, until


async def activate(popup: Target, selector: str) -> None:
    """Activate the focused real popup control through its keyboard handler."""
    focused = await popup.evaluate(
        "(() => {const el=document.querySelector(" + json.dumps(selector) + ");"
        "el.focus();return document.activeElement===el;})()"
    )
    assert focused, selector
    keys = {
        "key": "Enter",
        "code": "Enter",
        "windowsVirtualKeyCode": 13,
        "nativeVirtualKeyCode": 36,
    }
    await popup.send("Input.dispatchKeyEvent", type="keyDown", text="\r", **keys)
    await popup.send("Input.dispatchKeyEvent", type="keyUp", **keys)


async def chooser(popup: Target, path: Path) -> None:
    await popup.send("Page.enable")
    await popup.send("Page.setInterceptFileChooserDialog", enabled=True)
    opened: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    def received(envelope: dict[str, Any]) -> None:
        if envelope["sessionId"] == popup.session:
            message = json.loads(envelope["message"])
            if message.get("method") == "Page.fileChooserOpened" and not opened.done():
                opened.set_result(message["params"]["backendNodeId"])

    popup.browser.on("Target.receivedMessageFromTarget", received)
    try:
        await activate(popup, "#bookmarks")
        node = await asyncio.wait_for(opened, 10)
        await popup.send("DOM.setFileInputFiles", files=[str(path)], backendNodeId=node)
    finally:
        popup.browser.remove_listener("Target.receivedMessageFromTarget", received)
        await popup.send("Page.setInterceptFileChooserDialog", enabled=False)


async def shown(popup: Target, expression: str, label: str) -> None:
    try:
        await until(lambda: popup.evaluate(expression), label)
    except TimeoutError as error:
        state = await popup.evaluate(
            "['status','import-status','preview','filing'].map(id=>"
            "{const el=document.getElementById(id);return [id,el.hidden,el.innerText]})"
        )
        raise RuntimeError(f"{label}: {state}") from error


async def import_panel(popup: Target) -> None:
    await shown(popup, "!document.querySelector('#import-panel').hidden", "import panel ready")
    if not await popup.evaluate("document.querySelector('#import-panel').open"):
        await activate(popup, "#import-panel summary")


async def preview(popup: Target, path: Path, reason: str) -> list[str]:
    await chooser(popup, path)
    await popup.fill("import-reason", reason)
    await activate(popup, "#import-form button")
    await shown(popup, "!document.querySelector('#import-confirm').hidden", "bookmark preview")
    assert await popup.evaluate("document.querySelectorAll('#import-rows li *').length") == 0
    return list(
        await popup.evaluate(
            "Array.from(document.querySelectorAll('#import-rows li'),row=>row.textContent)"
        )
    )


def bookmark_file(home: Path) -> Path:
    path = home / "fixture.html"
    path.write_text(
        """<!doctype html><meta charset="utf-8">
    <a href="https://fixture.invalid/&lt;em&gt;literal&lt;/em&gt;">&lt;img src="https://probe.invalid/p"&gt;</a>
    <a href="https://fixture.invalid/&lt;em&gt;literal&lt;/em&gt;?utm_source=proof">duplicate</a>
    <a href="http://fixture.invalid/http">insecure</a>
    <img src="https://probe.invalid/bookmark">
    <script>fetch('https://probe.invalid/bookmark-script')</script>
    """
        + "".join(f'<a href="https://fixture.invalid/import-{i}">row {i}</a>' for i in range(6)),
        encoding="utf-8",
    )
    return path


def replace_grant(run: Run, *, writable: bool) -> None:
    policy = json.loads((run.home / ".open-brain-browser/policy.json").read_bytes())
    original = policy["binding"]
    command(
        run.host,
        "setup",
        "--data-dir",
        str(run.brain),
        "--extension-id",
        run.id,
        "--source",
        *original["source"][1:],
        "--workflow-principal",
        original["workflow_principal"],
        "--unassigned",
        "--complete-domain",
        "--tier",
        "public",
        "--tier",
        "work",
        "--tier",
        "personal",
        "--operation",
        "lookup",
        "--operation",
        "status",
        *(("--operation", "save") if writable else ()),
        home=run.home,
    )


async def organizer(run: Run, pw: Any) -> dict[str, Any]:
    name = "Synthetic reading shelf"
    space_id = command(
        run.cli, "space", "create", name, "--data-dir", str(run.brain), "--json", home=run.home
    )["space"]["space_id"]
    reason = "syntheticbookmarkbatch"
    path = bookmark_file(run.home)
    run.insert_relay()
    async with run.browser(pw):
        await run.page_at("filing")
        popup = await run.toolbar()
        await popup.ready()
        await popup.click_save("syntheticshelfparent")
        await shown(popup, "!document.querySelector('#filing').hidden", "filing controls ready")
        parent = await run.wait_hit("syntheticshelfparent")
        await activate(popup, "#show-spaces")
        await shown(popup, "!document.querySelector('#space-form').hidden", "space chooser")
        assert await popup.evaluate("document.querySelector('#space').value") == space_id
        replace_grant(run, writable=False)
        await activate(popup, "#file-page")
        await shown(
            popup,
            "document.querySelector('#status').textContent.includes('retry filing')",
            "page survives revoked filing grant",
        )
        assert run.events("page.file")[-1]["response"]["error"]["code"] == "operation_denied"
        assert [r["capture_id"] for r in run.hits("syntheticshelfparent")] == [parent]
        replace_grant(run, writable=True)
        await run.close_popup()
        popup = await run.toolbar()
        await popup.ready()
        await activate(popup, "#show-spaces")
        await shown(popup, "!document.querySelector('#space-form').hidden", "fresh filing gesture")
        await activate(popup, "#file-page")
        await shown(
            popup, "document.querySelector('#status').textContent==='Page filed.'", "filed parent"
        )
        failed, receipt = run.events("page.file")
        assert failed["request"]["operation_id"] != receipt["request"]["operation_id"]
        assert receipt["response"]["result"] == {
            "state": "accepted",
            "capture_id": parent,
            "space_id": space_id,
        }
        assert name not in await run.page.evaluate("document.body.textContent")

        await import_panel(popup)
        # Reject oversized input before any native upload.
        oversized = run.home / "oversized.html"
        oversized.write_bytes(b"a" * (1024 * 1024 + 1))
        await chooser(popup, oversized)
        await popup.fill("import-reason", reason)
        await activate(popup, "#import-form button")
        await shown(
            popup,
            "document.querySelector('#import-status').textContent.includes('1 MiB')",
            "oversize refusal",
        )
        assert not run.events("import.begin")

        rows = await preview(popup, path, reason)
        assert len(rows) == 9, rows
        assert "<em>literal</em>" in rows[0] and rows[1:].count("duplicate") == 1, rows
        assert rows.count("skipped") == 1 and len(run.events("import.begin")) == 1
        assert not run.events("import.step") and run.hits(reason) == []
        replace_grant(run, writable=False)
        await activate(popup, "#import-confirm")
        await shown(
            popup,
            "document.querySelector('#import-status').textContent.includes('grant')",
            "confirm refused under changed grant",
        )
        assert run.events("import.confirm")[-1]["response"]["error"]["code"] == "operation_denied"
        assert not run.events("import.step") and run.hits(reason) == []
        replace_grant(run, writable=True)
        await activate(popup, "#import-cancel")
        await shown(
            popup,
            "document.querySelector('#import-status').textContent.startsWith('canceled')",
            "zero-mutation cancellation",
        )
        assert not run.events("import.step") and run.hits(reason) == []
        await activate(popup, "#import-done")
        await shown(popup, "!document.querySelector('#import-form').hidden", "new import")
        assert await preview(popup, path, reason) == rows
        (run.home / "drop-import-step").touch()
        await activate(popup, "#import-confirm")

        async def interrupted() -> Any:
            return not (run.home / "drop-import-step").exists() and run.events("import.step")

        steps = await until(interrupted, "first canonical step followed by lost acknowledgement")
        [first] = steps
        assert first["response"]["result"]["state"] == "active"
        batch = first["request"]["args"]["batch_id"]
        partial = run.hits(reason)
        assert 0 < len(partial) < 7
    async with run.browser(pw):
        await run.page_at("filing")
        popup = await run.toolbar()
        await popup.ready()
        await import_panel(popup)
        await shown(
            popup, "!document.querySelector('#import-resume').hidden", "recover confirmed batch"
        )
        await activate(popup, "#import-resume")
        await shown(
            popup,
            "document.querySelector('#import-status').textContent.startsWith('complete')",
            "finished resumed batch",
        )
        assert all(e["request"]["args"]["batch_id"] == batch for e in run.events("import.step"))
        parent_ids = {row["capture_id"] for row in run.hits(reason)}
        assert len(parent_ids) == 7
        assert all(row["capture_id"] in parent_ids for row in partial)
        assert len(run.events("page.save")) == 1 and not run.blocked
    restored = run.portable_restore()
    async with restored.browser(pw):
        await restored.page_at("filing")
        popup = await restored.toolbar()
        await popup.ready()
        await activate(popup, "#show-spaces")
        await shown(popup, "!document.querySelector('#space-form').hidden", "restored filing")
        assert await popup.evaluate("document.querySelector('#space').value") == space_id
        assert name in await popup.evaluate("document.querySelector('#current-space').textContent")
        assert {r["capture_id"] for r in restored.hits(reason)} == parent_ids
        assert not restored.blocked
    return {
        "chrome": run.version,
        "filings": 1,
        "revoked_filing_refusals": 1,
        "revoked_preview_confirmations_refused": 1,
        "restored_space_matches": 1,
        "preview_rows": len(rows),
        "canceled_captures": 0,
        "bookmarks_at_interruption": len(partial),
        "accepted_bookmarks": len(parent_ids),
        "skipped_rows": 1,
        "duplicate_rows": 1,
        "external_markup_requests": 0,
        "restored_bookmarks": len(parent_ids),
    }


async def main() -> None:
    if not __debug__:
        raise RuntimeError("Run acceptance with assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True, type=Path)
    parser.add_argument("--host", type=Path, default=ROOT / ".venv/bin/open-brain-browser-host")
    parser.add_argument("--brain-cli", type=Path, default=ROOT / ".venv/bin/open-brain")
    args = parser.parse_args()
    playwright = importlib.import_module("playwright.async_api")
    with tempfile.TemporaryDirectory(prefix="open-brain-organizer-") as tmp:
        run = Run(
            Path(tmp).resolve(),
            args.chrome.resolve(strict=True),
            args.host.resolve(strict=True),
            args.brain_cli.resolve(strict=True),
        )
        run.setup()
        async with playwright.async_playwright() as pw:
            print(json.dumps(await organizer(run, pw), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
