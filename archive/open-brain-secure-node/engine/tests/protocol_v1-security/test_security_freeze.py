from __future__ import annotations

from open_brain_engine.protocol import SECURITY_INVARIANTS


def test_w0_security_invariants_are_executable_and_fail_closed() -> None:
    assert SECURITY_INVARIANTS == {
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
