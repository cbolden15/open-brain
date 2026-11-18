"""Canonical browser annotation evidence with checked parent and replay bindings."""

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from uuid import UUID

from open_brain_engine.engine.saved_item_identity import SavedItemSource

from .lifecycle_v15 import lifecycle_evidence_value
from .saved_item_admission_v20 import SAVED_ITEM_ADMISSION_PATH
from .saved_item_sources_v19 import validate_saved_item_sources
from .v1 import PortableValidationError, _portable_record
from .v4 import SOURCE_METADATA_PATH
from .v5 import ISSUER_MIGRATION_PATH
from .v8_capture_metadata import validate_capture_metadata

BROWSER_ANNOTATIONS_PATH = "history/browser-annotations/annotations-v1.json"
BROWSER_ANNOTATIONS_CONTRACT = "open-brain-browser-annotations.v1"
ANNOTATION_KEY = (
    "brain_id",
    "workflow_principal",
    "connector_name",
    "connection_id",
    "resource_id",
    "operation_id",
)
ANNOTATION_CONTENT = ("quote", "prefix", "suffix", "note")
ANNOTATION_COLUMNS = (
    *ANNOTATION_KEY,
    "request_sha256",
    "delivery_id",
    "item_key",
    "capture_id",
    "annotation_id",
    "state",
    *ANNOTATION_CONTENT,
)


def _uuid(value: str, *, version: int | None = None) -> bool:
    identifier = UUID(value)
    return str(identifier) == value and (version is None or identifier.version == version)


def _validate_content(
    row: dict[str, str | None], *, withdrawn: bool, source_reference: str
) -> None:
    if withdrawn:
        if row["state"] != "withdrawn" or any(row[name] is not None for name in ANNOTATION_CONTENT):
            raise PortableValidationError("withdrawn browser annotation contains retained text")
        return
    from open_brain_engine.engine.browser_annotations import Annotation

    quote, prefix, suffix, note = (row[name] for name in ANNOTATION_CONTENT)
    annotation_id = row["annotation_id"]
    if (
        row["state"] != "accepted"
        or annotation_id is None
        or quote is None
        or prefix is None
        or suffix is None
    ):
        raise PortableValidationError("invalid browser annotation content")
    Annotation(annotation_id, quote, prefix, suffix, note).validate(source_reference)
    digest = annotation_request_digest(row)
    if digest != row["request_sha256"]:
        raise PortableValidationError("browser annotation request digest mismatch")


def annotation_request_digest(row: Mapping[str, str | None]) -> str:
    request = {
        name: row[name] for name in ("item_key", "capture_id", "annotation_id", *ANNOTATION_CONTENT)
    }
    return sha256(
        json.dumps(request, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    ).hexdigest()


def validate_browser_annotations(files: Mapping[str, bytes]) -> list[dict[str, str | None]]:
    try:
        data = _portable_record(files.get(BROWSER_ANNOTATIONS_PATH, b""), "browser annotations")
        rows = data.get("annotations")
        if (
            set(data) != {"contract", "annotations"}
            or data["contract"] != BROWSER_ANNOTATIONS_CONTRACT
            or not isinstance(rows, list)
        ):
            raise PortableValidationError("invalid browser annotation family")
        if not rows:
            return []
        captures = {capture["capture_id"]: capture for capture in validate_capture_metadata(files)}
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        sources = {source["source_id"]: source for source in metadata["sources"]}
        revisions = {revision["capture_id"]: revision for revision in metadata["revisions"]}
        registered = set(validate_saved_item_sources(files))
        brain_id = json.loads(files[ISSUER_MIGRATION_PATH])["brain_id"]
        retracted = {
            event["payload"]["capture_id"]
            for event in lifecycle_evidence_value(files)["events"]
            if event["kind"] == "capture_retracted"
        }
        prior_save_operations = {
            tuple(row[name] for name in ANNOTATION_KEY)
            for row in json.loads(files[SAVED_ITEM_ADMISSION_PATH])["browser_operations"]
        }
        previous: tuple[str, ...] | None = None
        annotation_ids: set[str] = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != set(ANNOTATION_COLUMNS)
                or any(not isinstance(row[name], str) for name in ANNOTATION_COLUMNS[:-4])
                or any(
                    row[name] is not None and not isinstance(row[name], str)
                    for name in ANNOTATION_CONTENT
                )
            ):
                raise PortableValidationError("invalid browser annotation fields")
            key = tuple(row[name] for name in ANNOTATION_KEY)
            capture_id = row["capture_id"]
            capture = captures.get(capture_id)
            revision = revisions.get(capture_id)
            source = SavedItemSource(
                row["connector_name"], row["connection_id"], row["resource_id"]
            )
            if (
                row["brain_id"] != brain_id
                or (previous is not None and key <= previous)
                or key in prior_save_operations
                or row["operation_id"] == row["annotation_id"]
                or row["annotation_id"] in annotation_ids
                or not _uuid(row["workflow_principal"])
                or not _uuid(row["operation_id"], version=4)
                or not _uuid(row["annotation_id"], version=4)
                or not re.fullmatch(r"[0-9a-f]{64}", row["request_sha256"])
                or source not in registered
                or row["delivery_id"] != source.delivery_id(row["item_key"])
                or capture is None
                or capture["stage"] != 3
                or capture["delivery_id"] != row["delivery_id"]
                or capture["submission_path"] != "public_job"
                or capture["source_origin"] != "third_party"
                or revision is None
            ):
                raise PortableValidationError(
                    "invalid browser annotation parent or operation binding"
                )
            _validate_content(
                row,
                source_reference=capture["source_reference"],
                withdrawn=capture_id in retracted
                or sources[revision["source_id"]]["lifecycle"] != "active",
            )
            previous = key
            annotation_ids.add(row["annotation_id"])
        return rows
    except PortableValidationError:
        raise
    except ValueError, KeyError, TypeError, RecursionError:
        raise PortableValidationError("invalid browser annotation evidence") from None
