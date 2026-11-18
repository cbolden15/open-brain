"""Exercise a real unpacked extension and installed native host with a disposable HOME."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import importlib
import json
import platform
import shlex
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "packages/browser-extension/extension"
DOMAIN = "https://fixture.invalid"
TITLE = '<img src="https://probe.invalid/markup"> synthetic title'
PASSAGE = "<b>synthetic selected passage</b>"
TIMEOUT = 15


async def until(check: Callable[[], Any], label: str) -> Any:
    try:
        async with asyncio.timeout(TIMEOUT):
            while True:
                if result := await check():
                    return result
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        raise TimeoutError(f"Timed out waiting for {label}") from error


def command(path: Path, *args: str, home: Path) -> Any:
    output = subprocess.run(
        [path, *args], env=environment(home), capture_output=True, check=True, timeout=30
    )
    return json.loads(output.stdout)


def environment(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(home), "LANG": "en_US.UTF-8"}


def relay(home: Path, executable: Path, origin: str) -> None:
    """Forward Chrome frames intact, optionally dropping one already-accepted reply."""
    header = sys.stdin.buffer.read(4)
    body = sys.stdin.buffer.read(struct.unpack("=I", header)[0])
    request = json.loads(body)
    operation = request["operation"]
    if operation == "page.save" and (home / "hold").exists():
        (home / "dispatched").touch()
        deadline = time.monotonic() + TIMEOUT
        while (home / "hold").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("dispatch hold expired")
            time.sleep(0.02)
    result = subprocess.run(
        [executable, origin],
        input=header + body,
        capture_output=True,
        check=True,
        timeout=TIMEOUT,
        env=environment(home),
    ).stdout
    response = json.loads(result[4:])
    event = {"request": request, "response": response}
    with (home / "receipts").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    if (
        operation == "page.save"
        and response.get("result", {}).get("state") == "accepted"
        and (home / "drop").exists()
    ):
        (home / "drop").unlink()
        return
    sys.stdout.buffer.write(result)


class Target:
    """A popup/worker is a separate Chrome target outside Playwright's page list."""

    def __init__(self, browser: Any, session: str) -> None:
        self.browser = browser
        self.session = session
        self.sequence = 0
        self.responses: dict[int, asyncio.Future[Any]] = {}
        self.paused = asyncio.Event()
        browser.on("Target.receivedMessageFromTarget", self.receive)

    def receive(self, envelope: dict[str, Any]) -> None:
        if envelope["sessionId"] != self.session:
            return
        event = json.loads(envelope["message"])
        if event.get("method") == "Debugger.paused":
            self.paused.set()
        if future := self.responses.get(event.get("id", -1)):
            if "error" in event:
                future.set_exception(RuntimeError(str(event["error"])))
            else:
                future.set_result(event["result"])

    async def send(self, method: str, **args: Any) -> Any:
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.responses[number] = future
        try:
            await self.browser.send(
                "Target.sendMessageToTarget",
                {
                    "sessionId": self.session,
                    "message": json.dumps({"id": number, "method": method, "params": args}),
                },
            )
            return await asyncio.wait_for(future, TIMEOUT)
        finally:
            del self.responses[number]

    async def evaluate(self, expression: str) -> Any:
        reply = await self.send("Runtime.evaluate", expression=expression, returnByValue=True)
        if "exceptionDetails" in reply:
            raise RuntimeError(str(reply["exceptionDetails"]))
        return reply["result"].get("value")

    async def ready(self) -> None:
        async def available() -> Any:
            error = await self.evaluate("document.querySelector('#status')?.textContent")
            if error and any(
                word in error.lower() for word in ("unavailable", "refused", "changed", "could not")
            ):
                raise RuntimeError(error)
            return await self.evaluate("document.querySelector('#destination')?.textContent")

        await until(available, "popup destination")

    async def click_save(self, reason: str) -> None:
        await self.evaluate(
            "const reason=document.querySelector('#reason');reason.value="
            + json.dumps(reason)
            + ";reason.dispatchEvent(new Event('input',{bubbles:true}))"
        )

        point = await self.evaluate(
            "(() => {const r=document.querySelector('#save')"
            ".getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()"
        )
        for kind in ("mousePressed", "mouseReleased"):
            await self.send(
                "Input.dispatchMouseEvent", type=kind, button="left", clickCount=1, **point
            )


