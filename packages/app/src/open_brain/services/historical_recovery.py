"""Restricted owner CLI recovery without ordinary engine startup."""

from pathlib import Path

from open_brain_engine.engine.historical_admission import HistoricalConsentSnapshot
from open_brain_engine.engine.historical_dispatch import (
    RelationRequest,
    require_historical_provider_consent,
)
from open_brain_engine.engine.historical_recovery import (
    open_historical_recovery_database_read_only,
    recover_historical_profile,
)
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.storage.filesystem import StorageError
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.sqlite import SchemaError

from open_brain.local_data import FilesystemTypeProbe, LocalDataError, LocalRootSelection
from open_brain.profile import open_existing_single_user_local
from open_brain.services.managed_recovery import _prepare_existing_local_root
from open_brain.services.session_consent import DurableProviderConsentStore, SessionConsentError


def run_historical_recovery(
    selection: LocalRootSelection,
    *,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
    consent_state_path: Path | None = None,
) -> dict[str, object]:
    """Finish only the already durable pending intent in the selected Brain.

    No request, actor, provider, evidence or replacement operation is accepted
    from command arguments. Trusted local owner authority is established from
    the existing private root, not from serialized caller authority.
    """
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
        authority = EffectiveAuthority(
            profile.owner_actor_id, "owner-historical-maintenance", frozenset(), None, owner=True
        )

        def validate_consent(request: RelationRequest) -> None:
            if consent_state_path is None:
                raise SharingError("unsupported_capability")
            try:
                state = DurableProviderConsentStore(
                    consent_state_path,
                    brain_id=request.destination.brain_id,
                    issuer_epoch=request.destination.issuer_epoch,
                ).load()
            except SessionConsentError:
                raise SharingError("unsupported_capability") from None
            require_historical_provider_consent(
                HistoricalConsentSnapshot(request.destination, state),
                request,
            )

        try:
            # Validated13 pending history must settle before its14 upgrade.
            # Unsupported older schemas still refuse before creating a registry lock.
            connection = open_historical_recovery_database_read_only(profile)
            connection.close()
            with exclusive_runtime_admission(profile) as admission:
                receipt = recover_historical_profile(
                    profile,
                    authority=authority,
                    admission=admission,
                    validate_before_write=prepared.revalidate,
                    validate_relation_consent=validate_consent,
                )
            prepared.revalidate()
        except LockBusyError, T03Error:
            raise SharingError("operation_pending") from None
        except SchemaError, StorageError:
            raise SharingError("binding_mismatch") from None
        return {
            "status": "settled" if receipt is None else "recovered",
            "receipt": None if receipt is None else receipt.value(),
        }
