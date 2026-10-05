"""Complete bounded recovery bytes, not authentication or semantic permission.

A digest label alone does not retain a baseline. Validate its actual Portable
inventory and destination alongside the full journal and dependency bytes before
an independent protection adapter consumes them. This value grants no receipt,
body release, replay authority or claim that a backend retained anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.v5 import ISSUER_MIGRATION_PATH, _manifest
from open_brain_engine.portable.v8 import (
    PORTABLE_V8_SCHEMA_CATALOG_DIGEST,
    validate_portable_file_set_v8,
)
from open_brain_engine.portable.v9 import (
    PORTABLE_V9_SCHEMA_CATALOG_DIGEST,
    validate_portable_file_set_v9,
)

from .recovery_journal import (
    MAX_RECOVERY_DEPENDENCIES,
    RecoveryHead,
    RecoveryRecord,
    verify_recovery_chain,
)


@dataclass(frozen=True, slots=True)
class RecoveryClosureBounds:
    """Explicit deployment admission bounds; no inferred production defaults."""

    max_baseline_files: int
    max_baseline_bytes: int
    max_records: int
    max_journal_bytes: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            self.max_baseline_files, self.max_baseline_bytes,
            self.max_records, self.max_journal_bytes,
        )):
            raise ValueError("invalid recovery closure bounds")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryClosure:
    """Immutable complete bytes bound to a caller-authenticated exact head.

    Construction checks exact Portable8/9 semantics and hash/dependency closure only.
    Every journal operation still needs its own typed semantic replay validator;
    a recognized record kind is not proof that its payload can be recovered.
    Authenticate the latest head and baseline independently, never from this
    locally self-consistent value. Limits must come from actual admission policy.
    """

    bounds: RecoveryClosureBounds
    baseline_files: tuple[tuple[str, bytes], ...]
    records: tuple[RecoveryRecord, ...]
    expected_head: RecoveryHead
    dependency_payloads: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        if type(self.bounds) is not RecoveryClosureBounds or (
            type(self.expected_head) is not RecoveryHead
            or type(self.baseline_files) is not tuple
            or type(self.records) is not tuple
            or type(self.dependency_payloads) is not tuple
        ):
            raise ValueError("invalid recovery closure components")
        if not 0 < len(self.baseline_files) <= self.bounds.max_baseline_files or (
            len(self.records) > self.bounds.max_records
            or len(self.dependency_payloads) > MAX_RECOVERY_DEPENDENCIES
        ):
            raise ValueError("recovery closure exceeds item bounds")
        size = 0
        previous = ""
        for entry in self.baseline_files:
            if type(entry) is not tuple or len(entry) != 2 or (
                type(entry[0]) is not str or type(entry[1]) is not bytes
            ):
                raise ValueError("invalid recovery baseline entry")
            path, raw = entry
            if not path or len(path) > 1024 or path <= previous:
                raise ValueError("recovery baseline paths must be bounded sorted and unique")
            previous = path
            size += len(raw) + len(path.encode("utf-8"))
            if size > self.bounds.max_baseline_bytes:
                raise ValueError("recovery baseline exceeds byte bounds")
        size = 0
        for record in self.records:
            if type(record) is not RecoveryRecord:
                raise ValueError("invalid recovery closure record")
            size += len(record.to_bytes())
            if size > self.bounds.max_journal_bytes:
                raise ValueError("recovery journal exceeds byte bounds")
        for payload in self.dependency_payloads:
            if type(payload) is not bytes:
                raise ValueError("invalid recovery dependency bytes")
            size += len(payload)
            if size > self.bounds.max_journal_bytes:
                raise ValueError("recovery journal exceeds byte bounds")
        self._validate_baseline()
        verify_recovery_chain(
            self.expected_head.baseline, self.records, expected_head=self.expected_head,
            dependency_payloads=self.dependency_payloads,
        )

    def _validate_baseline(self) -> None:
        files = dict(self.baseline_files)
        raw = files.pop("portable-manifest.json", None)
        baseline = self.expected_head.baseline
        if raw is None or sha256(raw).hexdigest() != baseline.artifact_sha256:
            raise ValueError("recovery baseline manifest commitment mismatch")
        try:
            manifest = json.loads(raw)
            if type(manifest) is not dict or canonical(manifest) != raw:
                raise ValueError("invalid recovery baseline manifest")
            version = manifest.get("schema_version")
            if type(version) is not int or version not in {8, 9}:
                raise ValueError("unsupported recovery baseline version")
            catalog, validator = {
                8: (PORTABLE_V8_SCHEMA_CATALOG_DIGEST, validate_portable_file_set_v8),
                9: (PORTABLE_V9_SCHEMA_CATALOG_DIGEST, validate_portable_file_set_v9),
            }[version]
            inventory = _manifest(manifest, version=version, catalog=catalog)
            if inventory != {path: sha256(body).hexdigest() for path, body in files.items()}:
                raise ValueError("recovery baseline inventory mismatch")
            validator(files, tenant_id=cast(str, manifest["tenant_id"]))
            identity = json.loads(files[ISSUER_MIGRATION_PATH])
            if identity["brain_id"] != baseline.brain_id or (
                identity["current_issuer_epoch"] != baseline.issuer_epoch
            ):
                raise ValueError("recovery baseline destination mismatch")
        except KeyError, TypeError, ValueError, RecursionError:
            raise ValueError("invalid recovery baseline evidence") from None

    @property
    def closure_sha256(self) -> str:
        """Commit actual validated inventories, order and head, not durability."""
        head = self.expected_head
        return sha256(b"open-brain-recovery-closure.v1\0" + canonical({
            "baseline": head.baseline.value(),
            "head": {"sequence": head.sequence, "record_sha256": head.record_sha256},
            "files": [[path, sha256(raw).hexdigest()] for path, raw in self.baseline_files],
            "records": [record.record_sha256 for record in self.records],
            "dependencies": sorted(sha256(raw).hexdigest() for raw in self.dependency_payloads),
        })).hexdigest()
