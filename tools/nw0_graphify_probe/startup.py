"""Measure base startup with a dormant separate helper on disposable hosted CI only."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import statistics
import time
from pathlib import Path

from tools.nw0_graphify_probe.run import REPO, digest, run
from tools.open_brain_dev import base_native


def cold_command(environment: dict[str, str], system: str) -> list[str]:
    """Refuse ordinary developer machines and self-hosted runners before any mutation."""
    if (
        environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise ValueError("cache eviction is restricted to disposable GitHub-hosted CI")
    if system == "Darwin":
        return ["/usr/bin/sudo", "-n", "/usr/sbin/purge"]
    if system == "Linux":
        return [
            "/usr/bin/sudo",
            "-n",
            "/bin/sh",
            "-c",
            "/bin/sync && echo 3 > /proc/sys/vm/drop_caches",
        ]
    raise ValueError("unsupported native startup target")


def summarize(samples: dict[str, dict[str, list[float]]]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for mode, budget in (("cold", 0.5), ("warm", 0.2)):
        pair = samples[mode]
        if set(pair) != {"baseline", "candidate"} or any(
            len(series) != 5 or any(not math.isfinite(value) or value <= 0 for value in series)
            for series in pair.values()
        ):
            raise ValueError("five valid observations per layout are required")
        medians = {name: statistics.median(series) for name, series in pair.items()}
        delta = medians["candidate"] - medians["baseline"]
        summary[mode] = {
            "medians_seconds": medians,
            "added_seconds": delta,
            "budget_seconds": budget,
            "passed": delta <= budget,
        }
    summary["passed"] = all(
        isinstance(value, dict) and value["passed"] is True for value in summary.values()
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "build/nw0-startup")
    parser.add_argument("--base", type=Path, default=REPO / "build/native/dist/open-brain")
    parser.add_argument("--proof", type=Path, default=REPO / "build/nw0-graphify")
    args = parser.parse_args()
    eviction = cold_command(dict(os.environ), platform.system())
    base = args.base.resolve(strict=True)
    proof = args.proof.resolve(strict=True)
    artifact = json.loads((proof / "release/artifact-verification.json").read_text())
    runtime = json.loads((proof / "release/runtime-verification.json").read_text())
    helper = proof / "native/dist/open-brain-graphify"
    if artifact["passed"] is not True or runtime["passed"] is not True:
        raise ValueError("native proof must pass before startup measurement")
    current_platform = base_native.native_platform_tag()
    if artifact["platform"] != current_platform or runtime["native_platform"] != current_platform:
        raise ValueError("proof platform differs from current native target")
    helper_hash, base_hash = digest(helper), digest(base)
    if helper_hash != artifact["helper_sha256"] or helper_hash != runtime["helper_sha256"]:
        raise ValueError("helper differs from audited proof")
    if base_hash != artifact["base_audit"]["executable_sha256"]:
        raise ValueError("base differs from audited proof")
    archive = proof / "release" / f"open-brain-graphify-proof-{artifact['platform']}.tar.gz"
    if digest(archive) != artifact["archive_sha256"]:
        raise ValueError("archive differs from audited proof")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for directory in ("home", "tmp", "baseline", "candidate"):
        (output / directory).mkdir()
    for name in ("baseline", "candidate"):
        shutil.copy2(base, output / name / "open-brain")
        if digest(output / name / "open-brain") != base_hash:
            raise ValueError("base copy changed")
    shutil.copy2(helper, output / "candidate/open-brain-graphify")
    if digest(output / "candidate/open-brain-graphify") != helper_hash:
        raise ValueError("helper copy changed")
    environment = {
        "HOME": str(output / "home"),
        "TMPDIR": str(output / "tmp"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    samples: dict[str, dict[str, list[float]]] = {
        mode: {name: [] for name in ("baseline", "candidate")} for mode in ("cold", "warm")
    }
    expected: object = None
    phase = "initialize"

    def checkpoint(state: str, error: str | None = None) -> None:
        progress = {
            "status": state,
            "phase": phase,
            "exception_type": error,
            "platform": artifact["platform"],
            "base_sha256": base_hash,
            "helper_sha256": helper_hash,
            "samples_seconds": samples,
        }
        target = proof / "release/startup-progress.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(progress, indent=2) + "\n")
        temporary.replace(target)

    def observe(name: str, label: str) -> float:
        nonlocal expected, phase
        phase = label
        checkpoint("running")
        started = time.perf_counter()
        run(
            [
                str(output / name / "open-brain"),
                "status",
                "--data-dir",
                str(output / "brain"),
                "--json",
            ],
            output,
            environment,
            label,
            timeout=60,
            max_log_bytes=16384,
        )
        elapsed = time.perf_counter() - started
        value = json.loads((output / f"{label}.log").read_text())
        if expected is None:
            expected = value
        elif value != expected:
            raise ValueError("status output differs between observations")
        if any((output / "tmp").iterdir()):
            raise ValueError("native temporary files remain")
        return elapsed

    try:
        observe("baseline", "initialize")
        for mode in ("cold", "warm"):
            if mode == "warm":
                for name in ("baseline", "candidate"):
                    observe(name, f"warmup-{name}")
            for index in range(5):
                order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                for name in order:
                    label = f"{mode}-{index}-{name}"
                    if mode == "cold":
                        phase = f"evict-{label}"
                        checkpoint("running")
                        run(eviction, output, environment, phase, timeout=60)
                    samples[mode][name].append(observe(name, label))
                    checkpoint("running")
                    (output / "samples.json").write_text(json.dumps(samples, indent=2) + "\n")
                    shutil.copyfile(output / "samples.json", proof / "release/startup-samples.json")
        summary = summarize(samples)
        result = {
            "platform": artifact["platform"],
            "machine": platform.machine(),
            "os_release": platform.release(),
            "base_sha256": base_hash,
            "helper_sha256": helper_hash,
            "archive_sha256": artifact["archive_sha256"],
            "cold_method": "macOS purge"
            if platform.system() == "Darwin"
            else "sync then Linux drop_caches=3",
            "cold_scope": "disk buffer cache; not reboot, installation or anonymous-memory reset",
            "execution": "native GitHub-hosted disposable runner",
            "samples_seconds": samples,
            "summary": summary,
            "scope": (
                "unchanged base status with dormant separate helper present; "
                "no helper activation or whole journey"
            ),
            "model_calls": 0,
        }
        (output / "startup-verification.json").write_text(json.dumps(result, indent=2) + "\n")
        shutil.copyfile(
            output / "startup-verification.json", proof / "release/startup-verification.json"
        )
        if summary["passed"] is not True:
            raise ValueError("startup regression budget exceeded; all samples retained")
    except BaseException as error:
        checkpoint("failed", type(error).__name__)
        raise
    checkpoint("passed")
    print("Native base startup layout proof passed; disk-cache-cold and warm evidence retained.")


if __name__ == "__main__":
    main()
