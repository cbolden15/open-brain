"""Additive structured read field, leaving the frozen T03 v1 resource unchanged."""

from __future__ import annotations

from typing import Any
from uuid import UUID

SLACK_RECALL_RESPONSE_EXTENSIONS = {
    "slack_recall": {
        "version": 1,
        "record_read_optional_fields": ["selection"],
        "selection_version": 1,
        "maximum_selection_members": 24,
        "error_codes": ["reapproval_required"],
    }
}


def validate_selection(value: Any) -> None:
    if type(value) is not dict or set(value) != {
        "selection_id",
        "selection_version",
        "member_count",
        "member_ordinal",
        "members",
    }:
        raise ValueError("invalid selection")
    selection_id = value["selection_id"]
    if type(selection_id) is not str or not selection_id.startswith("selection_"):
        raise ValueError("invalid selection")
    identifier = UUID(selection_id.removeprefix("selection_"))
    if identifier.version != 4 or selection_id != "selection_" + str(identifier):
        raise ValueError("invalid selection")
    if (
        type(value["selection_version"]) is not int
        or value["selection_version"] != 1
        or type(value["member_count"]) is not int
        or not 1 <= value["member_count"] <= 24
        or type(value["member_ordinal"]) is not int
        or not 0 <= value["member_ordinal"] < value["member_count"]
        or type(value["members"]) is not list
        or len(value["members"]) != value["member_count"]
    ):
        raise ValueError("invalid selection")
    for ordinal, member in enumerate(value["members"]):
        if (
            type(member) is not dict
            or type(member.get("ordinal")) is not int
            or member["ordinal"] != ordinal
        ):
            raise ValueError("invalid member")
        if member.get("status") == "unavailable" and set(member) == {"ordinal", "status"}:
            continue
        if member.get("status") != "visible" or set(member) != {
            "ordinal",
            "status",
            "record_id",
            "revision_id",
        }:
            raise ValueError("invalid member")
        record = member["record_id"]
        if (
            type(record) is not str
            or not record.startswith("capture_")
            or member["revision_id"] != record
        ):
            raise ValueError("invalid member")
        identifier = UUID(record.removeprefix("capture_"))
        if identifier.version != 4 or record != "capture_" + str(identifier):
            raise ValueError("invalid member")
