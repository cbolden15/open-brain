"""Shell-agent intake calls the real owner engine without widening MCP grants."""

from __future__ import annotations

import io
import json
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, CaptureFault
from open_brain_engine.engine.consumer_contracts import OMISSION_KEYS
from open_brain_engine.engine.consumer_plans import (
    ConsumerConversationInput,
    ConsumerImportPlan,
    ConsumerMessageInput,
)
from open_brain_engine.engine.consumer_profiles import ConsumerProfileRecord

from open_brain.profile import compile_single_user_local
from open_brain.services.consumer_import import CLI_CONTRACT, MAX_INPUT_BYTES
from open_brain.services.local_entrypoints import _parser, run_cli


def _plan(provider: str = "chatgpt") -> ConsumerImportPlan:
    return ConsumerImportPlan(
        operation_id=str(uuid4()),
        provider=provider,
        profile_id=f"synthetic-{provider}",
        archive_sha256="a" * 64,
        new_scope_label="Synthetic account",
        conversations=(
            ConsumerConversationInput(
                provider_conversation_id="PRIVATE_SYNTHETIC_CONVERSATION",
                title="Synthetic title",
                messages=(
                    ConsumerMessageInput(
                        provider_message_id="message-1", role="user", text="PRIVATE_SYNTHETIC_BODY"
                    ),
                ),
                omissions=dict.fromkeys(OMISSION_KEYS, 0),
            ),
        ),
    )


def _bundle(plan: ConsumerImportPlan) -> dict[str, object]:
    return {
        "contract": CLI_CONTRACT,
        "plan": plan.value(),
        "objects": {key: json.loads(raw) for key, raw in plan.objects().items()},
    }


def _cli(root: Path, *args: str, payload: object = None) -> int:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
        return run_cli(
            ("consumer-import", *args, "--data-dir", str(root), "--json"),
            platform_name="darwin",
            filesystem_type_probe=lambda *_: "apfs",
        )


def _output(capsys: pytest.CaptureFixture[str], *, body: bool = False) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    if not body:
        assert "PRIVATE_SYNTHETIC" not in captured.out
    return cast(dict[str, object], json.loads(captured.out))


@pytest.fixture
def active_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "open_brain_engine.engine.consumer_profiles._ACTIVE_PROFILES",
        {
            f"synthetic-{p}": ConsumerProfileRecord(p, f"synthetic-{p}", "1" * 64)
            for p in ("chatgpt", "claude")
        },
    )


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
@pytest.mark.usefixtures("active_profiles")
def test_real_owner_preview_body_commit_status_and_deactivated_replay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    root, plan = tmp_path / "brain", _plan(provider)
    engine = BrainEngine.open(compile_single_user_local(root))
    bundle = _bundle(plan)
    assert _cli(root, "preview", payload=bundle) == 0
    preview = _output(capsys)
    identity = str(preview["preview_id"])
    assert _cli(root, "body", "--preview-id", identity, "--body-page", "0") == 0
    assert "PRIVATE_SYNTHETIC_BODY" in str(_output(capsys, body=True)["body"])
    assert _cli(root, "commit", "--preview-id", identity, "--confirm", payload=bundle) == 0
    receipt = _output(capsys)
    assert receipt["status"] == "committed"
    assert engine.retrieval.search("PRIVATE_SYNTHETIC_BODY")
    monkeypatch.setattr("open_brain_engine.engine.consumer_profiles._ACTIVE_PROFILES", {})
    assert _cli(root, "status", "--operation-id", plan.operation_id) == 0
    assert _output(capsys) == receipt
    assert _cli(root, "commit", "--preview-id", identity, "--confirm", payload=bundle) == 0
    assert _output(capsys) == receipt
    assert _cli(root, "recover", "--operation-id", plan.operation_id, "--confirm") == 0
    assert _output(capsys) == receipt
    assert _cli(root, "halt", "--operation-id", plan.operation_id, "--confirm") == 0
    assert _output(capsys) == receipt


