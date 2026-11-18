"""Owner-local source binding and grant-scoped lookup of saved page captures."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from typing import cast

from open_brain_engine.core.models import PrivacyTier

from .contracts import LocalEngineContext
from .local_schema import open_local_database_read_only
from .local_store import _LocalStore
from .records import RecordProjector
from .saved_item_authority import require_saved_item_owner
from .saved_item_identity import SavedItemSource
from .t03_contracts import EffectiveAuthority, T03Error


@dataclass(frozen=True, slots=True)
class SavedPage:
    capture_id: str
    space_id: str | None


def _accepted(connection: sqlite3.Connection, delivery: str) -> str | None:
    row = connection.execute(
        "SELECT capture_id FROM captures WHERE delivery_id=? AND stage=3 "
        "AND submission_path='public_job' AND source_origin='third_party'",
        (delivery,),
    ).fetchone()
    return str(row[0]) if row else None


class BrowserOwnerSetup:
    def __init__(self, profile: LocalEngineContext, authority: EffectiveAuthority) -> None:
        self.profile = profile
        self._authority = authority

    def sources(self, *, limit: int = 100, offset: int = 0) -> tuple[SavedItemSource, ...]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid source page")
        with closing(open_local_database_read_only(self.profile)) as db:
            require_saved_item_owner(self.profile, db, self._authority)
            return tuple(
                SavedItemSource(*row)
                for row in db.execute(
                    "SELECT connector_name,connection_id,resource_id FROM saved_item_sources "
                    "ORDER BY connector_name,connection_id,resource_id LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            )

    def register_legacy(self, source: SavedItemSource, item_key: str) -> None:
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            require_saved_item_owner(self.profile, db, self._authority)
            if _accepted(db, source.delivery_id(item_key)) is None:
                raise ValueError("legacy_rebinding_requires_original_source")
            source.register(db)

    def bind(
        self,
        source: SavedItemSource,
        *,
        tiers: Sequence[str],
        spaces: Sequence[str | None],
    ) -> str:
        if not tiers or not set(tiers) <= {"public", "work", "personal"}:
            raise ValueError("invalid browser tiers")
        if (
            not spaces
            or len(spaces) > 256
            or any(
                value is not None
                and (
                    not isinstance(value, str)
                    or re.fullmatch(r"space_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)
                    is None
                )
                for value in spaces
            )
        ):
            raise ValueError("invalid browser spaces")
        token = secrets.token_hex(32)
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            require_saved_item_owner(self.profile, db, self._authority)
            if not db.execute(
                "SELECT 1 FROM saved_item_sources WHERE connector_name=? AND "
                "connection_id=? AND resource_id=?",
                source.values(),
            ).fetchone():
                raise ValueError("legacy_rebinding_requires_original_source")
            db.execute(
                "INSERT INTO browser_grants SELECT ?,brain_id,generation,?,?,?,?,?,? "
                "FROM brain_identity CROSS JOIN browser_installation",
                (
                    sha256(token.encode()).hexdigest(),
                    *source.values(),
                    json.dumps(["lookup"]),
                    json.dumps(list(tiers)),
                    json.dumps(list(spaces)),
                ),
            )
        return token


class BrowserPages:
    def __init__(self, profile: LocalEngineContext) -> None:
        self.profile = profile

    @staticmethod
    def _grant(db: sqlite3.Connection, token: str, operation: str) -> sqlite3.Row:
        if not isinstance(token, str) or re.fullmatch("[0-9a-f]{64}", token) is None:
            raise PermissionError("browser_unbound")
        row = db.execute(
            "SELECT g.* FROM browser_grants g JOIN browser_installation i "
            "ON i.generation=g.generation JOIN brain_identity b ON b.brain_id=g.brain_id "
            "WHERE g.token_hash=?",
            (sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if row is None:
            raise PermissionError("browser_unbound")
        if operation not in json.loads(row["operations_json"]):
            raise PermissionError("browser_operation_denied")
        return cast(sqlite3.Row, row)

    def require(self, token: str, operation: str) -> None:
        with closing(open_local_database_read_only(self.profile)) as db:
            self._grant(db, token, operation)

    def lookup(self, token: str, item_key: str) -> SavedPage | None:
        with closing(open_local_database_read_only(self.profile)) as db:
            grant = self._grant(db, token, "lookup")
            source = SavedItemSource(
                grant["connector_name"], grant["connection_id"], grant["resource_id"]
            )
            capture = _accepted(db, source.delivery_id(item_key))
            if capture is None:
                return None
            spaces = json.loads(grant["spaces_json"])
            row = db.execute(
                "SELECT s.space_id FROM logical_sources s JOIN source_revisions r USING(source_id) "
                "WHERE r.capture_id=? AND s.head_capture_id=?",
                (capture, capture),
            ).fetchone()
            if row is None or row[0] not in spaces:
                return None
            authority = EffectiveAuthority(
                principal_id="browser",
                session_id="browser-lookup",
                capabilities=frozenset(),
                space_ids=None if row[0] is None else frozenset({row[0]}),
                allowed_read_tiers=frozenset(
                    PrivacyTier(t) for t in json.loads(grant["tiers_json"])
                ),
            )
            try:
                RecordProjector(self.profile, db, authority).source(capture, expected=capture)
            except T03Error as error:
                if error.code == "not_found":
                    return None
                raise
            return SavedPage(capture, row[0])
