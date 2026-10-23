"""Opt-in deterministic pytest shards for parallel CI jobs.

Load with ``-p tools.open_brain_dev.pytest_shard --shard INDEX/COUNT``. A SHA-256
of each node ID assigns it to one of COUNT shards, so shards 1 through COUNT of
the same collection together run every test exactly once. Selection keeps the
collected order. Without ``--shard`` the plugin selects every test.
"""

from __future__ import annotations

import hashlib

import pytest

PLUGIN = "tools.open_brain_dev.pytest_shard"
_SHARD = pytest.StashKey[tuple[int, int]]()


def parse_shard(value: str) -> tuple[int, int]:
    index, separator, count = value.partition("/")
    shard = (int(index), int(count)) if separator else (0, 0)
    if not 1 <= shard[0] <= shard[1]:
        raise ValueError(f"shard must be INDEX/COUNT with 1 <= INDEX <= COUNT, got {value!r}")
    return shard


def shard_of(nodeid: str, count: int) -> int:
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count + 1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="INDEX/COUNT",
        help="run only the tests in this 1-based shard of COUNT",
    )


def pytest_configure(config: pytest.Config) -> None:
    value = config.getoption("shard")
    if value is None:
        return
    try:
        config.stash[_SHARD] = parse_shard(value)
    except ValueError as error:
        raise pytest.UsageError(f"--shard must be INDEX/COUNT: {error}") from None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.stash.get(_SHARD, None)
    if shard is None:
        return
    index, count = shard
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        (selected if shard_of(item.nodeid, count) == index else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
