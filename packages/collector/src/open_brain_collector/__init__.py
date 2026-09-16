"""Optional unattended collector package for Open Brain."""

from open_brain_collector.boundary import (
    COLLECTOR_BOUNDARY,
    CollectorBoundary,
    CollectorBoundaryError,
    assert_collector_boundary,
)

__all__ = [
    "COLLECTOR_BOUNDARY",
    "CollectorBoundary",
    "CollectorBoundaryError",
    "assert_collector_boundary",
]
