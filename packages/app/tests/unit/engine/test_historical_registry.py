"""Independent denial identity survives optional relational projection loss."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine.historical_contracts import HistoricalDestination
from open_brain_engine.engine.historical_registry import (
    HistoricalClaimMembership,
    HistoricalClaimRegistry,
    HistoricalRegistryStore,
)
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    RootConfinementError,
    capture_root_identity,
)


def _destination() -> HistoricalDestination:
    return HistoricalDestination(brain_id="brn_synthetic", issuer_epoch=1)


def _original() -> HistoricalClaimMembership:
    return HistoricalClaimMembership(
        capture_id="capture_original",
        source_id="source_original",
        capture_source_id="source_original",
        claim_role="baseline_original",
    )


def _copy() -> HistoricalClaimMembership:
    return HistoricalClaimMembership(
        capture_id="capture_copy",
        source_id="source_original",
        capture_source_id="source_copy",
        claim_role="historical_copy",
    )


def _store(tmp_path: Path) -> HistoricalRegistryStore:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    return HistoricalRegistryStore(root, capture_root_identity(root))


def test_empty_registry_and_both_roles_round_trip() -> None:
    empty = HistoricalClaimRegistry.empty(_destination())
    assert empty.generation == 0 and empty.memberships == ()
    first = empty.register(_original())
    both = first.register(_copy())
    assert both.generation == 2
    assert {item.claim_role for item in both.memberships} == {
        "baseline_original",
        "historical_copy",
    }
    assert HistoricalClaimRegistry.from_bytes(both.canonical_bytes()) == both
    assert both.register(_copy()) == both
    assert both.registry_sha256 != first.registry_sha256


@pytest.mark.parametrize("change", ["role", "source", "capture_source"])
def test_existing_capture_membership_cannot_change(change: str) -> None:
    registry = HistoricalClaimRegistry.empty(_destination()).register(_copy())
    changes = {
        "role": {"claim_role": "baseline_original", "capture_source_id": "source_original"},
        "source": {"source_id": "source_other"},
        "capture_source": {"capture_source_id": "source_other"},
    }
    with pytest.raises(SharingError, match="invalid_arguments"):
        registry.register(replace(_copy(), **changes[change]))


def test_registry_refuses_tampering_and_duplicate_json_keys() -> None:
    registry = HistoricalClaimRegistry.empty(_destination()).register(_copy())
    value = json.loads(registry.canonical_bytes())
    value["memberships"] = []
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalClaimRegistry.from_bytes(json.dumps(value).encode())
    raw = registry.canonical_bytes().replace(b'"generation":1', b'"generation":1,"generation":1')
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalClaimRegistry.from_bytes(raw)


def test_store_missing_registry_is_not_an_empty_registry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SharingError, match="binding_mismatch"):
        store.read(_destination())
    empty = store.initialize_empty(_destination())
    assert store.read(_destination()) == empty
    assert store.initialize_empty(_destination()) == empty
    different = replace(_destination(), issuer_epoch=2)
    with pytest.raises(SharingError, match="binding_mismatch"):
        store.read(different)


def test_atomic_store_preserves_membership_and_rejects_stale_cas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    empty = store.initialize_empty(_destination())
    first = empty.register(_original())
    store.advance(empty, first)
    with pytest.raises(DuplicateConflictError):
        store.advance(empty, empty.register(_copy()))
    both = first.register(_copy())
    store.advance(first, both)
    assert store.read(_destination()) == both
    with pytest.raises(SharingError, match="invalid_arguments"):
        store.advance(both, first)
    # Reinitialization cannot erase managed identity.
    with pytest.raises(DuplicateConflictError):
        store.initialize_empty(_destination())


def test_registry_store_remains_bound_to_opened_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize_empty(_destination())
    root = tmp_path / "root"
    root.rename(tmp_path / "retained-root")
    root.mkdir(mode=0o700)
    with pytest.raises(RootConfinementError):
        store.read(_destination())
    with pytest.raises(RootConfinementError):
        store.initialize_empty(_destination())


@pytest.mark.parametrize(
    "discrepancy",
    [
        "generation",
        "digest",
        "missing_original",
        "missing_copy",
        "duplicate_copy",
        "different_source",
    ],
)
def test_projection_discrepancies_fence_before_optional_link_lookup(discrepancy: str) -> None:
    registry = HistoricalClaimRegistry.empty(_destination()).register(_original()).register(_copy())
    generation = registry.generation
    digest = registry.registry_sha256
    memberships = registry.memberships
    if discrepancy == "generation":
        generation += 1
    elif discrepancy == "digest":
        digest = "0" * 64
    elif discrepancy == "missing_original":
        memberships = (_copy(),)
    elif discrepancy == "missing_copy":
        memberships = (_original(),)
    elif discrepancy == "duplicate_copy":
        memberships = (*memberships, _copy())
    else:
        memberships = (_original(), replace(_copy(), capture_source_id="source_other"))
    with pytest.raises(SharingError, match="binding_mismatch"):
        registry.verify_projection(
            generation=generation, registry_sha256=digest, memberships=memberships
        )
    registry.verify_projection(
        generation=registry.generation,
        registry_sha256=registry.registry_sha256,
        memberships=tuple(reversed(registry.memberships)),
    )


def test_registry_file_cannot_follow_a_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize_empty(_destination())
    path = tmp_path / "root" / ".open-brain" / "historical-authority" / "historical-claims.v1.json"
    # Retain original bytes; only replace the disposable synthetic path.
    path.rename(path.with_suffix(".retained"))
    path.symlink_to(path.with_suffix(".retained"))
    with pytest.raises(RootConfinementError):
        store.read(_destination())


def test_registry_permissions_are_owner_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize_empty(_destination())
    authority = tmp_path / "root" / ".open-brain" / "historical-authority"
    assert authority.stat().st_mode & 0o777 == 0o700
    assert (authority / "historical-claims.v1.json").stat().st_mode & 0o777 == 0o600
