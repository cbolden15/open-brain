"""Safety and evidence controls for the CI-only startup experiment."""

import json
import sys
from pathlib import Path

import pytest

from tools.nw0_graphify_probe import startup
from tools.nw0_graphify_probe.run import digest
from tools.nw0_graphify_probe.startup import cold_command, summarize
from tools.open_brain_dev import base_native


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"GITHUB_ACTIONS": "true"},
        {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"},
    ],
)
def test_local_and_self_hosted_cache_eviction_rejected(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="disposable GitHub-hosted"):
        cold_command(environment, "Darwin")


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_supported_hosted_cache_commands_are_noninteractive(system: str) -> None:
    command = cold_command(
        {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}, system
    )
    assert command[:2] == ["/usr/bin/sudo", "-n"]
    if system == "Linux":
        assert command[-1] == "/bin/sync && echo 3 > /proc/sys/vm/drop_caches"


def test_startup_budget_failure_is_retained() -> None:
    result = summarize(
        {
            "cold": {"baseline": [1.0] * 5, "candidate": [1.6] * 5},
            "warm": {"baseline": [1.0] * 5, "candidate": [1.1] * 5},
        }
    )
    assert result["passed"] is False
    assert isinstance(result["cold"], dict) and result["cold"]["passed"] is False
    assert isinstance(result["warm"], dict) and result["warm"]["passed"] is True


@pytest.mark.parametrize("values", [[1.0] * 4, [float("nan")] * 5, [float("inf")] * 5, [0.0] * 5])
def test_missing_or_invalid_samples_cannot_pass(values: list[float]) -> None:
    with pytest.raises(ValueError, match="five valid"):
        summarize(
            {
                "cold": {"baseline": values, "candidate": [1.0] * 5},
                "warm": {"baseline": [1.0] * 5, "candidate": [1.0] * 5},
            }
        )


@pytest.mark.parametrize(
    "failure",
    ["budget", "partial", "initialize", "eviction", "platform", "helper", "base", "archive"],
)
def test_main_rejects_mismatch_and_preserves_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    proof = tmp_path / "proof"
    release = proof / "release"
    release.mkdir(parents=True)
    helper = proof / "native/dist/open-brain-graphify"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"synthetic helper")
    base = tmp_path / "base"
    base.write_bytes(b"synthetic base")
    archive = release / "open-brain-graphify-proof-macos-arm64.tar.gz"
    archive.write_bytes(b"synthetic archive")
    artifact = {
        "passed": True,
        "platform": "macos-arm64",
        "helper_sha256": digest(helper),
        "archive_sha256": digest(archive),
        "base_audit": {"executable_sha256": digest(base)},
    }
    runtime = {
        "passed": True,
        "native_platform": "linux-x86_64" if failure == "platform" else "macos-arm64",
        "helper_sha256": "wrong" if failure == "helper" else digest(helper),
    }
    (release / "artifact-verification.json").write_text(json.dumps(artifact))
    (release / "runtime-verification.json").write_text(json.dumps(runtime))
    if failure == "base":
        base.write_bytes(b"changed")
    if failure == "archive":
        archive.write_bytes(b"changed")
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        ["startup", "--output", str(output), "--base", str(base), "--proof", str(proof)],
    )
    monkeypatch.setattr(startup, "cold_command", lambda *_: ["synthetic-eviction"])
    monkeypatch.setattr(base_native, "native_platform_tag", lambda: "macos-arm64")
    calls: list[str] = []

    def fake_run(
        command: list[str],
        directory: Path,
        environment: dict[str, str],
        label: str,
        **kwargs: object,
    ) -> None:
        calls.append(label)
        if (
            (failure == "partial" and label == "cold-0-candidate")
            or (failure == "initialize" and label == "initialize")
            or (failure == "eviction" and label == "evict-cold-0-baseline")
        ):
            raise RuntimeError("synthetic interrupted invocation")
        (directory / f"{label}.log").write_text('{"synthetic": true}\n')

    monkeypatch.setattr(startup, "run", fake_run)
    if failure == "budget":
        monkeypatch.setattr(startup, "summarize", lambda _: {"passed": False})
    with pytest.raises((RuntimeError, ValueError)):
        startup.main()
    if failure == "budget":
        receipt = json.loads((release / "startup-verification.json").read_text())
        assert receipt["summary"]["passed"] is False
        assert all(
            len(series) == 5
            for pair in receipt["samples_seconds"].values()
            for series in pair.values()
        )
    elif failure == "partial":
        samples = json.loads((release / "startup-samples.json").read_text())
        assert len(samples["cold"]["baseline"]) == 1
        assert samples["cold"]["candidate"] == []
    elif failure in ("initialize", "eviction"):
        progress = json.loads((release / "startup-progress.json").read_text())
        assert progress["status"] == "failed"
        assert progress["exception_type"] == "RuntimeError"
        assert progress["phase"] == (
            "initialize" if failure == "initialize" else "evict-cold-0-baseline"
        )
        assert progress["samples_seconds"]["cold"]["baseline"] == []
    else:
        assert calls == []
        assert not output.exists()
