"""Bounded reuse of pure immutable decoding, never filesystem or authority checks."""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from threading import Lock
from types import MappingProxyType
from typing import Any, cast

_MAX_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 1024
_values: OrderedDict[tuple[Callable[[bytes], object], bytes], object] = OrderedDict()
_bytes = 0
_lock = Lock()


def _immutable(value: object) -> bool:
    if type(value) in (str, bytes, int, float, bool, type(None), date, datetime, timedelta):
        return True
    if isinstance(value, Enum):
        return _immutable(value.value)
    if type(value) in (tuple, frozenset):
        return all(_immutable(item) for item in cast(tuple[object, ...], value))
    if isinstance(value, MappingProxyType):
        return all(_immutable(key) and _immutable(item) for key, item in value.items())
    if is_dataclass(value) and not isinstance(value, type):
        return bool(cast(Any, value).__dataclass_params__.frozen) and all(
            _immutable(getattr(value, field.name)) for field in fields(value)
        )
    return False


def decode_historical_bytes[T](raw: bytes, decoder: Callable[[bytes], T]) -> T:
    """Caller must freshly validate/read confined bytes before every invocation.

    Only successful, transitively immutable values enter this byte-budgeted
    cache. Changing, removing or making a file unsafe cannot reuse an earlier
    read. Current registry, pending state and SQL checks remain the caller's job.
    """
    global _bytes
    key = (decoder, raw)
    with _lock:
        if key in _values:
            _values.move_to_end(key)
            return cast(T, _values[key])
    result = decoder(raw)
    if len(raw) > _MAX_BYTES or not _immutable(result):
        return result
    with _lock:
        if key in _values:
            _values.move_to_end(key)
            return cast(T, _values[key])
        while _values and (
            _bytes + len(raw) > _MAX_BYTES or len(_values) >= _MAX_ENTRIES
        ):
            (_, old), _ = _values.popitem(last=False)
            _bytes -= len(old)
        _values[key] = result
        _bytes += len(raw)
    return result
