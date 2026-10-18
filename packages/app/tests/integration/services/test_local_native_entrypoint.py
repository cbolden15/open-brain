from __future__ import annotations

import json

import pytest

from open_brain.services import local_native_entrypoint


def test_source_self_check_fails_without_touching_local_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert local_native_entrypoint.main(("__open-brain-self-check",)) == 1
    output = json.loads(capsys.readouterr().out)

    assert output == {
        "daemon_running": False,
        "frozen": False,
        "profile": "local",
        "status": "failed",
        "version": "0.1.0",
    }


def test_native_entrypoint_routes_normal_commands_to_local_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_run_cli(arguments: tuple[str, ...]) -> int:
        observed.append(arguments)
        return 7

    monkeypatch.setattr(local_native_entrypoint, "run_cli", fake_run_cli)

    assert local_native_entrypoint.main(("status", "--json")) == 7
    assert local_native_entrypoint.main(("--version", "--json")) == 7
    assert observed == [("status", "--json"), ("--version", "--json")]


@pytest.mark.parametrize("valid", [True, False])
def test_frozen_self_check_requires_relocated_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    valid: bool,
) -> None:
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(local_native_entrypoint, "_runtime_metadata_is_relocated", lambda: valid)
    assert local_native_entrypoint.main(("__open-brain-self-check",)) == (0 if valid else 1)
    assert json.loads(capsys.readouterr().out)["status"] == ("ok" if valid else "failed")


@pytest.mark.parametrize("broken", [None, "path", "abi", "missing"])
def test_runtime_metadata_checks_paths_and_pointer_abi(
    monkeypatch: pytest.MonkeyPatch, broken: str | None
) -> None:
    import ctypes
    import sys
    import sysconfig

    variables: dict[str, object] = {
        "BINDIR": "/frozen/bin",
        "LIBDIR": "/frozen/lib",
        "SIZEOF_VOID_P": ctypes.sizeof(ctypes.c_void_p),
        "SOABI": "cpython-synthetic",
    }
    if broken == "path":
        variables["LIBDIR"] = "/unrelated-build/lib"
    if broken == "abi":
        variables["SIZEOF_VOID_P"] = 1
    if broken == "missing":
        del variables["SOABI"]
    monkeypatch.setattr(sys, "prefix", "/frozen")
    monkeypatch.setattr(sys, "exec_prefix", "/frozen")
    monkeypatch.setattr(sysconfig, "get_config_vars", lambda: variables)
    assert local_native_entrypoint._runtime_metadata_is_relocated() is (broken is None)


@pytest.mark.parametrize("compiled", [False, True])
def test_frozen_self_check_requires_working_compiled_decimal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], compiled: bool
) -> None:
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(local_native_entrypoint, "_runtime_metadata_is_relocated", lambda: True)
    monkeypatch.setattr(local_native_entrypoint, "_compiled_decimal_is_available", lambda: compiled)
    assert local_native_entrypoint.main(("__open-brain-self-check",)) == (0 if compiled else 1)
    assert json.loads(capsys.readouterr().out)["status"] == ("ok" if compiled else "failed")


def test_compiled_decimal_probe_preserves_current_context() -> None:
    from decimal import getcontext

    before = getcontext().copy()
    assert local_native_entrypoint._compiled_decimal_is_available()
    assert str(getcontext()) == str(before)


@pytest.mark.parametrize("broken", ["missing", "different-type"])
def test_compiled_decimal_probe_refuses_missing_or_substituted_implementation(
    monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    import sys
    from types import SimpleNamespace

    module = None if broken == "missing" else SimpleNamespace(Decimal=object)
    monkeypatch.setitem(sys.modules, "_decimal", module)
    assert not local_native_entrypoint._compiled_decimal_is_available()
