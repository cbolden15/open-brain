from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from open_brain_legacy._compat.open_brain.config import (
    AppConfig,
    RetainedRootIdentities,
    RetainedRoots,
    SecretRef,
)
from open_brain_legacy.migrate.config import (
    EVIDENCE_VERSION,
    PRIVATE_MODE,
    AuthorityBinding,
    BackupReceipt,
    ConfigMigrationBlockedError,
    ConfigMigrationError,
    ConfigMigrationPlan,
    ConfigMigrationResult,
    ConfigMigrationState,
    DestinationSnapshot,
    MigrationPathIdentities,
    OverwriteReceipt,
    PrerequisiteClaims,
    PrerequisiteReceipt,
    PrerequisiteRequest,
    PrivateDestinationClass,
    PublicationConflictError,
    PublicationReceipt,
    PublicationRequest,
    RecoveryReceipt,
    RecoveryRequest,
    RefusedConfigOverwriteError,
    SecretMigrationValue,
    StaleConfigMigrationPlanError,
    apply_config_migration,
    plan_config_migration,
)


@dataclass(frozen=True)
class _PrerequisiteRecord:
    binding: AuthorityBinding
    claims: PrerequisiteClaims
    expires_at: int


class SyntheticPrerequisiteAuthority:
    def __init__(self, *, ready: bool = True, ttl: int = 10) -> None:
        self.ready = ready
        self.ttl = ttl
        self.now = 100
        self._counter = 0
        self._records: dict[str, _PrerequisiteRecord] = {}

    def probe(self, request: PrerequisiteRequest) -> PrerequisiteClaims:
        assert request.provider == "synthetic-provider"
        assert request.evidence_version == EVIDENCE_VERSION
        return PrerequisiteClaims(count=2, ready=self.ready)

    def issue(
        self, binding: AuthorityBinding, claims: PrerequisiteClaims
    ) -> PrerequisiteReceipt:
        self._counter += 1
        token = f"prerequisite-{self._counter}"
        self._records[token] = _PrerequisiteRecord(binding, claims, self.now + self.ttl)
        return PrerequisiteReceipt(EVIDENCE_VERSION, token)

    def issue_for(
        self, binding: AuthorityBinding, claims: PrerequisiteClaims
    ) -> PrerequisiteReceipt:
        return self.issue(binding, claims)

    def verify(
        self, receipt: PrerequisiteReceipt, binding: AuthorityBinding
    ) -> PrerequisiteClaims | None:
        record = self._records.get(receipt.token)
        if (
            receipt.version != EVIDENCE_VERSION
            or record is None
            or record.binding != binding
            or self.now > record.expires_at
        ):
            return None
        return record.claims

    def advance(self, seconds: int) -> None:
        self.now += seconds


@dataclass(frozen=True)
class _AuthorityRecord:
    binding: AuthorityBinding
    expires_at: int


class SyntheticBackupAuthority:
    def __init__(self, *, ttl: int = 10) -> None:
        self.ttl = ttl
        self.now = 100
        self._counter = 0
        self._records: dict[str, _AuthorityRecord] = {}

    def issue(self, binding: AuthorityBinding) -> BackupReceipt:
        self._counter += 1
        token = f"backup-{self._counter}"
        self._records[token] = _AuthorityRecord(binding, self.now + self.ttl)
        return BackupReceipt(EVIDENCE_VERSION, token)

    def verify(self, receipt: BackupReceipt, binding: AuthorityBinding) -> bool:
        record = self._records.get(receipt.token)
        return bool(
            receipt.version == EVIDENCE_VERSION
            and record is not None
            and record.binding == binding
            and self.now <= record.expires_at
        )

    def advance(self, seconds: int) -> None:
        self.now += seconds


