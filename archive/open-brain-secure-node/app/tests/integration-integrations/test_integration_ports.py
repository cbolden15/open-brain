from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

import pytest

import open_brain.integrations.ports as ports_module
from open_brain.integrations import (
    AuditDisposition,
    AuditFinding,
    AuditFindingCode,
    Capability,
    ExternalRuntimeAudit,
    FeedbackOutcome,
    HookEmitResult,
    HookInstaller,
    HookInstallRequest,
    HookInstallResult,
    HookInstallStatus,
    HookKind,
    HookSignalStatus,
    IntegrationConfig,
    IntegrationOutcome,
    IntegrationScope,
    OptionalIntegrationMetadata,
    OptionalProvider,
    PageDocument,
    PageReader,
    PageReadRequest,
    PostCommitSignal,
    PostCommitSignalPort,
    ProviderSync,
    ProviderSyncRequest,
    ProviderSyncResult,
    RedactedText,
    RedactionPolicyVersion,
    RedactionReceipt,
    RetrievalBatch,
    RetrievalFeedback,
    RetrievalFeedbackReceipt,
    RetrievalFeedbackRequest,
    RetrievalHit,
    RetrievalRequest,
    ReviewBoundWriter,
    ReviewDisposition,
    ReviewWriteKind,
    ReviewWriteRequest,
    ReviewWriteResult,
    RuntimeAuditRequest,
    RuntimeAuditResult,
    RuntimeField,
    RuntimeManifest,
    SyncStatus,
    SyntheticIntegrationAdapter,
    TrustLabel,
    UnavailableReason,
    VaultAdapter,
    VaultWriteDisposition,
    VaultWriteRequest,
    VaultWriteResult,
    WorkRetriever,
)


def test_public_ports_are_provider_neutral_and_scoped() -> None:
    assert {capability.value for capability in Capability} == {
        "finance",
        "mail_calendar",
        "messaging",
        "relationships",
        "life_os",
        "dev_workflow",
        "repository_identity",
        "work_context",
        "mcp",
        "ui",
        "obsidian",
        "hooks",
        "social_learning",
    }
    assert Capability.FINANCE.scope is IntegrationScope.PERSONAL
    assert Capability.MCP.scope is IntegrationScope.WORK


def test_live_integrations_and_lifeos_external_writes_are_disabled_by_default() -> None:
    config = IntegrationConfig()

    assert config.live_adapters == frozenset()
    assert not config.live_adapter_enabled(Capability.FINANCE)
    assert not config.lifeos_external_writes_enabled
    assert not config.external_writes_enabled(Capability.LIFE_OS)


def test_lifeos_external_writes_require_both_explicit_enablements() -> None:
    assert not IntegrationConfig(
        lifeos_external_writes_enabled=True
    ).external_writes_enabled(Capability.LIFE_OS)

    config = IntegrationConfig(
        live_adapters=frozenset({Capability.LIFE_OS}),
        lifeos_external_writes_enabled=True,
    )

    assert config.external_writes_enabled(Capability.LIFE_OS)


def test_configuration_methods_reject_invalid_capability_types() -> None:
    config = IntegrationConfig()

    with pytest.raises(ValueError, match="invalid integration capability"):
        config.live_adapter_enabled("finance")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid integration capability"):
        config.external_writes_enabled("life_os")  # type: ignore[arg-type]


