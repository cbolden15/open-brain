"""Native bookmark imports commit an immutable, resumable job."""

import sqlite3
from base64 import b64encode
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages

from open_brain.profile import open_existing_single_user_local
from open_brain.services.t03_adapters import owner_authority_for_principal
from open_brain_browser_host import operations, setup
from open_brain_browser_host.bookmark_import import CHUNK_BYTES, MAX_BYTES
from open_brain_browser_host.operations import Request, dispatch
from open_brain_browser_host.policy import Policy, Refused, load


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Policy:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    assert (
        setup.main(
            [
                "setup",
                "--extension-id",
                "a" * 32,
                "--data-dir",
                str(tmp_path / "brain"),
                "--operation",
                "save",
                "--operation",
                "lookup",
                "--operation",
                "status",
                "--initialize-brain",
                "--new-source",
                "--complete-domain",
                "--unassigned",
                "--tier",
                "personal",
                "--tier",
                "public",
                "--tier",
                "work",
            ]
        )
        == 0
    )
    return load()


def _call(policy: Policy, operation: str, **args: object) -> dict[str, object]:
    current = dispatch(policy, Request("describe", "", {}))["binding_id"]
    return dispatch(
        policy,
        Request.parse(
            {
                "version": "browser.v1",
                "operation": operation,
                "args": args,
                "binding_id": current,
            }
        ),
    )


def _upload(policy: Policy, data: bytes, *, format: str = "urls") -> str:
    batch = _call(
        policy,
        "import.begin",
        format=format,
        reason="Synthetic bookmark import",
        bytes=len(data),
        digest=sha256(data).hexdigest(),
        chunks=1,
    )["batch_id"]
    assert isinstance(batch, str)
    _call(policy, "import.chunk", batch_id=batch, index=0, data=b64encode(data).decode())
    return batch


def test_cancel_unconfirmed_upload_never_saves_a_page(installation: Policy) -> None:
    batch = _upload(installation, b"https://example.test/bookmark\n")
    assert _call(installation, "import.cancel", batch_id=batch)["state"] == "canceled"
    assert (
        _call(installation, "page.lookup", url="https://example.test/bookmark")["state"] == "absent"
    )


def _confirm(policy: Policy, batch: str) -> str:
    preview = _call(policy, "import.preview", batch_id=batch, offset=0, limit=30)
    digest = preview["digest"]
    assert isinstance(digest, str)
    assert _call(policy, "import.confirm", batch_id=batch, digest=digest) == {"state": "active"}
    return digest


def test_html_decodes_entities_and_previews_only_safe_urls(installation: Policy) -> None:
    data = (
        b'<script>"<a href=https://fetch.example.test>"</script>'
        b'<img src="https://images.example.test"><a href="https://example.test/post?'
        b'item=one&amp;utm_source=first#section">escaped</a>'
        b'<a href="https://example.test/post?item=one&amp;utm_source=second#section">duplicate</a>'
        b'<a href="http://example.test/bad?token=private#secret">non-HTTPS</a>'
    )
    batch = _upload(installation, data, format="html")
    first = _call(installation, "import.preview", batch_id=batch, offset=0, limit=1)
    assert first["total"] == 3 and first["next_offset"] == 1
    assert first["rows"] == [{"index": 0, "state": "ready", "url": "https://example.test/post"}]
    remaining = _call(installation, "import.preview", batch_id=batch, offset=1, limit=10)
    assert remaining["rows"] == [
        {"index": 1, "state": "duplicate", "url": ""},
        {"index": 2, "state": "skipped", "url": ""},
    ]
    with pytest.raises(Refused, match="import_confirmation_mismatch"):
        _call(installation, "import.confirm", batch_id=batch, digest="0" * 64)
    digest = _confirm(installation, batch)
    assert _call(installation, "import.confirm", batch_id=batch, digest=digest) == {
        "state": "active"
    }
    expected = {"state": "complete", "processed": 3, "total": 3}
    assert _call(installation, "import.step", batch_id=batch) == expected
    assert _call(installation, "import.step", batch_id=batch) == expected
    assert _call(installation, "import.confirm", batch_id=batch, digest=digest) == {
        "state": "complete"
    }
    status = _call(installation, "import.status", batch_id=batch, offset=0, limit=30)
    assert status["rows"] == [
        {"index": 0, "state": "accepted"},
        {"index": 1, "state": "duplicate"},
        {"index": 2, "state": "skipped"},
    ]
    assert (
        _call(installation, "page.lookup", url="https://example.test/post?item=one#section")[
            "state"
        ]
        == "saved"
    )
    assert (
        _call(installation, "page.lookup", url="https://images.example.test")["state"] == "absent"
    )
    with sqlite3.connect(Path.home() / ".open-brain-browser/imports/bookmarks.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM rows WHERE url IS NOT NULL").fetchone() == (0,)
        assert db.execute("SELECT reason FROM batches").fetchone() == (None,)


