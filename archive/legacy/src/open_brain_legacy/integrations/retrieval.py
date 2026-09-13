"""Bounded, work-only retrieval over synthetic or configured Markdown pages."""

from __future__ import annotations

import os
import re
import secrets
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from open_brain_engine.storage.filesystem import StorageError, read_confined

from open_brain_legacy._compat.open_brain.integrations.ports import (
    FeedbackOutcome,
    RedactedText,
    RetrievalBatch,
    RetrievalFeedbackReceipt,
    RetrievalFeedbackRequest,
    RetrievalHit,
    RetrievalRequest,
    TrustLabel,
)

_WORD = re.compile(r"[A-Za-z0-9]+")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_MAX_PAGE_BYTES = 65_536
_MAX_EXCERPT_CHARACTERS = 512
_MIN_RESULT_BYTES = 64
_MAX_RESULT_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class WorkPageSnapshot:
    """A path-free page snapshot available to bounded context expansion."""

    result_id: str
    title: RedactedText
    markdown: RedactedText
    trust: TrustLabel
    linked_result_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalFeedbackRecord:
    """Allow-listed retrieval metadata with no query or result content fields."""

    retrieval_id: str
    outcome: FeedbackOutcome
    result_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "retrieval_id": self.retrieval_id,
            "outcome": self.outcome.value,
            "result_ids": list(self.result_ids),
        }


class MetadataOnlyRetrievalFeedback:
    """In-memory feedback seam that accepts only IDs and allow-listed outcomes."""

    def __init__(self, *, enabled: bool = True) -> None:
        if type(enabled) is not bool:
            raise ValueError("invalid feedback configuration")
        self._enabled = enabled
        self._known_results: dict[str, frozenset[str]] = {}
        self._records: list[RetrievalFeedbackRecord] = []

    @property
    def records(self) -> tuple[RetrievalFeedbackRecord, ...]:
        return tuple(self._records)

    def register(self, batch: RetrievalBatch) -> None:
        """Register only the opaque IDs required to validate later feedback."""
        if self._enabled:
            self._known_results[batch.retrieval_id] = frozenset(
                hit.result_id for hit in batch.hits
            )

    def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt:
        if not isinstance(request, RetrievalFeedbackRequest):
            raise ValueError("invalid retrieval feedback")
        if not self._enabled:
            return RetrievalFeedbackReceipt(
                retrieval_id=request.retrieval_id,
                outcome=request.outcome,
                result_count=len(request.result_ids),
                recorded=False,
            )

        known = self._known_results.get(request.retrieval_id)
        if known is None:
            raise ValueError("unknown retrieval")
        if not set(request.result_ids).issubset(known):
            raise ValueError("unknown result")
        if request.outcome is FeedbackOutcome.CITED and not request.result_ids:
            raise ValueError("cited feedback requires result ids")
        if request.outcome is FeedbackOutcome.EMPTY and (known or request.result_ids):
            raise ValueError("invalid empty feedback")

        self._records.append(
            RetrievalFeedbackRecord(
                retrieval_id=request.retrieval_id,
                outcome=request.outcome,
                result_ids=request.result_ids,
            )
        )
        return RetrievalFeedbackReceipt(
            retrieval_id=request.retrieval_id,
            outcome=request.outcome,
            result_count=len(request.result_ids),
        )


@dataclass(frozen=True, slots=True)
class _PageSource:
    relative: str
    result_id: str
    title: RedactedText
    markdown: RedactedText
    trust: TrustLabel
    links: tuple[str, ...]


