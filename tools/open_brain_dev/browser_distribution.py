"""Build the unpacked Chrome candidate as a reproducible Web Store upload ZIP."""

import argparse
import base64
import hashlib
import json
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# Every shipped file is reviewed. Test scaffolding and local metadata stay outside the package.
FILES = (
    "anchors.js",
    "capture.mjs",
    "import-file.mjs",
    "icon128.png",
    "manifest.json",
    "organize.mjs",
    "popup.css",
    "popup.html",
    "popup.js",
    "revisits.mjs",
    "worker.js",
)


def extension_id(manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    return "".join(chr(97 + int(nibble, 16)) for nibble in digest)


def package_extension(root: Path, output: Path) -> str:
    extension = root / "packages/browser-extension/extension"
    files = {name: extension / name for name in FILES}
    files.update({name: root / name for name in ("LICENSE", "NOTICE")})
    with TemporaryDirectory(dir=output.parent) as staging:
        candidate = Path(staging) / "candidate.zip"
        with zipfile.ZipFile(candidate, "x") as archive:
            for name, path in sorted(files.items()):
                entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
        candidate.replace(output)
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    digest = package_extension(root, args.output)
    manifest = json.loads(
        (root / "packages/browser-extension/extension/manifest.json").read_bytes()
    )
    print(
        json.dumps(
            {
                "path": str(args.output),
                "sha256": digest,
                "version": manifest["version"],
                "extension_id": extension_id(manifest),
            },
            sort_keys=True,
        )
    )
