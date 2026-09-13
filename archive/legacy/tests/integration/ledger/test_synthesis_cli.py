from __future__ import annotations

from pathlib import Path
from typing import cast

from open_brain_engine.core.models import PrivacyDecision

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.ledger import synthesis
from open_brain_legacy.ledger.render import RenderResult, SynthesisRenderer
from open_brain_legacy.ledger.synthesis import SynthesisError, SynthesisOutcome, SynthesisRequest
from open_brain_legacy.ledger.synthesis_store import DurableSynthesisRecord, SqliteSynthesisStore
from packages.legacy.tests.unit.ledger.test_synthesis import _Provider, _request, _service


class _RecordingSynthesisService:
    def __init__(self) -> None:
        self.calls = 0

    def apply(
        self, *, request: SynthesisRequest, privacy: PrivacyDecision
    ) -> SynthesisOutcome:
        self.calls += 1
        return SynthesisOutcome(prepared=None, error=SynthesisError.SOURCE_GATE, attempts=0)


class _RecordingSynthesisStore:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, request_id: str) -> DurableSynthesisRecord | None:
        self.calls += 1
        return None


class _RecordingSynthesisRenderer:
    def __init__(self) -> None:
        self.calls = 0

    def render(self, record: DurableSynthesisRecord) -> RenderResult:
        self.calls += 1
        raise AssertionError("dry run rendered synthesis")


def test_synthesis_dry_run_invokes_no_provider_persistence_or_rendering() -> None:
    service = _RecordingSynthesisService()
    store = _RecordingSynthesisStore()
    renderer = _RecordingSynthesisRenderer()

    result = synthesis(
        service=service,
        store=store,
        renderer=renderer,
        request=cast(SynthesisRequest, object()),
        privacy=cast(PrivacyDecision, object()),
        dry_run=True,
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "command": "ledger.synthesis",
        "dry_run": True,
        "status": "dry_run",
    }
    assert (service.calls, store.calls, renderer.calls) == (0, 0, 0)


def test_synthesis_is_citation_bound_rendered_and_replay_safe(tmp_path: Path) -> None:
    request, privacy = _request()
    probes = (lambda: False,)
    provider = _Provider(probes)
    store = SqliteSynthesisStore(root=tmp_path / "private")
    service = _service(provider=provider, lock_probes=probes, store=store)
    render_root = tmp_path / "rendered"
    renderer = SynthesisRenderer(root=render_root)

    first = synthesis(
        service=service,
        store=store,
        renderer=renderer,
        request=request,
        privacy=privacy,
        dry_run=False,
    )
    second = synthesis(
        service=service,
        store=store,
        renderer=renderer,
        request=request,
        privacy=privacy,
        dry_run=False,
    )

    assert first.exit_code is ExitCode.SUCCESS
    assert first.envelope == {
        "attempts": 1,
        "command": "ledger.synthesis",
        "request_id": request.request_id,
        "status": "synthesized",
    }
    assert second.envelope == first.envelope
    assert store.record_count() == 1
    assert store.page_count() == 1
    assert len(tuple(render_root.rglob("*.md"))) == 1
    record = store.get(request.request_id)
    assert record is not None
    assert record.link_back_source_ids == ("cite-one", "cite-three", "cite-two")
    assert all(source_id in record.document.body for source_id in record.link_back_source_ids)
    assert "transcript-canary" not in record.document.body
    assert "Synthetic cross-source claim" not in first.to_json()
