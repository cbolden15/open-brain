from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import BrainEngine, ProposalDraft, ReferencePayload, TextPayload
from open_brain_engine.engine.contracts import (
    DaemonMutationPathUnavailableError,
    MutationAuthorityOwner,
    MutationTransport,
)

from open_brain.services.appliance_init import initialize_appliance
from open_brain.services.phase1_application import SingleUserLocalApplication
from open_brain_legacy._compat.open_brain.cli._common import CommandAdapterLookup, ExitCode
from open_brain_legacy._compat.open_brain.cli.phase1 import build_phase1_command_adapters
from open_brain_legacy._compat.open_brain.integrations.phase1_ui import (
    Phase1UiHandler,
    Phase1UiRequest,
    Phase1UiResponse,
)
from open_brain_legacy._compat.open_brain.profile import compile_single_user_local
from open_brain_legacy._compat.open_brain.services.runtime import (
    RESERVED_APPLIANCE_APPLICATION_MODULE,
    RESERVED_APPLIANCE_ENTRYPOINT_MODULE,
)
from open_brain_legacy.cli.main import main

TOKEN = "synthetic-ui-token"
AUTHORIZATION = (("Authorization", f"Bearer {TOKEN}"),)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_CHECKOUT_PATHS = [
    str(REPOSITORY_ROOT / "src"),
    str(REPOSITORY_ROOT / "packages" / "engine" / "src"),
]


def _source_checkout_module_command(module: str, *arguments: str) -> tuple[str, ...]:
    program = (
        "import runpy, sys; "
        f"sys.path[:0] = {SOURCE_CHECKOUT_PATHS!r}; "
        f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)"
    )
    return (sys.executable, "-I", "-B", "-c", program, *arguments)


def _source_checkout_secure_node_command(*arguments: str) -> tuple[str, ...]:
    program = (
        "import sys; "
        f"sys.path[:0] = {SOURCE_CHECKOUT_PATHS!r}; "
        "from open_brain.services.secure_node_entrypoints import run_cli; "
        "raise SystemExit(run_cli(sys.argv[1:]))"
    )
    return (sys.executable, "-I", "-B", "-c", program, *arguments)


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root))


def _cli(
    capsys: pytest.CaptureFixture[str],
    adapters: CommandAdapterLookup,
    *argv: str,
) -> tuple[ExitCode, dict[str, object]]:
    exit_code = ExitCode(main([*argv, "--json"], command_adapters=adapters))
    output = json.loads(capsys.readouterr().out)
    assert isinstance(output, dict)
    return exit_code, output


def _ui(
    handler: Phase1UiHandler,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    *,
    headers: tuple[tuple[str, str], ...] = AUTHORIZATION,
) -> tuple[Phase1UiResponse, dict[str, object]]:
    response = handler.handle(
        Phase1UiRequest(
            method=method,
            path=path,
            headers=headers,
            body=b"" if body is None else canonical_json_bytes(body),
        )
    )
    value = json.loads(response.body)
    assert isinstance(value, dict)
    return response, value


def test_cli_and_ui_share_capture_space_routing_and_retrieval_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _engine(tmp_path / "brain")
    adapters = build_phase1_command_adapters(engine.tasks.phase1)
    ui = Phase1UiHandler(expected_bearer_token=TOKEN, tasks=engine.tasks.phase1)

    exit_code, created = _cli(
        capsys,
        adapters,
        "spaces",
        "create",
        "Surface space",
        "--delivery=surface.space.create",
    )
    space_id = str(created["space_id"])
    ui_spaces_response, ui_spaces = _ui(ui, "GET", "/api/spaces")
    ui_space_items = cast(list[dict[str, object]], ui_spaces["spaces"])
    assert exit_code is ExitCode.SUCCESS
    assert ui_spaces_response.status == 200
    assert [space["space_id"] for space in ui_space_items] == [space_id]

    shared_text = "Shared surface lexical token"
    _, quick = _cli(
        capsys,
        adapters,
        "capture",
        "quick",
        "text",
        shared_text,
        "--delivery=surface.capture.quick",
    )
    quick_id = str(quick["capture_id"])
    inbox_response, inbox = _ui(ui, "GET", "/api/inbox")
    inbox_items = cast(list[dict[str, object]], inbox["captures"])
    assert inbox_response.status == 200
    assert [item["capture_id"] for item in inbox_items] == [quick_id]

    canonical_response, canonical = _ui(
        ui,
        "POST",
        "/api/captures/canonical",
        {
            "delivery_id": "surface.capture.canonical",
            "space_id": space_id,
            "text": shared_text,
        },
    )
    canonical_id = str(canonical["capture_id"])
    _, query = _cli(capsys, adapters, "query", "lexical")
    query_items = cast(list[dict[str, object]], query["results"])
    query_capture_ids = {item["capture_id"] for item in query_items}
    assert canonical_response.status == 200
    assert canonical["canonical"] is True
    assert {quick_id, canonical_id} <= query_capture_ids
    assert all("explanation" in item and "provenance" in item for item in query_items)

    route_response, routed = _ui(
        ui,
        "POST",
        f"/api/captures/{quick_id}/route",
        {"delivery_id": "surface.capture.route", "space_id": space_id},
    )
    _, cli_inbox = _cli(capsys, adapters, "inbox", "list")
    listed = cli_inbox["captures"]
    assert isinstance(listed, list)
    assert route_response.status == 200
    assert routed == {"capture_id": quick_id, "space_id": space_id, "status": "routed"}
    assert listed[0]["capture_id"] == quick_id
    assert listed[0]["space_id"] == space_id

    dashboard = ui.handle(Phase1UiRequest("GET", "/", AUTHORIZATION))
    assert dashboard.status == 200
    assert quick_id.encode("utf-8") in dashboard.body
    assert canonical_id not in dashboard.body.decode("utf-8")


