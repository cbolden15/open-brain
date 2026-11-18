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

    from open_brain.services.local_entrypoints import run_cli
    from open_brain.services.t03_adapters import owner_authority_for_principal
    from open_brain_connectors.runtime.saved_item import identity_url, item_key
    from open_brain_connectors.runtime.source_intake import SourceRecordKey

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

    from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages
    from open_brain_engine.engine.saved_item_identity import SavedItemSource

    profile = compile_single_user_local(restored)
    setup = BrowserOwnerSetup(
        profile,
        owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="setup"),
    )
    source = SavedItemSource("saved_item", folder, resource)
    key = item_key(identity_url("https://example.org/saved#0")[0])
    assert setup.sources() == ()
    with pytest.raises(ValueError, match="legacy_rebinding_requires_original_source"):
        setup.bind(source, tiers=("personal",), spaces=(None,))
    with pytest.raises(ValueError, match="legacy_rebinding_requires_original_source"):
        setup.register_legacy(SavedItemSource("saved_item", "folder:" + "0" * 32, resource), key)
    assert setup.sources() == ()
    setup.register_legacy(source, key)
    grant = setup.bind(source, tiers=("personal",), spaces=(None,))
    assert BrowserPages(profile).lookup(grant, key) is not None


def test_fresh_brain_has_installation_local_generation(tmp_path: Path) -> None:
    import sqlite3

    generations = set()
    for name in ("original", "restored"):
        root = tmp_path / name
        open_local_engine(compile_single_user_local(root))
        with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
            assert db.execute("SELECT * FROM saved_item_sources").fetchall() == []
            generations.add(db.execute("SELECT generation FROM browser_installation").fetchone()[0])
    assert len(generations) == 2


def test_saved_page_identity_round_trip(tmp_path: Path) -> None:
    import shutil

    from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages
    from open_brain_engine.engine.saved_item_identity import SavedItemSource

    from open_brain.services.local_entrypoints import run_cli
    from open_brain.services.t03_adapters import owner_authority_for_principal
    from open_brain_connectors.runtime.saved_item import identity_url, item_key

    producer = tmp_path / "producer"
    producer.mkdir(mode=0o700)
    root = producer / "brain"
    profile = compile_single_user_local(root)
    state = producer / "collector/state.json"
    base = ["--state", str(state), "--brain-root", str(root), "--foreground"]
    sources = []
    urls = ("https://example.org/post#one", "https://example.org/post#two")
    for n, pages in enumerate((urls, urls[:1])):
        folder = producer / f"inbox{n}"
        folder.mkdir(mode=0o700)
        for i, url in enumerate(pages):
            (folder / f"{i}.json").write_text(_record(url, "Harborwitness", "Saved"))
        code, selection = _cli([*base, "saved-item-select", "--folder-path", str(folder)])
        assert code == 0
        configured = producer / f"setup{n}.json"
        configured.write_text(json.dumps(selection))
        code, config = _cli([*base, "configure", "--arguments-file", str(configured)])
        assert code == 0
        src_id = str(config["source_id"])
        code, preview = _cli([*base, "preview", "--source-id", src_id])
        assert code == 0
        code, result = _cli(
            [*base, "import", "--source-id", src_id, "--preview-id", str(preview["preview_id"])]
        )
        assert code == 0 and result["captured_count"] == len(pages)
        identity, _ = select_folder(str(folder))
        sources.append(
            SavedItemSource(identity.connector_name, identity.connection_id, identity.resource_id)
        )
    owner = owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="setup")
    setup = BrowserOwnerSetup(profile, owner)
    assert set(setup.sources()) == set(sources)
    first_key = item_key(identity_url(urls[0])[0])
    second_key = item_key(identity_url(urls[1])[0])
    old = setup.bind(sources[0], tiers=("personal",), spaces=(None,))
    first = BrowserPages(profile).lookup(old, first_key)
    second = BrowserPages(profile).lookup(old, second_key)
    assert first and second and first != second
    import sqlite3

    from open_brain_engine.engine import PortabilityFault
    from open_brain_engine.engine.portability import restore_portable_clean
    from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        prior_grant = db.execute("SELECT * FROM browser_grants").fetchone()
    exported = tmp_path / "archive"
    assert run_cli(("export", str(exported), "--verify", "--data-dir", str(root))) == 0
    shutil.rmtree(producer)
    restored = tmp_path / "restored"
    restore = (
        "restore",
        str(exported),
        str(restored),
        "--import-id",
        "import_11111111-1111-4111-8111-111111111117",
    )

    def interrupt(point: PortabilityFault) -> None:
        if point is PortabilityFault.BEFORE_PROMOTION:
            raise RuntimeError("synthetic restore interruption")

    with pytest.raises(RuntimeError, match="synthetic restore interruption"):
        restore_portable_clean(
            exported, restored, import_id=restore[-1], authority=owner, checkpoint=interrupt
        )
    assert not restored.exists()
    assert run_cli(restore) == 0
    assert run_cli(restore) == 0
    profile = compile_single_user_local(restored)
    pages_api = BrowserPages(profile)
    setup = BrowserOwnerSetup(profile, owner)
    assert set(setup.sources()) == set(sources)
    # Model an old installation policy surviving a same-host restore. Even its token
    # hash, canonical Brain identity, scopes and source cannot restore its authority.
    with sqlite3.connect(restored / ".open-brain/state/phase1.sqlite3") as db:
        assert not db.execute("SELECT * FROM browser_grants").fetchall()
        db.execute("INSERT INTO browser_grants VALUES(?,?,?,?,?,?,?,?,?)", prior_grant)
    for token in ("", old):
        for operation in ("enumerate", "lookup", "status", "save"):
            with pytest.raises(PermissionError, match="browser_unbound"):
                pages_api.require(token, operation)
    grant = setup.bind(sources[0], tiers=("personal",), spaces=(None,))
    assert (
        pages_api.lookup(
            grant, item_key(identity_url("https://example.org/post?utm_source=test#one")[0])
        )
        == first
    )
    assert pages_api.lookup(grant, second_key) == second
    with pytest.raises(PermissionError):
        pages_api.require(grant, "save")
    other = setup.bind(sources[1], tiers=("personal",), spaces=(None,))
    different = pages_api.lookup(other, first_key)
    assert different and different not in (first, second)
    assert pages_api.lookup(other, second_key) is None
    restricted = setup.bind(sources[1], tiers=("work",), spaces=(None,))
    assert pages_api.lookup(restricted, first_key) is None
    with sqlite3.connect(restored / ".open-brain/state/phase1.sqlite3") as db:
        logical, generation = db.execute(
            "SELECT source_id,lifecycle_version FROM logical_sources "
            "JOIN source_lifecycle_state USING(source_id) WHERE head_capture_id=?",
            (different.capture_id,),
        ).fetchone()
        brain, epoch = db.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    tasks = open_local_engine(profile)
    assert tasks.sources is not None
    from open_brain_engine.engine.t03_contracts import SourceRouteRequest

    destination = tasks.inbox.create_space("Synthetic filing", delivery_id="saved-page-space")
    tasks.sources.route(
        SourceRouteRequest(
            source_id=logical,
            space_id=destination.space_id,
            expected_head=different.capture_id,
            expected_route_version=0,
            operation_id="operation_11111111-1111-4111-8111-111111111112",
        ),
        authority=owner,
    )
    assert pages_api.lookup(other, first_key) is None
    filed = setup.bind(sources[1], tiers=("personal",), spaces=(destination.space_id,))
    visible = pages_api.lookup(filed, first_key)
    assert (
        visible
        and visible.capture_id == different.capture_id
        and visible.space_id == destination.space_id
    )
    tasks.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.synthetic-test",
            source_id=logical,
            expected_head=different.capture_id,
            expected_lifecycle_version=generation,
            brain_id=brain,
            issuer_epoch=epoch,
            reason_code="owner_request",
        ),
        authority=owner,
    )
    assert pages_api.lookup(other, first_key) is None
    assert pages_api.lookup(filed, first_key) is None


