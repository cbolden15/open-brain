from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

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
from open_brain.services.t03_adapters import owner_authority


def share(workflow: HarnessWorkflow, *tiers: PrivacyTier) -> None:
    """The core's owner egress policy is the only source of shared tiers."""
    workflow.tasks.owner_egress_policy.set(
        set(tiers), operation_id="share-" + uuid4().hex,
        authority=owner_authority(workflow.tasks, session_id="owner-test"),
    )


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
        "sources": [
            {"id": "example", "project_root": str(project), "tier": "work", "local_only": False}
        ],
        "exclusions": [],
    }
    workflow = HarnessWorkflow(
        tasks, lambda: HarnessPolicy.from_dict(raw), consent_store=consent, local_only_host=True
    )
    share(workflow, PrivacyTier.WORK)
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
    assert sorted(m.text for m in local.memories) == [
        "I prefer violet notebooks.", "I prefer violet pencils.",
    ]


def test_owner_egress_policy_and_durable_provider_revocations(tmp_path: Path) -> None:
    workflow, _, project, _, consent = setup(tmp_path)
    workflow.capture(
        "codex", "session-one", project, [MemoryMessage("user", "I prefer lentil soup.", 1)]
    )
    assert workflow.recall("lentil", provider="codex").status == "success"
    share(workflow)
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    share(workflow, PrivacyTier.WORK)
    assert workflow.recall("lentil", provider="claude-code").status == "success"
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


def test_later_confidential_marking_lowers_the_whole_event_to_unknown(tmp_path: Path) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    first_message = MemoryMessage("user", "I prefer lentil soup.", 1)
    first = workflow.capture("codex", "marked-later", project, [first_message])
    assert first["status"] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success"
    marked = [first_message, MemoryMessage("user", "Confidential:\nI prefer violet pencils.", 2)]
    later = workflow.capture("codex", "marked-later", project, marked)
    repeated = workflow.capture("codex", "marked-later", project, marked)
    assert later["status"] == repeated["status"] == "acknowledged"
    assert later["capture_ids"] == repeated["capture_ids"]
    assert workflow.recall("lentil pencils", provider="codex").status == "success_empty"
    local = workflow.recall_local("lentil pencils", project=project)
    assert len(local.memories) == 2
    first_ids = first["capture_ids"]
    assert isinstance(first_ids, list)
    assert cast(CaptureTasks, workflow.tasks.capture).get(first_ids[0]) is not None
    # Policy B keeps no per-session latch: the tier follows the event as written.
    old = workflow.capture("codex", "marked-later", project, [first_message])
    assert old["status"] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success"


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
    workflow, _, project, _, _ = setup(tmp_path)
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


def test_spooled_long_session_delivers_early_late_recipe_and_dedupes_replay(
    tmp_path: Path,
) -> None:
    from open_brain.services.harness_events import HarnessCaptureEvent
    from open_brain.services.harness_spool import HarnessSpool

    workflow, _, project, _, _ = setup(tmp_path)
    spool = HarnessSpool(tmp_path / "outbox", derive_brain_id(workflow.tasks.profile.tenant_id))
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
    assert spool.append(source)["batch_count"] == 3
    first = spool.drain(workflow)
    assert first["status"] == "delivered"
    early = workflow.recall("fixture0", provider="codex")
    late = workflow.recall("fixture299", provider="claude-code")
    assert early.status == late.status == "success"
    assert [m.text for m in late.memories] == ["I prefer fixture299 pencils."]
    meal = workflow.recall("lentils", provider="pi")
    assert meal.status == "success"
    assert meal.memories[0].text == recipe
    context = workflow.read_context(meal.memory_ids[0], provider="pi")
    assert context.memories[0].source_context == recipe
    # Replaying the same event is a downstream no-op through its delivery IDs.
    assert spool.append(source)["status"] == "queued"
    replay = spool.drain(workflow)
    assert replay["status"] == "delivered"

    def acknowledgements(result: dict[str, object]) -> list[object]:
        return [row["acknowledgement"] for row in cast(list[dict[str, Any]], result["results"])]

    assert acknowledgements(replay) == acknowledgements(first)
    again = workflow.recall("lentils", provider="codex")
    assert again.memory_ids == meal.memory_ids