@pytest.mark.parametrize("action", ["commit", "recover", "halt"])
def test_mutation_confirmation_is_required_before_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    args = (
        ("--preview-id", "chatprev_" + "A" * 43)
        if action == "commit"
        else (
            "--operation-id",
            str(uuid4()),
        )
    )
    root = tmp_path / "missing"
    assert _cli(root, action, *args, payload=_bundle(_plan())) == 2
    assert _output(capsys)["error"] == {
        "code": "invalid_arguments",
        "retryable": False,
        "item_token": None,
    }
    assert not root.exists()


@pytest.mark.usefixtures("active_profiles")
def test_committed_mixed_outcome_has_nonzero_exit_and_exact_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, plan = tmp_path / "brain", _plan()
    BrainEngine.open(compile_single_user_local(root))
    assert _cli(root, "preview", payload=_bundle(plan)) == 0
    preview = _output(capsys)
    assert (
        _cli(
            root,
            "commit",
            "--preview-id",
            str(preview["preview_id"]),
            "--confirm",
            payload=_bundle(plan),
        )
        == 0
    )
    _output(capsys)
    original = plan.conversations[0]
    changed = replace(original, messages=(replace(original.messages[0], text="changed body"),))
    new = replace(original, provider_conversation_id="new-conversation")
    mixed = replace(
        plan,
        operation_id=str(uuid4()),
        archive_sha256="b" * 64,
        scope_id=str(preview["account_scope_id"]),
        new_scope_label=None,
        conversations=(changed, new),
    )
    assert _cli(root, "preview", payload=_bundle(mixed)) == 0
    identity = str(_output(capsys)["preview_id"])
    assert _cli(root, "commit", "--preview-id", identity, "--confirm", payload=_bundle(mixed)) == 1
    receipt = _output(capsys)
    assert receipt["status"] == "committed"
    assert cast(dict[str, int], receipt["counts"])["conflict"] == 1
    assert cast(dict[str, int], receipt["counts"])["committed"] == 1
    assert _cli(root, "status", "--operation-id", mixed.operation_id) == 1
    assert _output(capsys) == receipt


@pytest.mark.parametrize(
    "bad",
    [
        b'{"contract":0,"contract":1}',
        b'{"a":NaN}',
        b"\xff",
        b"{}",
        b"{",
        b"[" * 1100 + b"]" * 1100,
        b" " * (MAX_INPUT_BYTES + 1),
    ],
)
def test_bad_input_is_bounded_and_refused_before_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bad: bytes,
) -> None:
    root = tmp_path / "missing"
    assert _cli(root, "preview", payload=bad) in {2, 78}
    assert _output(capsys)["status"] == "failed"
    assert not root.exists()


@pytest.mark.parametrize("change", ["authority", "orphan", "missing", "hash", "plan-extra"])
def test_no_supplied_authority_or_unvalidated_object_closure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    bundle = _bundle(_plan())
    objects = cast(dict[str, object], bundle["objects"])
    if change == "authority":
        bundle["authority"] = {"owner": True}
    elif change == "orphan":
        objects[sha256(b"{}").hexdigest()] = {}
    elif change == "missing":
        objects.pop(next(iter(objects)))
    elif change == "hash":
        objects[next(iter(objects))] = {}
    else:
        cast(dict[str, object], bundle["plan"])["session_id"] = "owner-supplied"
    assert _cli(tmp_path / "missing", "preview", payload=bundle) in {2, 78}
    assert _output(capsys)["status"] == "failed"
    assert not (tmp_path / "missing").exists()


