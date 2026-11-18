"""Drive the popup by keyboard and inspect the accessibility tree in disposable Chrome."""

import argparse
import asyncio
import importlib
import json
import tempfile
from pathlib import Path
from typing import Any

from tools.open_brain_dev.browser_acceptance import ROOT, Run, Target, until


async def press(target: Target, key: str, *, shift: bool = False) -> None:
    codes = {"Tab": 9, "Enter": 13, "ArrowRight": 39, "Backspace": 8}
    code = codes.get(key, ord(key) if len(key) == 1 else 0)
    kwargs = {
        "key": key,
        "code": "Digit" + key if key.isdigit() else key,
        "windowsVirtualKeyCode": code,
        "modifiers": 8 if shift else 0,
    }
    if key == "Enter":
        kwargs["nativeVirtualKeyCode"] = 36
    text = "\r" if key == "Enter" else key if len(key) == 1 else ""
    await target.send("Input.dispatchKeyEvent", type="keyDown", text=text, **kwargs)
    await target.send("Input.dispatchKeyEvent", type="keyUp", **kwargs)


async def tab_to(target: Target, selector: str) -> None:
    trail = []
    for _ in range(65):
        if await target.evaluate("document.activeElement.matches(" + json.dumps(selector) + ")"):
            return
        trail.append(
            await target.evaluate("document.activeElement.id || document.activeElement.tagName")
        )
        await press(target, "Tab")
    raise AssertionError(f"Keyboard focus did not reach {selector}: {trail}")


async def activate(target: Target, selector: str) -> None:
    await tab_to(target, selector)
    await press(target, "Enter")


async def check(target: Target, expression: str, label: str) -> None:
    try:
        await until(lambda: target.evaluate(expression), label)
    except TimeoutError as error:
        detail = await target.evaluate(
            "({status:document.querySelector('#status').textContent,"
            "active:document.activeElement.id,"
            "revisit:document.querySelector('#revisit-status').textContent})"
        )
        raise AssertionError(f"{label}: {detail}") from error


async def keyboard(run: Run, pw: Any) -> dict[str, object]:
    run.insert_relay()
    async with run.browser(pw):
        popup = await run.passage("first", navigate=True)
        await popup.ready()
        await tab_to(popup, "#reason")
        await popup.send("Input.insertText", text="synthetickeyboard")
        await activate(popup, "#save")
        await check(popup, "!document.querySelector('#highlight-form').hidden", "keyboard save")
        await check(popup, "document.activeElement.id==='add-highlight'", "post-save focus")
        await activate(popup, "#add-highlight")
        await check(
            popup, "document.querySelector('#status').textContent==='Highlight saved.'", "highlight"
        )
        await check(popup, "document.activeElement.id==='show-highlights'", "post-highlight focus")
        await activate(popup, "#show-highlights")
        await check(
            popup,
            "Boolean(document.querySelector('#restore-highlights').offsetParent)",
            "listed highlight",
        )
        await activate(popup, "#restore-highlights")
        await check(
            popup,
            "document.querySelector('#status').textContent.includes('Located 1 of 1')",
            "restored highlight",
        )
        await activate(popup, "#revisit-edit summary")
        await check(
            popup, "document.querySelector('#revisit-zone').value.length>0", "revisit settings"
        )
        await tab_to(popup, "#revisit-date")
        # HTML date controls expose three keyboard-editable fields in the US fixture locale.
        for k in "01022024":
            await press(popup, k)
        day = await popup.evaluate("document.querySelector('#revisit-date').value")
        assert day == "2024-01-02", day
        await activate(popup, "#revisit-set")
        await check(
            popup,
            "document.querySelector('#revisit-status').textContent.includes('2024-01-02')",
            "saved date",
        )
        await activate(popup, "#due-panel summary")
        await check(popup, "document.querySelectorAll('#due-rows li').length===1", "due on demand")
        ax = (await popup.send("Accessibility.getFullAXTree"))["nodes"]
        assert any(
            n.get("role", {}).get("value") == "status"
            and any(
                p["name"] == "live" and p["value"]["value"] == "polite"
                for p in n.get("properties", [])
            )
            for n in ax
        )
        assert any(n.get("name", {}).get("value") == "Clear revisit date" for n in ax)
        assert await popup.evaluate("document.activeElement.matches('#due-panel summary')")
    restored = run.portable_restore()
    async with restored.browser(pw):
        popup = await restored.passage("first", navigate=True)
        await popup.ready()
        await activate(popup, "#due-panel summary")
        await check(
            popup,
            "document.querySelector('#due-rows').textContent.includes('2024-01-02')",
            "restored due date",
        )
        await activate(popup, "#revisit-edit summary")
        await check(
            popup, "!document.querySelector('#revisit-dismiss').hidden", "restored controls"
        )
        await activate(popup, "#revisit-dismiss")
        await check(
            popup, "document.querySelector('#due-rows').children.length===0", "dismissed due item"
        )
        await tab_to(popup, "#revisit-clear")
        await press(popup, "Enter")
        await check(popup, "document.querySelector('#revisit-clear').hidden", "cleared saved date")
        assert not restored.blocked
    return {
        "chrome": run.version,
        "extension_id": run.id,
        "keyboard_capture": True,
        "keyboard_highlight": True,
        "restored_due_date": day,
        "dismissed": True,
        "cleared": True,
        "polite_live_region": True,
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
    with tempfile.TemporaryDirectory(prefix="open-brain-keyboard-") as tmp:
        run = Run(
            Path(tmp).resolve(),
            args.chrome.resolve(strict=True),
            args.host.resolve(strict=True),
            args.brain_cli.resolve(strict=True),
        )
        run.setup()
        async with playwright.async_playwright() as pw:
            print(json.dumps(await keyboard(run, pw), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
