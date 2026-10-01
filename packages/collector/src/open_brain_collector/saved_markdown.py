"""Collector bridge for the reusable saved-Markdown selected-root adapter."""

from __future__ import annotations

from open_brain_collector.lifecycle import CollectorRunPage
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.saved_markdown import SavedMarkdownRootAdapter, SavedMarkdownScan
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = ["SavedMarkdownCollectorRuntime"]


class SavedMarkdownCollectorRuntime:
    """Expose one caller-bound saved-Markdown scan to collector custody.

    Refusals stay local in the returned scan. They are never turned into an
    intake, and an incomplete scan carries no disappearance semantics.
    """

    def __init__(self, adapter: SavedMarkdownRootAdapter) -> None:
        if not isinstance(adapter, SavedMarkdownRootAdapter):
            raise ConnectorContractError("invalid saved markdown runtime")
        self._adapter = adapter
        self.last_scan: SavedMarkdownScan | None = None

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        if selection != self._adapter.selection or cursor is not None:
            raise ConnectorContractError("invalid saved markdown page")
        scan = self._adapter.dry_run()
        self.last_scan = scan
        return CollectorRunPage(
            selection=selection,
            intakes=tuple(
                candidate.intake for candidate in scan.candidates if candidate.intake is not None
            ),
        )