def test_chunk_authorization_limits_and_exact_replay(installation: Policy) -> None:
    data = b"<!--" + b"x" * CHUNK_BYTES + b'--><a href="https://example.test">'
    begin = {
        "format": "html",
        "reason": "Synthetic multichunk upload",
        "bytes": len(data),
        "digest": sha256(data).hexdigest(),
        "chunks": 2,
    }
    with pytest.raises(Refused, match="import_too_large"):
        _call(installation, "import.begin", **{**begin, "bytes": MAX_BYTES + 1})
    with pytest.raises(Refused, match="invalid_request"):
        _call(installation, "import.begin", **begin, path="/tmp/never-open")
    batch = _call(installation, "import.begin", **begin)["batch_id"]
    part = b64encode(data[:CHUNK_BYTES]).decode()
    args = {"batch_id": batch, "index": 0, "data": part}
    with pytest.raises(Refused, match="binding_changed"):
        dispatch(
            installation,
            Request.parse(
                {
                    "version": "browser.v1",
                    "operation": "import.chunk",
                    "binding_id": "f" * 64,
                    "args": args,
                }
            ),
        )
    _call(installation, "import.chunk", **args)
    assert _call(installation, "import.chunk", **args) == {"state": "upload"}
    with pytest.raises(Refused, match="import_chunk_conflict"):
        _call(
            installation, "import.chunk", **{**args, "data": b64encode(b"x" * CHUNK_BYTES).decode()}
        )
    with pytest.raises(Refused, match="import_incomplete"):
        _call(installation, "import.preview", batch_id=batch, offset=0, limit=30)
    final = {"batch_id": batch, "index": 1, "data": b64encode(data[CHUNK_BYTES:]).decode()}
    assert _call(installation, "import.chunk", **final) == {"state": "preview"}
    assert _call(installation, "import.chunk", **final) == {"state": "preview"}
    assert _call(installation, "import.preview", batch_id=batch, offset=0, limit=30)["total"] == 1


def test_revoked_grant_cannot_confirm_read_or_cancel(installation: Policy) -> None:
    batch = _upload(installation, b"https://example.test/private\n")
    digest = _call(installation, "import.preview", batch_id=batch, offset=0, limit=30)["digest"]
    profile = open_existing_single_user_local(installation.brain_root)
    owner = owner_authority_for_principal(principal_id=profile.owner_actor_id, session_id="revoke")
    BrowserOwnerSetup(profile, owner).revoke(installation.token)
    for operation, args in (
        ("import.confirm", {"digest": digest}),
        ("import.status", {"offset": 0, "limit": 1}),
        ("import.preview", {"offset": 0, "limit": 1}),
        ("import.cancel", {}),
    ):
        with pytest.raises(PermissionError, match="browser_unbound"):
            _call(installation, operation, batch_id=batch, **args)


