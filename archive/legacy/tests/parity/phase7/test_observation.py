from __future__ import annotations

import json
from copy import copy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import ReviewId, canonical_json_bytes
from open_brain_engine.core.models import Intent
from open_brain_engine.core.policy import IntentPolicyReason

from open_brain_legacy.cli.doctor import show_doctor
from open_brain_legacy.operations.doctor import (
    DoctorResult,
    DoctorRole,
    ProbeName,
    ProbeReading,
    ProbeState,
    run_doctor,
)
from open_brain_legacy.parity.observation import (
    OPEN_BRAIN_CLI_PROFILE_FIELDS,
    NativePhase7ObservationPort,
    ObservationValidationError,
    OpenBrainCliProfile,
    Phase7ObservationPort,
    SyntheticPhase7Scenario,
    digest_cli_profile_fields,
    observe_open_brain_cli,
    observe_open_brain_doctor,
    observe_routing_result,
)
from open_brain_legacy.review.routing import (
    IntentRoutingDestination,
    IntentRoutingResult,
    IntentRoutingStatus,
)

_FIXTURES = Path(__file__).with_name("capture_scenarios.json")


def _status_envelope() -> dict[str, object]:
    return {
        "command": "status",
        "metrics": [
            {
                "metric": "queue_age_seconds",
                "observed_at": None,
                "state": "available",
                "unavailable_class": None,
                "unit": "seconds",
                "value": 0,
            }
        ],
        "schema_version": 1,
        "status": "complete",
        "strict": False,
    }


def _cron_envelope() -> dict[str, object]:
    return {
        "command": "cron",
        "run_count": 0,
        "runs": [],
        "status": "reported",
        "window_seconds": 86_400,
    }


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _changed_value(field: str, current: object) -> object:
    if field == "command":
        return f"{current}-changed"
    if field == "status":
        return f"{current}-changed"
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        return current + 1
    if isinstance(current, list):
        return [*current, {"changed": True}]
    raise AssertionError(field)


def _doctor_result() -> DoctorResult:
    return run_doctor(
        role=DoctorRole.PROBE,
        probes={ProbeName.OPTIONAL_PROVIDER: lambda _timeout: ProbeReading.unhealthy()},
        timeout_seconds=1.0,
        strict=True,
    )


def _clone_doctor_result(
    result: DoctorResult,
    *,
    checks: tuple[object, ...] | None = None,
    findings: tuple[object, ...] | None = None,
) -> DoctorResult:
    cloned = copy(result)
    if checks is not None:
        object.__setattr__(cloned, "checks", checks)
    if findings is not None:
        object.__setattr__(cloned, "findings", findings)
    return cloned


def _scenarios() -> list[SyntheticPhase7Scenario]:
    value = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return [SyntheticPhase7Scenario.from_mapping(cast(dict[str, object], item)) for item in value]


def test_cli_observation_requires_the_exact_status_and_cron_profiles() -> None:
    status = observe_open_brain_cli(
        profile=OpenBrainCliProfile.STATUS,
        stdout=_json(_status_envelope()),
        exit_code=0,
    )
    cron = observe_open_brain_cli(
        profile=OpenBrainCliProfile.CRON,
        stdout=_json(_cron_envelope()),
        exit_code=0,
    )

    assert tuple(item.field for item in status.field_digests) == (
        "command",
        "metrics",
        "schema_version",
        "status",
        "strict",
    )
    assert tuple(item.field for item in cron.field_digests) == (
        "command",
        "run_count",
        "runs",
        "status",
        "window_seconds",
    )
    assert status.field_digests[1].digest_sha256 == sha256(
        canonical_json_bytes(_status_envelope()["metrics"])
    ).hexdigest()


@pytest.mark.parametrize("mutation", ["add", "remove", "rename"])
def test_cli_observation_rejects_top_level_key_mutations(mutation: str) -> None:
    envelope = _status_envelope()
    if mutation == "add":
        envelope["extra"] = True
    elif mutation == "remove":
        del envelope["strict"]
    else:
        envelope["strict_mode"] = envelope.pop("strict")

    with pytest.raises(ObservationValidationError, match="CLI profile fields"):
        observe_open_brain_cli(
            profile=OpenBrainCliProfile.STATUS,
            stdout=_json(envelope),
            exit_code=0,
        )


