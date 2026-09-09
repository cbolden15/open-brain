"""Guard the contributor smoke's reserved tap and preserve the installed product."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

TAP = "open-brain-local/smoke"
FORMULA = f"{TAP}/open-brain-smoke"
MARKER = ".open-brain-smoke-owned"
OWNERSHIP = b"tap=open-brain-local/smoke\nformula=open-brain-smoke\nversion=1\n"


def brew(*arguments: str) -> str:
    return subprocess.run(
        ["brew", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()


def installed() -> list[str]:
    return brew("list", "--formula", "--full-name").splitlines()


def link_state(path: Path) -> dict[str, str]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return {"kind": "absent"}
    if stat.S_ISLNK(mode):
        return {"kind": "symlink", "target": os.readlink(path)}
    if stat.S_ISREG(mode):
        return {"kind": "file", "sha256": digest(path)}
    raise ValueError("unexpected Homebrew product link type")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def absolute_prefix(*arguments: str) -> Path:
    path = Path(brew("--prefix", *arguments))
    if not path.is_absolute():
        raise ValueError("Homebrew returned a non-absolute prefix")
    return path


def snapshot() -> dict[str, object]:
    products = [name for name in installed() if name.split("/")[-1] == "open-brain"]
    if len(products) > 1 or any(name.startswith(f"{TAP}/") for name in products):
        raise ValueError("ambiguous or reserved-tap product installation; refusing smoke")
    result: dict[str, object] = {
        "product": None,
        "link": link_state(absolute_prefix() / "bin/open-brain"),
    }
    if products:
        name = products[0]
        prefix = absolute_prefix(name)
        versions = brew("list", "--formula", "--versions", name)
        if not versions:
            raise ValueError("cannot read installed product version")
        result["product"] = {
            "formula": name,
            "versions": versions,
            "prefix": str(prefix),
            "resolved_prefix": str(prefix.resolve(strict=True)),
            "sha256": digest(prefix / "bin/open-brain"),
        }
    return result


def cleanup() -> None:
    """Fail closed on foreign state; never uninstall the product or force untap."""
    names = installed()
    repository = Path(brew("--repository", TAP))
    if not repository.is_absolute():
        raise ValueError("Homebrew returned a non-absolute tap path")
    owned_names = [name for name in names if name.startswith(f"{TAP}/")]
    if not repository.exists():
        if owned_names:
            raise ValueError("smoke formula has no ownership marker; refusing cleanup")
        return
    marker = repository / MARKER
    if (
        repository.is_symlink()
        or not marker.is_file()
        or marker.is_symlink()
        or marker.read_bytes() != OWNERSHIP
    ):
        raise ValueError("reserved smoke tap is not smoke-owned; refusing cleanup")
    if any(name != FORMULA for name in owned_names):
        raise ValueError("reserved smoke tap has another installed formula; refusing cleanup")
    if FORMULA in names:
        brew("uninstall", "--force", FORMULA)
    brew("untap", TAP)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("snapshot", "verify", "cleanup", "mark"))
    parser.add_argument("path", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        if args.operation == "cleanup":
            cleanup()
        elif args.path is None:
            parser.error("path is required")
        elif args.operation == "mark":
            (args.path / MARKER).write_bytes(OWNERSHIP)
        elif args.operation == "snapshot":
            args.path.write_text(json.dumps(snapshot(), sort_keys=True), encoding="utf-8")
        else:
            before = json.loads(args.path.read_text(encoding="utf-8"))
            if before != snapshot():
                raise ValueError("existing Open Brain installation changed during smoke")
            print(
                "existing_product: preserved" if before["product"] else "existing_product: absent"
            )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Do not dump Homebrew output or local installation paths into public logs.
        message = str(error) if isinstance(error, ValueError) else "Homebrew smoke guard failed"
        parser.exit(1, f"{message}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
