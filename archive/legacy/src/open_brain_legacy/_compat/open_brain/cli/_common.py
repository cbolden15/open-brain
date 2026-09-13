# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Stable, redacted output primitives for the public CLI."""

from __future__ import annotations

import json
import re
from enum import IntEnum
from typing import Protocol, TextIO
from urllib.parse import unquote

_MAX_PUBLIC_OUTPUT_DECODING_PASSES = 3
_ADAPTER_OUTPUT_COMMANDS = {
    "ledger": frozenset(
        {
            "ledger.apply",
            "ledger.lifecycle",
            "ledger.reconcile",
            "ledger.requarantine",
            "ledger.scan",
            "ledger.slim",
            "ledger.stage",
            "ledger.synthesis",
        }
    ),
    "migrate": frozenset({"migration"}),
    "social": frozenset({"social.compatibility"}),
}
_PUBLIC_OUTPUT_SCHEMA_KEYS = frozenset(
    {
        "action",
        "action_count",
        "attempts",
        "backup_id",
        "candidate_count",
        "capture_id",
        "captures",
        "canonical",
        "checks",
        "claim_count",
        "cloud_enabled",
        "code",
        "command",
        "commands",
        "configuration",
        "disposition",
        "decision_id",
        "dry_run",
        "duplicate",
        "egress_enabled",
        "entry_count",
        "enrichment_state",
        "error",
        "event_count",
        "excerpt",
        "explanation",
        "findings",
        "held_count",
        "historical_diagnoses",
        "ledger",
        "ledger_route_count",
        "manifest_digest",
        "manifest_digest_sha256",
        "manifest_id",
        "message",
        "metrics",
        "migrate",
        "name",
        "network",
        "network_access",
        "output",
        "output_mode",
        "owner_gated",
        "page_id",
        "pipeline",
        "proposal_id",
        "plan",
        "policy",
        "privacy_tier",
        "proposals",
        "proposed_intent",
        "protected_count",
        "provider",
        "provenance",
        "publication_id",
        "rank",
        "ranked_claim_ids",
        "reason",
        "record_count",
        "redacted",
        "redacted_count",
        "reject",
        "removed_count",
        "replayed",
        "request_id",
        "required_evidence",
        "restored_count",
        "result_id",
        "results",
        "retrieval_id",
        "review",
        "review_id",
        "reviews",
        "role",
        "run_count",
        "runs",
        "schema_version",
        "slug",
        "space_id",
        "spaces",
        "social",
        "source_ref_sha256",
        "source_type",
        "payload_family",
        "record_type",
        "staged_digest_sha256",
        "state",
        "status",
        "strict",
        "tier",
        "title",
        "truncated",
        "trust",
        "window_seconds",
    }
)
_PUBLIC_OPAQUE_ID_KEYS = frozenset(
    {
        "capture_id",
        "decision_id",
        "page_id",
        "proposal_id",
        "publication_id",
        "result_id",
        "review_id",
        "space_id",
    }
)
_RESERVED_READINESS_KEYS = frozenset(
    {
        "cutover",
        "cutover_ready",
        "live",
        "live_health",
        "live_healthy",
        "parity",
        "parity_green",
        "parity_ready",
    }
)
_PUBLIC_OUTPUT_ENUM_VALUES = {
    "action": frozenset({"apply", "approve", "archive", "edit", "reject"}),
    "enrichment_state": frozenset({"enriched", "pending_enrichment"}),
    "payload_family": frozenset({"event", "measurement", "reference_or_file", "text"}),
    "record_type": frozenset({"canonical", "source"}),
}
_ARGUMENT_ECHO_OUTPUT_KEYS = {
    "query": frozenset({"excerpt", "explanation", "title"}),
}
_PUBLIC_OUTPUT_RESIDUALS = (
    re.compile(
        r"(?ix)\b(?:api[\s_-]*key|authorization|bearer|credential|password|secret|token)"
        r"\b(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,31}://[^\s]+"),
    re.compile(r"(?<![\w.])(?:~)?/(?:[^/\s]+)(?:/[^/\s]+)*"),
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/][^\s]+"),
    re.compile(r"(?<!\\)\\\\[^\\\s]+\\[^\s]+"),
    re.compile(r"(?i)\btraceback \(most recent call last\)"),
    re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\s*:"),
)


class ExitCode(IntEnum):
    """Process exit classes exposed by the CLI scaffold."""

    SUCCESS = 0
    FAILURE = 1
    USAGE = 2
    DEFERRED = 3
    LOCK_HELD = 75
    CONFIGURATION = 78


class CommandDispatchResult(Protocol):
    """Redaction-safe result returned by one public command-family adapter."""

    @property
    def exit_code(self) -> int: ...

    @property
    def envelope(self) -> dict[str, object]: ...


class CommandFamilyAdapter(Protocol):
    """Typed effect boundary for one selected public command family."""

    def dispatch(self, argv: tuple[str, ...]) -> CommandDispatchResult: ...