def test_cli_observation_rejects_duplicate_non_object_extra_and_wrong_profile_output() -> None:
    invalid = (
        '{"command":"status","command":"status","metrics":[],"schema_version":1,'
        '"status":"complete","strict":false}',
        "[]",
        _json(_status_envelope()) + _json(_status_envelope()),
    )
    for stdout in invalid:
        with pytest.raises(ObservationValidationError):
            observe_open_brain_cli(
                profile=OpenBrainCliProfile.STATUS,
                stdout=stdout,
                exit_code=0,
            )

    with pytest.raises(ObservationValidationError, match="CLI profile fields"):
        observe_open_brain_cli(
            profile=OpenBrainCliProfile.CRON,
            stdout=_json(_status_envelope()),
            exit_code=0,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("command", "cron"), ("status", "failed"), ("schema_version", 2)],
)
def test_cli_observation_rejects_unsupported_profile_values(field: str, value: object) -> None:
    envelope = _status_envelope()
    envelope[field] = value

    with pytest.raises(ObservationValidationError, match="CLI profile value"):
        observe_open_brain_cli(
            profile=OpenBrainCliProfile.STATUS,
            stdout=_json(envelope),
            exit_code=0,
        )

    with pytest.raises(ObservationValidationError, match="CLI exit"):
        observe_open_brain_cli(
            profile=OpenBrainCliProfile.STATUS,
            stdout=_json(_status_envelope()),
            exit_code=1,
        )


def test_every_exact_cli_profile_field_changes_its_digest() -> None:
    for profile, envelope in (
        (OpenBrainCliProfile.STATUS, _status_envelope()),
        (OpenBrainCliProfile.CRON, _cron_envelope()),
    ):
        baseline = dict(digest_cli_profile_fields(profile=profile, envelope=envelope))
        assert tuple(baseline) == OPEN_BRAIN_CLI_PROFILE_FIELDS[profile]
        for field in OPEN_BRAIN_CLI_PROFILE_FIELDS[profile]:
            changed = dict(envelope)
            changed[field] = _changed_value(field, envelope[field])
            mutated = dict(digest_cli_profile_fields(profile=profile, envelope=changed))
            assert mutated[field] != baseline[field]

    nested = _status_envelope()
    metrics = cast(list[dict[str, object]], nested["metrics"])
    nested["metrics"] = [{**metrics[0], "value": 99}]
    assert dict(
        digest_cli_profile_fields(profile=OpenBrainCliProfile.STATUS, envelope=nested)
    )["metrics"] != dict(
        digest_cli_profile_fields(
            profile=OpenBrainCliProfile.STATUS, envelope=_status_envelope()
        )
    )["metrics"]


def test_doctor_observation_requires_all_eight_checks_and_exact_cli_envelope() -> None:
    result = _doctor_result()
    observation = observe_open_brain_doctor(result=result, cli_result=show_doctor(result=result))

    assert tuple(check.probe for check in observation.checks) == tuple(ProbeName)
    assert len(observation.findings) == 8
    assert observation.findings[-1].probe is ProbeName.OPTIONAL_PROVIDER
    assert observation.findings[-1].state is ProbeState.UNHEALTHY


@pytest.mark.parametrize("mutation", ["add", "remove", "duplicate", "alter"])
def test_doctor_observation_rejects_native_finding_mutations(mutation: str) -> None:
    result = _doctor_result()
    findings = list(result.findings)
    checks = list(result.checks)
    if mutation == "add":
        checks.append(checks[0])
        findings.append(findings[0])
    elif mutation == "remove":
        findings.pop()
    elif mutation == "duplicate":
        findings[-1] = findings[0]
    else:
        findings[-1] = replace(findings[-1], state=ProbeState.HEALTHY)
    mutated = _clone_doctor_result(
        result,
        checks=cast(tuple[object, ...], tuple(checks)),
        findings=cast(tuple[object, ...], tuple(findings)),
    )

    with pytest.raises(ObservationValidationError, match="doctor"):
        observe_open_brain_doctor(result=mutated, cli_result=show_doctor(result=mutated))


