from __future__ import annotations

import stat
from pathlib import Path

import pytest

from open_brain_connectors.runtime.calendar import CalendarEventRecord
from open_brain_connectors.runtime.google_calendar_contracts import (
    GoogleCalendarChange,
    GoogleCalendarError,
    GoogleCalendarPage,
    GoogleCalendarSelection,
    opaque_id,
)
from open_brain_connectors.runtime.google_calendar_sync import (
    GoogleCalendarSync,
    GoogleCalendarSyncStore,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake

_CONNECTION = "account:calendar-fixture"
_NATIVE_CALENDAR = "fixture@example.test"
_SOURCE_LINK = "https://www.google.com/calendar/event?eid=synthetic"


class _Provider:
    def __init__(self, responses: list[GoogleCalendarPage | GoogleCalendarError]) -> None:
        self.responses = responses
        self.calls: list[tuple[str | None, str | None]] = []

    @property
    def connection_id(self) -> str:
        return _CONNECTION

    def fetch_page(
        self,
        selection: GoogleCalendarSelection,
        *,
        page_token: str | None = None,
        sync_token: str | None = None,
    ) -> GoogleCalendarPage:
        assert selection.connection_id == self.connection_id
        self.calls.append((page_token, sync_token))
        response = self.responses.pop(0)
        if isinstance(response, GoogleCalendarError):
            raise response
        return response


class _Capture:
    def __init__(self, *, failure: RuntimeError | None = None) -> None:
        self.items: list[SourceRecordIntake] = []
        self.failure = failure

    def __call__(self, intake: SourceRecordIntake) -> None:
        if self.failure is not None:
            raise self.failure
        self.items.append(intake)


def test_prepare_persists_private_batch_and_apply_does_not_refetch(tmp_path: Path) -> None:
    selection = _selection()
    first = _change(selection, "one", "rev:1", description="Synthetic private first notes.")
    second = _change(selection, "two", "rev:2", description="Synthetic private second notes.")
    provider = _Provider(
        [
            GoogleCalendarPage((first,), next_page_token="page:2"),
            GoogleCalendarPage((second,), next_sync_token="sync:1"),
        ]
    )
    state_root = tmp_path / "calendar-state"
    sync = GoogleCalendarSync(provider, GoogleCalendarSyncStore(state_root), "brain:fixture")

    preview = sync.prepare(selection)

    assert preview["status"] == "ready"
    assert preview["record_count"] == 2
    assert "private first notes" not in repr(preview)
    assert provider.calls == [(None, None), ("page:2", None)]
    pending = next(state_root.glob("*.pending.json"))
    assert "Synthetic private first notes." in pending.read_text()
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600

    captured = _Capture()
    receipt = sync.apply(selection, _preview_id(preview), captured)

    assert receipt == {
        "checkpoint_committed": True,
        "known_count": 2,
        "preview_id": preview["preview_id"],
        "status": "completed",
        "submitted_count": 2,
    }
    assert provider.calls == [(None, None), ("page:2", None)]
    assert [item.text.splitlines()[0] for item in captured.items] == [
        "Source revision: rev:1",
        "Source revision: rev:2",
    ]
    assert not pending.exists()
    checkpoint = next(
        path
        for path in state_root.glob("calendar-*.json")
        if not path.name.endswith("pending.json")
    )
    assert "private first notes" not in checkpoint.read_text()
    assert sync.status(selection) == {
        "configured": True,
        "known_count": 2,
        "pending": False,
        "pending_preview_id": None,
        "status": "ready",
    }


def test_failed_capture_keeps_cursor_and_retries_without_network(tmp_path: Path) -> None:
    selection = _selection()
    provider = _Provider(
        [GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token="sync:1")]
    )
    sync = GoogleCalendarSync(
        provider,
        GoogleCalendarSyncStore(tmp_path / "state"),
        "brain:fixture",
    )
    preview = sync.prepare(selection)

    with pytest.raises(RuntimeError, match="synthetic capture failure"):
        sync.apply(
            selection,
            _preview_id(preview),
            _Capture(failure=RuntimeError("synthetic capture failure")),
        )

    assert sync.status(selection)["configured"] is False
    assert sync.status(selection)["pending"] is True
    captured = _Capture()
    receipt = sync.apply(selection, _preview_id(preview), captured)
    assert receipt["checkpoint_committed"] is True
    assert len(captured.items) == 1
    assert provider.calls == [(None, None)]


