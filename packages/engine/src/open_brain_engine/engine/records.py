"""Full immutable evidence projection for versioned retrieval, within one read snapshot."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.portable.v4 import canonical_revision_id
from open_brain_engine.storage.filesystem import StorageError, read_confined
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from .contracts import FilePayload, LocalEngineContext
from .materializer import _payload_search_text
from .normalization import _privacy, _role_claim
from .search_projection import public_search_text, public_source_origin, source_search_title
from .t03_contracts import EffectiveAuthority, T03Error, validate_wire


@dataclass(frozen=True, slots=True)
class ProjectedRecord:
    summary: dict[str, Any]
    text: str
    indexed_text: str | None = None


class RecordProjector:
    def __init__(
        self,
        profile: LocalEngineContext,
        connection: sqlite3.Connection,
        authority: EffectiveAuthority,
    ) -> None:
        self.profile = profile
        self.connection = connection
        self.authority = authority

    def _require_tier(self, value: object) -> None:
        try:
            if not isinstance(value, str):
                raise ValueError
            tier = PrivacyTier(value)
        except (TypeError, ValueError):
            tier = PrivacyTier.UNKNOWN
        if not self.authority.permits_read_tier(tier):
            raise T03Error("not_found")

    def _require_effective_privacy(self, raw: object) -> None:
        try:
            value = json.loads(raw) if isinstance(raw, str) else None
            if not isinstance(value, dict):
                raise ValueError
            tier = value["tier"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            tier = PrivacyTier.UNKNOWN
        self._require_tier(tier)

    def _require_source_revision_privacy(self, capture_id: str) -> None:
        row = self.connection.execute(
            "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise T03Error("not_found")
        self._require_effective_privacy(row["effective_privacy_json"])

    def _require_canonical_revision_privacy(self, publication_id: str) -> None:
        row = self.connection.execute(
            "SELECT effective_privacy_json FROM canonical_revision_privacy WHERE revision_id=?",
            (canonical_revision_id(publication_id),),
        ).fetchone()
        if row is None:
            raise T03Error("not_found")
        self._require_effective_privacy(row["effective_privacy_json"])

    def _capture(self, capture_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
        revision = self.connection.execute(
            "SELECT * FROM source_revisions WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        if revision is None:
            raise T03Error("not_found")
        raw = read_confined(
            root=self.profile.root,
            relative=revision["source_path"],
            expected_root_identity=self.profile.root_identity,
        )
        if (
            raw is None
            or raw != revision["source_bytes"]
            or sha256(raw).hexdigest() != revision["source_sha256"]
        ):
            raise T03Error("not_found")
        validate_portable_write(revision["source_path"], raw, self.profile.tenant_id)
        record = json.loads(raw)
        if record["capture_id"] != capture_id:
            raise T03Error("not_found")
        return revision, record

    def source(
        self, anchor: str, *, expected: str | None = None, history: bool = False
    ) -> ProjectedRecord:
        source = self.connection.execute(
            "SELECT s.* FROM source_revisions r JOIN logical_sources s USING(source_id) "
            "WHERE r.capture_id=?",
            (anchor,),
        ).fetchone()
        if (
            source is None
            or not self.authority.permits_space(source["space_id"])
            or source["historical_only"]
            and not history
            or source["lifecycle"] != "active"
            or source["availability"] != "available"
        ):
            raise T03Error("not_found")
        head = source["head_capture_id"]
        if history and expected is not None:
            retained = self.connection.execute(
                "SELECT 1 FROM source_revisions WHERE source_id=? AND capture_id=?",
                (source["source_id"], expected),
            ).fetchone()
            if retained is None:
                raise T03Error("not_found")
            head = expected
        elif expected is not None and expected != head:
            raise T03Error("revision_changed")
        self._require_source_revision_privacy(head)
        _revision, record = self._capture(head)
        reference = record["source"]["reference"]
        payload = record["payload"]
        text = _payload_search_text(payload)
        if payload.get("kind") == "file":
            digest = payload["blob_sha256"]
            blob = read_confined(
                root=self.profile.root,
                relative=f"sources/blobs/sha256/{digest[:2]}/{digest}",
                expected_root_identity=self.profile.root_identity,
            )
            if blob is None or sha256(blob).hexdigest() != digest:
                raise T03Error("not_found")
            text = FilePayload(payload["file_name"], payload["media_type"], blob).search_text()
        body = public_search_text(text, protected_source_reference=reference)
        row = self.connection.execute(
            "SELECT title FROM captures WHERE capture_id=?", (head,)
        ).fetchone()
        title = (
            row["title"]
            if row is not None and row["title"] is not None
            else source_search_title(payload_family=record["payload"]["family"], body=body)
        )
        origin = self._origin(record)
        summary = {
            "record_id": head,
            "record_type": "source",
            "revision_id": head,
            "source_id": source["source_id"],
            "payload_family": record["payload"]["family"],
            "space_id": source["space_id"],
            "title": public_search_text(title, protected_source_reference=reference),
            "excerpt": " ".join(body.split())[:500],
            "trust": "owner"
            if origin == "owner_authored"
            else "third_party"
            if origin == "third_party"
            else "unverified",
            "provenance": {
                "representative_capture_id": head,
                "capture_ids": [head],
                "source_origin": origin,
            },
            "source_update_available": False,
        }
        validate_wire("summary", summary)
        return ProjectedRecord(summary, body)

    @staticmethod
    def _origin(record: dict[str, Any]) -> str:
        return public_source_origin(
            {
                "provenance_json": json.dumps(record["provenance"]),
                "source_origin": record["source"]["origin"],
                "source_reference": record["source"]["reference"],
            }
        )

    def canonical(
        self, page_id: str, *, expected: str | None = None, history: bool = False
    ) -> ProjectedRecord:
        document = self.connection.execute(
            "SELECT * FROM search_documents WHERE result_id=? AND record_type='canonical'",
            (page_id,),
        ).fetchone()
        if document is None or not self.authority.permits_space(document["space_id"]):
            raise T03Error("not_found")
        self._require_tier(document["effective_tier"])
        head = self.connection.execute(
            "SELECT publication_id FROM review_page_heads WHERE page_id=?", (page_id,)
        ).fetchone()
        if head is None:
            head = self.connection.execute(
                "SELECT publication_id FROM captures WHERE page_id=? AND stage=3", (page_id,)
            ).fetchone()
        if head is None or head["publication_id"] is None:
            # Legacy unbound publication: the durable current bytes identify its exact revision.
            current = read_confined(
                root=self.profile.root,
                relative=document["canonical_path"],
                expected_root_identity=self.profile.root_identity,
            )
            candidates = self.connection.execute(
                "SELECT DISTINCT publication_id FROM canonical_revision_members WHERE page_id=?",
                (page_id,),
            )
            matching = [row[0] for row in candidates if self._publication(row[0])[1] == current]
            if len(matching) != 1:
                raise T03Error("not_found")
            publication_id = matching[0]
        else:
            publication_id = head["publication_id"]
        revision_id = canonical_revision_id(publication_id)
        current_revision = revision_id
        if history and expected is not None:
            retained = self.connection.execute(
                "SELECT DISTINCT publication_id FROM canonical_revision_members "
                "WHERE page_id=? AND revision_id=?",
                (page_id, expected),
            ).fetchone()
            if retained is None:
                raise T03Error("not_found")
            revision_id, publication_id = expected, retained[0]
        elif expected is not None and revision_id != expected:
            raise T03Error("revision_changed")
        publication, raw = self._publication(publication_id)
        current = read_confined(
            root=self.profile.root,
            relative=document["canonical_path"],
            expected_root_identity=self.profile.root_identity,
        )
        if (
            revision_id == current_revision
            and current != raw
            or publication["page_id"] != page_id
            or publication["published_path"] != document["canonical_path"]
        ):
            raise T03Error("not_found")
        page = parse_markdown(raw)
        members = [
            row[0]
            for row in self.connection.execute(
                "SELECT capture_id FROM canonical_revision_members "
                "WHERE revision_id=? ORDER BY ordinal",
                (revision_id,),
            )
        ]
        if (
            not members
            or members != page.fields["provenance"]
            or revision_id == current_revision
            and members[0] != document["capture_id"]
            or page.fields["space_id"] != document["space_id"]
        ):
            raise T03Error("not_found")
        captures = [self._capture(capture_id)[1] for capture_id in members]
        references = tuple(record["source"]["reference"] for record in captures)
        origins = {self._origin(record) for record in captures}
        origin = next(iter(origins)) if len(origins) == 1 else "mixed"
        body = public_search_text(
            page.body,
            protected_source_reference=references[0],
            additional_source_references=references[1:],
        )
        title = public_search_text(
            cast(str, page.fields["title"]),
            protected_source_reference=references[0],
            additional_source_references=references[1:],
        )
        updated = any(
            self.connection.execute(
                "SELECT s.head_capture_id!=r.capture_id FROM source_revisions r "
                "JOIN logical_sources s USING(source_id) WHERE r.capture_id=?",
                (capture_id,),
            ).fetchone()[0]
            for capture_id in members
        )
        trust = page.fields["trust"]
        if trust not in {"owner", "third_party", "reviewed", "unverified"} or origin in {
            "mixed",
            "unknown",
        }:
            trust = "unverified"
        summary = {
            "record_id": page_id,
            "record_type": "canonical",
            "revision_id": revision_id,
            "source_id": None,
            "payload_family": document["payload_family"],
            "space_id": document["space_id"],
            "title": title,
            "excerpt": " ".join(body.split())[:500],
            "trust": trust,
            "provenance": {
                "representative_capture_id": members[0],
                "capture_ids": members,
                "source_origin": origin,
            },
            "source_update_available": updated,
        }
        validate_wire("summary", summary)
        indexed_text = body
        automatic = self.connection.execute(
            "SELECT capture_id FROM captures WHERE publication_id=? AND action='canonical_note' "
            "AND submission_path != 'import'",
            (publication_id,),
        ).fetchone()
        if automatic is not None:
            _, capture = self._capture(automatic["capture_id"])
            indexed_text = public_search_text(
                capture["payload"]["text"],
                protected_source_reference=references[0],
                additional_source_references=references[1:],
            )
        return ProjectedRecord(summary, body, indexed_text)

    def _publication(self, publication_id: str) -> tuple[dict[str, Any], bytes]:
        self._require_canonical_revision_privacy(publication_id)
        rows = list(
            self.connection.execute(
                "SELECT publication_path FROM captures WHERE publication_id=? "
                "UNION SELECT publication_path FROM decisions WHERE publication_id=?",
                (publication_id, publication_id),
            )
        )
        if not rows or len(rows) != 1 or rows[0][0] is None:
            raise T03Error("not_found")
        path = rows[0][0]
        raw = read_confined(
            root=self.profile.root, relative=path, expected_root_identity=self.profile.root_identity
        )
        if raw is None:
            raise T03Error("not_found")
        validate_portable_write(path, raw, self.profile.tenant_id)
        publication = json.loads(raw)
        if publication["publication_id"] != publication_id:
            raise T03Error("not_found")
        published = base64.b64decode(publication["published_bytes_base64"], validate=True)
        decision = self.connection.execute(
            "SELECT * FROM decisions WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
        if decision is not None:
            if (
                decision["decision_id"] != publication["decision_id"]
                or decision["page_id"] != publication["page_id"]
                or decision["recorded_at"] != publication["recorded_at"]
                or decision["effective_bytes"] != published
                or decision["outcome"] not in {"approved", "edited"}
            ):
                raise T03Error("not_found")
        else:
            automatic = self.connection.execute(
                "SELECT * FROM captures WHERE publication_id=? "
                "AND action='canonical_note' AND stage=3",
                (publication_id,),
            ).fetchone()
            if automatic is None or automatic["auto_decision_id"] != publication["decision_id"]:
                raise T03Error("not_found")
            _, capture = self._capture(automatic["capture_id"])
            if capture["payload"]["family"] != "text":
                raise T03Error("not_found")
            body = capture["payload"]["text"]
            title = automatic["title"]
            if title is None:
                title = next(
                    (
                        line.strip().lstrip("#").strip()
                        for line in body.splitlines()
                        if line.strip()
                    ),
                    "Untitled note",
                )[:200]
            expected = render_markdown(
                fields={
                    "actor_id": self.profile.owner_actor_id,
                    "modified_at": capture["accepted_at"],
                    "page_id": automatic["page_id"],
                    "privacy": _privacy(),
                    "provenance": [automatic["capture_id"]],
                    "role_claim": _role_claim(self.profile),
                    "schema_version": 1,
                    "space_id": capture["space_id"],
                    "status": "active",
                    "tenant_id": self.profile.tenant_id,
                    "title": title,
                    "trust": "owner",
                },
                body=body if body.endswith("\n") else body + "\n",
            ).encode("utf-8")
            if published != expected or publication["recorded_at"] != capture["accepted_at"]:
                raise T03Error("not_found")
        head = self.connection.execute(
            "SELECT published_sha256,page_id,canonical_path FROM review_page_heads "
            "WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
        if head is not None and (
            head["published_sha256"] != sha256(published).hexdigest()
            or head["page_id"] != publication["page_id"]
            or head["canonical_path"] != publication["published_path"]
        ):
            raise T03Error("not_found")
        return publication, published

    def read(
        self, record_id: str, *, expected: str | None = None, history: bool = False
    ) -> ProjectedRecord:
        try:
            return (
                self.source(record_id, expected=expected, history=history)
                if record_id.startswith("capture_")
                else self.canonical(record_id, expected=expected, history=history)
            )
        except T03Error:
            raise
        except ValueError, TypeError, KeyError, OSError, StorageError:
            raise T03Error("not_found") from None
