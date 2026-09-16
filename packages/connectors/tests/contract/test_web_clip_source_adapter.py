from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_brain_connectors.runtime.connectors import ConnectorContractError, ConnectorOutcome
from open_brain_connectors.runtime.web_clip import (
    SafariWebClipDeliveryHarness,
    WebClipCheckpoint,
    WebClipCheckpointStore,
    WebClipPageStatus,
    WebClipRecord,
    WebClipSourceAdapter,
)

from .test_agent_session_source_adapter import _capture_sink
from .test_source_intake import _privacy

_BROWSER_ID = "browser:safari-fixture"
_CLIP_ID = "clip:0123456789abcdef0123456789abcdef"
_SAFARI_BROWSER_ID = "browser:safari"


def test_web_clip_adapter_builds_metadata_preview_without_body() -> None:
    adapter = WebClipSourceAdapter()
    selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_BROWSER_ID,
        clip_type="selected_passage",
    )

    page = adapter.page_from_clips(
        selection,
        (
            _clip(
                text="Synthetic selected passage body must not appear in preview.",
                title="Fixture Page",
            ),
        ),
        privacy=_privacy(),
        selected_clip_ids=(_CLIP_ID,),
        next_cursor="cursor:clip2",
    )

    assert page.status is WebClipPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert page.preview.next_cursor == "cursor:clip2"
    assert [record.content_type for record in page.preview.records] == [
        "selected_passage"
    ]
    assert page.preview.records[0].title == "Fixture Page"
    assert "must not appear" not in repr(page.preview.to_dict())


def test_safari_delivery_harness_builds_current_page_payload_without_history_or_cookies() -> None:
    adapter = WebClipSourceAdapter()
    selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_SAFARI_BROWSER_ID,
        clip_type="current_page",
    )

    delivery = SafariWebClipDeliveryHarness().deliver(
        selection,
        {
            "browser": "safari",
            "clip_type": "current_page",
            "cookie_capture": False,
            "delivery_kind": "explicit_browser_clip",
            "history_scan": False,
            "page_text": "Synthetic current page body.",
            "page_url": "https://example.invalid/current",
            "permission_granted": True,
            "selection_confirmed": True,
            "text_secret_scan": "clean",
            "title": "Current Page",
        },
    )

    assert delivery.status is WebClipPageStatus.READY
    assert delivery.clip is not None
    assert delivery.clip["browser_id"] == _SAFARI_BROWSER_ID
    assert delivery.clip["clip_type"] == "current_page"
    assert delivery.clip["cookie_capture"] is False
    assert delivery.clip["history_scan"] is False
    record = adapter.record_from_clip(delivery.clip)
    assert record.text == "Synthetic current page body."
    assert record.clip_id.startswith("clip:")


def test_safari_delivery_harness_builds_selected_passage_payload_from_activation_id() -> None:
    adapter = WebClipSourceAdapter()
    selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_SAFARI_BROWSER_ID,
        clip_type="selected_passage",
    )

    first = SafariWebClipDeliveryHarness().deliver(
        selection,
        {
            "activation_id": "activation:fixture-1",
            "browser": "safari",
            "clip_type": "selected_passage",
            "cookie_capture": False,
            "delivery_kind": "explicit_browser_clip",
            "history_scan": False,
            "page_url": "https://example.invalid/current",
            "permission_granted": True,
            "selected_text": "Synthetic selected text.",
            "selection_confirmed": True,
            "text_secret_scan": "clean",
            "title": "Selected Passage",
        },
    )
    second = SafariWebClipDeliveryHarness().deliver(
        selection,
        {
            "activation_id": "activation:fixture-1",
            "browser": "safari",
            "clip_type": "selected_passage",
            "cookie_capture": False,
            "delivery_kind": "explicit_browser_clip",
            "history_scan": False,
            "page_url": "https://example.invalid/current",
            "permission_granted": True,
            "selected_text": "Synthetic selected text updated.",
            "selection_confirmed": True,
            "text_secret_scan": "clean",
            "title": "Selected Passage",
        },
    )

    assert first.status is WebClipPageStatus.READY
    assert second.status is WebClipPageStatus.READY
    assert first.clip is not None
    assert second.clip is not None
    assert first.clip["clip_id"] == second.clip["clip_id"]
    assert first.clip["revision_id"] != second.clip["revision_id"]
    assert adapter.record_from_clip(first.clip).text == "Synthetic selected text."