def test_integration_outcome_rejects_invalid_reason_types_and_arbitrary_data() -> None:
    with pytest.raises(ValueError, match="invalid integration outcome"):
        IntegrationOutcome(
            available=False,
            capability=Capability.FINANCE,
            reason="disabled",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        IntegrationOutcome(
            available=True,
            capability=Capability.FINANCE,
            data={"credential": "synthetic-secret"},  # type: ignore[call-arg]
        )


def test_public_results_are_immutable_bounded_and_fixed_schema() -> None:
    title = RedactedText.redact("Synthetic result")
    excerpt = RedactedText.redact("Bounded redacted excerpt.")
    hit = RetrievalHit(
        result_id="result_fixture",
        rank=1,
        title=title,
        excerpt=excerpt,
        trust=TrustLabel.VERIFIED_WORK,
    )
    batch = RetrievalBatch(
        retrieval_id="retrieval_fixture",
        hits=(hit,),
        truncated=False,
    )

    with pytest.raises(FrozenInstanceError):
        hit.rank = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="invalid retrieval request"):
        RetrievalRequest(question="x" * 4097, limit=5)
    with pytest.raises(ValueError, match="invalid retrieval batch"):
        RetrievalBatch(
            retrieval_id="retrieval_fixture",
            hits=tuple(
                RetrievalHit(
                    result_id=f"result_{index}",
                    rank=(index % 8) + 1,
                    title=title,
                    excerpt=excerpt,
                    trust=TrustLabel.VERIFIED_WORK,
                )
                for index in range(9)
            ),
            truncated=True,
        )

    assert batch.to_dict() == {
        "retrieval_id": "retrieval_fixture",
        "scope": "work",
        "results": [
            {
                "result_id": "result_fixture",
                "rank": 1,
                "title": title.to_dict(),
                "excerpt": excerpt.to_dict(),
                "trust": "verified_work",
            }
        ],
        "truncated": False,
    }


def test_public_text_requires_a_factory_receipt_and_is_immutable() -> None:
    source = "Synthetic redacted result."
    public_text = RedactedText.redact(source)

    assert public_text.receipt.policy_version is RedactionPolicyVersion.V1
    assert public_text.receipt.source_digest == sha256(source.encode()).hexdigest()
    assert public_text.receipt.text_digest == sha256(public_text.text.encode()).hexdigest()
    assert public_text.receipt.verify(source=source, text=public_text.text)
    with pytest.raises(TypeError):
        RedactedText(source)
    with pytest.raises(FrozenInstanceError):
        public_text.text = "tampered"  # type: ignore[misc]

    with pytest.raises(ValueError, match="invalid retrieval hit"):
        RetrievalHit(
            result_id="result_fixture",
            rank=1,
            title=source,  # type: ignore[arg-type]
            excerpt=public_text,
            trust=TrustLabel.VERIFIED_WORK,
        )
    with pytest.raises(ValueError, match="invalid page document"):
        PageDocument(
            page_id="page_fixture",
            title=public_text,
            markdown=source,  # type: ignore[arg-type]
            trust=TrustLabel.VERIFIED_WORK,
        )


@pytest.mark.parametrize(
    ("constructor", "arguments"),
    (
        (RedactedText, ()),
        (RedactedText, ("Synthetic raw text",)),
        (RedactionReceipt, ()),
        (
            RedactionReceipt,
            (RedactionPolicyVersion.V1, "synthetic-source-digest", "synthetic-text-digest", 0),
        ),
    ),
)
def test_public_text_and_receipt_reject_all_direct_construction(
    constructor: Callable[..., object],
    arguments: tuple[object, ...],
) -> None:
    with pytest.raises(TypeError, match="factory"):
        constructor(*arguments)


def test_forged_receipts_cannot_authorize_credential_or_path_text() -> None:
    residual = "api key: synthetic-secret /private/synthetic"

    with pytest.raises(TypeError):
        RedactionReceipt._create(  # type: ignore[call-arg]
            source=residual,
            text=residual,
            redaction_count=0,
        )

    factory_receipt = RedactionReceipt._create(source=residual)
    forged_receipt = object.__new__(RedactionReceipt)
    object.__setattr__(forged_receipt, "policy_version", RedactionPolicyVersion.V1)
    object.__setattr__(forged_receipt, "source_digest", sha256(residual.encode()).hexdigest())
    object.__setattr__(forged_receipt, "text_digest", sha256(residual.encode()).hexdigest())
    object.__setattr__(forged_receipt, "redaction_count", 0)
    safe_title = RedactedText.redact("Synthetic result")
    safe_excerpt = RedactedText.redact("Synthetic excerpt")

    for receipt in (factory_receipt, forged_receipt):
        forged_text = object.__new__(RedactedText)
        object.__setattr__(forged_text, "text", residual)
        object.__setattr__(forged_text, "receipt", receipt)

        with pytest.raises(ValueError, match="invalid retrieval hit"):
            RetrievalHit(
                result_id="result_fixture",
                rank=1,
                title=safe_title,
                excerpt=forged_text,
                trust=TrustLabel.VERIFIED_WORK,
            )
        with pytest.raises(ValueError, match="invalid redacted text"):
            forged_text.to_dict()

        forged_result = RetrievalHit(
            result_id="result_fixture",
            rank=1,
            title=safe_title,
            excerpt=safe_excerpt,
            trust=TrustLabel.VERIFIED_WORK,
        )
        object.__setattr__(forged_result, "excerpt", forged_text)
        with pytest.raises(ValueError, match="invalid redacted text"):
            forged_result.to_dict()


