"""Refresh hashes after the coordinator integrates exact C0 schema bytes.

Run this script by its absolute path from any directory.
The manifest deliberately excludes its own hash; record that digest in the workstream ledger.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path


def refresh_manifest() -> None:
    fixture_root = Path(__file__).resolve().parent
    repo = fixture_root.parents[4]
    engine = repo / "packages/engine/src/open_brain_engine/engine"
    manifest_path = fixture_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_paths = [engine / "facet_evidence_schema.json", engine / "facet_schema.json"]
    manifest["schemas"] = {
        str(path.relative_to(repo)): sha256(path.read_bytes()).hexdigest()
        for path in schema_paths if path.exists()
    }
    for fixture in manifest["fixtures"] + manifest.get("wire_fixtures", []):
        fixture["sha256"] = sha256((fixture_root / fixture["path"]).read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(sha256(manifest_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    refresh_manifest()
