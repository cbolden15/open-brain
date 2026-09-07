"""Key-custody and user-presence ports frozen for Reference Node adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class KeyHandle:
    identifier: str
    version: int


@dataclass(frozen=True, slots=True)
class OwnerControlSession:
    identifier: str
    expires_monotonic: float


@dataclass(frozen=True, slots=True)
class UserPresenceProof:
    provider: str
    reason: str
    observed_monotonic: float


@dataclass(frozen=True, slots=True)
class KeyDestructionProof:
    key_identifier: str
    destroyed_at_utc: str
    provider_receipt: str


@runtime_checkable
class RootKeyCustodian(Protocol):
    """Own Brain root, owner, issuer, Node, and wrapping keys without exporting bytes."""

    def bootstrap(self, brain_id: str, brain_root: Path) -> KeyHandle: ...

    def unlock(
        self,
        brain_id: str,
        presence: UserPresenceProof,
    ) -> OwnerControlSession: ...

    def restart(self, brain_id: str) -> KeyHandle: ...

    def rotate(
        self,
        brain_id: str,
        presence: UserPresenceProof,
    ) -> KeyHandle: ...

    def destroy(
        self,
        brain_id: str,
        key: KeyHandle,
        presence: UserPresenceProof,
    ) -> KeyDestructionProof: ...


@runtime_checkable
class PrincipalKeyCustodian(Protocol):
    """Own client credentials without access to Brain root or Node signing keys."""

    def create(self, principal_id: str, *, ephemeral: bool = False) -> bytes: ...

    def load_public_key(self, principal_id: str) -> bytes: ...

    def sign(self, principal_id: str, message: bytes) -> bytes: ...

    def rotate(self, principal_id: str, presence: UserPresenceProof) -> bytes: ...

    def destroy(self, principal_id: str, presence: UserPresenceProof) -> None: ...


@runtime_checkable
class UserPresenceProvider(Protocol):
    """Obtain a fresh platform result or audited passphrase re-entry proof."""

    def challenge(self, reason: str, *, maximum_age_seconds: int) -> UserPresenceProof: ...
