"""Installed-package access to Brain Protocol v1 schemas and conformance vectors."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, cast

_PACKAGE = "open_brain_engine.protocol"


def schema_catalog() -> tuple[str, ...]:
    root = files(_PACKAGE).joinpath("schemas", "v1")
    return tuple(sorted(item.name.removesuffix(".json") for item in root.iterdir()))


def load_schema(name: str) -> dict[str, object]:
    if name not in schema_catalog():
        raise ValueError(f"unknown Brain Protocol v1 schema: {name}")
    payload = files(_PACKAGE).joinpath("schemas", "v1", f"{name}.json").read_text("utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise TypeError("protocol schema must be an object")
    return cast(dict[str, object], value)


def load_conformance_cases() -> dict[str, dict[str, list[object]]]:
    payload = files(_PACKAGE).joinpath("conformance", "v1", "cases.json").read_text("utf-8")
    return cast(dict[str, dict[str, list[object]]], json.loads(payload))


def load_signature_vectors() -> list[dict[str, Any]]:
    payload = files(_PACKAGE).joinpath("conformance", "v1", "signatures.json").read_text("utf-8")
    return cast(list[dict[str, Any]], json.loads(payload))
