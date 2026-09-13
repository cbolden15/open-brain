from __future__ import annotations

from enum import StrEnum


class RuntimeFailureCode(StrEnum):
    DISABLED = "disabled"
    INVALID_INPUT = "invalid_input"
    CONFINEMENT = "confinement"
    INTEGRITY = "integrity"
    UNSUPPORTED_CONTROL = "unsupported_control"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    EXECUTION_FAILED = "execution_failed"


class ProductionRuntimeError(Exception):
    """Closed failure that intentionally excludes paths, commands, and host details."""

    def __init__(self, code: RuntimeFailureCode) -> None:
        self.code = code
        super().__init__(code.value)
