from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    ManagedGraphSnapshot,
    ManagedGraphSource,
    ManagedProvider,
    ManagedSuggestion,
)
from open_brain_engine.storage.filesystem import capture_root_identity

from open_brain.services.graph_projection_store import (
    PROJECTION_PATH,
    GraphProjectionStore,
    StructuralGraphLink,
    StructuralGraphReceipt,
    canvas_result,
    projection_result,
)
from open_brain.services.graphify_projection import (
    GRAPHIFY_PARSER_PROFILE,
    GRAPHIFY_PATCH_ID,
    GRAPHIFY_PATCH_SHA256,
    GRAPHIFY_PROTOCOL,
    GRAPHIFY_UPSTREAM_COMMIT,
    GRAPHIFY_VERSION,
    GraphifyAdapter,
    GraphifyDiagnostic,
    GraphifyExtraction,
    GraphifyFailure,
    GraphifyLink,
    SubprocessGraphifyTransport,
    discover_graphify_executable,
)

FIRST = "page_00000000-0000-4000-8000-000000000101"
SECOND = "page_00000000-0000-4000-8000-000000000102"


def _snapshot() -> ManagedGraphSnapshot:
    return ManagedGraphSnapshot(
        workspace_id="workspace_00000000-0000-4000-8000-000000000100",
        observation_generation=1,
        policy_generation=2,
        snapshot_sha256="a" * 64,
        sources=(
            ManagedGraphSource(
                note_id=FIRST,
                revision_id="revision_00000000-0000-4000-8000-000000000101",
                relative_path="notes/first.md",
                body="# First\n\n[[second]]\n",
                body_sha256="b" * 64,
                privacy_sha256="c" * 64,
            ),
            ManagedGraphSource(
                note_id=SECOND,
                revision_id="revision_00000000-0000-4000-8000-000000000102",
                relative_path="notes/second.md",
                body="# Second\n",
                body_sha256="d" * 64,
                privacy_sha256="e" * 64,
            ),
        ),
    )


def _capabilities() -> dict[str, object]:
    return {
        "component": {
            "graphify_version": GRAPHIFY_VERSION,
            "parser_profile": GRAPHIFY_PARSER_PROFILE,
            "patch_id": GRAPHIFY_PATCH_ID,
            "patch_sha256": GRAPHIFY_PATCH_SHA256,
            "upstream_commit": GRAPHIFY_UPSTREAM_COMMIT,
        },
        "max_input_bytes": 16 * 1024,
        "max_output_bytes": 16 * 1024,
        "operations": ["extract_markdown"],
        "protocol": GRAPHIFY_PROTOCOL,
    }


@dataclass
class FakeTransport:
    capabilities: dict[str, object]
    result: dict[str, object]

    def __post_init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes]] = []

    def __call__(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        request: bytes,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        cancelled: Callable[[], bool],
    ) -> bytes:
        assert executable == Path("/opt/open-brain/libexec/open-brain-graphify")
        assert timeout_seconds == 60
        assert max_output_bytes == 16 * 1024
        assert cancelled() is False
        self.calls.append((arguments, request))
        value = self.capabilities if arguments == ("--capabilities",) else self.result
        return portable_canonical_json_bytes(value)


def test_adapter_binds_pinned_component_and_selected_snapshot() -> None:
    result: dict[str, object] = {
        "protocol": GRAPHIFY_PROTOCOL,
        "status": "ok",
        "pages": [FIRST, SECOND],
        "links": [
            {"source": FIRST, "target": SECOND, "kind": "explicit_reference"}
        ],
        "diagnostics": [{"source": FIRST, "code": "unresolved_reference"}],
    }
    transport = FakeTransport(_capabilities(), result)
    adapter = GraphifyAdapter(
        Path("/opt/open-brain/libexec/open-brain-graphify"), transport=transport
    )

    extraction = adapter.extract(_snapshot())

    assert adapter.identity.startswith("graphify:")
    assert extraction == GraphifyExtraction(
        snapshot_sha256="a" * 64,
        status="ok",
        links=(GraphifyLink(FIRST, SECOND),),
        diagnostics=(GraphifyDiagnostic("unresolved_reference", FIRST),),
    )
    assert [call[0] for call in transport.calls] == [
        ("--capabilities",),
        ("--extract-markdown",),
    ]
    request = transport.calls[1][1]
    assert request == portable_canonical_json_bytes(
        {
            "notes": [
                {"body": source.body, "id": source.note_id, "path": source.relative_path}
                for source in _snapshot().sources
            ],
            "operation": "extract_markdown",
            "protocol": GRAPHIFY_PROTOCOL,
        }
    )
    assert b"revision_" not in request
    assert b"privacy" not in request


