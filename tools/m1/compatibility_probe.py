"""Synthetic M1 dependency and encrypted-storage compatibility probe."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import rfc8785
from sqlcipher3 import dbapi2  # type: ignore[import-untyped]

DATABASE_KEY = bytes.fromhex("e6d4c7a749fb1ce2512038241df0b14ad73707b860132a03790009a3445dce8b")
WRONG_DATABASE_KEY = bytes.fromhex(
    "f7e5d8b85a0c2df3623149352e01c25be84818c971243b148a111ab4556edf9c"
)
PLAINTEXT_CANARY = "m1-synthetic-purge-canary-7e64bf3b"
COMMIT_ROWS = 256
QUERY_ROWS = 2_048
QUERY_PAGE_SIZE = 32


def _key_database(connection: dbapi2.Connection, key: bytes) -> None:
    connection.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
    connection.execute("PRAGMA cipher_compatibility = 4")
    connection.execute("PRAGMA cipher_page_size = 4096")
    connection.execute("PRAGMA kdf_iter = 256000")
    connection.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
    connection.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
    connection.execute("PRAGMA cipher_memory_security = ON")
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()


def _connect(path: Path, key: bytes = DATABASE_KEY) -> dbapi2.Connection:
    connection = dbapi2.connect(path)
    _key_database(connection, key)
    return connection


def _crash_child(database: Path, key_hex: str, committed: bool) -> None:
    connection = _connect(database, bytes.fromhex(key_hex))
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO crash_events(value) VALUES (?)",
        ("committed" if committed else "uncommitted",),
    )
    if committed:
        connection.commit()
    os._exit(73)


def _run_crash_child(database: Path, committed: bool) -> int:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--crash-child",
            str(database),
            DATABASE_KEY.hex(),
            "committed" if committed else "uncommitted",
        ],
        check=False,
    )
    return result.returncode


def _scan_for_canary(directory: Path) -> list[str]:
    canary = PLAINTEXT_CANARY.encode()
    residue: list[str] = []
    for candidate in directory.iterdir():
        if candidate.is_file() and canary in candidate.read_bytes():
            residue.append(candidate.name)
    return residue


def _probe_sqlcipher(root: Path) -> dict[str, Any]:
    database = root / "encrypted.sqlite3"
    connection = _connect(database)
    cipher_version = str(connection.execute("PRAGMA cipher_version").fetchone()[0])
    compile_options = {
        str(row[0]) for row in connection.execute("PRAGMA compile_options").fetchall()
    }
    if not any(option == "ENABLE_FTS5" for option in compile_options):
        raise AssertionError("SQLCipher was built without FTS5")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(body)")
    connection.execute("INSERT INTO documents(id, body) VALUES (?, ?)", (1, PLAINTEXT_CANARY))
    connection.execute(
        "INSERT INTO documents_fts(rowid, body) VALUES (?, ?)",
        (1, PLAINTEXT_CANARY),
    )
    connection.commit()
    connection.close()

    reopened = _connect(database)
    hit = reopened.execute(
        "SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?", ("synthetic",)
    ).fetchone()
    if hit != (1,):
        raise AssertionError(f"FTS5 keyed reopen returned {hit!r}")
    reopened.close()

    wrong_key_rejected = False
    try:
        wrong = _connect(database, WRONG_DATABASE_KEY)
        wrong.close()
    except dbapi2.DatabaseError:
        wrong_key_rejected = True
    if not wrong_key_rejected:
        raise AssertionError("SQLCipher accepted the wrong key")

    purged = _connect(database)
    purged.execute("DELETE FROM documents_fts")
    purged.execute("DELETE FROM documents")
    purged.execute("INSERT INTO documents_fts(documents_fts) VALUES ('optimize')")
    purged.commit()
    purged.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    purged.execute("VACUUM")
    purged.close()
    residue = _scan_for_canary(root)
    if residue:
        raise AssertionError(f"plaintext canary remained in {residue!r}")

    crash_database = root / "crash.sqlite3"
    crash = _connect(crash_database)
    crash.execute("PRAGMA journal_mode = WAL")
    crash.execute("CREATE TABLE crash_events(value TEXT NOT NULL)")
    crash.commit()
    crash.close()
    if _run_crash_child(crash_database, committed=False) != 73:
        raise AssertionError("uncommitted crash child did not exit at the crash boundary")
    recovered = _connect(crash_database)
    values = [str(row[0]) for row in recovered.execute("SELECT value FROM crash_events")]
    recovered.close()
    if values:
        raise AssertionError(f"uncommitted crash row survived: {values!r}")
    if _run_crash_child(crash_database, committed=True) != 73:
        raise AssertionError("committed crash child did not exit at the crash boundary")
    recovered = _connect(crash_database)
    values = [str(row[0]) for row in recovered.execute("SELECT value FROM crash_events")]
    recovered.close()
    if values != ["committed"]:
        raise AssertionError(f"committed crash row was not recovered: {values!r}")

    benchmark = root / "benchmark.sqlite3"
    measured = _connect(benchmark)
    measured.execute("PRAGMA journal_mode = WAL")
    measured.execute("CREATE TABLE commits(sequence INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    started = time.perf_counter()
    for sequence in range(COMMIT_ROWS):
        measured.execute(
            "INSERT INTO commits(sequence, body) VALUES (?, ?)",
            (sequence, f"synthetic commit {sequence}"),
        )
        measured.commit()
    commit_seconds = time.perf_counter() - started
    measured.execute("CREATE VIRTUAL TABLE query_fts USING fts5(body)")
    measured.executemany(
        "INSERT INTO query_fts(rowid, body) VALUES (?, ?)",
        (
            (row + 1, f"shared synthetic token label-{row % 16} record-{row}")
            for row in range(QUERY_ROWS)
        ),
    )
    measured.commit()
    started = time.perf_counter()
    pages = 0
    for offset in range(0, QUERY_ROWS, QUERY_PAGE_SIZE):
        rows = measured.execute(
            "SELECT rowid FROM query_fts WHERE query_fts MATCH ? ORDER BY rowid LIMIT ? OFFSET ?",
            ("shared", QUERY_PAGE_SIZE, offset),
        ).fetchall()
        if not rows:
            break
        pages += 1
    paged_read_seconds = time.perf_counter() - started
    measured.close()

    return {
        "cipher_version": cipher_version,
        "compile_options": sorted(
            option for option in compile_options if option in {"ENABLE_FTS5", "THREADSAFE=1"}
        ),
        "keyed_reopen": "pass",
        "wrong_key_rejection": "pass",
        "fts5": "pass",
        "purge_plaintext_residue": "pass",
        "crash_recovery": "pass",
        "benchmark": {
            "commit_rows": COMMIT_ROWS,
            "commit_seconds": round(commit_seconds, 6),
            "query_rows": QUERY_ROWS,
            "query_page_size": QUERY_PAGE_SIZE,
            "query_pages": pages,
            "paged_read_seconds": round(paged_read_seconds, 6),
        },
    }


def _probe() -> dict[str, Any]:
    canonical = rfc8785.dumps({"b": 1, "a": "é"})
    expected = b'{"a":"\xc3\xa9","b":1}'
    if canonical != expected:
        raise AssertionError(f"unexpected RFC 8785 bytes: {canonical!r}")
    with tempfile.TemporaryDirectory(prefix="open-brain-m1-") as temporary:
        storage = _probe_sqlcipher(Path(temporary))
    return {
        "status": "pass",
        "python": platform.python_version(),
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
        "dependencies": {
            "rfc8785": importlib.metadata.version("rfc8785"),
            "sqlcipher3": importlib.metadata.version("sqlcipher3"),
        },
        "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        "storage": storage,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-child", action="store_true")
    parser.add_argument("database", nargs="?", type=Path)
    parser.add_argument("key_hex", nargs="?")
    parser.add_argument("crash_mode", nargs="?", choices=("committed", "uncommitted"))
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    if arguments.crash_child:
        if arguments.database is None or arguments.key_hex is None or arguments.crash_mode is None:
            raise SystemExit("crash child requires database, key, and mode")
        _crash_child(
            arguments.database,
            arguments.key_hex,
            committed=arguments.crash_mode == "committed",
        )
    print(json.dumps(_probe(), sort_keys=True))


if __name__ == "__main__":
    main()
