"""Owner-local ``ob.embedding-health.v1``: text-free, metadata-only, never a helper launch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from open_brain_engine.engine.embedding_backend import EmbeddingFailure
from open_brain_engine.engine.embedding_cache import ActiveGeneration, active_status
from open_brain_engine.engine.embedding_policy import EmbeddingPolicy, EmbeddingPolicyTasks

from .embedding_helper import discover_embedding_resource, verify_embedding_metadata
from .t03_adapters import owner_authority

EMBEDDING_HEALTH_SCHEMA = "ob.embedding-health.v1"
_RESOURCE_STATES = {
    "resource_missing": "missing",
    "unsupported_platform": "unsupported",
}


def _resource(base_executable: Path | None) -> tuple[str, str | None, str | None]:
    """Resource state, verified component ID and the parent error category."""
    try:
        verified = verify_embedding_metadata(discover_embedding_resource(base_executable))
    except EmbeddingFailure as error:
        return _RESOURCE_STATES.get(error.category, "untrusted"), None, error.category
    return "ready", verified.component_id, None


def _consent(policy: EmbeddingPolicy) -> str:
    if policy.enabled:
        return "enabled"
    return "revoked" if policy.consent_version > 0 else "required"


def embedding_health(tasks: Any, *, base_executable: Path | None = None) -> dict[str, object]:
    """Compose resource, cache and consent state without inference or source counts."""
    resource_state, component_id, last_error = _resource(base_executable)
    policy_tasks = getattr(tasks, "embedding_policy", None)
    if not isinstance(policy_tasks, EmbeddingPolicyTasks):
        # No policy surface never breaks ``status``/``doctor``: nothing was consented to.
        policy: EmbeddingPolicy | None = None
    else:
        policy = policy_tasks.status(authority=owner_authority(tasks, session_id="owner-health"))
    consent_state = "required" if policy is None else _consent(policy)
    cache_state = "absent"
    if policy is not None and policy_tasks is not None and policy.enabled:
        # Reads the owner-local cache pointer and generation only; no vectors leave this call.
        cache = active_status(policy_tasks._engine)
        if isinstance(cache, ActiveGeneration):
            stale = component_id is not None and (
                cache.component_id != component_id or policy.component_id != component_id
            )
            cache_state = "stale" if stale else "ready"
            if stale:
                last_error = last_error or "component_mismatch"
        elif cache.reason == "cache_invalid":
            cache_state, last_error = "stale", last_error or "cache_invalid"
        elif cache.reason == "cache_incomplete":
            cache_state, last_error = "incomplete", last_error or "cache_incomplete"
    return {
        "schema": EMBEDDING_HEALTH_SCHEMA,
        "resource_state": resource_state,
        "cache_state": cache_state,
        "consent_state": consent_state,
        "component_id": component_id,
        "last_error": last_error,
    }


__all__ = ["EMBEDDING_HEALTH_SCHEMA", "embedding_health"]