@pytest.mark.parametrize("mutation", ["add", "remove", "duplicate", "alter"])
def test_doctor_observation_rejects_cli_envelope_finding_mutations(mutation: str) -> None:
    result = _doctor_result()
    cli_result = show_doctor(result=result)
    envelope = dict(cli_result.envelope)
    findings = cast(list[dict[str, object]], envelope["findings"])
    findings = [dict(item) for item in findings]
    if mutation == "add":
        findings.append({**findings[0], "probe": "unknown"})
    elif mutation == "remove":
        findings.pop()
    elif mutation == "duplicate":
        findings[-1] = dict(findings[0])
    else:
        findings[-1]["state"] = "healthy"
    envelope["findings"] = findings
    altered_cli = replace(cli_result, envelope=envelope)

    with pytest.raises(ObservationValidationError, match="doctor CLI envelope"):
        observe_open_brain_doctor(result=result, cli_result=altered_cli)


@pytest.mark.parametrize(
    ("intent", "status", "destination", "review_id"),
    [
        (Intent.HOLD, IntentRoutingStatus.HELD, IntentRoutingDestination.HOLD, None),
        (
            Intent.REFERENCE,
            IntentRoutingStatus.REFERENCE_APPLIED,
            IntentRoutingDestination.WORK,
            None,
        ),
        (
            Intent.REFERENCE,
            IntentRoutingStatus.REFERENCE_APPLIED,
            IntentRoutingDestination.PERSONAL,
            None,
        ),
        (
            Intent.IDEA,
            IntentRoutingStatus.REVIEW_OPEN,
            IntentRoutingDestination.REVIEW,
            "review_" + "a" * 64,
        ),
        (
            Intent.ACTION_CANDIDATE,
            IntentRoutingStatus.REVIEW_OPEN,
            IntentRoutingDestination.REVIEW,
            "review_" + "b" * 64,
        ),
    ],
)
def test_routing_observation_accepts_only_consistent_native_results(
    intent: Intent,
    status: IntentRoutingStatus,
    destination: IntentRoutingDestination,
    review_id: str | None,
) -> None:
    result = IntentRoutingResult(
        intent=intent,
        reason=IntentPolicyReason.PROPOSAL_ACCEPTED,
        status=status,
        destination=destination,
        review_id=None if review_id is None else ReviewId(review_id),
    )

    assert observe_routing_result(result).destination is destination

    mutated = replace(
        result,
        destination=(
            IntentRoutingDestination.REVIEW
            if destination is not IntentRoutingDestination.REVIEW
            else IntentRoutingDestination.HOLD
        ),
    )
    with pytest.raises(ObservationValidationError, match="routing observation"):
        observe_routing_result(mutated)


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda item: item.name)
def test_native_observation_port_uses_synthetic_services_and_keeps_status_unavailable(
    scenario: SyntheticPhase7Scenario, tmp_path: Path
) -> None:
    port: Phase7ObservationPort = NativePhase7ObservationPort()

    observation = port.observe(scenario=scenario, execution_root=tmp_path)

    assert observation.request.request_status is None
    assert observation.request.native_lifecycle_state == "acknowledged"
    assert observation.routing.destination.value == (
        "personal"
        if scenario.name == "saved_web_reference"
        else "work"
        if scenario.name == "social_reference"
        else "review"
        if scenario.name in {"idea_candidate", "third_party_action_candidate"}
        else "hold"
    )
    assert tuple(check.probe for check in observation.doctor.checks) == tuple(ProbeName)
    assert len(observation.cli.field_digests) == len(
        OPEN_BRAIN_CLI_PROFILE_FIELDS[observation.cli.profile]
    )
    with pytest.raises(FrozenInstanceError):
        observation.routing.destination = IntentRoutingDestination.HOLD  # type: ignore[misc]
