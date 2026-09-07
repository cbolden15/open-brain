"""Key-custody and user-presence ports frozen for Reference Node adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

type KeyPurpose = Literal[
    "root",
    "owner-signing",
    "issuer-signing",
    "node-signing",
    "database",
    "wrapping",
    "data",
]


@dataclass(frozen=True, slots=True)
class KeyHandle:
    identifier: str
    brain_id: str
    purpose: KeyPurpose
    version: int


@dataclass(frozen=True, slots=True)
class PrincipalKeyHandle:
    identifier: str
    principal_id: str
    epoch: int
    public_key: bytes


@dataclass(frozen=True, slots=True)
class CiphertextEnvelope:
    crypto_version: int
    key_identifier: str
    nonce: bytes
    associated_data_digest: str
    ciphertext: bytes


@dataclass(frozen=True, slots=True)
class WrappedKeyEnvelope:
    crypto_version: int
    wrapping_key_identifier: str
    wrapped_key: bytes


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
    """Use Brain secrets through opaque handles; secret key bytes never cross this port."""

    def bootstrap(self, brain_id: str, brain_root: Path) -> KeyHandle: ...

    def unlock(
        self,
        brain_id: str,
        presence: UserPresenceProof,
    ) -> OwnerControlSession: ...

    def restart(self, brain_id: str) -> KeyHandle: ...

    def derive(
        self,
        brain_id: str,
        parent: KeyHandle,
        purpose: KeyPurpose,
        context: bytes,
    ) -> KeyHandle: ...

    def generate_data_key(self, brain_id: str) -> KeyHandle: ...

    def public_key(self, key: KeyHandle) -> bytes: ...

    def sign(self, key: KeyHandle, message: bytes) -> bytes: ...

    def encrypt(
        self,
        key: KeyHandle,
        nonce: bytes,
        plaintext: bytes,
        associated_data: bytes,
    ) -> CiphertextEnvelope: ...

    def decrypt(self, key: KeyHandle, envelope: CiphertextEnvelope) -> bytes: ...

    def wrap_key(self, wrapping_key: KeyHandle, data_key: KeyHandle) -> WrappedKeyEnvelope: ...

    def unwrap_key(self, wrapping_key: KeyHandle, envelope: WrappedKeyEnvelope) -> KeyHandle: ...

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

    def create(self, principal_id: str, *, ephemeral: bool = False) -> PrincipalKeyHandle: ...

    def load(self, principal_id: str) -> PrincipalKeyHandle: ...

    def sign(self, key: PrincipalKeyHandle, message: bytes) -> bytes: ...

    def rotate(
        self,
        key: PrincipalKeyHandle,
        presence: UserPresenceProof,
    ) -> PrincipalKeyHandle: ...

    def destroy(self, key: PrincipalKeyHandle, presence: UserPresenceProof) -> None: ...


@runtime_checkable
class UserPresenceProvider(Protocol):
    """Obtain a fresh platform result or audited passphrase re-entry proof."""

    def challenge(self, reason: str, *, maximum_age_seconds: int) -> UserPresenceProof: ...
