from __future__ import annotations

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.explain import explain_policy


def test_explain_denies_network_and_fails_closed_for_unknown_policy() -> None:
    explained = explain_policy("no-network")
    denied = explain_policy("token=synthetic-secret /synthetic/private")

    assert explained.exit_code is ExitCode.SUCCESS
    assert explained.envelope == {
        "command": "explain",
        "network_access": "denied",
        "policy": "no-network",
        "status": "ok",
    }
    assert denied.exit_code is ExitCode.FAILURE
    assert denied.envelope == {
        "command": "explain",
        "error": {
            "code": "policy_explanation_unavailable",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }
    assert "synthetic-secret" not in denied.to_json()
    assert "/synthetic/private" not in denied.to_json()

