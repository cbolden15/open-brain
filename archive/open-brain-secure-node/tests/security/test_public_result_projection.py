from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from open_brain_engine.engine import CaptureAction, ReferencePayload, TextPayload
from open_brain_engine.engine.contracts import project_public_result_text
from open_brain_engine.engine.local import BrainEngine

from open_brain.integrations.phase1_ui import Phase1UiHandler, Phase1UiRequest
from open_brain.profile import compile_single_user_local


def test_public_result_projection_redacts_bounded_percent_and_html_encodings() -> None:
    protected = "https://example.test/private-reference"
    digest = sha256(protected.encode("utf-8")).hexdigest()
    case_varied = "HTTPS://EXAMPLE.TEST/private-reference"
    case_varied_digest = sha256(case_varied.encode("utf-8")).hexdigest()
    html_encoded = "".join(f"&#{ord(character)};" for character in protected)
    html_case_varied = "".join(f"&#{ord(character)};" for character in case_varied)
    html_digest = "".join(f"&#x{ord(character):x};" for character in digest)
    html_case_varied_digest = "".join(
        f"&#x{ord(character):x};" for character in case_varied_digest
    )
    encoded_values = (
        protected,
        case_varied,
        quote(protected, safe=""),
        quote(case_varied, safe=""),
        quote(quote(protected, safe=""), safe=""),
        html_encoded,
        html_case_varied,
        digest,
        case_varied_digest,
        quote(digest, safe=""),
        html_digest,
        html_case_varied_digest,
    )

    for value in encoded_values:
        rendered = project_public_result_text(
            "ordinary useful text " + value,
            protected_literals=(protected,),
        )
        assert rendered == "ordinary useful text [protected]"


def test_direct_task_results_project_space_slugs_and_canonical_paths(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    sensitive_name = "Sensitive /private/canary api_key=AAAA"
    space = engine.inbox.create_space(sensitive_name, delivery_id="task-projection.space")
    canonical = engine.capture.accept(
        TextPayload("ordinary canonical task text"),
        delivery_id="task-projection.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    stored = engine.capture.get(canonical.capture_id)
    durable_space = next((root / "content" / "spaces").rglob("_space.md"))
    durable_page = next((root / "content" / "spaces").rglob("page_*.md"))
    durable_page_before = durable_page.read_bytes()

    assert space.slug == space.space_id
    assert all(item.slug == item.space_id for item in engine.inbox.spaces())
    assert canonical.state == "published"
    assert canonical.canonical_path == canonical.capture_id
    assert stored is not None
    assert stored.state == "published"
    assert stored.canonical_path == canonical.capture_id
    assert sensitive_name in durable_space.read_text(encoding="utf-8")
    assert canonical.canonical_path not in str(durable_page.relative_to(root))
    assert durable_page.read_bytes() == durable_page_before


def test_public_results_project_sensitive_text_without_changing_durable_values(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    path_canary = "/private/" + "x" * 48
    credential_canary = "api" + "_key=" + "A" * 32
    source_canary = "https://example.test/private-reference"
    source_digest = sha256(source_canary.encode("utf-8")).hexdigest()
    space = engine.inbox.create_space(
        "Private " + path_canary,
        delivery_id="projection.space",
    )
    engine.capture.accept(
        ReferencePayload(
            source_canary,
            "useful normal search text "
            + path_canary
            + " "
            + credential_canary
            + " "
            + source_canary
            + " "
            + source_digest,
        ),
        delivery_id="projection.reference",
    )
    engine.capture.accept(
        TextPayload("useful normal canonical text"),
        delivery_id="projection.canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
        title="Title " + path_canary + " " + credential_canary,
    )

    results = engine.retrieval.search("useful normal")
    rendered = "\n".join(f"{result.title}\n{result.excerpt}" for result in results)
    handler = Phase1UiHandler(expected_bearer_token="synthetic-token", tasks=engine.tasks.phase1)
    headers = (("Authorization", "Bearer synthetic-token"),)
    spaces = handler.handle(Phase1UiRequest("GET", "/api/spaces", headers))
    dashboard = handler.handle(Phase1UiRequest("GET", "/", headers))

    assert "useful normal" in rendered
    assert path_canary not in rendered
    assert credential_canary not in rendered
    assert source_canary not in rendered
    assert source_digest not in rendered
    assert b"[protected]" in spaces.body
    assert path_canary.encode("utf-8") not in spaces.body + dashboard.body
    durable_space = next((tmp_path / "brain" / "content" / "spaces").rglob("_space.md"))
    assert path_canary in durable_space.read_text(encoding="utf-8")
