"""Compatibility imports for the app-owned local capability factory."""

from open_brain_legacy.services.application import (
    ProductionApplication,
    compose_production_application,
)

__all__ = ["ProductionApplication", "compose_production_application"]
