"""Explicit, redacted, capability-gated configuration migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from open_brain_legacy._compat.open_brain.config import (
    AppConfig,
    ConfigError,
    NamedSecretRef,
    RetainedRootIdentities,
    SecretRef,
    SecretRefKind,
)

SourceReader = Callable[[Path], Mapping[str, object]]


class ConfigMigrationError(RuntimeError):
    """Raised when migration inputs or capabilities fail safely."""


class ConfigMigrationBlockedError(ConfigMigrationError):
    """Raised when required authority or destination safety is unavailable."""


class RefusedConfigOverwriteError(ConfigMigrationBlockedError):
    """Raised when existing output lacks issuer-verified authority."""


class StaleConfigMigrationPlanError(ConfigMigrationBlockedError):
    """Raised when plan-bound inputs no longer match."""


class PublicationConflictError(ConfigMigrationBlockedError):
    """Raised by a publisher when exact compare-and-swap fails."""


class ConfigMigrationState(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"


class PrivateDestinationClass(StrEnum):
    PRIVATE = "private"
    PUBLIC_TREE = "public_tree"
    UNCONFINED = "unconfined"


_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_VERSION: Final = "config-migration-evidence-v1"
PRIVATE_MODE: Final = 0o600


@dataclass(frozen=True, slots=True)
class MigrationPathIdentities:
    """Injected physical identities for five roots and two destinations."""

    roots: RetainedRootIdentities
    public_destination: str
    private_destination: str
    public_tree: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Revalidate even if a frozen instance was forged after construction."""
        if not isinstance(self.roots, RetainedRootIdentities):
            raise ConfigMigrationError("invalid physical path identities")
        destinations = (
            self.public_destination,
            self.private_destination,
            self.public_tree,
        )
        if any(
            not isinstance(identity, str) or not _NAME_RE.fullmatch(identity)
            for identity in destinations
        ):
            raise ConfigMigrationError("invalid physical path identities")
        identities = self.roots.as_tuple() + destinations
        if len(set(identities)) != len(identities):
            raise ConfigMigrationError("physical aliases are not allowed")

    def digest(self) -> str:
        return _digest_parts(
            b"config-path-identities-v1",
            *(identity.encode() for identity in self.roots.as_tuple()),
            self.public_destination.encode(),
            self.private_destination.encode(),
            self.public_tree.encode(),
        )


@dataclass(frozen=True, slots=True)
class SecretMigrationValue:
    """Apply-only secret value paired with a public environment reference."""

    reference: SecretRef
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reference, SecretRef)
            or self.reference.kind is not SecretRefKind.ENVIRONMENT
            or not isinstance(self.value, str)
            or not self.value
            or any(character in self.value for character in ("\x00", "\r", "\n"))
        ):
            raise ConfigMigrationError("invalid secret migration value")


@dataclass(frozen=True, slots=True)
class PrerequisiteClaims:
    count: int
    ready: bool

    def __post_init__(self) -> None:
        if type(self.count) is not int or self.count < 0 or type(self.ready) is not bool:
            raise ConfigMigrationError("invalid prerequisite claims")


@dataclass(frozen=True, slots=True)
class PrerequisiteRequest:
    provider: str
    request_digest: str
    identity_digest: str
    evidence_version: str = EVIDENCE_VERSION


@dataclass(frozen=True, slots=True)
class AuthorityBinding:
    """Exact non-secret scope every apply authority must bind."""

    plan_digest: str
    request_digest: str
    destination_digest: str
    identity_digest: str
    evidence_version: str = EVIDENCE_VERSION


@dataclass(frozen=True, slots=True)
class PrerequisiteReceipt:
    version: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    version: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OverwriteReceipt:
    version: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    version: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RecoveryReceipt:
    version: str
    token: str = field(repr=False)


class PrerequisiteAuthority(Protocol):
    def probe(self, request: PrerequisiteRequest) -> PrerequisiteClaims: ...

    def issue(
        self, binding: AuthorityBinding, claims: PrerequisiteClaims
    ) -> PrerequisiteReceipt: ...

    def verify(
        self, receipt: PrerequisiteReceipt, binding: AuthorityBinding
    ) -> PrerequisiteClaims | None: ...


