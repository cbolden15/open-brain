from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_collector import sources_cli
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
from open_brain_collector.saved_item_folder import name_hash, select_folder
from open_brain_collector.saved_item_index import index_name, load_index
from open_brain_connectors.runtime.live_storage import PrivateJsonStore


def _cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = sources_cli.main(argv)
    return code, cast(dict[str, object], json.loads(output.getvalue()))


def _record(url: str, why: str, body: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "url": url,
            "why": why,
            "received_at": "2026-10-26T14:00:00Z",
            "fetch_status": "complete",
            "title": "Synthetic saved item",
            "body": body,
        }
    )


def test_saved_folder_flows_from_cli_selection_to_search(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    (folder / "first.json").write_text(
        _record(
            "https://example.com/post/123?utm_source=share#comments",
            "Saving for the Quietharbor dinner",
            "Charredlemon pasta with capers",
        )
    )
    state = tmp_path / "collector" / "state.json"
    base = ["--state", str(state), "--brain-root", str(tmp_path / "brain"), "--foreground"]

    code, selected = _cli([*base, "saved-item-select", "--folder-path", str(folder)])
    assert code == 0
    assert cast(dict[str, object], selected["options"])["privacy_tier"] == "personal"
    arguments = tmp_path / "selection.json"
    arguments.write_text(json.dumps(selected))
    code, configured = _cli([*base, "configure", "--arguments-file", str(arguments)])
    assert code == 0
    source_id = cast(str, configured["source_id"])
    code, preview = _cli([*base, "preview", "--source-id", source_id])
    assert code == 0 and preview["count"] == 1
    records = cast(list[dict[str, object]], preview["records"])
    assert records[0]["source_reference"] == "https://example.com/post/123"
    preview_id = cast(str, preview["preview_id"])
    code, imported = _cli([*base, "import", "--source-id", source_id, "--preview-id", preview_id])
    assert code == 0 and imported["captured_count"] == 1
    engine = open_local_engine(profile)
    for query in ("Charredlemon", "Quietharbor"):
        hits = engine.retrieval.search(query)
        assert len(hits) == 1 and hits[0].trust == "third_party"

    # Scheduled path: run-once drives LiveSourceManager.sync_due for enabled sources.
    (folder / "second.json").write_text(
        _record("https://example.com/post/456", "Gift idea for Brightmoss", "Ceramic teapot")
    )
    enable = ["control", "--source-id", source_id, "--action", "enable"]
    code, _enabled = _cli([*base, *enable, "--interval-seconds", "900"])
    assert code == 0
    results = LiveSourceManager(state.parent / "live", tmp_path / "brain").sync_due(
        process_developments=False
    )
    captured = [item.get("captured_count") for item in results if item["source_id"] == source_id]
    assert captured == [1]
    assert len(open_local_engine(profile).retrieval.search("Brightmoss")) == 1


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high", "low"])
@pytest.mark.parametrize(("field", "code"), [("why", "invalid_why"), ("body", "invalid_field")])
def test_mixed_folder_refuses_surrogates_before_custody(
    tmp_path: Path, field: str, code: str, surrogate: str
) -> None:
    root = tmp_path.resolve()
    profile = compile_single_user_local(root / "brain")
    folder = root / "inbox"
    folder.mkdir(mode=0o700)
    (folder / "valid.json").write_text(
        _record("https://example.com/post/valid", "Dinner idea", "Validharbor pasta"),
        encoding="utf-8",
    )
    refused_name = "malformed\nentry\x07.json"
    malformed = json.loads(_record("https://example.com/post/refused", "Refused reason", "Body"))
    malformed[field] = f"Synthetic {surrogate} text"
    (folder / refused_name).write_bytes(json.dumps(malformed, ensure_ascii=True).encode("ascii"))
    selection, resolved = select_folder(str(folder))
    runtime = PriorityRuntime(root / "live")
    store = PrivateJsonStore(runtime.root / "saved_items")
    options: dict[str, object] = {"folder_path": str(resolved)}
    expected_notices = (
        "saved_item_count:delivered:1",
        f"saved_item_count:{code}:1",
        f"saved_item_refused:{code}:{name_hash(refused_name)[:12]}",
    )

    # Real scans propose the refusal, but do not persist it before acknowledge.
    first = runtime.fetch(selection, options, None)
    assert len(first.intakes) == 1
    assert first.notices == expected_notices
    assert load_index(store, index_name(selection)).entries == {}
    again = runtime.fetch(selection, options, None)
    assert again == first
    assert load_index(store, index_name(selection)).entries == {}

    service = LiveCaptureService(root / "state", root / "brain", runtime=runtime)
    service.configure("saved", selection, {**options, "privacy_tier": "personal"})
    preview = service.preview("saved")
    assert preview["count"] == 1
    assert preview["notices"] == list(expected_notices)
    assert load_index(store, index_name(selection)).entries == {}
    imported = service.apply("saved", cast(str, preview["preview_id"]))
    assert imported["captured_count"] == 1
    assert imported["duplicate_count"] == imported["quarantined_count"] == 0
    assert imported["notices"] == list(expected_notices)

    # Apply commits the checkpoint and acknowledges both terminal outcomes.
    index = load_index(store, index_name(selection))
    assert set(index.entries) == {name_hash("valid.json"), name_hash(refused_name)}
    assert index.entries[name_hash("valid.json")].endswith(":delivered")
    assert index.entries[name_hash(refused_name)].endswith(f":{code}")
    assert len(index.delivered) == 1
    assert "malformed" not in json.dumps(imported)
    hits = open_local_engine(profile).retrieval.search("Validharbor")
    assert len(hits) == 1 and hits[0].trust == "third_party"
    assert service.preview("saved")["count"] == 0


def test_v18_saved_item_recovery_evidence(tmp_path: Path) -> None:
    import sqlite3
    from zipfile import ZipFile

    from open_brain_connectors.runtime.saved_item import identity_url, item_key
    from open_brain_connectors.runtime.source_intake import SourceRecordKey

    from open_brain.services.local_entrypoints import run_cli

    folder = "folder:62fc0a49f952534a96a8a1ef8e6f34cf"
    resource = "items:1f922d4e9e0568ec069490bbcee38798"
    archive = tmp_path / "archive"
    with ZipFile(Path(__file__).parents[1] / "fixtures/saved-item-v18.zip") as fixture:
        for name in fixture.namelist():
            raw = fixture.read(name)
            assert folder.encode() not in raw and resource.encode() not in raw
        fixture.extractall(archive)
    archive.chmod(0o700)
    restored = tmp_path / "restored"
    assert (
        run_cli(
            (
                "restore",
                str(archive),
                str(restored),
                "--import-id",
                "import_11111111-1111-4111-8111-111111111119",
            )
        )
        == 0
    )
    engine = open_local_engine(compile_single_user_local(restored))
    hits = engine.retrieval.search("Legacyharbor")
    assert len(hits) == 2
    with sqlite3.connect(restored / ".open-brain/state/phase1.sqlite3") as db:
        expected = {
            SourceRecordKey(
                "saved_item",
                folder,
                resource,
                item_key(identity_url(f"https://example.org/saved#{i}")[0]),
                "v1",
            ).delivery_id()
            for i in range(2)
        }
        assert expected == {row[0] for row in db.execute("SELECT delivery_id FROM captures")}