def test_safari_delivery_harness_denies_unselected_or_unsupported_delivery() -> None:
    selection = WebClipSourceAdapter().clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_SAFARI_BROWSER_ID,
        clip_type="selected_passage",
    )
    harness = SafariWebClipDeliveryHarness()

    denied = harness.deliver(
        selection,
        {
            "browser": "safari",
            "clip_type": "selected_passage",
            "cookie_capture": False,
            "delivery_kind": "explicit_browser_clip",
            "history_scan": False,
            "page_url": "https://example.invalid/current",
            "permission_granted": False,
            "selected_text": "Synthetic selected text.",
            "selection_confirmed": True,
            "text_secret_scan": "clean",
            "title": "Selected Passage",
        },
    )
    history = harness.deliver(
        selection,
        {
            "browser": "safari",
            "clip_type": "selected_passage",
            "cookie_capture": False,
            "delivery_kind": "explicit_browser_clip",
            "history_scan": True,
            "page_url": "https://example.invalid/current",
            "permission_granted": True,
            "selected_text": "Synthetic selected text.",
            "selection_confirmed": True,
            "text_secret_scan": "clean",
            "title": "Selected Passage",
        },
    )
    unsupported_browser = harness.deliver(
        WebClipSourceAdapter().clip_selection(
            connection_id="account:web-clip-fixture",
            browser_id=_BROWSER_ID,
            clip_type="selected_passage",
        ),
        {
            "browser": "safari",
            "clip_type": "selected_passage",
            "cookie_capture": False,
            "delivery_kind": "explicit_browser_clip",
            "history_scan": False,
            "page_url": "https://example.invalid/current",
            "permission_granted": True,
            "selected_text": "Synthetic selected text.",
            "selection_confirmed": True,
            "text_secret_scan": "clean",
            "title": "Selected Passage",
        },
    )

    assert denied.status is WebClipPageStatus.NOT_ALLOWED
    assert denied.clip is None
    assert history.status is WebClipPageStatus.UNSUPPORTED_FORMAT
    assert history.clip is None
    assert unsupported_browser.status is WebClipPageStatus.UNSUPPORTED_BROWSER
    assert unsupported_browser.clip is None


