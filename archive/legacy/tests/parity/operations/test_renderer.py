import plistlib
import shlex
from configparser import ConfigParser
from dataclasses import replace

import pytest

import open_brain_legacy.operations as operations
from open_brain_legacy.operations.catalog import JOB_CATALOG, get_job
from open_brain_legacy.operations.models import TriggerKind
from open_brain_legacy.operations.render import (
    ManifestValidationError,
    render_launchd,
    render_manifest,
    render_systemd_service,
    render_systemd_timer,
    validate_rendered_manifest,
)
from open_brain_legacy.operations.scheduler import SchedulerPlatform


def test_launchd_renderer_is_deterministic_and_uses_argv() -> None:
    job = get_job("JOB-001")

    first = render_launchd(job)
    second = render_launchd(job)
    payload = plistlib.loads(first.encode("utf-8"))

    assert first == second
    assert payload["Label"] == "org.open-brain.job-001"
    assert payload["ProgramArguments"] == list(job.command)
    assert payload["StartInterval"] == 3600
    assert payload["RunAtLoad"] is False
    assert payload["Disabled"] is False
    assert payload["Umask"] == 0o077
    assert payload["StandardOutPath"] == "<LOG_DIRECTORY>/JOB-001.stdout.log"
    assert payload["StandardErrorPath"] == "<LOG_DIRECTORY>/JOB-001.stderr.log"
    assert payload["WorkingDirectory"] == "<WORKING_DIRECTORY>"
    assert payload["EnvironmentVariables"] == {
        "OPEN_BRAIN_ENV_REFS": "",
        "OPEN_BRAIN_DEPLOYMENT_TARGET": "edge-operator",
        "OPEN_BRAIN_HOST_ROLE": "probe",
        "OPEN_BRAIN_JOB_ID": "JOB-001",
        "OPEN_BRAIN_JOB_STATE": "enabled",
        "OPEN_BRAIN_LOCK_SCOPE": "none",
        "OPEN_BRAIN_OUTPUT_POLICY": "metadata-only",
        "OPEN_BRAIN_RETRY_POLICY": "never",
        "OPEN_BRAIN_RUN_AT_LOAD": "false",
        "OPEN_BRAIN_SCHEDULER_PLATFORMS": "launchd",
        "OPEN_BRAIN_TIMEOUT_SECONDS": "300",
        "OPEN_BRAIN_TRIGGER_KIND": "interval",
        "OPEN_BRAIN_TRIGGER_PERSISTENT": "false",
        "OPEN_BRAIN_WRITER_SCOPE": "none",
    }


def test_launchd_renderer_maps_calendar_and_keepalive_triggers() -> None:
    weekly = plistlib.loads(render_launchd(get_job("JOB-008")).encode("utf-8"))
    keepalive = plistlib.loads(render_launchd(get_job("JOB-026")).encode("utf-8"))

    assert weekly["StartCalendarInterval"] == {"Hour": 4, "Minute": 0, "Weekday": 1}
    assert "StartInterval" not in weekly
    assert keepalive["KeepAlive"] is True
    assert keepalive["RunAtLoad"] is True
    assert keepalive["Disabled"] is False


def test_systemd_service_renderer_is_generic_and_env_reference_only() -> None:
    job = get_job("JOB-028")
    rendered = render_systemd_service(job)

    assert rendered == render_systemd_service(job)
    assert "After=network-online.target" in rendered
    assert "Type=simple" in rendered
    assert "Restart=on-failure" in rendered
    assert "ExecStart=open-brain capture serve --mode=ingress" in rendered
    assert "Environment=OPEN_BRAIN_CONFIG=<OPEN_BRAIN_CONFIG>" in rendered
    assert "Environment=OPEN_BRAIN_INGRESS_CONFIG=<OPEN_BRAIN_INGRESS_CONFIG>" in rendered
    assert "Environment=OPEN_BRAIN_INGRESS_TOKEN=<OPEN_BRAIN_INGRESS_TOKEN>" in rendered
    assert "Environment=OPEN_BRAIN_JOB_ID=JOB-028" in rendered
    assert "Environment=OPEN_BRAIN_DEPLOYMENT_TARGET=ingress-node" in rendered
    assert "Environment=OPEN_BRAIN_SCHEDULER_PLATFORMS=systemd" in rendered
    assert "Environment=OPEN_BRAIN_HOST_ROLE=ingress" in rendered
    assert "Environment=OPEN_BRAIN_WRITER_SCOPE=capture-ingress" in rendered
    assert "Environment=OPEN_BRAIN_LOCK_SCOPE=ingress" in rendered
    assert "TimeoutStartSec=300" in rendered
    assert "WorkingDirectory=<WORKING_DIRECTORY>" in rendered
    assert "StandardOutput=append:<LOG_DIRECTORY>/JOB-028.stdout.log" in rendered
    assert "StandardError=append:<LOG_DIRECTORY>/JOB-028.stderr.log" in rendered

    lowered = rendered.lower()
    for private_marker in ("/users/", "/home/", "laptop", "macmini", "open brain contributors"):
        assert private_marker not in lowered


