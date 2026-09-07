"""Accepted W0 constants; later waves consume but do not choose these values."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

SEMANTIC_OPERATIONS: Final = ("commit", "query", "changes", "inspect")


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    labels_per_record: int = 16
    active_label_sets_per_brain: int = 256
    authorized_query_fanout_per_round: int = 32
    records_per_label_set: int = 50_000
    commit_batch_items: int = 128
    blob_staging_bytes: int = 64 * 1024 * 1024
    concurrent_requests_per_brain: int = 32
    concurrent_requests_per_principal: int = 8
    retained_nonces_per_brain: int = 100_000
    retained_nonces_per_principal: int = 10_000
    query_text_bytes: int = 4_096
    query_top_k: int = 100
    query_pages_per_grant: int = 256
    reciprocal_rank_fusion_constant: int = 60
    durable_job_attempts: int = 8
    owner_exempt: bool = False


@dataclass(frozen=True, slots=True)
class ClockPolicy:
    owner_session_seconds: int = 900
    step_up_freshness_seconds: int = 60
    grant_minimum_seconds: int = 30
    grant_maximum_seconds: int = 900
    utc_skew_allowance_seconds: int = 300


@dataclass(frozen=True, slots=True)
class CryptoProfile:
    version: int = 1
    payload_aead: str = "AES-256-GCM"
    payload_nonce_bytes: int = 12
    data_key_bytes: int = 32
    data_key_wrap: str = "AES-256-KW"
    argon2id_memory_kib: int = 65_536
    argon2id_iterations: int = 3
    argon2id_parallelism: int = 4
    argon2id_salt_bytes: int = 16
    argon2id_tag_bytes: int = 32
    sqlcipher_compatibility: int = 4
    sqlcipher_page_bytes: int = 4_096
    sqlcipher_kdf_iterations: int = 256_000
    sqlcipher_kdf: str = "PBKDF2_HMAC_SHA512"
    sqlcipher_page_hmac: str = "HMAC_SHA512"


@dataclass(frozen=True, slots=True)
class AuthorizationPolicy:
    principal_signature: str = "Ed25519"
    bearer_grants: bool = False
    nonce_store: str = "separate-encrypted-operational-store"
    reserve_nonce_before_operation: bool = True


RESOURCE_LIMITS: Final = ResourceLimits()
CLOCK_POLICY: Final = ClockPolicy()
CRYPTO_PROFILE: Final = CryptoProfile()
AUTHORIZATION_POLICY: Final = AuthorizationPolicy()
SECURITY_INVARIANTS: Final = MappingProxyType(
    {
        "authorization_before_body_decode": True,
        "authorization_before_candidate_selection": True,
        "changes_reauthorize_each_page": True,
        "continuation_invalidated_by_bound_snapshot_purge": True,
        "fts_query_input": "bounded-literal-text",
        "grant_control_transport": "peer-uid-checked-unix-socket",
        "grant_durable_plaintext": False,
        "healthz_metadata": False,
        "http_bind": "numeric-loopback-only",
        "http_cors": False,
        "http_default_browser_origin": False,
        "inspect_absent_equals_unauthorized": True,
        "internal_projection_checkpoint_serialized": False,
        "known_network_or_synchronized_roots": "rejected",
        "nonce_reservation_rolls_back": False,
        "plaintext_root_key_in_brain_root": False,
        "production_fake_user_presence": False,
        "receipt_replay_after_sensitive_purge": "delivery_purged",
        "sensitive_values_in_logs_or_errors": False,
        "sqlite_temporary_store": "memory-or-encrypted-brain-boundary",
    }
)
