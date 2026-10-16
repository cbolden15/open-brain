"""Required canonical owner approval evidence bound to the inherited journal."""

from __future__ import annotations

from collections.abc import Mapping

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.owner_retraction_contracts import validate_approval_records

from .lifecycle_v13 import lifecycle_evidence_value
from .v1 import PortableValidationError, _portable_record

APPROVALS_PATH = "history/retraction/approvals-v1.json"
APPROVALS_CONTRACT = "ob.owner-retraction-evidence.v1"


def retraction_evidence_sidecar(records: object, journal: dict[str, object]) -> bytes:
    """Build the exact required evidence object after full journal binding checks."""
    validated = validate_approval_records(records, journal)
    return canonical({"contract": APPROVALS_CONTRACT, "records": validated})


def validate_retraction_evidence(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    """Validate all evidence before any destination staging or database mutation."""
    try:
        value = _portable_record(files[APPROVALS_PATH], "owner retraction evidence")
        if set(value) != {"contract", "records"} or value["contract"] != APPROVALS_CONTRACT:
            raise ValueError("invalid retraction evidence contract")
        return validate_approval_records(value["records"], lifecycle_evidence_value(files))
    except PortableValidationError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise PortableValidationError("Portable v14 owner retraction evidence invalid") from error
