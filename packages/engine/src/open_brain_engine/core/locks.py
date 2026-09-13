"""Canonical lock vocabulary shared across engine implementations."""

from enum import StrEnum


class LockScope(StrEnum):
    NONE = "none"
    SHARED_WRITER = "shared-writer"
    INDEX = "index"
    PORTABILITY_PROMOTION = "portability-promotion"