def test_adapter_reports_ambiguous_snapshot_without_guessing_links() -> None:
    transport = FakeTransport(
        _capabilities(),
        {
            "protocol": GRAPHIFY_PROTOCOL,
            "status": "blocked",
            "pages": [],
            "links": [],
            "diagnostics": [{"code": "ambiguous_snapshot"}],
        },
    )

    extraction = GraphifyAdapter(
        Path("/opt/open-brain/libexec/open-brain-graphify"), transport=transport
    ).extract(_snapshot())

    assert extraction.status == "blocked"
    assert extraction.links == ()
    assert extraction.diagnostics == (GraphifyDiagnostic("ambiguous_snapshot"),)


def test_graphify_link_requires_exact_permanent_page_ids() -> None:
    with pytest.raises(ValueError, match="invalid Graphify link"):
        GraphifyLink("page_first", SECOND)


def test_adapter_rejects_component_drift_before_sending_note_content() -> None:
    capabilities = _capabilities()
    capabilities["component"] = {**capabilities["component"], "graphify_version": "0.9.58"}  # type: ignore[dict-item]
    transport = FakeTransport(capabilities, {})

    with pytest.raises(GraphifyFailure, match="component_mismatch"):
        GraphifyAdapter(
            Path("/opt/open-brain/libexec/open-brain-graphify"), transport=transport
        ).extract(_snapshot())

    assert transport.calls == [(('--capabilities',), b"")]


@pytest.mark.parametrize(
    "result",
    (
        {
            "protocol": GRAPHIFY_PROTOCOL,
            "status": "ok",
            "pages": [FIRST, SECOND],
            "links": [
                {
                    "source": FIRST,
                    "target": "page_00000000-0000-4000-8000-000000000199",
                    "kind": "explicit_reference",
                }
            ],
            "diagnostics": [],
        },
        {
            "protocol": GRAPHIFY_PROTOCOL,
            "status": "blocked",
            "pages": [],
            "links": [{"source": FIRST, "target": SECOND, "kind": "explicit_reference"}],
            "diagnostics": [{"code": "ambiguous_snapshot"}],
        },
    ),
)
def test_adapter_rejects_unselected_or_guessed_links(result: dict[str, object]) -> None:
    adapter = GraphifyAdapter(
        Path("/opt/open-brain/libexec/open-brain-graphify"),
        transport=FakeTransport(_capabilities(), result),
    )

    with pytest.raises(GraphifyFailure, match="output_invalid"):
        adapter.extract(_snapshot())


def test_subprocess_transport_actively_cancels_normal_user_worker() -> None:
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    started = time.monotonic()
    with pytest.raises(GraphifyFailure, match="cancelled"):
        SubprocessGraphifyTransport()(
            Path(sys.executable).resolve(),
            ("-c", "import time; time.sleep(5)"),
            b"",
            timeout_seconds=5,
            max_output_bytes=1024,
            cancelled=cancelled,
        )

    assert time.monotonic() - started < 1