class SyntheticOverwriteAuthority:
    def __init__(self, *, ttl: int = 10) -> None:
        self.ttl = ttl
        self.now = 100
        self._counter = 0
        self._records: dict[str, _AuthorityRecord] = {}

    def issue(self, binding: AuthorityBinding) -> OverwriteReceipt:
        self._counter += 1
        token = f"overwrite-{self._counter}"
        self._records[token] = _AuthorityRecord(binding, self.now + self.ttl)
        return OverwriteReceipt(EVIDENCE_VERSION, token)

    def verify(self, receipt: OverwriteReceipt, binding: AuthorityBinding) -> bool:
        record = self._records.get(receipt.token)
        return bool(
            receipt.version == EVIDENCE_VERSION
            and record is not None
            and record.binding == binding
            and self.now <= record.expires_at
        )

    def advance(self, seconds: int) -> None:
        self.now += seconds


@dataclass
class _Journal:
    public_path: Path
    private_path: Path
    public_payload: bytes | None
    private_payload: bytes | None
    private_mode: int | None
    private_owner: str | None


class SyntheticPairPublisher:
    def __init__(
        self,
        *,
        identities: MigrationPathIdentities,
        owner: str,
        initial: Mapping[Path, bytes] | None = None,
    ) -> None:
        self.identities = identities
        self.owner = owner
        self.data = dict(initial or {})
        self.private_mode: int | None = None
        self.private_owner: str | None = None
        self.private_classification = PrivateDestinationClass.PRIVATE
        self.private_confined = True
        self.private_no_follow = True
        self.private_is_symlink = False
        self.publish_mode = PRIVATE_MODE
        self.fail_stage: int | None = None
        self.compete = False
        self.publish_count = 0
        self.recovery_count = 0
        self._counter = 0
        self._journal: _Journal | None = None
        self._external_pending = False
        self._publication_records: dict[str, PublicationRequest] = {}
        self._recovery_records: dict[str, RecoveryRequest] = {}

    def set_existing_private_metadata(self, *, mode: int, owner: str) -> None:
        self.private_mode = mode
        self.private_owner = owner

    def inspect(self, public_path: Path, private_path: Path) -> DestinationSnapshot:
        private_payload = self.data.get(private_path)
        return DestinationSnapshot(
            public_payload=self.data.get(public_path),
            private_payload=private_payload,
            public_identity=self.identities.public_destination,
            private_identity=self.identities.private_destination,
            private_classification=self.private_classification,
            private_confined=self.private_confined,
            private_no_follow=self.private_no_follow,
            private_is_symlink=self.private_is_symlink,
            private_owner=self.private_owner if private_payload is not None else None,
            private_mode=self.private_mode if private_payload is not None else None,
            recovery_required=self._external_pending,
        )

    def recover(self, request: RecoveryRequest) -> RecoveryReceipt:
        self.recovery_count += 1
        if (
            request.binding.identity_digest != request.identity_digest
            or request.public_identity != self.identities.public_destination
            or request.private_identity != self.identities.private_destination
            or request.public_tree_identity != self.identities.public_tree
            or request.expected_owner != self.owner
        ):
            raise ConfigMigrationBlockedError("synthetic recovery scope rejected")
        if self._journal is not None:
            self._restore_journal()
        self._external_pending = False
        self._counter += 1
        token = f"recovery-{self._counter}"
        self._recovery_records[token] = request
        return RecoveryReceipt(EVIDENCE_VERSION, token)

    def verify_recovery(
        self, receipt: RecoveryReceipt, request: RecoveryRequest
    ) -> bool:
        return (
            receipt.version == EVIDENCE_VERSION
            and self._recovery_records.get(receipt.token) == request
            and request.binding.identity_digest == request.identity_digest
            and request.public_identity == self.identities.public_destination
            and request.private_identity == self.identities.private_destination
            and request.public_tree_identity == self.identities.public_tree
            and request.expected_owner == self.owner
        )

    def publish(self, request: PublicationRequest) -> PublicationReceipt:
        if self.compete:
            self.data[request.public_path] = b"competing-writer"
            raise PublicationConflictError("synthetic conflict")
        if (
            self.data.get(request.public_path) != request.expected_public
            or self.data.get(request.private_path) != request.expected_private
        ):
            raise PublicationConflictError("synthetic conflict")
        self.publish_count += 1
        self._journal = _Journal(
            public_path=request.public_path,
            private_path=request.private_path,
            public_payload=request.expected_public,
            private_payload=request.expected_private,
            private_mode=self.private_mode,
            private_owner=self.private_owner,
        )
        self.data[request.public_path] = request.desired_public
        if self.fail_stage == 1:
            self._restore_journal()
            raise OSError("synthetic first publication failure")
        self.data[request.private_path] = request.desired_private
        self.private_mode = self.publish_mode
        self.private_owner = request.expected_owner
        if self.fail_stage == 2:
            self._restore_journal()
            raise OSError("synthetic second publication failure")
        self._counter += 1
        token = f"publication-{self._counter}"
        self._publication_records[token] = request
        return PublicationReceipt(EVIDENCE_VERSION, token)

    def verify_publication(
        self,
        receipt: PublicationReceipt,
        request: PublicationRequest,
        observed: DestinationSnapshot,
    ) -> bool:
        verified = (
            receipt.version == EVIDENCE_VERSION
            and self._publication_records.get(receipt.token) == request
            and request.before_digest() == request.binding.destination_digest
            and observed.digest() == request.after_digest()
            and observed.private_mode == PRIVATE_MODE
            and observed.private_owner == request.expected_owner
            and not observed.private_is_symlink
        )
        if verified:
            self._journal = None
        return verified

    def leave_interrupted_pair(
        self, public_path: Path, private_path: Path, partial_public: bytes
    ) -> None:
        self._journal = _Journal(
            public_path=public_path,
            private_path=private_path,
            public_payload=self.data.get(public_path),
            private_payload=self.data.get(private_path),
            private_mode=self.private_mode,
            private_owner=self.private_owner,
        )
        self.data[public_path] = partial_public
        self._external_pending = True

    def _restore_journal(self) -> None:
        assert self._journal is not None
        journal = self._journal
        if journal.public_payload is None:
            self.data.pop(journal.public_path, None)
        else:
            self.data[journal.public_path] = journal.public_payload
        if journal.private_payload is None:
            self.data.pop(journal.private_path, None)
        else:
            self.data[journal.private_path] = journal.private_payload
        self.private_mode = journal.private_mode
        self.private_owner = journal.private_owner
        self._journal = None


