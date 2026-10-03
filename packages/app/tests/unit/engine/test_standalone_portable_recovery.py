"""Owner-local recovery must not bootstrap a second Brain to obtain tooling."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.engine import BrainEngine, PortabilityFault
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.portability import restore_portable_clean
from open_brain_engine.engine.source_lifecycle_contracts import SourceInspectRequest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain import profile as profile_module
from open_brain.profile import compile_single_user_local
from open_brain.services import local_entrypoints
from packages.app.tests.unit.engine.test_historical_continuity import _adopt


def test_standalone_restore_preserves_identity_without_primary_or_new_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    engine = BrainEngine.open(compile_single_user_local(primary))
    tenant_id = engine.profile.tenant_id
    baseline = _adopt(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "local", frozenset(), None, owner=True,
    )
    inspection = engine.sources.inspect(
        SourceInspectRequest(source_id=baseline.source_cas.source_id), authority=owner,
    )
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(archive)
    primary.rename(tmp_path / "unavailable-primary")
    assert not primary.exists()
    del engine

    def forbid_identity() -> dict[str, object]:
        raise AssertionError("recovery must not create a caller Brain identity")

    monkeypatch.setattr(profile_module, "_new_identity", forbid_identity)
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    result = restore_portable_clean(archive, destination, import_id=import_id, authority=owner)
    assert result.schema_version == 8 and not result.duplicate
    assert restore_portable_clean(
        archive, destination, import_id=import_id, authority=owner,
    ).duplicate
    assert not primary.exists()
    assert validated_portable_snapshot(destination).files == snapshot.files
    restored = BrainEngine.open(compile_single_user_local(destination))
    assert restored.sources.inspect(
        SourceInspectRequest(source_id=baseline.source_cas.source_id), authority=owner,
    ) == inspection
    assert restored.profile.tenant_id == tenant_id


def test_standalone_restore_denies_delegated_authority_before_filesystem_mutation(
    tmp_path: Path,
) -> None:
    before = tuple(tmp_path.iterdir())
    delegated = EffectiveAuthority("delegated", "session", frozenset({"admin"}), None)
    with pytest.raises(T03Error, match="unsupported_capability"):
        restore_portable_clean(
            tmp_path / "missing-source", tmp_path / "destination",
            import_id="import_" + str(uuid4()), authority=delegated,
        )
    assert tuple(tmp_path.iterdir()) == before


def test_restore_cli_runs_without_primary_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    primary = tmp_path / "primary"
    engine = BrainEngine.open(compile_single_user_local(primary))
    _adopt(engine)
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    primary.rename(tmp_path / "unavailable-primary")
    del engine

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("standalone CLI must not select or bootstrap a Brain")

    for name in ("select_local_root", "open_local_brain", "initialize_local_brain"):
        monkeypatch.setattr(local_entrypoints, name, forbidden)
    monkeypatch.setattr(profile_module, "_new_identity", forbidden)
    destination = tmp_path / "restored"
    arguments = (
        "restore", str(archive), str(destination),
        "--import-id", "import_" + str(uuid4()), "--json",
    )
    assert local_entrypoints.run_cli(arguments, environment={}) == 0
    assert not json.loads(capsys.readouterr().out)["duplicate"]
    assert local_entrypoints.run_cli(arguments, environment={}) == 0
    assert json.loads(capsys.readouterr().out)["duplicate"]
    assert not primary.exists()


def test_restore_cli_redacts_invalid_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "sensitive-missing-archive"
    destination = tmp_path / "restored"
    assert local_entrypoints.run_cli((
        "restore", str(missing), str(destination),
        "--import-id", "import_" + str(uuid4()), "--json",
    ), environment={}) == 78
    output = capsys.readouterr().out
    assert "sensitive-missing-archive" not in output
    assert json.loads(output)["error"]["code"] == "portable_restore_failed"
    assert not destination.exists()


@pytest.mark.parametrize("fault", tuple(PortabilityFault))
def test_standalone_restore_faults_keep_archive_and_retry_exactly(
    tmp_path: Path, fault: PortabilityFault,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    _adopt(engine)
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(archive)
    owner = EffectiveAuthority("local-owner", "restore", frozenset(), None, owner=True)
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())

    def interrupt(checkpoint: PortabilityFault) -> None:
        if checkpoint is fault:
            raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        restore_portable_clean(
            archive, destination, import_id=import_id, authority=owner, checkpoint=interrupt,
        )
    assert destination.exists() is (fault is PortabilityFault.AFTER_PROMOTION)
    assert validated_portable_snapshot(archive).files == snapshot.files
    receipt = restore_portable_clean(archive, destination, import_id=import_id, authority=owner)
    assert receipt.duplicate is (fault is PortabilityFault.AFTER_PROMOTION)
    assert validated_portable_snapshot(destination).files == snapshot.files


def test_standalone_restore_denies_external_owner_before_filesystem_access(
    tmp_path: Path,
) -> None:
    authority = EffectiveAuthority(
        "external-owner", "session", frozenset(), None, owner=True,
        egress_mode=EgressMode.EXTERNAL_PROVIDER, provider_id="synthetic",
        consent_id="consent_" + "0" * 32,
        brain_id=derive_brain_id("tenant_123e4567-e89b-42d3-a456-426614174000"),
        issuer_epoch=1,
    )
    with pytest.raises(T03Error, match="unsupported_capability"):
        restore_portable_clean(
            tmp_path / "missing", tmp_path / "restored",
            import_id="import_" + str(uuid4()), authority=authority,
        )
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("source_kind", ("relative", "symlink", "public"))
def test_standalone_restore_refuses_unsafe_source_before_destination_mutation(
    tmp_path: Path, source_kind: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    if source_kind == "relative":
        source = Path("relative")
    elif source_kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(source, target_is_directory=True)
        source = link
    else:
        source.chmod(0o755)
    owner = EffectiveAuthority("local-owner", "restore", frozenset(), None, owner=True)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError):
        restore_portable_clean(
            source, destination, import_id="import_" + str(uuid4()), authority=owner,
        )
    assert not destination.exists()


def test_standalone_restore_keeps_frozen_v5_identity(tmp_path: Path) -> None:
    from packages.engine.tests.contract.test_portable_brain_v5 import _write
    from packages.engine.tests.unit.engine.test_portable_v5_restore import _fixture

    archive = tmp_path / "archive"
    archive.mkdir(mode=0o700)
    _write(archive, _fixture())
    snapshot = validated_portable_snapshot(archive)
    owner = EffectiveAuthority("local-owner", "restore", frozenset(), None, owner=True)
    destination = tmp_path / "restored"
    receipt = restore_portable_clean(
        archive, destination, import_id="import_" + str(uuid4()), authority=owner,
    )
    assert receipt.schema_version == 5
    assert validated_portable_snapshot(destination).files == snapshot.files


def test_standalone_restore_refuses_v1_without_generating_issuer(tmp_path: Path) -> None:
    from packages.engine.tests.contract.test_portable_brain_v1 import _root

    archive = _root(tmp_path)
    archive.chmod(0o700)
    assert validated_portable_snapshot(archive).manifest["schema_version"] == 1
    destination = tmp_path / "restored"
    owner = EffectiveAuthority("local-owner", "restore", frozenset(), None, owner=True)
    with pytest.raises(ValueError, match="retained issuer evidence"):
        restore_portable_clean(
            archive, destination, import_id="import_" + str(uuid4()), authority=owner,
        )
    assert not destination.exists()


def test_standalone_restore_refuses_conflicting_destination_without_overwrite(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    _adopt(engine)
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    destination = tmp_path / "existing"
    destination.mkdir(mode=0o700)
    sentinel = destination / "retained.txt"
    sentinel.write_bytes(b"Retained unrelated content")
    owner = EffectiveAuthority("local-owner", "restore", frozenset(), None, owner=True)
    with pytest.raises(ValueError):
        restore_portable_clean(
            archive, destination, import_id="import_" + str(uuid4()), authority=owner,
        )
    assert sentinel.read_bytes() == b"Retained unrelated content"
    assert tuple(destination.iterdir()) == (sentinel,)


@pytest.mark.parametrize("fault", (
    PortabilityFault.AFTER_STAGE_CREATED,
    PortabilityFault.BEFORE_PROMOTION,
    PortabilityFault.AFTER_PROMOTION,
))
def test_standalone_restore_survives_process_exit_without_cleanup(
    tmp_path: Path, fault: PortabilityFault,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    _adopt(engine)
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(archive)
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    program = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from open_brain_engine.engine.portability import restore_portable_clean\n"
        "from open_brain_engine.engine.t03_contracts import EffectiveAuthority\n"
        "def checkpoint(fault):\n"
        "    if fault.value == sys.argv[4]: os._exit(23)\n"
        "restore_portable_clean(Path(sys.argv[1]), Path(sys.argv[2]), "
        "import_id=sys.argv[3], authority=EffectiveAuthority("
        "'local-owner', 'restore', frozenset(), None, owner=True), checkpoint=checkpoint)\n"
    )
    result = subprocess.run(
        (sys.executable, "-c", program, str(archive), str(destination), import_id, fault.value),
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 23, result.stderr.decode("utf-8", errors="replace")
    assert destination.exists() is (fault is PortabilityFault.AFTER_PROMOTION)
    assert validated_portable_snapshot(archive).files == snapshot.files
    owner = EffectiveAuthority("local-owner", "restore", frozenset(), None, owner=True)
    receipt = restore_portable_clean(archive, destination, import_id=import_id, authority=owner)
    assert receipt.duplicate is (fault is PortabilityFault.AFTER_PROMOTION)
    assert validated_portable_snapshot(destination).files == snapshot.files
