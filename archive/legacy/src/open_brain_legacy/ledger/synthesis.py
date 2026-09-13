"""Strict, mutation-free ledger synthesis preparation outside all writer locks."""

from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision, ValidationError
from open_brain_engine.core.ports import Provider, TextModelRequest, TextModelResult
from open_brain_engine.storage.frontmatter import markdown_relative_path

from .merge import TrustedCitation
from .sanitize import LedgerSection, SanitizedLeaf, sanitize_leaf
from .stage import LedgerStage
from .store import DurableSlimState, LedgerRowIdentity, PublishedDocumentSet

if TYPE_CHECKING:
    from .synthesis_store import DurableSynthesisRecord

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_SOURCES = 32
_MAX_CONTEXT_BYTES = 8_192
_MAX_CLAIMS = 32
_MAX_CLAIM_BYTES = 2_048
_MAX_CLAIM_SOURCES = 16


class SynthesisError(StrEnum):
    INVALID_REQUEST = "invalid_request"
    SOURCE_GATE = "source_gate"
    LOCK_HELD = "lock_held"
    PROVIDER_FAILURE = "provider_failure"
    OUTPUT_LIMIT = "output_limit"
    MALFORMED_RESULT = "malformed_result"
    QUARANTINED_RESULT = "quarantined_result"
    PERSISTENCE_FAILURE = "persistence_failure"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class _CodecFailure(ValueError):
    def __init__(self, code: SynthesisError) -> None:
        self.code = code
        super().__init__(code.value)


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _bounded_text(value: object, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized.isspace()
        or "\x00" in normalized
        or len(normalized.encode("utf-8")) > max_bytes
    ):
        raise ValueError(f"invalid {field}")
    return normalized


def _is_sanitized_context(*, item_id: str, context: SanitizedLeaf) -> bool:
    if not isinstance(context.text, str) or not isinstance(context.normalized_key, str):
        return False
    if context.normalized_key != " ".join(
        unicodedata.normalize("NFC", context.text).casefold().split()
    ).rstrip(" ."):
        return False
    restored = html.unescape(context.text)
    for escaped, raw in ((r"\[", "["), (r"\]", "]"), (r"\(", "("), (r"\)", ")")):
        restored = restored.replace(escaped, raw)
    if restored.startswith((r"\#", r"\>")):
        restored = restored[1:]
    sanitized = sanitize_leaf(
        item_id=item_id,
        section=LedgerSection.CONTEXT,
        text=restored,
    )
    return sanitized.leaf == context


@dataclass(frozen=True, slots=True)
class SynthesisCandidate:
    topic_id: str
    stage_digests: tuple[str, ...]
    purpose: str

    @classmethod
    def create(
        cls,
        *,
        topic_id: str,
        stage_digests: tuple[str, ...],
        purpose: str,
    ) -> SynthesisCandidate:
        normalized_topic = _identifier(topic_id, field="synthesis topic ID")
        normalized_purpose = _bounded_text(
            purpose, field="synthesis purpose", max_bytes=128
        )
        if (
            not isinstance(stage_digests, tuple)
            or not stage_digests
            or len(stage_digests) > _MAX_SOURCES
        ):
            raise ValueError("invalid synthesis candidate")
        normalized_digests = tuple(
            _digest(value, field="synthesis stage digest") for value in stage_digests
        )
        if len(set(normalized_digests)) != len(normalized_digests):
            raise ValueError("invalid synthesis candidate")
        return cls(
            topic_id=normalized_topic,
            stage_digests=normalized_digests,
            purpose=normalized_purpose,
        )


