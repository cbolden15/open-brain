from __future__ import annotations

import math

import pytest
from open_brain_engine.core.ids import canonical_json_bytes, portable_canonical_json_bytes


def test_canonical_json_bytes_normalizes_unicode_sorts_keys_and_has_no_whitespace() -> None:
    assert canonical_json_bytes({"z": "e\u0301", "a": [1, True, None]}) == (
        b'{"a":[1,true,null],"z":"\xc3\xa9"}'
    )


def test_legacy_canonical_json_bytes_preserves_float_and_vector_callers() -> None:
    assert canonical_json_bytes([0.5, -0.5]) == b"[0.5,-0.5]"


@pytest.mark.parametrize("value", [1.0, math.nan, math.inf, -math.inf])
def test_portable_canonical_json_bytes_rejects_floats(value: float) -> None:
    with pytest.raises(ValueError, match="floats"):
        portable_canonical_json_bytes({"value": value})


def test_portable_canonical_json_bytes_rejects_non_string_object_keys() -> None:
    with pytest.raises(ValueError, match="string object keys"):
        portable_canonical_json_bytes({1: "synthetic"})


def test_portable_canonical_json_bytes_rejects_nfc_normalized_key_collisions() -> None:
    with pytest.raises(ValueError, match="normalized key collision"):
        portable_canonical_json_bytes({"e\u0301": "first", "\u00e9": "second"})


def _assert_same(actual: object, expected: object) -> None:
    """Equal values with identical types at every level."""
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        assert list(actual) == list(expected)
        for key in expected:
            _assert_same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _assert_same(left, right)
    else:
        assert actual == expected


def _reference_normalize(value: object) -> object:
    """The 0.2.4 normalizer, kept to pin the faster one to identical results."""
    import unicodedata

    if isinstance(value, float):
        raise ValueError("Portable canonical JSON rejects floats")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list | tuple):
        return [_reference_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON requires string object keys")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("canonical JSON rejects normalized key collision")
            normalized[normalized_key] = _reference_normalize(item)
        return normalized
    return value


class _Text(str):
    pass


class _Number(int):
    pass


class _Mapping(dict):  # type: ignore[type-arg]
    pass


@pytest.mark.parametrize(
    "value",
    [
        {"a": "é", "b": ["x", 1, True, None, ("y", "Å")], "c": {"d": []}},
        ["plain ascii", "café", "café", "ﬁ", "", "\x7f"],
        ({"k": (1, 2, ({"z": None},))},),
        {_Text("é"): _Text("café"), "n": _Number(7), "m": _Mapping(x="é")},
        [b"bytes stay as they are", 10**30, -1, False],
        {"deep": [[[[["é"]]]]]},
    ],
)
def test_portable_normalizer_matches_the_reference(value: object) -> None:
    from open_brain_engine.core.ids import _normalize_portable_json

    _assert_same(_normalize_portable_json(value), _reference_normalize(value))


@pytest.mark.parametrize(
    "value",
    [
        [1, 2.5],
        {"a": {"b": [0.0]}},
        ("x", {"y": float("inf")}),
        {"é": 1, "é": 2},
        {"ok": {1: "non-string key"}},
        [_Mapping({2: "x"})],
    ],
)
def test_portable_normalizer_refuses_what_the_reference_refuses(value: object) -> None:
    from open_brain_engine.core.ids import _normalize_portable_json

    with pytest.raises(ValueError) as reference:
        _reference_normalize(value)
    with pytest.raises(ValueError) as faster:
        _normalize_portable_json(value)
    assert str(faster.value) == str(reference.value)


class _OnceList(list):  # type: ignore[type-arg]
    """A list that yields its items only on the first iteration."""

    def __init__(self, items: list[object]) -> None:
        super().__init__(items)
        self.spent = False

    def __iter__(self):  # type: ignore[no-untyped-def]
        if self.spent:
            return iter(())
        self.spent = True
        return super().__iter__()


class _Unhashable(type):
    __hash__ = None  # type: ignore[assignment]


class _OddInt(int, metaclass=_Unhashable):
    pass


def test_portable_normalizer_visits_each_value_once() -> None:
    from open_brain_engine.core.ids import portable_canonical_json_bytes

    value = {"a": _OnceList([7]), _Text("b"): None}
    assert portable_canonical_json_bytes(value) == b'{"a":[7],"b":null}'


def test_portable_normalizer_accepts_int_subclasses_with_unhashable_classes() -> None:
    from open_brain_engine.core.ids import portable_canonical_json_bytes

    assert portable_canonical_json_bytes([_OddInt(7)]) == b"[7]"


def test_portable_normalizer_keeps_the_reference_recursion_depth() -> None:
    from open_brain_engine.core.ids import _normalize_portable_json

    class Box(list):  # type: ignore[type-arg]
        pass

    value: object = 1
    for _ in range(600):
        value = Box([value])
    _assert_same(_normalize_portable_json(value), _reference_normalize(value))
