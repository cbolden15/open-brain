"""Installed-package access to shared portability contracts."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import cast

_PACKAGE = "open_brain_engine.portability"


def shared_envelope_schema_bytes() -> bytes:
    return files(_PACKAGE).joinpath("schemas", "v1", "shared-record-envelope.json").read_bytes()


def load_shared_envelope_schema() -> dict[str, object]:
    value = json.loads(shared_envelope_schema_bytes())
    if not isinstance(value, dict):
        raise TypeError("shared record envelope schema must be an object")
    return cast(dict[str, object], value)


def load_conformance_cases() -> dict[str, object]:
    payload = files(_PACKAGE).joinpath("conformance", "v1", "cases.json").read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise TypeError("shared portability conformance cases must be an object")
    return cast(dict[str, object], value)


__all__ = ["load_conformance_cases", "load_shared_envelope_schema"]