class FilesystemWorkRetriever:
    """Deterministic lexical retrieval limited to one configured work root."""

    def __init__(
        self,
        *,
        work_root: Path,
        max_bytes: int = 16_384,
        feedback: MetadataOnlyRetrievalFeedback | None = None,
    ) -> None:
        if (
            not isinstance(work_root, Path)
            or type(max_bytes) is not int
            or not _MIN_RESULT_BYTES <= max_bytes <= _MAX_RESULT_BYTES
            or (feedback is not None and not isinstance(feedback, MetadataOnlyRetrievalFeedback))
        ):
            raise ValueError("invalid work retrieval configuration")
        self._work_root = work_root.absolute()
        self._max_bytes = max_bytes
        self._feedback = feedback
        self._snapshots: dict[str, WorkPageSnapshot] = {}

    @property
    def available(self) -> bool:
        return _safe_directory(self._work_root) and _safe_directory(
            self._work_root / "pages"
        )

    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        if not isinstance(request, RetrievalRequest):
            raise ValueError("invalid retrieval request")
        pages = self._load_pages()
        retrieval_id = "retrieval_" + secrets.token_hex(16)
        if not self.available:
            return self._finish(
                RetrievalBatch(retrieval_id=retrieval_id, hits=(), truncated=False)
            )

        terms = _terms(request.question)
        ranked = sorted(
            (
                (_score(page, terms, request.question), page)
                for page in pages
            ),
            key=lambda item: (-item[0], item[1].title.text.casefold(), item[1].result_id),
        )
        candidates = tuple(page for score, page in ranked if score > 0)
        hits: list[RetrievalHit] = []
        used_bytes = 0
        clipped = False

        for page in candidates:
            if len(hits) >= request.limit:
                break
            excerpt_source = _best_excerpt(page.markdown.text, terms)
            title_bytes = len(page.title.text.encode("utf-8"))
            remaining = self._max_bytes - used_bytes - title_bytes
            if remaining <= 0:
                clipped = True
                continue
            excerpt, was_clipped = _truncate_utf8(excerpt_source, remaining)
            if not excerpt:
                clipped = True
                continue
            clipped = clipped or was_clipped
            public_excerpt = RedactedText.redact(excerpt)
            hit_bytes = title_bytes + len(public_excerpt.text.encode("utf-8"))
            if used_bytes + hit_bytes > self._max_bytes:
                clipped = True
                continue
            hits.append(
                RetrievalHit(
                    result_id=page.result_id,
                    rank=len(hits) + 1,
                    title=page.title,
                    excerpt=public_excerpt,
                    trust=page.trust,
                )
            )
            used_bytes += hit_bytes

        batch = RetrievalBatch(
            retrieval_id=retrieval_id,
            hits=tuple(hits),
            truncated=clipped or len(hits) < len(candidates),
        )
        return self._finish(batch)

    def snapshot(self, result_id: str) -> WorkPageSnapshot | None:
        """Resolve an opaque result ID without returning its backing path."""
        if not self._snapshots and self.available:
            self._load_pages()
        return self._snapshots.get(result_id)

    def _finish(self, batch: RetrievalBatch) -> RetrievalBatch:
        if self._feedback is not None:
            self._feedback.register(batch)
        return batch

    def _load_pages(self) -> tuple[WorkPageSnapshot, ...]:
        self._snapshots = {}
        pages_root = self._work_root / "pages"
        if not self.available:
            return ()

        sources: list[_PageSource] = []
        try:
            walker = os.walk(pages_root, topdown=True, followlinks=False)
            for directory, directory_names, file_names in walker:
                directory_path = Path(directory)
                directory_names[:] = sorted(
                    name
                    for name in directory_names
                    if _include_directory(directory_path / name, name)
                )
                for file_name in sorted(file_names):
                    if file_name.startswith(".") or not file_name.endswith(".md"):
                        continue
                    source = self._read_page(directory_path / file_name)
                    if source is not None:
                        sources.append(source)
        except OSError:
            self._snapshots = {}
            return ()

        aliases = _unambiguous_aliases(sources)
        snapshots = tuple(
            WorkPageSnapshot(
                result_id=source.result_id,
                title=source.title,
                markdown=source.markdown,
                trust=source.trust,
                linked_result_ids=_resolve_links(source.links, aliases),
            )
            for source in sorted(sources, key=lambda item: item.result_id)
        )
        self._snapshots = {page.result_id: page for page in snapshots}
        return snapshots

    def _read_page(self, path: Path) -> _PageSource | None:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_PAGE_BYTES:
                return None
            relative = path.relative_to(self._work_root).as_posix()
            payload = read_confined(root=self._work_root, relative=relative)
            if payload is None or len(payload) > _MAX_PAGE_BYTES:
                return None
            text = payload.decode("utf-8")
        except (OSError, StorageError, UnicodeError, ValueError):
            return None

        frontmatter, body = _frontmatter(text)
        if frontmatter.get("status", "").casefold() == "archived":
            return None
        title = frontmatter.get("title") or _first_heading(body) or path.stem
        trust = (
            TrustLabel.UNREVIEWED_THIRD_PARTY
            if relative.startswith("pages/learning/")
            else TrustLabel.VERIFIED_WORK
        )
        return _PageSource(
            relative=relative,
            result_id="result_" + sha256(relative.encode("utf-8")).hexdigest()[:32],
            title=RedactedText.redact(title[:256]),
            markdown=RedactedText.redact(text),
            trust=trust,
            links=tuple(match.group(1).strip() for match in _WIKILINK.finditer(body)),
        )


