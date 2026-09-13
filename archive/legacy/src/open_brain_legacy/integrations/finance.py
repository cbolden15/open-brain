"""Synthetic, provider-neutral finance linking with no live provider support."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from open_brain_legacy._compat.open_brain.integrations.ports import (
    Capability,
    ProviderSyncResult,
    RedactedText,
    SyncStatus,
)

_ITEM_REF_PATTERN = re.compile(r"finance_item_[A-Za-z0-9_-]{1,120}")
_TRANSACTION_REF_PATTERN = re.compile(r"transaction_[A-Za-z0-9_-]{1,116}")
_CURSOR_REF_PATTERN = re.compile(r"finance_cursor_(?:[0-9]{6}|eof)")
_MONTH_PATTERN = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_MAX_PAGE_SIZE = 1_000
_MAX_AMOUNT_CENTS = 1_000_000_000
_MAX_REPORT_TRANSACTION_COUNT = 1_000_000


class FinanceLinkStatus(StrEnum):
    """Allow-listed outcomes for a finance link attempt."""

    LINKED = "linked"
    DUPLICATE = "duplicate"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FinanceLinkResult:
    """A link outcome that exposes an opaque item reference only on success."""

    status: FinanceLinkStatus
    item_ref: str | None
    detail: RedactedText | None

    def __post_init__(self) -> None:
        linked = self.status is FinanceLinkStatus.LINKED
        if (
            not isinstance(self.status, FinanceLinkStatus)
            or (linked and (not _is_item_ref(self.item_ref) or self.detail is not None))
            or (
                not linked
                and (self.item_ref is not None or not isinstance(self.detail, RedactedText))
            )
        ):
            raise ValueError("invalid finance link result")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status.value}
        if self.item_ref is not None:
            result["item_ref"] = self.item_ref
        if self.detail is not None:
            result["detail"] = self.detail.to_dict()
        return result


class FinanceUnlinkStatus(StrEnum):
    """Allow-listed outcomes for a local unlink and optional provider revoke."""

    LOCALLY_UNLINKED = "locally_unlinked"
    REVOKED_AND_UNLINKED = "revoked_and_unlinked"
    ALREADY_UNLINKED = "already_unlinked"


@dataclass(frozen=True, slots=True)
class FinanceUnlinkResult:
    """A structural unlink outcome with no provider response data."""

    status: FinanceUnlinkStatus

    def __post_init__(self) -> None:
        if not isinstance(self.status, FinanceUnlinkStatus):
            raise ValueError("invalid finance unlink result")


class FinanceProvider(Protocol):
    """The minimal provider seam required by the synthetic finance service."""

    def link_item(self) -> str | None: ...

    def fetch_page(self, *, cursor_ref: str | None) -> FinancePage: ...

    def revoke_item(self, *, item_ref: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SyntheticFinanceTransaction:
    """Bounded synthetic transaction metadata used only by the fake adapter."""

    transaction_ref: str
    month: str
    amount_cents: int

    def __post_init__(self) -> None:
        if (
            _TRANSACTION_REF_PATTERN.fullmatch(self.transaction_ref) is None
            or _MONTH_PATTERN.fullmatch(self.month) is None
            or type(self.amount_cents) is not int
            or not 0 <= self.amount_cents <= _MAX_AMOUNT_CENTS
        ):
            raise ValueError("invalid synthetic finance transaction")


@dataclass(frozen=True, slots=True)
class FinancePage:
    """One bounded fake-provider page with an opaque successor cursor."""

    transactions: tuple[SyntheticFinanceTransaction, ...]
    next_cursor_ref: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.transactions, tuple)
            or len(self.transactions) > _MAX_PAGE_SIZE
            or any(
                not isinstance(transaction, SyntheticFinanceTransaction)
                for transaction in self.transactions
            )
            or not _is_cursor_ref(self.next_cursor_ref)
        ):
            raise ValueError("invalid finance page")


@dataclass(frozen=True, slots=True)
class FinanceMonthlyReport:
    """Bounded aggregate finance metadata without item or transaction references."""

    month: str
    total_cents: int
    transaction_count: int
    truncated: bool
    metadata: RedactedText

    def __post_init__(self) -> None:
        if (
            _MONTH_PATTERN.fullmatch(self.month) is None
            or type(self.total_cents) is not int
            or not 0 <= self.total_cents <= _MAX_AMOUNT_CENTS
            or type(self.transaction_count) is not int
            or not 0 <= self.transaction_count <= _MAX_REPORT_TRANSACTION_COUNT
            or type(self.truncated) is not bool
            or not isinstance(self.metadata, RedactedText)
        ):
            raise ValueError("invalid finance monthly report")

    def to_dict(self) -> dict[str, object]:
        return {
            "month": self.month,
            "total_cents": self.total_cents,
            "transaction_count": self.transaction_count,
            "truncated": self.truncated,
            "metadata": self.metadata.to_dict(),
        }


class FakeFinanceProvider:
    """A deterministic fake provider; it contains no live-provider code or imports."""

    def __init__(
        self,
        *,
        timeout_on_link: bool = False,
        transactions: tuple[SyntheticFinanceTransaction, ...] = (),
        page_size: int = 100,
    ) -> None:
        if (
            type(timeout_on_link) is not bool
            or not isinstance(transactions, tuple)
            or any(
                not isinstance(transaction, SyntheticFinanceTransaction)
                for transaction in transactions
            )
            or type(page_size) is not int
            or not 1 <= page_size <= _MAX_PAGE_SIZE
        ):
            raise ValueError("invalid fake finance provider configuration")
        self._timeout_on_link = timeout_on_link
        self._transactions = transactions
        self._page_size = page_size
        self._next_item = 1
        self._revoked_item_refs: list[str] = []
        self._requested_cursor_refs: list[str | None] = []

    @property
    def revoked_item_refs(self) -> tuple[str, ...]:
        return tuple(self._revoked_item_refs)

    @property
    def requested_cursor_refs(self) -> tuple[str | None, ...]:
        return tuple(self._requested_cursor_refs)

    def link_item(self) -> str | None:
        if self._timeout_on_link:
            return None
        item_ref = f"finance_item_{self._next_item:06d}"
        self._next_item += 1
        return item_ref

    def fetch_page(self, *, cursor_ref: str | None) -> FinancePage:
        if cursor_ref is not None and not _is_cursor_ref(cursor_ref):
            raise ValueError("invalid finance cursor reference")
        self._requested_cursor_refs.append(cursor_ref)
        if cursor_ref is None:
            start = 0
        elif cursor_ref == "finance_cursor_eof":
            start = len(self._transactions)
        else:
            start = int(cursor_ref.removeprefix("finance_cursor_"))
        end = min(start + self._page_size, len(self._transactions))
        next_cursor_ref = (
            "finance_cursor_eof"
            if end >= len(self._transactions)
            else f"finance_cursor_{end:06d}"
        )
        return FinancePage(
            transactions=self._transactions[start:end],
            next_cursor_ref=next_cursor_ref,
        )

    def revoke_item(self, *, item_ref: str) -> None:
        if not _is_item_ref(item_ref):
            raise ValueError("invalid finance item reference")
        self._revoked_item_refs.append(item_ref)


class InMemoryFinanceStore:
    """Synthetic local linkage state; it never holds provider credentials or payloads."""

    def __init__(self, *, reject_next_page: bool = False) -> None:
        if type(reject_next_page) is not bool:
            raise ValueError("invalid finance store configuration")
        self._linked_item_refs: set[str] = set()
        self._transactions: dict[str, dict[str, SyntheticFinanceTransaction]] = {}
        self._cursor_refs: dict[str, str] = {}
        self._reject_next_page = reject_next_page

    def has_link(self) -> bool:
        return bool(self._linked_item_refs)

    def contains_link(self, *, item_ref: str) -> bool:
        if not _is_item_ref(item_ref):
            raise ValueError("invalid finance item reference")
        return item_ref in self._linked_item_refs

    def add_link(self, *, item_ref: str) -> None:
        if not _is_item_ref(item_ref):
            raise ValueError("invalid finance item reference")
        self._linked_item_refs.add(item_ref)

    def remove_link(self, *, item_ref: str) -> bool:
        if not _is_item_ref(item_ref):
            raise ValueError("invalid finance item reference")
        if item_ref not in self._linked_item_refs:
            return False
        self._linked_item_refs.remove(item_ref)
        self._transactions.pop(item_ref, None)
        self._cursor_refs.pop(item_ref, None)
        return True

    def cursor_for(self, *, item_ref: str) -> str | None:
        if not _is_item_ref(item_ref):
            raise ValueError("invalid finance item reference")
        return self._cursor_refs.get(item_ref)

    def accept_page(self, *, item_ref: str, page: FinancePage) -> int | None:
        if not self.contains_link(item_ref=item_ref) or not isinstance(page, FinancePage):
            raise ValueError("invalid finance page write")
        if self._reject_next_page:
            self._reject_next_page = False
            return None

        current = self._transactions.get(item_ref, {})
        accepted = dict(current)
        for transaction in page.transactions:
            accepted[transaction.transaction_ref] = transaction
        created = len(accepted) - len(current)
        self._transactions[item_ref] = accepted
        self._cursor_refs[item_ref] = page.next_cursor_ref
        return created

    def transactions_for(self, *, item_ref: str) -> tuple[SyntheticFinanceTransaction, ...]:
        if not self.contains_link(item_ref=item_ref):
            raise ValueError("unknown finance item reference")
        return tuple(self._transactions.get(item_ref, {}).values())


class FinanceIntegration:
    """Provider-neutral behavior with fake injection only and disabled-by-default linking."""

    def __init__(
        self,
        *,
        provider: FinanceProvider | None = None,
        store: InMemoryFinanceStore | None = None,
    ) -> None:
        self._provider = provider
        self._store = store if store is not None else InMemoryFinanceStore()

    def link(self) -> FinanceLinkResult:
        if self._provider is None:
            return _redacted_link_result(FinanceLinkStatus.UNAVAILABLE, "Finance unavailable.")
        if self._store.has_link():
            return _redacted_link_result(
                FinanceLinkStatus.DUPLICATE,
                "Finance link already exists.",
            )

        item_ref = self._provider.link_item()
        if item_ref is None:
            return _redacted_link_result(FinanceLinkStatus.TIMEOUT, "Finance link timed out.")
        if not _is_item_ref(item_ref):
            return _redacted_link_result(FinanceLinkStatus.UNAVAILABLE, "Finance unavailable.")

        self._store.add_link(item_ref=item_ref)
        return FinanceLinkResult(
            status=FinanceLinkStatus.LINKED,
            item_ref=item_ref,
            detail=None,
        )

    def unlink(self, *, item_ref: str, revoke_provider: bool) -> FinanceUnlinkResult:
        if type(revoke_provider) is not bool:
            raise ValueError("invalid revoke request")
        removed = self._store.remove_link(item_ref=item_ref)
        if not removed:
            return FinanceUnlinkResult(status=FinanceUnlinkStatus.ALREADY_UNLINKED)
        if revoke_provider and self._provider is not None:
            self._provider.revoke_item(item_ref=item_ref)
            return FinanceUnlinkResult(status=FinanceUnlinkStatus.REVOKED_AND_UNLINKED)
        return FinanceUnlinkResult(status=FinanceUnlinkStatus.LOCALLY_UNLINKED)

    def sync(self, *, item_ref: str) -> ProviderSyncResult:
        if not _is_item_ref(item_ref):
            raise ValueError("invalid finance item reference")
        if self._provider is None or not self._store.contains_link(item_ref=item_ref):
            return _sync_result(status=SyncStatus.UNSUPPORTED, created=0)

        page = self._provider.fetch_page(
            cursor_ref=self._store.cursor_for(item_ref=item_ref)
        )
        created = self._store.accept_page(item_ref=item_ref, page=page)
        if created is None:
            return _sync_result(status=SyncStatus.RETRYABLE, created=0)
        return _sync_result(
            status=SyncStatus.COMPLETED,
            created=created,
            next_cursor_ref=page.next_cursor_ref,
        )

    def monthly_report(
        self,
        *,
        item_ref: str,
        month: str,
        max_total_cents: int,
        max_transaction_count: int,
    ) -> FinanceMonthlyReport:
        if (
            _MONTH_PATTERN.fullmatch(month) is None
            or type(max_total_cents) is not int
            or not 0 <= max_total_cents <= _MAX_AMOUNT_CENTS
            or type(max_transaction_count) is not int
            or not 1 <= max_transaction_count <= _MAX_REPORT_TRANSACTION_COUNT
        ):
            raise ValueError("invalid finance monthly report request")
        transactions = tuple(
            transaction
            for transaction in self._store.transactions_for(item_ref=item_ref)
            if transaction.month == month
        )
        total_cents = sum(transaction.amount_cents for transaction in transactions)
        return FinanceMonthlyReport(
            month=month,
            total_cents=min(total_cents, max_total_cents),
            transaction_count=min(len(transactions), max_transaction_count),
            truncated=(
                total_cents > max_total_cents
                or len(transactions) > max_transaction_count
            ),
            metadata=RedactedText.redact("Synthetic monthly finance totals."),
        )


def _is_item_ref(value: object) -> bool:
    return isinstance(value, str) and _ITEM_REF_PATTERN.fullmatch(value) is not None


def _is_cursor_ref(value: object) -> bool:
    return isinstance(value, str) and _CURSOR_REF_PATTERN.fullmatch(value) is not None


def _redacted_link_result(status: FinanceLinkStatus, detail: str) -> FinanceLinkResult:
    return FinanceLinkResult(status=status, item_ref=None, detail=RedactedText.redact(detail))


def _sync_result(
    *,
    status: SyncStatus,
    created: int,
    next_cursor_ref: str | None = None,
) -> ProviderSyncResult:
    return ProviderSyncResult(
        capability=Capability.FINANCE,
        status=status,
        created=created,
        updated=0,
        removed=0,
        next_cursor_ref=next_cursor_ref,
    )
