"""Checked calendar-day revisit operations for retained saved page captures."""

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from uuid import UUID

from open_brain_engine.engine.saved_item_identity import SavedItemSource

from .browser_annotations_v21 import ANNOTATION_KEY, BROWSER_ANNOTATIONS_PATH
from .lifecycle_v15 import lifecycle_evidence_value
from .saved_item_admission_v20 import SAVED_ITEM_ADMISSION_PATH
from .saved_item_sources_v19 import validate_saved_item_sources
from .v1 import PortableValidationError, _portable_record
from .v4 import SOURCE_METADATA_PATH
from .v5 import ISSUER_MIGRATION_PATH
from .v8_capture_metadata import validate_capture_metadata

BROWSER_REVISITS_PATH = "history/browser-revisits/revisits-v1.json"
BROWSER_REVISITS_CONTRACT = "open-brain-browser-revisits.v1"
REVISIT_COLUMNS = (
    "sequence",
    *ANNOTATION_KEY,
    "request_sha256",
    "delivery_id",
    "item_key",
    "capture_id",
    "action",
    "local_date",
    "zone",
    "state",
)
_REQUEST_FIELDS = ("action", "item_key", "capture_id", "local_date", "zone")


def _validate_revisit_calendar(row: dict[str, object], *, withdrawn: bool) -> None:
    from open_brain_engine.engine.browser_revisits import validate_calendar

    if row["action"] not in ("set", "dismiss", "clear"):
        raise PortableValidationError("invalid browser revisit action")
    if row["state"] != ("withdrawn" if withdrawn else "accepted"):
        raise PortableValidationError("invalid browser revisit disposition")
    day, zone = row["local_date"], row["zone"]
    if not withdrawn and row["action"] == "set":
        if not isinstance(day, str) or not isinstance(zone, str):
            raise PortableValidationError("invalid browser revisit calendar")
        validate_calendar(day, zone, require_installed=False)
    elif day is not None or zone is not None:
        raise PortableValidationError("unexpected browser revisit calendar")
    if not withdrawn:
        request = {name: row[name] for name in _REQUEST_FIELDS}
        digest = sha256(
            json.dumps(request, separators=(",", ":"), sort_keys=True).encode("ascii")
        ).hexdigest()
        if row["request_sha256"] != digest:
            raise PortableValidationError("browser revisit request digest mismatch")


def validate_browser_revisits(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    from open_brain_engine.engine.browser_filing import _id

    try:
        data = _portable_record(files.get(BROWSER_REVISITS_PATH, b""), "browser revisits")
        rows = data.get("revisits")
        if (
            set(data) != {"contract", "revisits"}
            or data["contract"] != BROWSER_REVISITS_CONTRACT
            or not isinstance(rows, list)
        ):
            raise PortableValidationError("invalid browser revisit family")
        if not rows:
            return []
        captures = {row["capture_id"]: row for row in validate_capture_metadata(files)}
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        sources = {row["source_id"]: row for row in metadata["sources"]}
        revisions = {row["capture_id"]: row for row in metadata["revisions"]}
        registered = set(validate_saved_item_sources(files))
        brain_id = json.loads(files[ISSUER_MIGRATION_PATH])["brain_id"]
        retracted = {
            event["payload"]["capture_id"]
            for event in lifecycle_evidence_value(files)["events"]
            if event["kind"] == "capture_retracted"
        }
        old_keys = {
            tuple(row[name] for name in ANNOTATION_KEY)
            for path, family in (
                (SAVED_ITEM_ADMISSION_PATH, "browser_operations"),
                (BROWSER_ANNOTATIONS_PATH, "annotations"),
            )
            for row in json.loads(files[path])[family]
        }
        routes = {
            json.loads(raw)["route_id"]
            for path, raw in files.items()
            if path.startswith("history/routes/")
        }

        previous = 0
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != set(REVISIT_COLUMNS)
                or any(
                    not isinstance(row[name], str)
                    for name in REVISIT_COLUMNS
                    if name not in {"sequence", "local_date", "zone"}
                )
                or type(row["sequence"]) is not int
                or not previous < row["sequence"] <= 2**63 - 1
            ):
                raise PortableValidationError("invalid browser revisit fields or sequence")
            key = tuple(row[name] for name in ANNOTATION_KEY)
            operation_id = UUID(row["operation_id"])
            principal = UUID(row["workflow_principal"])
            source = SavedItemSource(
                row["connector_name"], row["connection_id"], row["resource_id"]
            )
            capture_id = row["capture_id"]
            capture = captures.get(capture_id)
            revision = revisions.get(capture_id)
            if (
                row["brain_id"] != brain_id
                or key in old_keys
                or _id("route", key) in routes
                or str(operation_id) != row["operation_id"]
                or operation_id.version != 4
                or str(principal) != row["workflow_principal"]
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
                raise PortableValidationError("invalid browser revisit parent or operation binding")
            _validate_revisit_calendar(
                row,
                withdrawn=capture_id in retracted
                or sources[revision["source_id"]]["lifecycle"] != "active",
            )
            previous = row["sequence"]
            old_keys.add(key)
        return rows
    except PortableValidationError:
        raise
    except ValueError, KeyError, TypeError, RecursionError:
        raise PortableValidationError("invalid browser revisit evidence") from None
