from __future__ import annotations

import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    SourceInspectRequest,
    SourceWithdrawRequest,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.source_intake import SourceRevisionSubmission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

import open_brain.services.local_entrypoints as entrypoints
from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _source(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "brain"
    profile = compile_single_user_local(root)
    tasks = open_local_engine(profile)
    receipt = tasks.capture.accept(
        TextPayload("SYNTHETIC_RETAINED_CLI_BODY"), delivery_id="synthetic.cli.source"
    )
    with open_local_database_read_only(profile) as connection:
        source_id = connection.execute(
            "SELECT source_id FROM source_revisions WHERE capture_id=?", (receipt.capture_id,)
        ).fetchone()[0]
    return root, source_id, receipt.capture_id


def _cli(
    root: Path, capsys: pytest.CaptureFixture[str], *arguments: str
) -> tuple[int, dict[str, object]]:
    code = entrypoints.run_cli(
        ("source", *arguments, "--data-dir", str(root), "--json"),
        filesystem_type_probe=_filesystem,
    )
    output = capsys.readouterr()
    assert output.err == ""
    assert "SYNTHETIC_RETAINED_CLI_BODY" not in output.out
    return code, cast(dict[str, object], json.loads(output.out))


def _request(inspection: dict[str, object]) -> dict[str, object]:
    return {
        "dto_version": 1,
        "operation_id": "withdraw.synthetic-cli",
        "source_id": inspection["source_id"],
        "expected_head": inspection["head_capture_id"],
        "expected_lifecycle_version": inspection["lifecycle_version"],
        "brain_id": inspection["destination_brain_id"],
        "issuer_epoch": inspection["issuer_epoch"],
        "reason_code": "owner_choice",
        "absence_evidence_digest": None,
    }


@pytest.mark.parametrize(
    "contract",
    [
        SourceInspectRequest(source_id="source_synthetic"),
        SourceWithdrawRequest(
            operation_id="withdraw.synthetic-cli",
            source_id="source_synthetic",
            expected_head="capture_synthetic",
            expected_lifecycle_version=0,
            brain_id="brn_synthetic",
            issuer_epoch=1,
            reason_code="owner_choice",
        ),
    ],
    ids=["inspect", "withdraw"],
)
@pytest.mark.parametrize("version", [True, 1.0], ids=["bool", "float"])
def test_source_lifecycle_dtos_require_exact_integer_version(
    contract: SourceInspectRequest | SourceWithdrawRequest, version: object
) -> None:
    with pytest.raises(T03Error, match="invalid_arguments"):
        replace(contract, dto_version=cast(int, version))


def _owner_cli(
    root: Path, capsys: pytest.CaptureFixture[str], *arguments: str
) -> tuple[int, dict[str, object]]:
    code = entrypoints.run_cli(
        (*arguments, "--data-dir", str(root), "--json"), filesystem_type_probe=_filesystem
    )
    output = capsys.readouterr()
    assert output.err == ""
    return code, cast(dict[str, object], json.loads(output.out))


def test_owner_cli_withdraw_retains_paged_exact_predecessor_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "brain"
    profile = compile_single_user_local(root)
    tasks = open_local_engine(profile)
    assert tasks.sources is not None
    original = _public_submission(tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="cli", resource_id="one", external_id="one"
    )
    texts = [
        "SYNTHETIC_RETAINED_CLI_BODY 漢字🙂\n" * 2000,
        "SYNTHETIC_RETAINED_CLI_BODY second revision",
        "SYNTHETIC_RETAINED_CLI_BODY third revision",
    ]
    assert len(texts[0].encode("utf-8")) > 65_536
    capture_ids: list[str] = []
    head = None
    for sequence, text in enumerate(texts):
        capture = replace(original, payload=TextPayload(text))
        receipt = tasks.sources.submit_revision(
            SourceRevisionSubmission(
                capture=capture,
                namespace=namespace,
                revision_key=str(sequence),
                canonical_sha256=capture.request_sha256(),
                expected_head=head,
                ordering={"kind": "unordered"}
                if sequence == 0
                else {"kind": "predecessor", "revision_key": str(sequence - 1)},
                expected_control_epoch=0,
            )
        )
        assert receipt.outcome == "captured"
        assert receipt.capture_id is not None
        head = receipt.capture_id
        capture_ids.append(head)
    assert receipt.source_id is not None
    source_id = receipt.source_id
    unrelated = tasks.capture.accept(
        TextPayload("Synthetic independent source"), delivery_id="synthetic.cli.unrelated"
    )
    with open_local_database_read_only(profile) as connection:
        retained = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM source_revisions WHERE source_id=? ORDER BY sequence", (source_id,)
            )
        ]
        evidence = {
            row["source_path"]: bytes(row["source_bytes"])
            for row in connection.execute(
                "SELECT source_path,source_bytes FROM source_revisions WHERE source_id=?",
                (source_id,),
            )
        }
    code, current = _owner_cli(
        root, capsys, "read", capture_ids[0], "--expected-revision-id", capture_ids[-1]
    )
    assert code == 0
    assert cast(dict[str, object], current["content"])["text"] == texts[-1]
    code, inspection = _cli(root, capsys, "inspect", source_id)
    assert code == 0
    path = tmp_path / "withdraw.json"
    path.write_text(json.dumps(_request(inspection)), encoding="utf-8")
    code, withdrawal = _cli(root, capsys, "withdraw", "--request-file", str(path))
    assert code == 0
    assert withdrawal["lifecycle"] == "retired"
    assert withdrawal["head_capture_id"] == capture_ids[-1]
    code, refused = _owner_cli(
        root,
        capsys,
        "history",
        "show",
        unrelated.capture_id,
        "--expected-revision-id",
        capture_ids[0],
    )
    assert code == 1
    assert cast(dict[str, object], refused["error"])["code"] == "not_found"
    assert "content" not in refused

    cursor_arguments: tuple[str, ...] = ()
    entries: list[dict[str, object]] = []
    for page_number in range(3):
        code, page = _owner_cli(
            root, capsys, "history", "list", capture_ids[0], "--limit", "1", *cursor_arguments
        )
        assert code == 0
        assert page["status"] == "ok"
        assert page["dto_version"] == 1
        members = cast(list[dict[str, object]], page["entries"])
        assert len(members) == 1
        entries.extend(members)
        assert page["complete"] is (page_number == 2)
        if page_number == 2:
            assert page["next_cursor"] is None
        else:
            assert isinstance(page["next_cursor"], str)
            cursor_arguments = ("--cursor", page["next_cursor"])
    assert [entry["revision_id"] for entry in entries] == capture_ids[::-1]
    assert [entry["predecessor_revision_id"] for entry in entries] == [
        capture_ids[1],
        capture_ids[0],
        None,
    ]
    assert [entry["is_current"] for entry in entries] == [True, False, False]
    for entry in entries:
        assert entry["lifecycle"] == "retired"
        assert entry["availability"] == "missing"
        assert entry["reason"] == "Retained source revision"
        assert cast(str, entry["recorded_at"]).endswith("Z")
        assert entry["provenance"] == {
            "representative_capture_id": entry["revision_id"],
            "capture_ids": [entry["revision_id"]],
            "source_origin": "third_party",
        }

    for capture_id, text in zip(capture_ids, texts, strict=True):
        cursor_arguments = ()
        chunks: list[bytes] = []
        offset = 0
        for _ in range(32):
            code, response = _owner_cli(
                root,
                capsys,
                "history",
                "show",
                capture_ids[-1],
                "--expected-revision-id",
                capture_id,
                "--target-bytes",
                "4096",
                *cursor_arguments,
            )
            assert code == 0
            assert response["status"] == "ok"
            assert response["dto_version"] == 1
            record = cast(dict[str, object], response["record"])
            assert record["record_id"] == record["revision_id"] == capture_id
            assert record["source_id"] == source_id
            assert record["provenance"] == {
                "representative_capture_id": capture_id,
                "capture_ids": [capture_id],
                "source_origin": "third_party",
            }
            content = cast(dict[str, object], response["content"])
            assert content["kind"] == "untrusted_text"
            chunk = cast(str, content["text"]).encode("utf-8")
            assert 0 < len(chunk) <= 4096
            assert response["start_byte"] == offset
            chunks.append(chunk)
            offset += len(chunk)
            assert response["end_byte"] == offset
            if response["complete"]:
                assert response["next_cursor"] is None
                break
            assert isinstance(response["next_cursor"], str)
            cursor_arguments = ("--cursor", response["next_cursor"])
        else:
            pytest.fail("owner CLI retained history exceeded the bounded fixture")
        assert b"".join(chunks) == text.encode("utf-8")
        if capture_id == capture_ids[0]:
            assert len(chunks) > 1
        code, refused = _owner_cli(
            root, capsys, "read", capture_id, "--expected-revision-id", capture_ids[-1]
        )
        assert code == 1
        assert cast(dict[str, object], refused["error"])["code"] == "not_found"
        assert "content" not in refused

    with open_local_database_read_only(profile) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM source_revisions WHERE source_id=? ORDER BY sequence", (source_id,)
            )
        ] == retained
    assert len(evidence) == 3
    assert all((root / path).read_bytes() == data for path, data in evidence.items())