def _safe_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _include_directory(path: Path, name: str) -> bool:
    return (
        not name.startswith(".")
        and name.casefold() not in {"archive", "archived"}
        and _safe_directory(path)
    )


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"title", "status"}:
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, text[end + 5 :]


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return None


def _terms(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group().casefold() for match in _WORD.finditer(question)))


def _score(page: WorkPageSnapshot, terms: tuple[str, ...], question: str) -> int:
    if not terms:
        return 0
    title = page.title.text.casefold()
    markdown = page.markdown.text.casefold()
    phrase = " ".join(_terms(question))
    score = 100 if phrase and phrase in title else 0
    score += sum(20 * title.count(term) for term in terms)
    score += sum(min(markdown.count(term), 8) * 2 for term in terms)
    if all(term in title for term in terms):
        score += 25
    return score


def _best_excerpt(markdown: str, terms: tuple[str, ...]) -> str:
    _, body = _frontmatter(markdown)
    chunks = tuple(
        chunk.strip()
        for chunk in re.split(r"\n\s*\n", body)
        if chunk.strip()
    )
    if not chunks:
        return "[no public excerpt]"
    best = max(
        enumerate(chunks),
        key=lambda item: (
            sum(item[1].casefold().count(term) for term in terms),
            -item[0],
        ),
    )[1]
    normalized = " ".join(best.lstrip("# ").split())
    return normalized[:_MAX_EXCERPT_CHARACTERS] or "[no public excerpt]"


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    clipped = encoded[:maximum]
    while clipped:
        try:
            decoded = clipped.decode("utf-8").rstrip()
            return decoded, True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True


def _unambiguous_aliases(sources: list[_PageSource]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for source in sources:
        without_prefix = source.relative.removeprefix("pages/").removesuffix(".md")
        aliases = {
            without_prefix.casefold(),
            Path(without_prefix).name.casefold(),
            source.title.text.casefold(),
        }
        for alias in aliases:
            candidates.setdefault(alias, set()).add(source.result_id)
    return {
        alias: next(iter(result_ids))
        for alias, result_ids in candidates.items()
        if len(result_ids) == 1
    }


def _resolve_links(links: tuple[str, ...], aliases: dict[str, str]) -> tuple[str, ...]:
    result: list[str] = []
    for link in links:
        normalized = link.strip().strip("/").removeprefix("pages/").removesuffix(".md")
        if not normalized or normalized.startswith(("raw/", "archive/", "archived/")):
            continue
        result_id = aliases.get(normalized.casefold())
        if result_id is None:
            result_id = aliases.get(Path(normalized).name.casefold())
        if result_id is not None and result_id not in result:
            result.append(result_id)
    return tuple(result)
