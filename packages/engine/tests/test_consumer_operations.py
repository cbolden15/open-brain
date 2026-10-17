"""Adversarial accepted-operation graphs bind every prospective input/capture."""

from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.consumer_intake import _successor
from open_brain_engine.engine.consumer_operations import PreparedConsumerOperation
from open_brain_engine.engine.consumer_profiles import ConsumerIntakeError

from open_brain.profile import compile_single_user_local
from packages.engine.tests.test_consumer_intake_engine import (
    _conversation,
    _owner,
    _plan,
)
from packages.engine.tests.test_consumer_intake_engine import (
    active_profiles as active_profiles,
)


def _prepared(tmp_path: Path, *, empty: bool = False) -> PreparedConsumerOperation:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan(conversations=(replace(_conversation(), messages=()),)) if empty else _plan()
    preview = engine.consumer_imports.preview_consumer_import(plan, authority=_owner(engine))
    with closing(engine._store.connect()) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        prepared, _ = engine.consumer_imports._preview(
            _successor(connection),
            cast(str, preview["preview_id"]),
            _owner(engine),
        )
        return prepared


@pytest.mark.parametrize("empty", [False, True])
def test_complete_operation_roundtrip_and_exact_object_loader(
    tmp_path: Path,
    active_profiles: None,
    empty: bool,
) -> None:
    prepared = _prepared(tmp_path, empty=empty)
    assert (
        PreparedConsumerOperation.from_loader(prepared.raw, prepared.payloads.__getitem__)
        == prepared
    )
    assert all(sha256(raw).hexdigest() == key for key, (_, raw) in prepared.inventory().items())
    assert (
        cast(Mapping[str, object], prepared.request.payload["consumer"])["intent_sha256"]
        == prepared.intent_sha256
    )
    payloads = dict(prepared.payloads)
    payloads.clear()
    assert prepared.payloads


@pytest.mark.parametrize("flag", [0, 1, "false", None])
def test_root_replacement_flag_is_strict_boolean(
    tmp_path: Path,
    active_profiles: None,
    flag: object,
) -> None:
    prepared = _prepared(tmp_path, empty=True)
    value = prepared.value()
    value["replace_current"] = flag
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        PreparedConsumerOperation.from_values(value, prepared.payloads)


@pytest.mark.parametrize("label", ["bad\nlabel", "bad\x7flabel", "bad\u202elabel", "", "x" * 121])
def test_existing_scope_labels_share_complete_input_grammar(
    tmp_path: Path,
    active_profiles: None,
    label: str,
) -> None:
    prepared = _prepared(tmp_path, empty=True)
    value = prepared.value()
    # Label validation is independent of whether the scope was proposed or exists.
    scope = cast(dict[str, object], value["scope"])
    scope["label"] = label
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        PreparedConsumerOperation.from_values(value, prepared.payloads)


def test_orphan_missing_and_rehashed_identity_substitution_refuse(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    prepared = _prepared(tmp_path)
    objects = dict(prepared.payloads)
    objects[sha256(b"{}").hexdigest()] = b"{}"
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        PreparedConsumerOperation(prepared.raw, objects)
    objects = dict(prepared.payloads)
    item = prepared.items[0]
    conversation = prepared.conversation(item)
    assert conversation is not None
    identity = cast(str, conversation.value()["provider_identity_sha256"])
    del objects[identity]
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        PreparedConsumerOperation(prepared.raw, objects)
    value = prepared.value()
    cast(list[dict[str, object]], value["items"])[0]["input_sha256"] = "f" * 64
    with pytest.raises(ConsumerIntakeError, match="invalid_evidence"):
        PreparedConsumerOperation.from_values(value, prepared.payloads)