def test_expired_incremental_token_restarts_one_complete_full_scan(tmp_path: Path) -> None:
    selection = _selection()
    store = GoogleCalendarSyncStore(tmp_path / "state")
    initial_provider = _Provider(
        [GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token="sync:1")]
    )
    initial = GoogleCalendarSync(initial_provider, store, "brain:fixture")
    preview = initial.prepare(selection)
    initial.apply(selection, _preview_id(preview), _Capture())

    provider = _Provider(
        [
            GoogleCalendarError("google_calendar_sync_expired"),
            GoogleCalendarPage(
                (_change(selection, "one", "rev:2"),),
                next_sync_token="sync:2",
            ),
        ]
    )
    sync = GoogleCalendarSync(provider, store, "brain:fixture")
    refreshed = sync.prepare(selection)

    assert refreshed["full_sync"] is True
    assert provider.calls == [(None, "sync:1"), (None, None)]


def test_full_resync_reconciles_missing_event_with_same_source_identity(tmp_path: Path) -> None:
    selection = _selection()
    store = GoogleCalendarSyncStore(tmp_path / "state")
    initial_provider = _Provider(
        [
            GoogleCalendarPage(
                (_change(selection, "one", "rev:1", description="Old sensitive details."),),
                next_sync_token="sync:1",
            )
        ]
    )
    initial = GoogleCalendarSync(initial_provider, store, "brain:fixture")
    first_preview = initial.prepare(selection)
    original = _Capture()
    initial.apply(selection, _preview_id(first_preview), original)

    provider = _Provider(
        [
            GoogleCalendarError("google_calendar_sync_expired"),
            GoogleCalendarPage((), next_sync_token="sync:2"),
        ]
    )
    sync = GoogleCalendarSync(provider, store, "brain:fixture")
    missing_preview = sync.prepare(selection)
    replacement = _Capture()
    sync.apply(selection, _preview_id(missing_preview), replacement)

    assert missing_preview["record_count"] == 1
    assert replacement.items[0].key.delivery_id() == original.items[0].key.delivery_id()
    assert replacement.items[0].source_reference == original.items[0].source_reference
    assert replacement.items[0].source_reference == _SOURCE_LINK
    assert replacement.items[0].privacy == original.items[0].privacy
    assert "Calendar event unavailable" in replacement.items[0].text
    assert "Old sensitive details" not in replacement.items[0].text


def test_known_cancellation_reuses_first_link_and_unknown_tombstone_is_ignored(
    tmp_path: Path,
) -> None:
    selection = _selection()
    store = GoogleCalendarSyncStore(tmp_path / "state")
    initial_provider = _Provider(
        [GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token="sync:1")]
    )
    initial = GoogleCalendarSync(initial_provider, store, "brain:fixture")
    first = initial.prepare(selection)
    original = _Capture()
    initial.apply(selection, _preview_id(first), original)

    known_id = _event_id("one")
    provider = _Provider(
        [
            GoogleCalendarPage(
                (
                    GoogleCalendarChange(
                        known_id,
                        "rev:cancelled",
                        None,
                        "cancelled",
                        "https://attacker.invalid/changed-link",
                    ),
                    GoogleCalendarChange(
                        _event_id("unknown"),
                        "rev:unknown",
                        None,
                        "cancelled",
                        "https://www.google.com/calendar/event?eid=unknown",
                    ),
                ),
                next_sync_token="sync:2",
            )
        ]
    )
    sync = GoogleCalendarSync(provider, store, "brain:fixture")
    cancellation = sync.prepare(selection)
    captured = _Capture()
    sync.apply(selection, _preview_id(cancellation), captured)

    assert cancellation["record_count"] == 1
    assert len(captured.items) == 1
    assert captured.items[0].source_reference == original.items[0].source_reference == _SOURCE_LINK
    assert "Status: cancelled" in captured.items[0].text


def test_incomplete_page_does_not_create_checkpoint(tmp_path: Path) -> None:
    selection = _selection()
    provider = _Provider([GoogleCalendarPage((_change(selection, "one", "rev:1"),))])
    sync = GoogleCalendarSync(
        provider,
        GoogleCalendarSyncStore(tmp_path / "state"),
        "brain:fixture",
    )

    with pytest.raises(GoogleCalendarError, match="google_calendar_sync_incomplete"):
        sync.prepare(selection)

    assert sync.status(selection)["configured"] is False
    assert sync.status(selection)["pending"] is False


def test_destination_id_separates_durable_state(tmp_path: Path) -> None:
    selection = _selection()
    store = GoogleCalendarSyncStore(tmp_path / "state")
    provider = _Provider(
        [GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token="sync:1")]
    )
    first = GoogleCalendarSync(provider, store, "brain:first")
    preview = first.prepare(selection)
    first.apply(selection, _preview_id(preview), _Capture())

    second = GoogleCalendarSync(_Provider([]), store, "brain:second")

    assert first.status(selection)["configured"] is True
    assert second.status(selection)["configured"] is False