def _roots(base: Path) -> RetainedRoots:
    return RetainedRoots(
        work=base / "retained-work",
        personal=base / "retained-personal",
        capture=base / "retained-capture",
        saved_content=base / "retained-saved",
        state=base / "runtime-state",
    )


def _source(base: Path, secret: str = "synthetic-private-value") -> dict[str, object]:
    return {
        "paths": {
            **_roots(base).to_dict(),
            "backup_root": str(base / "backup"),
        },
        "host": {"identity": "synthetic-writer"},
        "providers": {"default": "synthetic-provider", "cloud_enabled": False},
        "egress": {"enabled": False},
        "secrets": {
            "provider_token": SecretMigrationValue(
                reference=SecretRef.parse("env:SYNTHETIC_PROVIDER_TOKEN"),
                value=secret,
            )
        },
    }


def _identities() -> MigrationPathIdentities:
    return MigrationPathIdentities(
        roots=RetainedRootIdentities(
            work="physical-work",
            personal="physical-personal",
            capture="physical-capture",
            saved_content="physical-saved",
            state="physical-state",
        ),
        public_destination="physical-public-config",
        private_destination="physical-private-config",
        public_tree="physical-public-tree",
    )


def _paths(base: Path) -> tuple[Path, Path, Path]:
    public_tree = base / "public-tree"
    return public_tree / "config.toml", base / "private" / "config.env", public_tree


def _plan(
    *,
    base: Path,
    publisher: SyntheticPairPublisher,
    prerequisite_authority: SyntheticPrerequisiteAuthority,
    source: Mapping[str, object] | None = None,
) -> ConfigMigrationPlan:
    public_path, private_path, public_tree = _paths(base)
    return plan_config_migration(
        source=source or _source(base),
        public_path=public_path,
        private_path=private_path,
        public_tree=public_tree,
        expected_owner="synthetic-owner",
        publisher=publisher,
        prerequisite_authority=prerequisite_authority,
        identities=_identities(),
    )