@dataclass(frozen=True, slots=True)
class SynthesisSource:
    """One resolver-issued citation bound to transcript-free staged context."""

    source_id: str
    topic_id: str
    stage_digest_sha256: str
    citation: TrustedCitation
    context: SanitizedLeaf

    @classmethod
    def create(cls, *, stage: LedgerStage, citation: TrustedCitation) -> SynthesisSource:
        if not isinstance(stage, LedgerStage) or not isinstance(citation, TrustedCitation):
            raise ValueError("invalid synthesis source")
        stage.validate()
        if stage.binding.topic_id is None:
            raise ValueError("invalid synthesis source")
        sanitized = sanitize_leaf(
            item_id=stage.stage_digest_sha256,
            section=LedgerSection.CONTEXT,
            text=stage.staged_text,
        )
        if sanitized.leaf is None:
            raise ValueError("invalid synthesis source")
        source = cls(
            source_id=citation.citation_id,
            topic_id=_identifier(stage.binding.topic_id, field="synthesis topic ID"),
            stage_digest_sha256=_digest(stage.stage_digest_sha256, field="synthesis stage digest"),
            citation=citation,
            context=sanitized.leaf,
        )
        source.validate()
        return source

    def validate(self) -> None:
        if (
            self.source_id != self.citation.citation_id
            or not isinstance(self.context, SanitizedLeaf)
            or not self.context.text
            or "\n" in self.context.text
            or len(self.context.text.encode("utf-8")) > _MAX_CONTEXT_BYTES
            or not _is_sanitized_context(
                item_id=self.stage_digest_sha256,
                context=self.context,
            )
        ):
            raise ValueError("invalid synthesis source")
        _identifier(self.source_id, field="synthesis source ID")
        _identifier(self.topic_id, field="synthesis topic ID")
        _digest(self.stage_digest_sha256, field="synthesis stage digest")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "citation": {
                "citation_id": self.citation.citation_id,
                "destination": self.citation.destination,
            },
            "context": self.context.text,
            "source_id": self.source_id,
            "stage_digest_sha256": self.stage_digest_sha256,
            "topic_id": self.topic_id,
        }


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    request_id: str
    topic_id: str
    sources: tuple[SynthesisSource, ...]
    purpose: str
    timeout_seconds: float
    max_output_bytes: int

    @classmethod
    def create(
        cls,
        *,
        topic_id: str,
        sources: tuple[SynthesisSource, ...],
        purpose: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> SynthesisRequest:
        normalized_topic = _identifier(topic_id, field="synthesis topic ID")
        normalized_purpose = _bounded_text(purpose, field="synthesis purpose", max_bytes=128)
        if (
            not isinstance(timeout_seconds, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 300
            or not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes < 1
            or max_output_bytes > 1_000_000
        ):
            raise ValueError("invalid synthesis bounds")
        if not isinstance(sources, tuple) or not 3 <= len(sources) <= _MAX_SOURCES:
            raise ValueError("synthesis requires three trusted sources")
        if any(not isinstance(source, SynthesisSource) for source in sources):
            raise ValueError("invalid synthesis sources")
        ordered = tuple(sorted(sources, key=lambda source: source.source_id))
        for source in ordered:
            source.validate()
            if source.topic_id != normalized_topic:
                raise ValueError("synthesis source topic mismatch")
        if (
            len({source.source_id for source in ordered}) != len(ordered)
            or len({source.citation.destination for source in ordered}) != len(ordered)
            or len({source.stage_digest_sha256 for source in ordered}) != len(ordered)
        ):
            raise ValueError("synthesis requires distinct trusted sources")
        identity = {
            "max_output_bytes": max_output_bytes,
            "purpose": normalized_purpose,
            "sources": [source.to_dict() for source in ordered],
            "timeout_seconds": timeout_seconds,
            "topic_id": normalized_topic,
        }
        request_id = "synthesis_" + _sha256(canonical_json_bytes(identity))
        request = cls(
            request_id=request_id,
            topic_id=normalized_topic,
            sources=ordered,
            purpose=normalized_purpose,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        return request

    def validate(self) -> None:
        recreated = SynthesisRequest.create(
            topic_id=self.topic_id,
            sources=self.sources,
            purpose=self.purpose,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )
        if recreated != self:
            raise ValueError("synthesis request binding mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_output_bytes": self.max_output_bytes,
            "purpose": self.purpose,
            "request_id": self.request_id,
            "sources": [source.to_dict() for source in self.sources],
            "timeout_seconds": self.timeout_seconds,
            "topic_id": self.topic_id,
        }

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(self.to_dict())

    def to_model_request(self) -> TextModelRequest:
        return TextModelRequest.create(
            request_id=self.request_id,
            purpose=self.purpose,
            prompt=self.canonical_bytes().decode("utf-8"),
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> SynthesisRequest:
        value = _decode_mapping(payload)
        _exact_keys(
            value,
            {
                "max_output_bytes",
                "purpose",
                "request_id",
                "sources",
                "timeout_seconds",
                "topic_id",
            },
        )
        raw_sources = value["sources"]
        if not isinstance(raw_sources, list):
            raise ValueError("invalid synthesis request")
        sources = tuple(_source_from_dict(_mapping(item)) for item in raw_sources)
        request = cls.create(
            topic_id=_string(value["topic_id"]),
            sources=sources,
            purpose=_string(value["purpose"]),
            timeout_seconds=_float(value["timeout_seconds"]),
            max_output_bytes=_integer(value["max_output_bytes"]),
        )
        if request.request_id != value["request_id"] or request.canonical_bytes() != payload:
            raise ValueError("non-canonical synthesis request")
        return request


@dataclass(frozen=True, slots=True)
class SynthesisClaim:
    text: str
    confidence: Confidence
    source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "confidence": self.confidence.value,
            "source_ids": list(self.source_ids),
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    request_id: str
    claims: tuple[SynthesisClaim, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "claims": [claim.to_dict() for claim in self.claims],
            "request_id": self.request_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def validate_for(self, request: SynthesisRequest) -> None:
        request.validate()
        if (
            self.request_id != request.request_id
            or not isinstance(self.claims, tuple)
            or not 1 <= len(self.claims) <= _MAX_CLAIMS
        ):
            raise ValueError("invalid synthesis result")
        allowed = {source.source_id for source in request.sources}
        normalized_claims: set[str] = set()
        used_sources: set[str] = set()
        for claim in self.claims:
            if (
                not isinstance(claim, SynthesisClaim)
                or not isinstance(claim.confidence, Confidence)
                or not isinstance(claim.source_ids, tuple)
                or not 1 <= len(claim.source_ids) <= _MAX_CLAIM_SOURCES
            ):
                raise ValueError("invalid synthesis result")
            _bounded_text(
                claim.text,
                field="synthesis claim",
                max_bytes=_MAX_CLAIM_BYTES,
            )
            source_ids = tuple(
                _identifier(source_id, field="synthesis claim source ID")
                for source_id in claim.source_ids
            )
            if source_ids != tuple(sorted(set(source_ids))) or not set(source_ids) <= allowed:
                raise ValueError("invalid synthesis result")
            normalized_key = " ".join(
                unicodedata.normalize("NFC", claim.text).casefold().split()
            ).rstrip(" .")
            leaf = SanitizedLeaf(text=claim.text, normalized_key=normalized_key)
            if leaf.normalized_key in normalized_claims:
                raise ValueError("invalid synthesis result")
            normalized_claims.add(leaf.normalized_key)
            used_sources.update(source_ids)
        if len(used_sources) < 3:
            raise ValueError("invalid synthesis result")

    @classmethod
    def parse(cls, *, text: str, request: SynthesisRequest) -> SynthesisResult:
        try:
            raw = text.encode("utf-8")
        except (AttributeError, UnicodeError):
            raise _CodecFailure(SynthesisError.MALFORMED_RESULT) from None
        if len(raw) > request.max_output_bytes:
            raise _CodecFailure(SynthesisError.OUTPUT_LIMIT)
        try:
            value = _decode_mapping(raw)
            if canonical_json_bytes(value) != raw:
                raise ValueError
            _exact_keys(value, {"claims", "request_id"})
            if value["request_id"] != request.request_id:
                raise ValueError
            raw_claims = value["claims"]
            if not isinstance(raw_claims, list) or not 1 <= len(raw_claims) <= _MAX_CLAIMS:
                raise ValueError
            allowed = {source.source_id for source in request.sources}
            claims: list[SynthesisClaim] = []
            normalized_claims: set[str] = set()
            used_sources: set[str] = set()
            for raw_claim in raw_claims:
                claim_value = _mapping(raw_claim)
                _exact_keys(claim_value, {"confidence", "source_ids", "text"})
                claim_text = _bounded_text(
                    claim_value["text"],
                    field="synthesis claim",
                    max_bytes=_MAX_CLAIM_BYTES,
                )
                try:
                    confidence = Confidence(_string(claim_value["confidence"]))
                except ValueError:
                    raise _CodecFailure(SynthesisError.MALFORMED_RESULT) from None
                raw_source_ids = claim_value["source_ids"]
                if (
                    not isinstance(raw_source_ids, list)
                    or not 1 <= len(raw_source_ids) <= _MAX_CLAIM_SOURCES
                ):
                    raise ValueError
                source_ids = tuple(
                    _identifier(item, field="synthesis claim source ID") for item in raw_source_ids
                )
                if source_ids != tuple(sorted(set(source_ids))) or not set(source_ids) <= allowed:
                    raise ValueError
                sanitized = sanitize_leaf(
                    item_id=request.request_id,
                    section=LedgerSection.SUMMARY,
                    text=claim_text,
                )
                if sanitized.leaf is None:
                    raise _CodecFailure(SynthesisError.QUARANTINED_RESULT)
                if sanitized.leaf.normalized_key in normalized_claims:
                    raise ValueError
                normalized_claims.add(sanitized.leaf.normalized_key)
                used_sources.update(source_ids)
                claims.append(
                    SynthesisClaim(
                        text=sanitized.leaf.text,
                        confidence=confidence,
                        source_ids=source_ids,
                    )
                )
            if len(used_sources) < 3:
                raise _CodecFailure(SynthesisError.SOURCE_GATE)
            result = cls(request_id=request.request_id, claims=tuple(claims))
            result.validate_for(request)
            return result
        except _CodecFailure:
            raise
        except (TypeError, ValueError, UnicodeError):
            raise _CodecFailure(SynthesisError.MALFORMED_RESULT) from None


@dataclass(frozen=True, slots=True)
class PreparedSynthesis:
    state: str
    request: SynthesisRequest
    result: SynthesisResult
    link_back_source_ids: tuple[str, ...]

    def validate(self) -> None:
        self.request.validate()
        self.result.validate_for(self.request)
        expected_links = tuple(
            sorted({source_id for claim in self.result.claims for source_id in claim.source_ids})
        )
        if (
            self.state != "evaluating"
            or self.link_back_source_ids != expected_links
            or len(self.link_back_source_ids) < 3
        ):
            raise ValueError("invalid prepared synthesis")


@dataclass(frozen=True, slots=True)
class SynthesisOutcome:
    prepared: PreparedSynthesis | None
    error: SynthesisError | None
    attempts: int


class LockProbe(Protocol):
    def __call__(self) -> bool: ...


class SynthesisPublicationStore(Protocol):
    def published_document_set(
        self,
        stage_digest_sha256: str,
    ) -> PublishedDocumentSet | None: ...

    def slim_state(
        self,
        identity: LedgerRowIdentity,
    ) -> DurableSlimState | None: ...


@dataclass(frozen=True, slots=True)
class PersistedSynthesisSourceResolver:
    """Resolve only configured stage/citation bindings visible in durable ledger state."""

    publication_store: SynthesisPublicationStore
    bindings: Mapping[str, tuple[LedgerStage, TrustedCitation]]

    def __post_init__(self) -> None:
        if not isinstance(self.bindings, Mapping):
            raise ValueError("invalid synthesis source bindings")
        copied: dict[str, tuple[LedgerStage, TrustedCitation]] = {}
        for stage_digest, binding in self.bindings.items():
            if (
                not isinstance(binding, tuple)
                or len(binding) != 2
                or not isinstance(binding[0], LedgerStage)
                or not isinstance(binding[1], TrustedCitation)
            ):
                raise ValueError("invalid synthesis source bindings")
            stage, citation = binding
            stage.validate()
            citation.validate()
            if stage_digest != stage.stage_digest_sha256:
                raise ValueError("invalid synthesis source bindings")
            copied[stage_digest] = (stage, citation)
        object.__setattr__(self, "bindings", MappingProxyType(copied))

    def create_request(
        self,
        *,
        topic_id: str,
        stage_digests: tuple[str, ...],
        purpose: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> SynthesisRequest:
        return SynthesisRequest.create(
            topic_id=topic_id,
            sources=self.resolve_sources(
                topic_id=topic_id,
                stage_digests=stage_digests,
            ),
            purpose=purpose,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

    def resolve_sources(
        self,
        *,
        topic_id: str,
        stage_digests: tuple[str, ...],
    ) -> tuple[SynthesisSource, ...]:
        normalized_topic = _identifier(topic_id, field="synthesis topic ID")
        if (
            not isinstance(stage_digests, tuple)
            or not 3 <= len(stage_digests) <= _MAX_SOURCES
            or len(set(stage_digests)) != len(stage_digests)
        ):
            raise ValueError("synthesis requires three persisted synthesis sources")
        sources: list[SynthesisSource] = []
        for stage_digest in stage_digests:
            binding = self.bindings.get(_digest(stage_digest, field="synthesis stage digest"))
            if binding is None:
                raise ValueError("persisted synthesis source unavailable")
            stage, citation = binding
            source = SynthesisSource.create(stage=stage, citation=citation)
            published = self.publication_store.published_document_set(stage_digest)
            if published is None:
                raise ValueError("persisted synthesis source unavailable")
            durable = self.publication_store.slim_state(published.row_identity)
            persisted_citation_id = (
                published.citation_ids[0] if len(published.citation_ids) == 1 else ""
            )
            capture_document_id = "capture_ref_" + persisted_citation_id
            expected_destination = markdown_relative_path(capture_document_id).as_posix()
            if (
                durable is None
                or durable.source_id != str(stage.binding.capture_id)
                or published.citation_ids != (citation.citation_id,)
                or durable.citation_ids != (citation.citation_id,)
                or not published.document_ids
                or published.document_ids[0] != capture_document_id
                or citation.destination != expected_destination
                or source.topic_id != normalized_topic
            ):
                raise ValueError("persisted synthesis source mismatch")
            sources.append(source)
        if len({source.source_id for source in sources}) != len(sources):
            raise ValueError("synthesis requires three persisted synthesis sources")
        return tuple(sources)

    def authorizes(
        self,
        *,
        request: SynthesisRequest,
        privacy: PrivacyDecision,
    ) -> bool:
        try:
            resolved = self.resolve_sources(
                topic_id=request.topic_id,
                stage_digests=tuple(source.stage_digest_sha256 for source in request.sources),
            )
            if resolved != request.sources:
                return False
            return all(
                self.bindings[source.stage_digest_sha256][0].binding.privacy_decision == privacy
                for source in request.sources
            )
        except Exception:
            return False


def prepare_synthesis_batch(
    *,
    resolver: PersistedSynthesisSourceResolver,
    candidates: tuple[SynthesisCandidate, ...],
    cap: int,
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[SynthesisRequest, ...]:
    """Apply a deterministic cap before preparing resolver-bound requests."""
    if (
        not isinstance(resolver, PersistedSynthesisSourceResolver)
        or not isinstance(candidates, tuple)
        or any(not isinstance(candidate, SynthesisCandidate) for candidate in candidates)
        or not isinstance(cap, int)
        or isinstance(cap, bool)
        or not 1 <= cap <= 100
    ):
        raise ValueError("invalid synthesis preparation batch")
    ordered = tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.topic_id,
                candidate.purpose,
                candidate.stage_digests,
            ),
        )
    )
    return tuple(
        resolver.create_request(
            topic_id=candidate.topic_id,
            stage_digests=candidate.stage_digests,
            purpose=candidate.purpose,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        for candidate in ordered[:cap]
    )


class SynthesisPersistence(Protocol):
    def persist(
        self,
        prepared: PreparedSynthesis,
        *,
        privacy: PrivacyDecision,
    ) -> DurableSynthesisRecord: ...

    def get(self, request_id: str) -> DurableSynthesisRecord | None: ...


class SynthesisSourceAuthority(Protocol):
    def authorizes(
        self,
        *,
        request: SynthesisRequest,
        privacy: PrivacyDecision,
    ) -> bool: ...


class SynthesisService:
    def __init__(
        self,
        *,
        provider: Provider,
        source_resolver: SynthesisSourceAuthority,
        store: SynthesisPersistence,
        lock_probes: tuple[Callable[[], bool], ...],
    ) -> None:
        from .synthesis_store import SqliteSynthesisStore

        if type(store) is not SqliteSynthesisStore:
            raise ValueError("durable synthesis store required")
        if (
            not isinstance(lock_probes, tuple)
            or not lock_probes
            or any(not callable(probe) for probe in lock_probes)
        ):
            raise ValueError("authoritative lock probe required")
        self._provider = provider
        self._source_resolver = source_resolver
        self._store = store
        self._lock_probes = lock_probes

    def apply(
        self, *, request: SynthesisRequest, privacy: PrivacyDecision
    ) -> SynthesisOutcome:
        return self.run(request=request, privacy=privacy)

    def run(self, *, request: SynthesisRequest, privacy: PrivacyDecision) -> SynthesisOutcome:
        if not isinstance(request, SynthesisRequest) or not isinstance(privacy, PrivacyDecision):
            return SynthesisOutcome(None, SynthesisError.INVALID_REQUEST, 0)
        try:
            request.validate()
        except (TypeError, ValueError):
            return SynthesisOutcome(None, SynthesisError.INVALID_REQUEST, 0)
        try:
            if any(probe() for probe in self._lock_probes):
                return SynthesisOutcome(None, SynthesisError.LOCK_HELD, 0)
        except Exception:
            return SynthesisOutcome(None, SynthesisError.LOCK_HELD, 0)
        if not self._source_resolver.authorizes(request=request, privacy=privacy):
            return SynthesisOutcome(None, SynthesisError.SOURCE_GATE, 0)
        try:
            if any(probe() for probe in self._lock_probes):
                return SynthesisOutcome(None, SynthesisError.LOCK_HELD, 0)
        except Exception:
            return SynthesisOutcome(None, SynthesisError.LOCK_HELD, 0)
        model_request = request.to_model_request()
        try:
            model_result = self._provider.complete(model_request, privacy=privacy)
        except Exception:
            return SynthesisOutcome(None, SynthesisError.PROVIDER_FAILURE, 1)
        if not isinstance(model_result, TextModelResult):
            return SynthesisOutcome(None, SynthesisError.PROVIDER_FAILURE, 1)
        try:
            verified_result = model_result.validate_for(model_request)
        except ValidationError:
            return SynthesisOutcome(None, SynthesisError.OUTPUT_LIMIT, 1)
        try:
            result = SynthesisResult.parse(text=verified_result.text, request=request)
        except _CodecFailure as error:
            return SynthesisOutcome(None, error.code, 1)
        link_backs = tuple(
            sorted({source_id for claim in result.claims for source_id in claim.source_ids})
        )
        prepared = PreparedSynthesis(
            state="evaluating",
            request=request,
            result=result,
            link_back_source_ids=link_backs,
        )
        try:
            from .synthesis_store import DurableSynthesisRecord

            durable = self._store.persist(prepared, privacy=privacy)
            if type(durable) is not DurableSynthesisRecord:
                raise ValueError("invalid synthesis durable proof")
            durable.validate_for(prepared, privacy=privacy)
            read_back = self._store.get(request.request_id)
            if type(read_back) is not DurableSynthesisRecord or read_back != durable:
                raise ValueError("invalid synthesis durable read-back")
            read_back.validate_for(prepared, privacy=privacy)
        except Exception:
            return SynthesisOutcome(None, SynthesisError.PERSISTENCE_FAILURE, 1)
        return SynthesisOutcome(prepared, None, 1)


def _sha256(value: bytes) -> str:
    from hashlib import sha256

    return sha256(value).hexdigest()


def _decode_mapping(payload: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid synthesis JSON") from None
    return _mapping(value)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate synthesis field")
        value[key] = item
    return value


def _exact_keys(value: Mapping[str, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError("invalid synthesis fields")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("invalid synthesis object")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid synthesis string")
    return value


def _float(value: object) -> float:
    if not isinstance(value, float):
        raise ValueError("invalid synthesis float")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid synthesis integer")
    return value


def _source_from_dict(value: Mapping[str, object]) -> SynthesisSource:
    _exact_keys(
        value,
        {"citation", "context", "source_id", "stage_digest_sha256", "topic_id"},
    )
    citation_value = _mapping(value["citation"])
    _exact_keys(citation_value, {"citation_id", "destination"})
    citation = TrustedCitation.create(
        citation_id=_string(citation_value["citation_id"]),
        destination=_string(citation_value["destination"]),
    )
    context_text = _string(value["context"])
    sanitized = sanitize_leaf(
        item_id=_string(value["stage_digest_sha256"]),
        section=LedgerSection.CONTEXT,
        text=context_text,
    )
    if sanitized.leaf is None or sanitized.leaf.text != context_text:
        raise ValueError("invalid synthesis context")
    source = SynthesisSource(
        source_id=_string(value["source_id"]),
        topic_id=_string(value["topic_id"]),
        stage_digest_sha256=_string(value["stage_digest_sha256"]),
        citation=citation,
        context=sanitized.leaf,
    )
    source.validate()
    return source
