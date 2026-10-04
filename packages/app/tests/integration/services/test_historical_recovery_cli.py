"""Owner maintenance dispatch precedes ordinary bootstrap and its writers."""

import json
from pathlib import Path

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_projection import verify_historical_projection
from open_brain_engine.engine.historical_tasks import link_historical_copy, revoke_historical_copy
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_runtime_session import hold_local_runtime_session
from open_brain.services.session_consent import DurableProviderConsentStore
from packages.app.tests.unit.engine.test_historical_link import _link_request
from packages.app.tests.unit.engine.test_historical_migration import _schema_twelve
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition
from packages.app.tests.unit.engine.test_historical_revocation import _request, _retained_relation


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


@pytest.mark.parametrize("consent_case", ["valid", "missing", "revoked", "wrong_epoch"])
def test_cli_pending_link_requires_existing_brain_bound_current_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    consent_case: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, consent = _link_request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(point: str) -> None:
        if point == "historical_relation_pending":
            raise RuntimeError("synthetic interruption")

    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(RuntimeError, match="synthetic interruption"),
    ):
        link_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: consent,
            checkpoint=crash,
        )
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    pending = fence.pending()
    assert pending is not None
    consent_dir = tmp_path / "consent"
    consent_dir.mkdir(mode=0o700)
    path = consent_dir / "state.json"
    store = DurableProviderConsentStore(
        path,
        brain_id=request.destination.brain_id,
        issuer_epoch=2 if consent_case == "wrong_epoch" else request.destination.issuer_epoch,
    )
    if consent_case != "missing":
        store.grant(
            provider_id="openai",
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="consent.synthetic",
            decided_at="2026-10-03T00:00:00Z",
            consent_id_factory=lambda: "consent_" + "a" * 32,
        )
        if consent_case == "revoked":
            store.revoke(
                consent_id="consent_" + "a" * 32,
                operation_id="consent.revoke",
                decided_at="2026-10-03T00:01:00Z",
            )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary startup invoked during maintenance")

    monkeypatch.setattr(BrainEngine, "open", forbidden)
    monkeypatch.setattr("open_brain.services.local_bootstrap.open_local_engine", forbidden)
    command = (
        "historical",
        "recover",
        "--data-dir",
        str(engine.profile.root),
        "--json",
        "--consent-state",
        str(path),
    )
    result = run_cli(command, filesystem_type_probe=_filesystem)
    response = json.loads(capsys.readouterr().out)
    if consent_case == "valid":
        assert result == 0
        assert response == {"status": "recovered", "receipt": pending.receipt.value()}
        assert fence.pending() is None
    else:
        assert result == 2
        assert response["error"]["code"] == "unsupported_capability"
        assert fence.pending() == pending
        connection = open_local_database_read_only(engine.profile)
        try:
            assert (
                connection.execute("SELECT COUNT(*) FROM historical_relations").fetchone()[0] == 0
            )
        finally:
            connection.close()


@pytest.mark.parametrize("stage", ["historical_revocation_pending", "historical_sql_committed"])
def test_cli_recovers_revocation_without_ordinary_engine_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _retained_relation(engine)
    request = _request(engine, relation)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(point: str) -> None:
        if point == stage:
            raise RuntimeError("synthetic interruption")

    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(RuntimeError, match="synthetic interruption"),
    ):
        revoke_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            checkpoint=crash,
        )
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    pending = fence.pending()
    assert pending is not None

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary startup invoked during maintenance")

    monkeypatch.setattr(BrainEngine, "open", forbidden)
    monkeypatch.setattr("open_brain.services.local_bootstrap.open_local_engine", forbidden)
    command = ("historical", "recover", "--data-dir", str(engine.profile.root), "--json")
    assert run_cli(command, filesystem_type_probe=_filesystem) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "recovered",
        "receipt": pending.receipt.value(),
    }
    assert run_cli(command, filesystem_type_probe=_filesystem) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "settled", "receipt": None}
    connection = open_local_database_read_only(engine.profile)
    try:
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM historical_claims").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM historical_revocations").fetchone()[0] == 1
    finally:
        connection.close()


def test_cli_recovers_pending_history_without_ordinary_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(profile.root, profile.root_identity)
    fence.prepare(record)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary startup invoked during maintenance")

    monkeypatch.setattr(BrainEngine, "open", forbidden)
    monkeypatch.setattr("open_brain.services.local_bootstrap.open_local_engine", forbidden)
    # Ordinary status must refuse before creating/opening engine tasks.
    assert (
        run_cli(
            ("status", "--data-dir", str(profile.root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "operation_pending"
    assert fence.pending() == record
    command = ("historical", "recover", "--data-dir", str(profile.root), "--json")
    assert run_cli(command, filesystem_type_probe=_filesystem) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "recovered",
        "receipt": record.receipt.value(),
    }
    assert run_cli(command, filesystem_type_probe=_filesystem) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "settled", "receipt": None}
    fence.assert_settled(record.proposed)
    connection = open_local_database_read_only(profile)
    try:
        assert verify_historical_projection(connection, profile, record.proposed) == (record,)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
    finally:
        connection.close()
    monkeypatch.undo()
    assert (
        run_cli(
            ("status", "--data-dir", str(profile.root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["profile"] == "local"


def test_cli_historical_recovery_refuses_live_peer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    with hold_local_runtime_session(
        engine.profile.root,
        engine.profile.root_identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: 0,
    ):
        assert (
            run_cli(
                ("historical", "recover", "--data-dir", str(engine.profile.root), "--json"),
                filesystem_type_probe=_filesystem,
            )
            == 1
        )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "operation_pending"
    assert fence.pending() == record


def test_cli_historical_recovery_does_not_create_a_missing_brain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "missing"
    assert (
        run_cli(
            ("historical", "recover", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    capsys.readouterr()
    assert not root.exists()


def test_cli_historical_recovery_refuses_old_state_before_runtime_registry_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _schema_twelve(tmp_path, monkeypatch)
    sessions = profile.root / ".open-brain/runtime-sessions"
    assert not sessions.exists()
    assert (
        run_cli(
            ("historical", "recover", "--data-dir", str(profile.root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "binding_mismatch"
    assert not sessions.exists()


@pytest.mark.parametrize("argument", ["--operation-id", "--owner", "--request-json"])
def test_cli_historical_recovery_accepts_no_replacement_intent_or_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argument: str
) -> None:
    root = tmp_path / "missing"
    assert (
        run_cli(
            ("historical", "recover", "--data-dir", str(root), "--json", argument, "synthetic"),
            filesystem_type_probe=_filesystem,
        )
        == 2
    )
    capsys.readouterr()
    assert not root.exists()


def test_ordinary_cli_refuses_missing_historical_registry_before_engine_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    registry = engine.profile.root / ".open-brain/historical-authority/historical-claims.v1.json"
    registry.unlink()  # Synthetic store only: model loss of mandatory evidence.

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary startup ran with missing authority")

    monkeypatch.setattr("open_brain.services.local_bootstrap.open_local_engine", forbidden)
    assert (
        run_cli(
            ("status", "--data-dir", str(engine.profile.root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "failed"
    assert refused["error"]["code"] == "binding_mismatch"
