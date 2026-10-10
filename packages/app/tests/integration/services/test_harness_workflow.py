from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.capture import CaptureTasks
from open_brain_engine.engine.local import open_local_engine
from open_brain_engine.storage.filesystem import StorageError

from open_brain.profile import compile_single_user_local
from open_brain.services.harness_memory import MemoryMessage
from open_brain.services.harness_policy import HarnessPolicy
from open_brain.services.harness_workflow import HarnessWorkflow
from open_brain.services.session_consent import DurableProviderConsentStore


def setup(
    tmp_path: Path,
) -> tuple[
    HarnessWorkflow,
    dict[str, Any],
    Path,
    Path,
    DurableProviderConsentStore,
]:
    project = tmp_path / "project"
    project.mkdir()
    unknown = tmp_path / "unmapped"
    unknown.mkdir()
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    consent = DurableProviderConsentStore(
        authority / "consent.json",
        brain_id=derive_brain_id(tasks.profile.tenant_id),
        issuer_epoch=1,
    )
    for provider in ("codex", "claude-code", "pi"):
        consent.grant(
            provider_id=provider,
            allowed_tiers=frozenset({PrivacyTier.WORK}),
            operation_id=f"grant-{provider}",
            decided_at="2026-01-01T00:00:00Z",
        )
    raw = {
        "schema_version": "open-brain-harness-policy.v1",
        "enabled": True,
        "providers": {name: ["work"] for name in ("codex", "claude-code", "pi")},
        "sources": [
            {"id": "example", "project_root": str(project), "tier": "work", "local_only": False}
        ],
        "exclusions": [],
    }
    workflow = HarnessWorkflow(
        tasks, lambda: HarnessPolicy.from_dict(raw), consent_store=consent, local_only_host=True
    )
    return workflow, raw, project, unknown, consent


@pytest.mark.parametrize("source", ["codex", "claude-code", "pi"])
def test_cross_client_capture_recall_and_replay(tmp_path: Path, source: str) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    messages = [MemoryMessage("user", "I prefer lentil soup on Fridays.", 1)]
    first = workflow.capture(source, "session-one", project, messages)
    second = workflow.capture(source, "session-one", project, messages)
    assert first["status"] == second["status"] == "acknowledged"
    assert first["capture_ids"] == second["capture_ids"]
    for provider in ("codex", "claude-code", "pi"):
        result = workflow.recall("lentil soup", provider=provider)
        assert result.status == "success"
        assert len(result.memories) == 1
        assert result.memories[0].citation.harness == source


def test_unknown_and_marked_records_are_denied_cloud_but_local_project_read_works(
    tmp_path: Path,
) -> None:
    workflow, _, project, unknown, _ = setup(tmp_path)
    workflow.capture(
        "pi", "unknown-session", unknown, [MemoryMessage("user", "I prefer violet notebooks.", 1)]
    )
    workflow.capture(
        "pi",
        "marked-session",
        project,
        [MemoryMessage("user", "Confidential:\nI prefer violet pencils.", 1)],
    )
    assert workflow.recall("violet", provider="codex").status == "success_empty"
    local = workflow.recall_local("violet", project=unknown)
    assert local.status == "success"
    assert "notebooks" in local.memories[0].text


def test_current_policy_and_durable_provider_revocations(tmp_path: Path) -> None:
    workflow, raw, project, _, consent = setup(tmp_path)
    workflow.capture(
        "codex", "session-one", project, [MemoryMessage("user", "I prefer lentil soup.", 1)]
    )
    assert workflow.recall("lentil", provider="codex").status == "success"
    raw["sources"] = []
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    active = next(
        record
        for record in consent.load().records
        if record.provider_id == "claude-code" and record.active
    )
    consent.revoke(
        consent_id=active.consent_id,
        operation_id="revoke-claude",
        decided_at="2026-01-02T00:00:00Z",
    )
    assert workflow.recall("lentil", provider="claude-code").status == "refused"


