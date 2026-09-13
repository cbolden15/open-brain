"""Shared record and lossless upgrade boundary for Open Brain product profiles."""

from .limits import PORTABLE_BLOB_STAGING_BYTES
from .model import (
    PortabilityMappingError,
    SharedAttachment,
    SharedBlob,
    SharedBrain,
    SharedImportEvidence,
    SharedRecord,
)
from .portable_v1 import shared_brain_from_snapshot
from .resources import load_conformance_cases, load_shared_envelope_schema

__all__ = [
    "PORTABLE_BLOB_STAGING_BYTES",
    "PortabilityMappingError",
    "SharedAttachment",
    "SharedBlob",
    "SharedBrain",
    "SharedImportEvidence",
    "SharedRecord",
    "load_conformance_cases",
    "load_shared_envelope_schema",
    "shared_brain_from_snapshot",
]
