"""Deterministic synthetic multi-label corpus and encrypted FTS limit benchmark."""

from __future__ import annotations

import hashlib
import json
import platform
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import rfc8785
from sqlcipher3 import dbapi2  # type: ignore[import-untyped]

SEED = "open-brain-m1-v1-20260906"
LABEL_POOL_SIZE = 64
ACTIVE_LABEL_SETS = 256
HOT_SHARD_RECORDS = 50_000
COLD_SHARD_RECORDS = 64
QUERY_FANOUT_PER_ROUND = 32
COMMIT_BATCH_ITEMS = 128
DATABASE_KEY = "7a9dc5e98f5d67c547875e4912a102a5f90e09356b0aa3482fd0677cc3b8315a"


@dataclass(frozen=True, slots=True)
class Shard:
    index: int
    labels: tuple[str, ...]
    records: int


def _label_set(index: int, width: int) -> tuple[str, ...]:
    ranked = sorted(
        range(LABEL_POOL_SIZE),
        key=lambda number: hashlib.sha256(f"{SEED}:{index}:{number}".encode()).digest(),
    )
    return tuple(f"label-{number:02d}" for number in sorted(ranked[:width]))


def generate_shards() -> tuple[Shard, ...]:
    shards: list[Shard] = []
    seen: set[tuple[str, ...]] = set()
    for index in range(ACTIVE_LABEL_SETS):
        width = 16 if index == 0 else 1 + (index % 8)
        labels = _label_set(index, width)
        retry = 0
        while labels in seen:
            retry += 1
            if retry > LABEL_POOL_SIZE * ACTIVE_LABEL_SETS:
                raise AssertionError("could not create the required unique label-set inventory")
            labels = _label_set(index + retry * ACTIVE_LABEL_SETS, width)
        seen.add(labels)
        shards.append(
            Shard(
                index=index,
                labels=labels,
                records=HOT_SHARD_RECORDS if index == 0 else COLD_SHARD_RECORDS,
            )
        )
    return tuple(shards)


def corpus_catalog_digest(shards: tuple[Shard, ...]) -> str:
    value = [
        {"index": shard.index, "labels": list(shard.labels), "records": shard.records}
        for shard in shards
    ]
    return hashlib.sha256(rfc8785.dumps(cast(Any, value))).hexdigest()


def _connect(path: Path) -> dbapi2.Connection:
    connection = dbapi2.connect(path)
    connection.execute(f"PRAGMA key = \"x'{DATABASE_KEY}'\"")
    connection.execute("PRAGMA cipher_compatibility = 4")
    connection.execute("PRAGMA cipher_page_size = 4096")
    connection.execute("PRAGMA kdf_iter = 256000")
    connection.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
    connection.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
    connection.execute("PRAGMA cipher_memory_security = ON")
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return connection


def run_benchmark() -> dict[str, Any]:
    shards = generate_shards()
    with tempfile.TemporaryDirectory(prefix="open-brain-m1-corpus-") as temporary:
        connection = _connect(Path(temporary) / "corpus.sqlite3")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "CREATE TABLE shard_catalog(shard_index INTEGER PRIMARY KEY, labels TEXT NOT NULL)"
        )
        started = time.perf_counter()
        for shard in shards:
            table = f"fts_{shard.index:03d}"
            connection.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(body)")
            connection.execute(
                "INSERT INTO shard_catalog(shard_index, labels) VALUES (?, ?)",
                (shard.index, json.dumps(shard.labels)),
            )
            connection.executemany(
                f"INSERT INTO {table}(rowid, body) VALUES (?, ?)",
                (
                    (
                        row + 1,
                        f"shared synthetic shard-{shard.index:03d} record-{row:05d}",
                    )
                    for row in range(shard.records)
                ),
            )
        connection.commit()
        load_seconds = time.perf_counter() - started

        started = time.perf_counter()
        connection.execute(
            "CREATE TABLE commits(sequence INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO commits(sequence, value) VALUES (?, ?)",
            ((index, f"synthetic commit {index}") for index in range(COMMIT_BATCH_ITEMS)),
        )
        connection.commit()
        commit_batch_seconds = time.perf_counter() - started

        started = time.perf_counter()
        rounds = 0
        candidates: list[tuple[float, int, int]] = []
        for start in range(0, ACTIVE_LABEL_SETS, QUERY_FANOUT_PER_ROUND):
            rounds += 1
            for shard_index in range(start, start + QUERY_FANOUT_PER_ROUND):
                table = f"fts_{shard_index:03d}"
                rows = connection.execute(
                    f"SELECT bm25({table}), rowid FROM {table} "
                    f"WHERE {table} MATCH ? ORDER BY bm25({table}), rowid LIMIT 100",
                    ("shared",),
                ).fetchall()
                candidates.extend((float(score), shard_index, int(rowid)) for score, rowid in rows)
            candidates = sorted(candidates)[:100]
        query_seconds = time.perf_counter() - started
        connection.close()

    if rounds != 8 or len(candidates) != 100:
        raise AssertionError("synthetic fan-out benchmark did not traverse the frozen inventory")
    return {
        "status": "passed",
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "generator": {
            "version": 1,
            "seed": SEED,
            "label_pool": LABEL_POOL_SIZE,
            "active_label_sets": len(shards),
            "labels_per_record_maximum": max(len(shard.labels) for shard in shards),
            "hot_shard_records": max(shard.records for shard in shards),
            "cold_shard_records": COLD_SHARD_RECORDS,
            "total_records": sum(shard.records for shard in shards),
            "catalog_sha256": corpus_catalog_digest(shards),
        },
        "benchmark": {
            "load_seconds": round(load_seconds, 6),
            "commit_batch_items": COMMIT_BATCH_ITEMS,
            "commit_batch_seconds": round(commit_batch_seconds, 6),
            "query_fanout_per_round": QUERY_FANOUT_PER_ROUND,
            "query_rounds": rounds,
            "final_top_k": len(candidates),
            "query_seconds": round(query_seconds, 6),
        },
    }


def main() -> None:
    print(json.dumps(run_benchmark(), sort_keys=True))


if __name__ == "__main__":
    main()