@pytest.mark.parametrize(
    "canary",
    (
        "api key : synthetic-secret",
        "API KEY synthetic-secret",
        "Bearer synthetic-secret",
        "authorization = BEARER synthetic-secret",
        "/synthetic",
        "/synthetic/private",
        r"C:\Synthetic\private",
        "https://synthetic.invalid/private",
        "https%3A%2F%2Fsynthetic.invalid%2Fprivate",
        "%2Fsynthetic%2Fprivate",
        "RAW PAYLOAD : synthetic-content",
        "provider_payload=synthetic-content",
        "Captured-Payload synthetic-content",
    ),
)
def test_public_text_factory_leaves_no_adversarial_residuals(canary: str) -> None:
    public_text = RedactedText.redact(canary)
    safe_title = RedactedText.redact("Synthetic page")
    document = PageDocument(
        page_id="page_fixture",
        title=safe_title,
        markdown=public_text,
        trust=TrustLabel.VERIFIED_WORK,
    )

    assert public_text.text == "[redacted]"
    assert public_text.receipt.redaction_count == 1
    assert public_text.receipt.verify(source=canary, text=public_text.text)
    assert canary not in str(document.to_dict())


def _public_result_factories() -> tuple[Callable[[str], object], ...]:
    return (
        lambda value: RetrievalHit(
            result_id="result_fixture",
            rank=1,
            title=RedactedText.redact("Synthetic result"),
            excerpt=value,  # type: ignore[arg-type]
            trust=TrustLabel.VERIFIED_WORK,
        ),
        lambda value: RetrievalBatch(
            retrieval_id=value,
            hits=(),
            truncated=False,
        ),
        lambda value: RetrievalFeedbackReceipt(
            retrieval_id=value,
            outcome=FeedbackOutcome.USED,
            result_count=1,
        ),
        lambda value: PageDocument(
            page_id="page_fixture",
            title=RedactedText.redact("Synthetic page"),
            markdown=value,  # type: ignore[arg-type]
            trust=TrustLabel.VERIFIED_WORK,
        ),
        lambda value: ProviderSyncResult(
            capability=Capability.FINANCE,
            status=SyncStatus.COMPLETED,
            created=1,
            updated=0,
            removed=0,
            next_cursor_ref=value,
        ),
        lambda value: ReviewWriteResult(
            request_id=value,
            disposition=ReviewDisposition.QUEUED,
            review_id="review_fixture",
        ),
        lambda value: HookEmitResult(
            signal_id=value,
            status=HookSignalStatus.EMITTED,
        ),
        lambda value: HookInstallResult(
            repository_id=value,
            hook_kind=HookKind.POST_COMMIT,
            status=HookInstallStatus.PLANNED,
        ),
        lambda value: RuntimeAuditResult(
            audit_id=value,
            disposition=AuditDisposition.ALLOWED,
            findings=(),
        ),
        lambda value: VaultWriteResult(
            page_id=value,
            disposition=VaultWriteDisposition.CREATED,
            bytes_written=10,
        ),
    )


@pytest.mark.parametrize(
    "canary",
    (
        "credential=synthetic-secret",
        "/synthetic/private",
        "raw-payload=synthetic-content",
    ),
)
def test_credential_path_and_payload_canaries_are_rejected_from_every_public_result(
    canary: str,
) -> None:
    for factory in _public_result_factories():
        with pytest.raises(ValueError):
            factory(canary)