class CommandAdapterLookup(Protocol):
    """The narrow command-registry capability needed by the CLI process shell."""

    def get(self, name: str) -> CommandFamilyAdapter | None: ...


def redacted_error(code: str, _exception: Exception | None = None) -> dict[str, str | bool]:
    """Return a public error shape without exception or environment details."""
    return {
        "code": code,
        "message": "operation unavailable; details redacted",
        "redacted": True,
    }


def unavailable_envelope(command: str) -> dict[str, object]:
    """Return the closed response for a public family without an injected adapter."""
    return {
        "command": command,
        "error": redacted_error("command_adapter_unavailable"),
        "status": "unavailable",
    }


def adapter_failed_envelope(command: str) -> dict[str, object]:
    """Return the closed response when an injected adapter cannot dispatch safely."""
    return {
        "command": command,
        "error": redacted_error("command_adapter_failed"),
        "status": "failed",
    }


def invalid_envelope() -> dict[str, object]:
    """Return a redacted response for invalid command syntax."""
    return {
        "error": redacted_error("invalid_command"),
        "status": "invalid",
    }


def validate_adapter_envelope(
    command: str,
    envelope: object,
    *,
    argv: tuple[str, ...],
) -> dict[str, object]:
    """Reject malformed, mismatched, or residual-bearing adapter output."""
    if type(envelope) is not dict:
        raise ValueError("invalid command adapter envelope")
    output_command = envelope.get("command")
    allowed_commands = _ADAPTER_OUTPUT_COMMANDS.get(command, frozenset({command}))
    if (
        not isinstance(output_command, str)
        or output_command not in allowed_commands
        or not isinstance(envelope.get("status"), str)
    ):
        raise ValueError("mismatched command adapter envelope")
    payload = dict(envelope)
    del payload["command"]
    _validate_public_output(
        payload,
        argv=argv,
        argument_echo_keys=_ARGUMENT_ECHO_OUTPUT_KEYS.get(command, frozenset()),
    )
    json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return envelope


def _validate_public_output(
    value: object,
    *,
    argv: tuple[str, ...],
    argument_echo_keys: frozenset[str] = frozenset(),
    check_arguments: bool = True,
    is_key: bool = False,
) -> None:
    if isinstance(value, str):
        variants = _decoded_variants(value)
        if is_key and any(variant.casefold() in _RESERVED_READINESS_KEYS for variant in variants):
            raise ValueError("reserved readiness key in command adapter output")
        if any(
            pattern.search(variant) for pattern in _PUBLIC_OUTPUT_RESIDUALS for variant in variants
        ):
            raise ValueError("unsafe command adapter output")
        if any(ord(character) < 32 and character not in {"\n", "\t"} for character in value):
            raise ValueError("unsafe command adapter output")
        check_argv = check_arguments and not (is_key and value in _PUBLIC_OUTPUT_SCHEMA_KEYS)
        if check_argv and any(
            argument
            and argument not in {"--json", "--dry-run"}
            and (variant == argument or (len(argument) >= 8 and argument in variant))
            for argument in argv
            for variant in variants
        ):
            raise ValueError("command adapter echoed argv")
        return
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("invalid command adapter output key")
            _validate_public_output(
                key,
                argv=argv,
                argument_echo_keys=argument_echo_keys,
                check_arguments=True,
                is_key=True,
            )
            _validate_public_output(
                item,
                argv=argv,
                argument_echo_keys=argument_echo_keys,
                check_arguments=not (
                    isinstance(item, str)
                    and (
                        key in argument_echo_keys
                        or key in _PUBLIC_OPAQUE_ID_KEYS
                        or item in _PUBLIC_OUTPUT_ENUM_VALUES.get(key, frozenset())
                    )
                ),
            )
        return
    if type(value) is list:
        for item in value:
            _validate_public_output(
                item,
                argv=argv,
                argument_echo_keys=argument_echo_keys,
            )
        return
    if value is None or type(value) in {bool, int, float}:
        return
    raise ValueError("invalid command adapter output value")


def _decoded_variants(value: str) -> tuple[str, ...]:
    variants = [value]
    for _ in range(_MAX_PUBLIC_OUTPUT_DECODING_PASSES):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            break
        variants.append(decoded)
    if unquote(variants[-1]) != variants[-1]:
        raise ValueError("command adapter output encoding did not converge")
    return tuple(variants)


def write_envelope(envelope: dict[str, object], *, json_output: bool, stream: TextIO) -> None:
    """Write deterministic JSON or a deliberately non-sensitive text summary."""
    if json_output:
        stream.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")
        return

    status = envelope.get("status")
    if status == "invalid":
        stream.write("invalid command or arguments\n")
    elif envelope.get("owner_gated") is True:
        stream.write("command deferred; owner approval required\n")
    elif status == "deferred":
        stream.write("command deferred; application service is not implemented\n")
    elif status == "unavailable":
        stream.write("command adapter unavailable\n")
    elif status == "failed":
        stream.write("command failed\n")
    else:
        stream.write("command completed\n")
