"""The export-records CLI: JSONL of head captures under a consented external-provider policy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.session_consent import DurableProviderConsentStore
from packages.app.tests.unit.engine.test_record_export import _capture

CONSENT_ID = "consent_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PROVIDER = "synthetic-newsletter"


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _brain(tmp_path: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain"))


def _authority_files(tmp_path: Path, engine: BrainEngine, **overrides: Any) -> tuple[Path, Path]:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    brain_id, issuer_epoch = current_brain_identity(engine.tasks)
    consent_path = root / "consent.json"
    DurableProviderConsentStore(consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch).grant(
        provider_id=PROVIDER,
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="synthetic-newsletter-grant",
        decided_at="2026-10-30T12:00:00Z",
        consent_id_factory=lambda: CONSENT_ID,
    )
    document = {
        "policy_version": "launcher-policy.v1",
        "principal_id": "synthetic-newsletter-principal",
        "session_id": "synthetic-newsletter-session",
        "capabilities": ["content-read", "search"],
        "space_ids": None,
        "allowed_read_tiers": ["public", "work"],
        "allowed_capture_tiers": [],
        "egress_mode": "external_provider",
        "provider_id": PROVIDER,
        "consent_id": CONSENT_ID,
        "authorization_generation": 1,
        "brain_id": brain_id,
        "issuer_epoch": issuer_epoch,
    } | overrides
    policy_path = root / "newsletter-policy.json"
    policy_path.write_text(json.dumps(document), encoding="utf-8")
    policy_path.chmod(0o600)
    return policy_path, consent_path


def _since() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).date().isoformat()


def _run(
    capsys: pytest.CaptureFixture[str], engine: BrainEngine, *arguments: str
) -> tuple[int, str, str]:
    code = run_cli(
        ("export-records", "--data-dir", str(engine.profile.root), *arguments),
        filesystem_type_probe=_filesystem,
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_export_records_writes_jsonl_for_consented_tiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _brain(tmp_path)
    shared = _capture(engine, delivery_id="cli.shared", text="synthetic shared work")
    _capture(engine, delivery_id="cli.local", text="synthetic local work", external_egress=False)
    policy, consent = _authority_files(tmp_path, engine)
    code, out, err = _run(
        capsys,
        engine,
        "--policy",
        str(policy),
        "--consent-state",
        str(consent),
        "--since",
        _since(),
    )
    assert (code, err) == (0, "")
    lines = [json.loads(line) for line in out.splitlines()]
    assert [line["id"] for line in lines] == [shared]
    assert lines[0]["privacy_tier"] == "work"
    assert lines[0]["text"] == "synthetic shared work"
    assert "private-reference" not in out


def test_export_records_with_nothing_in_the_window_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _brain(tmp_path)
    _capture(engine, delivery_id="cli.old", text="synthetic shared work")
    policy, consent = _authority_files(tmp_path, engine)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    code, out, err = _run(
        capsys,
        engine,
        "--policy",
        str(policy),
        "--consent-state",
        str(consent),
        "--since",
        tomorrow,
        "--until",
        (datetime.now(UTC) + timedelta(days=2)).date().isoformat(),
    )
    assert (code, out, err) == (0, "", "")


def test_export_records_requires_a_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _brain(tmp_path)
    code, out, _err = _run(capsys, engine, "--since", _since(), "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "invalid_command"


@pytest.mark.parametrize("since", ["2026-13-01", "yesterday", "2026-10-31T00:00:00Z", ""])
def test_export_records_rejects_a_malformed_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], since: str
) -> None:
    engine = _brain(tmp_path)
    policy, consent = _authority_files(tmp_path, engine)
    code, out, _err = _run(
        capsys,
        engine,
        "--policy",
        str(policy),
        "--consent-state",
        str(consent),
        "--since",
        since,
        "--json",
    )
    assert code == 2
    assert json.loads(out)["error"]["code"] == "invalid_command"


@pytest.mark.parametrize(
    ("overrides", "with_consent", "error_code"),
    [
        ({"brain_id": "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa"}, True, "destination_mismatch"),
        ({"space_ids": []}, True, "invalid_policy"),
        ({}, False, "consent_unavailable"),
        (
            {
                "egress_mode": "owner_local",
                "provider_id": None,
                "consent_id": None,
                "authorization_generation": 0,
            },
            False,
            "invalid_policy",
        ),
    ],
)
def test_export_records_refuses_policies_that_do_not_authorize_external_export(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, Any],
    with_consent: bool,
    error_code: str,
) -> None:
    engine = _brain(tmp_path)
    _capture(engine, delivery_id="cli.refused", text="synthetic shared work")
    policy, consent = _authority_files(tmp_path, engine, **overrides)
    consent_arguments = ("--consent-state", str(consent)) if with_consent else ()
    code, out, _err = _run(
        capsys, engine, "--policy", str(policy), *consent_arguments, "--since", _since(), "--json"
    )
    assert code == 78
    assert json.loads(out)["error"]["code"] == error_code
    assert "synthetic shared work" not in out


def test_export_records_fails_closed_when_the_limit_is_exceeded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _brain(tmp_path)
    for index in range(2):
        _capture(engine, delivery_id=f"cli.limit.{index}", text=f"synthetic limit {index}")
    policy, consent = _authority_files(tmp_path, engine)
    code, out, _err = _run(
        capsys,
        engine,
        "--policy",
        str(policy),
        "--consent-state",
        str(consent),
        "--since",
        _since(),
        "--limit",
        "1",
        "--json",
    )
    assert code == 1
    assert json.loads(out)["error"]["code"] == "response_too_large"
    assert "synthetic limit" not in out
