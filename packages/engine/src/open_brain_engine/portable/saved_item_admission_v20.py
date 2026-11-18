"""Validate canonical saved-item admission and browser operation evidence."""

import json
import re
from collections.abc import Mapping
from uuid import UUID

from open_brain_engine.engine.saved_item_identity import SavedItemSource

from .saved_item_sources_v19 import validate_saved_item_sources
from .v1 import PortableValidationError, _portable_record
from .v5 import ISSUER_MIGRATION_PATH
from .v8_capture_metadata import validate_capture_metadata
from .v8_custody import validate_capture_custody

SAVED_ITEM_ADMISSION_PATH = "history/saved-item-admission/admissions-v1.json"
SAVED_ITEM_ADMISSION_CONTRACT = "open-brain-saved-item-admission.v1"
BROWSER_KEY = (
    "brain_id",
    "workflow_principal",
    "connector_name",
    "connection_id",
    "resource_id",
    "operation_id",
)
TABLE_COLUMNS = {
    "browser_operations": (
        *BROWSER_KEY,
        "operation_kind",
        "request_sha256",
        "state",
        "delivery_id",
        "item_key",
    ),
    "saved_item_admissions": ("delivery_id", "producer", "request_sha256"),
}
TABLE_KEYS = {"browser_operations": BROWSER_KEY, "saved_item_admissions": ("delivery_id",)}


def _ordered_rows(
    value: object,
    *,
    table: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PortableValidationError("invalid saved-item admission table")
    rows: list[dict[str, str]] = []
    previous: tuple[str, ...] | None = None
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row) != set(TABLE_COLUMNS[table])
            or any(not isinstance(field, str) for field in row.values())
        ):
            raise PortableValidationError("invalid saved-item admission fields")
        key = tuple(row[name] for name in TABLE_KEYS[table])
        if previous is not None and key <= previous:
            raise PortableValidationError("duplicate or unordered saved-item admission key")
        if not re.fullmatch(
            r"connector\.saved_item\.[0-9a-f]{64}", row["delivery_id"]
        ) or not re.fullmatch(r"[0-9a-f]{64}", row["request_sha256"]):
            raise PortableValidationError("invalid saved-item delivery or request digest")
        previous = key
        rows.append(row)
    return rows


def validate_saved_item_admission(files: Mapping[str, bytes]) -> dict[str, list[dict[str, str]]]:
    try:
        data = _portable_record(files.get(SAVED_ITEM_ADMISSION_PATH, b""), "saved-item admission")
    except RecursionError:
        raise PortableValidationError("invalid saved-item admission evidence") from None
    if (
        set(data) != {"contract", *TABLE_COLUMNS}
        or data["contract"] != SAVED_ITEM_ADMISSION_CONTRACT
    ):
        raise PortableValidationError("invalid saved-item admission evidence")
    rows = {table: _ordered_rows(data[table], table=table) for table in TABLE_COLUMNS}
    if not any(rows.values()):
        return rows
    sources = set(validate_saved_item_sources(files))
    brain_id = json.loads(files[ISSUER_MIGRATION_PATH])["brain_id"]
    custody = validate_capture_custody(files)
    capture_rows = validate_capture_metadata(files)
    captures = {row["delivery_id"]: row["request_sha256"] for row in capture_rows}
    accepted = {row["delivery_id"] for row in capture_rows if row["stage"] == 3}
    journal = {
        row["delivery_id"]: row["request_sha256"] for row in custody["capture_ingestion_items"]
    }
    tombstones = {
        row["delivery_id"]: row["request_sha256"] for row in custody["capture_ingestion_tombstones"]
    }
    refused = {
        row["delivery_id"]
        for row in custody["capture_ingestion_events"]
        if row["event_kind"] in {"quarantined", "discarded"}
    }
    admissions = {row["delivery_id"]: row for row in rows["saved_item_admissions"]}
    retained = journal | captures | tombstones
    for delivery, row in admissions.items():
        if row["producer"] not in {"folder", "browser"} or row["request_sha256"] != retained.get(
            delivery
        ):
            raise PortableValidationError("saved-item admission has no matching retained evidence")

    operation_deliveries: set[str] = set()
    for row in rows["browser_operations"]:
        delivery = row["delivery_id"]
        try:
            source = SavedItemSource(
                row["connector_name"], row["connection_id"], row["resource_id"]
            )
            source_delivery = source.delivery_id(row["item_key"])
            identifiers_canonical = all(
                str(UUID(row[field])) == row[field]
                for field in ("workflow_principal", "operation_id")
            )
        except ValueError:
            raise PortableValidationError("invalid browser operation identity") from None
        if (
            row["brain_id"] != brain_id
            or not identifiers_canonical
            or source not in sources
            or source_delivery != delivery
            or row["operation_kind"] != "page.save"
            or delivery not in admissions
            or not (
                (row["state"] == "pending" and delivery in journal)
                or (row["state"] == "accepted" and delivery in accepted)
                or (row["state"] == "refused" and (delivery in tombstones or delivery in refused))
            )
        ):
            raise PortableValidationError("invalid browser operation evidence binding")
        operation_deliveries.add(delivery)
    if any(
        row["producer"] == "browser" and delivery not in operation_deliveries
        for delivery, row in admissions.items()
    ):
        raise PortableValidationError("browser admission is missing an operation binding")
    return rows