def _apply(
    *,
    plan: ConfigMigrationPlan,
    base: Path,
    publisher: SyntheticPairPublisher,
    prerequisite_authority: SyntheticPrerequisiteAuthority,
    source: Mapping[str, object] | None = None,
    backup_authority: SyntheticBackupAuthority | None = None,
    backup_receipt: BackupReceipt | None = None,
    overwrite_authority: SyntheticOverwriteAuthority | None = None,
    overwrite_receipt: OverwriteReceipt | None = None,
    identities: MigrationPathIdentities | None = None,
) -> ConfigMigrationResult:
    public_path, private_path, public_tree = _paths(base)
    return apply_config_migration(
        plan=plan,
        source=source or _source(base),
        public_path=public_path,
        private_path=private_path,
        public_tree=public_tree,
        expected_owner="synthetic-owner",
        publisher=publisher,
        prerequisite_authority=prerequisite_authority,
        identities=identities or _identities(),
        backup_authority=backup_authority,
        backup_receipt=backup_receipt,
        overwrite_authority=overwrite_authority,
        overwrite_receipt=overwrite_receipt,
    )


def test_plan_is_deterministic_redacted_and_reads_only_explicit_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "synthetic-private-value"
    source = _source(tmp_path, canary)
    source_path = tmp_path / "explicit-source.toml"
    public_path, private_path, public_tree = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    reads: list[Path] = []

    def read_source(path: Path) -> Mapping[str, object]:
        reads.append(path)
        return source

    monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(AssertionError("home")))
    monkeypatch.setattr(Path, "cwd", lambda: (_ for _ in ()).throw(AssertionError("cwd")))
    first = plan_config_migration(
        source_path=source_path,
        read_source=read_source,
        public_path=public_path,
        private_path=private_path,
        public_tree=public_tree,
        expected_owner="synthetic-owner",
        publisher=publisher,
        prerequisite_authority=authority,
        identities=_identities(),
    )
    second = plan_config_migration(
        source_path=source_path,
        read_source=read_source,
        public_path=public_path,
        private_path=private_path,
        public_tree=public_tree,
        expected_owner="synthetic-owner",
        publisher=publisher,
        prerequisite_authority=authority,
        identities=_identities(),
    )

    assert first.to_redacted_dict() == second.to_redacted_dict()
    assert reads == [source_path, source_path]
    serialized = json.dumps(first.to_redacted_dict())
    assert str(tmp_path) not in serialized
    assert canary not in serialized
    assert canary not in repr(first)


def test_apply_publishes_typed_public_refs_and_owner_only_private_values(tmp_path: Path) -> None:
    public_path, private_path, _ = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)

    result = _apply(
        plan=plan,
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=authority,
    )

    public_text = publisher.data[public_path].decode()
    private_text = publisher.data[private_path].decode()
    loaded = AppConfig.from_sources(toml_data=tomllib.loads(public_text))
    assert result.state is ConfigMigrationState.APPLIED
    assert result.publication_receipt is not None
    assert loaded.roots == _roots(tmp_path)
    assert loaded.host_identity == "synthetic-writer"
    assert '[host]\nidentity = "synthetic-writer"' in public_text
    assert "env:SYNTHETIC_PROVIDER_TOKEN" in public_text
    assert "synthetic-private-value" not in public_text
    assert "synthetic-private-value" in private_text
    assert publisher.private_mode == PRIVATE_MODE
    assert publisher.private_owner == "synthetic-owner"


