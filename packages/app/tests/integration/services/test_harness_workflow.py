from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.capture import CaptureTasks
from open_brain_engine.engine.contracts import TextPayload
from open_brain_engine.engine.harness_memory import encode_memory
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


def test_paraphrased_tasks_keep_recipe_details_and_source_resolution(tmp_path: Path) -> None:
    workflow, raw, project, _, _ = setup(tmp_path)
    recipe = (
        "Chickpea supper\nIngredients\n- 1 cup chickpeas\n- 2 tablespoons lemon juice\n"
        "Directions\n1. Rinse the chickpeas.\n2. Mix and chill for 15 minutes."
    )
    message = "My preferred editor is Vim.\nCan you explain its shortcuts?"
    captured = workflow.capture(
        "claude-code",
        "task-source",
        project,
        [
            MemoryMessage("user", message, 1),
            MemoryMessage("user", "Please give me a chickpea recipe.", 2),
            MemoryMessage("assistant", recipe, 3),
            MemoryMessage("assistant", "You probably prefer meat.", 4),
            MemoryMessage("user", "My preferred editor is Emacs.", 5, authored=False),
            *[MemoryMessage("user", "Thanks!", i) for i in range(6, 12)],
        ],
    )
    assert captured["status"] == "acknowledged"
    meal = workflow.recall("Prepare supper tomorrow", provider="codex")
    assert meal.status == "success"
    assert [memory.text for memory in meal.memories] == [recipe]
    coding = workflow.recall("Configure an editing environment", provider="pi")
    assert [memory.text for memory in coding.memories] == ["My preferred editor is Vim."]
    assert len(coding.memory_ids) == 1
    context = workflow.read_context(coding.memory_ids[0], provider="codex")
    assert context.status == "success"
    assert context.memories[0].source_context == message
    assert context.memories[0].citation.timestamp is None
    raw["exclusions"] = [str(project)]
    assert workflow.read_context(coding.memory_ids[0], provider="codex").memories == ()


def test_explicit_changed_decision_withholds_previous_value_even_when_query_only_matches_old(
    tmp_path: Path,
) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    original = MemoryMessage("user", "We decided to use SQLite for the sample project.", 1)
    change = MemoryMessage(
        "user", "We changed our decision to PostgreSQL for the sample project.", 9
    )
    saved = workflow.capture("codex", "decision-source", project, [original, change])
    assert saved["status"] == "acknowledged"
    assert workflow.recall("SQLite", provider="claude-code").memories == ()
    current = workflow.recall("Which database should this sample project use?", provider="pi")
    assert [memory.text for memory in current.memories] == [change.text]
    capture_ids = saved["capture_ids"]
    assert isinstance(capture_ids, list)
    retained = workflow.read_context(capture_ids[0], provider="claude-code")
    assert retained.memories[0].text == original.text
    assert retained.memory_states == ("superseded",)


def test_changed_decision_does_not_supersede_a_different_named_subject(tmp_path: Path) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    workflow.capture(
        "codex",
        "decision-subjects",
        project,
        [
            MemoryMessage("user", "We decided to use SQLite for the archive project.", 1),
            MemoryMessage("user", "We decided to use YAML for the export format.", 2),
            MemoryMessage("user", "We changed our decision to JSON for the export format.", 3),
        ],
    )
    assert len(workflow.recall("SQLite", provider="codex").memories) == 1
    assert workflow.recall("YAML", provider="codex").memories == ()


def test_source_context_ids_cannot_read_private_or_ordinary_owner_records(tmp_path: Path) -> None:
    workflow, _, project, unknown, _ = setup(tmp_path)
    private = workflow.capture(
        "pi",
        "private-source",
        unknown,
        [MemoryMessage("user", "I prefer violet notebooks.", 1)],
    )
    ids = private["capture_ids"]
    assert isinstance(ids, list)
    cloud = workflow.read_context(ids[0], provider="codex")
    assert cloud.status == "refused" and cloud.memories == ()
    local = workflow.read_context_local(ids[0], project=unknown)
    assert local.status == "success"
    assert workflow.read_context_local(ids[0], project=project).memories == ()
    assert workflow.read_context("invalid-id", provider="codex").memories == ()