def test_helper_discovery_binds_to_same_keg_and_rejects_symlinked_helper(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "Cellar/open-brain/0.1.0"
    base = prefix / "bin/open-brain"
    helper = prefix / "libexec/open-brain-graphify"
    base.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    base.write_text("base", encoding="utf-8")
    helper.write_text("helper", encoding="utf-8")
    base.chmod(0o755)
    helper.chmod(0o755)
    linked_base = tmp_path / "bin/open-brain"
    linked_base.parent.mkdir()
    linked_base.symlink_to(base)

    assert discover_graphify_executable(linked_base) == helper

    helper.unlink()
    helper.symlink_to(base)
    with pytest.raises(GraphifyFailure, match="adapter_unavailable"):
        discover_graphify_executable(linked_base)


def test_projection_store_publishes_complete_generation_and_detects_staleness(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    result: dict[str, object] = {
        "protocol": GRAPHIFY_PROTOCOL,
        "status": "ok",
        "pages": [FIRST, SECOND],
        "links": [
            {"source": FIRST, "target": SECOND, "kind": "explicit_reference"}
        ],
        "diagnostics": [],
    }
    adapter = GraphifyAdapter(
        Path("/opt/open-brain/libexec/open-brain-graphify"),
        transport=FakeTransport(_capabilities(), result),
    )
    store = GraphProjectionStore(root, capture_root_identity(root))

    fresh = store.refresh(_snapshot(), adapter)

    assert fresh.status == "fresh"
    assert fresh.failure is None
    assert fresh.generation_id is not None
    assert fresh.links == (
        StructuralGraphLink(
            source_note_id=FIRST,
            source_revision_id="revision_00000000-0000-4000-8000-000000000101",
            target_note_id=SECOND,
            target_revision_id="revision_00000000-0000-4000-8000-000000000102",
        ),
    )
    assert (root / PROJECTION_PATH).is_file()

    changed = replace(_snapshot(), snapshot_sha256="f" * 64, policy_generation=3)
    stale = store.load(changed)

    assert stale.status == "stale"
    assert stale.failure == "snapshot_changed"
    assert stale.generation_id == fresh.generation_id
    assert stale.links == fresh.links


def test_projection_failure_retains_last_successful_generation(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    result: dict[str, object] = {
        "protocol": GRAPHIFY_PROTOCOL,
        "status": "ok",
        "pages": [FIRST, SECOND],
        "links": [
            {"source": FIRST, "target": SECOND, "kind": "explicit_reference"}
        ],
        "diagnostics": [],
    }
    store = GraphProjectionStore(root, capture_root_identity(root))
    adapter = GraphifyAdapter(
        Path("/opt/open-brain/libexec/open-brain-graphify"),
        transport=FakeTransport(_capabilities(), result),
    )
    fresh = store.refresh(_snapshot(), adapter)

    failed = store.record_failure(_snapshot(), "timeout")

    assert failed.status == "stale"
    assert failed.failure == "timeout"
    assert failed.generation_id == fresh.generation_id
    assert failed.links == fresh.links


def test_projection_store_never_overwrites_unrecognized_existing_bytes(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    target = root / PROJECTION_PATH
    target.parent.mkdir(mode=0o700, parents=True)
    target.write_bytes(b"owner content\n")
    identity = capture_root_identity(root)
    store = GraphProjectionStore(root, identity)

    with pytest.raises(GraphifyFailure, match="output_invalid"):
        store.record_failure(_snapshot(), "worker_failed")

    assert target.read_bytes() == b"owner content\n"


def test_projection_presentation_distinguishes_explicit_and_inferred_evidence() -> None:
    structural = StructuralGraphReceipt(
        workspace_id=_snapshot().workspace_id,
        status="fresh",
        generation_id="graph_" + "1" * 64,
        snapshot_sha256=_snapshot().snapshot_sha256,
        adapter_identity="graphify:" + "2" * 64,
        links=(
            StructuralGraphLink(
                source_note_id=FIRST,
                source_revision_id="revision_00000000-0000-4000-8000-000000000101",
                target_note_id=SECOND,
                target_revision_id="revision_00000000-0000-4000-8000-000000000102",
            ),
        ),
        diagnostics=(),
        failure=None,
    )
    inferred = ManagedSuggestion(
        suggestion_id="suggestion_00000000-0000-4000-8000-000000000103",
        workspace_id=_snapshot().workspace_id,
        source_note_id=FIRST,
        source_revision_id="revision_00000000-0000-4000-8000-000000000101",
        target_note_id=SECOND,
        target_revision_id="revision_00000000-0000-4000-8000-000000000102",
        source_quote="first evidence",
        target_quote="second evidence",
        provider=ManagedProvider.OPENAI_API,
        model="gpt-6-astra",
    )

    result = projection_result(_snapshot(), structural, (inferred,))

    assert result["structural_links"] == [
        {
            "kind": "explicit_reference",
            "revision_status": "current",
            "source_note_id": FIRST,
            "source_revision_id": "revision_00000000-0000-4000-8000-000000000101",
            "target_note_id": SECOND,
            "target_revision_id": "revision_00000000-0000-4000-8000-000000000102",
        }
    ]
    assert result["inferred_suggestions"] == [
        {
            "kind": "inferred_suggestion",
            "model": "gpt-6-astra",
            "provider": "openai_api",
            "revision_status": "current",
            "source_evidence": {"note_id": FIRST, "quote": "first evidence"},
            "suggestion_id": inferred.suggestion_id,
            "target_evidence": {"note_id": SECOND, "quote": "second evidence"},
        }
    ]


def test_canvas_projection_is_deterministic_and_distinguishes_edge_authority() -> None:
    snapshot = _snapshot()
    structural = StructuralGraphReceipt(
        workspace_id=snapshot.workspace_id,
        status="fresh",
        generation_id="graph_" + "1" * 64,
        snapshot_sha256=snapshot.snapshot_sha256,
        adapter_identity="graphify:" + "2" * 64,
        links=(
            StructuralGraphLink(
                source_note_id=FIRST,
                source_revision_id=snapshot.sources[0].revision_id,
                target_note_id=SECOND,
                target_revision_id=snapshot.sources[1].revision_id,
            ),
        ),
        diagnostics=(),
        failure=None,
    )
    suggestion = ManagedSuggestion(
        suggestion_id="suggestion_00000000-0000-4000-8000-000000000103",
        workspace_id=snapshot.workspace_id,
        source_note_id=SECOND,
        source_revision_id=snapshot.sources[1].revision_id,
        target_note_id=FIRST,
        target_revision_id=snapshot.sources[0].revision_id,
        source_quote="second evidence",
        target_quote="first evidence",
        provider=ManagedProvider.OPENAI_API,
        model="gpt-6-astra",
    )

    canvas = canvas_result(snapshot, structural, (suggestion,))

    assert canvas == canvas_result(snapshot, structural, (suggestion,))
    nodes = cast(list[dict[str, object]], canvas["nodes"])
    edges = cast(list[dict[str, object]], canvas["edges"])
    file_nodes = [node for node in nodes if node["type"] == "file"]
    assert [node["file"] for node in file_nodes] == ["notes/first.md", "notes/second.md"]
    assert [(edge["label"], edge["color"]) for edge in edges] == [
        ("Explicit link", "4"),
        ("Suggested", "3"),
    ]
    assert all(edge["toEnd"] == "arrow" for edge in edges)
    rendered = portable_canonical_json_bytes(canvas)
    assert b"first evidence" not in rendered
    assert b"gpt-6-astra" not in rendered


def test_canvas_projection_marks_old_revisions_and_omits_unmapped_edges() -> None:
    snapshot = _snapshot()
    structural = StructuralGraphReceipt(
        workspace_id=snapshot.workspace_id,
        status="stale",
        generation_id="graph_" + "1" * 64,
        snapshot_sha256="f" * 64,
        adapter_identity="graphify:" + "2" * 64,
        links=(
            StructuralGraphLink(
                source_note_id=FIRST,
                source_revision_id="revision_00000000-0000-4000-8000-000000000109",
                target_note_id=SECOND,
                target_revision_id=snapshot.sources[1].revision_id,
            ),
        ),
        diagnostics=(),
        failure="snapshot_changed",
    )
    suggestion = ManagedSuggestion(
        suggestion_id="suggestion_00000000-0000-4000-8000-000000000103",
        workspace_id=snapshot.workspace_id,
        source_note_id=FIRST,
        source_revision_id="revision_00000000-0000-4000-8000-000000000109",
        target_note_id=SECOND,
        target_revision_id=snapshot.sources[1].revision_id,
        source_quote="first evidence",
        target_quote="second evidence",
        provider=ManagedProvider.OPENAI_API,
        model="gpt-6-astra",
    )

    canvas = canvas_result(snapshot, structural, (suggestion,))

    nodes = cast(list[dict[str, object]], canvas["nodes"])
    edges = cast(list[dict[str, object]], canvas["edges"])
    assert [(edge["label"], edge["color"]) for edge in edges] == [
        ("Explicit link (stale)", "1"),
        ("Suggested (stale)", "1"),
    ]
    status_text = cast(str, nodes[0]["text"])
    assert "Status: stale" in status_text
    assert "Stale suggestions: 1" in status_text
