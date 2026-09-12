from __future__ import annotations

import sys
from types import ModuleType

import pytest

import open_brain.services.secure_node_entrypoints as entrypoints


def test_secure_node_wrappers_fail_before_advanced_imports_when_extra_is_absent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entrypoints, "_secure_node_dependencies_available", lambda: False)
    sys.modules.pop("open_brain.services.appliance_entrypoints", None)

    assert entrypoints.run_cli(("--help",), environment={}) == 2
    assert capsys.readouterr().err == (
        "Secure Node is not installed. Install open-brain[secure-node].\n"
    )
    assert "open_brain.services.appliance_entrypoints" not in sys.modules
    assert entrypoints.run_mcp() == 2
    assert capsys.readouterr().err == (
        "Secure Node is not installed. Install open-brain[secure-node].\n"
    )


def test_secure_node_wrappers_lazy_dispatch_when_extra_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appliance = ModuleType("open_brain.services.appliance_entrypoints")
    calls: list[tuple[object, object]] = []

    def run_cli(argv: object, *, environment: object) -> int:
        calls.append((argv, environment))
        return 17

    def run_mcp() -> int:
        calls.append(("mcp", None))
        return 19

    appliance.run_cli = run_cli  # type: ignore[attr-defined]
    appliance.run_mcp = run_mcp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, appliance.__name__, appliance)
    monkeypatch.setattr(entrypoints, "_secure_node_dependencies_available", lambda: True)

    assert entrypoints.run_cli(("--version",), environment={"A": "B"}) == 17
    assert entrypoints.run_mcp() == 19
    assert calls == [(("--version",), {"A": "B"}), ("mcp", None)]
