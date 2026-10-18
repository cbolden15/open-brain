"""Frozen synthetic relevance corpus for the G4 semantic acceptance gate.

The corpus has 200 records, 40 paraphrase queries and 20 exact-identifier
queries. Validation keeps the paraphrase gate honest: a paraphrase query may
share at most one stemmed content word with any of its relevant records, so
lexical search cannot pass it by word matching. Exact-identifier queries name
one token that appears verbatim only in their relevant records.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORPUS = Path(__file__).parent / "corpus" / "relevance-v1.json"
SCHEMA = "ob.g4-relevance-corpus.v1"
RECORD_COUNT = 200
PARAPHRASE_COUNT = 40
EXACT_COUNT = 20
MAX_PARAPHRASE_OVERLAP = 1
MIN_LEXICAL_DECOY_QUERIES = 20
IDENTIFIER = re.compile(r"^[A-Z0-9][A-Za-z0-9_.:-]{3,40}$")
STOPWORDS = frozenset(
    """
    about above after again against all also and any are because been before being below
    between both but can could did does doing down during each few for from further had has
    have having her here hers him his how into its itself just more most much must not now
    off once only other our ours out over own same she should some such than that the their
    theirs them then there these they this those through too under until upon very was were
    what when where which while who whom why will with would you your yours get got make made
    use used using one two way thing things like want need
    """.split()  # noqa: SIM905
)


class CorpusError(ValueError):
    """The relevance corpus violates its frozen contract."""


@dataclass(frozen=True)
class Query:
    id: str
    kind: str
    text: str
    relevant: tuple[str, ...]


@dataclass(frozen=True)
class Corpus:
    records: dict[str, tuple[str, str]]
    queries: tuple[Query, ...]


def stem(word: str) -> str:
    for suffix in ("ingly", "ings", "ing", "edly", "ed", "ies", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def content_words(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(stem(w) for w in words if len(w) >= 3 and w not in STOPWORDS)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorpusError(message)


def parse_corpus(document: Any) -> Corpus:
    _require(isinstance(document, dict), "corpus must be an object")
    _require(set(document) == {"schema", "records", "queries"}, "unexpected corpus keys")
    _require(document["schema"] == SCHEMA, "unsupported corpus schema")
    records: dict[str, tuple[str, str]] = {}
    for index, record in enumerate(document["records"], start=1):
        _require(set(record) == {"id", "title", "body"}, "unexpected record keys")
        _require(record["id"] == f"r{index:03d}", f"record ids must be contiguous at {index}")
        _require(3 <= len(record["title"]) <= 80, f"{record['id']} title length")
        word_count = len(record["body"].split())
        _require(30 <= word_count <= 200, f"{record['id']} body must be 30-200 words")
        records[record["id"]] = (record["title"], record["body"])
    _require(len(records) == RECORD_COUNT, f"expected {RECORD_COUNT} records")
    _require(len({t for t, _ in records.values()}) == RECORD_COUNT, "record titles must be unique")

    queries = []
    for query in document["queries"]:
        _require(set(query) == {"id", "kind", "text", "relevant"}, "unexpected query keys")
        relevant = tuple(query["relevant"])
        _require(1 <= len(relevant) <= 3, f"{query['id']} needs 1-3 relevant records")
        _require(all(r in records for r in relevant), f"{query['id']} names unknown records")
        _require(len(set(relevant)) == len(relevant), f"{query['id']} repeats a record")
        queries.append(Query(query["id"], query["kind"], query["text"], relevant))

    paraphrase = [q for q in queries if q.kind == "paraphrase"]
    exact = [q for q in queries if q.kind == "exact_id"]
    _require(len(paraphrase) + len(exact) == len(queries), "unknown query kind")
    _require(
        [q.id for q in paraphrase] == [f"p{i:02d}" for i in range(1, PARAPHRASE_COUNT + 1)],
        f"expected paraphrase ids p01-p{PARAPHRASE_COUNT}",
    )
    _require(
        [q.id for q in exact] == [f"e{i:02d}" for i in range(1, EXACT_COUNT + 1)],
        f"expected exact ids e01-e{EXACT_COUNT}",
    )
    record_words = {rid: content_words(f"{t} {b}") for rid, (t, b) in records.items()}

    decoyed = 0
    for q in paraphrase:
        words = content_words(q.text)
        _require(len(words) >= 3, f"{q.id} needs at least three content words")
        for rid in q.relevant:
            overlap = words & record_words[rid]
            _require(
                len(overlap) <= MAX_PARAPHRASE_OVERLAP,
                f"{q.id} shares {sorted(overlap)} with {rid}",
            )
        others = (rid for rid in records if rid not in q.relevant)
        if any(len(words & record_words[rid]) >= 2 for rid in others):
            decoyed += 1
    _require(
        decoyed >= MIN_LEXICAL_DECOY_QUERIES,
        f"only {decoyed} paraphrase queries have a lexical decoy record",
    )

    for q in exact:
        _require(bool(IDENTIFIER.match(q.text)), f"{q.id} must be a single identifier token")
        holders = {rid for rid, (t, b) in records.items() if q.text in f"{t} {b}"}
        _require(holders == set(q.relevant), f"{q.id} identifier must appear only in its records")
    return Corpus(records=records, queries=tuple(queries))


def load_corpus(path: Path = CORPUS) -> Corpus:
    return parse_corpus(json.loads(path.read_text(encoding="utf-8")))
