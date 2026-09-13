from __future__ import annotations

import html
import io
import json
import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote

from open_brain_engine.core.models import (
    Authority,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
)
from open_brain_engine.engine import (
    CaptureAction,
    ProposalDraft,
    Provenance,
    ReferencePayload,
    TextPayload,
)

from open_brain.capture.http import HttpRequest
from open_brain.cli._common import CommandFamilyAdapter
from open_brain.cli.phase1_registry import Phase1CommandAdapterRegistry
from open_brain.integrations.mcp import EngineMcpAdapter
from open_brain.integrations.phase1_ui import Phase1UiRequest
from open_brain.services.appliance_init import APPLIANCE_OWNER_CREDENTIAL, initialize_appliance
from open_brain.services.appliance_lifecycle import (
    ApplianceLifecycleError,
    ApplianceLifecycleFailureReceipt,
    ApplianceUninstallReceipt,
    ApplianceUpgradeReceipt,
    LifecycleMigrationReceipt,
)
from open_brain.services.appliance_status import read_appliance_status
from open_brain.services.mcp_stdio import serve_stdio_mcp
from open_brain.services.phase1_application import SingleUserLocalApplication
from open_brain_legacy.cli.scheduled import scheduled_result_envelope
from open_brain_legacy.operations.scheduled_results import ScheduledDispatchResult

