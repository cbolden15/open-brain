"""Foreground, destination-bound bridge for optional native memory integrations.

Enqueue and status never open the Brain. Reading or draining requires an existing
destination, current durable identity, current source policy and provider consent.
The CLI never accepts a request's claim that a provider is local-only.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.engine.harness_memory import HarnessMemory
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.storage.sqlite import connect_database_read_only

from open_brain.local_data import select_local_root
from open_brain.profile import open_existing_single_user_local

from .agent_setup import _open_directory
from .harness_events import HarnessEventError, _screen, capture_event
from .harness_outbox import HarnessOutbox
from .harness_policy import HarnessPolicyError, load_harness_policy
from .harness_workflow import HarnessWorkflow
from .local_bootstrap import open_local_brain
from .local_operations import current_brain_identity
from .session_consent import DurableProviderConsentStore

_SCHEMA = "open-brain-harness-bridge.v1"
_CLIENTS = frozenset({"codex", "claude-code", "pi"})


class HarnessBridgeError(ValueError):
    """Closed error metadata independent of private parser and storage details."""

    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_configuration",
            "unsafe_configuration",
            "destination_mismatch",
            "invalid_request",
            "local_only_host_required",
            "bridge_unavailable",
        }:
            raise ValueError("invalid bridge error")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HarnessBridgeConfig:
    brain_root: Path
    brain_id: str
    policy: Path
    consent_state: Path
    outbox: Path

    @classmethod
    def load(cls, path: Path) -> HarnessBridgeConfig:
        value = _read_configuration(path)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "brain_root",
                "brain_id",
                "policy",
                "consent_state",
                "outbox",
            }
            or value["schema_version"] != _SCHEMA
        ):
            raise HarnessBridgeError("invalid_configuration")
        brain_id = value["brain_id"]
        if not isinstance(brain_id, str):
            raise HarnessBridgeError("invalid_configuration")
        paths: list[Path] = []
        for name in ("brain_root", "policy", "consent_state", "outbox"):
            raw = value[name]
            if not isinstance(raw, str) or "\0" in raw:
                raise HarnessBridgeError("invalid_configuration")
            candidate = Path(raw)
            if not candidate.is_absolute() or ".." in candidate.parts:
                raise HarnessBridgeError("invalid_configuration")
            paths.append(candidate)
        # Validates destination format without creating outbox files.
        HarnessOutbox(paths[3], brain_id)
        return cls(paths[0], brain_id, paths[1], paths[2], paths[3])


def _unique(pairs: list[tuple[str, object]]) -> Mapping[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessBridgeError("invalid_configuration")
        result[key] = value
    return result


def _read_configuration(path: Path) -> object:
    directory = descriptor = -1
    try:
        directory = _open_directory(path.parent, create=False)
        parent = os.fstat(directory)
        if stat.S_IMODE(parent.st_mode) != 0o700:
            raise HarnessBridgeError("unsafe_configuration")
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 16384
        ):
            raise HarnessBridgeError("unsafe_configuration")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise HarnessBridgeError("invalid_configuration")
        return json.loads(raw, object_pairs_hook=_unique)
    except HarnessBridgeError:
        raise
    except Exception:
        raise HarnessBridgeError("unsafe_configuration") from None
    finally:
        for handle in (descriptor, directory):
            if handle >= 0:
                os.close(handle)


@contextmanager
def _workflow(config: HarnessBridgeConfig, *, local_only_host: bool) -> Iterator[HarnessWorkflow]:
    # Resolve and inspect the existing destination before any bootstrap write.
    profile = open_existing_single_user_local(config.brain_root)
    if derive_brain_id(profile.tenant_id) != config.brain_id:
        raise HarnessBridgeError("destination_mismatch")
    connection = connect_database_read_only(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    try:
        row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
    finally:
        connection.close()
    if row is None or row[0] != config.brain_id:
        raise HarnessBridgeError("destination_mismatch")
    identity = (cast(str, row[0]), cast(int, row[1]))
    selection = select_local_root(data_dir=str(config.brain_root), environment=os.environ)
    with open_local_brain(selection) as session:
        if current_brain_identity(session.tasks) != identity:
            raise HarnessBridgeError("destination_mismatch")
        consent = DurableProviderConsentStore(
            config.consent_state,
            brain_id=identity[0],
            issuer_epoch=identity[1],
        )
        yield HarnessWorkflow(
            session.tasks,
            lambda: load_harness_policy(config.policy),
            consent_store=consent,
            local_only_host=local_only_host,
        )


def _project(client: str, payload: dict[str, object]) -> Path:
    raw = payload.get("project_path") if client == "pi" else payload.get("cwd")
    if not isinstance(raw, str) or "\0" in raw or not Path(raw).is_absolute():
        raise HarnessBridgeError("invalid_request")
    return Path(raw)


def _citation(memory: HarnessMemory) -> str:
    citation = memory.citation
    return json.dumps(
        {
            "harness": citation.harness,
            "client": citation.client,
            "project_id": citation.project_id,
            "session_id": citation.session_id,
            "message_ordinal": citation.message_ordinal,
            "timestamp": citation.timestamp,
            "authorship": citation.authorship,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_results(memories: tuple[HarnessMemory, ...], *, automatic: bool) -> dict[str, object]:
    results: list[dict[str, str]] = []
    used = 128  # Reserve the integration's untrusted-data wrapper.
    omitted = 0
    for memory in memories:
        row = {"text": memory.text, "citation": _citation(memory)}
        cost = len((row["text"] + "\nSource: " + row["citation"] + "\n\n").encode())
        if automatic and used + cost > 8192:
            omitted += 1
            continue
        results.append(row)
        used += cost
    return {"status": "ok", "results": results, "omitted_count": omitted}


def run_harness(
    action: str,
    config_path: Path,
    client: str,
    payload: object = None,
    *,
    local_only_host: bool = False,
) -> dict[str, object]:
    """Trusted host function; models cannot grant local-only host authority.

    The subprocess CLI always leaves local_only_host false. A Pi host may call
    this API after enforcing every provider path; that native gate is separate
    from this function and must remain active through context release.
    """
    try:
        if client not in _CLIENTS or action not in {
            "enqueue",
            "drain",
            "status",
            "recall",
            "search",
        }:
            raise HarnessBridgeError("invalid_request")
        config = HarnessBridgeConfig.load(config_path)
        outbox = HarnessOutbox(config.outbox, config.brain_id)
        if action == "status":
            return outbox.status()
        policy = load_harness_policy(config.policy)
        if not policy.enabled:
            raise HarnessPolicyError("disabled")
        if action == "drain":
            try:
                with _workflow(config, local_only_host=False) as workflow:
                    return outbox.drain(workflow)
            except Exception:
                outbox.mark_unavailable()
                raise
        if not isinstance(payload, dict):
            raise HarnessBridgeError("invalid_request")
        project = _project(client, payload)
        # Exclusions and opt-out apply before transcript reads or Brain startup.
        query = payload.get("query", payload.get("prompt", ""))
        if not isinstance(query, str):
            raise HarnessBridgeError("invalid_request")
        decision = policy.classify(project, authored_text=query)
        if action == "enqueue":
            event = capture_event(client, payload)
            policy.classify(
                project,
                authored_text="\n".join(
                    message.text for message in event.messages if message.authored
                ),
            )
            return outbox.enqueue(event)
        if not query.strip() or len(query.encode()) > 2048:
            raise HarnessBridgeError("invalid_request")
        _screen(query)
        limit = payload.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise HarnessBridgeError("invalid_request")
        requested_local = payload.get("local_only", False)
        if type(requested_local) is not bool:
            raise HarnessBridgeError("invalid_request")
        if decision.local_only and not (client == "pi" and local_only_host and requested_local):
            return {
                "status": "refused",
                "requires_local_only": True,
                "error_code": "local_only_host_required",
            }
        if requested_local and not (client == "pi" and local_only_host):
            raise HarnessBridgeError("local_only_host_required")
        if not requested_local and not policy.allowed_tiers(client):
            return {"status": "refused", "error_code": "provider_denied"}
        if not requested_local:
            guard = outbox.cloud_read_guard(policy)
            if guard.get("status") != "allowed":
                return guard
        with _workflow(config, local_only_host=local_only_host) as workflow:
            consent = None if requested_local else workflow.consent_snapshot()
            response = (
                workflow.recall_local(query, project=project, limit=limit)
                if requested_local
                else workflow.recall(query, provider=client, limit=limit)
            )
        if response.status not in {"success", "success_empty"}:
            return {"status": response.status, "error_code": response.error_code}
        # Recheck source policy after closing runtime and before releasing content.
        if load_harness_policy(config.policy) != policy:
            return {"status": "refused", "error_code": "policy_changed"}
        if not requested_local:
            guard = outbox.cloud_read_guard(policy)
            if guard.get("status") != "allowed":
                return guard
        if not workflow.authorization_current(policy, consent):
            return {"status": "refused", "error_code": "authorization_changed"}
        return _bounded_results(response.memories, automatic=action == "recall")
    except (HarnessBridgeError, HarnessEventError, HarnessPolicyError) as error:
        return {"status": "refused", "error_code": error.code}
    except Exception:
        return {"status": "unavailable", "error_code": "bridge_unavailable"}
