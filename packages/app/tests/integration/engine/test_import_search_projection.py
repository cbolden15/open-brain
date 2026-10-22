"""Imported captures index exactly the text and title the record projector reads."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, FilePayload, TextPayload
from open_brain_engine.engine.materializer import _payload_search_text
from open_brain_engine.engine.owner_egress_policy import OwnerEgressPolicyTasks
from open_brain_engine.engine.paging import read_snapshot
from open_brain_engine.engine.reconciliation import rederive_live_search_projection
from open_brain_engine.engine.records import RecordProjector
from open_brain_engine.engine.search_projection import (
    source_search_title,
    upsert_search_document,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, SearchPageRequest

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority
from packages.app.tests.unit.engine.test_owner_egress_policy import _external

FIXTURE_ROOT = Path(str(files("open_brain_engine.portable").joinpath("conformance/v1/brain-root")))
_FIXTURE_FILE = "capture_123e4567-e89b-42d3-a456-426614174101"

_FILE_BODY = "Imported attachment heading\n\nThe zircondrift phrase lives only in this file body.\n"
# Redaction shortens the first line, so a title cut before redaction differs.
_LONG_FIRST_LINE = (
    "Imported note about /Users/synthetic/" + "deeply/nested/" * 12 + "notes.md and the "
    + "beryllium " * 12
)


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=()))


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="import-search-owner")


def _imported(tmp_path: Path) -> tuple[BrainEngine, str, str]:
    tmp_path.chmod(0o700)
    source = _engine(tmp_path / "source")
    file_capture = source.capture.accept(
        FilePayload("attachment.md", "text/markdown", _FILE_BODY.encode()),
        delivery_id="import-search.file",
    ).capture_id
    text_capture = source.capture.accept(
        TextPayload(_LONG_FIRST_LINE + "\nsecond line"),
        delivery_id="import-search.text",
    ).capture_id
    source.tasks.portability.export(
        tmp_path / "exported", export_id="export_123e4567-e89b-42d3-a456-4266141740e1"
    )
    source.tasks.portability.import_clean(
        tmp_path / "exported",
        tmp_path / "imported",
        import_id="import_123e4567-e89b-42d3-a456-4266141740e2",
    )
    return _engine(tmp_path / "imported"), file_capture, text_capture


def _found(engine: BrainEngine, query: str) -> dict[str, dict[str, object]]:
    page = engine.retrieval.search_page(
        SearchPageRequest(query=query, limit=10), authority=_owner(engine)
    ).to_wire()
    rows = cast(list[dict[str, object]], page["results"])
    return {str(row["record_id"]): row for row in rows}


def _imported_fixture(tmp_path: Path) -> BrainEngine:
    """Import the frozen v1 root, whose file capture retains only blob metadata."""
    tmp_path.chmod(0o700)
    _engine(tmp_path / "control").tasks.portability.import_clean(
        FIXTURE_ROOT,
        tmp_path / "imported",
        import_id="import_123e4567-e89b-42d3-a456-4266141740e3",
    )
    return _engine(tmp_path / "imported")


def test_imported_portable_file_capture_is_searchable_by_its_blob_text(tmp_path: Path) -> None:
    engine = _imported_fixture(tmp_path)
    found = _found(engine, "blob")
    assert set(found) == {_FIXTURE_FILE}
    assert found[_FIXTURE_FILE]["title"] == "fixture.txt text/plain Synthetic Portable Brain blob."
    # The file-name metadata the older index held must stay matchable without staleness.
    assert set(_found(engine, "fixture")) == {_FIXTURE_FILE}


def test_round_tripped_file_capture_is_searchable_by_its_file_body(tmp_path: Path) -> None:
    engine, file_capture, _text = _imported(tmp_path)
    found = _found(engine, "zircondrift")
    assert set(found) == {file_capture}
    assert found[file_capture]["title"] == "attachment.md text/markdown Imported attachment heading"


def test_imported_untitled_text_capture_title_agrees(tmp_path: Path) -> None:
    engine, _file, text_capture = _imported(tmp_path)
    found = _found(engine, "beryllium")
    assert set(found) == {text_capture}


def _write_drifted_rows(engine: BrainEngine) -> None:
    """Rewrite every live source row exactly as builds before the shared projection did."""
    with engine._writer_lease.acquire_shared_writer(), engine._store.transaction() as connection:
        for capture in connection.execute(
            "SELECT c.* FROM captures c JOIN search_documents d "
            "ON d.result_id = c.capture_id AND d.record_type = 'source'"
        ).fetchall():
            family = str(capture["payload_family"])
            body = _payload_search_text(json.loads(capture["payload_json"]))
            upsert_search_document(
                connection,
                result_id=capture["capture_id"],
                capture_id=capture["capture_id"],
                record_type="source",
                payload_family=family,
                space_id=capture["space_id"],
                title=(
                    capture["title"]
                    if capture["title"] is not None
                    else source_search_title(payload_family=family, body=body)
                ),
                body=body,
                canonical_path=None,
                updated_at=capture["accepted_at"],
            )


def _mismatched(engine: BrainEngine) -> set[str]:
    """Source rows whose body or title differ from what the record projector reads."""
    stale: set[str] = set()
    with read_snapshot(engine) as connection:
        projector = RecordProjector(engine.profile, connection, _owner(engine))
        rows = connection.execute(
            "SELECT result_id, title, body FROM search_documents WHERE record_type='source'"
        ).fetchall()
        assert rows
        for row in rows:
            projected = projector.read(row["result_id"])
            if (row["body"], row["title"]) != (
                projected.indexed_text,
                projected.summary["title"],
            ):
                stale.add(str(row["result_id"]))
    return stale


def _with_owner_captures(engine: BrainEngine) -> tuple[str, str]:
    file_capture = engine.capture.accept(
        FilePayload("owner.md", "text/markdown", _FILE_BODY.encode()),
        delivery_id="import-search.owner-file",
    ).capture_id
    text_capture = engine.capture.accept(
        TextPayload(_LONG_FIRST_LINE + "\nowner second line"),
        delivery_id="import-search.owner-text",
    ).capture_id
    return file_capture, text_capture


def test_rederive_heals_drifted_rows_for_every_capture_kind(tmp_path: Path) -> None:
    engine = _imported_fixture(tmp_path)
    owner_file, owner_text = _with_owner_captures(engine)
    _write_drifted_rows(engine)
    assert {_FIXTURE_FILE, owner_text} <= _mismatched(engine)
    rederive_live_search_projection(engine)
    assert _mismatched(engine) == set()
    with read_snapshot(engine) as connection:
        families = {
            str(row[0])
            for row in connection.execute(
                "SELECT payload_family FROM search_documents WHERE record_type='source'"
            )
        }
    assert families == {"text", "reference_or_file", "event", "measurement"}
    assert set(_found(engine, "blob")) == {_FIXTURE_FILE}
    assert set(_found(engine, "zircondrift")) == {owner_file}
    assert owner_text in _found(engine, "beryllium")


def test_rederive_leaves_retained_capture_evidence_unchanged(tmp_path: Path) -> None:
    engine = _imported_fixture(tmp_path)
    _with_owner_captures(engine)

    def retained() -> list[tuple[object, ...]]:
        with read_snapshot(engine) as connection:
            return [
                tuple(row)
                for row in connection.execute("SELECT * FROM captures ORDER BY capture_id")
            ]

    before = retained()
    _write_drifted_rows(engine)
    rederive_live_search_projection(engine)
    assert retained() == before


def test_owner_file_and_text_rows_are_unchanged_by_rederive(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    _with_owner_captures(engine)
    engine.capture.accept(TextPayload("Short owner note"), delivery_id="import-search.short")

    def rows() -> list[tuple[object, ...]]:
        with read_snapshot(engine) as connection:
            return [
                tuple(row)
                for row in connection.execute(
                    "SELECT result_id, title, body FROM search_documents ORDER BY result_id"
                )
            ]

    before = rows()
    rederive_live_search_projection(engine)
    assert rows() == before
    assert _mismatched(engine) == set()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unreadable_file_blob_is_left_out_of_the_index(tmp_path: Path, damage: str) -> None:
    engine = _imported_fixture(tmp_path)
    blobs = [path for path in (engine.profile.root / "sources/blobs").rglob("*") if path.is_file()]
    assert len(blobs) == 1
    blobs[0].chmod(0o600)
    if damage == "missing":
        blobs[0].unlink()
    else:
        blobs[0].write_bytes(b"tampered blob bytes")
    rederive_live_search_projection(engine)
    with read_snapshot(engine) as connection:
        indexed = {
            str(row[0]) for row in connection.execute("SELECT result_id FROM search_documents")
        }
    assert _FIXTURE_FILE not in indexed
    assert "capture_123e4567-e89b-42d3-a456-426614174100" in indexed
    assert _found(engine, "fixture") == {}
    assert _found(engine, "blob") == {}
    assert _mismatched(engine) == set()


def test_external_provider_finds_an_imported_work_file_when_allowed(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    source = _engine(tmp_path / "source")
    work = source.capture.accept(
        FilePayload("work.md", "text/markdown", _FILE_BODY.encode()),
        delivery_id="import-search.work-file",
        privacy_tier=PrivacyTier.WORK,
    ).capture_id
    source.tasks.portability.export(
        tmp_path / "exported", export_id="export_123e4567-e89b-42d3-a456-4266141740e4"
    )
    source.tasks.portability.import_clean(
        tmp_path / "exported",
        tmp_path / "imported",
        import_id="import_123e4567-e89b-42d3-a456-4266141740e5",
    )
    engine = _engine(tmp_path / "imported")
    _write_drifted_rows(engine)
    rederive_live_search_projection(engine)
    request = SearchPageRequest(query="zircondrift", limit=10)

    def found(*consented: PrivacyTier) -> set[str]:
        page = engine.retrieval.search_page(request, authority=_external(engine, *consented))
        rows = cast(list[dict[str, object]], page.to_wire()["results"])
        return {str(row["record_id"]) for row in rows}

    assert found(PrivacyTier.WORK) == set()
    OwnerEgressPolicyTasks(engine).set(
        {PrivacyTier.WORK}, operation_id="import-search.work", authority=_owner(engine)
    )
    assert found(PrivacyTier.WORK) == {work}
    assert found(PrivacyTier.PERSONAL) == set()
