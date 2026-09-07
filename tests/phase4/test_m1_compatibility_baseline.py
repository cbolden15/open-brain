from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import open_brain_engine
import open_brain_engine.engine as engine_facade
from open_brain_engine.engine import BrainEngine

from tools.m1.synthetic_corpus import corpus_catalog_digest, generate_shards

ROOT = Path(__file__).parents[2]
P4_HASHES = {
    "release/phase4-compatibility.json": (
        "f53e68166fc035595f9870d8f592684e808d5cbd8d6278641fc8f4cb8dafa61e"
    ),
    "release/phase4-toolchain.json": (
        "436f9e49a50cda89b653f080a9acb747017e743d56fada756fcffd72ddccb7cb"
    ),
    "release/public-history-allowlist.json": (
        "81addf952045cdcc3190fc4d0b840697aad64c81103181232077608f0e92e381"
    ),
}
PYTHON_ARTIFACT_COORDINATES = {
    "python.app.sdist",
    "python.app.wheel",
    "python.connectors.sdist",
    "python.connectors.wheel",
    "python.engine.sdist",
    "python.engine.wheel",
}


def test_completed_p4_evidence_bytes_are_unchanged() -> None:
    assert {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in P4_HASHES
    } == P4_HASHES


def test_v0_engine_facade_and_six_python_artifact_coordinates_are_preserved() -> None:
    assert BrainEngine.__module__ == "open_brain_engine.engine.local"
    assert open_brain_engine.__version__ == "0.1.0"
    facade_bytes = json.dumps(
        engine_facade.__all__, separators=(",", ":"), ensure_ascii=True
    ).encode()
    assert len(engine_facade.__all__) == 83
    assert hashlib.sha256(facade_bytes).hexdigest() == (
        "27163e8d7e1ef337f919d2858b066c9c2e8a314f75afe9642a15fd35f1c270d0"
    )
    policy = json.loads((ROOT / "release/v0-artifact-policy.json").read_text(encoding="utf-8"))
    assert set(policy["python_distributions"]) == {"app", "connector", "engine"}
    coordinates = {
        f"python.{distribution_name}.{kind}"
        for distribution_name in ("app", "connectors", "engine")
        for kind in ("sdist", "wheel")
    }
    assert coordinates == PYTHON_ARTIFACT_COORDINATES


def test_m1_dependency_strategy_preserves_python_range_and_hides_engine_extra() -> None:
    engine = tomllib.loads((ROOT / "packages/engine/pyproject.toml").read_text(encoding="utf-8"))
    app = tomllib.loads((ROOT / "packages/app/pyproject.toml").read_text(encoding="utf-8"))

    assert engine["project"]["requires-python"] == ">=3.12,<3.15"
    assert app["project"]["requires-python"] == ">=3.12,<3.15"
    assert engine["project"]["dependencies"] == ["rfc8785>=0.1.4,<0.2"]
    assert set(engine["project"]["optional-dependencies"]["node"]) == {
        "argon2-cffi>=25.1,<26",
        "cryptography>=50,<51",
        "keyring>=25.6,<26",
        "pyobjc-framework-LocalAuthentication>=12,<13; sys_platform == 'darwin'",
        "sqlcipher3==0.6.2",
    }
    assert set(app["project"]["dependencies"]) == {
        "open-brain-engine[node]==0.1.0",
        "starlette>=0.48,<1",
        "uvicorn>=0.40,<1",
    }


def test_m1_protocol_resources_extend_the_frozen_artifact_contract() -> None:
    policy = json.loads((ROOT / "release/v0-artifact-policy.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "docs/v0-package-classification.json").read_text(encoding="utf-8")
    )
    engine_artifacts = policy["python_distributions"]["engine"]["artifacts"]

    assert {
        tree["destination"] for tree in engine_artifacts["wheel"]["required_trees"]
    } >= {
        "open_brain_engine/protocol/conformance/v1",
        "open_brain_engine/protocol/schemas/v1",
    }
    assert {
        tree["destination"] for tree in engine_artifacts["sdist"]["required_trees"]
    } >= {
        "src/open_brain_engine/protocol/conformance/v1",
        "src/open_brain_engine/protocol/schemas/v1",
    }
    assert manifest["phase4"]["release_identity"]["m1_compatibility_record"] == (
        "release/m1-compatibility.json"
    )
    assert manifest["phase4"]["subjects"]["release/m1-compatibility.json"][
        "artifact_disposition"
    ] == ["app-sdist", "engine-sdist"]


def test_m1_matrix_is_complete_without_rewriting_p4_support() -> None:
    evidence = json.loads((ROOT / "release/m1-compatibility.json").read_text(encoding="utf-8"))
    cells = evidence["cells"]

    assert evidence["schema_version"] == 1
    assert evidence["windows"] == {"status": "unsupported", "milestone": "post-M1"}
    assert len(cells) == 12
    assert {
        (cell["platform"], cell["architecture"], cell["python"], cell["install"]) for cell in cells
    } == {
        (platform, architecture, python, install)
        for platform, architecture in (
            ("macos", "arm64"),
            ("linux", "x86_64"),
        )
        for python in ("3.12", "3.13", "3.14")
        for install in ("source", "wheel")
    }
    assert all(cell["status"] == "passed" for cell in cells)
    assert all(
        set(cell["checks"])
        == {
            "canonical_json",
            "crash_recovery",
            "fts5",
            "keyed_reopen",
            "paged_reads",
            "purge_plaintext_residue",
            "source_or_wheel_install",
            "wrong_key_rejection",
        }
        and set(cell["checks"].values()) == {"passed"}
        for cell in cells
    )
    application = evidence["top_level_application"]
    assert application["status"] == "passed"
    assert len(application["installations"]) == 12
    assert {
        (cell["platform"], cell["architecture"], cell["python"], cell["install"])
        for cell in application["installations"]
    } == {
        (platform, architecture, python, install)
        for platform, architecture in (
            ("macos", "arm64"),
            ("linux", "x86_64"),
        )
        for python in ("3.12", "3.13", "3.14")
        for install in ("source", "wheel")
    }
    assert all(cell["status"] == "passed" for cell in application["installations"])


def test_synthetic_corpus_reaches_every_provisional_label_and_shard_boundary() -> None:
    evidence = json.loads((ROOT / "release/m1-compatibility.json").read_text(encoding="utf-8"))
    shards = generate_shards()
    generator = evidence["synthetic_corpus"]["generator"]

    assert len(shards) == 256
    assert len({shard.labels for shard in shards}) == 256
    assert max(len(shard.labels) for shard in shards) == 16
    assert max(shard.records for shard in shards) == 50_000
    assert corpus_catalog_digest(shards) == generator["catalog_sha256"]
    assert evidence["synthetic_corpus"]["benchmarks"]
    assert all(
        benchmark["status"] == "passed"
        and benchmark["benchmark"]["query_fanout_per_round"] == 32
        and benchmark["benchmark"]["query_rounds"] == 8
        and benchmark["benchmark"]["commit_batch_items"] == 128
        for benchmark in evidence["synthetic_corpus"]["benchmarks"]
    )
