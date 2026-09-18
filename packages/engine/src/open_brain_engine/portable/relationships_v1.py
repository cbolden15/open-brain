"""Portable relationship evidence: explicit edges and complete append-only decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from .v1 import PortableValidationError, _identifier, _portable_record, _timestamp

RELATIONSHIP_METADATA_PATH = "history/relationships/decisions-v1.json"


def validate_relationship_metadata(payload: bytes, sources: Mapping[str, Any]) -> None:
    try:
        value = cast(dict[str, Any], _portable_record(payload, "relationship evidence"))
        if (
            set(value) != {"schema_version", "relationships", "decisions"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError
        if not isinstance(value["relationships"], list) or not isinstance(value["decisions"], list):
            raise ValueError
        revisions = {row["capture_id"]: row["source_id"] for row in sources["revisions"]}
        canonical = {(row["page_id"], row["revision_id"]) for row in sources["canonical_members"]}
        edges: dict[str, dict[str, Any]] = {}
        unique = set()

        def endpoint(value: object) -> tuple[str, str, str]:
            if not isinstance(value, dict) or set(value) != {"record_id", "revision_id"}:
                raise ValueError
            record, revision = value["record_id"], value["revision_id"]
            if not isinstance(record, str) or not isinstance(revision, str):
                raise ValueError
            if record.startswith("capture_"):
                _identifier(record, "capture", "endpoint")
                if record != revision or record not in revisions:
                    raise ValueError
                logical = revisions[record]
            else:
                _identifier(record, "page", "endpoint")
                if (record, revision) not in canonical:
                    raise ValueError
                logical = record
            return record, revision, logical

        previous_id = ""
        for row in value["relationships"]:
            if set(row) != {"relationship_id", "left", "right", "kind", "status", "version"}:
                raise ValueError
            identity = row["relationship_id"]
            _identifier(identity, "relationship", "relationship")
            if identity <= previous_id:
                raise ValueError
            previous_id = identity
            left, right = endpoint(row["left"]), endpoint(row["right"])
            if left[2] == right[2] or row["kind"] not in {
                "duplicate_of",
                "supersedes",
                "contradicts",
            }:
                raise ValueError
            if row["kind"] in {"duplicate_of", "contradicts"} and right[:2] < left[:2]:
                raise ValueError
            if (
                type(row["version"]) is not int
                or not 1 <= row["version"] <= 9007199254740991
                or row["status"] not in {"accepted", "rejected", "removed"}
            ):
                raise ValueError
            key = (left[:2], right[:2], row["kind"])
            if key in unique:
                raise ValueError
            unique.add(key)
            edges[identity] = row
        states: dict[str, tuple[int, str]] = {}
        decision_ids = set()
        for sequence, row in enumerate(value["decisions"], 1):
            if set(row) != {
                "decision_id",
                "relationship_id",
                "sequence",
                "decision",
                "version",
                "recorded_at",
                "actor_id",
            }:
                raise ValueError
            _identifier(row["decision_id"], "decision", "decision")
            _identifier(row["actor_id"], "actor", "decision actor")
            _timestamp(row["recorded_at"], "decision time")
            identity = row["relationship_id"]
            prior = states.get(identity, (0, ""))
            if (
                row["decision_id"] in decision_ids
                or identity not in edges
                or type(row["sequence"]) is not int
                or row["sequence"] != sequence
                or type(row["version"]) is not int
                or row["version"] != prior[0] + 1
                or row["decision"] not in {"accept", "reject", "remove"}
            ):
                raise ValueError
            decision_ids.add(row["decision_id"])
            states[identity] = (row["version"], row["decision"])
            if row["decision"] == "accept" and edges[identity]["kind"] == "supersedes":
                edge = edges[identity]
                target = endpoint(edge["left"])[:2]
                queue = [endpoint(edge["right"])[:2]]
                seen = set()
                while queue:
                    node = queue.pop()
                    if node == target:
                        raise ValueError
                    if node in seen:
                        continue
                    seen.add(node)
                    for candidate_id, (_, decision) in states.items():
                        candidate = edges[candidate_id]
                        if (
                            decision == "accept"
                            and candidate["kind"] == "supersedes"
                            and endpoint(candidate["left"])[:2] == node
                        ):
                            queue.append(endpoint(candidate["right"])[:2])
        if set(states) != set(edges):
            raise ValueError
        labels = {"accept": "accepted", "reject": "rejected", "remove": "removed"}
        for identity, (version, decision) in states.items():
            if (
                edges[identity]["version"] != version
                or edges[identity]["status"] != labels[decision]
            ):
                raise ValueError
    except TypeError, KeyError, ValueError:
        raise PortableValidationError("relationship evidence invalid") from None
