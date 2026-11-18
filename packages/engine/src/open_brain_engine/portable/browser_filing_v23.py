"""Verified opaque saved-item source witnesses for retained browser filing routes."""

import json
from collections.abc import Mapping
from uuid import UUID

from open_brain_engine.engine.saved_item_identity import SavedItemSource

from .browser_annotations_v21 import ANNOTATION_KEY, BROWSER_ANNOTATIONS_PATH
from .browser_revisits_v22 import BROWSER_REVISITS_PATH
from .lifecycle_v15 import lifecycle_evidence_value
from .saved_item_admission_v20 import SAVED_ITEM_ADMISSION_PATH
from .saved_item_sources_v19 import validate_saved_item_sources
from .v1 import PortableValidationError, _portable_record
from .v4 import SOURCE_METADATA_PATH
from .v5 import ISSUER_MIGRATION_PATH
from .v8_capture_metadata import validate_capture_metadata

BROWSER_FILING_PATH = "history/browser-filing/witnesses-v1.json"
BROWSER_FILING_CONTRACT = "open-brain-browser-filing-witnesses.v1"
FILING_WITNESS_COLUMNS = (
    "route_id",
    "workflow_principal",
    "connector_name",
    "connection_id",
    "resource_id",
    "item_key",
    "delivery_id",
)


def validate_filing_witnesses(files: Mapping[str, bytes]) -> list[dict[str, str]]:
    from open_brain_engine.engine.browser_filing import _id, recover_filing_operation_for_scope

    try:
        payload = _portable_record(files.get(BROWSER_FILING_PATH, b""), "browser filing witnesses")
        rows = payload.get("witnesses")
        if (
            set(payload) != {"contract", "witnesses"}
            or payload["contract"] != BROWSER_FILING_CONTRACT
            or not isinstance(rows, list)
        ):
            raise PortableValidationError("invalid browser filing witness family")
        if not rows:
            return []
        registered = set(validate_saved_item_sources(files))
        brain_id = json.loads(files[ISSUER_MIGRATION_PATH])["brain_id"]
        captures = {row["capture_id"]: row for row in validate_capture_metadata(files)}
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        sources = {row["source_id"]: row for row in metadata["sources"]}
        revisions = {row["capture_id"]: row for row in metadata["revisions"]}
        retracted = {
            event["payload"]["capture_id"]
            for event in lifecycle_evidence_value(files)["events"]
            if event["kind"] == "capture_retracted"
        }
        routes = {
            route["route_id"]: route
            for path, raw in files.items()
            if path.startswith("history/routes/")
            for route in (json.loads(raw),)
        }
        other_operation_routes = {
            _id("route", tuple(row[name] for name in ANNOTATION_KEY))
            for path, family in (
                (SAVED_ITEM_ADMISSION_PATH, "browser_operations"),
                (BROWSER_ANNOTATIONS_PATH, "annotations"),
                (BROWSER_REVISITS_PATH, "revisits"),
            )
            for row in json.loads(files[path])[family]
        }
        previous = ""
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != set(FILING_WITNESS_COLUMNS)
                or not all(isinstance(value, str) for value in row.values())
                or row["route_id"] <= previous
            ):
                raise PortableValidationError("invalid or duplicate browser filing witness")
            route_id, principal, connector, connection, resource, item_key, delivery = (
                row[name] for name in FILING_WITNESS_COLUMNS
            )
            source = SavedItemSource(connector, connection, resource)
            route = routes[route_id]
            capture_id = route["capture_id"]
            capture = captures[capture_id]
            revision = revisions[capture_id]
            if (
                route_id in other_operation_routes
                or str(UUID(principal)) != principal
                or source not in registered
                or source.delivery_id(item_key) != delivery
                or capture["delivery_id"] != delivery
                or capture["stage"] != 3
                or capture["submission_path"] != "public_job"
                or capture["source_origin"] != "third_party"
                or capture_id in retracted
                or sources[revision["source_id"]]["lifecycle"] != "active"
                or recover_filing_operation_for_scope(
                    (brain_id, principal, *source.values()),
                    route_id,
                    route["receipt"]["receipt_id"],
                    delivery,
                    capture_id,
                    route["space_id"],
                ) is None
            ):
                raise PortableValidationError("invalid browser filing source or receipt binding")
            previous = route_id
        return rows
    except PortableValidationError:
        raise
    except ValueError, KeyError, TypeError, RecursionError:
        raise PortableValidationError("invalid browser filing witness evidence") from None
