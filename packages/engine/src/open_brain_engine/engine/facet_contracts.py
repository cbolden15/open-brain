"""Immutable draft C0 facet values and authority-explicit engine boundaries.

No callable here implements persistence, publication, vault I/O, or registration.
Publication preserves the existing narrow review receipt fields plus dto_version;
its effective_idempotency_key carries the effective operation ID.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from hashlib import sha256
from types import MappingProxyType
from typing import Any, ClassVar, NewType, Protocol, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .facet_validation import CONTRACT, parse, validate
from .t03_contracts import EffectiveAuthority

FacetDimensionId = NewType("FacetDimensionId", str)
FacetValueId = NewType("FacetValueId", str)
FacetQueryId = NewType("FacetQueryId", str)
FacetEventId = NewType("FacetEventId", str)

MAX_REQUEST_BYTES = 65_536
MAX_PUBLICATION_REQUEST_BYTES = 1_048_576
MAX_LIST_RESPONSE_BYTES = 250_000
MAX_READ_RESPONSE_BYTES = 1_048_576
MAX_RECEIPT_RESPONSE_BYTES = 4_096
PUBLICATION_OPERATION = "review.propose_from_facet_query"


class FacetError(ValueError):
    """One closed public failure, without private exception details."""

    def __init__(self, code: str) -> None:
        if code not in CONTRACT["definitions"]["error"]["properties"]["code"]["enum"]:
            raise ValueError("invalid error code")
        self.code = code
        super().__init__(code)

    def to_wire(self) -> dict[str, object]:
        return {"status": "error", "dto_version": 1, "code": self.code}


def validate_wire(schema: str, value: object) -> Any:
    try:
        return validate(schema, value)
    except ValueError, TypeError, KeyError, UnicodeError, RecursionError:
        raise FacetError("invalid_arguments") from None


def parse_wire(raw: str | bytes, *, operation: str | None = None) -> Any:
    """Default to the ordinary bound; explicitly admit the larger review request."""
    try:
        maximum = (
            MAX_PUBLICATION_REQUEST_BYTES
            if operation == PUBLICATION_OPERATION
            else MAX_REQUEST_BYTES
        )
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > maximum:
            raise ValueError("request too large")
        return parse(raw)
    except ValueError, TypeError, UnicodeError, RecursionError, AttributeError:
        raise FacetError("invalid_arguments") from None


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, FacetSelector | FacetSubject | FacetHistorySubject):
        return value.to_wire()
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(child) for child in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class FacetSelector:
    all_value_ids: tuple[str, ...] | list[str]
    space_ids: tuple[str, ...] | list[str] = ()
    payload_families: tuple[str, ...] | list[str] = ()
    record_types: tuple[str, ...] | list[str] = ("source", "canonical")

    def __post_init__(self) -> None:
        value = validate_wire("selector.input", self.to_wire())
        for item in fields(self):
            object.__setattr__(self, item.name, _freeze(value[item.name]))

    def to_wire(self) -> dict[str, Any]:
        return {item.name: _thaw(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class FacetSubject:
    kind: str
    id: str

    def __post_init__(self) -> None:
        validate_wire("subject", self.to_wire())

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True, slots=True)
class FacetHistorySubject(FacetSubject):
    def __post_init__(self) -> None:
        validate_wire("history_subject", self.to_wire())


@dataclass(frozen=True, slots=True, kw_only=True)
class FacetRequest:
    dto_version: int = 1
    operation: ClassVar[str]

    def __post_init__(self) -> None:
        raw = self.to_wire()
        value = validate_wire(self.operation + ".request", raw)
        _check_request_size(self.operation, raw)
        for item in fields(self):
            if item.name in value:
                object.__setattr__(self, item.name, _freeze(value[item.name]))

    def to_wire(self) -> dict[str, Any]:
        return {item.name: _thaw(getattr(self, item.name)) for item in fields(self)}

    def request_sha256(self) -> str:
        return semantic_request_sha256(self.operation, self)


@dataclass(frozen=True, slots=True)
class FacetResponse:
    """Validate immutable canonical JSON; each read returns detached caller data."""

    encoded: str
    operation: ClassVar[str]

    def __post_init__(self) -> None:
        try:
            value = parse(self.encoded)
        except ValueError, TypeError, UnicodeError, RecursionError, AttributeError:
            raise FacetError("invalid_arguments") from None
        value = validate_wire(self.operation + ".response", value)
        encoded = portable_canonical_json_bytes(value).decode("utf-8")
        if len(encoded.encode("utf-8")) > RESPONSE_BYTE_LIMITS[self.operation]:
            raise FacetError("response_too_large")
        object.__setattr__(self, "encoded", encoded)

    @classmethod
    def from_wire(cls, value: object) -> FacetResponse:
        validated = validate_wire(cls.operation + ".response", value)
        return cls(portable_canonical_json_bytes(validated).decode("utf-8"))

    def to_wire(self) -> dict[str, object]:
        return cast(dict[str, object], parse(self.encoded))


@dataclass(frozen=True, slots=True, kw_only=True)
class DimensionsListRequest(FacetRequest):
    operation: ClassVar[str] = "facet.dimension.list"
    limit: int = 50
    cursor: str | None = None
    include_archived: bool = False


@dataclass(frozen=True, slots=True)
class DimensionsListResponse(FacetResponse):
    operation = "facet.dimension.list"


@dataclass(frozen=True, slots=True, kw_only=True)
class DimensionCreateRequest(FacetRequest):
    operation: ClassVar[str] = "facet.dimension.create"
    operation_id: str
    expected_version: int
    name: str


@dataclass(frozen=True, slots=True)
class DimensionCreateResponse(FacetResponse):
    operation = "facet.dimension.create"


@dataclass(frozen=True, slots=True, kw_only=True)
class DimensionRenameRequest(FacetRequest):
    operation: ClassVar[str] = "facet.dimension.rename"
    operation_id: str
    expected_version: int
    dimension_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DimensionRenameResponse(FacetResponse):
    operation = "facet.dimension.rename"


@dataclass(frozen=True, slots=True, kw_only=True)
class DimensionArchiveRequest(FacetRequest):
    operation: ClassVar[str] = "facet.dimension.archive"
    operation_id: str
    expected_version: int
    dimension_id: str


@dataclass(frozen=True, slots=True)
class DimensionArchiveResponse(FacetResponse):
    operation = "facet.dimension.archive"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValuesListRequest(FacetRequest):
    operation: ClassVar[str] = "facet.value.list"
    limit: int = 50
    cursor: str | None = None
    include_archived: bool = False
    dimension_id: str


@dataclass(frozen=True, slots=True)
class ValuesListResponse(FacetResponse):
    operation = "facet.value.list"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueCreateRequest(FacetRequest):
    operation: ClassVar[str] = "facet.value.create"
    operation_id: str
    expected_version: int
    dimension_id: str
    expected_dimension_version: int
    name: str


@dataclass(frozen=True, slots=True)
class ValueCreateResponse(FacetResponse):
    operation = "facet.value.create"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueRenameRequest(FacetRequest):
    operation: ClassVar[str] = "facet.value.rename"
    operation_id: str
    expected_version: int
    value_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ValueRenameResponse(FacetResponse):
    operation = "facet.value.rename"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueArchiveRequest(FacetRequest):
    operation: ClassVar[str] = "facet.value.archive"
    operation_id: str
    expected_version: int
    value_id: str


@dataclass(frozen=True, slots=True)
class ValueArchiveResponse(FacetResponse):
    operation = "facet.value.archive"


@dataclass(frozen=True, slots=True, kw_only=True)
class AssignmentsGetRequest(FacetRequest):
    operation: ClassVar[str] = "facet.assignment.get"
    record_id: str


@dataclass(frozen=True, slots=True)
class AssignmentsGetResponse(FacetResponse):
    operation = "facet.assignment.get"


@dataclass(frozen=True, slots=True, kw_only=True)
class AssignmentsPatchRequest(FacetRequest):
    operation: ClassVar[str] = "facet.assignment.patch"
    operation_id: str
    expected_version: int
    record_id: str
    expected_revision_id: str
    add_value_ids: tuple[str, ...] | list[str] = ()
    remove_value_ids: tuple[str, ...] | list[str] = ()


@dataclass(frozen=True, slots=True)
class AssignmentsPatchResponse(FacetResponse):
    operation = "facet.assignment.patch"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueriesListRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.list"
    limit: int = 50
    cursor: str | None = None
    include_archived: bool = False


@dataclass(frozen=True, slots=True)
class QueriesListResponse(FacetResponse):
    operation = "facet.query.list"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryGetRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.get"
    query_id: str


@dataclass(frozen=True, slots=True)
class QueryGetResponse(FacetResponse):
    operation = "facet.query.get"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryCreateRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.create"
    operation_id: str
    expected_version: int
    name: str
    selector: Mapping[str, Any] | FacetSelector


@dataclass(frozen=True, slots=True)
class QueryCreateResponse(FacetResponse):
    operation = "facet.query.create"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryUpdateRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.update"
    operation_id: str
    expected_version: int
    query_id: str
    name: str
    selector: Mapping[str, Any] | FacetSelector


@dataclass(frozen=True, slots=True)
class QueryUpdateResponse(FacetResponse):
    operation = "facet.query.update"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryArchiveRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.archive"
    operation_id: str
    expected_version: int
    query_id: str


@dataclass(frozen=True, slots=True)
class QueryArchiveResponse(FacetResponse):
    operation = "facet.query.archive"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryAttachRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.attach"
    operation_id: str
    expected_version: int
    page_id: str
    expected_revision_id: str
    query_id: str
    expected_query_version: int


@dataclass(frozen=True, slots=True)
class QueryAttachResponse(FacetResponse):
    operation = "facet.query.attach"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryAttachmentGetRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.attachment.get"
    page_id: str


@dataclass(frozen=True, slots=True)
class QueryAttachmentGetResponse(FacetResponse):
    operation = "facet.query.attachment.get"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryPageRequest(FacetRequest):
    operation: ClassVar[str] = "facet.query.page"
    limit: int = 50
    cursor: str | None = None
    selector: Mapping[str, Any] | FacetSelector | None = None
    query_id: str | None = None
    expected_query_version: int | None = None

    def to_wire(self) -> dict[str, Any]:
        value = super(QueryPageRequest, self).to_wire()
        for key in ("selector", "query_id", "expected_query_version"):
            if value[key] is None:
                del value[key]
        return value


@dataclass(frozen=True, slots=True)
class QueryPageResponse(FacetResponse):
    operation = "facet.query.page"


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryListRequest(FacetRequest):
    operation: ClassVar[str] = "facet.history.list"
    limit: int = 50
    cursor: str | None = None
    subject: Mapping[str, str] | FacetHistorySubject
    as_of_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryListResponse(FacetResponse):
    operation = "facet.history.list"


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryStateRequest(FacetRequest):
    operation: ClassVar[str] = "facet.history.state"
    subject: Mapping[str, str] | FacetHistorySubject
    as_of_sequence: int


@dataclass(frozen=True, slots=True)
class HistoryStateResponse(FacetResponse):
    operation = "facet.history.state"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeFromFacetQueryRequest(FacetRequest):
    operation: ClassVar[str] = "review.propose_from_facet_query"
    operation_id: str
    query_id: str
    expected_query_version: int
    target_page_id: str | None
    expected_page_revision_id: str | None
    destination_space_id: str | None
    title: str
    markdown: str


@dataclass(frozen=True, slots=True)
class ProposeFromFacetQueryResponse(FacetResponse):
    operation = "review.propose_from_facet_query"


@dataclass(frozen=True, slots=True, kw_only=True)
class VaultPreviewRequest(FacetRequest):
    operation: ClassVar[str] = "facet.vault.preview"
    workspace_id: str
    record_id: str


@dataclass(frozen=True, slots=True)
class VaultPreviewResponse(FacetResponse):
    operation = "facet.vault.preview"


@dataclass(frozen=True, slots=True, kw_only=True)
class VaultApplyRequest(FacetRequest):
    operation: ClassVar[str] = "facet.vault.apply"
    preview_id: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class VaultApplyResponse(FacetResponse):
    operation = "facet.vault.apply"


REQUEST_TYPES: Mapping[str, type[FacetRequest]] = MappingProxyType(
    {
        "facet.dimension.list": DimensionsListRequest,
        "facet.dimension.create": DimensionCreateRequest,
        "facet.dimension.rename": DimensionRenameRequest,
        "facet.dimension.archive": DimensionArchiveRequest,
        "facet.value.list": ValuesListRequest,
        "facet.value.create": ValueCreateRequest,
        "facet.value.rename": ValueRenameRequest,
        "facet.value.archive": ValueArchiveRequest,
        "facet.assignment.get": AssignmentsGetRequest,
        "facet.assignment.patch": AssignmentsPatchRequest,
        "facet.query.list": QueriesListRequest,
        "facet.query.get": QueryGetRequest,
        "facet.query.create": QueryCreateRequest,
        "facet.query.update": QueryUpdateRequest,
        "facet.query.archive": QueryArchiveRequest,
        "facet.query.attach": QueryAttachRequest,
        "facet.query.attachment.get": QueryAttachmentGetRequest,
        "facet.query.page": QueryPageRequest,
        "facet.history.list": HistoryListRequest,
        "facet.history.state": HistoryStateRequest,
        "review.propose_from_facet_query": ProposeFromFacetQueryRequest,
        "facet.vault.preview": VaultPreviewRequest,
        "facet.vault.apply": VaultApplyRequest,
    }
)

RESPONSE_TYPES: Mapping[str, type[FacetResponse]] = MappingProxyType(
    {
        "facet.dimension.list": DimensionsListResponse,
        "facet.dimension.create": DimensionCreateResponse,
        "facet.dimension.rename": DimensionRenameResponse,
        "facet.dimension.archive": DimensionArchiveResponse,
        "facet.value.list": ValuesListResponse,
        "facet.value.create": ValueCreateResponse,
        "facet.value.rename": ValueRenameResponse,
        "facet.value.archive": ValueArchiveResponse,
        "facet.assignment.get": AssignmentsGetResponse,
        "facet.assignment.patch": AssignmentsPatchResponse,
        "facet.query.list": QueriesListResponse,
        "facet.query.get": QueryGetResponse,
        "facet.query.create": QueryCreateResponse,
        "facet.query.update": QueryUpdateResponse,
        "facet.query.archive": QueryArchiveResponse,
        "facet.query.attach": QueryAttachResponse,
        "facet.query.attachment.get": QueryAttachmentGetResponse,
        "facet.query.page": QueryPageResponse,
        "facet.history.list": HistoryListResponse,
        "facet.history.state": HistoryStateResponse,
        "review.propose_from_facet_query": ProposeFromFacetQueryResponse,
        "facet.vault.preview": VaultPreviewResponse,
        "facet.vault.apply": VaultApplyResponse,
    }
)

OPERATION_METHODS: Mapping[str, str] = MappingProxyType(
    {
        "facet.dimension.list": "dimensions_list",
        "facet.dimension.create": "dimension_create",
        "facet.dimension.rename": "dimension_rename",
        "facet.dimension.archive": "dimension_archive",
        "facet.value.list": "values_list",
        "facet.value.create": "value_create",
        "facet.value.rename": "value_rename",
        "facet.value.archive": "value_archive",
        "facet.assignment.get": "assignments_get",
        "facet.assignment.patch": "assignments_patch",
        "facet.query.list": "queries_list",
        "facet.query.get": "query_get",
        "facet.query.create": "query_create",
        "facet.query.update": "query_update",
        "facet.query.archive": "query_archive",
        "facet.query.attach": "query_attach",
        "facet.query.attachment.get": "query_attachment_get",
        "facet.query.page": "query_page",
        "facet.history.list": "history_list",
        "facet.history.state": "history_state",
        "review.propose_from_facet_query": "propose_from_facet_query",
        "facet.vault.preview": "vault_preview",
        "facet.vault.apply": "vault_apply",
    }
)


def _check_request_size(operation: str, value: object) -> None:
    maximum = (
        MAX_PUBLICATION_REQUEST_BYTES if operation == PUBLICATION_OPERATION else MAX_REQUEST_BYTES
    )
    if len(portable_canonical_json_bytes(value)) > maximum:
        raise FacetError("invalid_arguments")


def request_from_wire(operation: str, value: object) -> FacetRequest:
    if isinstance(value, FacetRequest):
        if value.operation != operation:
            raise FacetError("invalid_arguments")
        value = value.to_wire()
    validated = validate_wire(operation + ".request", value)
    _check_request_size(operation, value)
    try:
        return REQUEST_TYPES[operation](**validated)
    except TypeError, KeyError:
        raise FacetError("invalid_arguments") from None


def response_from_wire(operation: str, value: object) -> FacetResponse:
    if isinstance(value, FacetResponse):
        if value.operation != operation:
            raise FacetError("invalid_arguments")
        value = value.to_wire()
    try:
        return RESPONSE_TYPES[operation].from_wire(value)
    except KeyError:
        raise FacetError("invalid_arguments") from None


def response_to_wire(operation: str, value: object) -> dict[str, object]:
    return response_from_wire(operation, value).to_wire()


def semantic_request_value(operation: str, value: object) -> dict[str, object]:
    """Method and full normalized arguments only, without authority or transport state."""
    return {"method": operation, "arguments": request_from_wire(operation, value).to_wire()}


def semantic_request_sha256(operation: str, value: object) -> str:
    return sha256(
        portable_canonical_json_bytes(semantic_request_value(operation, value))
    ).hexdigest()


class FacetTasks(Protocol):
    def dimensions_list(
        self, request: DimensionsListRequest, *, authority: EffectiveAuthority
    ) -> DimensionsListResponse: ...

    def dimension_create(
        self, request: DimensionCreateRequest, *, authority: EffectiveAuthority
    ) -> DimensionCreateResponse: ...

    def dimension_rename(
        self, request: DimensionRenameRequest, *, authority: EffectiveAuthority
    ) -> DimensionRenameResponse: ...

    def dimension_archive(
        self, request: DimensionArchiveRequest, *, authority: EffectiveAuthority
    ) -> DimensionArchiveResponse: ...

    def values_list(
        self, request: ValuesListRequest, *, authority: EffectiveAuthority
    ) -> ValuesListResponse: ...

    def value_create(
        self, request: ValueCreateRequest, *, authority: EffectiveAuthority
    ) -> ValueCreateResponse: ...

    def value_rename(
        self, request: ValueRenameRequest, *, authority: EffectiveAuthority
    ) -> ValueRenameResponse: ...

    def value_archive(
        self, request: ValueArchiveRequest, *, authority: EffectiveAuthority
    ) -> ValueArchiveResponse: ...

    def assignments_get(
        self, request: AssignmentsGetRequest, *, authority: EffectiveAuthority
    ) -> AssignmentsGetResponse: ...

    def assignments_patch(
        self, request: AssignmentsPatchRequest, *, authority: EffectiveAuthority
    ) -> AssignmentsPatchResponse: ...

    def queries_list(
        self, request: QueriesListRequest, *, authority: EffectiveAuthority
    ) -> QueriesListResponse: ...

    def query_get(
        self, request: QueryGetRequest, *, authority: EffectiveAuthority
    ) -> QueryGetResponse: ...

    def query_create(
        self, request: QueryCreateRequest, *, authority: EffectiveAuthority
    ) -> QueryCreateResponse: ...

    def query_update(
        self, request: QueryUpdateRequest, *, authority: EffectiveAuthority
    ) -> QueryUpdateResponse: ...

    def query_archive(
        self, request: QueryArchiveRequest, *, authority: EffectiveAuthority
    ) -> QueryArchiveResponse: ...

    def query_attach(
        self, request: QueryAttachRequest, *, authority: EffectiveAuthority
    ) -> QueryAttachResponse: ...

    def query_attachment_get(
        self, request: QueryAttachmentGetRequest, *, authority: EffectiveAuthority
    ) -> QueryAttachmentGetResponse: ...

    def query_page(
        self, request: QueryPageRequest, *, authority: EffectiveAuthority
    ) -> QueryPageResponse: ...

    def history_list(
        self, request: HistoryListRequest, *, authority: EffectiveAuthority
    ) -> HistoryListResponse: ...

    def history_state(
        self, request: HistoryStateRequest, *, authority: EffectiveAuthority
    ) -> HistoryStateResponse: ...


class FacetPublicationTasks(Protocol):
    def propose_from_facet_query(
        self, request: ProposeFromFacetQueryRequest, *, authority: EffectiveAuthority
    ) -> ProposeFromFacetQueryResponse: ...


class FacetVaultTasks(Protocol):
    def vault_preview(
        self, request: VaultPreviewRequest, *, authority: EffectiveAuthority
    ) -> VaultPreviewResponse: ...

    def vault_apply(
        self, request: VaultApplyRequest, *, authority: EffectiveAuthority
    ) -> VaultApplyResponse: ...


REQUIRED_CAPABILITIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "facet.dimension.list": ("facet-read",),
        "facet.value.list": ("facet-read",),
        "facet.assignment.get": ("facet-read",),
        "facet.query.list": ("facet-read",),
        "facet.query.get": ("facet-read",),
        "facet.query.attachment.get": ("facet-read",),
        "facet.query.page": ("facet-read", "search"),
        "facet.history.list": ("facet-read", "history-read"),
        "facet.history.state": ("facet-read", "history-read"),
        "facet.dimension.create": ("facet-catalog-write",),
        "facet.dimension.rename": ("facet-catalog-write",),
        "facet.dimension.archive": ("facet-catalog-write",),
        "facet.value.create": ("facet-catalog-write",),
        "facet.value.rename": ("facet-catalog-write",),
        "facet.value.archive": ("facet-catalog-write",),
        "facet.query.create": ("facet-query-write",),
        "facet.query.update": ("facet-query-write",),
        "facet.query.archive": ("facet-query-write",),
        "facet.query.attach": ("facet-query-write",),
        "facet.assignment.patch": ("facet-assign",),
        "review.propose_from_facet_query": ("facet-read", "search", "review-propose"),
        "facet.vault.preview": (),
        "facet.vault.apply": (),
    }
)

RESPONSE_BYTE_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "facet.dimension.list": MAX_LIST_RESPONSE_BYTES,
        "facet.dimension.create": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.dimension.rename": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.dimension.archive": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.value.list": MAX_LIST_RESPONSE_BYTES,
        "facet.value.create": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.value.rename": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.value.archive": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.assignment.get": MAX_READ_RESPONSE_BYTES,
        "facet.assignment.patch": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.query.list": MAX_LIST_RESPONSE_BYTES,
        "facet.query.get": MAX_READ_RESPONSE_BYTES,
        "facet.query.create": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.query.update": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.query.archive": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.query.attach": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.query.attachment.get": MAX_READ_RESPONSE_BYTES,
        "facet.query.page": MAX_LIST_RESPONSE_BYTES,
        "facet.history.list": MAX_LIST_RESPONSE_BYTES,
        "facet.history.state": MAX_READ_RESPONSE_BYTES,
        "review.propose_from_facet_query": MAX_RECEIPT_RESPONSE_BYTES,
        "facet.vault.preview": MAX_READ_RESPONSE_BYTES,
        "facet.vault.apply": MAX_RECEIPT_RESPONSE_BYTES,
    }
)
