from __future__ import annotations

import json

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector.slack_policy import SlackPolicyStore
from open_brain_collector.sources_cli import main
from open_brain_connectors.runtime.live_common import LiveSourceError

_ACCOUNT = "account:" + "a" * 64
_PAGE = "page_11111111-1111-4111-8111-111111111111"


def test_private_slack_policy_defaults_suggestions_and_explicit_approval(tmp_path) -> None:
    store = SlackPolicyStore(tmp_path / "live")
    configured = store.setup(
        _ACCOUNT,
        {"keywords": ["Roadmap"], "proposal_opt_in": True, "allowlist": []},
    )
    assert configured == {
        "allowlisted_channels": 0,
        "connection_id": _ACCOUNT,
        "mapping_count": 0,
        "pending_suggestion_count": 0,
        "proposal_opt_in": True,
        "status": "configured",
    }
    store.record_discovery(
        _ACCOUNT,
        {"resumable": "synthetic"},
        (
            {
                "channel_id": "CROADMAP",
                "keyword_hits": 1,
                "message_count": 0,
                "name": "roadmap",
                "score": 20,
                "topic": None,
            },
        ),
        completed_at=None,
    )

    assert store.suggestions(_ACCOUNT)["suggestions"][0]["channel_id"] == "CROADMAP"
    assert store.approve_suggestion(_ACCOUNT, "CROADMAP")["status"] == "approved"
    assert store.policy(_ACCOUNT)["allowlist"] == ["CROADMAP"]
    assert store.suggestions(_ACCOUNT)["suggestions"] == []
    assert store.status() == {
        "allowlisted_channels": 1,
        "configured_accounts": 1,
        "mappings": 0,
        "pending_suggestions": 0,
    }


def test_mapping_validates_page_before_writing_and_supports_removal(tmp_path) -> None:
    store = SlackPolicyStore(tmp_path / "live")
    store.setup(_ACCOUNT, {"keywords": []})
    with pytest.raises(LiveSourceError, match="source_unknown_page"):
        store.add_mapping(_ACCOUNT, "CROADMAP", _PAGE, None, page_exists=lambda _page: False)
    assert store.mappings(_ACCOUNT)["mappings"] == []

    mapping = store.add_mapping(
        _ACCOUNT, "CROADMAP", _PAGE, "release", page_exists=lambda page: page == _PAGE
    )
    assert mapping["page_id"] == _PAGE and mapping["keyword"] == "release"
    removed = store.remove_mapping(_ACCOUNT, mapping["mapping_id"])
    assert removed["status"] == "removed"
    assert store.mappings(_ACCOUNT)["mappings"] == []


def test_sources_cli_exposes_policy_setup_and_status_counts(tmp_path, capsys) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    state = brain / "collector" / "state.json"
    assert (
        main(
            [
                "--state",
                str(state),
                "--brain-root",
                str(brain),
                "--foreground",
                "slack-policy-setup",
                "--connection-id",
                _ACCOUNT,
                "--continue-without-keywords",
                "--allow-channel",
                "CROADMAP",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["allowlisted_channels"] == 1
    assert (
        main(["--state", str(state), "--brain-root", str(brain), "--foreground", "slack-status"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["configured_accounts"] == 1
