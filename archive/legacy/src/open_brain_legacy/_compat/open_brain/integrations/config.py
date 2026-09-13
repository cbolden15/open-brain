# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Fail-closed configuration for optional integrations."""

from __future__ import annotations

from dataclasses import dataclass, field

from .ports import Capability


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """Explicit enablement gates; live providers are disabled by default."""

    live_adapters: frozenset[Capability] = field(default_factory=frozenset)
    lifeos_external_writes_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.live_adapters, frozenset) or not all(
            isinstance(capability, Capability) for capability in self.live_adapters
        ):
            raise ValueError("invalid live integration configuration")
        if type(self.lifeos_external_writes_enabled) is not bool:
            raise ValueError("invalid LifeOS external write configuration")

    def live_adapter_enabled(self, capability: Capability) -> bool:
        if not isinstance(capability, Capability):
            raise ValueError("invalid integration capability")
        return capability in self.live_adapters

    def external_writes_enabled(self, capability: Capability) -> bool:
        if not isinstance(capability, Capability):
            raise ValueError("invalid integration capability")
        return (
            capability is Capability.LIFE_OS
            and self.live_adapter_enabled(Capability.LIFE_OS)
            and self.lifeos_external_writes_enabled
        )