_ABSOLUTE_PATH = re.compile(
    r"(?<![:/\w])/(?:[^\s<>\"']+)|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"']+"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret|token)"
    r"\s*[:=]\s*(?:\S+)"
)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def test_recursive_public_result_oracle_covers_every_retained_surface(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    absolute_path = "/private/" + "x" * 32
    windows_path = "C:\\private\\result.txt"
    unc_path = "\\\\server\\share\\result.txt"
    credential = "api" + "_key=" + "A" * 32
    source = "https://example.test/private-reference"
    case_varied_source = "HTTPS://EXAMPLE.TEST/private-reference"
    protected = (absolute_path, windows_path, unc_path, credential, source)
    digests = (
        *(sha256(value.encode("utf-8")).hexdigest() for value in protected),
        sha256(case_varied_source.encode("utf-8")).hexdigest(),
    )
    searchable = "useful residue regression text"
    encoded_source = quote(source, safe="")
    encoded_case_varied_source = quote(case_varied_source, safe="")
    encoded_source_digest = quote(sha256(source.encode("utf-8")).hexdigest(), safe="")
    html_source = "".join(f"&#{ord(character)};" for character in source)
    html_case_varied_source = "".join(
        f"&#{ord(character)};" for character in case_varied_source
    )
    query_canary = searchable + " " + credential
    query_digest = sha256(query_canary.encode("utf-8")).hexdigest()
    query_derivatives = (query_digest, query_digest[:32])

    space = application.tasks.inbox.create_space(
        "Sensitive " + absolute_path + " " + credential,
        delivery_id="residue.space",
    )
    capture = application.tasks.capture.accept(
        ReferencePayload(
            source,
            " ".join(
                (
                    searchable,
                    absolute_path,
                    windows_path,
                    unc_path,
                    credential,
                    source,
                    case_varied_source,
                    encoded_source,
                    encoded_case_varied_source,
                    encoded_source_digest,
                    html_source,
                    html_case_varied_source,
                    *digests,
                )
            ),
        ),
        delivery_id="residue.capture",
    )
    canonical = application.tasks.capture.accept(
        TextPayload("ordinary canonical residue text"),
        delivery_id="residue.canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    application.tasks.inbox.route(
        capture.capture_id,
        space.space_id,
        delivery_id="residue.route",
    )
    proposals = application.tasks.review.propose(
        capture.capture_id,
        (ProposalDraft("Residue", "safe review text"),),
        delivery_id="residue.proposal",
    )
    proposal = proposals[0]

    public_values: list[object] = [
        space,
        application.tasks.inbox.spaces(),
        capture,
        canonical,
        application.tasks.retrieval.search(query_canary),
    ]
    adapters = application.cli_adapters()
    public_values.extend(
        (
            _adapter(adapters, "capture").dispatch(
                ("quick", "text", "CLI residue", "--delivery=residue.cli.capture")
            ),
            _adapter(adapters, "inbox").dispatch(("list",)),
            _adapter(adapters, "spaces").dispatch(("list",)),
            _adapter(adapters, "proposals").dispatch(("list",)),
            _adapter(adapters, "query").dispatch((query_canary,)),
            _adapter(adapters, "review").dispatch(
                ("approve", proposal.proposal_id, "--delivery=residue.cli.review")
            ),
        )
    )

    token = "synthetic-residue-token"
    public_values.extend(_http_outputs(application, token, protected))
    ui = application.ui_handler(token)
    headers = (("Authorization", "Bearer " + token),)
    public_values.extend(
        (
            ui.handle(Phase1UiRequest("GET", "/api/spaces", headers)),
            ui.handle(Phase1UiRequest("GET", "/api/search?q=" + quote(query_canary), headers)),
            ui.handle(Phase1UiRequest("GET", "/", headers)),
        )
    )
    mcp = application.mcp_adapter(allowed_space_ids=frozenset({space.space_id}))
    mcp_query = mcp.call_tool("brain_query", {"question": query_canary})
    repeated_mcp_query = mcp.call_tool("brain_query", {"question": query_canary})
    mcp_results = cast(list[dict[str, object]], mcp_query["results"])
    result_id = mcp_results[0]["result_id"]
    assert isinstance(result_id, str)
    assert mcp_query["retrieval_id"] != repeated_mcp_query["retrieval_id"]
    public_values.extend(
        (
            mcp_query,
            repeated_mcp_query,
            mcp.call_tool("brain_fetch", {"result_id": result_id}),
        )
    )
    public_values.append(_stdio_output(mcp, query_canary))

    sink = application.public_job_sink("JOB-029")
    public_values.append(
        sink.submit(
            ReferencePayload(source, searchable + " " + credential),
            delivery_id="residue.public-job",
            source_origin=ContentOrigin.THIRD_PARTY,
            source_reference=source,
            provenance=Provenance.create(
                source_ref=source,
                content_origin=ContentOrigin.THIRD_PARTY,
                owner_context="automation_absent",
            ),
            privacy=_public_privacy(),
        )
    )
    public_values.append(scheduled_result_envelope(ScheduledDispatchResult.unavailable("JOB-029")))

    assert application.tasks.retrieval.search(query_canary)
    for value in public_values:
        _assert_no_public_residue(
            value,
            protected=protected,
            digests=(*digests, *query_derivatives),
        )


def _http_outputs(
    application: SingleUserLocalApplication,
    token: str,
    protected: tuple[str, ...],
) -> tuple[object, ...]:
    absolute_path, _windows_path, _unc_path, credential, source = protected
    payloads = (
        {"family": "text", "text": "HTTP " + credential},
        {
            "family": "reference_or_file",
            "kind": "reference",
            "supplied_text": "HTTP " + absolute_path,
            "url": source,
        },
        {
            "attributes": {"label": credential},
            "event_type": "synthetic.residue",
            "family": "event",
        },
        {
            "dimensions": {"label": absolute_path},
            "family": "measurement",
            "unit": "count",
            "value": "1",
        },
    )
    outputs: list[object] = []
    for index, payload in enumerate(payloads):
        body = _json_bytes({"delivery_id": f"residue.http.{index}", "payload": payload})
        handler = application.share_handler(
            expected_bearer_token=token,
            body_reader=lambda _maximum, _timeout, value=body: value,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        outputs.append(
            handler.handle(
                HttpRequest(
                    "POST",
                    "/captures",
                    (
                        ("Authorization", "Bearer " + token),
                        ("Content-Length", str(len(body))),
                        ("Content-Type", "application/json"),
                    ),
                )
            )
        )
        if index == 0:
            outputs.append(
                handler.handle(
                    HttpRequest(
                        "POST",
                        "/captures",
                        (
                            ("Authorization", "Bearer " + token),
                            ("Content-Length", str(len(body))),
                            ("Content-Type", "application/json"),
                        ),
                    )
                )
            )
    outputs.append(
        application.share_handler(
            expected_bearer_token=token,
            body_reader=lambda _maximum, _timeout: b"{",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ).handle(HttpRequest("POST", "/captures", (),))
    )
    return tuple(outputs)


def _stdio_output(adapter: EngineMcpAdapter, question: str) -> tuple[dict[str, object], ...]:
    requests = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "brain_query", "arguments": {"question": question}},
        },
    )
    input_stream = io.BytesIO(
        b"".join(
            json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
            for request in requests
        )
    )
    output_stream = io.BytesIO()
    serve_stdio_mcp(adapter, input_stream=input_stream, output_stream=output_stream)
    return tuple(
        cast(dict[str, object], json.loads(line))
        for line in output_stream.getvalue().splitlines()
    )


