from __future__ import annotations

from open_brain_legacy._compat.open_brain.integrations.ports import SyncStatus
from open_brain_legacy.integrations.finance import (
    FakeFinanceProvider,
    FinanceIntegration,
    FinanceLinkStatus,
    FinanceUnlinkStatus,
    InMemoryFinanceStore,
    SyntheticFinanceTransaction,
)


def test_fake_finance_link_and_unlink_contract_stays_opaque_and_idempotent() -> None:
    provider = FakeFinanceProvider()
    finance = FinanceIntegration(provider=provider, store=InMemoryFinanceStore())

    unavailable = FinanceIntegration().link()
    assert unavailable.status is FinanceLinkStatus.UNAVAILABLE
    assert unavailable.item_ref is None
    assert unavailable.detail is not None
    assert unavailable.detail.receipt.verifies_text(unavailable.detail.text)

    linked = finance.link()
    assert linked.status is FinanceLinkStatus.LINKED
    assert linked.item_ref is not None
    assert linked.item_ref.startswith("finance_item_")
    assert linked.detail is None
    assert set(linked.to_dict()) == {"status", "item_ref"}

    duplicate = finance.link()
    assert duplicate.status is FinanceLinkStatus.DUPLICATE
    assert duplicate.item_ref is None
    assert duplicate.detail is not None
    assert duplicate.detail.receipt.verifies_text(duplicate.detail.text)

    timed_out = FinanceIntegration(
        provider=FakeFinanceProvider(timeout_on_link=True), store=InMemoryFinanceStore()
    ).link()
    assert timed_out.status is FinanceLinkStatus.TIMEOUT
    assert timed_out.item_ref is None
    assert timed_out.detail is not None
    assert timed_out.detail.receipt.verifies_text(timed_out.detail.text)

    local_only = finance.unlink(item_ref=linked.item_ref, revoke_provider=False)
    assert local_only.status is FinanceUnlinkStatus.LOCALLY_UNLINKED
    assert len(provider.revoked_item_refs) == 0

    repeat = finance.unlink(item_ref=linked.item_ref, revoke_provider=True)
    assert repeat.status is FinanceUnlinkStatus.ALREADY_UNLINKED
    assert len(provider.revoked_item_refs) == 0

    revoke_provider = FakeFinanceProvider()
    revoking_finance = FinanceIntegration(
        provider=revoke_provider,
        store=InMemoryFinanceStore(),
    )
    revoke_link = revoking_finance.link()
    assert revoke_link.item_ref is not None
    revoked = revoking_finance.unlink(item_ref=revoke_link.item_ref, revoke_provider=True)
    assert revoked.status is FinanceUnlinkStatus.REVOKED_AND_UNLINKED
    assert revoke_provider.revoked_item_refs == (revoke_link.item_ref,)
    assert (
        revoking_finance.unlink(item_ref=revoke_link.item_ref, revoke_provider=True).status
        is FinanceUnlinkStatus.ALREADY_UNLINKED
    )


def test_finance_cursor_waits_for_durable_write_and_report_is_bounded_metadata() -> None:
    provider = FakeFinanceProvider(
        transactions=(
            SyntheticFinanceTransaction(
                transaction_ref="transaction_001",
                month="2026-08",
                amount_cents=1_200,
            ),
            SyntheticFinanceTransaction(
                transaction_ref="transaction_002",
                month="2026-08",
                amount_cents=800,
            ),
        ),
        page_size=1,
    )
    store = InMemoryFinanceStore(reject_next_page=True)
    finance = FinanceIntegration(provider=provider, store=store)
    linked = finance.link()
    assert linked.item_ref is not None

    rejected = finance.sync(item_ref=linked.item_ref)
    assert rejected.status is SyncStatus.RETRYABLE
    assert rejected.created == 0
    assert rejected.next_cursor_ref is None
    assert store.cursor_for(item_ref=linked.item_ref) is None

    first_page = finance.sync(item_ref=linked.item_ref)
    assert first_page.status is SyncStatus.COMPLETED
    assert first_page.created == 1
    assert first_page.next_cursor_ref is not None
    assert store.cursor_for(item_ref=linked.item_ref) == first_page.next_cursor_ref
    assert provider.requested_cursor_refs[:2] == (None, None)

    final_page = finance.sync(item_ref=linked.item_ref)
    assert final_page.status is SyncStatus.COMPLETED
    assert final_page.created == 1
    assert final_page.next_cursor_ref == "finance_cursor_eof"
    assert store.cursor_for(item_ref=linked.item_ref) == "finance_cursor_eof"

    report = finance.monthly_report(
        item_ref=linked.item_ref,
        month="2026-08",
        max_total_cents=1_500,
        max_transaction_count=1,
    )
    assert report.total_cents == 1_500
    assert report.transaction_count == 1
    assert report.truncated is True
    assert report.metadata.receipt.verifies_text(report.metadata.text)
    serialized = repr(report.to_dict())
    assert "transaction_001" not in serialized
    assert "transaction_002" not in serialized
    assert "finance_item_" not in serialized