def test_renderers_preserve_adversarial_argv_literals() -> None:
    command = (
        "open-brain",
        "capture",
        "serve",
        "--percent=%n",
        "--spaces=two words",
        "--single=it's",
        '--double=say "hello"',
        r"--backslash=one\two",
        "--dollar=$USER",
        "--braced=${OPEN_BRAIN_TEST}",
    )
    launchd_job = replace(get_job("JOB-001"), command=command)
    systemd_job = replace(get_job("JOB-028"), command=command)

    launchd_payload = plistlib.loads(render_launchd(launchd_job).encode("utf-8"))
    exec_start = next(
        line for line in render_systemd_service(systemd_job).splitlines()
        if line.startswith("ExecStart=")
    )

    assert launchd_payload["ProgramArguments"] == list(command)
    assert exec_start == (
        'ExecStart=open-brain capture serve "--percent=%%n" '
        '"--spaces=two words" "--single=it\'s" '
        '"--double=say \\"hello\\"" "--backslash=one\\\\two" '
        '"--dollar=$$USER" "--braced=$${OPEN_BRAIN_TEST}"'
    )
    encoded_arguments = shlex.split(exec_start.removeprefix("ExecStart="), posix=True)
    assert tuple(
        argument.replace("%%", "%").replace("$$", "$")
        for argument in encoded_arguments
    ) == command


def test_systemd_timer_renderer_uses_calendar_missed_run_semantics() -> None:
    job = get_job("JOB-029")
    service = render_systemd_service(job)
    timer = render_systemd_timer(job)
    parsed = ConfigParser(interpolation=None, strict=True)
    parsed.read_string(timer)

    assert "Type=oneshot" in service
    assert job.trigger.kind is TriggerKind.CALENDAR_INTERVAL
    assert dict(parsed["Timer"]) == {
        "oncalendar": "*-*-* *:0/5:00",
        "persistent": "true",
        "unit": "org.open-brain.job-029.service",
    }
    assert dict(parsed["Unit"])["x-openbrain-state"] == "enabled"
    assert "OnBootSec" not in parsed["Timer"]
    assert "OnUnitActiveSec" not in parsed["Timer"]

    with pytest.raises(ValueError, match="monotonic interval cannot be persistent"):
        replace(get_job("JOB-001").trigger, persistent=True)

    with pytest.raises(ValueError, match="timer trigger"):
        render_systemd_timer(get_job("JOB-028"))


@pytest.mark.parametrize(
    ("platform", "job_id", "expects_timer"),
    [
        (SchedulerPlatform.LAUNCHD, "JOB-001", False),
        (SchedulerPlatform.LAUNCHD, "JOB-012", False),
        (SchedulerPlatform.SYSTEMD, "JOB-028", False),
        (SchedulerPlatform.SYSTEMD, "JOB-029", True),
    ],
)
def test_generic_renderer_and_validator_are_deterministic_and_pure(
    monkeypatch: pytest.MonkeyPatch,
    platform: SchedulerPlatform,
    job_id: str,
    expects_timer: bool,
) -> None:
    def unexpected_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("renderer attempted file access")

    monkeypatch.setattr("builtins.open", unexpected_open)
    job = get_job(job_id)
    rendered = render_manifest(job, platform)

    assert rendered == render_manifest(job, platform)
    assert (rendered.timer is not None) is expects_timer
    validate_rendered_manifest(job, rendered)


def test_manifest_validator_rejects_any_tampering() -> None:
    job = get_job("JOB-029")
    rendered = render_manifest(job, SchedulerPlatform.SYSTEMD)

    with pytest.raises(ManifestValidationError, match="does not match"):
        validate_rendered_manifest(
            job,
            replace(rendered, service=rendered.service.replace("JOB-029", "JOB-999")),
        )
    with pytest.raises(ManifestValidationError, match="does not match"):
        validate_rendered_manifest(job, replace(rendered, timer=None))


def test_every_catalog_row_renders_only_on_its_allowed_platforms() -> None:
    for job in JOB_CATALOG:
        for platform in SchedulerPlatform:
            if platform not in job.allowed_platforms:
                with pytest.raises(ManifestValidationError, match="not allowed"):
                    render_manifest(job, platform)
                continue

            rendered = render_manifest(job, platform)
            assert rendered.job_id == job.id
            validate_rendered_manifest(job, rendered)


def test_direct_renderers_reject_incompatible_platforms() -> None:
    with pytest.raises(ManifestValidationError, match="not allowed"):
        render_launchd(get_job("JOB-028"))
    with pytest.raises(ManifestValidationError, match="not allowed"):
        render_systemd_service(get_job("JOB-001"))
    with pytest.raises(ManifestValidationError, match="not allowed"):
        render_systemd_timer(get_job("JOB-001"))


def test_public_operations_api_has_no_live_service_actions() -> None:
    forbidden_actions = {
        "install",
        "load",
        "unload",
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "query",
    }

    assert forbidden_actions.isdisjoint(operations.__all__)
