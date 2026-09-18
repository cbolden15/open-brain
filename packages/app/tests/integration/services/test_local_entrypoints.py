from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine import (
    CaptureAction,
    DecisionOutcome,
    ProposalDraft,
    PublicJobCaptureContext,
    TextPayload,
    open_local_engine,
)

import open_brain.services.local_bootstrap as bootstrap_module
import open_brain.services.obsidian_plugin as obsidian_plugin_module
from open_brain.local_data import LocalDataError
from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.local_entrypoints import run_cli


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def _private_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    return home


def _subprocess_cli(root: Path, *arguments: str) -> dict[str, object]:
    program = (
        "from open_brain.services.local_entrypoints import run_cli;"
        "raise SystemExit(run_cli())"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            *arguments,
            "--data-dir",
            str(root),
            "--json",
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stderr == ""
    return cast(dict[str, object], json.loads(result.stdout))


def test_local_help_and_version_are_root_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(("--help",), environment={}) == 0
    help_output = capsys.readouterr().out
    assert "daemonless" in help_output
    for command in (
        "agent",
        "capture",
        "import",
        "search",
        "review",
        "obsidian-plugin",
        "export",
        "status",
        "doctor",
    ):
        assert command in help_output
    assert run_cli(("--version",), environment={}) == 0
    assert capsys.readouterr().out == "open-brain 0.1.0\n"


def test_review_help_explains_the_complete_root_free_workflow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(("review", "--help"), environment={}) == 0
    output = " ".join(capsys.readouterr().out.split())
    for phrase in (
        "explicit capture IDs",
        "routing alone does not publish",
        "review token",
        "target page ID",
        "idempotency key",
        "fresh inspection",
        "Privacy projection",
        "untrusted data",
        "Owner CLI commands need no MCP grants",
        "32 cumulative sources",
        "64 KiB",
        "Homebrew release",
        "three-note example",
    ):
        assert phrase in output


def test_headless_agent_setup_previews_then_applies_the_matching_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    project = home / "project"
    project.mkdir()
    runtime = home / "runtime/open-brain"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o700)
    brain = home / "selected-brain"
    base = (
        "agent",
        "setup",
        "--client",
        "codex",
        "--scope",
        "project",
        "--project-dir",
        str(project),
        "--allow-search",
        "--runtime",
        str(runtime),
        "--data-dir",
        str(brain),
        "--json",
    )

    assert run_cli(base, environment={"HOME": str(home)}, platform_name="darwin") == 0
    preview = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert preview["status"] == "preview"
    assert not brain.exists()

    assert (
        run_cli(
            base + ("--apply", "--preview-id", cast(str, preview["preview_id"])),
            environment={"HOME": str(home)},
            platform_name="darwin",
        )
        == 0
    )
    applied = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert applied["status"] == "configured"
    assert (project / ".codex/config.toml").is_file()
    assert (project / "AGENTS.md").is_file()
    assert not brain.exists()