def _public_privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _assert_no_public_residue(
    value: object,
    *,
    protected: Iterable[str],
    digests: Iterable[str],
) -> None:
    if isinstance(value, bytes):
        _assert_no_public_residue(value.decode("utf-8"), protected=protected, digests=digests)
    elif isinstance(value, str):
        for candidate in _text_variants(value):
            plain = _HTML_TAG.sub("", candidate)
            folded = candidate.casefold()
            assert not any(item.casefold() in folded for item in protected)
            assert not any(digest.casefold() in folded for digest in digests)
            assert _ABSOLUTE_PATH.search(plain) is None
            assert _CREDENTIAL_ASSIGNMENT.search(plain) is None
        with suppress(json.JSONDecodeError):
            _assert_no_public_residue(
                json.loads(value),
                protected=protected,
                digests=digests,
            )
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _assert_no_public_residue(
                getattr(value, field.name), protected=protected, digests=digests
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_public_residue(key, protected=protected, digests=digests)
            _assert_no_public_residue(item, protected=protected, digests=digests)
    elif isinstance(value, tuple | list | set | frozenset):
        for item in value:
            _assert_no_public_residue(item, protected=protected, digests=digests)


def _text_variants(value: str) -> tuple[str, ...]:
    variants = [value, html.unescape(value)]
    for _ in range(3):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            break
        variants.append(decoded)
    return tuple(variants)


def _adapter(registry: Phase1CommandAdapterRegistry, name: str) -> CommandFamilyAdapter:
    adapter = registry.get(name)
    assert adapter is not None
    return adapter


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_appliance_status_does_not_leak_private_paths_or_generated_credentials(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Personal",))
    credential = (root / APPLIANCE_OWNER_CREDENTIAL).read_text(encoding="utf-8").strip()
    status = read_appliance_status(root).to_dict()
    rendered = json.dumps(status, sort_keys=True)

    assert credential
    _assert_no_public_residue(
        rendered,
        protected=(credential, str(root)),
        digests=(sha256(credential.encode("utf-8")).hexdigest(),),
    )


def test_appliance_lifecycle_receipts_do_not_leak_paths_or_raw_failures() -> None:
    protected = (
        "/private/brain-root",
        "/private/backup-root",
        "/private/candidate-checkout",
        "password=secret",
        "RuntimeError: secret",
    )
    digests = tuple(sha256(value.encode("utf-8")).hexdigest() for value in protected)
    values = (
        ApplianceUpgradeReceipt(
            request_id="upgrade_123e4567-e89b-42d3-a456-4266141744ab",
            status="upgraded",
            candidate_id="candidate_source-checkout-v110",
            prior_candidate_id="candidate_current-v1",
            active_candidate_id="candidate_source-checkout-v110",
            compatibility_state="compatible",
            backup_id="backup_123e4567-e89b-42d3-a456-4266141744ac",
            manifest_digest_sha256="a" * 64,
            preflight_state="ready",
            migrations=(
                LifecycleMigrationReceipt(
                    component="engine",
                    from_version="1.0.0",
                    to_version="1.1.0",
                    status="applied",
                ),
                LifecycleMigrationReceipt(
                    component="app",
                    from_version="1.0.0",
                    to_version="1.1.0",
                    status="applied",
                ),
            ),
            activation_state="activated",
            restart_state="restarted",
            doctor_state="healthy",
        ).to_dict(),
        ApplianceUninstallReceipt(
            request_id="uninstall_123e4567-e89b-42d3-a456-4266141744ad",
            status="uninstalled",
            prior_candidate_id="candidate_current-v1",
            daemon_stop_state="stopped",
            supervisor_remove_state="removed",
            artifact_remove_state="removed",
            brain_root_state="preserved",
        ).to_dict(),
        ApplianceLifecycleError(
            ApplianceLifecycleFailureReceipt(
                operation="upgrade",
                request_id="upgrade_123e4567-e89b-42d3-a456-4266141744ae",
                status="failed",
                failure_stage="doctor",
                candidate_id="candidate_source-checkout-v110",
                prior_candidate_id="candidate_current-v1",
                active_candidate_id="candidate_current-v1",
                rollback_state="rollback_failed",
            )
        ).receipt.to_dict(),
    )

    for value in values:
        _assert_no_public_residue(value, protected=protected, digests=digests)