def test_phase3_appliance_control_plane_is_reserved_and_fails_closed(
    tmp_path: Path,
) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")

    control_plane = application.appliance_control_plane()

    assert control_plane.application_module == RESERVED_APPLIANCE_APPLICATION_MODULE
    assert control_plane.entrypoint_module == RESERVED_APPLIANCE_ENTRYPOINT_MODULE
    assert control_plane.cli_entrypoint == "open_brain.services.appliance_entrypoints:run_cli"
    assert control_plane.http_entrypoint == "open_brain.services.appliance_entrypoints:run_http"
    assert control_plane.mcp_entrypoint == "open_brain.services.appliance_entrypoints:run_mcp"
    assert control_plane.daemon_mutation_path.owner is MutationAuthorityOwner.APPLIANCE_DAEMON
    assert (
        control_plane.daemon_mutation_path.transport is MutationTransport.UNIX_DOMAIN_SOCKET
    )
    assert (
        control_plane.daemon_mutation_path.socket_path
        == tmp_path / "brain" / ".open-brain" / "run" / "control.sock"
    )
    with pytest.raises(
        DaemonMutationPathUnavailableError,
        match="daemon-only mutation path is reserved",
    ):
        control_plane.daemon_mutation_path.open()


def test_cli_and_ui_accept_every_generic_payload_family_through_the_same_engine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _engine(tmp_path / "brain")
    adapters = build_phase1_command_adapters(engine.tasks.phase1)
    ui = Phase1UiHandler(expected_bearer_token=TOKEN, tasks=engine.tasks.phase1)
    cli_requests = (
        ("text", "Synthetic CLI text"),
        (
            "reference",
            "https://example.test/cli-reference",
            "--supplied-text=Synthetic CLI reference",
        ),
        (
            "file",
            "cli.txt",
            "--media-type=text/plain",
            "--data-base64=" + base64.b64encode(b"Synthetic CLI file").decode("ascii"),
        ),
        (
            "event",
            "synthetic.cli",
            '--attributes-json={"label":"Synthetic CLI event"}',
        ),
        (
            "measurement",
            "42",
            "--unit=count",
            '--dimensions-json={"label":"Synthetic CLI measurement"}',
        ),
    )
    cli_ids: set[str] = set()
    for index, request in enumerate(cli_requests):
        exit_code, result = _cli(
            capsys,
            adapters,
            "capture",
            "quick",
            *request,
            f"--delivery=surface.cli.payload.{index}",
        )
        assert exit_code is ExitCode.SUCCESS
        cli_ids.add(str(result["capture_id"]))

    ui_payloads: tuple[dict[str, object], ...] = (
        {"family": "text", "text": "Synthetic UI text"},
        {
            "family": "reference_or_file",
            "kind": "reference",
            "supplied_text": "Synthetic UI reference",
            "url": "https://example.test/ui-reference",
        },
        {
            "data_base64": base64.b64encode(b"Synthetic UI file").decode("ascii"),
            "family": "reference_or_file",
            "file_name": "ui.txt",
            "kind": "file",
            "media_type": "text/plain",
        },
        {
            "attributes": {"label": "Synthetic UI event"},
            "event_type": "synthetic.ui",
            "family": "event",
        },
        {
            "dimensions": {"label": "Synthetic UI measurement"},
            "family": "measurement",
            "unit": "count",
            "value": "43",
        },
    )
    ui_ids: set[str] = set()
    for index, payload in enumerate(ui_payloads):
        response, result = _ui(
            ui,
            "POST",
            "/api/captures/quick",
            {"delivery_id": f"surface.ui.payload.{index}", "payload": payload},
        )
        assert response.status == 200
        ui_ids.add(str(result["capture_id"]))

    _, inbox = _cli(capsys, adapters, "inbox", "list")
    listed = cast(list[dict[str, object]], inbox["captures"])
    assert cli_ids | ui_ids == {str(item["capture_id"]) for item in listed}
    assert {str(item["payload_family"]) for item in listed} == {
        "event",
        "measurement",
        "reference_or_file",
        "text",
    }


