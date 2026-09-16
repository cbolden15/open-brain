from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from open_brain_engine.engine import CaptureAction, ReferencePayload, TextPayload, open_local_engine
from open_brain_engine.engine.contracts import InboxItem, InboxSpaceTask, SpaceRecord

import open_brain.services.local_entrypoints as entrypoints
from open_brain.profile import compile_single_user_local
from open_brain.services.space_inbox import (
    SpaceInboxError,
    SpaceInboxService,
    validate_space_inbox_arguments,
)


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _cli(root: Path, *arguments: str) -> int:
    return entrypoints.run_cli(
        ("--data-dir", str(root), "--json", *arguments),
        environment={"HOME": str(root.parent)},
        platform_name="darwin", filesystem_type_probe=_filesystem,
    )


def test_cli_organization_survives_restart_and_preserves_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"

    def run(*arguments: str) -> dict[str, Any]:
        assert _cli(root, *arguments) == 0
        return cast(dict[str, Any], json.loads(capsys.readouterr().out))

    assert run("space", "list")["spaces"] == []
    capture = run("capture", "Synthetic lunar mission reference")
    capture_id = capture["capture_id"]
    source = next((root / "sources/captures").rglob("*.json"))
    original = source.read_bytes()
    prior_search = run("search", "lunar")["results"][0]
    inbox = run("inbox", "list", "--unassigned")
    assert [item["capture_id"] for item in inbox["items"]] == [capture_id]
    assert inbox["items"][0]["preview"] == "Synthetic lunar mission reference"
    created = run("space", "create", "Astronomy", "--idempotency-key", "create-mission")
    assert run("space", "create", "Astronomy", "--idempotency-key", "create-mission") == created
    space_id = created["space"]["space_id"]
    routed = run("inbox", "route", capture_id, space_id, "--idempotency-key", "route-mission")
    assert (
        run("inbox", "route", capture_id, space_id, "--idempotency-key", "route-mission") == routed
    )
    renamed = run("space", "rename", space_id, "Spaceflight", "--idempotency-key", "rename-mission")
    assert renamed["space"]["space_id"] == space_id
    assert renamed["space"]["name"] == "Spaceflight"
    assert run("inbox", "list", "--unassigned")["items"] == []
    assert run("inbox", "list")["items"][0]["space_id"] == space_id
    after_search = run("search", "lunar")["results"][0]
    tasks = open_local_engine(compile_single_user_local(root))
    assert tasks.retrieval.search("lunar")[0].space_id == space_id
    for key in ("capture_id", "record_type", "trust", "source_origin", "excerpt"):
        assert after_search[key] == prior_search[key]
    assert source.read_bytes() == original
    assert [path.name for path in (root / "content/spaces").rglob("*.md")] == ["_space.md"]
    assert len(tuple((root / "history/routes").rglob("*.json"))) == 1
    assert _cli(root, "space", "create", "Different", "--idempotency-key", "create-mission") == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "idempotency_conflict"
    assert len(run("space", "list")["spaces"]) == 1


def test_lists_page_filter_and_redact_previews(tmp_path: Path) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    service = SpaceInboxService(tasks.spaces)
    ids = []
    for index in range(3):
        ids.append(tasks.capture.accept(
            TextPayload(f"Synthetic record {index} /private/example/file "
                        + "token" + "=synthetic-secret "
                        + "x" * 600), delivery_id=f"delivery.page.{index}",
        ).capture_id)
    first = service.inbox_list({"limit": 2})
    second = service.inbox_list({"limit": 2, "offset": first["next_offset"]})
    rows = cast(list[dict[str, Any]], first["items"]) + cast(list[dict[str, Any]], second["items"])
    assert [row["capture_id"] for row in rows] == ids
    assert second["next_offset"] is None
    assert all(len(row["preview"]) <= 320 for row in rows)
    assert "synthetic-secret" not in json.dumps(rows)
    assert "/private/example" not in json.dumps(rows)
    spaces = [tasks.spaces.create_space(name, delivery_id=f"space.{name}")
              for name in ("Zulu", "Alpha", "Middle")]
    page = cast(list[dict[str, object]], service.space_list({"limit": 2})["spaces"])
    assert [space["name"] for space in page] == [
        "Alpha", "Middle"
    ]
    assert service.space_list({"limit": 2, "offset": 2})["next_offset"] is None
    tasks.spaces.route(ids[0], spaces[0].space_id, delivery_id="route.page")
    unassigned = service.inbox_list({"unassigned_only": True, "limit": 1, "offset": 1})
    assert cast(list[dict[str, object]], unassigned["items"])[0]["capture_id"] == ids[2]
    assert unassigned["next_offset"] is None
    assert service.inbox_list({"offset": 99})["items"] == []


@pytest.mark.parametrize("suffix", ["canary", "long-canary" * 60], ids=["short", "long"])
def test_inbox_redacts_source_reference_before_truncating(tmp_path: Path, suffix: str) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    reference = "https://synthetic.invalid/" + suffix
    tasks.capture.accept(
        ReferencePayload(reference, "Synthetic reference body"), delivery_id="reference.inbox",
    )
    result = SpaceInboxService(tasks.spaces).inbox_list({})
    output = json.dumps(result)
    assert "synthetic.invalid" not in output
    assert "canary" not in output
    assert "Synthetic reference body" in output


