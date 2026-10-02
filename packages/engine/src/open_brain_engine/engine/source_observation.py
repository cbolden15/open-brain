"""Closed adapter attestations bound to a managed source revision envelope."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .t03_contracts import T03Error


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRevisionObservation:
    original_sha256: str
    transformed_sha256: str
    normalization_version: str
    privacy_policy_version: str
    privacy_policy_sha256: str
    admitted_payload_sha256: str
    dto_version: int = 1

    def __post_init__(self) -> None:
        if type(self.dto_version) is not int or self.dto_version != 1:
            raise T03Error("invalid_arguments")
        for value in (
            self.original_sha256,
            self.transformed_sha256,
            self.privacy_policy_sha256,
            self.admitted_payload_sha256,
        ):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise T03Error("invalid_arguments")
        for value in (self.normalization_version, self.privacy_policy_version):
            if type(value) is not str or not 0 < len(value) <= 1024 or "\x00" in value:
                raise T03Error("invalid_arguments")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise T03Error("invalid_arguments") from None

    def value(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: object) -> SourceRevisionObservation:
        if type(value) is not dict or set(value) != {
            "dto_version",
            "original_sha256",
            "transformed_sha256",
            "normalization_version",
            "privacy_policy_version",
            "privacy_policy_sha256",
            "admitted_payload_sha256",
        }:
            raise T03Error("invalid_arguments")
        try:
            return cls(**value)
        except TypeError:
            raise T03Error("invalid_arguments") from None

    def validate_capture(self, capture: Mapping[str, Any]) -> None:
        privacy = capture.get("privacy")
        if (
            not isinstance(privacy, Mapping)
            or privacy.get("policy_version") != self.privacy_policy_version
            or sha256(portable_canonical_json_bytes(privacy)).hexdigest()
            != self.privacy_policy_sha256
            or sha256(portable_canonical_json_bytes(capture.get("payload"))).hexdigest()
            != self.admitted_payload_sha256
        ):
            raise T03Error("invalid_arguments")
