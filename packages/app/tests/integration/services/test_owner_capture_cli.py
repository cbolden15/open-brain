"""Owner capture and search options used by a forced-command capture server."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import BrainEngine, local_schema
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_operations import current_brain_identity


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _brain(tmp_path: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain"))


def _run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    engine: BrainEngine,
    *arguments: str,
    stdin: bytes = b"",
) -> tuple[int, Any]:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8"))
    code = run_cli(
        (*arguments, "--json", "--data-dir", str(engine.profile.root)),
        filesystem_type_probe=_filesystem,
    )
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def _identity(engine: BrainEngine) -> tuple[str, str]:
    brain_id, epoch = current_brain_identity(engine.tasks)
    return brain_id, str(epoch)


def test_capture_reads_stdin_and_replays_by_delivery_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    brain_id, epoch = _identity(engine)
    arguments = (
        "capture",
        "-",
        "--privacy-tier",
        "work",
        "--delivery-id",
        "owner.6f1c7c1e-1d2b-4a55-9b7e-2f0c7a1d9e10",
        "--expect-brain-id",
        brain_id,
        "--expect-issuer-epoch",
        epoch,
    )
    code, first = _run(
        capsys, monkeypatch, engine, *arguments, stdin="Synthetic owner note é".encode()
    )
    assert code == 0, first
    # A forced-command server passes this dict through as `result`; its client checks these keys.
    assert set(first) == {"capture_id", "duplicate", "payload_family", "state", "status"}
    code, second = _run(
        capsys, monkeypatch, engine, *arguments, stdin="Synthetic owner note é".encode()
    )
    assert code == 0, second
    assert second["capture_id"] == first["capture_id"]
    assert second["duplicate"] is True
    code, found = _run(
        capsys,
        monkeypatch,
        engine,
        "search",
        "-",
        "--limit",
        "5",
        "--expect-brain-id",
        brain_id,
        "--expect-issuer-epoch",
        epoch,
        stdin=b"Synthetic owner note",
    )
    assert code == 0, found
    # A recall client reads row["excerpt"]; matched terms in it are bracketed, so match the row
    # by capture ID and require a non-empty excerpt string.
    rows = [row for row in found["results"] if row["capture_id"] == first["capture_id"]]
    assert len(rows) == 1
    assert isinstance(rows[0]["excerpt"], str) and "Synthetic" in rows[0]["excerpt"]


def test_capture_refuses_a_reused_delivery_id_with_different_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    base = ("capture", "-", "--delivery-id", "owner.reuse-check")
    assert _run(capsys, monkeypatch, engine, *base, stdin=b"first text")[0] == 0
    code, payload = _run(capsys, monkeypatch, engine, *base, stdin=b"second text")
    assert code != 0, payload


@pytest.mark.parametrize("command", ["capture", "search"])
def test_wrong_destination_is_refused_before_any_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    engine = _brain(tmp_path)
    brain_id, epoch = _identity(engine)
    code, payload = _run(
        capsys,
        monkeypatch,
        engine,
        command,
        "-",
        "--expect-brain-id",
        brain_id + "x",
        "--expect-issuer-epoch",
        epoch,
        stdin=b"Synthetic text",
    )
    assert code == 78
    assert cast(dict[str, Any], payload)["error"]["code"] == "destination_mismatch"
    code, payload = _run(capsys, monkeypatch, engine, "search", "Synthetic", "--limit", "5")
    assert payload["results"] == []


def _state_files(root: Path) -> dict[str, str]:
    state = root / ".open-brain/state"
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(state.glob("phase1.sqlite3*"))
    }


def _schema(root: Path) -> int:
    connection = sqlite3.connect(f"file:{root}/.open-brain/state/phase1.sqlite3?mode=ro", uri=True)
    try:
        return cast(
            int, connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        )
    finally:
        connection.close()


def test_wrong_destination_never_opens_an_unmigrated_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A schema-22 store, as an older release leaves it. A plain CLI call would migrate it to 23.
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 22)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:22])
        engine = _brain(tmp_path)
        brain_id, epoch = _identity(engine)
    root = engine.profile.root
    assert _schema(root) == 22
    before = _state_files(root)
    code, payload = _run(
        capsys,
        monkeypatch,
        engine,
        "search",
        "-",
        "--expect-brain-id",
        brain_id + "x",
        "--expect-issuer-epoch",
        epoch,
        stdin=b"Synthetic",
    )
    assert code == 78
    assert cast(dict[str, Any], payload)["error"]["code"] == "destination_mismatch"
    assert _schema(root) == 22
    assert _state_files(root) == before


@pytest.mark.parametrize(
    "arguments",
    [
        ("capture", "x", "--delivery-id", "delivery.engine-owned"),
        ("capture", "x", "--delivery-id", "-leading-dash"),
        ("capture", "x", "--delivery-id", "has space"),
        ("capture", "x", "--expect-brain-id", "only-one-half"),
        ("search", "x", "--expect-issuer-epoch", "3"),
    ],
)
def test_malformed_owner_options_are_usage_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    engine = _brain(tmp_path)
    code, _payload = _run(capsys, monkeypatch, engine, *arguments)
    assert code == 2


def test_oversized_stdin_capture_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _brain(tmp_path)
    code, _payload = _run(capsys, monkeypatch, engine, "capture", "-", stdin=b"a" * 1_048_577)
    assert code == 2