def test_cli_and_ui_serialize_only_public_retrieval_provenance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _engine(tmp_path / "brain")
    adapters = build_phase1_command_adapters(engine.tasks.phase1)
    ui = Phase1UiHandler(expected_bearer_token=TOKEN, tasks=engine.tasks.phase1)
    private_source = "https://example.test/private-engine-source"
    capture = engine.capture.accept(
        ReferencePayload(private_source, "Synthetic source-safe surface token"),
        delivery_id="surface.safe-provenance",
    )

    _, cli = _cli(capsys, adapters, "query", "source-safe")
    response, ui_value = _ui(ui, "GET", "/api/search?q=source-safe")
    cli_item = cast(list[dict[str, object]], cli["results"])[0]
    ui_item = cast(list[dict[str, object]], ui_value["results"])[0]

    expected = {
        "capture_id": capture.capture_id,
        "source_origin": "third_party",
        "source_record_id": capture.capture_id,
    }
    assert response.status == 200
    assert cli_item["provenance"] == expected
    assert ui_item["provenance"] == expected
    assert private_source not in json.dumps({"cli": cli, "ui": ui_value})
    assert "source_ref" not in json.dumps({"cli": cli, "ui": ui_value})
    assert "sha256" not in json.dumps({"cli": cli, "ui": ui_value})


def test_cli_and_ui_each_approve_reject_and_edit_with_shared_terminal_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _engine(tmp_path / "brain")
    adapters = build_phase1_command_adapters(engine.tasks.phase1)
    ui = Phase1UiHandler(expected_bearer_token=TOKEN, tasks=engine.tasks.phase1)
    space = engine.inbox.create_space("Review", delivery_id="surface.review.space")
    capture = engine.capture.accept(
        TextPayload("Synthetic six-way surface review"),
        delivery_id="surface.review.capture",
        space_id=space.space_id,
    )
    proposals = engine.review.propose(
        capture.capture_id,
        tuple(
            ProposalDraft(f"Meaning {index}", f"Synthetic meaning {index}")
            for index in range(6)
        ),
        delivery_id="surface.review.proposals",
    )

    cli_results: list[dict[str, object]] = []
    for action, proposal, suffix, edited in (
        ("approve", proposals[0], "approve", None),
        ("reject", proposals[1], "reject", None),
        ("edit", proposals[2], "edit", "CLI safely edited meaning"),
    ):
        argv = [
            "review",
            action,
            proposal.proposal_id,
            *(() if edited is None else (edited,)),
            f"--delivery=surface.cli.{suffix}",
        ]
        exit_code, result = _cli(capsys, adapters, *argv)
        assert exit_code is ExitCode.SUCCESS
        cli_results.append(result)

    ui_results: list[dict[str, object]] = []
    for outcome, proposal, suffix, edited in (
        ("approved", proposals[3], "approve", None),
        ("rejected", proposals[4], "reject", None),
        ("edited", proposals[5], "edit", "UI safely edited meaning"),
    ):
        body: dict[str, object] = {
            "delivery_id": f"surface.ui.{suffix}",
            "outcome": outcome,
        }
        if edited is not None:
            body["edited_markdown"] = edited
        response, result = _ui(
            ui,
            "POST",
            f"/api/proposals/{proposal.proposal_id}/decision",
            body,
        )
        assert response.status == 200
        ui_results.append(result)

    _, cli_list = _cli(capsys, adapters, "proposals", "list")
    ui_list_response, ui_list = _ui(ui, "GET", "/api/proposals")
    cli_proposals = cast(list[dict[str, object]], cli_list["proposals"])
    ui_proposals = cast(list[dict[str, object]], ui_list["proposals"])
    cli_terminal = {
        item["proposal_id"]: (item["decision_id"], item["state"])
        for item in cli_proposals
    }
    ui_terminal = {
        item["proposal_id"]: (item["decision_id"], item["state"])
        for item in ui_proposals
    }
    expected_ids = {
        str(result["proposal_id"]): str(result["decision_id"])
        for result in (*cli_results, *ui_results)
    }
    assert ui_list_response.status == 200
    assert cli_terminal == ui_terminal
    assert {proposal_id: value[0] for proposal_id, value in cli_terminal.items()} == expected_ids
    assert {value[1] for value in cli_terminal.values()} == {"approved", "rejected", "edited"}