@pytest.mark.parametrize("client", ["codex", "claude-code", "pi", "hermes"])
def test_capture_tier_follows_client_default_unless_a_source_rule_matches(
    tmp_path: Path, client: str,
) -> None:
    workflow, _, project, unknown, consent = setup(tmp_path)
    share(workflow, PrivacyTier.WORK, PrivacyTier.PERSONAL)
    active = next(r for r in consent.load().records if r.provider_id == "codex" and r.active)
    consent.replace(
        consent_id=active.consent_id, provider_id="codex",
        allowed_tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        operation_id="regrant-codex", decided_at="2026-01-02T00:00:00Z",
    )
    pairs = ((unknown, "I prefer violet notebooks."), (project, "I prefer violet pens."))
    for target, text in pairs:
        message = MemoryMessage("user", text, 1, True, "entry-" + target.name)
        assert workflow.capture(client, "tiers", target, [message])["status"] == "acknowledged"
    visible = sorted(m.text for m in workflow.recall("violet", provider="codex").memories)
    # Unmapped: claude-code/codex default to personal; pi/hermes default to unknown.
    # The work source rule overrides every client default.
    expected = ["I prefer violet notebooks."] if client in {"codex", "claude-code"} else []
    assert visible == sorted(expected + ["I prefer violet pens."])
    local = sorted(m.text for m in workflow.recall_local("violet", project=unknown).memories)
    assert local == ["I prefer violet notebooks.", "I prefer violet pens."]


def test_existing_clients_keep_empty_recall_when_consent_and_policy_do_not_overlap(
    tmp_path: Path,
) -> None:
    workflow, raw, project, _, _ = setup(tmp_path)
    raw["sources"][0]["tier"] = "personal"
    share(workflow, PrivacyTier.WORK, PrivacyTier.PERSONAL)
    captured = workflow.capture(
        "codex", "synthetic-personal-session", project,
        [MemoryMessage("user", "I prefer violet pencils.", 1)],
    )
    assert captured["status"] == "acknowledged"
    # The policy shares personal, but codex consent grants work only. Omitted narrowing retains the
    # established empty-result status, while an explicit narrowed read is denied.
    ordinary = workflow.recall("violet pencils", provider="codex")
    assert ordinary.status == "success_empty"
    assert ordinary.memories == ()
    narrowed = workflow.recall(
        "violet pencils", provider="codex", read_tiers=frozenset({PrivacyTier.PERSONAL}),
    )
    assert narrowed.status == "refused"
    assert narrowed.error_code == "provider_denied"


def test_stale_spooled_replay_cannot_undo_a_newer_confidential_marking(tmp_path: Path) -> None:
    from open_brain.services.harness_events import HarnessCaptureEvent
    from open_brain.services.harness_spool import HarnessSpool

    workflow, _, project, _, _ = setup(tmp_path)
    spool = HarnessSpool(tmp_path / "outbox", derive_brain_id(workflow.tasks.profile.tenant_id))
    message = MemoryMessage("user", "I prefer lentil soup.", 1, True, "entry-1")
    # Event A (unmarked, work) failed delivery and waits in the spool.
    stale = HarnessCaptureEvent("codex", "session", project, (message,), {})
    assert spool.append(stale)["status"] == "queued"
    # Event B, later in the same session, marks it confidential and is delivered.
    marked = (message, MemoryMessage("user", "Confidential:\nI prefer violet pencils.", 2))
    assert workflow.capture("codex", "session", project, marked)["status"] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    # Replaying A is superseded by the newer head; it neither re-widens nor stays spooled.
    assert spool.drain(workflow)["status"] == "delivered"
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    assert spool.status()["counts"] == {"pending": 0, "refused": 0, "unavailable": 0}
    # A capture observed after B still applies normally.
    assert workflow.capture("codex", "session", project, [message])["status"] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success"


def test_tightening_replay_with_an_older_timestamp_still_applies(tmp_path: Path) -> None:
    workflow, _, project, _, _ = setup(tmp_path)
    message = MemoryMessage("user", "I prefer lentil soup.", 1, True, "entry-1")
    assert workflow.capture("codex", "session", project, [message])["status"] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success"
    marked = (message, MemoryMessage("user", "Confidential:\nI prefer violet pencils.", 2))
    # A replay stamped long before the head (a skewed clock) still lowers the tier.
    assert workflow.capture("codex", "session", project, marked, observed_at=1)["status"] == (
        "acknowledged"
    )
    assert workflow.recall("lentil", provider="codex").status == "success_empty"


def test_inline_capture_is_never_skipped_even_with_a_skewed_back_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain.services import harness_workflow

    workflow, _, project, _, _ = setup(tmp_path)
    message = MemoryMessage("user", "I prefer lentil soup.", 1, True, "entry-1")
    marked = (message, MemoryMessage("user", "Confidential:\nI prefer violet pencils.", 2))
    # A replay from a host whose clock runs far ahead wrote the current head.
    ahead = 4_000_000_000_000_000_000
    assert workflow.capture("codex", "session", project, marked, observed_at=ahead)[
        "status"
    ] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    monkeypatch.setattr(harness_workflow, "_now_ns", lambda: 1)
    # This host's clock is behind, but an inline capture always applies.
    assert workflow.capture("codex", "session", project, [message])["status"] == "acknowledged"
    assert workflow.recall("lentil", provider="codex").status == "success"