@pytest.mark.parametrize("input_kind", ["file", "stdin"])
def test_owner_cli_inspect_withdraw_and_exact_replay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    input_kind: str,
) -> None:
    root, source_id, capture_id = _source(tmp_path)
    code, inspection = _cli(root, capsys, "inspect", source_id)
    assert code == 0
    assert inspection["source_id"] == source_id
    assert inspection["head_capture_id"] == capture_id
    assert inspection["lifecycle"] == "active"
    assert inspection["availability"] == "available"
    assert inspection["lifecycle_version"] == 0
    assert inspection["withdrawal_receipt"] is None
    request = _request(inspection)
    payload = json.dumps(request).encode()
    path = tmp_path / "withdraw.json"
    path.write_bytes(payload)
    selected = str(path) if input_kind == "file" else "-"
    receipts = []
    for _ in range(2):
        if input_kind == "stdin":
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
        code, receipt = _cli(root, capsys, "withdraw", "--request-file", selected)
        assert code == 0
        assert receipt["operation_id"] == request["operation_id"]
        assert receipt["source_id"] == source_id
        assert receipt["head_capture_id"] == capture_id
        assert receipt["lifecycle"] == "retired"
        assert receipt["lifecycle_version"] == 1
        assert len(cast(str, receipt["request_sha256"])) == 64
        assert len(cast(str, receipt["receipt_sha256"])) == 64
        receipts.append(receipt)
    assert receipts[0] == receipts[1]
    code, retired = _cli(root, capsys, "inspect", source_id)
    assert code == 0
    assert retired["lifecycle"] == "retired"
    assert retired["availability"] == "missing"
    assert retired["head_capture_id"] == capture_id
    assert retired["lifecycle_version"] == 1
    assert retired["withdrawal_receipt"] == receipts[0]
    profile = compile_single_user_local(root)
    with open_local_database_read_only(profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM source_lifecycle_operations").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_head", "capture_foreign"),
        ("expected_lifecycle_version", 1),
        ("brain_id", "brn_foreign"),
        ("issuer_epoch", 999),
    ],
)
def test_owner_cli_withdraw_refuses_stale_bindings_without_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
) -> None:
    root, source_id, _ = _source(tmp_path)
    _, inspection = _cli(root, capsys, "inspect", source_id)
    request = _request(inspection)
    request[field] = value
    path = tmp_path / "withdraw.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    code, refused = _cli(root, capsys, "withdraw", "--request-file", str(path))
    assert code == 1
    assert cast(dict[str, object], refused["error"])["code"] == "revision_changed"
    assert _cli(root, capsys, "inspect", source_id) == (0, inspection)