def test_inactive_profile_refuses_without_canonical_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root))
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())
    assert _cli(root, "preview", payload=_bundle(_plan())) == 78
    assert _output(capsys)["error"] == {
        "code": "unsupported_profile",
        "retryable": False,
        "item_token": None,
    }
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("action", ["recover", "halt"])
@pytest.mark.usefixtures("active_profiles")
def test_interrupted_acceptance_can_be_inspected_then_explicitly_recovered_or_halted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    root, plan = tmp_path / "brain", _plan()
    engine = BrainEngine.open(compile_single_user_local(root))
    assert _cli(root, "preview", payload=_bundle(plan)) == 0
    identity = str(_output(capsys)["preview_id"])
    original = BrainEngine._fault

    def interrupt(self: BrainEngine, point: CaptureFault) -> None:
        if point is CaptureFault.AFTER_CONSUMER_INTENT:
            raise KeyboardInterrupt()
        original(self, point)

    with monkeypatch.context() as patch:
        patch.setattr(BrainEngine, "_fault", interrupt)
        assert (
            _cli(root, "commit", "--preview-id", identity, "--confirm", payload=_bundle(plan))
            == 130
        )
    assert _output(capsys)["operation_id"] == plan.operation_id
    assert _cli(root, "status", "--operation-id", plan.operation_id) == 0
    pending = _output(capsys)
    assert pending["status"] == "materializing"
    assert cast(dict[str, int], pending["counts"])["segments_materialized"] == 0
    assert not engine.retrieval.search("PRIVATE_SYNTHETIC_BODY")
    assert _cli(root, action, "--operation-id", plan.operation_id, "--confirm") == (
        0 if action == "recover" else 1
    )
    receipt = _output(capsys)
    assert receipt["status"] == ("committed" if action == "recover" else "halted")
    assert bool(engine.retrieval.search("PRIVATE_SYNTHETIC_BODY")) == (action == "recover")
    assert _cli(root, action, "--operation-id", plan.operation_id, "--confirm") == (
        0 if action == "recover" else 1
    )
    assert _output(capsys) == receipt


def test_exact_app_input_boundary_and_missing_brain_never_initialize(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = json.dumps(_bundle(_plan())).encode()
    raw += b" " * (MAX_INPUT_BYTES - len(raw))
    assert _cli(tmp_path / "missing", "preview", payload=raw) == 78
    assert _output(capsys)["error"] == {
        "code": "local_operation_failed",
        "retryable": False,
        "item_token": None,
    }
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("body", "--preview-id", "PRIVATE_SYNTHETIC", "--body-page", "0"),
        ("body", "--preview-id", "chatprev_" + "A" * 43, "--body-page", "-1"),
        ("status", "--operation-id", "PRIVATE_SYNTHETIC"),
    ],
)
def test_invalid_identifiers_refuse_before_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
) -> None:
    assert _cli(tmp_path / "missing", *args) == 2
    assert _output(capsys)["status"] == "failed"
    assert not (tmp_path / "missing").exists()


@pytest.mark.usefixtures("active_profiles")
def test_body_and_status_are_read_only_without_startup_recovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, plan = tmp_path / "brain", _plan()
    engine = BrainEngine.open(compile_single_user_local(root))
    assert _cli(root, "preview", payload=_bundle(plan)) == 0
    preview = _output(capsys)
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read command must not run recovery")

    monkeypatch.setattr(BrainEngine, "_recover", fail)
    monkeypatch.setattr(type(engine.consumer_imports), "recover_consumer_import", fail)
    assert _cli(root, "body", "--preview-id", str(preview["preview_id"]), "--body-page", "0") == 0
    _output(capsys, body=True)
    assert _cli(root, "status", "--operation-id", plan.operation_id) == 78
    assert cast(dict[str, object], _output(capsys)["error"])["code"] == "operation_not_found"
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before


def test_cli_discovery_adds_no_mcp_owner_capability() -> None:
    from open_brain.services.catalog import cli_registrations
    from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS

    commands = {tuple(cast(list[str], item["command"])) for item in cli_registrations(_parser())}
    assert {
        ("consumer-import", action)
        for action in (
            "preview",
            "body",
            "commit",
            "status",
            "recover",
            "halt",
        )
    } <= commands
    assert all("consumer" not in str(item["name"]) for item in MCP_REGISTERED_TOOLS)
