from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import open_brain_engine
import open_brain_engine.engine as engine_facade
import pytest
from open_brain_engine.engine import BrainEngine

from tools.m1.compatibility_matrix import command_templates
from tools.m1.compatibility_probe import _connect, _purge_documents
from tools.m1.synthetic_corpus import corpus_catalog_digest, fuse_ranked_shards, generate_shards

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
    assert manifest["phase4"]["release_identity"]["m1_compatibility_receipts"] == (
        "release/m1-compatibility-receipts.json"
    )
    assert manifest["phase4"]["subjects"]["release/m1-compatibility.json"][
        "artifact_disposition"
    ] == ["app-sdist", "engine-sdist"]
    assert manifest["phase4"]["subjects"]["release/m1-compatibility-receipts.json"][
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
            "purge_logical_deletion",
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


def test_m1_matrix_summary_is_bound_to_reproducible_raw_receipts() -> None:
    evidence = json.loads((ROOT / "release/m1-compatibility.json").read_text(encoding="utf-8"))
    receipt_path = ROOT / evidence["raw_receipts"]["path"]
    receipts = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == evidence["raw_receipts"][
        "sha256"
    ]
    assert hashlib.sha256((ROOT / receipts["probe_path"]).read_bytes()).hexdigest() == receipts[
        "probe_sha256"
    ]
    assert receipts["probe_sha256"] == evidence["raw_receipts"]["probe_sha256"]
    assert receipts["status"] == "passed"
    assert len(receipts["cells"]) == 12

    raw_by_cell = {
        (cell["platform"], cell["architecture"], cell["python"], cell["install"]): cell
        for cell in receipts["cells"]
    }
    summary_by_cell = {
        (cell["platform"], cell["architecture"], cell["python"], cell["install"]): cell
        for cell in evidence["cells"]
    }
    assert set(raw_by_cell) == set(summary_by_cell)
    for key, raw in raw_by_cell.items():
        summary = summary_by_cell[key]
        assert raw["status"] == "passed"
        assert json.loads(raw["probe_stdout"].splitlines()[-1]) == raw["probe"]
        assert raw["commands"] == command_templates(raw["platform"], raw["python"], raw["install"])
        assert str(ROOT) not in json.dumps(raw["commands"])
        assert raw["probe"]["storage"]["purge_logical_deletion"] == "pass"
        assert summary["measurements"] == {
            "commit_256_rows_seconds": raw["probe"]["storage"]["benchmark"]["commit_seconds"],
            "paged_read_2048_rows_seconds": raw["probe"]["storage"]["benchmark"][
                "paged_read_seconds"
            ],
        }


def test_synthetic_corpus_reaches_every_provisional_label_and_shard_boundary() -> None:
    evidence = json.loads((ROOT / "release/m1-compatibility.json").read_text(encoding="utf-8"))
    shards = generate_shards()
    generator = evidence["synthetic_corpus"]["generator"]

    assert len(shards) == 256
    assert len({shard.labels for shard in shards}) == 256
    assert max(len(shard.labels) for shard in shards) == 16
    assert max(shard.records for shard in shards) == 50_000
    assert corpus_catalog_digest(shards) == generator["catalog_sha256"]
    corpus = evidence["synthetic_corpus"]
    assert hashlib.sha256((ROOT / corpus["generator_path"]).read_bytes()).hexdigest() == corpus[
        "generator_sha256"
    ]
    assert "raw BM25 values never cross shard boundaries" in corpus["ranking_method"]
    assert evidence["synthetic_corpus"]["benchmarks"]
    assert all(
        benchmark["status"] == "passed"
        and benchmark["benchmark"]["query_fanout_per_round"] == 32
        and benchmark["benchmark"]["query_rounds"] == 8
        and benchmark["benchmark"]["commit_batch_items"] == 128
        and benchmark["benchmark"]["fusion"] == "reciprocal_rank"
        and benchmark["benchmark"]["reciprocal_rank_constant"] == 60
        for benchmark in evidence["synthetic_corpus"]["benchmarks"]
    )


def test_purge_probe_removes_rows_and_search_hits(tmp_path: Path) -> None:
    connection = _connect(tmp_path / "purge.sqlite3")
    connection.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(body)")
    connection.execute("INSERT INTO documents(id, body) VALUES (1, 'synthetic canary')")
    connection.execute("INSERT INTO documents_fts(rowid, body) VALUES (1, 'synthetic canary')")
    connection.commit()

    _purge_documents(connection)

    assert connection.execute("SELECT count(*) FROM documents").fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM documents_fts").fetchone() == (0,)
    connection.close()


def test_purge_verifier_rejects_readable_rows(tmp_path: Path) -> None:
    from tools.m1.compatibility_probe import _verify_documents_absent

    connection = _connect(tmp_path / "not-purged.sqlite3")
    connection.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(body)")
    connection.execute("INSERT INTO documents(id, body) VALUES (1, 'synthetic canary')")
    connection.execute("INSERT INTO documents_fts(rowid, body) VALUES (1, 'synthetic canary')")
    connection.commit()

    with pytest.raises(AssertionError, match="purge left readable rows"):
        _verify_documents_absent(connection)
    connection.close()


def test_cross_shard_ranking_uses_rrf_not_raw_fts_scores() -> None:
    assert fuse_ranked_shards([(7, [700, 701]), (2, [200, 201])], top_k=4) == [
        (1 / 61, 2, 200),
        (1 / 61, 7, 700),
        (1 / 62, 2, 201),
        (1 / 62, 7, 701),
    ]
