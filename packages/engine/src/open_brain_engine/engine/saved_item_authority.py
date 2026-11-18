"""Verify the local owner's identity against the same snapshot used for admission."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .consent_contracts import EgressMode
from .saved_item_identity import SavedItemSource
from .t03_contracts import EffectiveAuthority, SessionOrigin

if TYPE_CHECKING:
    from .contracts import LocalEngineContext


def require_saved_item_owner(
    profile: LocalEngineContext,
    db: sqlite3.Connection,
    authority: EffectiveAuthority,
) -> None:
    if (
        type(authority) is not EffectiveAuthority
        or not authority.owner
        or authority.egress_mode is not EgressMode.OWNER_LOCAL
        or authority.session_origin is not SessionOrigin.LOCAL_OWNER
        or authority.principal_id != profile.owner_actor_id
    ):
        raise PermissionError("owner_local_required")
    row = db.execute(
        "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if (
        row is None
        or authority.brain_id is not None
        and tuple(row) != (authority.brain_id, authority.issuer_epoch)
    ):
        raise PermissionError("owner_local_required")


@dataclass(frozen=True, slots=True)
class SelectedSavedItemAdmission:
    source: SavedItemSource
    item_key: str
    authority: EffectiveAuthority

    def admit(self, profile: LocalEngineContext, db: sqlite3.Connection, delivery_id: str) -> None:
        require_saved_item_owner(profile, db, self.authority)
        if self.source.delivery_id(self.item_key) != delivery_id:
            raise PermissionError("saved_item_selection_mismatch")
        self.source.register(db)
        if db.execute("PRAGMA user_version").fetchone()[0] >= 25:
            self.source.witness(db, self.item_key)