def test_typed_retrieval_page_and_metadata_feedback_contracts() -> None:
    hit = RetrievalHit(
        result_id="result_fixture",
        rank=1,
        title=RedactedText.redact("Synthetic result"),
        excerpt=RedactedText.redact("Redacted work-only excerpt."),
        trust=TrustLabel.UNREVIEWED_THIRD_PARTY,
    )
    batch = RetrievalBatch(
        retrieval_id="retrieval_fixture",
        hits=(hit,),
        truncated=False,
    )

    class SyntheticRetriever:
        def search(self, request: RetrievalRequest) -> RetrievalBatch:
            assert request.scope is IntegrationScope.WORK
            return batch

    class SyntheticFeedback:
        def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt:
            return RetrievalFeedbackReceipt(
                retrieval_id=request.retrieval_id,
                outcome=request.outcome,
                result_count=len(request.result_ids),
            )

    class SyntheticVault:
        def read(self, request: PageReadRequest) -> PageDocument | None:
            return PageDocument(
                page_id=request.page_id,
                title=RedactedText.redact("Synthetic page"),
                markdown=RedactedText.redact("Redacted Markdown."),
                trust=TrustLabel.VERIFIED_WORK,
            )

        def write(self, request: VaultWriteRequest) -> VaultWriteResult:
            return VaultWriteResult(
                page_id=request.page_id,
                disposition=VaultWriteDisposition.CREATED,
                bytes_written=len(request.markdown.encode()),
            )

    retriever: WorkRetriever = SyntheticRetriever()
    feedback: RetrievalFeedback = SyntheticFeedback()
    page_reader: PageReader = SyntheticVault()
    vault: VaultAdapter = SyntheticVault()

    returned = retriever.search(RetrievalRequest(question="synthetic topic", limit=1))
    receipt = feedback.record(
        RetrievalFeedbackRequest(
            retrieval_id=returned.retrieval_id,
            outcome=FeedbackOutcome.CITED,
            result_ids=(returned.hits[0].result_id,),
        )
    )
    page = page_reader.read(PageReadRequest(page_id="page_fixture"))
    write_result = vault.write(
        VaultWriteRequest(
            page_id="page_fixture",
            title="Synthetic page",
            markdown="Redacted Markdown.",
            review_id="review_fixture",
        )
    )

    assert returned.hits[0].trust is TrustLabel.UNREVIEWED_THIRD_PARTY
    assert receipt.to_dict() == {
        "retrieval_id": "retrieval_fixture",
        "outcome": "cited",
        "recorded": True,
        "result_count": 1,
    }
    assert page is not None and page.page_id == "page_fixture"
    assert write_result.disposition is VaultWriteDisposition.CREATED