def test_web_clip_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = WebClipSourceAdapter()
    selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_BROWSER_ID,
        clip_type="current_page",
    )
    original = adapter.record_from_clip(
        _clip(
            clip_type="current_page",
            text="Synthetic original web page text.",
            revision_id="rev-1",
        )
    )
    original_page = adapter.preview(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        WebClipCheckpoint.initial(selection),
        adapter.page_from_clips(
            selection,
            (
                _clip(
                    clip_type="current_page",
                    text="Synthetic original web page text.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_clip_ids=(_CLIP_ID,),
        ),
        (original_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (
        original_intake.key.revision_identity(),
    )

    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_clips(
            selection,
            (
                _clip(
                    clip_type="current_page",
                    text="Synthetic original web page text.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_clip_ids=(_CLIP_ID,),
        ),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.record_from_clip(
        _clip(
            clip_type="current_page",
            text="Synthetic changed web page text.",
            revision_id="rev-2",
        )
    )
    changed_page = adapter.preview(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    changed_checkpoint, changed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_clips(
            selection,
            (
                _clip(
                    clip_type="current_page",
                    text="Synthetic changed web page text.",
                    revision_id="rev-2",
                ),
            ),
            privacy=_privacy(),
            selected_clip_ids=(_CLIP_ID,),
        ),
        (changed_intake,),
        sink,
    )

    assert original_page.records[0].delivery_id == changed_page.records[0].delivery_id
    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.created_count == 1
    assert changed_checkpoint.committed_delivery_ids == checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_intake.key.revision_identity(),
    )


def test_web_clip_checkpoint_store_is_metadata_only_and_atomic(tmp_path: Path) -> None:
    adapter = WebClipSourceAdapter()
    selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_BROWSER_ID,
        clip_type="selected_passage",
    )
    record = adapter.record_from_clip(_clip(text="Synthetic checkpoint body excluded."))
    page = adapter.preview(selection, (record,), privacy=_privacy(), next_cursor="cursor:clip2")
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = WebClipCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )

    path = WebClipCheckpointStore(tmp_path / "checkpoints").save(checkpoint)

    assert WebClipCheckpointStore(tmp_path / "checkpoints").load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic checkpoint body" not in path.read_text(encoding="utf-8")


def test_web_clip_adapter_requires_selected_clip_and_omits_others() -> None:
    adapter = WebClipSourceAdapter()
    selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_BROWSER_ID,
        clip_type="selected_passage",
    )

    page = adapter.page_from_clips(
        selection,
        (
            _clip(
                clip_id="clip:fedcba9876543210fedcba9876543210",
                text="access_token = synthetic-secret",
            ),
            _clip(title="Selected Passage"),
        ),
        privacy=_privacy(),
        selected_clip_ids=(_CLIP_ID,),
    )

    assert page.preview is not None
    assert len(page.preview.records) == 1
    assert page.preview.records[0].title == "Selected Passage"

    with pytest.raises(ConnectorContractError, match="invalid web clip records"):
        adapter.page_from_clips(
            selection,
            (_clip(clip_type="current_page"),),
            privacy=_privacy(),
            selected_clip_ids=(_CLIP_ID,),
        )
    with pytest.raises(ConnectorContractError, match="invalid web clip records"):
        adapter.page_from_clips(
            selection,
            (_clip(),),
            privacy=_privacy(),
            selected_clip_ids=(),
        )


def test_web_clip_adapter_rejects_secret_history_cookie_and_bad_source() -> None:
    adapter = WebClipSourceAdapter()

    with pytest.raises(ConnectorContractError, match="invalid web clip record"):
        adapter.record_from_clip(_clip(source_kind="browser_history"))
    with pytest.raises(ConnectorContractError, match="invalid web clip record"):
        adapter.record_from_clip(_clip(history_scan=True))
    with pytest.raises(ConnectorContractError, match="invalid web clip record"):
        adapter.record_from_clip(_clip(cookie_capture=True))
    with pytest.raises(ConnectorContractError, match="invalid web clip record"):
        adapter.record_from_clip(_clip(title="access_token = synthetic-secret"))
    with pytest.raises(ConnectorContractError, match="invalid web clip record"):
        adapter.record_from_clip(
            _clip(page_url="https://example.invalid/page?access_token=synthetic-secret")
        )
    with pytest.raises(ConnectorContractError, match="invalid web clip record"):
        adapter.record_from_clip(_clip(text_secret_scan="finding"))
    with pytest.raises(ConnectorContractError, match="invalid web clip record"):
        WebClipRecord(
            clip_id=_CLIP_ID,
            revision_id="rev-1",
            clip_type="selected_passage",
            browser_id=_BROWSER_ID,
            page_url="https://example.invalid/page",
            title="Fixture Page",
            text="client_secret = synthetic-secret",
        )


def test_web_clip_adapter_models_denied_browser_and_format_statuses() -> None:
    adapter = WebClipSourceAdapter()

    assert adapter.not_allowed_page().status is WebClipPageStatus.NOT_ALLOWED
    assert adapter.unsupported_browser_page().status is WebClipPageStatus.UNSUPPORTED_BROWSER
    assert adapter.unsupported_format_page().status is WebClipPageStatus.UNSUPPORTED_FORMAT


def test_web_clip_checkpoint_store_rejects_mismatched_embedded_selection(
    tmp_path: Path,
) -> None:
    adapter = WebClipSourceAdapter()
    selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_BROWSER_ID,
        clip_type="selected_passage",
    )
    other_selection = adapter.clip_selection(
        connection_id="account:web-clip-fixture",
        browser_id=_BROWSER_ID,
        clip_type="current_page",
    )
    store = WebClipCheckpointStore(tmp_path / "checkpoints")
    path = store.save(WebClipCheckpoint.initial(selection))
    path.write_text(
        json.dumps(WebClipCheckpoint.initial(other_selection).to_dict()),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorContractError, match="invalid web clip checkpoint store"):
        store.load(selection)


def _clip(
    *,
    clip_id: str = _CLIP_ID,
    revision_id: str = "rev-1",
    clip_type: str = "selected_passage",
    browser_id: str = _BROWSER_ID,
    page_url: str = "https://example.invalid/page",
    title: str = "Fixture Page",
    text: str = "Synthetic selected web text.",
    source_kind: object = "web_clip",
    text_secret_scan: object = "clean",
    history_scan: object = False,
    cookie_capture: object = False,
) -> dict[str, object]:
    return {
        "browser_id": browser_id,
        "clip_id": clip_id,
        "clip_type": clip_type,
        "cookie_capture": cookie_capture,
        "history_scan": history_scan,
        "page_url": page_url,
        "revision_id": revision_id,
        "source_kind": source_kind,
        "text": text,
        "text_secret_scan": text_secret_scan,
        "title": title,
    }
