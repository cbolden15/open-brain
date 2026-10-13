"""Schema14-only stored V2 bounds; rebuilds preserve old immutable row values."""

from .historical_schema import HISTORICAL_AUTHORITY_SCHEMA


def _rebuild(table: str, old_check: str, new_check: str) -> tuple[str, ...]:
    original = next(
        statement
        for statement in HISTORICAL_AUTHORITY_SCHEMA
        if statement.startswith(f"CREATE TABLE {table} (")
    )
    temporary = table + "_compatibility_next"
    definition = original.replace(f"CREATE TABLE {table} (", f"CREATE TABLE {temporary} (")
    definition = definition.replace(old_check, new_check)
    retained = table + "_compatibility_rows"
    return (
        f"CREATE TABLE {retained} AS SELECT * FROM {table}",
        definition,
        f"DROP TABLE {table}",
        f"ALTER TABLE {temporary} RENAME TO {table}",
        f"INSERT INTO {table} SELECT * FROM {retained}",
        f"DROP TABLE {retained}",
        *(
            statement
            for statement in HISTORICAL_AUTHORITY_SCHEMA
            if statement.startswith(f"CREATE TRIGGER {table}_")
        ),
    )


# Foreign keys stay enabled. Deferred checking allows a same-transaction replacement
# of referenced tables, and the migration verifies the complete final authority.
HISTORICAL_STORED_V2_BOUNDS = (
    "PRAGMA defer_foreign_keys=ON",
    *_rebuild(
        "historical_operations",
        "length(request_json)<=65536 AND json_valid(request_json)",
        "json_valid(request_json) AND json_type(request_json,'$.dto_version')='integer' "
        "AND ((json_extract(request_json,'$.dto_version')=1 AND length(request_json)<=65536) "
        "OR (json_extract(request_json,'$.dto_version')=2 AND length(request_json)<=524288))",
    ),
    *_rebuild(
        "historical_baselines",
        "length(envelope_bytes)<=65536 AND json_valid(envelope_bytes)",
        "length(envelope_bytes)<=524288 AND json_valid(envelope_bytes)",
    ),
    """CREATE TRIGGER historical_baselines_insert_versioned BEFORE INSERT ON historical_baselines
WHEN (SELECT json_extract(request_json,'$.dto_version') FROM historical_operations
      WHERE operation_id=NEW.operation_id)=1 AND length(NEW.envelope_bytes)>65536
BEGIN SELECT RAISE(ABORT,'historical V1 envelope exceeds its frozen bound'); END""",
)