@pytest.mark.parametrize("stage", [1, 2])
def test_first_or_second_publication_failure_rolls_back_exact_pair(
    tmp_path: Path, stage: int
) -> None:
    public_path, private_path, _ = _paths(tmp_path)
    initial = {public_path: b"before-public", private_path: b"before-private"}
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner", initial=initial
    )
    publisher.set_existing_private_metadata(mode=PRIVATE_MODE, owner="synthetic-owner")
    publisher.fail_stage = stage
    prerequisite = SyntheticPrerequisiteAuthority()
    backup = SyntheticBackupAuthority()
    overwrite = SyntheticOverwriteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=prerequisite)

    with pytest.raises(ConfigMigrationError, match="publication failed"):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=prerequisite,
            backup_authority=backup,
            backup_receipt=backup.issue(plan.binding()),
            overwrite_authority=overwrite,
            overwrite_receipt=overwrite.issue(plan.binding()),
        )

    assert publisher.data == initial
    assert publisher.private_mode == PRIVATE_MODE
    assert publisher.private_owner == "synthetic-owner"


def test_pending_interruption_is_recovered_before_retry(tmp_path: Path) -> None:
    public_path, private_path, _ = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)
    publisher.leave_interrupted_pair(public_path, private_path, b"partial-publication")

    result = _apply(
        plan=plan,
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=authority,
    )

    assert result.state is ConfigMigrationState.APPLIED
    assert publisher.recovery_count >= 1
    assert publisher.data[public_path] != b"partial-publication"


def test_relative_destinations_fail_before_recovery(tmp_path: Path) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)

    with pytest.raises(ConfigMigrationError, match="absolute path"):
        apply_config_migration(
            plan=plan,
            source=_source(tmp_path),
            public_path=Path("relative-public"),
            private_path=Path("relative-private"),
            public_tree=Path("relative-tree"),
            expected_owner="synthetic-owner",
            publisher=publisher,
            prerequisite_authority=authority,
            identities=_identities(),
        )

    assert publisher.recovery_count == 0


def test_unconfined_private_destination_fails_before_recovery(tmp_path: Path) -> None:
    public_path, _, public_tree = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)

    with pytest.raises(ConfigMigrationBlockedError, match="not confined"):
        apply_config_migration(
            plan=plan,
            source=_source(tmp_path),
            public_path=public_path,
            private_path=public_tree / "private.env",
            public_tree=public_tree,
            expected_owner="synthetic-owner",
            publisher=publisher,
            prerequisite_authority=authority,
            identities=_identities(),
        )

    assert publisher.recovery_count == 0


def test_forged_alias_identities_fail_before_recovery(tmp_path: Path) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)
    forged_identities = _identities()
    object.__setattr__(forged_identities, "public_destination", "physical-work")

    with pytest.raises(ConfigMigrationError, match="identities invalid"):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
            source=_source(tmp_path),
            identities=forged_identities,
        )

    assert publisher.recovery_count == 0


def test_invalid_source_fails_before_recovery(tmp_path: Path) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)

    with pytest.raises(ConfigMigrationError, match="legacy configuration"):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
            source={"paths": {}},
        )

    assert publisher.recovery_count == 0


def test_forged_and_source_stale_plans_fail_before_recovery(tmp_path: Path) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    source = _source(tmp_path)
    plan = _plan(
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=authority,
        source=source,
    )
    forged = replace(plan, plan_digest="0" * 64)
    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=forged,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
            source=source,
        )
    stale_source = _source(tmp_path)
    providers = stale_source["providers"]
    assert isinstance(providers, dict)
    providers["default"] = "changed-provider"
    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
            source=stale_source,
        )

    assert publisher.recovery_count == 0


def test_invalid_prerequisite_receipt_fails_before_recovery(tmp_path: Path) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)
    forged = replace(
        plan,
        prerequisite_receipt=PrerequisiteReceipt(EVIDENCE_VERSION, "forged"),
    )

    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=forged,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )

    assert publisher.recovery_count == 0


def test_competing_writer_fails_exact_cas_without_overwrite(tmp_path: Path) -> None:
    public_path, _, _ = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)
    publisher.compete = True

    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )

    assert publisher.data[public_path] == b"competing-writer"
    assert publisher.publish_count == 0