@pytest.mark.parametrize(
    "arguments",
    (
        ("review", "show", "proposal_invalid"),
        ("review", "list", "--limit", "101"),
        (
            "review",
            "approve",
            "proposal_8d87546c-3008-42ee-8632-0f2401904b35",
            "--review-token",
            "short",
        ),
    ),
)
def test_invalid_review_arguments_do_not_create_a_brain(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
) -> None:
    home = _private_home(tmp_path)
    brain = home / "absent-brain"
    assert (
        run_cli(
            (*arguments, "--data-dir", str(brain), "--json"),
            environment={"HOME": str(home)},
            platform_name="darwin",
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not brain.exists()


@pytest.mark.parametrize("platform_name", ["darwin", "linux"])
def test_owner_review_cli_runs_all_operations_without_mcp_grants(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platform_name: str
) -> None:
    home = _private_home(tmp_path)
    brain = home / "brain"
    tasks = open_local_engine(compile_single_user_local(brain))
    space = tasks.spaces.create_space("Projects", delivery_id="review.cli.space")
    sources = [
        tasks.capture.accept(
            TextPayload(text),
            delivery_id=f"review.cli.capture.{index}",
            space_id=space.space_id,
        ).capture_id
        for index, text in enumerate(("First CLI source", "Second CLI source"))
    ]
    draft = home / "draft.md"
    draft_body = "CLI combined body\n\n- first item\n\tindented code\n"
    draft.write_text(draft_body, encoding="utf-8")

    def run(*arguments: str) -> dict[str, object]:
        assert (
            run_cli(
                (*arguments, "--data-dir", str(brain), "--json"),
                environment={"HOME": str(home)},
                platform_name=platform_name,
                filesystem_type_probe=_filesystem,
            )
            == 0
        )
        return cast(dict[str, object], json.loads(capsys.readouterr().out))

    proposed = run(
        "review",
        "propose",
        "--capture-id",
        sources[0],
        "--capture-id",
        sources[1],
        "--title",
        "CLI combined",
        "--markdown-file",
        str(draft),
        "--idempotency-key",
        "cli-proposal",
    )
    shown = run("review", "show", cast(str, proposed["proposal_id"]))
    assert shown["markdown"] == draft_body.removesuffix("\n")
    assert (
        run_cli(
            ("review", "show", str(proposed["proposal_id"]), "--data-dir", str(brain)),
            environment={"HOME": str(home)},
            platform_name=platform_name,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert draft_body.removesuffix("\n") in capsys.readouterr().out
    approved = run(
        "review",
        "approve",
        cast(str, proposed["proposal_id"]),
        "--review-token",
        cast(str, shown["review_token"]),
        "--idempotency-key",
        "cli-approve",
    )
    assert approved["status"] == "approved"
    assert approved["page_id"] == proposed["page_id"]
    listed = run("review", "list", "--status", "approved")
    assert [row["proposal_id"] for row in cast(list[dict[str, object]], listed["proposals"])] == [
        proposed["proposal_id"]
    ]

    rejected_source = tasks.capture.accept(
        TextPayload("Rejected CLI source"),
        delivery_id="review.cli.capture.rejected",
        space_id=space.space_id,
    ).capture_id
    rejected_proposal = run(
        "review",
        "propose",
        "--capture-id",
        rejected_source,
        "--title",
        "Rejected",
        "--markdown-file",
        str(draft),
    )
    rejected_show = run("review", "show", cast(str, rejected_proposal["proposal_id"]))
    rejected = run(
        "review",
        "reject",
        cast(str, rejected_proposal["proposal_id"]),
        "--review-token",
        cast(str, rejected_show["review_token"]),
    )
    assert rejected["status"] == "rejected" and rejected["publication_id"] is None

    edited_source = tasks.capture.accept(
        TextPayload("Edited CLI source"),
        delivery_id="review.cli.capture.edited",
        space_id=space.space_id,
    ).capture_id
    edited_proposal = run(
        "review",
        "propose",
        "--capture-id",
        edited_source,
        "--title",
        "Edited",
        "--markdown-file",
        str(draft),
    )
    edited_show = run("review", "show", cast(str, edited_proposal["proposal_id"]))
    revised = home / "revised.md"
    revised.write_text("Explicit CLI edit\n", encoding="utf-8")
    edited = run(
        "review",
        "edit-and-approve",
        cast(str, edited_proposal["proposal_id"]),
        "--review-token",
        cast(str, edited_show["review_token"]),
        "--markdown-file",
        str(revised),
    )
    assert edited["status"] == "edited" and edited["publication_id"] is not None


@pytest.mark.parametrize(
    ("platform_name", "expected_relative"),
    (
        ("darwin", "Library/Application Support/open-brain/brain"),
        ("linux", ".local/share/open-brain/brain"),
    ),
)
def test_local_init_creates_exact_default_once_without_daemon_or_environment_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    expected_relative: str,
) -> None:
    home = _private_home(tmp_path)
    forbidden_root = tmp_path / "must-not-be-used"

    def reject_listener(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default bootstrap must not open a listener")

    monkeypatch.setattr(socket.socket, "bind", reject_listener)
    environment = {"HOME": str(home), "OPEN_BRAIN_ROOT": str(forbidden_root)}

    assert (
        run_cli(
            ("init", "--json"),
            environment=environment,
            platform_name=platform_name,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    first = cast(dict[str, object], json.loads(capsys.readouterr().out))
    brain_root = home / expected_relative
    identity = (brain_root / "brain.toml").read_bytes()

    assert first == {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": False,
        "profile": "local",
        "state_schema_version": 7,
        "status": "initialized",
        "storage": "sqlite",
    }
    assert (brain_root / ".open-brain/state/phase1.sqlite3").is_file()
    assert not forbidden_root.exists()

    assert (
        run_cli(
            ("--json", "init"),
            environment=environment,
            platform_name=platform_name,
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    second = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert second["status"] == "already_initialized"
    assert (brain_root / "brain.toml").read_bytes() == identity


def test_local_init_absolute_override_names_brain_root_directly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)
    brain_root = home / "selected"

    assert (
        run_cli(
            ("init", "--data-dir", str(brain_root), "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"
    assert (brain_root / "brain.toml").is_file()
    assert not (brain_root / "brain").exists()


def test_obsidian_plugin_cli_installs_reports_and_removes_owned_assets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    root = home / "brain"
    assets = tmp_path / "plugin-assets"
    assets.mkdir()
    (assets / "main.js").write_text("module.exports = {};\n", encoding="utf-8")
    (assets / "manifest.json").write_text(
        json.dumps(
            {
                "id": "open-brain",
                "isDesktopOnly": True,
                "minAppVersion": "1.12.7",
                "name": "Open Brain",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )
    (assets / "styles.css").write_text(".open-brain {}\n", encoding="utf-8")
    monkeypatch.setattr(
        obsidian_plugin_module,
        "discover_obsidian_plugin_assets",
        lambda: assets,
    )

    def call(arguments: tuple[str, ...]) -> int:
        return run_cli(
            arguments,
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )

    assert call(("workspace", "setup", "--data-dir", str(root), "--json")) == 0
    capsys.readouterr()
    assert call(("obsidian-plugin", "install", "--data-dir", str(root), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "installed"

    assert call(("obsidian-plugin", "status", "--data-dir", str(root), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "current"

    workspace = home / "Open Brain Vault"
    assert not (workspace / ".obsidian/community-plugins.json").exists()
    assert call(("obsidian-plugin", "remove", "--data-dir", str(root), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "removed"


def test_local_init_redacts_private_data_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _private_home(tmp_path)

    assert (
        run_cli(
            ("--json", "--data-dir", "relative", "init"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    assert json.loads(capsys.readouterr().out) == {
        "error": {
            "code": "private_data_directory_unavailable",
            "message": "Open Brain could not use the private data directory.",
        },
        "status": "failed",
    }


def test_identity_revalidation_runs_inside_profile_before_first_identity_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _private_home(tmp_path)
    root = home / "brain"
    original = compile_single_user_local

    def compile_after_replacement(
        selected_root: Path,
        *,
        validate_before_identity_write: object,
    ) -> object:
        selected_root.rename(home / "pinned")
        selected_root.mkdir(mode=0o700)
        assert callable(validate_before_identity_write)
        validate_before_identity_write()
        return original(selected_root)

    monkeypatch.setattr(
        "open_brain.services.local_bootstrap.compile_single_user_local",
        compile_after_replacement,
    )
    selection = importlib.import_module("open_brain.local_data").select_local_root(
        data_dir=str(root),
        environment={"HOME": str(home)},
        platform_name="linux",
    )

    with pytest.raises(LocalDataError, match="changed"):
        bootstrap_module.initialize_local_brain(selection, filesystem_type_probe=_filesystem)
    assert not (root / "brain.toml").exists()
    assert not (home / "pinned/brain.toml").exists()


def test_sqlite_revalidation_runs_at_engine_write_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _private_home(tmp_path)
    root = home / "brain"

    def open_after_replacement(
        _profile: object, *, validate_before_write: object, recover_abandoned_sessions: bool
    ) -> object:
        assert recover_abandoned_sessions is False
        root.rename(home / "pinned")
        root.mkdir(mode=0o700)
        assert callable(validate_before_write)
        validate_before_write()
        raise AssertionError("unreachable")

    monkeypatch.setattr(bootstrap_module, "open_local_engine", open_after_replacement)
    selection = importlib.import_module("open_brain.local_data").select_local_root(
        data_dir=str(root),
        environment={"HOME": str(home)},
        platform_name="linux",
    )

    with pytest.raises(LocalDataError, match="changed"):
        bootstrap_module.initialize_local_brain(selection, filesystem_type_probe=_filesystem)
    assert not (root / ".open-brain/state/phase1.sqlite3").exists()
    assert not (home / "pinned/.open-brain/state/phase1.sqlite3").exists()


def test_exact_local_data_journey_bootstraps_without_init_or_background_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    forbidden_root = tmp_path / "must-not-be-used"
    environment = {"HOME": str(home), "OPEN_BRAIN_ROOT": str(forbidden_root)}
    token = "open-brain-five-minute-acceptance"

    def reject_listener(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default commands must not open a listener")

    monkeypatch.setattr(socket.socket, "bind", reject_listener)

    assert (
        run_cli(
            ("capture", token, "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capture = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert capture["status"] == "captured"
    assert isinstance(capture["capture_id"], str)
    assert token not in json.dumps(capture)

    brain_root = home / ".local/share/open-brain/brain"
    assert (brain_root / "brain.toml").is_file()
    assert (brain_root / ".open-brain/state/phase1.sqlite3").is_file()
    assert not forbidden_root.exists()

    assert (
        run_cli(
            ("search", token),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert token in capsys.readouterr().out

    assert (
        run_cli(
            ("status", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    before_export = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert before_export == {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": False,
        "live_search": {
            "authoritative": True,
            "contents_agree": True,
            "fts_count": 1,
            "identity_count": 1,
            "projection_count": 1,
            "result_ids_agree": True,
            "state": "current",
        },
        "portable_export": "absent",
        "portable_snapshot": {
            "authoritative": False,
            "document_count": 0,
            "freshness": "potentially_stale",
            "generation": None,
            "state": "absent",
        },
        "profile": "local",
        "storage": "sqlite",
    }

    destination = tmp_path / "brain-export"
    assert (
        run_cli(
            ("export", str(destination), "--verify", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    exported = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert exported["status"] == "exported"
    assert exported["verification"] == "verified"
    assert exported["schema_version"] == 4
    assert (destination / "portable-manifest.json").is_file()
    assert any(
        token.encode("utf-8") in path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    )
    assert not any(".open-brain" in path.parts for path in destination.rglob("*"))
    assert not any(path.suffix in {".sqlite", ".sqlite3"} for path in destination.rglob("*"))

    assert (
        run_cli(
            ("status", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    after_export = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert after_export == before_export | {"portable_export": "verified"}

    for check in (
        "private-data-directory",
        "foreground-runtime",
        "base-dependency-closure",
        "search-index",
    ):
        assert (
            run_cli(
                ("doctor", "--check", check),
                environment=environment,
                platform_name="linux",
                filesystem_type_probe=_filesystem,
            )
            == 0
        )
        assert capsys.readouterr().out == f"{check}: ok\n"

    assert not (brain_root / ".open-brain/run/control.sock").exists()


def test_local_search_json_is_bounded_and_export_failure_is_redacted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    token = "synthetic-local-search-token"

    assert (
        run_cli(
            ("--json", "capture", token),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        run_cli(
            ("search", token, "--limit", "1", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    results = cast(list[dict[str, object]], payload["results"])
    assert payload["status"] == "ok"
    assert len(results) == 1
    assert str(results[0]["excerpt"]).replace("[", "").replace("]", "") == token
    assert set(results[0]) == {
        "capture_id",
        "excerpt",
        "payload_family",
        "record_type",
        "result_id",
        "source_origin",
        "title",
        "trust",
        "explanation",
    }

    destination = tmp_path / "conflicting-export"
    destination.mkdir()
    assert (
        run_cli(
            ("export", str(destination), "--verify", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 78
    )
    failure = capsys.readouterr().out
    assert json.loads(failure) == {
        "error": {
            "code": "local_operation_failed",
            "message": "Open Brain could not complete the local command.",
        },
        "status": "failed",
    }
    assert str(destination) not in failure
    assert token not in failure


def test_owner_cli_subprocess_continues_paging_and_unicode_reads_across_invocations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    expected_ids = [
        tasks.capture.accept(
            TextPayload("identical subprocess nebula"),
            delivery_id=f"cli.retrieval.{index}",
        ).capture_id
        for index in range(201)
    ]
    recipe = json.loads(
        (
            Path(__file__).resolve().parents[5]
            / "tests/fixtures/new-user-t03/security-boundaries.json"
        ).read_bytes()
    )["long_text_recipe"]
    unicode_payload = TextPayload(
        "".join(part["text"] * part["repeat"] for part in recipe["parts"])
    )
    unicode_capture = tasks.capture.accept(
        unicode_payload, delivery_id="cli.retrieval.unicode"
    )

    search_arguments = ["search-page", "nebula", "--limit", "100"]
    found: list[str] = []
    page_sizes: list[int] = []
    while True:
        page = _subprocess_cli(root, *search_arguments)
        results = cast(list[dict[str, object]], page["results"])
        page_sizes.append(len(results))
        found.extend(cast(str, row["record_id"]) for row in results)
        if page["complete"]:
            assert page["next_cursor"] is None
            break
        search_arguments = [
            "search-page",
            "nebula",
            "--limit",
            "100",
            "--cursor",
            cast(str, page["next_cursor"]),
        ]
    assert page_sizes == [100, 100, 1]
    assert found == sorted(expected_ids)

    read_arguments = [
        "read",
        unicode_capture.capture_id,
        "--expected-revision-id",
        unicode_capture.capture_id,
        "--target-bytes",
        "32768",
    ]
    chunks: list[str] = []
    offset = 0
    while True:
        response = _subprocess_cli(root, *read_arguments)
        assert response["start_byte"] == offset
        text = cast(str, cast(dict[str, object], response["content"])["text"])
        offset += len(text.encode("utf-8"))
        assert response["end_byte"] == offset
        chunks.append(text)
        if response["complete"]:
            assert response["next_cursor"] is None
            break
        read_arguments = [
            "read",
            unicode_capture.capture_id,
            "--expected-revision-id",
            unicode_capture.capture_id,
            "--target-bytes",
            "32768",
            "--cursor",
            cast(str, response["next_cursor"]),
        ]
    assert "".join(chunks) == unicode_payload.text


def test_owner_cli_reads_complete_validated_imported_file_projection(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    body = "# Imported synthetic\n\nreference-file-nebula 漢字🙂\n"
    (vault / "Imported.md").write_text(body, encoding="utf-8")
    imported = _subprocess_cli(root, "import", str(vault), "--yes")
    assert imported["status"] == "completed"

    page = _subprocess_cli(
        root,
        "search-page",
        "reference-file-nebula",
        "--payload-family",
        "reference_or_file",
        "--record-type",
        "source",
    )
    results = cast(list[dict[str, object]], page["results"])
    assert len(results) == 1
    record = results[0]
    assert record["payload_family"] == "reference_or_file"
    assert record["record_type"] == "source"
    read = _subprocess_cli(
        root,
        "read",
        cast(str, record["record_id"]),
        "--expected-revision-id",
        cast(str, record["revision_id"]),
    )
    assert read["complete"] is True
    text = cast(str, cast(dict[str, object], read["content"])["text"])
    assert text == "Imported.md text/markdown " + body


def test_owner_cli_lists_and_reads_historical_unicode_revision_across_invocations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root, starter_spaces=("Notes",)))
    space = tasks.inbox.spaces()[0]
    source = tasks.capture.accept(
        TextPayload("history source"),
        delivery_id="cli.history.source",
        space_id=space.space_id,
    )
    old_body = "Historical CLI é🙂é 漢字\n" * 2000
    first = tasks.review.propose(
        (source.capture_id,),
        (ProposalDraft("Historical CLI", old_body),),
        delivery_id="cli.history.first",
    )[0]
    approved = tasks.review.decide(
        first.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="cli.history.first.decision",
        expected_review_digest=first.review_digest,
    )
    assert approved.page_id is not None
    second = tasks.review.propose(
        (source.capture_id,),
        (ProposalDraft("Current CLI", "Current CLI body"),),
        delivery_id="cli.history.second",
        target_page_id=approved.page_id,
    )[0]
    tasks.review.decide(
        second.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="cli.history.second.decision",
        expected_review_digest=second.review_digest,
    )

    first_page = _subprocess_cli(root, "history", "list", approved.page_id, "--limit", "1")
    current = cast(list[dict[str, object]], first_page["entries"])[0]
    assert current["is_current"] is True
    assert first_page["complete"] is False
    tail = _subprocess_cli(
        root,
        "history",
        "list",
        approved.page_id,
        "--limit",
        "1",
        "--cursor",
        cast(str, first_page["next_cursor"]),
    )
    historical = cast(list[dict[str, object]], tail["entries"])[0]
    assert historical["is_current"] is False
    assert current["predecessor_revision_id"] == historical["revision_id"]

    base_arguments = [
        "history",
        "show",
        approved.page_id,
        "--expected-revision-id",
        cast(str, historical["revision_id"]),
        "--target-bytes",
        "32768",
    ]
    arguments = base_arguments
    chunks: list[str] = []
    while True:
        response = _subprocess_cli(root, *arguments)
        chunks.append(cast(str, cast(dict[str, object], response["content"])["text"]))
        if response["complete"]:
            break
        arguments = [*base_arguments, "--cursor", cast(str, response["next_cursor"])]
    reconstructed = "".join(chunks)
    assert TextPayload(old_body).text in reconstructed
    assert "Current CLI body" not in reconstructed


def test_owner_cli_relationship_replay_cas_history_and_verified_export(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    captures = [
        tasks.capture.accept(
            TextPayload(f"independent relationship endpoint {index}"),
            delivery_id=f"cli.relationship.{index}",
        )
        for index in range(2)
    ]
    left, right = (capture.capture_id for capture in captures)
    accept_arguments = [
        "relationship",
        "decide",
        "--left-record-id",
        left,
        "--left-revision-id",
        left,
        "--right-record-id",
        right,
        "--right-revision-id",
        right,
        "--kind",
        "duplicate_of",
        "--decision",
        "accept",
        "--expected-relationship-version",
        "0",
        "--operation-id",
        "operation_11111111-1111-4111-8111-111111111111",
    ]
    accepted = _subprocess_cli(root, *accept_arguments)
    assert accepted["version"] == 1
    assert _subprocess_cli(root, *accept_arguments) == accepted

    program = (
        "from open_brain.services.local_entrypoints import run_cli;"
        "raise SystemExit(run_cli())"
    )
    stale = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            *accept_arguments[:-1],
            "operation_22222222-2222-4222-8222-222222222222",
            "--data-dir",
            str(root),
            "--json",
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert stale.returncode == 1
    assert json.loads(stale.stdout)["error"]["code"] == "revision_changed"
    assert stale.stderr == ""

    removed = _subprocess_cli(
        root,
        *[
            "1" if value == "0" else "remove" if value == "accept" else value
            for value in accept_arguments
        ][:-1],
        "operation_33333333-3333-4333-8333-333333333333",
    )
    assert removed["relationship_id"] == accepted["relationship_id"]
    assert removed["version"] == 2

    relationships = _subprocess_cli(root, "relationship", "list", left)
    entry = cast(list[dict[str, object]], relationships["entries"])[0]
    assert entry["status"] == "removed"
    assert {cast(dict[str, object], entry[side])["record_id"] for side in ("left", "right")} == {
        left,
        right,
    }

    first_decision = _subprocess_cli(root, "decision", "history", left, "--limit", "1")
    first_entry = cast(list[dict[str, object]], first_decision["entries"])[0]
    assert first_entry["decision"] == "remove"
    tail = _subprocess_cli(
        root,
        "decision",
        "history",
        left,
        "--limit",
        "1",
        "--cursor",
        cast(str, first_decision["next_cursor"]),
    )
    assert cast(list[dict[str, object]], tail["entries"])[0]["decision"] == "accept"
    assert tail["complete"] is True

    search = _subprocess_cli(root, "search-page", "independent relationship endpoint")
    assert {row["record_id"] for row in cast(list[dict[str, object]], search["results"])} == {
        left,
        right,
    }
    export = tmp_path / "export"
    receipt = _subprocess_cli(root, "export", str(export), "--verify")
    assert receipt["verification"] == "verified"
    sidecar = json.loads(
        (export / "history/relationships/decisions-v1.json").read_text(encoding="utf-8")
    )
    assert len(sidecar["relationships"]) == 1
    assert [decision["decision"] for decision in sidecar["decisions"]] == [
        "accept",
        "remove",
    ]

def test_local_search_reconciles_owner_markdown_and_renders_one_safe_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    assert (
        run_cli(
            ("init", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    root = home / ".local/share/open-brain/brain"
    tasks = open_local_engine(open_existing_single_user_local(root))
    space = tasks.spaces.create_space("Notes", delivery_id="search.cli.space")
    tasks.capture.accept(
        TextPayload("Original owner search body"),
        delivery_id="search.cli.canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
        title="Owner note",
    )
    page = next((root / "content/spaces").rglob("page_*.md"))
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "Original owner search body",
            "Fresh owner search body",
        ),
        encoding="utf-8",
    )

    assert (
        run_cli(
            ("search", "Fresh owner search"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    output = capsys.readouterr().out

    assert len(output.splitlines()) == 2
    assert "Owner note" in output
    assert "owner" in output
    assert "owner_authored" in output
    assert all(
        not (ord(character) < 32 or 0x7F <= ord(character) <= 0x9F)
        and unicodedata.category(character) != "Cf"
        for line in output.splitlines()
        for character in line
    )


def test_local_search_renders_owner_and_unknown_automation_labels_in_both_formats(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    assert (
        run_cli(
            ("init", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    root = home / ".local/share/open-brain/brain"
    tasks = open_local_engine(open_existing_single_user_local(root))
    tasks.capture.accept(
        TextPayload("Shared trust canary owner"),
        delivery_id="search.cli.trust.owner",
    )
    actor_id = "actor_00000000-0000-4000-8000-000000000101"
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000102",
            "role_id": "role_00000000-0000-4000-8000-000000000103",
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    source_reference = "urn:synthetic:private-automation-reference"
    tasks.capture.public_job_sink(context).submit(
        TextPayload("Shared trust canary automation"),
        delivery_id="search.cli.trust.unknown",
        source_origin=ContentOrigin.UNKNOWN,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=ContentOrigin.UNKNOWN,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL,
            reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )

    assert (
        run_cli(
            ("search", "Shared trust canary", "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    json_output = capsys.readouterr().out
    payload = cast(dict[str, object], json.loads(json_output))
    results = cast(list[dict[str, object]], payload["results"])
    labels = {cast(str, result["source_origin"]): cast(str, result["trust"]) for result in results}

    assert labels == {"owner_authored": "owner", "unknown": "unverified"}
    assert source_reference not in json_output

    assert (
        run_cli(
            ("search", "Shared trust canary"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    human_output = capsys.readouterr().out

    assert "[owner | owner_authored]" in human_output
    assert "[unverified | unknown]" in human_output
    assert source_reference not in human_output


def test_local_search_human_output_removes_terminal_and_bidi_controls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _private_home(tmp_path)
    environment = {"HOME": str(home)}
    unsafe = "Visible unsafe \x1b]0;title\x07\rline \u202eoverride \u2066isolate"
    assert (
        run_cli(
            ("capture", unsafe, "--json"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()

    assert (
        run_cli(
            ("search", "override isolate"),
            environment=environment,
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    output = capsys.readouterr().out

    assert len(output.splitlines()) == 1
    assert "Visible" in output
    assert "title" in output
    assert "override" in output
    assert "isolate" in output
    assert all(
        not (ord(character) < 32 or 0x7F <= ord(character) <= 0x9F)
        and unicodedata.category(character) != "Cf"
        for character in output.rstrip("\n")
    )


@pytest.mark.parametrize(
    ("poisoned_distribution", "poisoned_requirement"),
    (
        ("open-brain", "starlette>=0.48,<1"),
        ("open-brain-engine", "cryptography>=50,<51"),
        ("rfc8785", "keyring>=25.6,<26"),
    ),
)
def test_local_dependency_doctor_rejects_an_unconditional_secure_node_dependency(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    poisoned_distribution: str,
    poisoned_requirement: str,
) -> None:
    home = _private_home(tmp_path)

    expected = {
        "open-brain": ["open-brain-engine==0.1.0"],
        "open-brain-engine": ["rfc8785<0.2,>=0.1.4"],
        "rfc8785": [],
    }

    def poisoned_requirements(distribution: str) -> list[str]:
        requirements = list(expected[distribution])
        if distribution == poisoned_distribution:
            requirements.append(poisoned_requirement)
        return requirements

    monkeypatch.setattr(
        importlib.metadata,
        "requires",
        poisoned_requirements,
    )

    assert (
        run_cli(
            ("doctor", "--check", "base-dependency-closure", "--json"),
            environment={"HOME": str(home)},
            platform_name="linux",
            filesystem_type_probe=_filesystem,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "check": "base-dependency-closure",
        "status": "failed",
    }


@pytest.mark.parametrize(
    ("arguments", "json_output", "private_values"),
    (
        (("private-command",), False, ("private-command",)),
        (("capture",), False, ()),
        (
            ("capture", "synthetic-private-text", "synthetic-extra"),
            False,
            ("synthetic-private-text", "synthetic-extra"),
        ),
        (
            ("--json", "capture", "synthetic-private-text", "synthetic-extra"),
            True,
            ("synthetic-private-text", "synthetic-extra"),
        ),
        (
            ("capture", "synthetic-private-text", "synthetic-extra", "--json"),
            True,
            ("synthetic-private-text", "synthetic-extra"),
        ),
        (
            ("doctor", "--check", "synthetic-private-check", "--json"),
            True,
            ("synthetic-private-check",),
        ),
        (
            ("export", "/synthetic/private/export", "synthetic-extra"),
            False,
            ("/synthetic/private/export", "synthetic-extra"),
        ),
    ),
)
def test_local_usage_failures_are_bounded_and_redacted(
    arguments: tuple[str, ...],
    json_output: bool,
    private_values: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(arguments, environment={}) == 2
    output = capsys.readouterr()
    if json_output:
        assert output.err == ""
        assert json.loads(output.out) == {
            "error": {
                "code": "invalid_command",
                "message": "Open Brain could not parse the command.",
            },
            "status": "failed",
        }
    else:
        assert output.out == ""
        assert output.err == "Open Brain could not parse the command.\n"
    for private_value in private_values:
        assert private_value not in output.out
        assert private_value not in output.err
