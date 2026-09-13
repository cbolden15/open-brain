from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from open_brain_legacy._compat.open_brain.integrations import (
    Capability,
    IntegrationConfig,
    ProviderSyncRequest,
    SyncStatus,
)
from open_brain_legacy.integrations.mail_calendar import (
    AgendaItem,
    CalendarEvent,
    DurableCursorStore,
    MailCalendarIntegration,
    MailPage,
)


class RacingCursorStore(DurableCursorStore):
    def advance(
        self,
        *,
        resource_ref: str,
        expected_cursor_ref: str | None,
        cursor_ref: str | None,
    ) -> bool:
        super().advance(
            resource_ref=resource_ref,
            expected_cursor_ref=expected_cursor_ref,
            cursor_ref="cursor_winner",
        )
        return super().advance(
            resource_ref=resource_ref,
            expected_cursor_ref=expected_cursor_ref,
            cursor_ref=cursor_ref,
        )


def test_mail_sync_dry_run_redacts_content_and_advances_a_durable_cursor(
    tmp_path: Path,
) -> None:
    cursor_store = DurableCursorStore(path=tmp_path / "mail-cursor.json")
    disabled = MailCalendarIntegration(
        config=IntegrationConfig(),
        cursor_store=cursor_store,
    )
    integration = MailCalendarIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MAIL_CALENDAR})),
        cursor_store=cursor_store,
    )
    page = MailPage(
        item_refs=("mail_1", "mail_2"),
        next_cursor_ref="cursor_2",
    )
    dry_run = ProviderSyncRequest(
        capability=Capability.MAIL_CALENDAR,
        resource_ref="mailbox_fixture",
        cursor_ref=None,
        dry_run=True,
    )

    unavailable = disabled.sync_mail(request=dry_run, page=page)
    preview = integration.sync_mail(request=dry_run, page=page)

    assert unavailable.status is SyncStatus.UNSUPPORTED
    assert preview.status is SyncStatus.DRY_RUN
    assert preview.created == 2
    assert cursor_store.read(resource_ref="mailbox_fixture") is None

    applied = integration.sync_mail(
        request=ProviderSyncRequest(
            capability=Capability.MAIL_CALENDAR,
            resource_ref="mailbox_fixture",
            cursor_ref=None,
            dry_run=False,
        ),
        page=page,
    )

    assert applied.status is SyncStatus.COMPLETED
    assert applied.next_cursor_ref == "cursor_2"
    assert cursor_store.read(resource_ref="mailbox_fixture") == "cursor_2"
    assert DurableCursorStore(path=tmp_path / "mail-cursor.json").read(
        resource_ref="mailbox_fixture"
    ) == "cursor_2"


def test_agenda_is_bounded_and_never_returns_private_descriptions(tmp_path: Path) -> None:
    integration = MailCalendarIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MAIL_CALENDAR})),
        cursor_store=DurableCursorStore(path=tmp_path / "mail-cursor.json"),
    )
    starts_at = datetime(2026, 8, 14, 9, tzinfo=UTC)

    agenda = integration.agenda(
        events=tuple(
            CalendarEvent(
                event_id=f"event_{index}",
                starts_at=starts_at + timedelta(hours=index),
                ends_at=starts_at + timedelta(hours=index + 1),
                title="api key: synthetic-secret" if index == 0 else f"Synthetic event {index}",
                description="private synthetic description",
            )
            for index in range(10)
        ),
        limit=2,
    )

    assert len(agenda) == 2
    assert all(isinstance(item, AgendaItem) for item in agenda)
    assert all(not hasattr(item, "description") for item in agenda)
    assert agenda[0].title.text == "[redacted]"
    assert "private synthetic description" not in str(agenda)
    assert "synthetic-secret" not in str(agenda)


def test_mail_sync_compare_and_swap_rejects_a_stale_concurrent_advance(
    tmp_path: Path,
) -> None:
    store = RacingCursorStore(path=tmp_path / "mail-cursor.json")
    integration = MailCalendarIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.MAIL_CALENDAR})),
        cursor_store=store,
    )

    result = integration.sync_mail(
        request=ProviderSyncRequest(
            capability=Capability.MAIL_CALENDAR,
            resource_ref="mailbox_fixture",
            cursor_ref=None,
            dry_run=False,
        ),
        page=MailPage(item_refs=("mail_1",), next_cursor_ref="cursor_loser"),
    )

    assert result.status is SyncStatus.RETRYABLE
    assert result.created == 0
    assert result.next_cursor_ref == "cursor_winner"
    assert store.read(resource_ref="mailbox_fixture") == "cursor_winner"