def test_existing_outputs_require_issuer_bound_backup_and_overwrite_receipts(
    tmp_path: Path,
) -> None:
    public_path, private_path, _ = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(),
        owner="synthetic-owner",
        initial={public_path: b"old-public", private_path: b"old-private"},
    )
    publisher.set_existing_private_metadata(mode=PRIVATE_MODE, owner="synthetic-owner")
    prerequisite = SyntheticPrerequisiteAuthority()
    backup = SyntheticBackupAuthority()
    overwrite = SyntheticOverwriteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=prerequisite)
    binding = plan.binding()

    forged_backup = BackupReceipt(EVIDENCE_VERSION, "forged")
    forged_overwrite = OverwriteReceipt(EVIDENCE_VERSION, "forged")
    with pytest.raises(RefusedConfigOverwriteError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=prerequisite,
            backup_authority=backup,
            backup_receipt=forged_backup,
            overwrite_authority=overwrite,
            overwrite_receipt=forged_overwrite,
        )

    wrong_binding = replace(binding, identity_digest="0" * 64)
    with pytest.raises(RefusedConfigOverwriteError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=prerequisite,
            backup_authority=backup,
            backup_receipt=backup.issue(wrong_binding),
            overwrite_authority=overwrite,
            overwrite_receipt=overwrite.issue(wrong_binding),
        )

    expired_backup = backup.issue(binding)
    expired_overwrite = overwrite.issue(binding)
    backup.advance(11)
    overwrite.advance(11)
    with pytest.raises(RefusedConfigOverwriteError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=prerequisite,
            backup_authority=backup,
            backup_receipt=expired_backup,
            overwrite_authority=overwrite,
            overwrite_receipt=expired_overwrite,
        )

    fresh_backup = SyntheticBackupAuthority()
    fresh_overwrite = SyntheticOverwriteAuthority()
    result = _apply(
        plan=plan,
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=prerequisite,
        backup_authority=fresh_backup,
        backup_receipt=fresh_backup.issue(binding),
        overwrite_authority=fresh_overwrite,
        overwrite_receipt=fresh_overwrite.issue(binding),
    )
    assert result.state is ConfigMigrationState.APPLIED


def test_forged_expired_and_wrong_scope_prerequisite_receipts_fail_before_noop(
    tmp_path: Path,
) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)
    _apply(
        plan=plan,
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=authority,
    )
    forged = replace(
        plan,
        prerequisite_receipt=PrerequisiteReceipt(EVIDENCE_VERSION, "forged"),
    )
    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=forged,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )

    wrong_binding = replace(plan.binding(), destination_digest="0" * 64)
    wrong_scope = replace(
        plan,
        prerequisite_receipt=authority.issue_for(
            wrong_binding, PrerequisiteClaims(count=2, ready=True)
        ),
    )
    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=wrong_scope,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )

    authority.advance(11)
    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )


def test_unready_prerequisite_receipt_fails_closed(tmp_path: Path) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority(ready=False)
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)

    assert plan.ready is False
    with pytest.raises(ConfigMigrationBlockedError, match="prerequisites unavailable"):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "classification"])
def test_private_destination_rejects_symlink_unsafe_mode_or_public_classification(
    tmp_path: Path, unsafe: str
) -> None:
    public_path, private_path, _ = _paths(tmp_path)
    initial = {private_path: b"existing-private"} if unsafe == "mode" else None
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner", initial=initial
    )
    if unsafe == "symlink":
        publisher.private_is_symlink = True
    elif unsafe == "mode":
        publisher.set_existing_private_metadata(mode=0o644, owner="synthetic-owner")
    else:
        publisher.private_classification = PrivateDestinationClass.PUBLIC_TREE

    with pytest.raises(ConfigMigrationBlockedError):
        _plan(
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=SyntheticPrerequisiteAuthority(),
        )
    assert public_path not in publisher.data


def test_private_path_inside_public_tree_is_rejected(tmp_path: Path) -> None:
    public_path, _, public_tree = _paths(tmp_path)
    private_path = public_tree / "private.env"
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )

    with pytest.raises(ConfigMigrationBlockedError, match="not confined"):
        plan_config_migration(
            source=_source(tmp_path),
            public_path=public_path,
            private_path=private_path,
            public_tree=public_tree,
            expected_owner="synthetic-owner",
            publisher=publisher,
            prerequisite_authority=SyntheticPrerequisiteAuthority(),
            identities=_identities(),
        )


