"""Explicit, fail-closed adapters for already-local public CLI services."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from open_brain_engine.core.models import PrivacyTier

from open_brain_legacy._compat.open_brain.cli._common import (
    CommandDispatchResult,
    ExitCode,
    redacted_error,
    validate_adapter_envelope,
)
from open_brain_legacy._compat.open_brain.integrations.ports import WorkRetriever
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.capture import (
    CaptureEnqueuer,
    ShareSubmitter,
    capture_text,
    share_capture,
)
from open_brain_legacy.cli.config import show_registry
from open_brain_legacy.cli.explain import explain_policy
from open_brain_legacy.cli.operations import (
    CronReader,
    DigestOutputMode,
    DigestService,
    OkfService,
    RetentionService,
    render_digest,
    run_okf,
    run_retention,
    show_cron,
)
from open_brain_legacy.cli.proposals import list_proposals
from open_brain_legacy.cli.query import query_work
from open_brain_legacy.cli.social import SocialCompatibilityAction, compatibility
from open_brain_legacy.cli.status import show_status
from open_brain_legacy.operations.status import StatusResult

_FAMILIES = frozenset(
    {
        "capture",
        "share",
        "query",
        "status",
        "explain",
        "registry",
        "proposals",
        "retention",
        "cron",
        "digest",
        "ledger",
        "okf",
        "social",
    }
)
_FORBIDDEN_READINESS_TEXT = frozenset(
    {"deferred", "owner-gated", "owner_gated", "live", "parity", "cutover", "ready"}
)


class StatusService(Protocol):
    """Collect an already-composed metadata-only status result."""

    def collect(self, *, strict: bool) -> StatusResult: ...


class ProposalReader(Protocol):
    """Read typed review aggregates without exposing persistence details."""

    def list(self) -> Iterable[object]: ...


class FamilyService(Protocol):
    """Dispatch one explicit local family that owns its typed subcommands."""

    def dispatch(self, argv: tuple[str, ...]) -> CommandDispatchResult: ...


@dataclass(frozen=True, slots=True)
class ProductionCommandDependencies:
    """Concrete local services supplied by the caller; no discovery occurs here."""

    capture_queue: CaptureEnqueuer | None = None
    clock: Callable[[], datetime] | None = None
    share_submitter: ShareSubmitter | None = None
    retriever: WorkRetriever | None = None
    status: StatusService | None = None
    proposals: ProposalReader | None = None
    retention: RetentionService | None = None
    cron: CronReader | None = None
    digest: DigestService | None = None
    ledger: FamilyService | None = None
    okf: OkfService | None = None


@dataclass(frozen=True, slots=True)
class ProductionCliResult:
    """A public adapter result with an already-validated safe envelope."""

    exit_code: ExitCode
    envelope: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProductionCommandAdapter:
    """Route one public family through explicitly injected local services."""

    dependencies: ProductionCommandDependencies
    family: str = "capture"

    def dispatch(
        self, argv: tuple[str, ...], *, family: str | None = None
    ) -> ProductionCliResult:
        selected = self.family if family is None else family
        if selected not in _FAMILIES:
            return _invalid(selected)
        request = _request(argv)
        if request is None:
            return _invalid(selected)
        positional, options, dry_run = request
        try:
            result = self._dispatch(selected, positional, options, dry_run)
        except Exception:
            return _failed(selected, "production_command_failed")
        return _safe_result(selected, result, argv)

    def _dispatch(
        self,
        family: str,
        positional: tuple[str, ...],
        options: frozenset[str],
        dry_run: bool,
    ) -> CommandDispatchResult | ProductionCliResult:
        if family == "capture":
            return self._capture(positional, options, dry_run)
        if family == "share":
            return self._share(positional, options, dry_run)
        if family == "query":
            return self._query(positional, options, dry_run)
        if family == "status":
            return self._status(positional, options, dry_run)
        if family == "explain":
            return self._explain(positional, options, dry_run)
        if family == "registry":
            return self._registry(positional, options, dry_run)
        if family == "proposals":
            return self._proposals(positional, options, dry_run)
        if family == "retention":
            return self._retention(positional, options, dry_run)
        if family == "cron":
            return self._cron(positional, options, dry_run)
        if family == "digest":
            return self._digest(positional, options, dry_run)
        if family == "ledger":
            return self._delegate(self.dependencies.ledger, family, positional, options, dry_run)
        if family == "okf":
            return self._okf(positional, options, dry_run)
        return self._social(positional, options, dry_run)

    def _capture(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        valid_privacy, privacy_tier = _privacy_option(options)
        if len(positional) != 3 or positional[0] != "text" or not valid_privacy:
            return _invalid("capture")
        if self.dependencies.capture_queue is None or self.dependencies.clock is None:
            return _missing("capture")
        return _omit_fields(
            "capture",
            capture_text(
                queue=self.dependencies.capture_queue,
                now=self.dependencies.clock(),
                text=positional[1],
                why=positional[2],
                dry_run=dry_run,
                privacy_tier=privacy_tier,
            ),
            "source_type",
        )

    def _share(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        valid_privacy, privacy_tier = _privacy_option(options)
        if len(positional) not in {2, 3} or not valid_privacy:
            return _invalid("share")
        if self.dependencies.share_submitter is None:
            return _missing("share")
        return share_capture(
            submitter=self.dependencies.share_submitter,
            url=positional[0],
            why=positional[1],
            text="" if len(positional) == 2 else positional[2],
            dry_run=dry_run,
            privacy_tier=privacy_tier,
        )

    def _query(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if len(positional) != 1 or dry_run:
            return _invalid("query")
        limit = 5
        for option in options:
            if not option.startswith("--limit="):
                return _invalid("query")
            try:
                limit = int(option.removeprefix("--limit="))
            except ValueError:
                return _invalid("query")
        if self.dependencies.retriever is None:
            return _missing("query")
        return query_work(
            retriever=self.dependencies.retriever,
            question=positional[0],
            limit=limit,
        )

    def _status(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if positional or options - {"--strict"} or dry_run:
            return _invalid("status")
        if self.dependencies.status is None:
            return _missing("status")
        result = self.dependencies.status.collect(strict="--strict" in options)
        if not isinstance(result, StatusResult):
            return _failed("status", "production_command_failed")
        return show_status(result=result)

    def _explain(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if positional != ("no-network",) or options or dry_run:
            return _invalid("explain")
        return _omit_fields("explain", explain_policy("no-network"), "policy")

    def _registry(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if positional or options or dry_run:
            return _invalid("registry")
        return show_registry()

    def _proposals(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if positional not in {(), ("list",)} or options or dry_run:
            return _invalid("proposals")
        if self.dependencies.proposals is None:
            return _missing("proposals")
        result = list_proposals(proposals=self.dependencies.proposals.list())
        if result.exit_code is ExitCode.DEFERRED:
            return _failed("proposals", "production_command_failed")
        return result

    def _retention(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if positional or options - {"--apply"} or (dry_run and "--apply" in options):
            return _invalid("retention")
        if self.dependencies.retention is None:
            return _missing("retention")
        return run_retention(
            service=self.dependencies.retention,
            dry_run="--apply" not in options,
        )

    def _cron(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if positional not in {(), ("report",)} or dry_run:
            return _invalid("cron")
        window_seconds = 86_400
        for option in options:
            if not option.startswith("--window-seconds="):
                return _invalid("cron")
            try:
                window_seconds = int(option.removeprefix("--window-seconds="))
            except ValueError:
                return _invalid("cron")
        if self.dependencies.cron is None:
            return _missing("cron")
        return show_cron(reader=self.dependencies.cron, window_seconds=window_seconds)

    def _digest(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if len(positional) != 1 or options - {"--text"} or dry_run:
            return _invalid("digest")
        if self.dependencies.digest is None:
            return _missing("digest")
        if positional[0] != PrivacyTier.WORK.value:
            return _invalid("digest")
        output_mode = DigestOutputMode.TEXT if "--text" in options else DigestOutputMode.JSON
        return _omit_fields(
            "digest",
            render_digest(
                service=self.dependencies.digest,
                tier=PrivacyTier.WORK,
                output_mode=output_mode,
            ),
            "tier",
        )

    def _delegate(
        self,
        service: FamilyService | None,
        family: str,
        positional: tuple[str, ...],
        options: frozenset[str],
        dry_run: bool,
    ) -> CommandDispatchResult | ProductionCliResult:
        if not positional:
            return _invalid(family)
        if service is None:
            return _missing(family)
        argv = (*positional, *sorted(options), *( ("--dry-run",) if dry_run else () ))
        return service.dispatch(argv)

    def _okf(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> CommandDispatchResult | ProductionCliResult:
        if len(positional) != 1 or options or dry_run:
            return _invalid("okf")
        if self.dependencies.okf is None:
            return _missing("okf")
        return _omit_fields(
            "okf",
            run_okf(service=self.dependencies.okf, action=positional[0]),
            "action",
        )

    def _social(
        self, positional: tuple[str, ...], options: frozenset[str], dry_run: bool
    ) -> ProductionCliResult:
        if len(positional) != 1 or options:
            return _invalid("social")
        try:
            result = compatibility(
                action=SocialCompatibilityAction(positional[0]),
                dry_run=dry_run,
            )
        except ValueError:
            return _invalid("social")
        if result.exit_code is not ExitCode.SUCCESS:
            return _failed("social", "production_command_failed")
        return ProductionCliResult(
            ExitCode.SUCCESS,
            {
                "command": "social.compatibility",
                "dry_run": dry_run,
                "status": "ok",
            },
        )


def build_production_command_adapters(
    dependencies: ProductionCommandDependencies,
) -> CommandAdapterRegistry:
    """Build every explicitly supported adapter without loading ambient runtime state."""
    return CommandAdapterRegistry(
        {
            family: ProductionCommandAdapter(dependencies=dependencies, family=family)
            for family in sorted(_FAMILIES)
        }
    )


def _request(argv: object) -> tuple[tuple[str, ...], frozenset[str], bool] | None:
    if not isinstance(argv, tuple) or any(not isinstance(argument, str) for argument in argv):
        return None
    positional: list[str] = []
    options: set[str] = set()
    dry_run = False
    for argument in argv:
        if argument == "--json":
            continue
        if argument == "--dry-run":
            if dry_run:
                return None
            dry_run = True
            continue
        if argument.startswith("--"):
            if argument in options:
                return None
            options.add(argument)
            continue
        if argument.startswith("-") or "\x00" in argument:
            return None
        positional.append(argument)
    return tuple(positional), frozenset(options), dry_run


def _privacy_option(options: frozenset[str]) -> tuple[bool, PrivacyTier | None]:
    if not options:
        return True, None
    if len(options) != 1:
        return False, None
    option = next(iter(options))
    if not option.startswith("--privacy="):
        return False, None
    try:
        return True, PrivacyTier(option.removeprefix("--privacy="))
    except ValueError:
        return False, None


def _safe_result(
    family: str,
    result: CommandDispatchResult | ProductionCliResult,
    argv: tuple[str, ...],
) -> ProductionCliResult:
    if isinstance(result, ProductionCliResult):
        exit_code = result.exit_code
        envelope = result.envelope
    else:
        try:
            exit_code = ExitCode(result.exit_code)
        except (AttributeError, TypeError, ValueError):
            raw_exit_code = getattr(result, "exit_code", None)
            if not isinstance(raw_exit_code, int) or isinstance(raw_exit_code, bool):
                return _failed(family, "production_command_failed")
            exit_code = ExitCode.SUCCESS if raw_exit_code == 0 else ExitCode.FAILURE
        try:
            envelope = result.envelope
        except AttributeError:
            return _failed(family, "production_command_failed")
    try:
        validated = validate_adapter_envelope(family, envelope, argv=argv)
    except Exception:
        return _failed(family, "production_command_failed")
    if _contains_forbidden_readiness(validated):
        return _failed(family, "production_command_failed")
    return ProductionCliResult(exit_code, validated)


def _omit_fields(
    family: str, result: CommandDispatchResult, *fields: str
) -> ProductionCliResult:
    """Retain typed result status while withholding argv-derived public metadata."""
    try:
        exit_code = ExitCode(result.exit_code)
        envelope = dict(result.envelope)
    except (AttributeError, TypeError, ValueError):
        return _failed(family, "production_command_failed")
    for field in fields:
        envelope.pop(field, None)
    return ProductionCliResult(exit_code, envelope)


def _contains_forbidden_readiness(value: object) -> bool:
    if isinstance(value, str):
        folded = value.casefold()
        return any(forbidden in folded for forbidden in _FORBIDDEN_READINESS_TEXT)
    if isinstance(value, dict):
        return any(
            _contains_forbidden_readiness(key) or _contains_forbidden_readiness(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_forbidden_readiness(item) for item in value)
    return False


def _invalid(family: str) -> ProductionCliResult:
    return ProductionCliResult(
        ExitCode.USAGE,
        {
            "command": family,
            "error": redacted_error("invalid_production_command_request"),
            "status": "invalid",
        },
    )


def _missing(family: str) -> ProductionCliResult:
    return _failed(family, "production_dependency_unavailable")


def _failed(family: str, code: str) -> ProductionCliResult:
    return ProductionCliResult(
        ExitCode.FAILURE,
        {"command": family, "error": redacted_error(code), "status": "failed"},
    )