def test_owner_cli_withdraw_refuses_altered_operation_replay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, source_id, _ = _source(tmp_path)
    _, inspection = _cli(root, capsys, "inspect", source_id)
    request = _request(inspection)
    path = tmp_path / "withdraw.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert _cli(root, capsys, "withdraw", "--request-file", str(path))[0] == 0
    _, retired = _cli(root, capsys, "inspect", source_id)
    request["reason_code"] = "changed_reason"
    path.write_text(json.dumps(request), encoding="utf-8")
    code, refused = _cli(root, capsys, "withdraw", "--request-file", str(path))
    assert code == 2
    assert cast(dict[str, object], refused["error"])["code"] == "invalid_arguments"
    assert _cli(root, capsys, "inspect", source_id) == (0, retired)


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"[]",
        b"{}",
        b"\xff",
        b'{"dto_version":1,"dto_version":1}',
        b'{"dto_version":NaN}',
        b" " * 16_385,
    ],
)
def test_owner_cli_withdraw_rejects_malformed_files_before_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(payload)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid withdrawal must not select or bootstrap a Brain")

    monkeypatch.setattr(entrypoints, "select_local_root", forbidden)
    code, refused = _cli(tmp_path / "uncreated", capsys, "withdraw", "--request-file", str(path))
    assert code == 2
    assert "error" in refused
    assert not (tmp_path / "uncreated").exists()