class Run:
    def __init__(self, home: Path, chrome: Path, host: Path, brain_cli: Path) -> None:
        self.home, self.chrome, self.host, self.cli = home, chrome, host, brain_cli
        self.brain = home / "brain"
        self.profile = home / "chrome"
        manifest = json.loads((EXTENSION / "manifest.json").read_text())
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
        self.id = "".join(chr(97 + int(i, 16)) for i in digest)
        self.blocked: list[str] = []
        self.version = ""

    def setup(self) -> None:
        command(
            self.host,
            "setup",
            "--data-dir",
            str(self.brain),
            "--extension-id",
            self.id,
            "--initialize-brain",
            "--new-source",
            "--complete-domain",
            "--unassigned",
            "--operation",
            "lookup",
            "--operation",
            "save",
            "--operation",
            "status",
            "--tier",
            "public",
            "--tier",
            "work",
            "--tier",
            "personal",
            home=self.home,
        )
        self.manifest = Path(
            command(
                self.host, "install", "--chrome-user-data-dir", str(self.profile), home=self.home
            )["manifest"]
        )
        assert self.manifest.parent == self.profile / "NativeMessagingHosts"
        self.registration = json.loads(self.manifest.read_text())
        assert self.registration["path"] == str(self.host)
        assert self.registration["allowed_origins"] == [f"chrome-extension://{self.id}/"]

    def insert_relay(self) -> None:
        wrapper = self.home / "relay"
        args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_relay",
            str(self.home),
            str(self.host),
        ]
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join(args) + ' "$@"\n')
        wrapper.chmod(0o700)
        self.manifest.write_text(json.dumps({**self.registration, "path": str(wrapper)}))

    def events(self, operation: str) -> list[dict[str, Any]]:
        path = self.home / "receipts"
        return [
            e
            for line in (path.read_text().splitlines() if path.exists() else [])
            if (e := json.loads(line))["request"]["operation"] == operation
        ]

    async def route(self, route: Any) -> None:
        if route.request.url.startswith(DOMAIN + "/"):
            await route.fulfill(
                content_type="text/html",
                body=(
                    "<title>" + html.escape(TITLE) + "</title><p>" + html.escape(PASSAGE) + "</p>"
                ),
            )
        else:
            self.blocked.append(route.request.url)
            await route.abort()

    @asynccontextmanager
    async def browser(self, playwright: Any) -> AsyncIterator[None]:
        ctx = await playwright.chromium.launch_persistent_context(
            str(self.profile),
            executable_path=str(self.chrome),
            headless=True,
            env=environment(self.home),
            ignore_default_args=["--disable-extensions"],
            args=[
                "--use-mock-keychain",
                "--enable-unsafe-extension-debugging",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                f"--load-extension={EXTENSION}",
                f"--disable-extensions-except={EXTENSION}",
            ],
        )
        try:
            self.cdp = await ctx.browser.new_browser_cdp_session()
            self.version = (await self.cdp.send("Browser.getVersion"))["product"]
            self.ctx = ctx
            self.page = ctx.pages[0]
            await ctx.route("**/*", self.route)
            yield
        finally:
            await ctx.close()

    async def identify(self, kind: str, suffix: str = "") -> Any:
        targets = await self.cdp.send("Target.getTargets", {"filter": [{"type": kind}]})
        return next(
            (i for i in targets["targetInfos"] if i["type"] == kind and i["url"].endswith(suffix)),
            None,
        )

    async def attach(self, kind: str, suffix: str) -> Target:
        info = await until(lambda: self.identify(kind, suffix), suffix)
        session = await self.cdp.send("Target.attachToTarget", {"targetId": info["targetId"]})
        return Target(self.cdp, session["sessionId"])

    async def page_at(self, slug: str) -> None:
        await self.page.goto(DOMAIN + "/" + slug + "#fixture")
        await self.page.bring_to_front()
        await self.page.evaluate(
            "window.getSelection().selectAllChildren(document.querySelector('p'))"
        )

    async def toolbar(self) -> Target:
        await self.page.bring_to_front()
        tab = await self.identify("tab")
        await self.cdp.send(
            "Extensions.triggerAction", {"id": self.id, "targetId": tab["targetId"]}
        )
        return await self.attach("page", f"{self.id}/popup.html")

    async def close_popup(self) -> None:
        info = await self.identify("page", f"{self.id}/popup.html")
        if info:
            await self.cdp.send("Target.closeTarget", {"targetId": info["targetId"]})

        async def gone() -> bool:
            return not await self.identify("page", f"{self.id}/popup.html")

        await until(gone, "popup teardown")

    def hits(self, reason: str) -> list[dict[str, Any]]:
        result = command(
            self.cli, "search", reason, "--data-dir", str(self.brain), "--json", home=self.home
        )
        return list(result["results"])

    async def wait_hit(self, reason: str) -> str:
        async def hit() -> Any:
            return self.hits(reason)

        hits = await until(hit, reason)
        assert len(hits) == 1 and hits[0]["trust"] == "third_party"
        return str(hits[0]["capture_id"])

    async def scenarios(self, pw: Any) -> dict[str, Any]:
        reason = "syntheticquartzbird"
        async with self.browser(pw):
            await self.page_at("direct")
            popup = await self.toolbar()
            await popup.ready()
            assert await popup.evaluate("document.querySelector('#title').textContent") == TITLE
            assert await popup.evaluate("passage.textContent") == PASSAGE
            assert await popup.evaluate("url.textContent") == DOMAIN + "/direct"
            assert (
                await popup.evaluate("document.querySelectorAll('#title *,#passage *').length") == 0
            )
            await popup.click_save(reason)
            await until(
                lambda: popup.evaluate(
                    "document.querySelector('#status').textContent.startsWith('Page is saved.')"
                ),
                "direct save",
            )
            direct = await self.wait_hit(reason)
            await self.close_popup()
            popup = await self.toolbar()
            await popup.ready()
            assert "Already saved" in await popup.evaluate(
                "document.querySelector('#status').textContent"
            )
        self.insert_relay()
        async with self.browser(pw):
            await self.page_at("lost")
            popup = await self.toolbar()
            await popup.ready()
            (self.home / "drop").touch()
            await popup.click_save("syntheticamberbird")
            await until(
                lambda: popup.evaluate("!document.querySelector('#pending').hidden"),
                "lost response shown as unresolved",
            )
            assert not (self.home / "drop").exists()
        async with self.browser(pw):
            await self.page_at("lost")
            popup = await self.toolbar()
            await popup.ready()
            assert (
                "saved"
                in (await popup.evaluate("document.querySelector('#status').textContent")).lower()
            )
            lost = await self.wait_hit("syntheticamberbird")
            [save] = self.events("page.save")
            [status] = self.events("operation.status")
            assert save["request"]["operation_id"] == status["request"]["operation_id"]
            assert save["response"] == status["response"]
            assert save["response"]["result"]["capture_id"] == lost
            await self.close_popup()
            await self.page_at("before")
            popup = await self.toolbar()
            await popup.ready()
            worker = await self.attach("service_worker", f"{self.id}/worker.js")
            await worker.send("Debugger.enable")
            lines = (EXTENSION / "worker.js").read_text().splitlines()
            line = next(
                i for i, value in enumerate(lines) if "const current = await snapshotTab" in value
            )
            bp = await worker.send(
                "Debugger.setBreakpointByUrl",
                lineNumber=line,
                url=f"chrome-extension://{self.id}/worker.js",
            )
            assert bp["locations"]
            await popup.click_save("syntheticstalebird")
            await asyncio.wait_for(worker.paused.wait(), TIMEOUT)
            await self.page.goto(DOMAIN + "/replacement")
            await self.close_popup()
            await worker.send("Debugger.removeBreakpoint", breakpointId=bp["breakpointId"])
            await worker.send("Debugger.resume")

            # The worker has finished its cleanup once the ledger becomes empty.
            async def cleared() -> Any:
                response = await worker.send(
                    "Runtime.evaluate",
                    expression=(
                        "chrome.storage.local.get('pending').then(x=>x.pending?.length===0)"
                    ),
                    awaitPromise=True,
                    returnByValue=True,
                )
                return response["result"]["value"]

            await until(cleared, "stale cancellation")
            assert len(self.events("page.save")) == 1
            assert self.hits("syntheticstalebird") == []
            await self.page_at("after")
            popup = await self.toolbar()
            await popup.ready()
            (self.home / "hold").touch()
            await popup.click_save("syntheticmaplebird")

            async def dispatched() -> bool:
                return (self.home / "dispatched").exists()

            await until(dispatched, "native host frame")
            await self.page.goto(DOMAIN + "/replacement")
            await self.close_popup()
            (self.home / "hold").unlink()

            async def accepted() -> Any:
                rows = self.events("page.save")
                return rows[-1] if len(rows) == 2 else None

            received = await until(accepted, "second native save receipt")
            assert "result" in received["response"], received["response"]
            after = await self.wait_hit("syntheticmaplebird")
            assert received["response"]["result"]["capture_id"] == after
            args = received["request"]["args"]
            assert args["url"] == DOMAIN + "/after#fixture"
            assert args["passage"] == PASSAGE and args["title"] == TITLE
            popup = await self.toolbar()
            await popup.ready()
            assert self.events("page.lookup")[-1]["response"]["result"]["state"] == "absent"
            await self.close_popup()
            # The same page remains visible to the owner after the browser grant changes source.
            command(
                self.host,
                "setup",
                "--data-dir",
                str(self.brain),
                "--extension-id",
                self.id,
                "--new-source",
                "--operation",
                "lookup",
                "--tier",
                "public",
                "--unassigned",
                home=self.home,
            )
            await self.page_at("direct")
            popup = await self.toolbar()
            await popup.ready()
            assert "No saved page" in await popup.evaluate(
                "document.querySelector('#status').textContent"
            )
            assert len(self.hits(reason)) == 1
        assert len({direct, lost, after}) == 3 and not self.blocked
        return {
            "chrome": self.version,
            "macos": platform.mac_ver()[0],
            "extension_id": self.id,
            "extension_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(EXTENSION.iterdir())
                if p.is_file()
            },
            "searchable_captures": 3,
            "stale_saves_dispatched": 0,
            "accepted_lost_replies": 1,
            "replayed_mutations": 0,
            "navigation_after_dispatch": 1,
            "hidden_source_misses": 1,
            "markup_network_requests": 0,
        }


async def main() -> None:
    if not __debug__:
        raise RuntimeError("Run acceptance with assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["local"])
    parser.add_argument("--chrome", required=True, type=Path)
    parser.add_argument("--host", type=Path, default=ROOT / ".venv/bin/open-brain-browser-host")
    parser.add_argument("--brain-cli", type=Path, default=ROOT / ".venv/bin/open-brain")
    args = parser.parse_args()
    playwright = importlib.import_module("playwright.async_api")
    with tempfile.TemporaryDirectory(prefix="open-brain-acceptance-") as tmp:
        run = Run(
            Path(tmp).resolve(),
            args.chrome.resolve(strict=True),
            args.host.resolve(strict=True),
            args.brain_cli.resolve(strict=True),
        )
        run.setup()
        async with playwright.async_playwright() as pw:
            print(json.dumps(await run.scenarios(pw), indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "_relay":
        relay(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        asyncio.run(main())
