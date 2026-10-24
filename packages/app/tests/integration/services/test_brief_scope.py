"""Scope rules of the daily brief on one real Brain (spec Testing item 3)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.followup_contracts import (
    EvidenceRef,
    EvidenceRole,
    FollowupKind,
    OwnerKind,
    ProposeRequest,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from open_brain.services.brief import (
    BRIEF_GRANTS,
    brief_generate,
    brief_last,
    brief_state_dir,
)
from open_brain.services.brief_authority import brief_session_authority, visibility_key
from open_brain.services.brief_contracts import BriefItemKind
from open_brain.services.brief_log import BriefLogStore
from open_brain.services.followups import FOLLOWUP_GRANTS
from open_brain.services.t03_adapters import agent_authority, owner_authority
from packages.app.tests.unit.engine.test_followups import Clock, SyntheticSource, _revise_source

FROZEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
LATER = FROZEN + timedelta(days=10)
WORK_TEXT = "Synthetic work promise: send the deck"
PERSONAL_TEXT = "Synthetic personal promise: call the dentist"


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock(FROZEN)
        self.root = tmp_path / "brain"
        self.engine = BrainEngine.open(
            compile_single_user_local(self.root, starter_spaces=()), clock=self.clock
        )
        self.tasks = self.engine.tasks
        self.owner = owner_authority(self.tasks, session_id="owner-fixture")
        self.items: dict[PrivacyTier, str] = {}
        self.captures: dict[PrivacyTier, str] = {}
        for tier, text in ((PrivacyTier.WORK, WORK_TEXT), (PrivacyTier.PERSONAL, PERSONAL_TEXT)):
            capture_id = self.tasks.capture.accept(
                TextPayload(text), delivery_id=f"brief-{tier.value}", privacy_tier=tier
            ).capture_id
            start = text.index("promise")
            end = start + len("promise")
            ref = EvidenceRef(
                capture_id,
                capture_id,
                start,
                end,
                sha256(text[start:end].encode("utf-8")).hexdigest(),
                EvidenceRole.ORIGIN,
            )
            item = self.engine.followups.propose(
                ProposeRequest(
                    operation_id=f"brief-op-{tier.value}",
                    kind=FollowupKind.COMMITMENT,
                    summary=text,
                    project=None,
                    owner=OwnerKind.SELF,
                    owner_name=None,
                    due=None,
                    due_evidence_index=None,
                    evidence=(ref,),
                ),
                authority=self.owner,
            )
            self.items[tier] = item.item_id
            self.captures[tier] = capture_id
        # One capture family per tier through the source intake. A family is one connector
        # connection, so each tier gets its own connection_id (``_new_source`` shares one).
        for tier in (PrivacyTier.WORK, PrivacyTier.PERSONAL):
            source = SyntheticSource(
                namespace={
                    "connector_name": "synthetic",
                    "connection_id": f"brief-{tier.value}",
                    "resource_id": str(uuid4()),
                    "external_id": "one",
                },
                head="",
                revisions=0,
            )
            _revise_source(self.engine, source, f"Synthetic {tier.value} source note", tier=tier)
        # Ten days pass: both families are stale at the default seven days.
        self.clock.now = LATER
        self.log = BriefLogStore(brief_state_dir(self.tasks.profile), clock=self.clock)

    def owner_session(self) -> Any:
        return brief_session_authority(
            self.owner,
            grants=BRIEF_GRANTS | FOLLOWUP_GRANTS,
            launcher_policy=True,
            owner_cli=True,
        )

    def client_session(
        self,
        grants: frozenset[str],
        *,
        launcher_policy: bool = False,
        tiers: frozenset[PrivacyTier] | None = None,
        egress: EgressMode = EgressMode.OWNER_LOCAL,
    ) -> Any:
        authority = agent_authority(
            principal_id="open-brain-mcp-local", session_id="mcp-" + str(uuid4()), grants=grants
        )
        if tiers is not None:
            authority = replace(authority, allowed_read_tiers=tiers)
        if egress is not EgressMode.OWNER_LOCAL:
            authority = replace(
                authority,
                egress_mode=egress,
                provider_id="synthetic-provider",
                consent_id="consent_" + "0" * 32,
                brain_id="brn_" + "a" * 26,
                issuer_epoch=1,
            )
        return brief_session_authority(authority, grants=grants, launcher_policy=launcher_policy)

    def generate(self, session: Any, **arguments: Any) -> Any:
        return brief_generate(self.tasks, session, log=self.log, clock=self.clock, **arguments)


@pytest.fixture
def brain(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _refs(brief: Any) -> list[str]:
    return [item.ref for item in brief.items]


def test_sessions_differing_in_one_attribute_get_distinct_keys_and_separate_logs(
    brain: Fixture,
) -> None:
    read_only = frozenset({"brief-read"})
    with_followups = frozenset({"brief-read", "followup-read"})
    sessions = {
        "owner": brain.owner_session(),
        "client": brain.client_session(read_only),
        "client+followup-read": brain.client_session(with_followups),
        "client+ack": brain.client_session(frozenset({"brief-read", "brief-ack"})),
        "client personal tier": brain.client_session(
            read_only,
            launcher_policy=True,
            tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        ),
        "client external": brain.client_session(read_only, egress=EgressMode.EXTERNAL_PROVIDER),
    }
    keys = {
        name: visibility_key(session, selected_space_ids=None) for name, session in sessions.items()
    }
    assert len(set(keys.values())) == len(keys), keys
    assert visibility_key(sessions["client"], selected_space_ids=None) == keys["client"], "stable"
    # Two sessions, two files; neither reads the other's entries.
    first_owner = brain.generate(sessions["owner"])
    first_client = brain.generate(sessions["client"])
    assert first_owner.since is None and first_client.since is None
    briefs = brain.root / ".open-brain" / "state" / "briefs"
    assert {path.stem for path in briefs.glob("*.json")} == {keys["owner"], keys["client"]}
    brain.clock.now = LATER + timedelta(hours=1)
    second_client = brain.generate(sessions["client"])
    assert second_client.since == first_client.generated_at
    assert brain.generate(sessions["client+followup-read"]).since is None
    assert brain.log.read(keys["owner"]).entries == 1


def test_work_only_mcp_session_never_sees_personal_followups_or_families(brain: Fixture) -> None:
    owner_brief = brain.generate(brain.owner_session())
    owner_refs = _refs(owner_brief)
    assert "followup:" + brain.items[PrivacyTier.WORK] in owner_refs
    assert "followup:" + brain.items[PrivacyTier.PERSONAL] in owner_refs
    owner_families = {ref for ref in owner_refs if ref.startswith("family:")}
    assert len(owner_families) == 2 and owner_brief.coverage.families_checked == 2
    assert owner_brief.coverage.families_stale == 2
    assert owner_brief.scope.tiers == ("personal", "public", "work")

    recorded: list[EffectiveAuthority] = []
    real = brain.tasks.source_freshness

    class _Recording:
        def freshness(self, authority: EffectiveAuthority, **arguments: Any) -> Any:
            recorded.append(authority)
            return real.freshness(authority, **arguments)

    tasks = replace(brain.tasks, source_freshness=_Recording())
    session = brain.client_session(frozenset({"brief-read", "followup-read"}))
    client_brief = brief_generate(tasks, session, log=brain.log, clock=brain.clock)
    client_refs = _refs(client_brief)
    assert "followup:" + brain.items[PrivacyTier.WORK] in client_refs
    assert "followup:" + brain.items[PrivacyTier.PERSONAL] not in client_refs
    client_families = {ref for ref in client_refs if ref.startswith("family:")}
    assert len(client_families) == 1 and client_families < owner_families
    assert client_brief.coverage.families_checked == 1
    assert client_brief.scope.tiers == ("work",)
    assert client_brief.coverage.followups == "read" and client_brief.coverage.hidden_dropped == 0
    assert recorded and all(
        authority.owner is False and authority.allowed_read_tiers == frozenset({PrivacyTier.WORK})
        for authority in recorded
    ), "source_freshness ran under the clamped authority"
    assert brain.items[PrivacyTier.PERSONAL] not in json.dumps(client_brief.to_wire())
    assert PERSONAL_TEXT not in json.dumps(client_brief.to_wire())
    assert WORK_TEXT not in json.dumps(client_brief.to_wire()), "no summaries in a brief"

    # A launcher policy that allows Personal lets the same grants see both.
    policy_session = brain.client_session(
        frozenset({"brief-read", "followup-read"}),
        launcher_policy=True,
        tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
    )
    policy_refs = _refs(brain.generate(policy_session))
    assert "followup:" + brain.items[PrivacyTier.PERSONAL] in policy_refs


def test_session_without_followup_read_gets_family_items_only(brain: Fixture) -> None:
    brief = brain.generate(brain.client_session(frozenset({"brief-read"})))
    assert brief.coverage.followups == "not_granted" and brief.coverage.followups_scanned == 0
    assert all(item.kind is BriefItemKind.SOURCE_STALE for item in brief.items)
    assert len(brief.items) == 1


def test_non_owner_never_receives_withheld_or_hidden_refs_after_retraction(brain: Fixture) -> None:
    work_session = brain.client_session(frozenset({"brief-read", "followup-read"}))
    before = brain.generate(work_session)
    work_ref = "followup:" + brain.items[PrivacyTier.WORK]
    assert work_ref in _refs(before)
    owner_before = brain.generate(brain.owner_session())
    assert work_ref in _refs(owner_before)

    preview = brain.engine.lifecycle.preview_retraction(
        capture_id=brain.captures[PrivacyTier.WORK], authority=brain.owner
    )
    brain.engine.lifecycle.commit_retraction(
        str(preview["preview_id"]), str(uuid4()), authority=brain.owner
    )
    brain.clock.now = LATER + timedelta(hours=1)

    after = brain.generate(work_session)
    encoded = json.dumps(after.to_wire())
    assert work_ref not in _refs(after)
    assert "withheld" not in encoded and brain.items[PrivacyTier.WORK] not in encoded
    assert after.coverage.hidden_dropped == 0, "the engine already hides it at list time"

    # The owner's dated brief stays readable and marks the retracted item withheld.
    last: Any = brief_last(brain.tasks, brain.owner_session(), log=brain.log)
    assert last is not None and last["generated_at"] == owner_before.generated_at
    statuses = {item["ref"]: item["status"] for item in last["items"]}
    assert statuses[work_ref] == "withheld"
    assert statuses["followup:" + brain.items[PrivacyTier.PERSONAL]] == "current"
    assert all(status == "logged" for ref, status in statuses.items() if ref.startswith("family:"))
