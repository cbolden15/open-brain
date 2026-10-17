"""Frozen synthetic relevance corpus contract for the G4 semantic gate."""

import copy
import json
from typing import Any

import pytest

from tools.g4_embedding_probe import corpus as corpus_module
from tools.g4_embedding_probe.corpus import CORPUS, CorpusError, content_words, parse_corpus


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(CORPUS.read_text(encoding="utf-8"))
    return document


def test_committed_corpus_satisfies_contract() -> None:
    corpus = parse_corpus(_document())
    assert len(corpus.records) == 200
    assert sum(q.kind == "paraphrase" for q in corpus.queries) == 40
    assert sum(q.kind == "exact_id" for q in corpus.queries) == 20


def test_content_words_stem_and_drop_stopwords() -> None:
    assert content_words("The invoices were being reconciled") == {"invoic", "reconcil"}


def test_rejects_paraphrase_that_copies_its_target() -> None:
    document = _document()
    query = next(q for q in document["queries"] if q["kind"] == "paraphrase")
    target = query["relevant"][0]
    title, body = next((r["title"], r["body"]) for r in document["records"] if r["id"] == target)
    query["text"] = f"{title} {body}"[:200]
    with pytest.raises(CorpusError, match="shares"):
        parse_corpus(document)


def test_rejects_identifier_present_outside_relevant_records() -> None:
    document = _document()
    query = next(q for q in document["queries"] if q["kind"] == "exact_id")
    other = next(r for r in document["records"] if r["id"] not in query["relevant"])
    other["body"] += f" {query['text']}"
    with pytest.raises(CorpusError, match="identifier"):
        parse_corpus(document)


def test_rejects_too_few_lexical_decoys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corpus_module, "MIN_LEXICAL_DECOY_QUERIES", 41)
    with pytest.raises(CorpusError, match="decoy"):
        parse_corpus(_document())


@pytest.mark.parametrize("mutate", ["drop_record", "bad_kind", "unknown_relevant"])
def test_rejects_structural_violations(mutate: str) -> None:
    document = copy.deepcopy(_document())
    if mutate == "drop_record":
        document["records"].pop()
    elif mutate == "bad_kind":
        document["queries"][0]["kind"] = "fuzzy"
    else:
        document["queries"][0]["relevant"] = ["r999"]
    with pytest.raises(CorpusError):
        parse_corpus(document)
