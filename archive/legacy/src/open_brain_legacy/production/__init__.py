"""Opt-in production adapters for security-sensitive runtime primitives."""

from .assets import (
    ContentAddressedDerivedAssetStore,
    ContentAddressedRawAssetStore,
    DerivedAssetRef,
)
from .errors import ProductionRuntimeError, RuntimeFailureCode
from .runtime import AssetReader, RuntimeLimits, StagedLocalModelRuntime
from .transport import DnsPinnedHttpTransport, PinnedConnectionFactory, SystemResolver

__all__ = [
    "AssetReader",
    "ContentAddressedDerivedAssetStore",
    "ContentAddressedRawAssetStore",
    "DerivedAssetRef",
    "DnsPinnedHttpTransport",
    "PinnedConnectionFactory",
    "ProductionRuntimeError",
    "RuntimeFailureCode",
    "RuntimeLimits",
    "StagedLocalModelRuntime",
    "SystemResolver",
]
