"""Shared metadata-only source operations for collector CLI and native desktop."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import cast

from open_brain_engine.engine import ReadViewUnavailableError, open_local_read_view

from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.runner import CollectorProfileError, _open_existing_collector_profile
from open_brain_collector.slack_patches import SlackPatchProposer
from open_brain_collector.slack_policy import SlackPolicyStore
from open_brain_connectors.runtime.agent_session_hooks import (
    AgentSessionHookManager,
    HookClient,
    HookConfigError,
)
from open_brain_connectors.runtime.agent_session_live import AgentSessionLiveSource
from open_brain_connectors.runtime.google_sources import DriveSourceClient, GmailSourceClient
from open_brain_connectors.runtime.google_sources_auth import GoogleSourcesAuth
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveCredentialStore,
    LiveSourceError,
    bounded_json,
    safe_text,
)
from open_brain_connectors.runtime.live_http import LiveHttpTransport
from open_brain_connectors.runtime.live_storage import OsCredentialStore, PrivateJsonStore
from open_brain_connectors.runtime.slack_auth import SlackAuth
from open_brain_connectors.runtime.slack_live import SlackSourceClient
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

OPERATIONS = frozenset(
    {
        "sources.status",
        "sources.accounts",
        "sources.connect",
        "sources.disconnect",
        "sources.resources",
        "sources.configure",
        "sources.preview",
        "sources.import",
        "sources.control",
        "sources.custody_status",
        "sources.custody_inspect",
        "sources.custody_retry",
        "sources.session_preview",
        "sources.session_apply",
        "sources.slack_policy_setup",
        "sources.slack_mapping_add",
        "sources.slack_mapping_list",
        "sources.slack_mapping_remove",
        "sources.slack_suggestions",
        "sources.slack_suggestion_approve",
        "sources.slack_suggestion_dismiss",
        "sources.slack_status",
        "sources.slack_discover",
    }
)


def _shape(
    arguments: dict[str, object], required: set[str], optional: set[str] | None = None
) -> None:
    if not required <= set(arguments) or not set(arguments) <= required | (optional or set()):
        raise LiveSourceError("source_invalid_arguments")


def _provider(value: object) -> str:
    if value not in {"gmail", "google_drive", "slack"}:
        raise LiveSourceError("source_invalid_provider")
    return value


class PriorityRuntime:
    def __init__(
        self,
        root: Path,
        *,
        http: LiveHttpTransport | None = None,
        credentials: LiveCredentialStore | None = None,
    ) -> None:
        self.root = root
        self.http = http or LiveHttpTransport()
        self.credentials = credentials or OsCredentialStore()

    def auth(self, provider: str) -> GoogleSourcesAuth | SlackAuth:
        provider = _provider(provider)
        root = self.root / "accounts"
        if provider == "slack":
            return SlackAuth(root / "slack", credentials=self.credentials, http=self.http)
        return GoogleSourcesAuth(provider, root, credentials=self.credentials, http=self.http)

    def client(self, provider: str) -> GmailSourceClient | DriveSourceClient | SlackSourceClient:
        account = self.auth(provider)
        if isinstance(account, SlackAuth):
            return SlackSourceClient(account, http=self.http)
        return (
            GmailSourceClient(account, http=self.http)
            if provider == "gmail"
            else DriveSourceClient(account, http=self.http)
        )

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        if selection.connector_name == "agent_session":
            return AgentSessionLiveSource(self.root / "sessions").fetch(
                selection, options, checkpoint
            )
        return self.client(selection.connector_name).fetch(selection, options, checkpoint)

    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None:
        if selection.connector_name == "agent_session":
            AgentSessionLiveSource(self.root / "sessions").acknowledge(selection, checkpoint)


class LiveSourceManager:
    def __init__(
        self,
        root: Path,
        brain_root: Path,
        *,
        background: bool = False,
        runtime: PriorityRuntime | None = None,
    ) -> None:
        self.root = root
        self.brain_root = brain_root
        self.runtime = runtime or PriorityRuntime(root)
        self._setup = PrivateJsonStore(root / "setup")
        self._slack_policy = SlackPolicyStore(root)
        self._slack_patches = SlackPatchProposer(brain_root, self._slack_policy)
        self.capture = LiveCaptureService(
            root / "capture",
            brain_root,
            runtime=self.runtime,
            post_apply=self._post_apply,
        )
        self.background = background

    def _post_apply(
        self,
        selection: SourceResourceSelection,
        records: tuple[tuple[SourceRecordIntake, str], ...],
    ) -> None:
        self._slack_patches.propose(selection, records)

    def sync_due(self) -> list[dict[str, object]]:
        """Run due discovery before channel capture, without enabling suggestions."""
        results: list[dict[str, object]] = []
        for account in self._slack_policy.accounts():
            try:
                policy = self._slack_policy.policy(account)
                discovery = cast(dict[str, object], policy["discovery"])
                now = int(time.time())
                if (
                    discovery["checkpoint"] is not None
                    or discovery["completed_at"] is None
                    or now - cast(int, discovery["completed_at"]) >= 86_400
                ):
                    outcome = self._slack("sources.slack_discover", {"connection_id": account})
                    results.append(
                        {
                            "source_id": "slack-discovery:" + account,
                            "outcome": "completed",
                            **outcome,
                        }
                    )
            except LiveSourceError as error:
                results.append(
                    {
                        "source_id": "slack-discovery:" + account,
                        "outcome": "failed",
                        "failure_code": error.code,
                    }
                )
        return [*results, *self.capture.sync_due()]

    def dispatch(self, operation: str, arguments: object) -> dict[str, object]:
        if operation not in OPERATIONS or type(arguments) is not dict:
            raise LiveSourceError("source_invalid_operation")
        args = cast(dict[str, object], arguments)
        bounded_json(args, 65_536)
        if operation == "sources.status":
            _shape(args, set())
            return {
                **self.capture.status(),
                "collector": {
                    "connected": True,
                    "background": self.background,
                },
                "slack": self._slack_policy.status(),
            }
        if operation == "sources.custody_status":
            _shape(args, set(), {"source_id"})
            source_id = args.get("source_id")
            return self.capture.custody_status(None if source_id is None else safe_text(source_id))
        if operation.startswith("sources.slack_"):
            return self._slack(operation, args)
        if operation == "sources.custody_inspect":
            _shape(args, {"receipt_id"})
            return self.capture.custody_inspect(safe_text(args["receipt_id"]))
        if operation == "sources.custody_retry":
            _shape(args, {"receipt_id"})
            return self.capture.custody_retry(safe_text(args["receipt_id"]))
        if operation in {"sources.accounts", "sources.connect", "sources.disconnect"}:
            return self._account(operation, args)
        if operation == "sources.resources":
            _shape(args, {"provider", "connection_id"}, {"cursor"})
            cursor = args.get("cursor")
            page = self.runtime.client(_provider(args["provider"])).resources(
                safe_text(args["connection_id"]),
                cursor=None if cursor is None else safe_text(cursor, maximum=8192),
            )
            return {
                "resources": [item.to_dict() for item in page.resources],
                "next_cursor": page.next_cursor,
            }
        if operation == "sources.configure":
            _shape(args, {"selection", "options"}, {"source_id", "reset"})
            value = args["selection"]
            if not isinstance(value, dict) or set(value) != {
                "connector_name",
                "connection_id",
                "resource_id",
                "resource_type",
            }:
                raise LiveSourceError("source_invalid_selection")
            selected = SourceResourceSelection(**value)
            if selected.connector_name == "agent_session":
                raise LiveSourceError("source_use_session_setup")
            if type(args["options"]) is not dict or type(args.get("reset", False)) is not bool:
                raise LiveSourceError("source_invalid_arguments")
            source_id = safe_text(args.get("source_id", self._source_id(selected)))
            result = self.capture.configure(
                source_id,
                selected,
                cast(dict[str, object], args["options"]),
                reset=cast(bool, args.get("reset", False)),
            )
            if selected.connector_name == "slack":
                result = self.capture.control(source_id, "schedule", interval_seconds=14_400)
            return result
        if operation == "sources.preview":
            _shape(args, {"source_id"})
            return self.capture.preview(safe_text(args["source_id"]))
        if operation == "sources.import":
            _shape(args, {"source_id", "preview_id"})
            return self.capture.apply(safe_text(args["source_id"]), safe_text(args["preview_id"]))
        if operation == "sources.control":
            _shape(args, {"source_id", "action"}, {"interval_seconds"})
            return self.capture.control(
                safe_text(args["source_id"]),
                safe_text(args["action"]),
                interval_seconds=cast(int | None, args.get("interval_seconds")),
            )
        if operation == "sources.session_preview":
            return self._session_preview(args)
        _shape(args, {"preview_id"})
        return self._session_apply(safe_text(args["preview_id"]))

    def _slack(self, operation: str, args: dict[str, object]) -> dict[str, object]:
        if operation == "sources.slack_policy_setup":
            _shape(
                args,
                {"connection_id"},
                {
                    "activity_weight",
                    "allowlist",
                    "keyword_weight",
                    "keywords",
                    "lookback_seconds",
                    "proposal_opt_in",
                    "threshold",
                },
            )
            return self._slack_policy.setup(
                args["connection_id"],
                {key: value for key, value in args.items() if key != "connection_id"},
            )
        if operation == "sources.slack_mapping_add":
            _shape(args, {"channel_id", "connection_id", "page_id"}, {"keyword"})
            return self._slack_policy.add_mapping(
                args["connection_id"],
                args["channel_id"],
                args["page_id"],
                args.get("keyword"),
                page_exists=self._page_exists,
            )
        if operation == "sources.slack_mapping_list":
            _shape(args, {"connection_id"})
            return self._slack_policy.mappings(args["connection_id"])
        if operation == "sources.slack_mapping_remove":
            _shape(args, {"connection_id", "mapping_id"})
            return self._slack_policy.remove_mapping(args["connection_id"], args["mapping_id"])
        if operation == "sources.slack_suggestions":
            _shape(args, {"connection_id"})
            return self._slack_policy.suggestions(args["connection_id"])
        if operation in {"sources.slack_suggestion_approve", "sources.slack_suggestion_dismiss"}:
            _shape(args, {"channel_id", "connection_id"})
            decide = (
                self._slack_policy.approve_suggestion
                if operation.endswith("approve")
                else self._slack_policy.dismiss_suggestion
            )
            return decide(args["connection_id"], args["channel_id"])
        if operation == "sources.slack_status":
            _shape(args, set())
            return {"status": "shown", **self._slack_policy.status()}
        _shape(args, {"connection_id"})
        policy = self._slack_policy.policy(args["connection_id"])
        discovery = cast(dict[str, object], policy["discovery"])
        now = int(time.time())
        if (
            discovery["checkpoint"] is None
            and discovery["completed_at"] is not None
            and (now - cast(int, discovery["completed_at"]) < 86_400)
        ):
            raise LiveSourceError("source_discovery_not_due")
        client = self.runtime.client("slack")
        if not isinstance(client, SlackSourceClient):
            raise LiveSourceError("source_invalid_provider")
        batch = client.discover(
            safe_text(args["connection_id"], maximum=128),
            {
                field: policy[field]
                for field in (
                    "activity_weight",
                    "allowlist",
                    "keyword_weight",
                    "keywords",
                    "lookback_seconds",
                    "threshold",
                )
            },
            cast(dict[str, object] | None, discovery["checkpoint"]),
        )
        saved = self._slack_policy.record_discovery(
            args["connection_id"],
            batch.checkpoint,
            batch.suggestions,
            completed_at=now if batch.checkpoint is None else None,
        )
        return {
            "fetch_candidates": list(batch.fetch_candidates),
            "has_more": batch.has_more,
            "pending_suggestion_count": len(cast(list[object], saved["pending_suggestions"])),
            "suggestions": list(batch.suggestions),
        }

    def _page_exists(self, page_id: str) -> bool:
        try:
            profile = _open_existing_collector_profile(self.brain_root)
            return open_local_read_view(profile).read_page(page_id) is not None
        except CollectorProfileError:
            raise LiveSourceError("source_brain_unavailable") from None
        except (ReadViewUnavailableError, ValueError, OSError):
            raise LiveSourceError("source_brain_unavailable") from None

    @staticmethod
    def _source_id(selection: SourceResourceSelection) -> str:
        return (
            selection.connector_name
            + "."
            + hashlib.sha256(bounded_json(asdict(selection))).hexdigest()[:24]
        )

    def _account(self, operation: str, args: dict[str, object]) -> dict[str, object]:
        required = {"provider"}
        if operation == "sources.connect":
            required.add("client_config")
        elif operation == "sources.disconnect":
            required.add("connection_id")
        _shape(args, required)
        provider = _provider(args["provider"])
        auth = self.runtime.auth(provider)
        if operation == "sources.accounts":
            return {"accounts": [item.to_dict() for item in auth.accounts()]}
        if operation == "sources.connect":
            path = Path(safe_text(args["client_config"], maximum=4096))
            if not path.is_absolute():
                raise LiveSourceError("source_invalid_configuration_path")
            return {"account": auth.connect(path).to_dict()}
        connection = safe_text(args["connection_id"])
        for item in cast(list[dict[str, object]], self.capture.status()["sources"]):
            selected = cast(dict[str, object], item["selection"])
            if selected["connector_name"] == provider and selected["connection_id"] == connection:
                self.capture.control(cast(str, item["source_id"]), "disable")
        auth.disconnect(connection)
        return {"status": "disconnected"}

    def _session_preview(self, args: dict[str, object]) -> dict[str, object]:
        _shape(args, {"client", "project_path", "capture_summary", "capture_transcript", "action"})
        if (
            args["client"] not in {"claude_code", "codex"}
            or args["action"] not in {"configure", "remove"}
            or type(args["capture_summary"]) is not bool
            or type(args["capture_transcript"]) is not bool
        ):
            raise LiveSourceError("source_invalid_arguments")
        client: HookClient = args["client"]
        project = Path(safe_text(args["project_path"]))
        if not project.is_absolute():
            raise LiveSourceError("source_invalid_project")
        if args["action"] == "configure" and not (
            args["capture_summary"] or args["capture_transcript"]
        ):
            raise LiveSourceError("source_capture_consent_required")
        manager = AgentSessionHookManager(self.root / "sessions")
        try:
            preview = (
                manager.preview_apply(client, project)
                if args["action"] == "configure"
                else manager.preview_uninstall(client, project)
            )
        except HookConfigError, OSError:
            raise LiveSourceError("source_invalid_hook_configuration") from None
        preview_id = "setup:" + uuid.uuid4().hex
        value = {
            "schema_version": 1,
            "preview_id": preview_id,
            "arguments": args,
            "before_sha256": preview.before_sha256,
            "config_path": str(preview.config_path),
        }
        self._setup.write(preview_id.replace(":", "-") + ".json", value)
        return {"preview_id": preview_id, **args, "changes": [str(preview.config_path)]}

    def _session_apply(self, preview_id: str) -> dict[str, object]:
        if not preview_id.startswith("setup:") or len(preview_id) != 38:
            raise LiveSourceError("source_stale_preview")
        name = preview_id.replace(":", "-") + ".json"
        with self._setup.lock("setup"):
            value = self._setup.read(name)
            if not isinstance(value, dict) or not isinstance(value.get("arguments"), dict):
                raise LiveSourceError("source_stale_preview")
            args = value["arguments"]
            client, project = cast(HookClient, args["client"]), Path(args["project_path"])
            manager = AgentSessionHookManager(self.root / "sessions")
            removing = args["action"] == "remove"
            preview = (
                manager.preview_uninstall(client, project)
                if removing
                else manager.preview_apply(client, project)
            )
            if preview.before_sha256 != value["before_sha256"]:
                raise LiveSourceError("source_stale_preview")
            selection = AgentSessionLiveSource(self.root / "sessions").selection(
                client=client, project_path=project
            )
            source_id = self._source_id(selection)
            options = {
                key: args[key]
                for key in ("client", "project_path", "capture_summary", "capture_transcript")
            }
            if removing:
                options.update(capture_summary=False, capture_transcript=False)
            try:
                with manager.mutation(preview):
                    self.capture.configure(source_id, selection, options, reset=True, disable=True)
            except HookConfigError, OSError:
                raise LiveSourceError("source_hook_apply_failed") from None
            self._setup.delete(name)
            return {"status": "removed" if removing else "configured", "source_id": source_id}