def test_changed_range_is_rejected_for_same_destination_calendar(tmp_path: Path) -> None:
    selection = _selection()
    store = GoogleCalendarSyncStore(tmp_path / "state")
    provider = _Provider(
        [GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token="sync:1")]
    )
    sync = GoogleCalendarSync(provider, store, "brain:fixture")
    preview = sync.prepare(selection)
    sync.apply(selection, _preview_id(preview), _Capture())
    changed = GoogleCalendarSelection(
        connection_id=selection.connection_id,
        calendar_id=selection.calendar_id,
        range_start="2026-09-16T00:00:00Z",
        range_end="2026-09-17T00:00:00Z",
        timezone=selection.timezone,
    )

    with pytest.raises(GoogleCalendarError, match="google_calendar_selection_changed"):
        sync.status(changed)


def test_printable_eight_kib_google_cursor_is_accepted(tmp_path: Path) -> None:
    selection = _selection()
    token = "sync token/" + "x" * 600
    provider = _Provider(
        [GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token=token)]
    )
    sync = GoogleCalendarSync(
        provider,
        GoogleCalendarSyncStore(tmp_path / "state"),
        "brain:fixture",
    )

    preview = sync.prepare(selection)
    sync.apply(selection, _preview_id(preview), _Capture())

    assert sync.status(selection)["configured"] is True


def test_store_rejects_symlink_root(tmp_path: Path) -> None:
    selection = _selection()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    provider = _Provider(
        [GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token="sync:1")]
    )
    sync = GoogleCalendarSync(provider, GoogleCalendarSyncStore(linked), "brain:fixture")

    with pytest.raises(GoogleCalendarError, match="google_calendar_state_invalid"):
        sync.prepare(selection)

    assert provider.calls == []


def _selection() -> GoogleCalendarSelection:
    return GoogleCalendarSelection(
        connection_id=_CONNECTION,
        calendar_id=_NATIVE_CALENDAR,
        range_start="2026-09-15T00:00:00Z",
        range_end="2026-09-16T00:00:00Z",
        timezone="America/Chicago",
    )


def test_retry_after_checkpoint_commit_recovers_lost_cleanup_and_keeps_new_preview(
    tmp_path: Path,
) -> None:
    class InterruptedStore(GoogleCalendarSyncStore):
        interrupted = True

        def delete_pending(self, key: str) -> None:
            if self.interrupted:
                self.interrupted = False
                raise GoogleCalendarError("google_calendar_state_invalid")
            super().delete_pending(key)

    selection = _selection()
    provider = _Provider([
        GoogleCalendarPage((_change(selection, "one", "rev:1"),), next_sync_token="sync:1"),
        GoogleCalendarPage((_change(selection, "one", "rev:2"),), next_sync_token="sync:2"),
    ])
    store = InterruptedStore(tmp_path / "state")
    sync = GoogleCalendarSync(provider, store, "brain:fixture")
    capture = _Capture()
    first = sync.prepare(selection)
    with pytest.raises(GoogleCalendarError, match="google_calendar_state_invalid"):
        sync.apply(selection, _preview_id(first), capture)
    assert len(capture.items) == 1
    recovered = sync.apply(selection, _preview_id(first), capture)
    assert recovered["checkpoint_committed"] is True
    assert sync.status(selection)["pending"] is False
    assert len(capture.items) == 1 and len(provider.calls) == 1
    second = sync.prepare(selection)
    assert sync.apply(selection, _preview_id(first), capture) == recovered
    assert sync.status(selection)["pending_preview_id"] == _preview_id(second)
    sync.apply(selection, _preview_id(second), capture)
    assert len(capture.items) == 2


def _event_id(name: str) -> str:
    return opaque_id(name, "event")


def _change(
    selection: GoogleCalendarSelection,
    name: str,
    revision: str,
    *,
    description: str | None = None,
) -> GoogleCalendarChange:
    event_id = _event_id(name)
    return GoogleCalendarChange(
        event_id=event_id,
        revision_id=revision,
        record=CalendarEventRecord(
            calendar_id=selection.resource_id,
            event_id=event_id,
            revision_id=revision,
            provider="google_calendar",
            title=f"Synthetic {name} event",
            start_time="2026-09-15T14:00:00Z",
            end_time="2026-09-15T14:30:00Z",
            timezone=selection.timezone,
            description=description,
        ),
        source_reference=_SOURCE_LINK,
    )


def _preview_id(receipt: dict[str, object]) -> str:
    value = receipt["preview_id"]
    assert isinstance(value, str)
    return value