def test_excluded_source_and_credentials_never_captured(tmp_path: Path) -> None:
    workflow, raw, project, _, _ = setup(tmp_path)
    raw["exclusions"] = [str(project)]
    assert (
        workflow.capture(
            "codex", "excluded", project, [MemoryMessage("user", "I prefer soup.", 1)]
        )["status"]
        == "refused"
    )
    raw["exclusions"] = []
    assert (
        workflow.capture(
            "codex",
            "credential",
            project,
            [MemoryMessage("user", "I prefer password=synthetic-credential.", 1)],
        )["status"]
        == "refused"
    )
    assert workflow.recall("synthetic", provider="codex").status == "success_empty"


def test_recipe_preserves_full_material_and_authorship_not_indexed_metadata(tmp_path: Path) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    recipe = (
        "Lentil soup\nIngredients\n- 2 cups lentils\n- 4 cups water\nDirections\n"
        "1. Rinse lentils.\n2. Simmer for 25 minutes.\n"
    )
    result = workflow.capture(
        "claude-code",
        "recipe-locator",
        project,
        [
            MemoryMessage("user", "Please give me a complete lentil soup recipe.", 1),
            MemoryMessage("assistant", recipe, 2),
        ],
        timestamps={2: "2026-01-01T12:00:00Z"},
    )
    assert result["status"] == "acknowledged"
    recalled = workflow.recall("lentil", provider="codex")
    assert recalled.status == "success"
    assert recalled.memories[0].text == recipe.strip()
    assert recalled.memories[0].citation.authorship == "assistant"
    assert recalled.memories[0].citation.timestamp == "2026-01-01T12:00:00Z"
    assert workflow.recall("recipe-locator", provider="codex").status == "success_empty"
    assert workflow.recall("project-", provider="codex").status == "success_empty"


@pytest.mark.parametrize("restriction", ["exclusion", "nested_rule"])
def test_overlapping_restriction_denies_rule_space_before_ranking(
    tmp_path: Path,
    restriction: str,
) -> None:
    workflow, raw, project, _, _ = setup(tmp_path)
    workflow.capture(
        "codex", "before-change", project, [MemoryMessage("user", "I prefer lentil soup.", 1)]
    )
    nested = project / "restricted"
    nested.mkdir()
    if restriction == "exclusion":
        raw["exclusions"] = [str(nested)]
    else:
        raw["sources"].append(
            {"id": "restricted", "project_root": str(nested), "tier": "secret", "local_only": True}
        )
    assert workflow.recall("lentil", provider="codex", limit=1).status == "success_empty"


def test_policy_rechecked_before_result_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow, raw, project, _, _ = setup(tmp_path)
    workflow.capture(
        "codex", "before-change", project, [MemoryMessage("user", "I prefer lentil soup.", 1)]
    )
    search = workflow.tasks.retrieval.search_page

    def changed(*args: Any, **kwargs: Any) -> Any:
        response = search(*args, **kwargs)
        raw["providers"] = {}
        return response

    monkeypatch.setattr(workflow.tasks.retrieval, "search_page", changed)
    result = workflow.recall("lentil", provider="codex")
    assert result.status == "refused"
    assert result.error_code == "authorization_changed"
    assert result.memories == ()


def test_cloud_constructor_cannot_choose_local_owner_route(tmp_path: Path) -> None:
    workflow, _, project, _, consent = setup(tmp_path)
    cloud = HarnessWorkflow(workflow.tasks, workflow.policy, consent_store=consent)
    with pytest.raises(ValueError, match="local_only_host_required"):
        cloud.recall_local("anything", project=project)


def test_resumed_message_identity_does_not_depend_on_earlier_selection(tmp_path: Path) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    later = MemoryMessage("user", "I prefer violet pencils.", 5)
    first = workflow.capture("codex", "resume", project, [later])
    second = workflow.capture(
        "codex",
        "resume",
        project,
        [
            MemoryMessage("user", "I prefer lentil soup.", 1),
            later,
            *[MemoryMessage("user", "Thanks.", number) for number in range(6, 11)],
        ],
    )
    assert first["status"] == second["status"] == "acknowledged"
    first_ids, second_ids = first["capture_ids"], second["capture_ids"]
    assert isinstance(first_ids, list) and isinstance(second_ids, list)
    assert first_ids[0] in second_ids
    assert len(workflow.recall("pencils", provider="codex").memories) == 1


