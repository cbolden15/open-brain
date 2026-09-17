"""Restricted owner recovery before ordinary local-engine bootstrap."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from open_brain_engine.storage.locks import LockBusyError

from open_brain.local_data import (
    FilesystemTypeProbe,
    LocalDataError,
    LocalRootSelection,
    PreparedLocalRoot,
)
from open_brain.profile import open_existing_single_user_local
from open_brain.services.local_runtime_session import (
    LocalRuntimeCompatibilityError,
    LocalRuntimeSession,
    hold_local_runtime_session,
)


class ManagedRecoveryCommandFailure(RuntimeError):
    """A bounded engine recovery failure safe for owner CLI projection."""

    def __init__(self, code: str, *, schema_upgraded: bool) -> None:
        super().__init__(code)
        self.code = code
        self.schema_upgraded = schema_upgraded


def run_managed_recovery(
    selection: LocalRootSelection,
    *,
    operation_id: str | None,
    after: str | None,
    limit: int,
    abandon: bool,
    expected_digest: str | None,
    request_id: str | None,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> dict[str, object]:
    """Inspect or abandon one legacy write without opening ordinary engine tasks."""
    if (
        not isinstance(selection, LocalRootSelection)
        or operation_id is not None
        and not isinstance(operation_id, str)
        or after is not None
        and not isinstance(after, str)
        or type(limit) is not int
        or not 1 <= limit <= 100
        or type(abandon) is not bool
        or expected_digest is not None
        and not isinstance(expected_digest, str)
        or request_id is not None
        and not isinstance(request_id, str)
    ):
        raise ValueError("invalid managed recovery request")

    from open_brain_engine.engine.managed_inference import recover_inference_sessions
    from open_brain_engine.engine.managed_recovery import (
        ManagedRecoveryFailure,
        abandon_managed_write,
        inspect_managed_recovery,
    )

    with _prepare_existing_local_root(
        selection, filesystem_type_probe=filesystem_type_probe
    ) as prepared:
        if not prepared.private_file_exists("brain.toml") or not prepared.private_file_exists(
            ".open-brain/state/phase1.sqlite3"
        ):
            raise LocalDataError("existing private Brain is required")
        prepared.revalidate()
        profile = open_existing_single_user_local(selection.brain_root)
        prepared.revalidate()
        schema_upgraded = False
        try:
            if not abandon:
                inspection = inspect_managed_recovery(
                    profile,
                    operation_id=operation_id,
                    after=after,
                    limit=limit,
                )
                prepared.revalidate()
                payload = inspection.to_dict()
                payload["schema_upgraded"] = False
                return payload

            if operation_id is None or expected_digest is None or request_id is None:
                raise ValueError("abandonment requires exact owner decision arguments")
            receipt: object | None = None

            def clean_inference_journal() -> int:
                return recover_inference_sessions(
                    profile,
                    validate_before_write=prepared.revalidate,
                )

            def admit_exclusive(runtime_session: LocalRuntimeSession) -> None:
                nonlocal receipt, schema_upgraded
                if runtime_session.live_peer_count != 0:
                    raise LocalRuntimeCompatibilityError(
                        "managed recovery requires exclusive runtime admission"
                    )
                receipt = abandon_managed_write(
                    profile,
                    operation_id=operation_id,
                    expected_digest=expected_digest,
                    request_id=request_id,
                    validate_before_write=prepared.revalidate,
                )
                schema_upgraded = bool(getattr(receipt, "schema_upgraded", False))

            with hold_local_runtime_session(
                profile.root,
                profile.root_identity,
                legacy_state_exists=True,
                recover_abandoned_sessions=clean_inference_journal,
                admit_session=admit_exclusive,
            ):
                if receipt is None:
                    raise RuntimeError("managed recovery admission did not complete")
            prepared.revalidate()
            to_dict = getattr(receipt, "to_dict", None)
            if not callable(to_dict):
                raise RuntimeError("managed recovery receipt is unavailable")
            return cast(dict[str, object], to_dict())
        except ManagedRecoveryFailure as error:
            raise ManagedRecoveryCommandFailure(
                error.code,
                schema_upgraded=error.schema_upgraded,
            ) from None
        except LocalRuntimeCompatibilityError:
            raise
        except LockBusyError:
            raise ManagedRecoveryCommandFailure(
                "database_busy", schema_upgraded=schema_upgraded
            ) from None
        except Exception:
            raise ManagedRecoveryCommandFailure(
                "recovery_unavailable",
                schema_upgraded=schema_upgraded,
            ) from None


@contextmanager
def _prepare_existing_local_root(
    selection: LocalRootSelection,
    *,
    filesystem_type_probe: FilesystemTypeProbe | None,
) -> Iterator[PreparedLocalRoot]:
    from open_brain import local_data

    probe = local_data._filesystem_type if filesystem_type_probe is None else filesystem_type_probe
    effective_uid = os.geteuid()
    root_fd, filesystem_type = local_data._open_target(
        selection,
        effective_uid=effective_uid,
        create=False,
        filesystem_type_probe=probe,
    )
    metadata = os.fstat(root_fd)
    prepared = PreparedLocalRoot(
        selection=selection,
        filesystem_type=filesystem_type,
        root_identity=(metadata.st_dev, metadata.st_ino),
        _root_fd=root_fd,
        _effective_uid=effective_uid,
        _filesystem_type_probe=probe,
    )
    try:
        yield prepared
    finally:
        prepared.close()


__all__ = ["ManagedRecoveryCommandFailure", "run_managed_recovery"]