def test_owner_json_cannot_impersonate_harness_context_even_in_a_harness_space(
    tmp_path: Path,
) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    workflow.capture(
        "codex",
        "harness-context",
        project,
        [MemoryMessage("user", "I prefer violet notebooks.", 1)],
    )
    recalled = workflow.recall("violet", provider="codex")
    spaces = workflow.tasks.spaces.spaces()
    assert len(spaces) == 1
    owner = workflow.tasks.capture.accept(
        TextPayload(encode_memory(recalled.memories[0])),
        delivery_id="owner-json",
        space_id=spaces[0].space_id,
        privacy_tier=PrivacyTier.WORK,
    )
    assert owner.capture_id is not None
    assert workflow.read_context_local(owner.capture_id, project=project).status == "refused"
    assert workflow.read_context(owner.capture_id, provider="codex").memories == ()
    assert len(workflow.recall("violet", provider="codex").memories) == 1


def test_resolved_context_rechecks_consent_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _, project, _, consent = setup(tmp_path)
    workflow.capture(
        "codex",
        "context-revoked",
        project,
        [MemoryMessage("user", "I prefer violet notebooks.", 1)],
    )
    source_id = workflow.recall("violet", provider="codex").memory_ids[0]
    read = workflow.tasks.retrieval.read_record

    def revoked(*args: Any, **kwargs: Any) -> Any:
        result = read(*args, **kwargs)
        grant = next(r for r in consent.load().records if r.provider_id == "codex" and r.active)
        consent.revoke(
            consent_id=grant.consent_id,
            operation_id="revoke-during-context",
            decided_at="2026-01-02T00:00:00Z",
        )
        return result

    monkeypatch.setattr(workflow.tasks.retrieval, "read_record", revoked)
    result = workflow.read_context(source_id, provider="codex")
    assert result.status == "refused"
    assert result.error_code == "authorization_changed"
    assert result.memories == ()
    assert result.memory_ids == ()


def test_supersession_scan_fails_closed_when_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    workflow.capture(
        "codex",
        "scan-bound",
        project,
        [MemoryMessage("user", "We decided to use SQLite for the sample project.", 1)],
    )
    search = workflow.tasks.retrieval.search_page

    class Incomplete:
        def to_wire(self) -> dict[str, Any]:
            return {"results": [], "complete": False, "next_cursor": "metadata-only"}

    def bounded(*args: Any, **kwargs: Any) -> Any:
        if args[0].query == "changed instead now":
            return Incomplete()
        return search(*args, **kwargs)

    monkeypatch.setattr(workflow.tasks.retrieval, "search_page", bounded)
    result = workflow.recall("SQLite", provider="codex")
    assert result.status == "refused"
    assert result.error_code == "retrieval_budget_exceeded"
    assert result.memories == ()


@pytest.mark.parametrize(
    "change",
    [
        "Our decision is now PostgreSQL for the sample project.",
        "We decided to use PostgreSQL instead of SQLite for the sample project.",
    ],
)
def test_other_explicit_supersession_syntax_is_source_grounded(tmp_path: Path, change: str) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    workflow.capture(
        "codex",
        "changed-wording",
        project,
        [
            MemoryMessage("user", "We decided to use SQLite for the sample project.", 1),
            MemoryMessage("user", change, 2),
        ],
    )
    recalled = workflow.recall("SQLite", provider="codex")
    assert all(memory.text == change for memory in recalled.memories)
    assert workflow.recall("PostgreSQL", provider="codex").memories[0].text == change