def test_unknown_project_exclusion_is_rechecked_for_local_reads(tmp_path: Path) -> None:
    workflow, raw, _, unknown, _ = setup(tmp_path)
    workflow.capture(
        "pi", "unknown", unknown, [MemoryMessage("user", "I prefer violet notebooks.", 1)]
    )
    assert workflow.recall_local("violet", project=unknown).status == "success"
    raw["exclusions"] = [str(unknown)]
    assert workflow.recall_local("violet", project=unknown).status == "refused"


def test_later_confidential_source_creates_private_heads_and_never_widens_on_old_replay(
    tmp_path: Path,
) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    first_message = MemoryMessage("user", "I prefer lentil soup.", 1)
    first = workflow.capture("codex", "marked-later", project, [first_message])
    assert first["status"] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success"
    marked = [first_message, MemoryMessage("user", "Confidential:\nI prefer violet pencils.", 2)]
    later = workflow.capture("codex", "marked-later", project, marked)
    repeated = workflow.capture("codex", "marked-later", project, marked)
    old = workflow.capture("codex", "marked-later", project, [first_message])
    assert later["status"] == repeated["status"] == old["status"] == "acknowledged"
    assert later["capture_ids"] == repeated["capture_ids"]
    assert workflow.recall("lentil pencils", provider="codex").status == "success_empty"
    local = workflow.recall_local("lentil pencils", project=project)
    assert len(local.memories) == 2
    first_ids = first["capture_ids"]
    assert isinstance(first_ids, list)
    assert cast(CaptureTasks, workflow.tasks.capture).get(first_ids[0]) is not None


@pytest.mark.parametrize("failure", [StorageError, sqlite3.OperationalError])
def test_real_storage_failures_are_metadata_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    workflow, _, project, _, _ = setup(tmp_path)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise failure("sensitive synthetic error text")

    monkeypatch.setattr(workflow.tasks.spaces, "create_space", broken)
    capture = workflow.capture(
        "codex", "storage", project, [MemoryMessage("user", "I prefer lentil soup.", 1)]
    )
    assert capture["status"] == "unavailable"
    assert "sensitive" not in str(capture)
    monkeypatch.setattr(workflow.tasks.retrieval, "search_page", broken)
    recall = workflow.recall("lentil", provider="codex")
    assert recall.status == "unavailable"
    assert "sensitive" not in str(recall)


def test_private_revision_survives_route_failure_and_old_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    original = MemoryMessage("user", "I prefer lentil soup.", 1)
    assert (
        workflow.capture("codex", "route-failure", project, [original])["status"] == "acknowledged"
    )
    route = workflow._route

    def failed_route(*args: Any, **kwargs: Any) -> Any:
        raise StorageError("synthetic route durability failure")

    monkeypatch.setattr(workflow, "_route", failed_route)
    marked = [original, MemoryMessage("user", "Confidential:\nI prefer violet pencils.", 2)]
    assert workflow.capture("codex", "route-failure", project, marked)["status"] == "unavailable"
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    monkeypatch.setattr(workflow, "_route", route)
    assert (
        workflow.capture("codex", "route-failure", project, [original])["status"] == "acknowledged"
    )
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    assert workflow.capture("codex", "route-failure", project, marked)["status"] == "acknowledged"
    assert len(workflow.recall_local("lentil pencils", project=project).memories) == 2


def test_stable_native_id_survives_changed_display_ordinal_and_timestamp(tmp_path: Path) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    first = workflow.capture(
        "codex",
        "rotate",
        project,
        [
            MemoryMessage("user", "I prefer lentil soup.", 8, source_id="message-stable"),
        ],
    )
    rotated = workflow.capture(
        "codex",
        "rotate",
        project,
        [
            MemoryMessage("user", "I prefer lentil soup.", 1, source_id="message-stable"),
        ],
        timestamps={1: "2026-01-01T00:00:00Z"},
    )
    assert first["status"] == rotated["status"] == "acknowledged"
    assert first["capture_ids"] == rotated["capture_ids"]
    recalled = workflow.recall("lentil", provider="codex")
    assert len(recalled.memories) == 1
    assert recalled.memories[0].citation.message_ordinal == 8
    assert recalled.memories[0].citation.timestamp is None