@pytest.mark.parametrize("resume", [True, False])
def test_admitted_job_survives_expiry_and_lost_terminal_acknowledgment(
    installation: Policy, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    data = b"".join(f"https://example.test/item-{index}\n".encode() for index in range(5))
    batch = _upload(installation, data)
    _confirm(installation, batch)
    abandoned = _upload(installation, b"https://example.test/expired\n")
    with sqlite3.connect(Path.home() / ".open-brain-browser/imports/bookmarks.sqlite3") as db:
        db.execute("UPDATE batches SET touched=0,created=0")
    with pytest.raises(Refused, match="import_not_found"):
        _call(installation, "import.status", batch_id=abandoned, offset=0, limit=30)
    assert _call(installation, "import.step", batch_id=batch) == {
        "state": "active",
        "processed": 4,
        "total": 5,
    }

    class LostAck(Exception):
        pass

    save = operations._save_page

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        result = save(*args, **kwargs)
        assert result.state == "accepted"
        raise LostAck

    with monkeypatch.context() as interrupted:
        interrupted.setattr(operations, "_save_page", interrupt)
        with pytest.raises(LostAck):
            _call(installation, "import.step", batch_id=batch)
    status = _call(installation, "import.status", batch_id=batch, offset=0, limit=30)
    assert status["processed"] == 4
    assert _call(installation, "page.lookup", url="https://example.test/item-4")["state"] == "saved"
    if resume:
        assert _call(installation, "import.step", batch_id=batch) == {
            "state": "complete",
            "processed": 5,
            "total": 5,
        }
    else:
        assert _call(installation, "import.cancel", batch_id=batch) == {"state": "canceled"}
        status = _call(installation, "import.status", batch_id=batch, offset=4, limit=1)
        assert status["rows"] == [{"index": 4, "state": "accepted"}]
    with sqlite3.connect(installation.brain_root / ".open-brain/state/phase1.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone() == (5,)
        assert db.execute("SELECT count(*) FROM browser_operations").fetchone() == (5,)


def test_active_job_counts_against_staging_quota(installation: Policy) -> None:
    batches = [_upload(installation, b"https://example.test/item\n") for _ in range(4)]
    _confirm(installation, batches[0])
    with pytest.raises(Refused, match="import_preview_limit"):
        _upload(installation, b"https://example.test/later\n")
    _call(installation, "import.cancel", batch_id=batches[0])
    _upload(installation, b"https://example.test/later\n")


def test_bookmark_apply_and_folder_worker_share_first_save(
    installation: Policy,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from open_brain_browser_host.policy import encoded
    from open_brain_collector.lifecycle import EngineCaptureSink
    from open_brain_collector.runner import collector_capture_sink
    from open_brain_connectors.runtime.saved_item import SavedItemSourceAdapter, parse_record
    from open_brain_connectors.runtime.source_registry import SourceResourceSelection

    url = "https://example.test/concurrent-bookmark"
    batch = _upload(installation, (url + "\n").encode())
    _confirm(installation, batch)
    profile = open_existing_single_user_local(installation.brain_root)
    source = BrowserPages(profile).binding(installation.token).source
    record = parse_record(
        encoded(
            {
                "schema_version": 1,
                "url": url,
                "why": "Synthetic concurrent folder reason",
                "received_at": "2026-11-18T06:00:00Z",
                "fetch_status": "not_attempted",
            }
        )
    )
    intake = SavedItemSourceAdapter(SourceResourceSelection(*source.values(), "folder")).intake(
        record
    )
    barrier = Barrier(2)

    def browser() -> dict[str, object]:
        barrier.wait()
        return _call(installation, "import.step", batch_id=batch)

    def folder() -> object:
        barrier.wait()
        return EngineCaptureSink(
            collector_capture_sink(installation.brain_root, selected=(source, record.item_key))
        ).submit(intake)

    with ThreadPoolExecutor(max_workers=2) as pool:
        apply = pool.submit(browser)
        collector = pool.submit(folder)
        assert apply.result(timeout=8)["state"] == "complete"
        assert collector.result(timeout=8) is not None
    with sqlite3.connect(installation.brain_root / ".open-brain/state/phase1.sqlite3") as db:
        rows = db.execute("SELECT delivery_id,search_text FROM captures").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == source.delivery_id(record.item_key)
        (producer,) = db.execute("SELECT producer FROM saved_item_admissions").fetchone()
        assert (
            "Synthetic bookmark import"
            if producer == "browser"
            else "Synthetic concurrent folder reason"
        ) in rows[0][1]


def test_status_reconciles_canceled_pending_row_after_canonical_recovery(
    installation: Policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import BrainEngine, CaptureFault, InjectedFault
    from open_brain_engine.engine.contracts import ManagedWorkspaceFault, PortabilityFault

    url = "https://example.test/canceled-after-commit"
    batch = _upload(installation, (url + "\n").encode())
    _confirm(installation, batch)
    previous_fault = BrainEngine._fault

    def interrupt(
        self: BrainEngine, point: CaptureFault | PortabilityFault | ManagedWorkspaceFault
    ) -> None:
        if point is CaptureFault.AFTER_JOURNAL_COMMIT:
            raise InjectedFault(point)
        previous_fault(self, point)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(BrainEngine, "_fault", interrupt)
        with pytest.raises(InjectedFault):
            _call(installation, "import.step", batch_id=batch)
    assert _call(installation, "import.cancel", batch_id=batch) == {"state": "canceled"}
    assert _call(installation, "import.status", batch_id=batch, offset=0, limit=1)["rows"] == [
        {"index": 0, "state": "pending"}
    ]
    root = installation.brain_root
    profile = open_existing_single_user_local(root)
    assert BrainEngine.open(profile, recover_on_open=False).recover() == 1

    expected = {
        "state": "canceled",
        "total": 1,
        "processed": 1,
        "rows": [{"index": 0, "state": "accepted"}],
        "next_offset": None,
    }
    assert _call(installation, "import.status", batch_id=batch, offset=0, limit=1) == expected
    assert _call(installation, "import.status", batch_id=batch, offset=0, limit=1) == expected
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM browser_operations").fetchone() == (1,)
    with sqlite3.connect(Path.home() / ".open-brain-browser/imports/bookmarks.sqlite3") as db:
        assert db.execute("SELECT url,display FROM rows").fetchone() == (None, None)
        assert db.execute("SELECT reason FROM batches").fetchone() == (None,)
        assert db.execute("SELECT count(*) FROM chunks").fetchone() == (0,)