def test_typed_sync_review_hook_and_runtime_audit_contracts() -> None:
    class SyntheticSync:
        def sync(self, request: ProviderSyncRequest) -> ProviderSyncResult:
            return ProviderSyncResult(
                capability=request.capability,
                status=SyncStatus.DRY_RUN if request.dry_run else SyncStatus.COMPLETED,
                created=1,
                updated=0,
                removed=0,
                next_cursor_ref=None,
            )

    class SyntheticReviewWriter:
        def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult:
            return ReviewWriteResult(
                request_id=request.request_id,
                disposition=ReviewDisposition.QUEUED,
                review_id=request.review_id,
            )

    class SyntheticSignalEmitter:
        def emit(self, signal: PostCommitSignal) -> HookEmitResult:
            return HookEmitResult(
                signal_id=signal.signal_id,
                status=HookSignalStatus.EMITTED,
            )

    class SyntheticHookInstaller:
        def install(self, request: HookInstallRequest) -> HookInstallResult:
            return HookInstallResult(
                repository_id=request.repository_id,
                hook_kind=request.hook_kind,
                status=HookInstallStatus.PLANNED,
            )

    class SyntheticRuntimeAudit:
        def inspect(self, request: RuntimeAuditRequest) -> RuntimeAuditResult:
            finding = AuditFinding(
                field=RuntimeField.EXECUTABLE,
                code=AuditFindingCode.FORBIDDEN_REFERENCE,
            )
            return RuntimeAuditResult(
                audit_id=request.manifest.manifest_id,
                disposition=AuditDisposition.DENIED,
                findings=(finding,),
            )

    sync: ProviderSync = SyntheticSync()
    review_writer: ReviewBoundWriter = SyntheticReviewWriter()
    signal_port: PostCommitSignalPort = SyntheticSignalEmitter()
    hook_installer: HookInstaller = SyntheticHookInstaller()
    runtime_audit: ExternalRuntimeAudit = SyntheticRuntimeAudit()

    sync_result = sync.sync(
        ProviderSyncRequest(
            capability=Capability.FINANCE,
            resource_ref="resource_fixture",
            cursor_ref=None,
            dry_run=True,
        )
    )
    review_result = review_writer.submit(
        ReviewWriteRequest(
            request_id="request_fixture",
            kind=ReviewWriteKind.PROPOSAL,
            content_ref="content_fixture",
            review_id="review_fixture",
        )
    )
    signal_result = signal_port.emit(
        PostCommitSignal(
            signal_id="signal_fixture",
            repository_id="repository_fixture",
            revision_id="revision_fixture",
        )
    )
    install_result = hook_installer.install(
        HookInstallRequest(
            repository_id="repository_fixture",
            hook_kind=HookKind.POST_COMMIT,
            dry_run=True,
        )
    )
    audit_result = runtime_audit.inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_fixture",
                executable_ref="synthetic_executable",
                argument_refs=("synthetic_argument",),
                working_directory_ref="synthetic_workdir",
                referenced_file_refs=(),
            ),
            forbidden_roots=("synthetic_forbidden_root",),
            allowed_roots=("synthetic_allowed_root",),
        )
    )

    assert sync_result.to_dict() == {
        "capability": "finance",
        "status": "dry_run",
        "created": 1,
        "updated": 0,
        "removed": 0,
    }
    assert review_result.disposition is ReviewDisposition.QUEUED
    assert signal_result.status is HookSignalStatus.EMITTED
    assert install_result.status is HookInstallStatus.PLANNED
    assert audit_result.to_dict() == {
        "audit_id": "manifest_fixture",
        "disposition": "denied",
        "findings": [
            {"field": "executable", "code": "forbidden_reference"},
        ],
    }


@pytest.mark.parametrize(
    "invalid_ref",
    (
        "synthetic\nref",
        "synthetic\tref",
        "synthetic\rref",
        "synthetic\0ref",
        "synthetic\x7fref",
        "../synthetic",
        r"..\synthetic",
        "%2e%2e%2fsynthetic",
    ),
)
@pytest.mark.parametrize(
    "field_name",
    (
        "executable_ref",
        "argument_refs",
        "working_directory_ref",
        "referenced_file_refs",
    ),
)
def test_runtime_manifest_rejects_controls_and_traversal_in_every_field(
    field_name: str,
    invalid_ref: str,
) -> None:
    values: dict[str, object] = {
        "manifest_id": "manifest_fixture",
        "executable_ref": "synthetic_executable",
        "argument_refs": ("synthetic_argument",),
        "working_directory_ref": "synthetic_workdir",
        "referenced_file_refs": ("synthetic_file",),
    }
    values[field_name] = (
        (invalid_ref,) if field_name in {"argument_refs", "referenced_file_refs"} else invalid_ref
    )

    with pytest.raises(ValueError, match="invalid runtime manifest"):
        RuntimeManifest(**values)  # type: ignore[arg-type]


def test_runtime_manifest_accepts_bounded_opaque_synthetic_refs() -> None:
    manifest = RuntimeManifest(
        manifest_id="manifest_fixture",
        executable_ref="synthetic_executable",
        argument_refs=("--mode=synthetic", "synthetic argument"),
        working_directory_ref="synthetic/workdir",
        referenced_file_refs=("synthetic/file",),
    )

    assert manifest.executable_ref == "synthetic_executable"
    assert manifest.argument_refs == ("--mode=synthetic", "synthetic argument")


def test_typed_records_reject_invalid_enum_and_container_types() -> None:
    with pytest.raises(ValueError, match="invalid retrieval hit"):
        RetrievalHit(
            result_id="result_fixture",
            rank=1,
            title=RedactedText.redact("Synthetic result"),
            excerpt=RedactedText.redact("Redacted excerpt."),
            trust="verified_work",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="invalid provider sync result"):
        ProviderSyncResult(
            capability=Capability.FINANCE,
            status="completed",  # type: ignore[arg-type]
            created=1,
            updated=0,
            removed=0,
        )
    with pytest.raises(ValueError, match="invalid runtime audit result"):
        RuntimeAuditResult(
            audit_id="audit_fixture",
            disposition=AuditDisposition.ALLOWED,
            findings=[],  # type: ignore[arg-type]
        )


