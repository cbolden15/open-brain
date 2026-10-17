"""Restore Portable16 history while leaving destination local embedding disabled."""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from open_brain_engine.portable.embedding_policy_v16 import validate_embedding_policy_evidence
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v16_catalog import PORTABLE_V16_SCHEMA_CATALOG_DIGEST

from .contracts import LocalEngineContext
from .embedding_policy import install_restored_history, read_policy
from .portable_v15_restore import V15RestoreBundle


class V16RestoreBundle(V15RestoreBundle):
    portable_version: ClassVar[int] = 16
    catalog_digest: ClassVar[str] = PORTABLE_V16_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        # Validate before any inherited destination write; never reuse provider-consent restore.
        rows = validate_embedding_policy_evidence(self.snapshot.files)
        super().install(connection, profile=profile)
        install_restored_history(connection, rows)
        policy = read_policy(connection)
        if policy.enabled or policy.component_id is not None:
            raise PortableValidationError("restored embedding policy must be disabled")
        self.checkpoint("embedding_policy_restored")
