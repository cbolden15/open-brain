"""A redaction policy that tightens after admission keeps admitted history portable."""

import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.engine import TextPayload, local_schema
from open_brain_engine.engine import historical_admission as admission_v1
from open_brain_engine.engine import historical_admission_v2 as admission_v2
from open_brain_engine.engine.historical_tasks import link_historical_copy
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError

from open_brain.services.local_entrypoints import run_cli

from ._historical_compatibility_fixtures import baseline_v2, portable5_import_engine
from .test_historical_compatibility import linked_v2

TEXT = "Synthetic drift note for the public copy"


def _tighten(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """The current policy now finds what the admission-time policy did not."""
    for module in (admission_v1, admission_v2):
        monkeypatch.setattr(module, "has_redaction_finding", lambda value: text in value)


def _export_and_restore(tmp_path: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "export"
    capsys.readouterr()
    assert run_cli(("export", str(archive), "--verify", "--json", "--data-dir", str(root))) == 0
    exported = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert exported["verification"] == "verified"
    restored = tmp_path / "restored"
    assert (
        run_cli(("restore", str(archive), str(restored), "--import-id", f"import_{uuid4()}")) == 0
    )


def test_v2_copy_admitted_before_a_policy_change_exports_and_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch, text=TEXT)
    linked_v2(engine)
    _tighten(monkeypatch, TEXT)
    _export_and_restore(tmp_path, engine.profile.root, capsys)


def test_v2_new_copy_is_refused_under_the_tightened_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch, text=TEXT)
    relation, snapshot, owner = linked_v2(engine, perform_link=False)
    _tighten(monkeypatch, TEXT)
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="unsupported_capability"),
    ):
        link_historical_copy(
            engine.profile,
            relation,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: snapshot,
        )


def test_v1_copy_admitted_before_a_policy_change_exports_and_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from open_brain_engine.engine import BrainEngine

    from open_brain.profile import compile_single_user_local

    from .test_portable_v8_historical import _linked

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _linked(engine)
    _tighten(monkeypatch, "synthetic retained")
    assert relation.dto_version == 1
    _export_and_restore(tmp_path, engine.profile.root, capsys)


def test_v8_export_validates_a_v1_copy_admitted_before_a_policy_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import BrainEngine

    from open_brain.profile import compile_single_user_local

    from .test_portable_v8_historical import _linked

    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    _linked(engine)
    _tighten(monkeypatch, "synthetic retained")
    receipt = engine.portability.export(tmp_path / "export", export_id="export_" + str(uuid4()))
    assert receipt.schema_version == 8


@pytest.mark.parametrize("module", [admission_v1, admission_v2])
def test_only_admission_applies_the_current_redaction_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Any
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch, text=TEXT)
    baseline = baseline_v2(engine)
    original = baseline.observed_delivery.submission.capture
    privacy = original.privacy.to_dict()
    privacy["authority"] = {"cloud": False, "external_egress": True}
    copy = {"payload": TextPayload(TEXT).to_dict(), "privacy": privacy}
    _tighten(monkeypatch, TEXT)
    with pytest.raises(SharingError, match="unsupported_capability"):
        module.verify_historical_relation_payload(baseline, copy)
    module.verify_historical_relation_payload(baseline, copy, apply_redaction_policy=False)


@pytest.mark.parametrize("module", [admission_v1, admission_v2])
@pytest.mark.parametrize(
    ("break_copy", "error"),
    [
        (
            lambda copy: copy.update(payload=TextPayload(TEXT + " altered").to_dict()),
            "binding_mismatch",
        ),
        (
            lambda copy: copy["privacy"].update(
                authority={"cloud": False, "external_egress": False}
            ),
            "unsupported_capability",
        ),
    ],
    ids=["payload-mismatch", "no-external-egress"],
)
def test_skipping_the_redaction_policy_keeps_every_other_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    break_copy: Any,
    error: str,
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch, text=TEXT)
    baseline = baseline_v2(engine)
    privacy = baseline.observed_delivery.submission.capture.privacy.to_dict()
    privacy["authority"] = {"cloud": False, "external_egress": True}
    copy = {"payload": TextPayload(TEXT).to_dict(), "privacy": privacy}
    break_copy(copy)
    with pytest.raises(SharingError, match=error):
        module.verify_historical_relation_payload(baseline, copy, apply_redaction_policy=False)