class BackupAuthority(Protocol):
    def verify(self, receipt: BackupReceipt, binding: AuthorityBinding) -> bool: ...


class OverwriteAuthority(Protocol):
    def verify(self, receipt: OverwriteReceipt, binding: AuthorityBinding) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class DestinationSnapshot:
    """Publisher-issued exact pair state and private-destination metadata."""

    public_payload: bytes | None = field(repr=False)
    private_payload: bytes | None = field(repr=False)
    public_identity: str
    private_identity: str
    private_classification: PrivateDestinationClass
    private_confined: bool
    private_no_follow: bool
    private_is_symlink: bool
    private_owner: str | None
    private_mode: int | None
    recovery_required: bool = False

    def digest(self) -> str:
        return _snapshot_digest(self.public_payload, self.private_payload)


@dataclass(frozen=True, slots=True, repr=False)
class PublicationRequest:
    """Exact compare-and-swap request owned by the transactional publisher."""

    binding: AuthorityBinding
    public_path: Path = field(repr=False)
    private_path: Path = field(repr=False)
    expected_public: bytes | None = field(repr=False)
    expected_private: bytes | None = field(repr=False)
    desired_public: bytes = field(repr=False)
    desired_private: bytes = field(repr=False)
    public_identity: str
    private_identity: str
    expected_owner: str
    private_mode: int = PRIVATE_MODE

    def before_digest(self) -> str:
        return _snapshot_digest(self.expected_public, self.expected_private)

    def after_digest(self) -> str:
        return _snapshot_digest(self.desired_public, self.desired_private)


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryRequest:
    """Validated authority and confinement scope for mutating recovery."""

    binding: AuthorityBinding
    public_path: Path = field(repr=False)
    private_path: Path = field(repr=False)
    public_tree: Path = field(repr=False)
    public_identity: str
    private_identity: str
    public_tree_identity: str
    identity_digest: str
    expected_owner: str
    evidence_version: str = EVIDENCE_VERSION