def test_disabled_optional_provider_is_never_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[OptionalProvider] = []

    def import_canary(provider: OptionalProvider) -> object:
        calls.append(provider)
        raise AssertionError("disabled provider import attempted")

    monkeypatch.setattr(ports_module, "_loaded_optional_provider", import_canary)
    metadata = OptionalIntegrationMetadata(
        capability=Capability.FINANCE,
        provider=OptionalProvider.OPENAI,
    )

    outcome = metadata.load(config=IntegrationConfig())

    assert calls == []
    assert outcome.reason is UnavailableReason.DISABLED
    assert outcome.to_dict() == {
        "available": False,
        "capability": "finance",
        "reason": "disabled",
    }


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    (
        (ModuleNotFoundError("synthetic missing module"), UnavailableReason.OPTIONAL_DEPENDENCY),
        (
            RuntimeError("credential=synthetic-secret /synthetic/private raw-payload"),
            UnavailableReason.LOAD_FAILURE,
        ),
    ),
)
def test_missing_and_crashing_provider_loads_are_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_reason: UnavailableReason,
) -> None:
    def failing_import(_: OptionalProvider) -> object:
        raise failure

    monkeypatch.setattr(ports_module, "_loaded_optional_provider", failing_import)
    metadata = OptionalIntegrationMetadata(
        capability=Capability.FINANCE,
        provider=OptionalProvider.OPENAI,
    )
    config = IntegrationConfig(live_adapters=frozenset({Capability.FINANCE}))

    outcome = metadata.load(config=config)

    assert outcome.reason is expected_reason
    assert outcome.to_dict() == {
        "available": False,
        "capability": "finance",
        "reason": expected_reason.value,
    }
    assert "synthetic-secret" not in str(outcome)
    assert "/synthetic/private" not in str(outcome)
    assert "raw-payload" not in str(outcome)


def test_optional_package_imports_remain_lazy() -> None:
    module_name = "openai"

    assert module_name not in sys.modules
    metadata = OptionalIntegrationMetadata(
        capability=Capability.FINANCE,
        provider=OptionalProvider.OPENAI,
    )

    assert module_name not in sys.modules
    assert metadata.scope is IntegrationScope.PERSONAL


@pytest.mark.parametrize(
    "module_name",
    (
        "open_brain.services",
        "open_brain_connectors",
        "open_brain_engine.engine.local",
        "open_brain_legacy",
    ),
)
def test_internal_packages_cannot_be_optional_providers(module_name: str) -> None:
    with pytest.raises(ValueError):
        OptionalProvider(module_name)
    with pytest.raises(ValueError, match="invalid optional integration metadata"):
        OptionalIntegrationMetadata(
            capability=Capability.FINANCE,
            provider=module_name,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="invalid optional integration provider"):
        ports_module._loaded_optional_provider(module_name)  # type: ignore[arg-type]


def test_enabled_installed_optional_provider_loads_without_preload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "openai"
    (tmp_path / f"{module_name}.py").write_text("AVAILABLE = True\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    metadata = OptionalIntegrationMetadata(
        capability=Capability.FINANCE,
        provider=OptionalProvider.OPENAI,
    )

    outcome = metadata.load(
        config=IntegrationConfig(live_adapters=frozenset({Capability.FINANCE}))
    )

    assert outcome == IntegrationOutcome.available_for(capability=Capability.FINANCE)
    assert module_name in sys.modules


def test_synthetic_adapter_returns_declared_typed_outcome() -> None:
    declared = IntegrationOutcome.available_for(capability=Capability.DEV_WORKFLOW)
    adapter = SyntheticIntegrationAdapter(
        capability=Capability.DEV_WORKFLOW,
        outcome=declared,
    )

    assert adapter.scope is IntegrationScope.WORK
    assert adapter.availability() is declared
    assert adapter.availability().to_dict() == {
        "available": True,
        "capability": "dev_workflow",
    }
