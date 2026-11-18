"""Exercise the packaged macOS browser host and extension in an isolated installation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from tools.open_brain_dev.browser_acceptance import ROOT, Run, command, environment, until
from tools.open_brain_dev.browser_distribution import FILES
from tools.open_brain_dev.browser_organizer_acceptance import activate, shown


def native(
    run: Run, operation: str, args: dict[str, Any], *, home: Path | None = None, **fields: Any
) -> Any:
    body = json.dumps(
        {"version": "browser.v1", "operation": operation, "args": args, **fields}
    ).encode()
    raw = subprocess.run(
        [run.host, f"chrome-extension://{run.id}/"],
        input=struct.pack("=I", len(body)) + body,
        check=True,
        capture_output=True,
        env=environment(home or run.home),
        timeout=20,
    ).stdout
    assert len(raw) >= 4 and struct.unpack("=I", raw[:4])[0] == len(raw) - 4
    return json.loads(raw[4:])


def listener_check(run: Run) -> None:
    """Inspect only the synthetic host and its bootloader child while waiting for stdin."""
    with subprocess.Popen(
        [run.host, f"chrome-extension://{run.id}/"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment(run.home),
    ) as proc:
        try:
            children = subprocess.run(["/usr/bin/pgrep", "-P", str(proc.pid)], capture_output=True)
            pids = [str(proc.pid), *children.stdout.decode().split()]
            sockets = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(pids), "-iTCP", "-sTCP:LISTEN"],
                capture_output=True,
                timeout=5,
            )
            assert sockets.returncode == 1 and not sockets.stdout and not sockets.stderr
            body = b'{"version":"browser.v1","operation":"binding.describe","args":{}}'
            out, _ = proc.communicate(struct.pack("=I", len(body)) + body, timeout=15)
            assert (
                proc.returncode == 0
                and json.loads(out[4:])["result"]["destination"] == "This computer"
            )
        finally:
            if proc.poll() is None:
                proc.kill()


def status(run: Run, action: str) -> str:
    result = command(run.host, action, "--chrome-user-data-dir", str(run.profile), home=run.home)
    assert result["extension_id"] == run.id
    return str(result["state"])


def schema(root: Path) -> int:
    db = root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def core_upgrade(home: Path, cli: Path, prior: Path) -> dict[str, Any]:
    clean = command(cli, "init", "--data-dir", str(home / "clean-core"), "--json", home=home)
    assert clean["daemon_running"] is False
    root = home / "brain"
    args = ["--data-dir", str(root), "--json"]
    initialized = command(prior, "init", *args, home=home)
    assert initialized["daemon_running"] is False
    seed = command(prior, "capture", "syntheticpriorowner", *args, home=home)
    before = schema(root)
    [hit] = command(cli, "search", "syntheticpriorowner", *args, home=home)["results"]
    assert hit["capture_id"] == seed["capture_id"]
    after = schema(root)
    assert after > before and after == clean["state_schema_version"]
    export = command(cli, "export", str(home / "core-export"), "--verify", *args, home=home)
    assert export["verification"] == "verified"
    assert not (home / ".open-brain-browser").exists()
    return {"prior_schema": before, "upgraded_schema": after, "prior_capture_found": 1}


def install_copy(home: Path, candidate: Path, slot: str) -> Path:
    prefix = home / "prefix"
    cellar = prefix / "Cellar/open-brain-browser-host" / slot
    executable = cellar / "libexec/open-brain-browser-host"
    executable.parent.mkdir(parents=True)
    shutil.copy2(candidate, executable)
    shutil.copytree(candidate.parent / "_internal", executable.parent / "_internal", symlinks=True)
    share = cellar / "share/open-brain-browser-host"
    (share / "licenses").mkdir(parents=True)
    source = candidate.parent.parent
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(source / name, share / "licenses" / name)
    shutil.copy2(source / "manifest.json", share / "manifest.json")
    local = cellar / "bin/open-brain-browser-host"
    local.parent.mkdir()
    local.symlink_to(executable)
    link = prefix / "bin/open-brain-browser-host"
    link.parent.mkdir(exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(local)
    return link.resolve(strict=True)


async def load(run: Run) -> None:
    result = await run.cdp.send("Extensions.loadUnpacked", {"path": str(run.extension)})
    assert result["id"] == run.id


async def storage(run: Run) -> dict[str, Any]:
    worker = await run.attach("service_worker", f"{run.id}/worker.js")
    await until(
        lambda: worker.evaluate("Boolean(globalThis.chrome?.storage?.local)"),
        "reinstalled worker storage API",
    )
    value = await worker.send(
        "Runtime.evaluate",
        expression="chrome.storage.local.get(null)",
        awaitPromise=True,
        returnByValue=True,
    )
    if "exceptionDetails" in value:
        raise RuntimeError(str(value["exceptionDetails"]))
    return dict(value["result"]["value"])


async def exercise(home: Path, args: argparse.Namespace, pw: Any) -> dict[str, Any]:
    cli, prior, tar, archive, chrome = (
        p.resolve(strict=True)
        for p in (args.brain_cli, args.prior_core, args.host_archive, args.zip, args.chrome)
    )
    # Prove the core-owned upgrade before staging either optional browser artifact.
    core_sha = hashlib.sha256(cli.read_bytes()).hexdigest()
    evidence = core_upgrade(home, cli, prior)
    extracted = home / "host-package"
    staged_tar = home / "host.tar.gz"
    shutil.copyfile(tar, staged_tar)
    archive_sha = hashlib.sha256(staged_tar.read_bytes()).hexdigest()
    formula = tar.with_name("open-brain-browser-host.rb").read_text()
    assert f'sha256 "{archive_sha}"' in formula
    with tarfile.open(staged_tar, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    metadata = json.loads((extracted / "manifest.json").read_bytes())
    assert (
        metadata["product"] == "open-brain-browser-host" and metadata["platform"] == "macos-arm64"
    )
    candidate = extracted / "open-brain-browser-host/open-brain-browser-host"
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == metadata["executable_sha256"]
    host = install_copy(home, candidate, "first")
    package = home / "upload"
    staged_zip = home / "extension.zip"
    shutil.copyfile(archive, staged_zip)
    with zipfile.ZipFile(staged_zip) as extension:
        assert set(extension.namelist()) == set(FILES) | {"LICENSE", "NOTICE"}
        extension.extractall(package)
    run = Run(home, chrome, host, cli, package)
    run.setup("--new-source")
    assert status(run, "diagnose") == "registered"
    original = json.loads((home / ".open-brain-browser/policy.json").read_bytes())
    listener_check(run)
    neighbor = run.manifest.with_name("other.fixture.json")
    neighbor.write_text("{}")
    owned = run.manifest.read_bytes()
    async with run.browser(pw, load_extension=False):
        await load(run)
        popup = await run.passage("first", navigate=True)
        await popup.click_save("syntheticinstalledparent")
        await shown(
            popup, "!document.querySelector('#highlight-form').hidden", "installed parent saved"
        )
        parent = await run.wait_hit("syntheticinstalledparent")
        # Capture one accepted reply at the pipe boundary before Chrome sees it.
        run.insert_relay()
        (home / "drop").touch()
        await popup.fill("note", "syntheticinstallednote")
        await popup.click("add-highlight")
        await shown(popup, "!document.querySelector('#pending').hidden", "installed reply lost")
        [add] = run.events("annotation.add")
        operation = add["request"]["operation_id"]
        receipt = add["response"]["result"]
        assert receipt["capture_id"] == parent and receipt["state"] == "accepted"
        ledger = (await storage(run))["pending"]
        assert ledger == [{"id": operation, "binding": add["request"]["binding_id"]}]
        await run.close_popup()
        await run.cdp.send("Extensions.uninstall", {"id": run.id})

        async def unloaded() -> bool:
            return not await run.identify("service_worker", f"{run.id}/worker.js")

        await until(unloaded, "extension uninstalled")
        # Remove the test relay and return this fixture's exact owned registration.
        run.manifest.write_bytes(owned)
        # Re-registration on an upgrade points Chrome at the new executable path.
        run.host = install_copy(home, candidate, "replacement")
        command(run.host, "install", "--chrome-user-data-dir", str(run.profile), home=home)
        assert json.loads(run.manifest.read_bytes())["path"] == str(run.host)
        assert status(run, "diagnose") == "registered"
        replacement_bytes = run.manifest.read_bytes()
        assert status(run, "unregister") == "removed" and status(run, "unregister") == "absent"
        assert neighbor.is_file()
        for foreign in (b'{"fixture":"foreign"}', replacement_bytes):
            run.manifest.write_bytes(foreign)
            run.manifest.chmod(0o600)
            assert status(run, "diagnose") == "foreign"
            for action in ("install", "unregister"):
                refused = subprocess.run(
                    [run.host, action, "--chrome-user-data-dir", str(run.profile)],
                    capture_output=True,
                    env=environment(home),
                    timeout=20,
                )
                assert refused.returncode and run.manifest.read_bytes() == foreign
            run.manifest.unlink()
        run.setup(
            "--source",
            *original["binding"]["source"][1:],
            "--workflow-principal",
            original["binding"]["workflow_principal"],
        )
        current = json.loads((home / ".open-brain-browser/policy.json").read_bytes())
        assert current["token"] != original["token"]
        stale = home / "stale-grant"
        stale_policy = stale / ".open-brain-browser/policy.json"
        stale.mkdir(mode=0o700)
        stale_policy.parent.mkdir(mode=0o700)
        stale_policy.write_text(json.dumps(original))
        stale_policy.chmod(0o600)
        assert native(run, "binding.describe", {}, home=stale)["error"]["code"] == "browser_unbound"
        await load(run)
        assert not (await storage(run)).get("pending")
        popup = await run.passage("first")
        await shown(popup, "!document.querySelector('#recovery').hidden", "fresh workflow recovery")
        await activate(popup, "#recovery summary")
        await shown(
            popup,
            "document.querySelector('#recovery-list').children.length===2",
            "canonical receipt discovery",
        )
        recovered = await popup.evaluate(
            "Array.from(document.querySelectorAll('#recovery-list li'),e=>e.textContent)"
        )
        assert operation + " · accepted" in recovered
        checked = native(
            run,
            "operation.status",
            {},
            operation_id=operation,
            binding_id=add["request"]["binding_id"],
        )
        assert checked == add["response"]
        replay = native(
            run,
            "annotation.add",
            add["request"]["args"],
            operation_id=operation,
            binding_id=add["request"]["binding_id"],
        )
        assert replay == checked
        await activate(popup, "#show-highlights")
        await shown(
            popup,
            "document.querySelector('#quotes').children.length===1",
            "one canonical highlight",
        )
        assert [row["capture_id"] for row in run.hits("syntheticinstallednote")] == [parent]
        await run.close_popup()
        await run.cdp.send("Extensions.uninstall", {"id": run.id})
    assert status(run, "unregister") == "removed" and neighbor.is_file()

    async def foreground_processes_exited() -> bool:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "comm="], check=True, capture_output=True, text=True, timeout=5
        )
        return not any(
            line.startswith(str(home / "prefix") + "/") for line in processes.stdout.splitlines()
        )

    await until(foreground_processes_exited, "foreground native hosts to exit")
    # Remove only the isolated prefix wholly created by this run.
    shutil.rmtree(home / "prefix")
    assert not run.host.exists()
    assert len(run.hits("syntheticinstallednote")) == 1 and not run.blocked
    assert hashlib.sha256(cli.read_bytes()).hexdigest() == core_sha
    return {
        **evidence,
        "chrome": run.version,
        "extension_id": run.id,
        "host_archive_sha256": archive_sha,
        "host_version": metadata["version"],
        "host_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "extension_zip_sha256": hashlib.sha256(staged_zip.read_bytes()).hexdigest(),
        "core_sha256": core_sha,
        "recovered_visible_intents": len(recovered),
        "accepted_highlights": 1,
        "foreign_registration_refusals": 4,
        "observed_listeners": 0,
        "hosts_after_unregistration": 0,
    }


async def main() -> None:
    if not __debug__:
        raise RuntimeError("Run acceptance with assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True, type=Path)
    parser.add_argument("--prior-core", required=True, type=Path)
    parser.add_argument("--brain-cli", required=True, type=Path)
    parser.add_argument("--host-archive", required=True, type=Path)
    parser.add_argument("--zip", type=Path, default=ROOT / "build/browser/open-brain-bookmarks.zip")
    args = parser.parse_args()
    playwright = importlib.import_module("playwright.async_api")
    with tempfile.TemporaryDirectory(prefix="open-brain-installed-") as temp:
        async with playwright.async_playwright() as pw:
            print(json.dumps(await exercise(Path(temp).resolve(), args, pw), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
