from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import CommandAdapterRegistry, command_names
from open_brain_legacy.cli.main import main


@dataclass(frozen=True, slots=True)
class _Result:
    exit_code: ExitCode
    envelope: dict[str, object]


@dataclass(frozen=True, slots=True)
class _StaticAdapter:
    result: _Result

    def dispatch(self, argv: tuple[str, ...]) -> _Result:
        return self.result


_FALSE_GREEN_KEYS = {
    "cutover",
    "cutover_ready",
    "live",
    "live_health",
    "live_healthy",
    "parity",
    "parity_green",
    "parity_ready",
}


def _percent_encode(value: str, *, layers: int) -> str:
    for _ in range(layers):
        value = quote(value, safe="")
    return value


@pytest.mark.parametrize("command", command_names())
def test_default_command_families_cannot_false_green_without_an_adapter(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([command, "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "unavailable"
    assert output["error"]["redacted"] is True
    assert _FALSE_GREEN_KEYS.isdisjoint(output)


@pytest.mark.parametrize("reserved_key", sorted(_FALSE_GREEN_KEYS))
@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "nested"])
@pytest.mark.parametrize("encoded", [False, True], ids=["plain", "encoded"])
def test_ordinary_adapter_rejects_reserved_readiness_keys_without_residue(
    reserved_key: str,
    nested: bool,
    encoded: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_key = _percent_encode(reserved_key, layers=3) if encoded else reserved_key
    unsafe: dict[str, object] = {
        "command": "capture",
        "status": "completed",
    }
    if nested:
        unsafe["details"] = {"nested": [{output_key: True}]}
    else:
        unsafe[output_key] = True
    registry = CommandAdapterRegistry(
        {"capture": _StaticAdapter(_Result(ExitCode.SUCCESS, unsafe))}
    )

    exit_code = main(
        ["capture", "synthetic-input", "--json"],
        command_adapters=registry,
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "failed"
    assert _FALSE_GREEN_KEYS.isdisjoint(output)


@pytest.mark.parametrize("command", command_names())
def test_every_family_rejects_nested_encoded_residuals_from_an_injected_adapter(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unsafe: dict[str, object] = {
        "command": command,
        "details": {"nested": ["token%253Dsynthetic-secret"]},
        "status": "completed",
    }
    registry = CommandAdapterRegistry(
        {command: _StaticAdapter(_Result(ExitCode.SUCCESS, unsafe))}
    )

    exit_code = main(
        [command, "synthetic-input", "--json"],
        command_adapters=registry,
    )

    output = json.loads(capsys.readouterr().out)
    rendered = json.dumps(output, sort_keys=True)
    assert exit_code is ExitCode.FAILURE
    assert output == {
        "command": command,
        "error": {
            "code": "command_adapter_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }
    assert "synthetic-secret" not in rendered
    assert "%253D" not in rendered
    assert _FALSE_GREEN_KEYS.isdisjoint(output)


def test_percent_decoding_accepts_safe_value_that_converges_at_strict_bound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    encoded = _percent_encode("synthetic value", layers=3)
    envelope: dict[str, object] = {
        "command": "capture",
        "detail": encoded,
        "status": "completed",
    }
    registry = CommandAdapterRegistry(
        {"capture": _StaticAdapter(_Result(ExitCode.SUCCESS, envelope))}
    )

    exit_code = main(["capture", "--json"], command_adapters=registry)

    assert exit_code is ExitCode.SUCCESS
    assert json.loads(capsys.readouterr().out) == envelope


def test_percent_decoding_rejects_value_one_level_beyond_strict_bound_without_residue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    encoded = _percent_encode("token=synthetic-secret", layers=4)
    envelope: dict[str, object] = {
        "command": "capture",
        "detail": encoded,
        "status": "completed",
    }
    registry = CommandAdapterRegistry(
        {"capture": _StaticAdapter(_Result(ExitCode.SUCCESS, envelope))}
    )

    exit_code = main(["capture", "--json"], command_adapters=registry)

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "failed"
    assert encoded not in json.dumps(output, sort_keys=True)


@pytest.mark.parametrize("command", command_names())
@pytest.mark.parametrize("encoded", [False, True], ids=["plain", "encoded"])
def test_family_command_value_cannot_echo_argv(
    command: str,
    encoded: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "private-capture-body-1234"
    unsafe_suffix = _percent_encode(canary, layers=2) if encoded else canary
    envelope: dict[str, object] = {
        "command": f"{command}.{unsafe_suffix}",
        "status": "completed",
    }
    registry = CommandAdapterRegistry(
        {command: _StaticAdapter(_Result(ExitCode.SUCCESS, envelope))}
    )

    exit_code = main([command, canary, "--json"], command_adapters=registry)

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "failed"
    assert canary not in json.dumps(output, sort_keys=True)
    assert unsafe_suffix not in json.dumps(output, sort_keys=True)


def test_nested_command_value_cannot_echo_argv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "private-capture-body-1234"
    envelope: dict[str, object] = {
        "command": "capture",
        "details": {"command": canary},
        "status": "completed",
    }
    registry = CommandAdapterRegistry(
        {"capture": _StaticAdapter(_Result(ExitCode.SUCCESS, envelope))}
    )

    exit_code = main(["capture", canary, "--json"], command_adapters=registry)

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "failed"
    assert canary not in json.dumps(output, sort_keys=True)


@pytest.mark.parametrize("command", command_names())
@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "nested"])
@pytest.mark.parametrize("encoded", [False, True], ids=["plain", "encoded"])
def test_dynamic_output_key_cannot_echo_argv(
    command: str,
    nested: bool,
    encoded: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "private-capture-body-1234"
    unsafe_key = _percent_encode(canary, layers=2) if encoded else canary
    envelope: dict[str, object] = {
        "command": command,
        "status": "completed",
    }
    if nested:
        envelope["details"] = {unsafe_key: True}
    else:
        envelope[unsafe_key] = True
    registry = CommandAdapterRegistry(
        {command: _StaticAdapter(_Result(ExitCode.SUCCESS, envelope))}
    )

    exit_code = main([command, canary, "--json"], command_adapters=registry)

    output = json.loads(capsys.readouterr().out)
    rendered = json.dumps(output, sort_keys=True)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "failed"
    assert canary not in rendered
    assert unsafe_key not in rendered