def test_ui_authenticates_before_mutating_or_parsing_private_body(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    ui = Phase1UiHandler(expected_bearer_token=TOKEN, tasks=engine.tasks.phase1)
    private_body = b'{"delivery_id":"unauthorized","text":"synthetic private body"}'

    unauthorized = ui.handle(
        Phase1UiRequest("POST", "/api/captures/quick", (), private_body)
    )
    duplicated = ui.handle(
        Phase1UiRequest(
            "POST",
            "/api/captures/quick",
            (AUTHORIZATION[0], AUTHORIZATION[0]),
            private_body,
        )
    )

    assert unauthorized.status == 401
    assert duplicated.status == 401
    assert engine.inbox.list() == ()
    assert b"synthetic private body" not in unauthorized.body


def test_source_checkout_cli_uses_one_brain_root_across_processes(tmp_path: Path) -> None:
    del tmp_path
    with tempfile.TemporaryDirectory(prefix="ob-", dir=Path("/tmp").resolve()) as directory:
        root = Path(directory) / "brain"
        initialize_appliance(root, starter_spaces=())
        environment = {
            key: os.environ[key]
            for key in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "UV_CACHE_DIR")
            if key in os.environ
        }
        environment.update(
            {
                "NO_COLOR": "1",
                "OPEN_BRAIN_ROOT": str(root),
                "OPEN_BRAIN_UI_PORT": str(_free_port()),
                "PYTHONUTF8": "1",
            }
        )
        status_probe = (
            "import sys; from pathlib import Path; "
            f"sys.path[:0] = {SOURCE_CHECKOUT_PATHS!r}; "
            "from open_brain.services.appliance_daemon import request_status; "
            "request_status(Path(sys.argv[1]), timeout=0.25)"
        )

        def start_daemon() -> subprocess.Popen[str]:
            process = subprocess.Popen(
                _source_checkout_module_command(
                    "open_brain.services.appliance_daemon",
                    "--root",
                    str(root),
                ),
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    assert process.stderr is not None
                    raise AssertionError(process.stderr.read())
                try:
                    probe = subprocess.run(
                        (sys.executable, "-I", "-B", "-c", status_probe, str(root)),
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=1,
                    )
                except subprocess.TimeoutExpired:
                    pass
                else:
                    if probe.returncode == 0:
                        return process
                time.sleep(0.05)
            process.terminate()
            process.wait(timeout=5)
            raise AssertionError("appliance daemon status did not become ready")

        daemon = start_daemon()
        try:
            created = subprocess.run(
                _source_checkout_secure_node_command(
                    "spaces",
                    "create",
                    "Process space",
                    "--delivery=process.space",
                    "--json",
                ),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            created_payload = json.loads(created.stdout)
            space_id = created_payload["space_id"]

            first_capture = subprocess.run(
                _source_checkout_secure_node_command(
                    "capture",
                    "quick",
                    "text",
                    "Process lexical token",
                    "--delivery=process.capture",
                    f"--space={space_id}",
                    "--json",
                ),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            first_payload = json.loads(first_capture.stdout)
            daemon.kill()
            daemon.wait(timeout=5)

            daemon = start_daemon()

            replay_capture = subprocess.run(
                _source_checkout_secure_node_command(
                    "capture",
                    "quick",
                    "text",
                    "Process lexical token",
                    "--delivery=process.capture",
                    f"--space={space_id}",
                    "--json",
                ),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            replay_payload = json.loads(replay_capture.stdout)
            inbox = subprocess.run(
                _source_checkout_secure_node_command("inbox", "list", "--json"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            listed_spaces = subprocess.run(
                _source_checkout_secure_node_command("spaces", "list", "--json"),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            queried = subprocess.run(
                _source_checkout_secure_node_command(
                    "query", "Process lexical token", "--json"
                ),
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)

        results = json.loads(queried.stdout)["results"]
        captures = json.loads(inbox.stdout)["captures"]
        spaces = json.loads(listed_spaces.stdout)["spaces"]

        assert first_payload["capture_id"] == replay_payload["capture_id"]
        assert replay_payload["duplicate"] is True
        assert [capture["capture_id"] for capture in captures] == [first_payload["capture_id"]]
        assert [space["space_id"] for space in spaces] == [space_id]
        assert {result["capture_id"] for result in results} == {first_payload["capture_id"]}


def _free_port() -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import socket; "
                "listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
                "listener.bind(('127.0.0.1', 0)); "
                "print(listener.getsockname()[1]); "
                "listener.close()"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())
