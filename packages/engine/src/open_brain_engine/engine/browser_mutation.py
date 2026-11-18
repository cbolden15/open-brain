"""Current write-domain admission shared by child mutations on saved pages."""

import sqlite3

from .browser_pages import _require_complete_write_domain
from .saved_item_identity import SavedItemSource


def require_child_write(db: sqlite3.Connection, grant: sqlite3.Row) -> None:
    if not grant["complete_domain"]:
        raise PermissionError("browser_operation_denied")
    _require_complete_write_domain(
        db, SavedItemSource(grant["connector_name"], grant["connection_id"], grant["resource_id"])
    )
