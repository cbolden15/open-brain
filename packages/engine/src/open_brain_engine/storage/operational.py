"""Public root-confined storage capabilities for bounded operational state."""

from .filesystem import (
    RootIdentity,
    StorageError,
    WriteState,
    atomic_replace,
    atomic_write_new,
    capture_root_identity,
    confined_unlink,
    read_confined,
    read_confined_tree,
)
from .locks import FileLease, LockBusyError, LockStateSnapshot, inspect_file_leases

__all__ = [
    "RootIdentity",
    "StorageError",
    "WriteState",
    "FileLease",
    "LockBusyError",
    "LockStateSnapshot",
    "atomic_replace",
    "atomic_write_new",
    "capture_root_identity",
    "confined_unlink",
    "inspect_file_leases",
    "read_confined",
    "read_confined_tree",
]