@pytest.mark.parametrize("operation", ["inbox_list", "space_list"])
def test_maximum_offset_reports_cap_without_unusable_cursor(operation: str) -> None:
    task = Mock(spec=InboxSpaceTask)
    task.list.return_value = (
        InboxItem("capture_" + str(uuid.uuid4()), "text", "inbox", None, None, None),
    ) * 101
    task.spaces.return_value = (
        SpaceRecord("space_" + str(uuid.uuid4()), "Name", "opaque"),
    ) * 101
    result = getattr(SpaceInboxService(task), operation)({"limit": 100, "offset": 1_000_000})
    assert result["next_offset"] is None
    assert result["offset_limit_reached"] is True


@pytest.mark.parametrize("arguments", [
    ("space", "create", " "),
    ("space", "create", "x" * 121),
    ("space", "create", "Name\x1b[31m"),
    ("space", "rename", "invalid-id", "Name"),
    ("space", "list", "--limit", "101"),
    ("inbox", "list", "--offset", "-1"),
    ("inbox", "list", "--offset", "1000001"),
    ("inbox", "route", "invalid-capture", "invalid-space"),
    ("space", "create", "Name", "--idempotency-key", ""),
    ("inbox",),
])
def test_invalid_cli_arguments_do_not_create_a_brain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arguments: tuple[str, ...]
) -> None:
    root = tmp_path / "absent"
    assert _cli(root, *arguments) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not root.exists()


@pytest.mark.parametrize(("operation", "arguments"), [
    ("inbox_list", {"limit": True}),
    ("space_list", {"offset": False}),
    ("inbox_list", {"unassigned_only": 1}),
    ("space_list", {"limit": None}),
    ("inbox_list", {"unexpected": "value"}),
    ("space_create", {"name": "Name", "idempotency_key": None}),
    ("space_create", {"name": "Name", "idempotency_key": "\ud800"}),
    ("space_create", {"name": "\x00"}),
    ("space_create", {"name": "\ud800"}),
    ("space_rename", {"name": "Name"}),
    ("inbox_route", {"capture_id": "capture_" + str(uuid.UUID(int=0)),
                     "space_id": "space_" + str(uuid.uuid4())}),
])
def test_shared_validator_rejects_invalid_tool_values(
    operation: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(SpaceInboxError, match="invalid_arguments"):
        validate_space_inbox_arguments(operation, arguments)


def test_route_errors_and_reassignment_keep_history(tmp_path: Path) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    service = SpaceInboxService(tasks.spaces)
    a = tasks.spaces.create_space("Alpha", delivery_id="space.alpha")
    b = tasks.spaces.create_space("Beta", delivery_id="space.beta")
    capture = tasks.capture.accept(TextPayload("Synthetic assignment"), delivery_id="capture.route")
    first = {"capture_id": capture.capture_id, "space_id": a.space_id, "idempotency_key": "route"}
    service.inbox_route(first)
    with pytest.raises(SpaceInboxError, match="idempotency_conflict"):
        service.inbox_route({**first, "space_id": b.space_id})
    service.inbox_route({**first, "space_id": b.space_id, "idempotency_key": "route-new"})
    service.inbox_route(first)  # Old retries do not undo a later assignment.
    assert tasks.spaces.list()[0].space_id == b.space_id
    with pytest.raises(SpaceInboxError, match="unknown_space"):
        service.space_rename({"space_id": "space_" + str(uuid.uuid4()), "name": "Name"})
    with pytest.raises(SpaceInboxError, match="unknown_route_target"):
        service.inbox_route({"capture_id": "capture_" + str(uuid.uuid4()), "space_id": a.space_id})
    published = tasks.capture.accept(
        TextPayload("Synthetic published note"), delivery_id="capture.published",
        action=CaptureAction.CANONICAL_NOTE, space_id=a.space_id,
    )
    with pytest.raises(SpaceInboxError, match="published_capture"):
        service.inbox_route({"capture_id": published.capture_id, "space_id": b.space_id})


def test_cli_busy_errors_are_retryable_and_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*_args: object, **_kwargs: object) -> Any:
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(entrypoints, "open_local_brain", busy)
    assert _cli(tmp_path / "brain", "space", "create", "Private-name") == 75
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "database_busy"
    assert "Private-name" not in output


def test_human_output_is_safe_and_options_work_at_each_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    assert _cli(root, "space", "create", "Synthetic\u202e\nName") == 0
    capsys.readouterr()
    for args in (
        ("--data-dir", str(root), "space", "list"),
        ("space", "--data-dir", str(root), "list"),
        ("space", "list", "--data-dir", str(root)),
    ):
        assert entrypoints.run_cli(
            args, environment={"HOME": str(root.parent)},
            platform_name="darwin", filesystem_type_probe=_filesystem,
        ) == 0
        output = capsys.readouterr().out
        assert "Synthetic" in output and "Name" in output
        assert "\x1b" not in output
        assert "\u202e" not in output
        assert len(output.splitlines()) == 1