def test_saved_item_authority_refuses_foreign_and_generic_jobs(tmp_path: Path) -> None:
    import sqlite3
    from dataclasses import replace

    from open_brain_engine.engine.browser_pages import BrowserOwnerSetup
    from open_brain_engine.engine.saved_item_identity import SavedItemSource

    from open_brain.services.t03_adapters import owner_authority_for_principal
    from open_brain_collector.lifecycle import EngineCaptureSink
    from open_brain_collector.runner import collector_capture_sink

    root = tmp_path / "brain"
    profile = compile_single_user_local(root)
    open_local_engine(profile)
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    (folder / "1.json").write_text(_record("https://example.org/sample", "why", "sample"))
    selection, _ = select_folder(str(folder))
    source = SavedItemSource(
        selection.connector_name, selection.connection_id, selection.resource_id
    )
    runtime = PriorityRuntime(tmp_path / "live")
    (item,) = runtime.fetch(selection, {"folder_path": str(folder)}, None).intakes
    generic = EngineCaptureSink(collector_capture_sink(root))
    with pytest.raises(PermissionError):
        generic.submit(item)
    owner = owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="setup")
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        brain, epoch = db.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
    for unauthorized in (
        replace(owner, owner=False),
        replace(owner, principal_id="other"),
        replace(owner, brain_id=brain, issuer_epoch=epoch + 1),
        replace(owner, brain_id="brn_" + "a" * 26, issuer_epoch=epoch),
    ):
        with pytest.raises(PermissionError):
            BrowserOwnerSetup(profile, unauthorized).sources()
        with pytest.raises(PermissionError):
            BrowserOwnerSetup(profile, unauthorized).register_legacy(source, item.key.external_id)
        with pytest.raises(PermissionError):
            BrowserOwnerSetup(profile, unauthorized).bind(
                source, tiers=("personal",), spaces=(None,)
            )
    assert BrowserOwnerSetup(profile, owner).sources() == ()