def test_owner_cli_withdraw_rejects_missing_file_and_requires_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid withdrawal must not select or bootstrap a Brain")

    monkeypatch.setattr(entrypoints, "select_local_root", forbidden)
    path = tmp_path / "missing.json"
    assert _cli(tmp_path / "uncreated", capsys, "withdraw", "--request-file", str(path))[0] == 2
    assert entrypoints.run_cli(("source", "withdraw", "--request-file", str(path))) == 2
    assert capsys.readouterr().err
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("dto_version", 2),
        pytest.param("dto_version", True, id="dto-version-bool"),
        pytest.param("dto_version", 1.0, id="dto-version-float"),
        ("expected_lifecycle_version", True),
        ("issuer_epoch", False),
        ("absence_evidence_digest", "not-a-digest"),
        ("source_id", "not-a-source"),
        ("owner", True),
    ],
)
def test_owner_cli_withdraw_rejects_invalid_typed_requests_before_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    request = _request(
        {
            "source_id": "source_synthetic",
            "head_capture_id": "capture_synthetic",
            "lifecycle_version": 0,
            "destination_brain_id": "brn_synthetic",
            "issuer_epoch": 1,
        }
    )
    request[field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(request), encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid withdrawal must not select or bootstrap a Brain")

    monkeypatch.setattr(entrypoints, "select_local_root", forbidden)
    code, refused = _cli(tmp_path / "uncreated", capsys, "withdraw", "--request-file", str(path))
    assert code == 2
    assert "error" in refused
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.parametrize("action", ["inspect", "withdraw"])
def test_source_cli_does_not_bypass_owner_authority(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    root, source_id, _ = _source(tmp_path)
    _, inspection = _cli(root, capsys, "inspect", source_id)
    path = tmp_path / "withdraw.json"
    path.write_text(json.dumps(_request(inspection)), encoding="utf-8")
    with monkeypatch.context() as restricted:
        restricted.setattr(
            entrypoints,
            "owner_authority",
            lambda *_args, **_kwargs: EffectiveAuthority(
                "synthetic-agent", "session", frozenset(), None
            ),
        )
        arguments = (
            ("inspect", source_id)
            if action == "inspect"
            else ("withdraw", "--request-file", str(path))
        )
        code, refused = _cli(root, capsys, *arguments)
        assert code == 1
        assert cast(dict[str, object], refused["error"])["code"] == "unsupported_capability"
    assert _cli(root, capsys, "inspect", source_id) == (0, inspection)


def test_owner_cli_inspect_unknown_source_returns_bounded_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, _ = _source(tmp_path)
    code, refused = _cli(root, capsys, "inspect", "source_unknown")
    assert code == 1
    assert cast(dict[str, object], refused["error"])["code"] == "not_found"
