"""Portable v1 saved-item source identity and the existing connector delivery identity."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from hashlib import sha256


def source_delivery_id(connector: str, connection: str, resource: str, external: str) -> str:
    digest = sha256("\x1f".join((connector, connection, resource, external)).encode()).hexdigest()
    return f"connector.{connector}.{digest}"


@dataclass(frozen=True, slots=True, order=True)
class SavedItemSource:
    connector_name: str
    connection_id: str
    resource_id: str

    def __post_init__(self) -> None:
        if (
            self.connector_name != "saved_item"
            or not isinstance(self.connection_id, str)
            or re.fullmatch(r"folder:[0-9a-f]{32}", self.connection_id) is None
            or not isinstance(self.resource_id, str)
            or re.fullmatch(r"items:[0-9a-f]{32}", self.resource_id) is None
        ):
            raise ValueError("invalid saved-item source identity")

    def values(self) -> tuple[str, str, str]:
        return self.connector_name, self.connection_id, self.resource_id

    def delivery_id(self, item_key: str) -> str:
        if not isinstance(item_key, str) or re.fullmatch(r"[0-9a-f]{32}", item_key) is None:
            raise ValueError("invalid saved-item key")
        return source_delivery_id(*self.values(), item_key)

    def register(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO saved_item_sources VALUES (?,?,?) ON CONFLICT DO NOTHING", self.values()
        )

    def witness(self, db: sqlite3.Connection, item_key: str) -> None:
        """Remember an owner-verified source mapping on this installation."""
        db.execute(
            "INSERT OR IGNORE INTO saved_item_local_witnesses VALUES(?,?,?,?,?)",
            (self.delivery_id(item_key), *self.values(), item_key),
        )