def test_child_first_delivery_retains_original_parent_ordinal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from open_brain.services.harness_events import HarnessCaptureEvent
    from open_brain.services.harness_outbox import HarnessOutbox

    workflow, _, project, _, _ = setup(tmp_path)
    outbox = HarnessOutbox(tmp_path / "outbox", derive_brain_id(workflow.tasks.profile.tenant_id))
    parent = HarnessCaptureEvent(
        "pi", "parent", project,
        (MemoryMessage("user", "I prefer violet notebooks.", 7, True, "native-entry"),), {},
    )
    assert outbox.enqueue(parent)["status"] == "queued"
    child = replace(
        parent, session_id="child", parent_session_id="parent",
        messages=(replace(parent.messages[0], ordinal=0),),
    )
    assert outbox.enqueue(child)["status"] == "queued"
    capture = workflow.capture

    def parent_offline(client: str, session: str, *args: Any, **kwargs: Any) -> dict[str, object]:
        if session == "parent":
            return {"status": "unavailable", "error_code": "bridge_unavailable"}
        return capture(client, session, *args, **kwargs)

    monkeypatch.setattr(workflow, "capture", parent_offline)
    outbox.drain(workflow)
    first = workflow.recall("violet", provider="codex")
    assert len(first.memories) == 1
    assert first.memories[0].citation.session_id == "parent"
    assert first.memories[0].citation.message_ordinal == 7
    monkeypatch.setattr(workflow, "capture", capture)
    outbox.drain(workflow)
    again = workflow.recall("violet", provider="codex")
    assert again.memory_ids == first.memory_ids
    assert again.memories == first.memories


def test_native_custody_long_session_delivers_early_late_recipe_and_stable_fork(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from open_brain.services.harness_events import HarnessCaptureEvent
    from open_brain.services.harness_outbox import HarnessOutbox

    workflow, _, project, _, _ = setup(tmp_path)
    outbox = HarnessOutbox(tmp_path / "outbox", derive_brain_id(workflow.tasks.profile.tenant_id))
    messages = tuple(
        MemoryMessage(
            "user",
            f"I prefer fixture{i} pencils." if i < 40 or i >= 260 else "Thanks.",
            i,
            True,
            f"entry-{i}",
        )
        for i in range(300)
    )
    recipe = (
        "Ingredients:\n- 1 cup lentils\n- 2 cups water\nDirections:\n"
        "1. Rinse lentils.\n2. Simmer 20 minutes."
    )
    messages += (
        MemoryMessage("user", "Please give me a complete soup recipe.", 300, True, "request"),
        MemoryMessage("assistant", recipe, 301, True, "recipe"),
    )
    source = HarnessCaptureEvent("pi", "parent", project, messages, {})
    assert outbox.enqueue(source)["batch_count"] == 3
    assert outbox.drain(workflow)["status"] == "delivered"
    early = workflow.recall("fixture0", provider="codex")
    late = workflow.recall("fixture299", provider="claude-code")
    assert early.status == late.status == "success"
    assert [m.text for m in late.memories] == ["I prefer fixture299 pencils."]
    meal = workflow.recall("lentils", provider="pi")
    assert meal.status == "success"
    assert meal.memories[0].text == recipe
    context = workflow.read_context(meal.memory_ids[0], provider="pi")
    assert context.memories[0].source_context == recipe
    assert outbox.enqueue(source)["status"] == "queued"
    assert outbox.drain(workflow)["already_delivered"] == 3
    child = replace(
        source,
        session_id="child",
        parent_session_id="parent",
        session_timestamp="2026-01-02T00:00:00Z",
        messages=(messages[-1], messages[-2]),
    )
    # Native chronological order is part of the event contract.
    child = replace(child, messages=tuple(reversed(child.messages)))
    assert outbox.enqueue(child)["status"] == "queued"
    assert outbox.drain(workflow)["status"] == "delivered"
    again = workflow.recall("lentils", provider="codex")
    assert again.memory_ids == meal.memory_ids
    assert again.memories[0].citation.session_id == "parent"