class ConfigPairPublisher(Protocol):
    """Confined publisher responsible for CAS, rollback, recovery, and durability."""

    def inspect(self, public_path: Path, private_path: Path) -> DestinationSnapshot: ...

    def recover(self, request: RecoveryRequest) -> RecoveryReceipt: ...

    def verify_recovery(
        self, receipt: RecoveryReceipt, request: RecoveryRequest
    ) -> bool: ...

    def publish(self, request: PublicationRequest) -> PublicationReceipt: ...

    def verify_publication(
        self,
        receipt: PublicationReceipt,
        request: PublicationRequest,
        observed: DestinationSnapshot,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ConfigMigrationPlan:
    """Redacted envelope authenticated by an issuer-bound prerequisite receipt."""

    request_digest: str
    destination_digest: str
    identity_digest: str
    plan_digest: str
    root_count: int
    secret_count: int
    prerequisite_count: int
    existing_output_count: int
    change_count: int
    ready: bool
    prerequisite_receipt: PrerequisiteReceipt = field(repr=False)
    evidence_version: str = EVIDENCE_VERSION

    def __post_init__(self) -> None:
        digests = (
            self.request_digest,
            self.destination_digest,
            self.identity_digest,
            self.plan_digest,
        )
        counts = (
            self.root_count,
            self.secret_count,
            self.prerequisite_count,
            self.existing_output_count,
            self.change_count,
        )
        if any(not isinstance(value, str) or not _DIGEST_RE.fullmatch(value) for value in digests):
            raise ConfigMigrationError("invalid migration plan")
        if any(type(value) is not int or value < 0 for value in counts):
            raise ConfigMigrationError("invalid migration plan")
        if (
            type(self.ready) is not bool
            or not isinstance(self.prerequisite_receipt, PrerequisiteReceipt)
            or self.evidence_version != EVIDENCE_VERSION
        ):
            raise ConfigMigrationError("invalid migration plan")

    def binding(self) -> AuthorityBinding:
        return AuthorityBinding(
            plan_digest=self.plan_digest,
            request_digest=self.request_digest,
            destination_digest=self.destination_digest,
            identity_digest=self.identity_digest,
            evidence_version=self.evidence_version,
        )

    def to_redacted_dict(self) -> dict[str, object]:
        return {
            "change_count": self.change_count,
            "existing_output_count": self.existing_output_count,
            "output_count": 2,
            "plan_digest": self.plan_digest,
            "prerequisite_count": self.prerequisite_count,
            "ready": self.ready,
            "root_count": self.root_count,
            "secret_count": self.secret_count,
        }


@dataclass(frozen=True, slots=True)
class ConfigMigrationResult:
    state: ConfigMigrationState
    output_count: int
    plan_digest: str
    publication_receipt: PublicationReceipt | None = field(default=None, repr=False)

    def to_redacted_dict(self) -> dict[str, object]:
        return {
            "output_count": self.output_count,
            "plan_digest": self.plan_digest,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True, repr=False)
class _NormalizedSource:
    config: AppConfig = field(repr=False)
    secrets: tuple[tuple[NamedSecretRef, SecretMigrationValue], ...] = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedMigration:
    public_path: Path = field(repr=False)
    private_path: Path = field(repr=False)
    public_payload: bytes = field(repr=False)
    private_payload: bytes = field(repr=False)
    snapshot: DestinationSnapshot = field(repr=False)
    request_digest: str
    identity_digest: str
    provider: str
    root_count: int
    secret_count: int


@dataclass(frozen=True, slots=True, repr=False)
class _StaticPreparation:
    public_path: Path = field(repr=False)
    private_path: Path = field(repr=False)
    public_tree: Path = field(repr=False)
    public_payload: bytes = field(repr=False)
    private_payload: bytes = field(repr=False)
    request_digest: str
    identity_digest: str
    provider: str
    root_count: int
    secret_count: int


def plan_config_migration(
    *,
    public_path: Path,
    private_path: Path,
    public_tree: Path,
    expected_owner: str,
    publisher: ConfigPairPublisher,
    prerequisite_authority: PrerequisiteAuthority,
    identities: MigrationPathIdentities,
    source: Mapping[str, object] | None = None,
    source_path: Path | None = None,
    read_source: SourceReader | None = None,
) -> ConfigMigrationPlan:
    static = _prepare_static(
        source=source,
        source_path=source_path,
        read_source=read_source,
        public_path=public_path,
        private_path=private_path,
        public_tree=public_tree,
        expected_owner=expected_owner,
        identities=identities,
    )
    prepared = _prepare_destination(
        static=static,
        publisher=publisher,
        expected_owner=expected_owner,
        identities=identities,
    )
    claims = _probe_prerequisites(
        prerequisite_authority,
        PrerequisiteRequest(
            provider=static.provider,
            request_digest=static.request_digest,
            identity_digest=static.identity_digest,
        ),
    )
    existing_count = sum(
        payload is not None
        for payload in (prepared.snapshot.public_payload, prepared.snapshot.private_payload)
    )
    change_count = sum(
        current != desired
        for current, desired in (
            (prepared.snapshot.public_payload, prepared.public_payload),
            (prepared.snapshot.private_payload, prepared.private_payload),
        )
    )
    ready = claims.ready and (change_count == 0 or existing_count == 0)
    plan_digest = _canonical_plan_digest(
        request_digest=prepared.request_digest,
        destination_digest=prepared.snapshot.digest(),
        identity_digest=prepared.identity_digest,
        root_count=prepared.root_count,
        secret_count=prepared.secret_count,
        prerequisite_count=claims.count,
        existing_output_count=existing_count,
        change_count=change_count,
        ready=ready,
    )
    provisional = ConfigMigrationPlan(
        request_digest=prepared.request_digest,
        destination_digest=prepared.snapshot.digest(),
        identity_digest=prepared.identity_digest,
        plan_digest=plan_digest,
        root_count=prepared.root_count,
        secret_count=prepared.secret_count,
        prerequisite_count=claims.count,
        existing_output_count=existing_count,
        change_count=change_count,
        ready=ready,
        prerequisite_receipt=PrerequisiteReceipt(EVIDENCE_VERSION, "pending"),
    )
    try:
        receipt = prerequisite_authority.issue(provisional.binding(), claims)
    except Exception:
        raise ConfigMigrationBlockedError("prerequisite authority unavailable") from None
    plan = replace(provisional, prerequisite_receipt=receipt)
    if _verify_prerequisites(prerequisite_authority, plan) != claims:
        raise ConfigMigrationBlockedError("prerequisite authority unavailable")
    return plan


def apply_config_migration(
    *,
    plan: ConfigMigrationPlan,
    public_path: Path,
    private_path: Path,
    public_tree: Path,
    expected_owner: str,
    publisher: ConfigPairPublisher,
    prerequisite_authority: PrerequisiteAuthority,
    identities: MigrationPathIdentities,
    source: Mapping[str, object] | None = None,
    source_path: Path | None = None,
    read_source: SourceReader | None = None,
    backup_authority: BackupAuthority | None = None,
    backup_receipt: BackupReceipt | None = None,
    overwrite_authority: OverwriteAuthority | None = None,
    overwrite_receipt: OverwriteReceipt | None = None,
) -> ConfigMigrationResult:
    if not isinstance(plan, ConfigMigrationPlan):
        raise ConfigMigrationError("invalid migration apply request")
    static = _prepare_static(
        source=source,
        source_path=source_path,
        read_source=read_source,
        public_path=public_path,
        private_path=private_path,
        public_tree=public_tree,
        expected_owner=expected_owner,
        identities=identities,
    )
    claims = _validate_plan_static(plan, static, prerequisite_authority)
    if not claims.ready:
        raise ConfigMigrationBlockedError("prerequisites unavailable")
    recovery_request = _recovery_request(
        binding=plan.binding(),
        static=static,
        identities=identities,
        expected_owner=expected_owner,
    )
    _recover(publisher, recovery_request)
    prepared = _prepare_destination(
        static=static,
        publisher=publisher,
        expected_owner=expected_owner,
        identities=identities,
    )
    _validate_plan_destination(plan, prepared)
    if (
        prepared.snapshot.public_payload == prepared.public_payload
        and prepared.snapshot.private_payload == prepared.private_payload
    ):
        return ConfigMigrationResult(ConfigMigrationState.NOOP, 0, plan.plan_digest)
    if prepared.snapshot.digest() != plan.destination_digest:
        raise StaleConfigMigrationPlanError("migration plan is stale")
    if plan.existing_output_count:
        _verify_overwrite_authorities(
            plan,
            backup_authority,
            backup_receipt,
            overwrite_authority,
            overwrite_receipt,
        )
    request = PublicationRequest(
        binding=plan.binding(),
        public_path=prepared.public_path,
        private_path=prepared.private_path,
        expected_public=prepared.snapshot.public_payload,
        expected_private=prepared.snapshot.private_payload,
        desired_public=prepared.public_payload,
        desired_private=prepared.private_payload,
        public_identity=identities.public_destination,
        private_identity=identities.private_destination,
        expected_owner=expected_owner,
    )
    try:
        receipt = publisher.publish(request)
    except PublicationConflictError:
        raise StaleConfigMigrationPlanError("migration plan is stale") from None
    except Exception:
        _verify_rollback(publisher, request, static.public_tree, expected_owner, identities)
        raise ConfigMigrationError("configuration publication failed") from None
    observed = _inspect(publisher, prepared.public_path, prepared.private_path)
    try:
        _validate_destination(
            snapshot=observed,
            public_path=prepared.public_path,
            private_path=prepared.private_path,
            public_tree=public_tree,
            expected_owner=expected_owner,
            identities=identities,
            require_private_file=True,
        )
        verified = publisher.verify_publication(receipt, request, observed)
    except Exception:
        verified = False
    if (
        not verified
        or observed.public_payload != prepared.public_payload
        or observed.private_payload != prepared.private_payload
    ):
        _verify_rollback(publisher, request, static.public_tree, expected_owner, identities)
        raise ConfigMigrationError("configuration publication verification failed")
    return ConfigMigrationResult(
        ConfigMigrationState.APPLIED,
        2,
        plan.plan_digest,
        publication_receipt=receipt,
    )


def _prepare_static(
    *,
    source: Mapping[str, object] | None,
    source_path: Path | None,
    read_source: SourceReader | None,
    public_path: Path,
    private_path: Path,
    public_tree: Path,
    expected_owner: str,
    identities: MigrationPathIdentities,
) -> _StaticPreparation:
    loaded_source, source_identity = _load_source(source, source_path, read_source)
    try:
        normalized = _normalize_source(loaded_source)
        identities.validate()
        normalized.config.roots.validate_identities(identities.roots)
    except (ConfigError, ConfigMigrationError, TypeError, ValueError):
        raise ConfigMigrationError("legacy configuration or identities invalid") from None
    normalized_public = _explicit_path(public_path)
    normalized_private = _explicit_path(private_path)
    normalized_tree = _explicit_path(public_tree)
    if not isinstance(expected_owner, str) or not _NAME_RE.fullmatch(expected_owner):
        raise ConfigMigrationError("invalid private output owner")
    all_paths = normalized.config.roots.as_tuple() + (
        normalized.config.backup_root,
        normalized_public,
        normalized_private,
    )
    if any(
        _paths_overlap(left, right)
        for index, left in enumerate(all_paths)
        for right in all_paths[index + 1 :]
    ):
        raise ConfigMigrationError("roots and destinations must be non-overlapping")
    if normalized_tree not in normalized_public.parents or _within(
        normalized_private, normalized_tree
    ):
        raise ConfigMigrationBlockedError("private destination is not confined")
    public_payload = _render_public(normalized)
    private_payload = _render_private(normalized)
    identity_digest = identities.digest()
    request_digest = _digest_parts(
        b"config-request-v2",
        source_identity,
        os.fspath(normalized_public).encode(),
        os.fspath(normalized_private).encode(),
        os.fspath(normalized_tree).encode(),
        public_payload,
        private_payload,
        identity_digest.encode(),
        expected_owner.encode(),
    )
    return _StaticPreparation(
        public_path=normalized_public,
        private_path=normalized_private,
        public_tree=normalized_tree,
        public_payload=public_payload,
        private_payload=private_payload,
        request_digest=request_digest,
        identity_digest=identity_digest,
        provider=normalized.config.provider,
        root_count=5,
        secret_count=len(normalized.secrets),
    )


def _prepare_destination(
    *,
    static: _StaticPreparation,
    publisher: ConfigPairPublisher,
    expected_owner: str,
    identities: MigrationPathIdentities,
) -> _PreparedMigration:
    snapshot = _inspect(publisher, static.public_path, static.private_path)
    _validate_destination(
        snapshot=snapshot,
        public_path=static.public_path,
        private_path=static.private_path,
        public_tree=static.public_tree,
        expected_owner=expected_owner,
        identities=identities,
        require_private_file=False,
    )
    return _PreparedMigration(
        public_path=static.public_path,
        private_path=static.private_path,
        public_payload=static.public_payload,
        private_payload=static.private_payload,
        snapshot=snapshot,
        request_digest=static.request_digest,
        identity_digest=static.identity_digest,
        provider=static.provider,
        root_count=static.root_count,
        secret_count=static.secret_count,
    )


def _validate_plan_static(
    plan: ConfigMigrationPlan,
    static: _StaticPreparation,
    prerequisite_authority: PrerequisiteAuthority,
) -> PrerequisiteClaims:
    canonical = _canonical_plan_digest(
        request_digest=plan.request_digest,
        destination_digest=plan.destination_digest,
        identity_digest=plan.identity_digest,
        root_count=plan.root_count,
        secret_count=plan.secret_count,
        prerequisite_count=plan.prerequisite_count,
        existing_output_count=plan.existing_output_count,
        change_count=plan.change_count,
        ready=plan.ready,
    )
    claims = _verify_prerequisites(prerequisite_authority, plan)
    expected_ready = claims is not None and claims.ready and (
        plan.change_count == 0 or plan.existing_output_count == 0
    )
    if (
        claims is None
        or plan.plan_digest != canonical
        or plan.request_digest != static.request_digest
        or plan.identity_digest != static.identity_digest
        or plan.root_count != static.root_count
        or plan.secret_count != static.secret_count
        or plan.prerequisite_count != claims.count
        or plan.existing_output_count > 2
        or plan.change_count > 2
        or plan.ready is not expected_ready
    ):
        raise StaleConfigMigrationPlanError("migration plan is stale")
    return claims


def _validate_plan_destination(
    plan: ConfigMigrationPlan, prepared: _PreparedMigration
) -> None:
    current_is_desired = (
        prepared.snapshot.public_payload == prepared.public_payload
        and prepared.snapshot.private_payload == prepared.private_payload
    )
    if current_is_desired:
        return
    existing_count = sum(
        payload is not None
        for payload in (prepared.snapshot.public_payload, prepared.snapshot.private_payload)
    )
    change_count = sum(
        current != desired
        for current, desired in (
            (prepared.snapshot.public_payload, prepared.public_payload),
            (prepared.snapshot.private_payload, prepared.private_payload),
        )
    )
    if (
        prepared.snapshot.digest() != plan.destination_digest
        or existing_count != plan.existing_output_count
        or change_count != plan.change_count
    ):
        raise StaleConfigMigrationPlanError("migration plan is stale")


def _verify_overwrite_authorities(
    plan: ConfigMigrationPlan,
    backup_authority: BackupAuthority | None,
    backup_receipt: BackupReceipt | None,
    overwrite_authority: OverwriteAuthority | None,
    overwrite_receipt: OverwriteReceipt | None,
) -> None:
    try:
        backup_ok = (
            backup_authority is not None
            and isinstance(backup_receipt, BackupReceipt)
            and backup_authority.verify(backup_receipt, plan.binding())
        )
        overwrite_ok = (
            overwrite_authority is not None
            and isinstance(overwrite_receipt, OverwriteReceipt)
            and overwrite_authority.verify(overwrite_receipt, plan.binding())
        )
    except Exception:
        backup_ok = overwrite_ok = False
    if not backup_ok or not overwrite_ok:
        raise RefusedConfigOverwriteError("issuer-verified backup and overwrite authority required")


def _probe_prerequisites(
    authority: PrerequisiteAuthority, request: PrerequisiteRequest
) -> PrerequisiteClaims:
    try:
        claims = authority.probe(request)
    except Exception:
        raise ConfigMigrationBlockedError("prerequisite authority unavailable") from None
    if not isinstance(claims, PrerequisiteClaims):
        raise ConfigMigrationBlockedError("prerequisite authority unavailable")
    return claims


def _verify_prerequisites(
    authority: PrerequisiteAuthority, plan: ConfigMigrationPlan
) -> PrerequisiteClaims | None:
    try:
        claims = authority.verify(plan.prerequisite_receipt, plan.binding())
    except Exception:
        return None
    return claims if isinstance(claims, PrerequisiteClaims) else None


def _recovery_request(
    *,
    binding: AuthorityBinding,
    static: _StaticPreparation,
    identities: MigrationPathIdentities,
    expected_owner: str,
) -> RecoveryRequest:
    return RecoveryRequest(
        binding=binding,
        public_path=static.public_path,
        private_path=static.private_path,
        public_tree=static.public_tree,
        public_identity=identities.public_destination,
        private_identity=identities.private_destination,
        public_tree_identity=identities.public_tree,
        identity_digest=identities.digest(),
        expected_owner=expected_owner,
    )


def _recover(publisher: ConfigPairPublisher, request: RecoveryRequest) -> None:
    try:
        receipt = publisher.recover(request)
        verified = publisher.verify_recovery(receipt, request)
    except Exception:
        verified = False
    if not verified:
        raise ConfigMigrationBlockedError("configuration recovery unavailable")


def _verify_rollback(
    publisher: ConfigPairPublisher,
    request: PublicationRequest,
    public_tree: Path,
    expected_owner: str,
    identities: MigrationPathIdentities,
) -> None:
    recovery_request = RecoveryRequest(
        binding=request.binding,
        public_path=request.public_path,
        private_path=request.private_path,
        public_tree=public_tree,
        public_identity=identities.public_destination,
        private_identity=identities.private_destination,
        public_tree_identity=identities.public_tree,
        identity_digest=identities.digest(),
        expected_owner=expected_owner,
    )
    _recover(publisher, recovery_request)
    restored = _inspect(publisher, request.public_path, request.private_path)
    _validate_destination(
        snapshot=restored,
        public_path=request.public_path,
        private_path=request.private_path,
        public_tree=public_tree,
        expected_owner=expected_owner,
        identities=identities,
        require_private_file=request.expected_private is not None,
    )
    if (
        restored.public_payload != request.expected_public
        or restored.private_payload != request.expected_private
    ):
        raise ConfigMigrationError("configuration rollback failed")


def _inspect(
    publisher: ConfigPairPublisher, public_path: Path, private_path: Path
) -> DestinationSnapshot:
    try:
        snapshot = publisher.inspect(public_path, private_path)
    except Exception:
        raise ConfigMigrationBlockedError("configuration destinations unavailable") from None
    if not isinstance(snapshot, DestinationSnapshot):
        raise ConfigMigrationBlockedError("configuration destinations unavailable")
    return snapshot


def _validate_destination(
    *,
    snapshot: DestinationSnapshot,
    public_path: Path,
    private_path: Path,
    public_tree: Path,
    expected_owner: str,
    identities: MigrationPathIdentities,
    require_private_file: bool,
) -> None:
    if snapshot.recovery_required:
        raise ConfigMigrationBlockedError("configuration recovery required")
    if public_tree not in public_path.parents or _within(private_path, public_tree):
        raise ConfigMigrationBlockedError("private destination is not confined")
    if (
        snapshot.public_identity != identities.public_destination
        or snapshot.private_identity != identities.private_destination
        or snapshot.private_classification is not PrivateDestinationClass.PRIVATE
        or not snapshot.private_confined
        or not snapshot.private_no_follow
        or snapshot.private_is_symlink
    ):
        raise ConfigMigrationBlockedError("private destination is unsafe")
    private_exists = snapshot.private_payload is not None
    if require_private_file and not private_exists:
        raise ConfigMigrationBlockedError("private destination publication missing")
    if private_exists and (
        snapshot.private_owner != expected_owner or snapshot.private_mode != PRIVATE_MODE
    ):
        raise ConfigMigrationBlockedError("private destination permissions are unsafe")


def _load_source(
    source: Mapping[str, object] | None,
    source_path: Path | None,
    read_source: SourceReader | None,
) -> tuple[Mapping[str, object], bytes]:
    if source is not None:
        if source_path is not None or read_source is not None or not isinstance(source, Mapping):
            raise ConfigMigrationError("supply exactly one legacy configuration source")
        return source, b"mapping"
    if source_path is None or not callable(read_source):
        raise ConfigMigrationError("supply exactly one legacy configuration source")
    normalized_path = _explicit_path(source_path)
    try:
        loaded = read_source(normalized_path)
    except Exception:
        raise ConfigMigrationError("legacy configuration unavailable") from None
    if not isinstance(loaded, Mapping):
        raise ConfigMigrationError("legacy configuration invalid")
    return loaded, os.fspath(normalized_path).encode()


def _normalize_source(source: Mapping[str, object]) -> _NormalizedSource:
    explicit = dict(source)
    raw_secrets = explicit.pop("secrets", {})
    if not isinstance(raw_secrets, Mapping) or not all(
        isinstance(name, str) for name in raw_secrets
    ):
        raise ConfigMigrationError("legacy configuration invalid")
    values: dict[str, SecretMigrationValue] = {}
    references: dict[str, SecretRef] = {}
    for name, value in raw_secrets.items():
        if not isinstance(value, SecretMigrationValue):
            raise ConfigMigrationError("legacy configuration invalid")
        values[name] = value
        references[name] = value.reference
    if references:
        explicit["secrets"] = references
    config = AppConfig.from_sources(explicit=explicit, environment={})
    named_values = tuple((reference, values[reference.name]) for reference in config.secret_refs)
    environment_names = [value.reference.value for _, value in named_values]
    if len(environment_names) != len(set(environment_names)):
        raise ConfigMigrationError("legacy configuration invalid")
    return _NormalizedSource(config=config, secrets=named_values)


def _render_public(source: _NormalizedSource) -> bytes:
    roots = source.config.roots.to_dict()
    lines = [
        "[paths]",
        f"work_root = {_toml_string(roots['work_root'])}",
        f"personal_root = {_toml_string(roots['personal_root'])}",
        f"capture_root = {_toml_string(roots['capture_root'])}",
        f"saved_content_root = {_toml_string(roots['saved_content_root'])}",
        f"state_root = {_toml_string(roots['state_root'])}",
        f"backup_root = {_toml_string(os.fspath(source.config.backup_root))}",
    ]
    if source.config.host_identity is not None:
        lines.extend(
            (
                "",
                "[host]",
                f"identity = {_toml_string(source.config.host_identity)}",
            )
        )
    lines.extend(
        (
            "",
            "[providers]",
            f"default = {_toml_string(source.config.provider)}",
            f"cloud_enabled = {_toml_boolean(source.config.cloud_enabled)}",
            "",
            "[egress]",
            f"enabled = {_toml_boolean(source.config.egress_enabled)}",
        )
    )
    if source.config.secret_refs:
        lines.extend(("", "[secrets]"))
        lines.extend(
            f"{reference.name} = {_toml_string(reference.reference.to_string())}"
            for reference in source.config.secret_refs
        )
    return ("\n".join(lines) + "\n").encode()


def _render_private(source: _NormalizedSource) -> bytes:
    lines = sorted(
        f"{value.reference.value}={_shell_string(value.value)}"
        for _, value in source.secrets
    )
    return (("\n".join(lines) + "\n") if lines else "").encode()


def _canonical_plan_digest(
    *,
    request_digest: str,
    destination_digest: str,
    identity_digest: str,
    root_count: int,
    secret_count: int,
    prerequisite_count: int,
    existing_output_count: int,
    change_count: int,
    ready: bool,
) -> str:
    envelope = {
        "change_count": change_count,
        "destination_digest": destination_digest,
        "evidence_version": EVIDENCE_VERSION,
        "existing_output_count": existing_output_count,
        "identity_digest": identity_digest,
        "prerequisite_count": prerequisite_count,
        "ready": ready,
        "request_digest": request_digest,
        "root_count": root_count,
        "secret_count": secret_count,
    }
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return _digest_parts(b"config-plan-envelope-v2", payload)


def _snapshot_digest(public_payload: bytes | None, private_payload: bytes | None) -> str:
    framed: list[bytes] = [b"config-destination-v2"]
    for payload in (public_payload, private_payload):
        framed.extend((b"missing",) if payload is None else (b"present", payload))
    return _digest_parts(*framed)


def _digest_parts(*parts: bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _toml_boolean(value: bool) -> str:
    return "true" if value else "false"


def _shell_string(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _explicit_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise ConfigMigrationError("explicit path required")
    text = os.fspath(path)
    if "~" in text or any(ord(character) < 32 for character in text) or not path.is_absolute():
        raise ConfigMigrationError("explicit absolute path required")
    return Path(os.path.normpath(text))


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
