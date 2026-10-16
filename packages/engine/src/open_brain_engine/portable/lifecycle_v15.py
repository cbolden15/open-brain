"""Explicit shared journal2 layout and mixed owner/consumer capture validation."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_lifecycle_contracts import LifecycleRequest
from open_brain_engine.engine.consumer_journal import (
    consumer_items,
    restore_lifecycle_journal_v2_value,
)

from .lifecycle_v13 import (
    LIFECYCLE_PATH as PREDECESSOR_PATH,
)
from .lifecycle_v13 import (
    LIFECYCLE_PAYLOAD_PREFIX,
    _require_capture,
)
from .lifecycle_v13 import (
    lifecycle_evidence_sidecars as predecessor_sidecars,
)
from .lifecycle_v13 import (
    lifecycle_evidence_value as predecessor_value,
)
from .v1 import PortableValidationError
from .v5 import ISSUER_MIGRATION_PATH
from .v8_capture_metadata import validate_capture_metadata

LIFECYCLE_PATH = "history/capture-lifecycle/journal-v2.json"


def is_lifecycle_path(path: str) -> bool:
    from open_brain_engine.engine.capture_lifecycle_contracts import validate_digest

    if path == LIFECYCLE_PATH:
        return True
    if not path.startswith(LIFECYCLE_PAYLOAD_PREFIX):
        return False
    try:
        validate_digest(path.removeprefix(LIFECYCLE_PAYLOAD_PREFIX))
        return True
    except ValueError:
        return False


def lifecycle_evidence_sidecars(evidence: dict[str, object]) -> dict[str, bytes]:
    files = predecessor_sidecars(evidence)
    files[LIFECYCLE_PATH] = files.pop(PREDECESSOR_PATH)
    return files


def lifecycle_evidence_value(files: Mapping[str, bytes]) -> dict[str, Any]:
    if PREDECESSOR_PATH in files:
        raise PortableValidationError("Portable15 contains predecessor lifecycle layout")
    translated = {path: raw for path, raw in files.items() if path != LIFECYCLE_PATH}
    translated[PREDECESSOR_PATH] = files[LIFECYCLE_PATH]
    return predecessor_value(translated)


def validate_lifecycle_evidence(files: Mapping[str, bytes]) -> dict[str, Any]:
    from open_brain_engine.engine.capture_lifecycle import LifecycleSegment
    from open_brain_engine.engine.consumer_segments import ConsumerSegmentEnvelope

    try:
        evidence = lifecycle_evidence_value(files)
        issuer = json.loads(files[ISSUER_MIGRATION_PATH])
        with closing(sqlite3.connect(":memory:")) as staging:
            staging.execute("BEGIN IMMEDIATE")
            journal = restore_lifecycle_journal_v2_value(
                staging,
                evidence,
                brain_id=issuer["brain_id"],
                issuer_epoch=issuer["current_issuer_epoch"],
            )
            state = journal._state()
        captures = {row["capture_id"]: row for row in validate_capture_metadata(files)}
        payloads = {
            key: base64.b64decode(raw, validate=True) for key, raw in evidence["payloads"].items()
        }
        requests = {
            raw["operation_id"]: LifecycleRequest.from_bytes(canonical(raw))
            for raw in evidence["requests"]
        }
        deliveries: set[str] = set()
        for (operation, group_key), group in state.staged.items():
            typed = consumer_items(requests[operation]) is not None
            for segment in group.segments:
                row = captures.get(segment.capture_id)
                if (operation, group_key, segment.capture_id) not in state.custodied:
                    if row is not None:
                        raise ValueError("capture lacks custody")
                    continue
                retained = (
                    ConsumerSegmentEnvelope.from_bytes(payloads[segment.request_sha256])
                    if typed
                    else LifecycleSegment.from_bytes(payloads[segment.request_sha256])
                )
                submission = retained.envelope.submission
                if (
                    retained.binding != segment
                    or submission.tenant_id != issuer["tenant_id"]
                    or (
                        retained.body != payloads[segment.body_sha256]
                        or submission.delivery_id in deliveries
                    )
                ):
                    raise ValueError("capture envelope mismatch")
                deliveries.add(submission.delivery_id)
                if row is None:
                    if (operation, group_key) in state.completed:
                        raise ValueError("activated capture missing")
                else:
                    _require_capture(row, retained.envelope, identities=asdict(retained.identities))
        if any(capture not in captures for capture in state.retracted_captures):
            raise ValueError("retracted capture missing")
        return evidence
    except PortableValidationError:
        raise
    except (ValueError, TypeError, KeyError, sqlite3.Error, OverflowError, RecursionError) as error:
        raise PortableValidationError("Portable15 lifecycle evidence invalid") from error