def test_unsafe_published_private_mode_rolls_back(tmp_path: Path) -> None:
    public_path, private_path, _ = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    publisher.publish_mode = 0o644
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)

    with pytest.raises(ConfigMigrationError, match="verification failed"):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )

    assert public_path not in publisher.data
    assert private_path not in publisher.data


def test_stale_source_destination_and_forged_plan_envelope_fail_before_publication(
    tmp_path: Path,
) -> None:
    public_path, _, _ = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    source = _source(tmp_path)
    plan = _plan(
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=authority,
        source=source,
    )
    changed_source = _source(tmp_path)
    providers = changed_source["providers"]
    assert isinstance(providers, dict)
    providers["default"] = "changed-provider"

    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
            source=changed_source,
        )
    publisher.data[public_path] = b"changed-after-plan"
    with pytest.raises(StaleConfigMigrationPlanError):
        _apply(
            plan=plan,
            base=tmp_path,
            publisher=publisher,
            prerequisite_authority=authority,
        )
    publisher.data.pop(public_path)
    for forged in (
        replace(plan, plan_digest="0" * 64),
        replace(plan, change_count=plan.change_count + 1),
        replace(plan, ready=not plan.ready),
    ):
        with pytest.raises(StaleConfigMigrationPlanError):
            _apply(
                plan=forged,
                base=tmp_path,
                publisher=publisher,
                prerequisite_authority=authority,
            )


def test_idempotent_replay_is_verified_noop(tmp_path: Path) -> None:
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    authority = SyntheticPrerequisiteAuthority()
    plan = _plan(base=tmp_path, publisher=publisher, prerequisite_authority=authority)
    first = _apply(
        plan=plan,
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=authority,
    )
    replay = _apply(
        plan=plan,
        base=tmp_path,
        publisher=publisher,
        prerequisite_authority=authority,
    )

    assert first.state is ConfigMigrationState.APPLIED
    assert replay.state is ConfigMigrationState.NOOP
    assert publisher.publish_count == 1


def test_destinations_reject_lexical_overlap_and_physical_aliases(tmp_path: Path) -> None:
    public_path, _, public_tree = _paths(tmp_path)
    publisher = SyntheticPairPublisher(
        identities=_identities(), owner="synthetic-owner"
    )
    overlapping_private = public_path / "private.env"
    with pytest.raises(ConfigMigrationError, match="non-overlapping"):
        plan_config_migration(
            source=_source(tmp_path),
            public_path=public_path,
            private_path=overlapping_private,
            public_tree=public_tree,
            expected_owner="synthetic-owner",
            publisher=publisher,
            prerequisite_authority=SyntheticPrerequisiteAuthority(),
            identities=_identities(),
        )
    with pytest.raises(ConfigMigrationError, match="physical aliases"):
        MigrationPathIdentities(
            roots=_identities().roots,
            public_destination="physical-work",
            private_destination="physical-private-config",
            public_tree="physical-public-tree",
        )


def test_reader_failure_is_redacted(tmp_path: Path) -> None:
    public_path, private_path, public_tree = _paths(tmp_path)
    canary = "synthetic-reader-failure"

    def failing_reader(path: Path) -> Mapping[str, object]:
        raise OSError(f"{canary}:{path}")

    with pytest.raises(ConfigMigrationError) as error:
        plan_config_migration(
            source_path=tmp_path / "explicit-source.toml",
            read_source=failing_reader,
            public_path=public_path,
            private_path=private_path,
            public_tree=public_tree,
            expected_owner="synthetic-owner",
            publisher=SyntheticPairPublisher(
                identities=_identities(), owner="synthetic-owner"
            ),
            prerequisite_authority=SyntheticPrerequisiteAuthority(),
            identities=_identities(),
        )
    assert str(error.value) == "legacy configuration unavailable"
    assert error.value.__cause__ is None
    assert canary not in str(error.value)
    assert str(tmp_path) not in str(error.value)
