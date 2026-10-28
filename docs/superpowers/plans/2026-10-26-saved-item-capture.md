# Saved Item Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture any URL the owner saves (with the reason they saved it) from producer-written JSON files in one selected folder, through the optional collector and custody, as searchable third-party records, with no engine, schema, Portable Brain, listener or dependency change.

**Architecture:** A pure connector adapter (`runtime/saved_item.py`) validates version 1 records, computes the identity URL and `item_key`, masks text and renders a bounded `SourceRecordIntake`. Three collector modules read the folder through one no-follow descriptor (`saved_item_folder.py`), keep a fixed-size known-file index in its own `PrivateJsonStore` file (`saved_item_index.py`), and turn one scan into one `LiveBatch` page (`saved_item_live.py`) that `PriorityRuntime.fetch`/`acknowledge` dispatch to, with `agent_session` as the template. The index advances only in `acknowledge`, after the checkpoint commit. One new collector CLI step (`sources saved-item-select`) turns a folder path into the `configure` arguments; the existing `configure`, `preview`, `import`, `control` and `run-once` commands drive the source.

**Tech Stack:** Python 3.14, uv workspace, pytest (synthetic fixtures, `tmp_path`, `monkeypatch`), ruff (E, F, I, UP, B, SIM; line length 100), mypy strict. Engine helpers already in the repo: `canonicalize_source_url`, `redact_text`, `has_redaction_finding`, `ReferencePayload`, `SourceType`, `ContentKind`.

Spec: `docs/superpowers/specs/2026-10-26-saved-item-capture-design.md` (revision 5, `4105864`). Every code block below was applied mechanically to a `git archive` export of this branch's HEAD: `ruff check .` and `mypy` were clean, and the full suite gave 9,671 passed; its only failures (2 failed, 26 errors) were the harness-acceptance tests, which call `git rev-parse HEAD` and cannot run in a non-git export. They pass on the real checkout.

## Spec deviations

1. **`url_secret` checks the query that survives tracking removal, not the raw query.** The spec says "the raw URL's query and fragment ... are checked with `has_redaction_finding`". Evidence: `_SECRET_SHAPED_TOKEN` in `packages/engine/src/open_brain_engine/capture/redaction.py:34` matches any run of 32 or more `[A-Za-z0-9_-]`, and measured on this branch `has_redaction_finding("fbclid=IwAR2abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")` is `True`. `fbclid` and `gclid` values are routinely longer than 32 characters, so the raw check would refuse most Facebook-shared and ad-click links, the very parameters the spec removes and never stores or hashes. Resolution: `identity_url` removes tracking parameters first, then checks the remaining query and the full fragment (both of which enter the identity hash). `token=` in a query or fragment is still refused, and so is any other long run that survives removal. Recorded as gotcha CAPTURE-004.
2. **An intake in a committed page is indexed as `delivered` even when custody quarantined it.** The spec lists the terminal custody receipts as "captured or duplicate". Evidence: `PriorityRuntime.acknowledge(selection, checkpoint)` (`live_manager.py:140-145`) receives no receipt outcomes; `LiveCaptureService.apply` calls `self._custody.validate_terminal(receipt_ids)` before it commits the checkpoint and only then calls `_acknowledge` (`live_capture.py:858-895`), so every receipt is terminal, `quarantined` included. A quarantined item (a changed record after index loss) is owned by the existing collector recovery. Leaving it unindexed would put the same intake back on every page and hold a page slot forever. Resolution: `acknowledge` records every intake of the committed page as `delivered`.
3. **Bounds the spec does not state, needed to keep its stated bounds.** (a) A page records at most 500 local outcomes (refusals and first-save-wins skips) in the checkpoint, then sets `has_more`. The proposed outcomes must ride in the checkpoint until `acknowledge`, and the checkpoint is capped at 131,072 bytes; measured, 500 outcomes plus 25 deliveries encode to 49,718 bytes. (b) The index evicts its oldest file entries past 10,000 and its oldest delivered `item_key`s past 10,000, the counts the spec's size bound assumes. An evicted `item_key` falls back to custody dedupe, the spec's own "index lost" behavior. (c) The stat fingerprint is stored as the first 24 hex characters of the SHA-256 of `"<st_dev>:<st_ino>:<st_size>:<st_mtime_ns>"`, not as four integers, so every entry is fixed-size. Measured worst case for 10,000 entries plus 10,000 delivered keys: 1,500,046 bytes, under 2,097,152.
4. **Truncated sections keep a marker.** The spec says a record is refused if it "still does not fit with those sections empty". The adapter reduces `Media text`, `Body` and `Shared text` to the `[truncated]` marker, not to nothing, so the reader can see content was dropped. It refuses as `payload_too_large` if the record does not fit with all three at the marker. The difference is at most 3 × 11 characters.

## Global Constraints

- Coordinates stay **23/16/18**. No engine, schema, runtime-session or Portable Brain change. No new dependency, listener, network path or packaging change. The only core-package edit is one `OPTIONAL_SOURCE_DESCRIPTORS` entry in `packages/app/src/open_brain/services/catalog.py`, and `catalog.py` must not import `open_brain_connectors` or `open_brain_collector` (pinned by `packages/app/tests/unit/test_catalog.py:523-536`).
- Connector name `saved_item`; descriptor display name `Saved Items`; auth mode `session_only`; `resource_types == ("local_folder",)`; `content_types == ("saved_item",)`; `preview_limit == 25`; `public_onboarding is False`. App catalog entry `("saved_item", "saved items")` between `("notion", "Notion")` and `("slack", "Slack")`.
- Record format v1: one UTF-8 JSON object per `*.json` file, **at most 131,072 bytes**. Required `schema_version` (int `1`), `url` (`https` only), `why` (after trimming 1 to 280 characters, one line, no `Cc`/`Zl`/`Zp` characters), `received_at` (`YYYY-MM-DDTHH:MM:SS[.1-9 digits]Z`, a real UTC time), `fetch_status` (`not_attempted`, `complete`, `partial`, `failed`). Optional `source_type` (engine `SourceType` value or null; null derives `youtube` for `youtube.com`/`youtu.be`, `social` for `bsky.app`, `facebook.com`, `instagram.com`, `linkedin.com`, `reddit.com`, `threads.com`, `threads.net`, `tiktok.com`, `twitter.com`, `x.com` and their subdomains, else `web`), `content_kind` (engine `ContentKind` value; null means `other`), `platform` (`[a-z0-9][a-z0-9 ._+-]{0,39}`), `title` (at most **300**), `author` (at most 200), `body`, `media_text`, `shared_text` (strings, default `""`), `summary`, `summary_origin` (both or neither; origin `producer:[a-z0-9][a-z0-9._-]{0,63}`). Unknown fields refused. No NUL in any string.
- Identity: `urlsplit`, `https` only, then `canonicalize_source_url` on the URL without fragment, then remove tracking parameters, then append `#<fragment>` with percent escapes uppercased. `item_key` = first 32 hex of SHA-256 of the identity URL. Stored intake URL = identity without fragment. Tracking rule version **1**, parameter names compared lowercased:

  | Hosts (and subdomains) | Removed parameters |
  |---|---|
  | Any | `utm_*`, `fbclid`, `gclid` |
  | `instagram.com`, `threads.net`, `threads.com` | `igsh`, `igshid` |
  | `youtube.com`, `youtu.be`, `spotify.com` | `si` |

- Intake: key `SourceRecordKey("saved_item", selection.connection_id, selection.resource_id, item_key, "1")`; `privacy=local_source_privacy()` (the tier travels as the `privacy_tier` option and `capture_policy.admitted_intake` applies it); title = record title, else `<author> on <platform>`, else stored URL, masked, whitespace-collapsed and cut to **200** characters. Text sections in order `Stated reason`, `Summary (<origin>)`, `Body`, `Media text`, `Shared text`, `Kind` (`source_type: <v>; content_kind: <v>[; platform: <v>]`), `Fetch status`, each rendered `<heading>\n<body>`, joined by a blank line, empty sections omitted, every body masked with `redact_text` and stripped before measuring.
- Bounds: rendered text at most **60,000** characters and `bounded_json(ReferencePayload(url, text).to_dict())` at most **65,536** encoded bytes (ASCII-escaped JSON, so one non-Latin character costs 6 bytes). Past either, truncate `Media text`, then `Body`, then `Shared text` to the longest prefix that fits, followed by `\n[truncated]`; still too large refuses `payload_too_large`. Page: at most **25** intakes and at most **1,048,576** bytes of `encoded_intake_size`; custody quota 2,097,152 bytes; checkpoint 131,072 bytes; index file under **2,097,152** bytes (half of `PrivateJsonStore`'s 4,194,304); at most **10,000** `*.json` entries per folder, else `folder_over_limit`.
- Record refusal codes (`RECORD_REFUSAL_CODES`): `file_too_large`, `invalid_field`, `invalid_json`, `invalid_url`, `invalid_why`, `missing_field`, `payload_too_large`, `unknown_field`, `url_secret`. Entry codes: `not_regular_file`, `already_captured`. Folder errors (`LiveSourceError` codes, nothing delivered): `folder_missing`, `folder_unsafe`, `folder_over_limit`. Non-terminal skips (retried next scan): `placeholder`, `unstable`, `vanished`, `deferred_duplicate`.
- Folder: opened once with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`; `fstat` must show a directory owned by `os.getuid()` with no group/other write bit. Entries come from `os.scandir(fd)`; names starting with `.` or ending in `.icloud`, `.tmp`, `.partial` are skipped; each `*.json` is `lstat`ed via `dir_fd`, skipped if `st_flags & SF_DATALESS` (`getattr(info, "st_flags", 0)`, `SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)`), skipped without opening if its fingerprint matches the index, refused if not regular, else opened with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` relative to the folder descriptor, `fstat`-checked, read up to 131,073 bytes, and accepted only if device, inode, size and `mtime_ns` match across `lstat`, `fstat` before and `fstat` after. Selection: `connection_id = "folder:" + h[:32]`, `resource_id = "items:" + h[32:]`, `h = sha256(bounded_json([str(resolved_path), st_dev, st_ino])).hexdigest()`, `resource_type = "local_folder"`.
- Index file `index-<sha256(resource_id)[:32]>.json` under `<live root>/saved_items`: `{"schema_version": 1, "entries": {<name_hash32>: "<fingerprint24>:<state>"}, "delivered": {<item_key32>: <content_digest32>}}`. Terminal states: `delivered`, `skipped`, `already_captured`, `not_regular_file` and every record refusal code. Pruning of vanished names runs in `acknowledge` when the page completed the scan.
- Checkpoint: `{"schema_version": 1, "folder_path": "<resolved>", "complete": bool, "outcomes": [[name_hash, fingerprint, state, item_key_or_"", content_or_""], ...]}`, at most 525 outcomes. Options: `{"folder_path": "<resolved>", "privacy_tier": "<tier>"}`; the runtime sees only `folder_path`.
- Within a page one intake per `item_key`: earliest `received_at` (compared as datetimes), then lowest name hash; losers set `has_more` and resolve by first-save-wins on the next page. Notices (at most 25): `saved_item_count:<outcome>:<n>` sorted by outcome, then `saved_item_refused:<code>:<name_hash[:12]>`. Raw entry names never appear in notices, errors or logs.
- CLI: `open-brain-collector sources ... saved-item-select --folder-path PATH [--privacy-tier {public,work,personal,secret}]` (default `personal`) dispatches `sources.saved_item_select` and prints `{"selection": {...}, "options": {...}}`, the exact `configure --arguments-file` content. `saved_item` is not added to `_REVISION_CONNECTORS` or any revision allow-list.
- Tests use synthetic fixtures only: `tmp_path` folders, `example.com`-style URLs, no network, no real Brain, credentials, hostnames or private paths. Commit messages go in a file and are committed with `git commit -F`; every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Before each commit run `uv run --frozen ruff check --fix <touched .py files>` so import order matches `[tool.ruff.lint.isort]` (`open_brain_engine` third-party; `open_brain`, `open_brain_collector`, `open_brain_connectors` first-party).

## File structure

| Path | Action | Responsibility |
|---|---|---|
| `packages/connectors/src/open_brain_connectors/runtime/saved_item.py` | Create (Tasks 1, 2, 4) | Identity URL and tracking table, record validation, intake rendering, masking, truncation, size and content digests |
| `packages/connectors/tests/contract/test_saved_item.py` | Create (Tasks 1, 2, 4) | Adapter contract tests |
| `packages/connectors/src/open_brain_connectors/runtime/source_registry.py` | Modify (Task 3) | `SAVED_ITEM_SOURCE`, `saved_item_source_descriptor()` |
| `packages/connectors/src/open_brain_connectors/runtime/source_cli.py` | Modify (Task 3) | Catalog tuple entry |
| `packages/connectors/tests/contract/test_source_registry.py`, `test_source_cli.py` | Modify (Task 3) | Catalog pins |
| `packages/app/src/open_brain/services/catalog.py`, `packages/app/tests/unit/test_catalog.py` | Modify (Task 3) | Optional-source entry and its test |
| `packages/collector/src/open_brain_collector/saved_item_folder.py` | Create (Task 5) | Folder selection, descriptor-anchored listing and entry reads |
| `packages/collector/tests/contract/test_saved_item_folder.py` | Create (Task 5) | Reader tests |
| `packages/collector/src/open_brain_collector/saved_item_index.py` | Create (Task 6) | Known-file index value, load and save |
| `packages/collector/tests/contract/test_saved_item_index.py` | Create (Task 6) | Index tests including the 10,000-entry size test |
| `packages/collector/src/open_brain_collector/saved_item_live.py` | Create (Task 7) | `SavedItemLiveSource.fetch`/`acknowledge` |
| `packages/collector/src/open_brain_collector/live_capture.py`, `live_manager.py` | Modify (Tasks 7, 8) | `_PROVIDERS`, runtime dispatch, `sources.saved_item_select` |
| `packages/collector/tests/integration/test_saved_item_live.py` | Create (Task 7) | Paging, first-save-wins, index ordering, folder safety |
| `packages/collector/tests/integration/test_live_capture.py` | Modify (Task 7) | `_PROVIDERS` pin |
| `packages/collector/src/open_brain_collector/sources_cli.py` | Modify (Task 8) | `saved-item-select` subcommand |
| `packages/collector/tests/integration/test_live_manager.py` | Modify (Task 8) | Select operation test |
| `packages/collector/tests/integration/test_saved_item_capture.py` | Create (Task 8) | CLI to temporary Brain to search, plus scheduled run |
| `docs/integrations/saved-items.md`, `tests/release/test_saved_items_docs.py`, `CLAUDE.md`, `docs/engineering/gotchas/README.md` | Create/Modify (Task 9) | Integration guide, doc test, contributor line, CAPTURE-004 |

## Verified baseline facts

- `SourceRecordIntake` caps text at 65,536 characters and title at 200 (`source_intake.py:28-29`); `ReferencePayload` canonicalizes the URL and NFC-normalizes text (`contracts.py:201-213`).
- The plain capture path prefixes `Source revision: <64 hex>\n\n` (83 characters) and refuses past 65,536 characters (`live_capture.py:653-655`).
- `LiveBatch` allows at most 25 intakes and 25 notices matching `[a-z][a-z0-9_]{0,99}(?::[A-Za-z0-9_-]{1,128}){0,2}`, and bounds the checkpoint at 131,072 bytes (`live_common.py:14, 52-75`).
- `LiveCaptureService.preview` refuses any intake whose privacy is not `local_source_privacy()` before `admitted_intake` applies the tier (`live_capture.py:520-530`); `acknowledge` runs after the checkpoint commit and again at the start of the next preview (`live_capture.py:504-506, 886-895`).
- `PriorityRuntime.fetch`/`acknowledge` dispatch by `if` on `connector_name` (`live_manager.py:122-145`); `dispatch` ends with a fall-through to `sources.session_apply` (`live_manager.py:368-371`), so new branches go before `sources.session_preview`.
- `revision_store_dirname(name)` returns `None` for connectors outside `_REVISION_CONNECTORS` (`capture_policy.py:72-81`).
- `os.open(name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK, dir_fd=fd)` returns at once on a FIFO and fails with `ELOOP` on a symlink (measured on macOS).

---

<!-- model: opus -->
### Task 1: Identity URL, tracking table and `item_key`

**Files:**
- Create: `packages/connectors/src/open_brain_connectors/runtime/saved_item.py`
- Create: `packages/connectors/tests/contract/test_saved_item.py`

**Interfaces:**
- Consumes: `canonicalize_source_url(value: str) -> str` (`open_brain_engine.core.ids`, raises `UrlError`, a `ValueError`); `has_redaction_finding(value: str) -> bool` (`open_brain_engine.capture.redaction`).
- Produces (in `saved_item.py`):
  - `TRACKING_RULE_VERSION: int = 1`
  - `TRACKING_RULES: tuple[tuple[tuple[str, ...], frozenset[str], tuple[str, ...]], ...]` (hosts, exact names, name prefixes)
  - `class SavedItemRefusal(Exception)` with attribute `code: str`
  - `identity_url(raw: object) -> tuple[str, str]` returning `(identity_url, stored_url)`; raises `SavedItemRefusal("invalid_url")` or `SavedItemRefusal("url_secret")`
  - `item_key(identity: str) -> str` (32 lowercase hex)
  - private `_host_matches(host: str, hosts: tuple[str, ...]) -> bool` (reused by Task 2)

- [ ] **Step 1: Write the failing tests**

Create `packages/connectors/tests/contract/test_saved_item.py`:

```python
from __future__ import annotations

import re

import pytest

from open_brain_connectors.runtime.saved_item import (
    TRACKING_RULE_VERSION,
    TRACKING_RULES,
    SavedItemRefusal,
    identity_url,
    item_key,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.example.com/a?utm_source=x&UTM_Medium=y&fbclid=abc&gclid=def"
            "&ref=keep&si=keep#frag",
            "https://www.example.com/a?ref=keep&si=keep#frag",
        ),
        ("https://www.instagram.com/p/abc/?igsh=xyz&igshid=q", "https://www.instagram.com/p/abc/"),
        (
            "https://www.threads.net/@user/post/1?igshid=1&x=2",
            "https://www.threads.net/@user/post/1?x=2",
        ),
        ("https://threads.com/@u/post/2?igsh=1", "https://threads.com/@u/post/2"),
        ("https://m.youtube.com/watch?v=abc&si=xyz", "https://m.youtube.com/watch?v=abc"),
        ("https://youtu.be/abc?si=xyz", "https://youtu.be/abc"),
        ("https://open.spotify.com/track/1?si=x", "https://open.spotify.com/track/1"),
        ("https://example.com/a?igsh=1&si=2&ref=3", "https://example.com/a?igsh=1&si=2&ref=3"),
        ("https://notyoutube.com/v?si=1", "https://notyoutube.com/v?si=1"),
        ("https://Example.COM:443/a%2fb?x=%2f#Frag%2f", "https://example.com/a%2Fb?x=%2F#Frag%2F"),
    ],
)
def test_identity_applies_each_tracking_row_with_subdomains(raw: str, expected: str) -> None:
    identity, stored = identity_url(raw)
    assert identity == expected
    assert stored == expected.partition("#")[0]
    assert "#" not in stored


def test_tracking_table_is_versioned() -> None:
    assert TRACKING_RULE_VERSION == 1
    assert (
        ((), frozenset({"fbclid", "gclid"}), ("utm_",)),
        (("instagram.com", "threads.net", "threads.com"), frozenset({"igsh", "igshid"}), ()),
        (("youtube.com", "youtu.be", "spotify.com"), frozenset({"si"}), ()),
    ) == TRACKING_RULES


def test_fragment_is_identity_input_but_never_stored() -> None:
    first, first_stored = identity_url("https://app.example/#/note/123")
    second, second_stored = identity_url("https://app.example/#/note/456")
    assert first_stored == second_stored == "https://app.example/"
    assert item_key(first) != item_key(second)
    assert re.fullmatch(r"[0-9a-f]{32}", item_key(first))
    assert item_key(identity_url("https://example.com/a?utm_source=x")[0]) == item_key(
        identity_url("https://example.com/a")[0]
    )


def test_long_path_ids_and_removed_tracking_values_are_admitted() -> None:
    path_id = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"
    assert len(path_id) == 44
    assert identity_url(f"https://example.com/{path_id}")[1] == f"https://example.com/{path_id}"
    fbclid = "IwAR2" + "x" * 50
    assert identity_url(f"https://example.com/a?fbclid={fbclid}") == (
        "https://example.com/a",
        "https://example.com/a",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/a?token=abc",
        "https://example.com/a#access_token=abc",
        "https://example.com/a?id=" + "0123456789abcdef" * 3,
    ],
)
def test_secret_shaped_query_or_fragment_is_refused(raw: str) -> None:
    with pytest.raises(SavedItemRefusal) as refusal:
        identity_url(raw)
    assert refusal.value.code == "url_secret"


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com/a",
        "ftp://example.com/a",
        "https://user@example.com/",
        "https://",
        "not a url",
        7,
        None,
    ],
)
def test_non_https_or_malformed_urls_are_refused(raw: object) -> None:
    with pytest.raises(SavedItemRefusal) as refusal:
        identity_url(raw)
    assert refusal.value.code == "invalid_url"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_saved_item.py::test_identity_applies_each_tracking_row_with_subdomains packages/connectors/tests/contract/test_saved_item.py::test_secret_shaped_query_or_fragment_is_refused`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'open_brain_connectors.runtime.saved_item'`.

- [ ] **Step 3: Write the module**

Create `packages/connectors/src/open_brain_connectors/runtime/saved_item.py`:

```python
"""Saved item records: one producer-written JSON file per URL the owner kept."""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import canonicalize_source_url

__all__ = [
    "TRACKING_RULES",
    "TRACKING_RULE_VERSION",
    "SavedItemRefusal",
    "identity_url",
    "item_key",
]

TRACKING_RULE_VERSION = 1
# (hosts matched with their subdomains, () for any host; exact names; name prefixes).
TRACKING_RULES: tuple[tuple[tuple[str, ...], frozenset[str], tuple[str, ...]], ...] = (
    ((), frozenset({"fbclid", "gclid"}), ("utm_",)),
    (("instagram.com", "threads.net", "threads.com"), frozenset({"igsh", "igshid"}), ()),
    (("youtube.com", "youtu.be", "spotify.com"), frozenset({"si"}), ()),
)
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


class SavedItemRefusal(Exception):
    """A record that stays local, named by a stable refusal code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _host_matches(host: str, hosts: tuple[str, ...]) -> bool:
    return not hosts or any(host == item or host.endswith("." + item) for item in hosts)


def _is_tracking(host: str, name: str) -> bool:
    lowered = name.lower()
    return any(
        _host_matches(host, hosts) and (lowered in names or lowered.startswith(prefixes))
        for hosts, names, prefixes in TRACKING_RULES
    )


def identity_url(raw: object) -> tuple[str, str]:
    """Return ``(identity URL, stored URL)``; the stored URL never carries a fragment."""
    if type(raw) is not str:
        raise SavedItemRefusal("invalid_url")
    try:
        parts = urlsplit(raw)
        https = parts.scheme.lower() == "https"
        base = canonicalize_source_url(urlunsplit(parts._replace(fragment=""))) if https else ""
    except ValueError:
        raise SavedItemRefusal("invalid_url") from None
    if not https:
        raise SavedItemRefusal("invalid_url")
    split = urlsplit(base)
    host = split.hostname or ""
    query = "&".join(
        pair
        for pair in split.query.split("&")
        if pair and not _is_tracking(host, pair.partition("=")[0])
    )
    fragment = _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), parts.fragment)
    # Removed tracking values are never stored or hashed, so only what survives is checked.
    if has_redaction_finding(query) or has_redaction_finding(fragment):
        raise SavedItemRefusal("url_secret")
    stored = urlunsplit(split._replace(query=query))
    return stored + ("#" + fragment if fragment else ""), stored


def item_key(identity: str) -> str:
    return sha256(identity.encode("utf-8")).hexdigest()[:32]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_saved_item.py`
Expected: PASS (6 test functions, 23 parametrized cases in total).

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/connectors/src/open_brain_connectors/runtime/saved_item.py packages/connectors/tests/contract/test_saved_item.py
uv run --frozen mypy packages/connectors/src/open_brain_connectors/runtime/saved_item.py packages/connectors/tests/contract/test_saved_item.py
git add packages/connectors/src/open_brain_connectors/runtime/saved_item.py \
  packages/connectors/tests/contract/test_saved_item.py
printf 'feat(connectors): saved item identity URL and tracking table\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: sonnet:high -->
### Task 2: Record validation (`SavedItemRecord`, `parse_record`)

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/saved_item.py` (import block, `__all__`, append after `item_key`)
- Modify: `packages/connectors/tests/contract/test_saved_item.py` (import block, append tests)

**Interfaces:**
- Consumes: Task 1 `identity_url`, `item_key`, `_host_matches`, `SavedItemRefusal`; `SourceType`, `ContentKind` (`open_brain_engine.core.models`).
- Produces:
  - `MAX_RECORD_BYTES = 131_072`, `MAX_WHY_CHARS = 280`, `MAX_RECORD_TITLE_CHARS = 300`, `MAX_AUTHOR_CHARS = 200`
  - `FETCH_STATUSES: frozenset[str]`, `RECORD_REFUSAL_CODES: frozenset[str]` (the nine codes in Global Constraints)
  - `@dataclass(frozen=True, slots=True) class SavedItemRecord` with fields `identity_url: str, stored_url: str, item_key: str, why: str, received_at: datetime, fetch_status: str, source_type: SourceType, content_kind: ContentKind, platform: str | None, title: str | None, author: str | None, body: str, media_text: str, shared_text: str, summary: str | None, summary_origin: str | None`
  - `parse_record(raw: bytes) -> SavedItemRecord`; raises `SavedItemRefusal(code)`

- [ ] **Step 1: Write the failing tests**

Replace the import block of `packages/connectors/tests/contract/test_saved_item.py` with:

```python
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from open_brain_engine.core.models import ContentKind, SourceType

from open_brain_connectors.runtime.saved_item import (
    MAX_RECORD_BYTES,
    RECORD_REFUSAL_CODES,
    TRACKING_RULE_VERSION,
    TRACKING_RULES,
    SavedItemRecord,
    SavedItemRefusal,
    identity_url,
    item_key,
    parse_record,
)
```

Append to the same file:

```python
THIN: dict[str, object] = {
    "schema_version": 1,
    "url": "https://example.com/post/123",
    "why": "Recipe to try this weekend",
    "received_at": "2026-10-26T14:00:00Z",
    "fetch_status": "not_attempted",
}
RICH: dict[str, object] = {
    **THIN,
    "url": "https://www.instagram.com/p/abc/?igsh=xyz",
    "why": "  Compare with the other recipe  ",
    "received_at": "2026-10-26T14:00:00.123456789Z",
    "fetch_status": "complete",
    "source_type": "social",
    "content_kind": "post",
    "platform": "instagram",
    "title": "Pasta night",
    "author": "Ada",
    "body": "Full caption text.",
    "media_text": "Transcript words.",
    "shared_text": "Shared by a friend.",
    "summary": "Short summary.",
    "summary_origin": "producer:shortcut",
}


def _raw(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _refusal(raw: bytes) -> str:
    with pytest.raises(SavedItemRefusal) as refusal:
        parse_record(raw)
    return refusal.value.code


def test_thin_record_maps_required_fields_and_defaults() -> None:
    assert parse_record(_raw(THIN)) == SavedItemRecord(
        identity_url="https://example.com/post/123",
        stored_url="https://example.com/post/123",
        item_key=item_key("https://example.com/post/123"),
        why="Recipe to try this weekend",
        received_at=datetime(2026, 10, 26, 14, 0, tzinfo=UTC),
        fetch_status="not_attempted",
        source_type=SourceType.WEB,
        content_kind=ContentKind.OTHER,
        platform=None,
        title=None,
        author=None,
        body="",
        media_text="",
        shared_text="",
        summary=None,
        summary_origin=None,
    )


def test_rich_record_keeps_every_field() -> None:
    record = parse_record(_raw(RICH))
    assert record.stored_url == "https://www.instagram.com/p/abc/"
    assert record.why == "Compare with the other recipe"
    assert record.received_at == datetime(2026, 10, 26, 14, 0, 0, 123456, tzinfo=UTC)
    assert (record.source_type, record.content_kind, record.platform) == (
        SourceType.SOCIAL,
        ContentKind.POST,
        "instagram",
    )
    assert (record.title, record.author) == ("Pasta night", "Ada")
    assert (record.body, record.media_text, record.shared_text) == (
        "Full caption text.",
        "Transcript words.",
        "Shared by a friend.",
    )
    assert (record.summary, record.summary_origin) == ("Short summary.", "producer:shortcut")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=a", SourceType.YOUTUBE),
        ("https://youtu.be/a", SourceType.YOUTUBE),
        ("https://x.com/u/status/1", SourceType.SOCIAL),
        ("https://www.instagram.com/p/a/", SourceType.SOCIAL),
        ("https://example.com/a", SourceType.WEB),
    ],
)
def test_source_type_is_derived_from_the_host_when_null(url: str, expected: SourceType) -> None:
    record = parse_record(_raw({**THIN, "url": url, "source_type": None}))
    assert record.source_type is expected


def test_refusal_codes_are_the_documented_set() -> None:
    assert frozenset(
        {
            "file_too_large",
            "invalid_field",
            "invalid_json",
            "invalid_url",
            "invalid_why",
            "missing_field",
            "payload_too_large",
            "unknown_field",
            "url_secret",
        }
    ) == RECORD_REFUSAL_CODES


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{", "invalid_json"),
        (b"[]", "invalid_json"),
        (b"\xff\xfe", "invalid_json"),
        (b'{"schema_version": 1, "schema_version": 1}', "invalid_json"),
        (b'{"schema_version": NaN}', "invalid_json"),
        (_raw({**THIN, "extra": 1}), "unknown_field"),
        (b"x" * (MAX_RECORD_BYTES + 1), "file_too_large"),
        (_raw({**THIN, "url": "http://example.com/a"}), "invalid_url"),
        (_raw({**THIN, "url": "https://example.com/a?token=abc"}), "url_secret"),
    ],
)
def test_malformed_records_are_refused_with_named_codes(raw: bytes, code: str) -> None:
    assert _refusal(raw) == code


@pytest.mark.parametrize("field", ["schema_version", "url", "why", "received_at", "fetch_status"])
def test_each_required_field_is_required(field: str) -> None:
    assert _refusal(_raw({key: value for key, value in THIN.items() if key != field})) == (
        "missing_field"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"fetch_status": "done"},
        {"received_at": "2026-10-26 14:00:00Z"},
        {"received_at": "2026-10-26T14:00:00+01:00"},
        {"received_at": "2026-02-30T00:00:00Z"},
        {"source_type": "blog"},
        {"content_kind": "story"},
        {"platform": "Instagram"},
        {"platform": "x" * 41},
        {"title": "t" * 301},
        {"author": "a" * 201},
        {"body": 5},
        {"body": "nul\x00byte"},
        {"summary": "Only a summary."},
        {"summary_origin": "producer:shortcut"},
        {"summary": "Summary.", "summary_origin": "shortcut"},
    ],
)
def test_invalid_values_are_refused_as_invalid_field(change: dict[str, object]) -> None:
    assert _refusal(_raw({**THIN, **change})) == "invalid_field"


@pytest.mark.parametrize(
    "why", ["", "   ", "\t", "\t \t", "line one\nline two", "a b", "x" * 281, 7, None]
)
def test_blank_multiline_or_long_why_is_refused(why: object) -> None:
    assert _refusal(_raw({**THIN, "why": why})) == "invalid_why"


def test_why_is_trimmed_and_may_be_280_characters() -> None:
    assert parse_record(_raw({**THIN, "why": " " + "x" * 280 + " "})).why == "x" * 280
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_saved_item.py::test_thin_record_maps_required_fields_and_defaults packages/connectors/tests/contract/test_saved_item.py::test_invalid_values_are_refused_as_invalid_field`
Expected: FAIL at collection with `ImportError: cannot import name 'MAX_RECORD_BYTES' from 'open_brain_connectors.runtime.saved_item'`.

- [ ] **Step 3: Implement record validation**

Replace the import block of `saved_item.py` (everything between the module docstring and `__all__`) with:

```python
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import canonicalize_source_url
from open_brain_engine.core.models import ContentKind, SourceType
```

Replace `__all__` with:

```python
__all__ = [
    "FETCH_STATUSES",
    "MAX_RECORD_BYTES",
    "RECORD_REFUSAL_CODES",
    "TRACKING_RULES",
    "TRACKING_RULE_VERSION",
    "SavedItemRecord",
    "SavedItemRefusal",
    "identity_url",
    "item_key",
    "parse_record",
]
```

Append after `item_key`:

```python
MAX_RECORD_BYTES = 131_072
MAX_WHY_CHARS = 280
MAX_RECORD_TITLE_CHARS = 300
MAX_AUTHOR_CHARS = 200
FETCH_STATUSES = frozenset({"complete", "failed", "not_attempted", "partial"})
RECORD_REFUSAL_CODES = frozenset(
    {
        "file_too_large",
        "invalid_field",
        "invalid_json",
        "invalid_url",
        "invalid_why",
        "missing_field",
        "payload_too_large",
        "unknown_field",
        "url_secret",
    }
)
_REQUIRED = frozenset({"schema_version", "url", "why", "received_at", "fetch_status"})
_OPTIONAL = frozenset(
    {
        "source_type",
        "content_kind",
        "platform",
        "title",
        "author",
        "body",
        "media_text",
        "shared_text",
        "summary",
        "summary_origin",
    }
)
_RECEIVED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
_PLATFORM = re.compile(r"[a-z0-9][a-z0-9 ._+-]{0,39}")
_SUMMARY_ORIGIN = re.compile(r"producer:[a-z0-9][a-z0-9._-]{0,63}")
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
_SOCIAL_HOSTS = (
    "bsky.app",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "reddit.com",
    "threads.com",
    "threads.net",
    "tiktok.com",
    "twitter.com",
    "x.com",
)


@dataclass(frozen=True, slots=True)
class SavedItemRecord:
    """A validated version 1 record; build it only with ``parse_record``."""

    identity_url: str
    stored_url: str
    item_key: str
    why: str
    received_at: datetime
    fetch_status: str
    source_type: SourceType
    content_kind: ContentKind
    platform: str | None
    title: str | None
    author: str | None
    body: str
    media_text: str
    shared_text: str
    summary: str | None
    summary_origin: str | None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate key")
    return value


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite number")


def _string(value: object, maximum: int | None = None) -> str:
    if type(value) is not str or "\x00" in value or (maximum is not None and len(value) > maximum):
        raise SavedItemRefusal("invalid_field")
    return value


def _optional_string(value: object, maximum: int | None = None) -> str | None:
    return None if value is None else _string(value, maximum)


def _why(value: object) -> str:
    if type(value) is not str:
        raise SavedItemRefusal("invalid_why")
    why = value.strip()
    if not 1 <= len(why) <= MAX_WHY_CHARS or any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in why
    ):
        raise SavedItemRefusal("invalid_why")
    return why


def _received_at(value: object) -> datetime:
    if type(value) is not str or _RECEIVED_AT.fullmatch(value) is None:
        raise SavedItemRefusal("invalid_field")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise SavedItemRefusal("invalid_field") from None


def _source_type(value: object, host: str) -> SourceType:
    if value is None:
        if _host_matches(host, _YOUTUBE_HOSTS):
            return SourceType.YOUTUBE
        return SourceType.SOCIAL if _host_matches(host, _SOCIAL_HOSTS) else SourceType.WEB
    if type(value) is not str or value not in {item.value for item in SourceType}:
        raise SavedItemRefusal("invalid_field")
    return SourceType(value)


def _content_kind(value: object) -> ContentKind:
    if value is None:
        return ContentKind.OTHER
    if type(value) is not str or value not in {item.value for item in ContentKind}:
        raise SavedItemRefusal("invalid_field")
    return ContentKind(value)


def _platform(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _PLATFORM.fullmatch(value) is None:
        raise SavedItemRefusal("invalid_field")
    return value


def _summary(summary: object, origin: object) -> tuple[str | None, str | None]:
    text = _optional_string(summary)
    if (text is None) != (origin is None):
        raise SavedItemRefusal("invalid_field")
    if origin is not None and (
        type(origin) is not str or _SUMMARY_ORIGIN.fullmatch(origin) is None
    ):
        raise SavedItemRefusal("invalid_field")
    return text, None if origin is None else str(origin)


def parse_record(raw: bytes) -> SavedItemRecord:
    """Validate one version 1 record file; any fault is a named local refusal."""
    if type(raw) is not bytes or len(raw) > MAX_RECORD_BYTES:
        raise SavedItemRefusal("file_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError, ValueError, RecursionError:
        raise SavedItemRefusal("invalid_json") from None
    if not isinstance(value, dict):
        raise SavedItemRefusal("invalid_json")
    if set(value) - (_REQUIRED | _OPTIONAL):
        raise SavedItemRefusal("unknown_field")
    if _REQUIRED - set(value):
        raise SavedItemRefusal("missing_field")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SavedItemRefusal("invalid_field")
    identity, stored = identity_url(value["url"])
    why = _why(value["why"])
    received_at = _received_at(value["received_at"])
    if value["fetch_status"] not in FETCH_STATUSES:
        raise SavedItemRefusal("invalid_field")
    summary, summary_origin = _summary(value.get("summary"), value.get("summary_origin"))
    return SavedItemRecord(
        identity_url=identity,
        stored_url=stored,
        item_key=item_key(identity),
        why=why,
        received_at=received_at,
        fetch_status=str(value["fetch_status"]),
        source_type=_source_type(value.get("source_type"), urlsplit(stored).hostname or ""),
        content_kind=_content_kind(value.get("content_kind")),
        platform=_platform(value.get("platform")),
        title=_optional_string(value.get("title"), MAX_RECORD_TITLE_CHARS),
        author=_optional_string(value.get("author"), MAX_AUTHOR_CHARS),
        body=_string(value.get("body", "")),
        media_text=_string(value.get("media_text", "")),
        shared_text=_string(value.get("shared_text", "")),
        summary=summary,
        summary_origin=summary_origin,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_saved_item.py`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/connectors/src/open_brain_connectors/runtime/saved_item.py packages/connectors/tests/contract/test_saved_item.py
uv run --frozen mypy packages/connectors/src/open_brain_connectors/runtime/saved_item.py packages/connectors/tests/contract/test_saved_item.py
git add packages/connectors/src/open_brain_connectors/runtime/saved_item.py \
  packages/connectors/tests/contract/test_saved_item.py
printf 'feat(connectors): validate saved item records with named refusals\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: sonnet:high -->
### Task 3: Descriptor, connectors catalog, app catalog and pin tests

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/source_registry.py` (`__all__` at :13-50, constants at :57-72, new factory before `_ordered_unique_labels` at :556)
- Modify: `packages/connectors/src/open_brain_connectors/runtime/source_cli.py` (import at :56-69, catalog tuple at :417-433)
- Modify: `packages/connectors/tests/contract/test_source_registry.py` (imports at :9-28, catalog test at :47-88, new test)
- Modify: `packages/connectors/tests/contract/test_source_cli.py` (expected list at :150-166)
- Modify: `packages/app/src/open_brain/services/catalog.py:47-48`
- Modify: `packages/app/tests/unit/test_catalog.py` (append one test)

**Interfaces:**
- Produces: `SAVED_ITEM_SOURCE = "saved_item"` and `saved_item_source_descriptor() -> SourceConnectorDescriptor` in `source_registry.py` (both in `__all__`). Tasks 4, 5 and 7 import `SAVED_ITEM_SOURCE`.

- [ ] **Step 1: Write the failing tests**

In `packages/connectors/tests/contract/test_source_registry.py`, add to the `from open_brain_connectors.runtime.source_registry import (...)` list, keeping it alphabetical:

```python
    SAVED_ITEM_SOURCE,
```

as the first name in that list (isort places constants before `SourceAuthMode`), and

```python
    saved_item_source_descriptor,
```

between `notion_source_descriptor,` and `slack_source_descriptor,`. In `test_source_catalog_lists_descriptors_without_import_or_capture_authority`, replace

```python
            notion_source_descriptor(),
            slack_source_descriptor(),
            web_clip_source_descriptor(),
            meeting_transcript_source_descriptor(),
```

with

```python
            notion_source_descriptor(),
            saved_item_source_descriptor(),
            slack_source_descriptor(),
            web_clip_source_descriptor(),
            meeting_transcript_source_descriptor(),
```

and after `    assert catalog.require("notion").resource_types == ("data_source", "page")` add

```python
    assert catalog.require("saved_item").resource_types == ("local_folder",)
```

Append to the same file:

```python
def test_saved_item_descriptor_declares_a_local_folder_without_onboarding() -> None:
    descriptor = saved_item_source_descriptor()

    assert descriptor.connector_name == SAVED_ITEM_SOURCE == "saved_item"
    assert descriptor.display_name == "Saved Items"
    assert descriptor.auth_mode is SourceAuthMode.SESSION_ONLY
    assert descriptor.resource_types == ("local_folder",)
    assert descriptor.content_types == ("saved_item",)
    assert descriptor.preview_limit == 25
    assert descriptor.public_onboarding is False
    assert SourceConnectorDescriptor.from_dict(descriptor.to_dict()) == descriptor
```

In `packages/connectors/tests/contract/test_source_cli.py`, replace

```python
            "resource_types": ["data_source", "page"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "slack",
```

with

```python
            "resource_types": ["data_source", "page"],
            "schema_version": 1,
        },
        {
            "auth_mode": "session_only",
            "connector_name": "saved_item",
            "content_types": ["saved_item"],
            "display_name": "Saved Items",
            "preview_limit": 25,
            "public_onboarding": False,
            "resource_types": ["local_folder"],
            "schema_version": 1,
        },
        {
            "auth_mode": "device_flow",
            "connector_name": "slack",
```

Append to `packages/app/tests/unit/test_catalog.py`:

```python
def test_optional_sources_list_saved_items_between_notion_and_slack() -> None:
    sources = cast(list[dict[str, object]], _catalog()["optional_sources"])
    names = [item["name"] for item in sources]
    assert names.index("notion") < names.index("saved_item") < names.index("slack")
    assert sources[names.index("saved_item")] == {
        "acceptance": "source_only",
        "authorization": "unassessed",
        "implementation": "implemented",
        "name": "saved_item",
        "readiness": "unassessed",
        "scope": "source_only",
        "title": "saved items",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_source_registry.py::test_saved_item_descriptor_declares_a_local_folder_without_onboarding packages/connectors/tests/contract/test_source_cli.py::test_source_cli_catalog_exposes_registered_public_onboarding packages/app/tests/unit/test_catalog.py::test_optional_sources_list_saved_items_between_notion_and_slack`
Expected: FAIL. `test_source_registry.py` errors at collection with `ImportError: cannot import name 'SAVED_ITEM_SOURCE'`; the catalog CLI test fails its list equality (no `saved_item` dict); the app test fails with `ValueError: 'saved_item' is not in list`.

- [ ] **Step 3: Register the descriptor**

In `source_registry.py` `__all__`, insert `"SAVED_ITEM_SOURCE",` after `"D5_MEETING_TRANSCRIPT_SOURCE",` and `"saved_item_source_descriptor",` after `"meeting_transcript_source_descriptor",`. After `D5_MEETING_TRANSCRIPT_SOURCE = "meeting_transcript"` add:

```python
SAVED_ITEM_SOURCE = "saved_item"
```

Insert immediately before `def _ordered_unique_labels(`:

```python
def saved_item_source_descriptor() -> SourceConnectorDescriptor:
    """Return the selected local folder of producer-written saved items."""

    return SourceConnectorDescriptor(
        schema_version=1,
        connector_name=SAVED_ITEM_SOURCE,
        display_name="Saved Items",
        auth_mode=SourceAuthMode.SESSION_ONLY,
        resource_types=("local_folder",),
        content_types=("saved_item",),
        preview_limit=25,
        public_onboarding=False,
    )


```

In `source_cli.py`, replace

```python
    notion_source_descriptor,
    slack_source_descriptor,
    web_clip_source_descriptor,
)
```

with

```python
    notion_source_descriptor,
    saved_item_source_descriptor,
    slack_source_descriptor,
    web_clip_source_descriptor,
)
```

and replace (the only occurrence, inside the `catalog` command)

```python
                notion_source_descriptor(),
                slack_source_descriptor(),
```

with

```python
                notion_source_descriptor(),
                saved_item_source_descriptor(),
                slack_source_descriptor(),
```

In `packages/app/src/open_brain/services/catalog.py`, replace

```python
    ("notion", "Notion"),
    ("slack", "Slack"),
```

with

```python
    ("notion", "Notion"),
    ("saved_item", "saved items"),
    ("slack", "Slack"),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_source_registry.py packages/connectors/tests/contract/test_source_cli.py packages/app/tests/unit/test_catalog.py`
Expected: PASS, including the existing `catalog.describe` import-boundary test at `test_catalog.py:523-536`.

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/connectors/src/open_brain_connectors/runtime/source_registry.py packages/connectors/src/open_brain_connectors/runtime/source_cli.py packages/connectors/tests/contract/test_source_registry.py packages/connectors/tests/contract/test_source_cli.py packages/app/src/open_brain/services/catalog.py packages/app/tests/unit/test_catalog.py
uv run --frozen mypy packages/connectors/src/open_brain_connectors/runtime/source_registry.py packages/connectors/src/open_brain_connectors/runtime/source_cli.py packages/app/src/open_brain/services/catalog.py
git add packages/connectors/src/open_brain_connectors/runtime/source_registry.py \
  packages/connectors/src/open_brain_connectors/runtime/source_cli.py \
  packages/connectors/tests/contract/test_source_registry.py \
  packages/connectors/tests/contract/test_source_cli.py \
  packages/app/src/open_brain/services/catalog.py \
  packages/app/tests/unit/test_catalog.py
printf 'feat(connectors): register the saved_item source descriptor\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: opus -->
### Task 4: Intake rendering, masking, truncation and title bound

**Files:**
- Modify: `packages/connectors/src/open_brain_connectors/runtime/saved_item.py` (import block, `__all__`, append after `parse_record`)
- Modify: `packages/connectors/tests/contract/test_saved_item.py` (import block, append tests)

**Interfaces:**
- Consumes: Task 2 `SavedItemRecord`, `parse_record`; Task 3 `SAVED_ITEM_SOURCE`; `redact_text(value: str) -> str`; `ReferencePayload(url: str, supplied_text: str | None)`; `bounded_json(value: object, maximum: int = 131_072) -> bytes`; `local_source_privacy() -> PrivacyDecision`; `SourceRecordIntake`, `SourceRecordKey`, `SourceResourceSelection`.
- Produces:
  - `MAX_TEXT_CHARS = 60_000`, `MAX_ENCODED_BYTES = 65_536`, `MAX_INTAKE_TITLE_CHARS = 200`, `TRUNCATED_MARKER = "[truncated]"`
  - `class SavedItemSourceAdapter` with `__init__(self, selection: SourceResourceSelection) -> None` (raises `LiveSourceError("source_selection_mismatch")` for a non-`saved_item` selection), `@staticmethod record_from_bytes(raw: bytes) -> SavedItemRecord`, `intake(self, record: SavedItemRecord) -> SourceRecordIntake` (raises `SavedItemRefusal("payload_too_large")`)
  - `encoded_intake_size(intake: SourceRecordIntake) -> int`
  - `content_digest(intake: SourceRecordIntake) -> str` (32 hex over `[url, title, text]`)

- [ ] **Step 1: Write the failing tests**

Replace the import block of `test_saved_item.py` with:

```python
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from open_brain_engine.core.models import ContentKind, SourceType
from open_brain_engine.engine import ReferencePayload

from open_brain_connectors.runtime.live_common import (
    LiveSourceError,
    bounded_json,
    local_source_privacy,
)
from open_brain_connectors.runtime.saved_item import (
    MAX_ENCODED_BYTES,
    MAX_RECORD_BYTES,
    MAX_TEXT_CHARS,
    RECORD_REFUSAL_CODES,
    TRACKING_RULE_VERSION,
    TRACKING_RULES,
    TRUNCATED_MARKER,
    SavedItemRecord,
    SavedItemRefusal,
    SavedItemSourceAdapter,
    content_digest,
    encoded_intake_size,
    identity_url,
    item_key,
    parse_record,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection
```

Append:

```python
SELECTION = SourceResourceSelection(
    "saved_item", "folder:" + "a" * 32, "items:" + "b" * 32, "local_folder"
)


def _intake(value: dict[str, object]) -> SourceRecordIntake:
    adapter = SavedItemSourceAdapter(SELECTION)
    return adapter.intake(adapter.record_from_bytes(_raw(value)))


def _encoded(intake: SourceRecordIntake) -> int:
    payload = ReferencePayload(url=intake.url, supplied_text=intake.text).to_dict()
    return len(bounded_json(payload, 1_048_576))


def test_thin_record_intake_layout_and_key() -> None:
    intake = _intake(THIN)
    assert intake.text == (
        "Stated reason\nRecipe to try this weekend\n\n"
        "Kind\nsource_type: web; content_kind: other\n\n"
        "Fetch status\nnot_attempted"
    )
    assert intake.title == "https://example.com/post/123"
    assert intake.url == "https://example.com/post/123"
    assert intake.privacy == local_source_privacy()
    assert intake.key == SourceRecordKey(
        "saved_item",
        SELECTION.connection_id,
        SELECTION.resource_id,
        item_key("https://example.com/post/123"),
        "1",
    )


def test_rich_record_intake_layout_in_section_order() -> None:
    intake = _intake(RICH)
    assert intake.text == (
        "Stated reason\nCompare with the other recipe\n\n"
        "Summary (producer:shortcut)\nShort summary.\n\n"
        "Body\nFull caption text.\n\n"
        "Media text\nTranscript words.\n\n"
        "Shared text\nShared by a friend.\n\n"
        "Kind\nsource_type: social; content_kind: post; platform: instagram\n\n"
        "Fetch status\ncomplete"
    )
    assert intake.title == "Pasta night"
    assert intake.url == "https://www.instagram.com/p/abc/"


def test_title_falls_back_to_author_on_platform_then_url_and_caps_at_200() -> None:
    no_title = {key: value for key, value in RICH.items() if key != "title"}
    assert _intake(no_title).title == "Ada on instagram"
    no_platform = {key: value for key, value in no_title.items() if key != "platform"}
    assert _intake(no_platform).title == "https://www.instagram.com/p/abc/"
    long_title = _intake({**THIN, "title": "Long title " * 27})
    assert long_title.title == ("Long title " * 27)[:200]
    assert len(long_title.title or "") == 200


def test_fragment_variants_share_the_stored_url_but_not_the_item() -> None:
    first = _intake({**THIN, "url": "https://app.example/#/note/123"})
    second = _intake({**THIN, "url": "https://app.example/#/note/456"})
    assert first.url == second.url == "https://app.example/"
    assert first.key.external_id != second.key.external_id
    assert "#" not in first.text and "#" not in (first.title or "")


def test_long_non_latin_body_is_truncated_to_the_encoded_bound() -> None:
    raw = _raw({**THIN, "body": "東" * 30_000})
    assert len(raw) <= MAX_RECORD_BYTES
    adapter = SavedItemSourceAdapter(SELECTION)
    intake = adapter.intake(adapter.record_from_bytes(raw))
    assert _encoded(intake) <= MAX_ENCODED_BYTES
    assert len(intake.text) <= MAX_TEXT_CHARS
    body = intake.text.split("Body\n", 1)[1].split("\n\nKind\n", 1)[0]
    assert body.endswith("\n" + TRUNCATED_MARKER)
    assert 10_000 < body.count("東") < 30_000
    assert intake.text.startswith("Stated reason\nRecipe to try this weekend\n\n")
    assert encoded_intake_size(intake) <= MAX_ENCODED_BYTES + 1_024


def test_media_text_is_truncated_before_body() -> None:
    intake = _intake({**THIN, "body": "Short body.", "media_text": "語" * 30_000})
    assert "Body\nShort body.\n\n" in intake.text
    assert f"\n{TRUNCATED_MARKER}\n\nKind\n" in intake.text
    assert _encoded(intake) <= MAX_ENCODED_BYTES


def test_record_whose_reason_and_url_alone_do_not_fit_is_refused() -> None:
    adapter = SavedItemSourceAdapter(SELECTION)
    record = adapter.record_from_bytes(_raw({**THIN, "url": "https://example.com/" + "a" * 70_000}))
    with pytest.raises(SavedItemRefusal) as refusal:
        adapter.intake(record)
    assert refusal.value.code == "payload_too_large"


def test_sensitive_text_is_masked_and_the_save_lands() -> None:
    intake = _intake(
        {**THIN, "why": "Works with token=abc123", "body": "Ask bob@example.com about it"}
    )
    assert "abc123" not in intake.text and "bob@example.com" not in intake.text
    assert "Stated reason\nWorks with token=[REDACTED_CREDENTIAL]\n\n" in intake.text
    assert "Body\nAsk [REDACTED_PERSONAL_IDENTIFIER] about it\n\n" in intake.text
    path_id = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"
    assert _intake({**THIN, "url": f"https://example.com/{path_id}"}).url.endswith(path_id)


def test_content_digest_tracks_admitted_bytes() -> None:
    first = _intake(THIN)
    assert re.fullmatch(r"[0-9a-f]{32}", content_digest(first))
    later = _intake({**THIN, "received_at": "2026-10-27T00:00:00Z"})
    assert content_digest(first) == content_digest(later)
    assert content_digest(first) != content_digest(_intake({**THIN, "why": "Another reason"}))


def test_adapter_requires_a_saved_item_selection() -> None:
    with pytest.raises(LiveSourceError, match="source_selection_mismatch"):
        SavedItemSourceAdapter(
            SourceResourceSelection("gmail", "account:x", "label:INBOX", "mail_label")
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_saved_item.py::test_thin_record_intake_layout_and_key packages/connectors/tests/contract/test_saved_item.py::test_long_non_latin_body_is_truncated_to_the_encoded_bound`
Expected: FAIL at collection with `ImportError: cannot import name 'MAX_ENCODED_BYTES' from 'open_brain_connectors.runtime.saved_item'`.

- [ ] **Step 3: Implement rendering and the adapter**

Replace the import block of `saved_item.py` with:

```python
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit

from open_brain_engine.capture.redaction import has_redaction_finding, redact_text
from open_brain_engine.core.ids import canonicalize_source_url
from open_brain_engine.core.models import ContentKind, SourceType
from open_brain_engine.engine import ReferencePayload

from open_brain_connectors.runtime.live_common import (
    LiveSourceError,
    bounded_json,
    local_source_privacy,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import (
    SAVED_ITEM_SOURCE,
    SourceResourceSelection,
)
```

Replace `__all__` with:

```python
__all__ = [
    "FETCH_STATUSES",
    "MAX_ENCODED_BYTES",
    "MAX_INTAKE_TITLE_CHARS",
    "MAX_RECORD_BYTES",
    "MAX_TEXT_CHARS",
    "RECORD_REFUSAL_CODES",
    "TRACKING_RULES",
    "TRACKING_RULE_VERSION",
    "TRUNCATED_MARKER",
    "SavedItemRecord",
    "SavedItemRefusal",
    "SavedItemSourceAdapter",
    "content_digest",
    "encoded_intake_size",
    "identity_url",
    "item_key",
    "parse_record",
]
```

Append after `parse_record`:

```python
MAX_TEXT_CHARS = 60_000
MAX_ENCODED_BYTES = 65_536
MAX_INTAKE_TITLE_CHARS = 200
TRUNCATED_MARKER = "[truncated]"
_TRUNCATION_ORDER = ("Media text", "Body", "Shared text")


def _sections(record: SavedItemRecord) -> list[tuple[str, str]]:
    kind = [
        f"source_type: {record.source_type.value}",
        f"content_kind: {record.content_kind.value}",
    ]
    if record.platform is not None:
        kind.append(f"platform: {record.platform}")
    raw = [
        ("Stated reason", record.why),
        (f"Summary ({record.summary_origin})", record.summary or ""),
        ("Body", record.body),
        ("Media text", record.media_text),
        ("Shared text", record.shared_text),
        ("Kind", "; ".join(kind)),
        ("Fetch status", record.fetch_status),
    ]
    # Masking, never refusing: the engine policy flags ordinary emails and long ids too.
    return [(heading, redact_text(body).strip()) for heading, body in raw]


def _render(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{heading}\n{body}" for heading, body in sections if body)


def _fits(url: str, text: str) -> bool:
    if len(text) > MAX_TEXT_CHARS:
        return False
    try:
        bounded_json(ReferencePayload(url=url, supplied_text=text).to_dict(), MAX_ENCODED_BYTES)
    except ValueError, LiveSourceError:
        return False
    return True


def _truncated(body: str, keep: int) -> str:
    head = body[:keep].rstrip()
    return f"{head}\n{TRUNCATED_MARKER}" if head else TRUNCATED_MARKER


def _bounded_text(url: str, sections: list[tuple[str, str]]) -> str:
    text = _render(sections)
    if _fits(url, text):
        return text
    for heading in _TRUNCATION_ORDER:
        index = next(i for i, (name, _body) in enumerate(sections) if name == heading)
        body = sections[index][1]
        if not body:
            continue
        best, low, high = TRUNCATED_MARKER, 1, len(body) - 1
        while low <= high:
            middle = (low + high) // 2
            sections[index] = (heading, _truncated(body, middle))
            if _fits(url, _render(sections)):
                best, low = sections[index][1], middle + 1
            else:
                high = middle - 1
        sections[index] = (heading, best)
        text = _render(sections)
        if _fits(url, text):
            return text
    raise SavedItemRefusal("payload_too_large")


def _title(record: SavedItemRecord) -> str:
    if record.title is not None and record.title.strip():
        base = record.title
    elif record.author is not None and record.author.strip() and record.platform is not None:
        base = f"{record.author.strip()} on {record.platform}"
    else:
        base = record.stored_url
    title = " ".join(redact_text(base).split())[:MAX_INTAKE_TITLE_CHARS].strip()
    return title or record.stored_url[:MAX_INTAKE_TITLE_CHARS]


def encoded_intake_size(intake: SourceRecordIntake) -> int:
    """Bytes one intake adds to a page, measured the way the collector encodes it."""
    value = {"payload": intake.payload().to_dict(), "title": intake.title}
    return len(bounded_json(value, 2 * MAX_ENCODED_BYTES))


def content_digest(intake: SourceRecordIntake) -> str:
    """Fixed-size digest of the admitted bytes, used by first-save-wins."""
    value = [intake.url, intake.title, intake.text]
    return sha256(bounded_json(value, 2 * MAX_ENCODED_BYTES)).hexdigest()[:32]


class SavedItemSourceAdapter:
    """Turn validated saved-item records into intakes for one selected folder."""

    def __init__(self, selection: SourceResourceSelection) -> None:
        if type(selection) is not SourceResourceSelection or (
            selection.connector_name != SAVED_ITEM_SOURCE
        ):
            raise LiveSourceError("source_selection_mismatch")
        self._selection = selection

    @staticmethod
    def record_from_bytes(raw: bytes) -> SavedItemRecord:
        return parse_record(raw)

    def intake(self, record: SavedItemRecord) -> SourceRecordIntake:
        text = _bounded_text(record.stored_url, _sections(record))
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name=SAVED_ITEM_SOURCE,
                connection_id=self._selection.connection_id,
                resource_id=self._selection.resource_id,
                external_id=record.item_key,
                revision_id="1",
            ),
            url=record.stored_url,
            text=text,
            privacy=local_source_privacy(),
            title=_title(record),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/connectors/tests/contract/test_saved_item.py`
Expected: PASS. (Measured in the prototype: the 30,000-character body renders 11,012 characters and 65,533 encoded bytes.)

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/connectors/src/open_brain_connectors/runtime/saved_item.py packages/connectors/tests/contract/test_saved_item.py
uv run --frozen mypy packages/connectors/src/open_brain_connectors/runtime/saved_item.py packages/connectors/tests/contract/test_saved_item.py
git add packages/connectors/src/open_brain_connectors/runtime/saved_item.py \
  packages/connectors/tests/contract/test_saved_item.py
printf 'feat(connectors): render bounded, masked saved item intakes\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: sonnet:high -->
### Task 5: Folder selection and descriptor-anchored reads (`saved_item_folder.py`)

**Files:**
- Create: `packages/collector/src/open_brain_collector/saved_item_folder.py`
- Create: `packages/collector/tests/contract/test_saved_item_folder.py`

**Interfaces:**
- Consumes: `LiveSourceError`, `bounded_json` (`live_common`); `SAVED_ITEM_SOURCE`, `SourceResourceSelection` (`source_registry`).
- Produces:
  - `MAX_RECORD_BYTES = 131_072`, `MAX_RECORD_FILES = 10_000`, `RESOURCE_TYPE = "local_folder"`, `SKIPPED_SUFFIXES = (".icloud", ".tmp", ".partial")`, `SF_DATALESS: int`, `EntryStatus = Literal["unchanged", "deferred", "refused", "read"]`
  - `name_hash(name: str) -> str` (32 hex), `stat_fingerprint(info: os.stat_result) -> str` (24 hex)
  - `folder_selection(path: Path, info: os.stat_result) -> SourceResourceSelection`
  - `@dataclass(frozen=True, slots=True) class OpenFolder(fd: int, path: Path, selection: SourceResourceSelection)`
  - `open_folder(path: Path) -> contextlib context manager yielding OpenFolder` (raises `folder_missing` / `folder_unsafe`)
  - `select_folder(raw: str) -> tuple[SourceResourceSelection, Path]`
  - `@dataclass(frozen=True, slots=True) class FolderListing(records: tuple[str, ...], placeholders: int)`; `list_records(folder: OpenFolder) -> FolderListing` (raises `folder_over_limit`)
  - `@dataclass(frozen=True, slots=True) class EntryRead(name_hash: str, status: EntryStatus, fingerprint: str = "", code: str = "", data: bytes = b"")`; `read_entry(folder: OpenFolder, name: str, known: str | None) -> EntryRead`
  - Patch points used by tests: module functions `_entry_flags(info: os.stat_result) -> int` and `_open_entry(name: str, folder_fd: int) -> int`

- [ ] **Step 1: Write the failing tests**

Create `packages/collector/tests/contract/test_saved_item_folder.py`:

```python
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from open_brain_collector import saved_item_folder
from open_brain_collector.saved_item_folder import (
    MAX_RECORD_BYTES,
    FolderListing,
    list_records,
    name_hash,
    open_folder,
    read_entry,
    select_folder,
    stat_fingerprint,
)
from open_brain_connectors.runtime.live_common import LiveSourceError


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    return folder.resolve()


def _write(folder: Path, name: str, data: bytes = b'{"x": 1}') -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def _refuse_open(name: str, folder_fd: int) -> int:
    raise AssertionError(f"opened {name!r}")


def test_select_folder_hashes_the_resolved_path_and_inode(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    selection, resolved = select_folder(str(folder))
    assert resolved == folder
    assert (selection.connector_name, selection.resource_type) == ("saved_item", "local_folder")
    assert re.fullmatch(r"folder:[0-9a-f]{32}", selection.connection_id)
    assert re.fullmatch(r"items:[0-9a-f]{32}", selection.resource_id)
    assert select_folder(str(folder))[0] == selection
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    assert select_folder(str(other))[0] != selection
    with open_folder(folder) as opened:
        assert opened.selection == selection and opened.path == folder


def test_folder_must_exist_be_owner_private_and_not_a_symlink(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    link = folder.parent / "link"
    link.symlink_to(folder, target_is_directory=True)
    plain_file = _write(folder.parent, "plain")
    with pytest.raises(LiveSourceError, match="folder_missing"):
        select_folder(str(folder.parent / "absent"))
    for raw in (str(link), "relative/inbox", ""):
        with pytest.raises(LiveSourceError, match="folder_unsafe"):
            select_folder(raw)
    for path in (link, plain_file):
        with pytest.raises(LiveSourceError, match="folder_unsafe"), open_folder(path):
            pass
    folder.chmod(0o770)
    try:
        with pytest.raises(LiveSourceError, match="folder_unsafe"), open_folder(folder):
            pass
    finally:
        folder.chmod(0o700)


def test_list_records_skips_hidden_cloud_and_partial_names(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    for name in (
        "b.json",
        "a.json",
        ".hidden.json",
        ".a.json.icloud",
        "c.json.tmp",
        "d.json.partial",
        "notes.txt",
    ):
        _write(folder, name, b"{}")
    with open_folder(folder) as opened:
        listing = list_records(opened)
    assert listing == FolderListing(tuple(sorted(("a.json", "b.json"), key=name_hash)), 4)


def test_more_json_entries_than_the_limit_refuses_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_folder, "MAX_RECORD_FILES", 3)
    folder = _folder(tmp_path)
    for number in range(3):
        _write(folder, f"{number}.json", b"{}")
    with open_folder(folder) as opened:
        assert len(list_records(opened).records) == 3
    _write(folder, ".3.json", b"{}")
    with pytest.raises(LiveSourceError, match="folder_over_limit"), open_folder(folder) as opened:
        list_records(opened)


def test_regular_entry_is_read_with_a_stable_fingerprint(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    path = _write(folder, "a.json")
    with open_folder(folder) as opened:
        entry = read_entry(opened, "a.json", None)
    assert (entry.status, entry.data, entry.code) == ("read", b'{"x": 1}', "")
    assert entry.name_hash == name_hash("a.json")
    assert entry.fingerprint == stat_fingerprint(path.lstat())
    assert re.fullmatch(r"[0-9a-f]{24}", entry.fingerprint)


def test_known_fingerprint_is_skipped_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    path = _write(folder, "a.json")
    monkeypatch.setattr(saved_item_folder, "_open_entry", _refuse_open)
    with open_folder(folder) as opened:
        entry = read_entry(opened, "a.json", stat_fingerprint(path.lstat()))
    assert entry.status == "unchanged"


def test_pipe_symlink_directory_and_oversize_entries_are_refused_without_blocking(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path)
    os.mkfifo(folder / "pipe.json")
    target = _write(folder, "target.json")
    (folder / "link.json").symlink_to(target)
    (folder / "dir.json").mkdir()
    _write(folder, "big.json", b" " * (MAX_RECORD_BYTES + 1))
    with open_folder(folder) as opened:
        results = {
            name: (read_entry(opened, name, None).status, read_entry(opened, name, None).code)
            for name in ("pipe.json", "link.json", "dir.json", "big.json")
        }
        fd = saved_item_folder._open_entry("pipe.json", opened.fd)
        try:
            assert stat.S_ISFIFO(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
        with pytest.raises(OSError):
            saved_item_folder._open_entry("link.json", opened.fd)
    assert results == {
        "pipe.json": ("refused", "not_regular_file"),
        "link.json": ("refused", "not_regular_file"),
        "dir.json": ("refused", "not_regular_file"),
        "big.json": ("refused", "file_too_large"),
    }


def test_dataless_entry_is_deferred_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    inode = _write(folder, "cloud.json").lstat().st_ino

    def flags(info: os.stat_result) -> int:
        return saved_item_folder.SF_DATALESS if info.st_ino == inode else 0

    monkeypatch.setattr(saved_item_folder, "_entry_flags", flags)
    monkeypatch.setattr(saved_item_folder, "_open_entry", _refuse_open)
    with open_folder(folder) as opened:
        entry = read_entry(opened, "cloud.json", None)
    assert (entry.status, entry.code) == ("deferred", "placeholder")


def test_entry_changed_during_the_read_is_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    path = _write(folder, "a.json")
    real_open = saved_item_folder._open_entry

    def grow(name: str, folder_fd: int) -> int:
        fd = real_open(name, folder_fd)
        with path.open("ab") as handle:
            handle.write(b" ")
        return fd

    monkeypatch.setattr(saved_item_folder, "_open_entry", grow)
    with open_folder(folder) as opened:
        entry = read_entry(opened, "a.json", None)
    assert (entry.status, entry.code) == ("deferred", "unstable")


def test_name_hash_is_fixed_size_for_any_name() -> None:
    for name in ("a.json", "bad\nname\x07.json", "é.json"):
        assert re.fullmatch(r"[0-9a-f]{32}", name_hash(name))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/collector/tests/contract/test_saved_item_folder.py::test_select_folder_hashes_the_resolved_path_and_inode packages/collector/tests/contract/test_saved_item_folder.py::test_pipe_symlink_directory_and_oversize_entries_are_refused_without_blocking`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'open_brain_collector.saved_item_folder'`.

- [ ] **Step 3: Write the module**

Create `packages/collector/src/open_brain_collector/saved_item_folder.py`:

```python
"""Descriptor-anchored reads of one selected saved-item folder.

Other writers control the folder's entry names, so names are hashed before they reach
state, notices or errors, and no entry is opened through a path that could follow a link.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.source_registry import (
    SAVED_ITEM_SOURCE,
    SourceResourceSelection,
)

MAX_RECORD_BYTES = 131_072
MAX_RECORD_FILES = 10_000
RESOURCE_TYPE = "local_folder"
SKIPPED_SUFFIXES = (".icloud", ".tmp", ".partial")
# macOS marks cloud placeholders dataless; Linux has no st_flags, so the check is inert there.
SF_DATALESS = int(getattr(stat, "SF_DATALESS", 0x40000000))
EntryStatus = Literal["unchanged", "deferred", "refused", "read"]


def name_hash(name: str) -> str:
    """Fixed-size key for an entry name."""
    return hashlib.sha256(os.fsencode(name)).hexdigest()[:32]


def stat_fingerprint(info: os.stat_result) -> str:
    value = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:24]


def folder_selection(path: Path, info: os.stat_result) -> SourceResourceSelection:
    digest = hashlib.sha256(bounded_json([str(path), info.st_dev, info.st_ino])).hexdigest()
    return SourceResourceSelection(
        connector_name=SAVED_ITEM_SOURCE,
        connection_id="folder:" + digest[:32],
        resource_id="items:" + digest[32:],
        resource_type=RESOURCE_TYPE,
    )


@dataclass(frozen=True, slots=True)
class OpenFolder:
    fd: int
    path: Path
    selection: SourceResourceSelection


@contextmanager
def open_folder(path: Path) -> Iterator[OpenFolder]:
    """Open the folder once, no-follow, and check owner, mode and type on the descriptor."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise LiveSourceError("folder_unsafe")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise LiveSourceError("folder_missing") from None
    except OSError:
        raise LiveSourceError("folder_unsafe") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise LiveSourceError("folder_unsafe")
        yield OpenFolder(fd, path, folder_selection(path, info))
    finally:
        os.close(fd)


def select_folder(raw: str) -> tuple[SourceResourceSelection, Path]:
    """Derive the selection from a folder path the owner typed."""
    if type(raw) is not str or not raw or "\x00" in raw:
        raise LiveSourceError("folder_unsafe")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        raise LiveSourceError("folder_unsafe")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise LiveSourceError("folder_missing") from None
    with open_folder(resolved) as folder:
        return folder.selection, resolved


@dataclass(frozen=True, slots=True)
class FolderListing:
    records: tuple[str, ...]
    placeholders: int


def list_records(folder: OpenFolder) -> FolderListing:
    """List ``*.json`` names in name-hash order; refuse folders over the file limit."""
    records: list[str] = []
    placeholders = json_entries = 0
    with os.scandir(folder.fd) as entries:
        for entry in entries:
            name = entry.name
            if name.endswith(".json"):
                json_entries += 1
                if json_entries > MAX_RECORD_FILES:
                    raise LiveSourceError("folder_over_limit")
            if name.startswith(".") or name.endswith(SKIPPED_SUFFIXES):
                placeholders += int(".json" in name)
                continue
            if name.endswith(".json"):
                records.append(name)
    return FolderListing(tuple(sorted(records, key=name_hash)), placeholders)


@dataclass(frozen=True, slots=True)
class EntryRead:
    name_hash: str
    status: EntryStatus
    fingerprint: str = ""
    code: str = ""
    data: bytes = b""


def _entry_flags(info: os.stat_result) -> int:
    return int(getattr(info, "st_flags", 0))


def _open_entry(name: str, folder_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def read_entry(folder: OpenFolder, name: str, known: str | None) -> EntryRead:
    """Read one entry through the folder descriptor without following or blocking."""
    hashed = name_hash(name)
    try:
        info = os.stat(name, dir_fd=folder.fd, follow_symlinks=False)
    except FileNotFoundError:
        return EntryRead(hashed, "deferred", code="vanished")
    if _entry_flags(info) & SF_DATALESS:
        return EntryRead(hashed, "deferred", code="placeholder")
    fingerprint = stat_fingerprint(info)
    if fingerprint == known:
        return EntryRead(hashed, "unchanged", fingerprint)
    if not stat.S_ISREG(info.st_mode):
        return EntryRead(hashed, "refused", fingerprint, "not_regular_file")
    if info.st_size > MAX_RECORD_BYTES:
        return EntryRead(hashed, "refused", fingerprint, "file_too_large")
    try:
        fd = _open_entry(name, folder.fd)
    except FileNotFoundError:
        return EntryRead(hashed, "deferred", code="vanished")
    except OSError:
        return EntryRead(hashed, "refused", fingerprint, "not_regular_file")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return EntryRead(hashed, "refused", stat_fingerprint(before), "not_regular_file")
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if not _identity(info) == _identity(before) == _identity(after):
        return EntryRead(hashed, "deferred", code="unstable")
    data = b"".join(chunks)
    if len(data) > MAX_RECORD_BYTES:
        return EntryRead(hashed, "refused", stat_fingerprint(after), "file_too_large")
    return EntryRead(hashed, "read", stat_fingerprint(after), data=data)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/collector/tests/contract/test_saved_item_folder.py packages/collector/tests/contract/test_package_boundary.py`
Expected: PASS (the boundary test confirms the new module imports nothing forbidden).

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/collector/src/open_brain_collector/saved_item_folder.py packages/collector/tests/contract/test_saved_item_folder.py
uv run --frozen mypy packages/collector/src/open_brain_collector/saved_item_folder.py packages/collector/tests/contract/test_saved_item_folder.py
git add packages/collector/src/open_brain_collector/saved_item_folder.py \
  packages/collector/tests/contract/test_saved_item_folder.py
printf 'feat(collector): read a selected saved item folder through one descriptor\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: sonnet:high -->
### Task 6: Known-file index (`saved_item_index.py`)

**Files:**
- Create: `packages/collector/src/open_brain_collector/saved_item_index.py`
- Create: `packages/collector/tests/contract/test_saved_item_index.py`

**Interfaces:**
- Consumes: `RECORD_REFUSAL_CODES` (Task 2); `PrivateJsonStore(root: Path)` with `read(name) -> object | None` and `write(name, value) -> None`; `bounded_json`; `SourceResourceSelection`.
- Produces:
  - `INDEX_MAX_ENTRIES = 10_000`, `INDEX_MAX_DELIVERED = 10_000`, `INDEX_SIZE_LIMIT = 2_097_152`, `TERMINAL_STATES: frozenset[str]`
  - `@dataclass(slots=True) class SavedItemIndex(entries: dict[str, str], delivered: dict[str, str])` with `fingerprint(self, name_hash: str) -> str | None`, `record(self, name_hash: str, fingerprint: str, state: str, item_key: str = "", content: str = "") -> None`, `prune(self, present: Collection[str]) -> None`, `to_dict(self) -> dict[str, object]`, `@classmethod from_dict(cls, value: object) -> SavedItemIndex`. Invalid input raises `LiveSourceError("saved_item_invalid_index")`.
  - `index_name(selection: SourceResourceSelection) -> str`, `load_index(store: PrivateJsonStore, name: str) -> SavedItemIndex`, `save_index(store: PrivateJsonStore, name: str, index: SavedItemIndex) -> None` (raises `saved_item_index_too_large`)

- [ ] **Step 1: Write the failing tests**

Create `packages/collector/tests/contract/test_saved_item_index.py`:

```python
from __future__ import annotations

import re
from pathlib import Path

import pytest

from open_brain_collector import saved_item_index
from open_brain_collector.saved_item_index import (
    INDEX_MAX_DELIVERED,
    INDEX_MAX_ENTRIES,
    INDEX_SIZE_LIMIT,
    TERMINAL_STATES,
    SavedItemIndex,
    index_name,
    load_index,
    save_index,
)
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import RECORD_REFUSAL_CODES
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

NAME = "a" * 32
FINGERPRINT = "b" * 24
KEY = "c" * 32
CONTENT = "d" * 32


def test_record_tracks_terminal_states_and_delivered_items() -> None:
    index = SavedItemIndex()
    assert index.fingerprint(NAME) is None
    index.record(NAME, FINGERPRINT, "delivered", KEY, CONTENT)
    index.record("e" * 32, FINGERPRINT, "invalid_json")
    assert index.fingerprint(NAME) == FINGERPRINT
    assert index.entries == {
        NAME: FINGERPRINT + ":delivered",
        "e" * 32: FINGERPRINT + ":invalid_json",
    }
    assert index.delivered == {KEY: CONTENT}
    assert SavedItemIndex.from_dict(index.to_dict()) == index
    index.prune({NAME})
    assert set(index.entries) == {NAME} and index.delivered == {KEY: CONTENT}


def test_terminal_states_cover_every_local_outcome() -> None:
    assert frozenset(
        {"already_captured", "delivered", "not_regular_file", "skipped", *RECORD_REFUSAL_CODES}
    ) == TERMINAL_STATES


@pytest.mark.parametrize(
    "arguments",
    [
        ("x", FINGERPRINT, "skipped", "", ""),
        (NAME, "short", "skipped", "", ""),
        (NAME, FINGERPRINT, "placeholder", "", ""),
        (NAME, FINGERPRINT, "delivered", "", ""),
        (NAME, FINGERPRINT, "skipped", KEY, CONTENT),
    ],
)
def test_record_refuses_malformed_or_nonterminal_values(
    arguments: tuple[str, str, str, str, str],
) -> None:
    with pytest.raises(LiveSourceError, match="saved_item_invalid_index"):
        SavedItemIndex().record(*arguments)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"schema_version": 1},
        {"schema_version": 2, "entries": {}, "delivered": {}},
        {"schema_version": 1, "entries": {NAME: "zz:skipped"}, "delivered": {}},
        {"schema_version": 1, "entries": {NAME: FINGERPRINT + ":placeholder"}, "delivered": {}},
        {"schema_version": 1, "entries": {}, "delivered": {KEY: 5}},
    ],
)
def test_from_dict_refuses_foreign_shapes(value: object) -> None:
    with pytest.raises(LiveSourceError, match="saved_item_invalid_index"):
        SavedItemIndex.from_dict(value)


def test_oldest_entries_and_delivered_items_are_evicted_past_the_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saved_item_index, "INDEX_MAX_ENTRIES", 2)
    monkeypatch.setattr(saved_item_index, "INDEX_MAX_DELIVERED", 2)
    index = SavedItemIndex()
    for number in range(3):
        index.record(f"{number:032x}", FINGERPRINT, "delivered", f"{number + 10:032x}", CONTENT)
    assert list(index.entries) == [f"{1:032x}", f"{2:032x}"]
    assert list(index.delivered) == [f"{11:032x}", f"{12:032x}"]


def test_index_at_full_capacity_stays_under_half_the_store_bound(tmp_path: Path) -> None:
    longest = max(TERMINAL_STATES, key=len)
    index = SavedItemIndex(
        entries={f"{n:032x}": "f" * 24 + ":" + longest for n in range(INDEX_MAX_ENTRIES)},
        delivered={f"{n:032x}": "e" * 32 for n in range(INDEX_MAX_DELIVERED)},
    )
    assert INDEX_MAX_ENTRIES == INDEX_MAX_DELIVERED == 10_000
    assert INDEX_SIZE_LIMIT == 4_194_304 // 2
    assert len(bounded_json(index.to_dict(), 4_194_304)) < INDEX_SIZE_LIMIT
    store = PrivateJsonStore(tmp_path / "saved_items")
    save_index(store, "index-test.json", index)
    assert load_index(store, "index-test.json") == index


def test_oversized_index_is_refused_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_index, "INDEX_SIZE_LIMIT", 64)
    index = SavedItemIndex()
    index.record(NAME, FINGERPRINT, "delivered", KEY, CONTENT)
    store = PrivateJsonStore(tmp_path / "saved_items")
    with pytest.raises(LiveSourceError, match="saved_item_index_too_large"):
        save_index(store, "index-test.json", index)
    assert store.read("index-test.json") is None


def test_lost_index_loads_empty_and_names_are_per_selection(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "saved_items")
    first = SourceResourceSelection(
        "saved_item", "folder:" + "1" * 32, "items:" + "2" * 32, "local_folder"
    )
    second = SourceResourceSelection(
        "saved_item", "folder:" + "1" * 32, "items:" + "3" * 32, "local_folder"
    )
    assert load_index(store, index_name(first)) == SavedItemIndex()
    assert re.fullmatch(r"index-[0-9a-f]{32}\.json", index_name(first))
    assert index_name(first) != index_name(second)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/collector/tests/contract/test_saved_item_index.py::test_index_at_full_capacity_stays_under_half_the_store_bound`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'open_brain_collector.saved_item_index'`.

- [ ] **Step 3: Write the module**

Create `packages/collector/src/open_brain_collector/saved_item_index.py`:

```python
"""Fixed-size known-file index for one selected saved-item folder.

The index lives in its own private store file, not in the 131,072-byte checkpoint. It is
written only from ``acknowledge``, after the collector commits the checkpoint.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import cast

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import RECORD_REFUSAL_CODES
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

INDEX_MAX_ENTRIES = 10_000
INDEX_MAX_DELIVERED = 10_000
# Half of PrivateJsonStore's 4 MiB file bound.
_STORE_MAX_BYTES = 4_194_304
INDEX_SIZE_LIMIT = _STORE_MAX_BYTES // 2
TERMINAL_STATES = frozenset(
    {"already_captured", "delivered", "not_regular_file", "skipped", *RECORD_REFUSAL_CODES}
)
_HASH = re.compile(r"[0-9a-f]{32}")
_FINGERPRINT = re.compile(r"[0-9a-f]{24}")


def _invalid() -> LiveSourceError:
    return LiveSourceError("saved_item_invalid_index")


@dataclass(slots=True)
class SavedItemIndex:
    entries: dict[str, str] = field(default_factory=dict)
    delivered: dict[str, str] = field(default_factory=dict)

    def fingerprint(self, name_hash: str) -> str | None:
        value = self.entries.get(name_hash)
        return None if value is None else value.partition(":")[0]

    def record(
        self,
        name_hash: str,
        fingerprint: str,
        state: str,
        item_key: str = "",
        content: str = "",
    ) -> None:
        delivered = state == "delivered"
        if (
            _HASH.fullmatch(name_hash) is None
            or _FINGERPRINT.fullmatch(fingerprint) is None
            or state not in TERMINAL_STATES
            or (
                delivered
                and (_HASH.fullmatch(item_key) is None or _HASH.fullmatch(content) is None)
            )
            or (not delivered and (item_key or content))
        ):
            raise _invalid()
        self.entries.pop(name_hash, None)
        self.entries[name_hash] = f"{fingerprint}:{state}"
        while len(self.entries) > INDEX_MAX_ENTRIES:
            del self.entries[next(iter(self.entries))]
        if delivered:
            self.delivered.pop(item_key, None)
            self.delivered[item_key] = content
            # An evicted key falls back to custody dedupe, as after a lost index.
            while len(self.delivered) > INDEX_MAX_DELIVERED:
                del self.delivered[next(iter(self.delivered))]

    def prune(self, present: Collection[str]) -> None:
        self.entries = {key: value for key, value in self.entries.items() if key in present}

    def to_dict(self) -> dict[str, object]:
        return {
            "delivered": dict(self.delivered),
            "entries": dict(self.entries),
            "schema_version": 1,
        }

    @classmethod
    def from_dict(cls, value: object) -> SavedItemIndex:
        if not isinstance(value, dict) or set(value) != {"delivered", "entries", "schema_version"}:
            raise _invalid()
        entries, delivered = value["entries"], value["delivered"]
        if (
            value["schema_version"] != 1
            or not isinstance(entries, dict)
            or not isinstance(delivered, dict)
            or len(entries) > INDEX_MAX_ENTRIES
            or len(delivered) > INDEX_MAX_DELIVERED
        ):
            raise _invalid()
        index = cls()
        for key, item in cast(dict[object, object], entries).items():
            if type(key) is not str or type(item) is not str:
                raise _invalid()
            fingerprint, _, state = item.partition(":")
            if (
                _HASH.fullmatch(key) is None
                or _FINGERPRINT.fullmatch(fingerprint) is None
                or state not in TERMINAL_STATES
            ):
                raise _invalid()
            index.entries[key] = item
        for key, item in cast(dict[object, object], delivered).items():
            if type(key) is not str or type(item) is not str:
                raise _invalid()
            if _HASH.fullmatch(key) is None or _HASH.fullmatch(item) is None:
                raise _invalid()
            index.delivered[key] = item
        return index


def index_name(selection: SourceResourceSelection) -> str:
    return "index-" + hashlib.sha256(selection.resource_id.encode()).hexdigest()[:32] + ".json"


def load_index(store: PrivateJsonStore, name: str) -> SavedItemIndex:
    value = store.read(name)
    return SavedItemIndex() if value is None else SavedItemIndex.from_dict(value)


def save_index(store: PrivateJsonStore, name: str, index: SavedItemIndex) -> None:
    value = index.to_dict()
    # Encode against the store's own 4 MiB ceiling, then hold the index to half of it.
    if len(bounded_json(value, _STORE_MAX_BYTES)) > INDEX_SIZE_LIMIT:
        raise LiveSourceError("saved_item_index_too_large")
    store.write(name, value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/collector/tests/contract/test_saved_item_index.py`
Expected: PASS (measured full-capacity index: 1,500,046 bytes).

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/collector/src/open_brain_collector/saved_item_index.py packages/collector/tests/contract/test_saved_item_index.py
uv run --frozen mypy packages/collector/src/open_brain_collector/saved_item_index.py packages/collector/tests/contract/test_saved_item_index.py
git add packages/collector/src/open_brain_collector/saved_item_index.py \
  packages/collector/tests/contract/test_saved_item_index.py
printf 'feat(collector): fixed-size known-file index for saved items\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: opus -->
### Task 7: Live source page and runtime wiring (`saved_item_live.py`)

**Files:**
- Create: `packages/collector/src/open_brain_collector/saved_item_live.py`
- Modify: `packages/collector/src/open_brain_collector/live_capture.py:33-38` (`_PROVIDERS`)
- Modify: `packages/collector/src/open_brain_collector/live_manager.py` (import after line 14; `PriorityRuntime.fetch` :128-131; `PriorityRuntime.acknowledge` :143-144)
- Create: `packages/collector/tests/integration/test_saved_item_live.py`
- Modify: `packages/collector/tests/integration/test_live_capture.py:169-174` (`_PROVIDERS` pin)

**Interfaces:**
- Consumes: Task 4 `SavedItemSourceAdapter`, `SavedItemRefusal`, `content_digest`, `encoded_intake_size`; Task 5 `open_folder`, `list_records`, `read_entry`, `name_hash`; Task 6 `index_name`, `load_index`, `save_index`; `LiveBatch(intakes, checkpoint, has_more=False, notices=())`; `PrivateJsonStore.lock(name: str, *, timeout_seconds: float = 5)`.
- Produces:
  - `PAGE_INTAKES = 25`, `PAGE_BYTES = 1_048_576`, `MAX_PAGE_OUTCOMES = 500`
  - `class SavedItemLiveSource` with `__init__(self, root: Path) -> None`, `fetch(self, selection: SourceResourceSelection, options: dict[str, object], checkpoint: dict[str, object] | None) -> LiveBatch`, `acknowledge(self, selection: SourceResourceSelection, checkpoint: dict[str, object]) -> None` (the `LiveRuntime` protocol, `live_capture.py:42-52`)
  - `PriorityRuntime` routes `saved_item` to `SavedItemLiveSource(self.root / "saved_items")`; `_PROVIDERS["saved_item"] == frozenset({"local_folder"})`

- [ ] **Step 1: Write the failing tests**

In `packages/collector/tests/integration/test_live_capture.py`, replace

```python
        "agent_session": frozenset({"local_project"}),
    } == _PROVIDERS
```

with

```python
        "agent_session": frozenset({"local_project"}),
        "saved_item": frozenset({"local_folder"}),
    } == _PROVIDERS
```

Create `packages/collector/tests/integration/test_saved_item_live.py`:

```python
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector import saved_item_folder, saved_item_live
from open_brain_collector.capture_policy import revision_store_dirname
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.live_manager import PriorityRuntime
from open_brain_collector.saved_item_folder import name_hash, select_folder
from open_brain_collector.saved_item_index import index_name, load_index
from open_brain_collector.saved_item_live import (
    MAX_PAGE_OUTCOMES,
    PAGE_BYTES,
    PAGE_INTAKES,
    SavedItemLiveSource,
)
from open_brain_connectors.runtime.live_common import LiveBatch, LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import encoded_intake_size
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def _record(number: int, **fields: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "url": f"https://example.com/post/{number}",
        "why": f"Synthetic reason {number}",
        "received_at": "2026-10-26T14:00:00Z",
        "fetch_status": "not_attempted",
        **fields,
    }


def _write(folder: Path, name: str, value: dict[str, object]) -> None:
    (folder / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _setup(
    tmp_path: Path,
) -> tuple[SavedItemLiveSource, SourceResourceSelection, dict[str, object], Path]:
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    selection, resolved = select_folder(str(folder))
    source = SavedItemLiveSource(tmp_path / "live" / "saved_items")
    return source, selection, {"folder_path": str(resolved)}, resolved


def _drain(
    source: SavedItemLiveSource, selection: SourceResourceSelection, options: dict[str, object]
) -> list[LiveBatch]:
    batches: list[LiveBatch] = []
    checkpoint: dict[str, object] | None = None
    while True:
        batch = source.fetch(selection, options, checkpoint)
        source.acknowledge(selection, batch.checkpoint)
        batches.append(batch)
        checkpoint = batch.checkpoint
        if not batch.has_more:
            return batches


def _reason(intake: SourceRecordIntake) -> str:
    return intake.text.splitlines()[1]


def test_thirty_new_files_arrive_in_two_pages(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(30):
        _write(folder, f"item-{number}.json", _record(number))
    batches = _drain(source, selection, options)
    assert [len(batch.intakes) for batch in batches] == [PAGE_INTAKES, 5]
    assert [batch.has_more for batch in batches] == [True, False]
    assert "saved_item_count:delivered:25" in batches[0].notices
    assert len({item.key.external_id for batch in batches for item in batch.intakes}) == 30
    assert source.fetch(selection, options, batches[-1].checkpoint).intakes == ()


def test_large_non_latin_records_split_under_the_page_budget(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(20):
        _write(folder, f"wide-{number}.json", _record(number, body="東" * 30_000))
    batches = _drain(source, selection, options)
    sizes = [sum(encoded_intake_size(item) for item in batch.intakes) for batch in batches]
    assert len(batches) >= 2 and len(batches[0].intakes) < 20
    assert all(size <= PAGE_BYTES for size in sizes)
    assert sum(len(batch.intakes) for batch in batches) == 20


def test_first_save_wins_across_and_within_pages(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    earliest = "Earliest reason"
    _write(folder, "later.json", _record(1, received_at="2026-10-26T15:00:00Z"))
    _write(folder, "earliest.json", _record(1, why=earliest, received_at="2026-10-26T13:00:00.5Z"))
    _write(folder, "middle.json", _record(1, received_at="2026-10-26T14:00:00Z"))
    _write(folder, "copy.json", _record(1, why=earliest, received_at="2026-10-26T16:00:00Z"))
    first = source.fetch(selection, options, None)
    assert [_reason(item) for item in first.intakes] == ["Earliest reason"]
    assert first.has_more and "saved_item_count:deferred_duplicate:3" in first.notices
    source.acknowledge(selection, first.checkpoint)
    second = source.fetch(selection, options, first.checkpoint)
    assert second.intakes == () and not second.has_more
    assert "saved_item_count:skipped:1" in second.notices
    assert "saved_item_count:already_captured:2" in second.notices
    assert {notice for notice in second.notices if notice.startswith("saved_item_refused:")} == {
        f"saved_item_refused:already_captured:{name_hash(name)[:12]}"
        for name in ("later.json", "middle.json")
    }


def test_same_url_with_equal_times_keeps_the_lowest_name_hash(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    names = ("one.json", "two.json")
    for name in names:
        _write(folder, name, _record(7, why=f"Reason from {name}"))
    batch = source.fetch(selection, options, None)
    assert [_reason(item) for item in batch.intakes] == [f"Reason from {min(names, key=name_hash)}"]


def test_unchanged_files_are_not_opened_on_a_second_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(501):
        _write(folder, f"r{number}.json", _record(number))
    batches = _drain(source, selection, options)
    assert sum(len(batch.intakes) for batch in batches) == 501
    opened: list[str] = []
    real_open = saved_item_folder._open_entry

    def spy(name: str, folder_fd: int) -> int:
        opened.append(name)
        return real_open(name, folder_fd)

    monkeypatch.setattr(saved_item_folder, "_open_entry", spy)
    again = source.fetch(selection, options, batches[-1].checkpoint)
    assert again.intakes == () and not again.has_more and opened == []
    assert "saved_item_count:unchanged:501" in again.notices


def test_more_than_ten_thousand_files_refuse_the_scan(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(10_001):
        (folder / f"{number}.json").write_bytes(b"{}")
    with pytest.raises(LiveSourceError, match="folder_over_limit"):
        source.fetch(selection, options, None)
    assert not (tmp_path / "live" / "saved_items" / index_name(selection)).exists()


def test_named_pipe_and_entry_symlink_are_refused_without_blocking(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    os.mkfifo(folder / "pipe.json")
    _write(folder, "real.json", _record(1))
    (folder / "link.json").symlink_to(folder / "real.json")
    batch = source.fetch(selection, options, None)
    assert len(batch.intakes) == 1
    assert "saved_item_count:not_regular_file:2" in batch.notices
    assert {
        f"saved_item_refused:not_regular_file:{name_hash(name)[:12]}"
        for name in ("pipe.json", "link.json")
    } <= set(batch.notices)


def test_dataless_and_icloud_entries_are_skipped_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, selection, options, folder = _setup(tmp_path)
    _write(folder, "cloud.json", _record(1))
    (folder / ".evicted.json.icloud").write_bytes(b"placeholder")
    _write(folder, "local.json", _record(2))
    inode = (folder / "cloud.json").lstat().st_ino

    def dataless(info: os.stat_result) -> int:
        return saved_item_folder.SF_DATALESS if info.st_ino == inode else 0

    def downloaded(info: os.stat_result) -> int:
        return 0

    opened: list[str] = []
    real_open = saved_item_folder._open_entry

    def spy(name: str, folder_fd: int) -> int:
        opened.append(name)
        return real_open(name, folder_fd)

    monkeypatch.setattr(saved_item_folder, "_entry_flags", dataless)
    monkeypatch.setattr(saved_item_folder, "_open_entry", spy)
    batch = source.fetch(selection, options, None)
    assert opened == ["local.json"]
    assert len(batch.intakes) == 1 and "saved_item_count:placeholder:2" in batch.notices
    source.acknowledge(selection, batch.checkpoint)
    monkeypatch.setattr(saved_item_folder, "_entry_flags", downloaded)
    later = source.fetch(selection, options, batch.checkpoint)
    assert [_reason(item) for item in later.intakes] == ["Synthetic reason 1"]


def test_refusal_report_names_entries_only_by_hash_prefix(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    name = "bad\nname\x07.json"
    (folder / name).write_bytes(b"{not json")
    batch = source.fetch(selection, options, None)
    assert batch.notices == (
        "saved_item_count:invalid_json:1",
        f"saved_item_refused:invalid_json:{name_hash(name)[:12]}",
    )
    assert all("bad" not in notice and "\n" not in notice for notice in batch.notices)


def test_index_advances_only_after_acknowledge_and_prunes_vanished_files(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    _write(folder, "a.json", _record(1))
    first = source.fetch(selection, options, None)
    again = source.fetch(selection, options, None)
    assert [item.key for item in again.intakes] == [item.key for item in first.intakes]
    source.acknowledge(selection, first.checkpoint)
    source.acknowledge(selection, first.checkpoint)
    store = PrivateJsonStore(tmp_path / "live" / "saved_items")
    index = load_index(store, index_name(selection))
    assert index.entries[name_hash("a.json")].endswith(":delivered")
    assert source.fetch(selection, options, first.checkpoint).intakes == ()
    (folder / "a.json").unlink()
    _drain(source, selection, options)
    index = load_index(store, index_name(selection))
    assert name_hash("a.json") not in index.entries and len(index.delivered) == 1


def test_local_outcomes_past_the_page_cap_continue_on_the_next_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_live, "MAX_PAGE_OUTCOMES", 2)
    source, selection, options, folder = _setup(tmp_path)
    for number in range(5):
        (folder / f"bad-{number}.json").write_bytes(b"{")
    batches = _drain(source, selection, options)
    assert [batch.has_more for batch in batches] == [True, True, False]
    index = load_index(PrivateJsonStore(tmp_path / "live" / "saved_items"), index_name(selection))
    assert sorted(value.partition(":")[2] for value in index.entries.values()) == [
        "invalid_json"
    ] * 5


def test_page_outcome_cap_keeps_the_checkpoint_bounded() -> None:
    refused = ["a" * 32, "b" * 24, "payload_too_large", "", ""]
    delivered = ["a" * 32, "b" * 24, "delivered", "c" * 32, "d" * 32]
    worst = {
        "complete": False,
        "folder_path": "/" + "p" * 4096,
        "outcomes": [refused] * MAX_PAGE_OUTCOMES + [delivered] * PAGE_INTAKES,
        "schema_version": 1,
    }
    assert len(bounded_json(worst)) < 131_072


def test_unsafe_replaced_or_misconfigured_folder_refuses_the_scan(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    bad_options: tuple[dict[str, object], ...] = (
        {"folder_path": "relative"},
        {**options, "extra": 1},
    )
    for bad in bad_options:
        with pytest.raises(LiveSourceError, match="source_invalid_options"):
            source.fetch(selection, bad, None)
    folder.chmod(0o777)
    try:
        with pytest.raises(LiveSourceError, match="folder_unsafe"):
            source.fetch(selection, options, None)
    finally:
        folder.chmod(0o700)
    folder.rename(tmp_path / "moved")
    folder.mkdir(mode=0o700)
    with pytest.raises(LiveSourceError, match="source_selection_mismatch"):
        source.fetch(selection, options, None)


def test_priority_runtime_routes_saved_items_on_the_plain_capture_path(tmp_path: Path) -> None:
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    selection, resolved = select_folder(str(folder))
    _write(resolved, "a.json", _record(1))
    runtime = PriorityRuntime(tmp_path / "live")
    batch = runtime.fetch(selection, {"folder_path": str(resolved)}, None)
    assert len(batch.intakes) == 1
    runtime.acknowledge(selection, batch.checkpoint)
    assert (runtime.root / "saved_items" / index_name(selection)).is_file()
    assert revision_store_dirname("saved_item") is None


def test_failed_delivery_is_redelivered_and_the_index_follows_the_commit(tmp_path: Path) -> None:
    compile_single_user_local(tmp_path / "brain")
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    selection, resolved = select_folder(str(folder))
    _write(resolved, "a.json", _record(1))
    runtime = PriorityRuntime(tmp_path / "live")
    delivered: list[SourceRecordIntake] = []
    fail = True

    def sink(intake: SourceRecordIntake) -> None:
        if fail:
            raise RuntimeError("synthetic delivery failure")
        delivered.append(intake)

    service = LiveCaptureService(
        tmp_path / "state", tmp_path / "brain", runtime=runtime, sink=sink
    )
    service.configure("saved", selection, {"folder_path": str(resolved), "privacy_tier": "work"})
    preview = service.preview("saved")
    with pytest.raises(RuntimeError, match="synthetic delivery failure"):
        service.apply("saved", cast(str, preview["preview_id"]))
    store = PrivateJsonStore(runtime.root / "saved_items")
    assert load_index(store, index_name(selection)).entries == {}
    fail = False
    again = service.preview("saved")
    assert again["count"] == 1
    service.apply("saved", cast(str, again["preview_id"]))
    assert len(delivered) == 1 and delivered[0].privacy.tier.value == "work"
    assert load_index(store, index_name(selection)).entries[name_hash("a.json")].endswith(
        ":delivered"
    )
    assert service.preview("saved")["count"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/collector/tests/integration/test_saved_item_live.py::test_thirty_new_files_arrive_in_two_pages packages/collector/tests/integration/test_live_capture.py::test_selection_admits_every_drive_shape_and_rejects_foreign_resource_types`
Expected: FAIL. `test_saved_item_live.py` errors at collection with `ModuleNotFoundError: No module named 'open_brain_collector.saved_item_live'`; the `_PROVIDERS` pin fails its dict equality (no `saved_item` key).

- [ ] **Step 3: Write the live source and wire it**

Create `packages/collector/src/open_brain_collector/saved_item_live.py`:

```python
"""Selected saved-item folder capture on the live-source runtime.

One fetch is one page: at most 25 new items under a 1 MiB encoded budget. Proposed
outcomes ride in the checkpoint; the known-file index advances only in ``acknowledge``,
which the collector calls after it commits that checkpoint.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from open_brain_collector.saved_item_folder import (
    list_records,
    name_hash,
    open_folder,
    read_entry,
)
from open_brain_collector.saved_item_index import index_name, load_index, save_index
from open_brain_connectors.runtime.live_common import LiveBatch, LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import (
    SavedItemRefusal,
    SavedItemSourceAdapter,
    content_digest,
    encoded_intake_size,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

PAGE_INTAKES = 25
PAGE_BYTES = 1_048_576
MAX_PAGE_OUTCOMES = 500
_NOTICE_LIMIT = 25
Outcome = tuple[str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    received_at: datetime
    name_hash: str
    fingerprint: str
    item_key: str
    content: str
    size: int
    intake: SourceRecordIntake

    @property
    def order(self) -> tuple[datetime, str]:
        return (self.received_at, self.name_hash)


def _folder_path(value: object) -> Path:
    if type(value) is not str or not value or "\x00" in value or not Path(value).is_absolute():
        raise LiveSourceError("source_invalid_options")
    return Path(value)


def _options(options: object) -> Path:
    if not isinstance(options, dict) or set(options) != {"folder_path"}:
        raise LiveSourceError("source_invalid_options")
    return _folder_path(options["folder_path"])


def _checkpoint(value: object) -> tuple[Path, bool, tuple[Outcome, ...]]:
    if (
        not isinstance(value, dict)
        or set(value) != {"complete", "folder_path", "outcomes", "schema_version"}
        or value["schema_version"] != 1
        or type(value["complete"]) is not bool
        or not isinstance(value["outcomes"], list)
        or len(value["outcomes"]) > MAX_PAGE_OUTCOMES + PAGE_INTAKES
    ):
        raise LiveSourceError("saved_item_invalid_checkpoint")
    outcomes: list[Outcome] = []
    for item in cast(list[object], value["outcomes"]):
        if (
            not isinstance(item, list)
            or len(item) != 5
            or any(type(part) is not str for part in item)
        ):
            raise LiveSourceError("saved_item_invalid_checkpoint")
        outcomes.append(cast(Outcome, tuple(item)))
    try:
        folder = _folder_path(value["folder_path"])
    except LiveSourceError:
        raise LiveSourceError("saved_item_invalid_checkpoint") from None
    return folder, value["complete"], tuple(outcomes)


def _notices(counts: Counter[str], refused: list[tuple[str, str]]) -> tuple[str, ...]:
    notices = [f"saved_item_count:{outcome}:{counts[outcome]}" for outcome in sorted(counts)]
    # Other writers control names, so an entry is reported only by its name-hash prefix.
    notices.extend(f"saved_item_refused:{code}:{hashed[:12]}" for hashed, code in refused)
    return tuple(notices[:_NOTICE_LIMIT])


class SavedItemLiveSource:
    """One page of new saved items per fetch; the index advances only on acknowledge."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise LiveSourceError("source_invalid_value")
        self._root = root

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        folder_path = _options(options)
        if checkpoint is not None and _checkpoint(checkpoint)[0] != folder_path:
            raise LiveSourceError("source_selection_mismatch")
        store = PrivateJsonStore(self._root)
        with open_folder(folder_path) as folder:
            if folder.selection != selection:
                raise LiveSourceError("source_selection_mismatch")
            listing = list_records(folder)
            index = load_index(store, index_name(selection))
            adapter = SavedItemSourceAdapter(selection)
            counts: Counter[str] = Counter()
            if listing.placeholders:
                counts["placeholder"] = listing.placeholders
            outcomes: list[Outcome] = []
            refused: list[tuple[str, str]] = []
            page: dict[str, _Candidate] = {}
            page_bytes = 0
            has_more = False
            for name in listing.records:
                if len(outcomes) >= MAX_PAGE_OUTCOMES:
                    has_more = True
                    break
                entry = read_entry(folder, name, index.fingerprint(name_hash(name)))
                if entry.status != "read":
                    counts[entry.code or entry.status] += 1
                    if entry.status == "refused":
                        outcomes.append((entry.name_hash, entry.fingerprint, entry.code, "", ""))
                        refused.append((entry.name_hash, entry.code))
                    continue
                try:
                    record = adapter.record_from_bytes(entry.data)
                    intake = adapter.intake(record)
                except SavedItemRefusal as refusal:
                    counts[refusal.code] += 1
                    outcomes.append((entry.name_hash, entry.fingerprint, refusal.code, "", ""))
                    refused.append((entry.name_hash, refusal.code))
                    continue
                content = content_digest(intake)
                delivered = index.delivered.get(record.item_key)
                if delivered is not None:
                    state = "skipped" if delivered == content else "already_captured"
                    counts[state] += 1
                    outcomes.append((entry.name_hash, entry.fingerprint, state, "", ""))
                    if state == "already_captured":
                        refused.append((entry.name_hash, state))
                    continue
                candidate = _Candidate(
                    record.received_at,
                    entry.name_hash,
                    entry.fingerprint,
                    record.item_key,
                    content,
                    encoded_intake_size(intake),
                    intake,
                )
                current = page.get(record.item_key)
                if current is not None:
                    # The loser stays unindexed; first-save-wins settles it next page.
                    counts["deferred_duplicate"] += 1
                    has_more = True
                    if (
                        candidate.order < current.order
                        and page_bytes - current.size + candidate.size <= PAGE_BYTES
                    ):
                        page_bytes += candidate.size - current.size
                        page[record.item_key] = candidate
                    continue
                if len(page) == PAGE_INTAKES or page_bytes + candidate.size > PAGE_BYTES:
                    has_more = True
                    break
                page[record.item_key] = candidate
                page_bytes += candidate.size
        winners = sorted(page.values(), key=lambda item: item.order)
        if winners:
            counts["delivered"] = len(winners)
        outcomes.extend(
            (item.name_hash, item.fingerprint, "delivered", item.item_key, item.content)
            for item in winners
        )
        proposed: dict[str, object] = {
            "complete": not has_more,
            "folder_path": str(folder_path),
            "outcomes": [list(item) for item in outcomes],
            "schema_version": 1,
        }
        return LiveBatch(
            intakes=tuple(item.intake for item in winners),
            checkpoint=proposed,
            has_more=has_more,
            notices=_notices(counts, refused),
        )

    def acknowledge(
        self,
        selection: SourceResourceSelection,
        checkpoint: dict[str, object],
    ) -> None:
        """Record outcomes the caller's committed checkpoint proves final.

        Every intake of a committed page has a terminal custody receipt (captured,
        duplicate or quarantined); quarantine is owned by collector recovery.
        Repeating this call is safe.
        """
        folder_path, complete, outcomes = _checkpoint(checkpoint)
        store = PrivateJsonStore(self._root)
        name = index_name(selection)
        with store.lock(name.removesuffix(".json")):
            with open_folder(folder_path) as folder:
                if folder.selection != selection:
                    raise LiveSourceError("source_selection_mismatch")
                present = (
                    {name_hash(entry) for entry in list_records(folder).records}
                    if complete
                    else None
                )
            index = load_index(store, name)
            for hashed, fingerprint, state, item_key, content in outcomes:
                index.record(hashed, fingerprint, state, item_key, content)
            if present is not None:
                index.prune(present)
            save_index(store, name, index)
```

In `live_capture.py`, replace

```python
    "agent_session": frozenset({"local_project"}),
}
```

with

```python
    "agent_session": frozenset({"local_project"}),
    "saved_item": frozenset({"local_folder"}),
}
```

In `live_manager.py`, after `from open_brain_collector.runner import CollectorProfileError, _open_existing_collector_profile` add:

```python
from open_brain_collector.saved_item_live import SavedItemLiveSource
```

In `PriorityRuntime.fetch`, after the `agent_session` block

```python
        if selection.connector_name == "agent_session":
            return AgentSessionLiveSource(self.root / "sessions").fetch(
                selection, options, checkpoint
            )
```

add

```python
        if selection.connector_name == "saved_item":
            return SavedItemLiveSource(self.root / "saved_items").fetch(
                selection, options, checkpoint
            )
```

In `PriorityRuntime.acknowledge`, after

```python
        if selection.connector_name == "agent_session":
            AgentSessionLiveSource(self.root / "sessions").acknowledge(selection, checkpoint)
```

add

```python
        if selection.connector_name == "saved_item":
            SavedItemLiveSource(self.root / "saved_items").acknowledge(selection, checkpoint)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/collector/tests/integration/test_saved_item_live.py packages/collector/tests/integration/test_live_capture.py packages/collector/tests/integration/test_live_manager.py packages/collector/tests/contract`
Expected: PASS. The 10,001-file and 501-file tests take a few seconds each.

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/collector/src/open_brain_collector/saved_item_live.py packages/collector/src/open_brain_collector/live_capture.py packages/collector/src/open_brain_collector/live_manager.py packages/collector/tests/integration/test_saved_item_live.py packages/collector/tests/integration/test_live_capture.py
uv run --frozen mypy packages/collector/src/open_brain_collector packages/collector/tests/integration/test_saved_item_live.py
git add packages/collector/src/open_brain_collector/saved_item_live.py \
  packages/collector/src/open_brain_collector/live_capture.py \
  packages/collector/src/open_brain_collector/live_manager.py \
  packages/collector/tests/integration/test_saved_item_live.py \
  packages/collector/tests/integration/test_live_capture.py
printf 'feat(collector): page saved item folders through the live-source runtime\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: sonnet:high -->
### Task 8: `saved-item-select` CLI step and the end-to-end capture test

**Files:**
- Modify: `packages/collector/src/open_brain_collector/live_manager.py` (import, `OPERATIONS` at :39-77, new `dispatch` branch before `if operation == "sources.session_preview":` at :368)
- Modify: `packages/collector/src/open_brain_collector/sources_cli.py` (new subparser after the `session-apply` parser at :352)
- Modify: `packages/collector/tests/integration/test_live_manager.py` (imports, append test)
- Create: `packages/collector/tests/integration/test_saved_item_capture.py`

**Interfaces:**
- Consumes: Task 5 `select_folder(raw: str) -> tuple[SourceResourceSelection, Path]`; `safe_text(value: object, *, maximum: int = 512) -> str`; `_shape`.
- Produces: operation `sources.saved_item_select` with arguments exactly `{"folder_path": str, "privacy_tier": "public"|"work"|"personal"|"secret"}`, returning `{"selection": asdict(selection), "options": {"folder_path": "<resolved>", "privacy_tier": tier}}`; CLI subcommand `saved-item-select --folder-path PATH [--privacy-tier TIER]` (default `personal`).

- [ ] **Step 1: Write the failing tests**

In `packages/collector/tests/integration/test_live_manager.py`, add `from dataclasses import asdict` to the standard-library imports and `from open_brain_collector.saved_item_folder import select_folder` after `from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime`. Append:

```python
def test_saved_item_select_derives_the_selection_and_configure_accepts_it(
    tmp_path: Path,
) -> None:
    manager, _project = _manager(tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    result = manager.dispatch(
        "sources.saved_item_select", {"folder_path": str(folder), "privacy_tier": "work"}
    )
    selection, resolved = select_folder(str(folder))
    assert result == {
        "selection": asdict(selection),
        "options": {"folder_path": str(resolved), "privacy_tier": "work"},
    }
    summary = manager.dispatch("sources.configure", result)
    assert summary["selection"] == asdict(selection) and summary["status"] == "disabled"
    for arguments, code in (
        ({"folder_path": str(folder), "privacy_tier": "unknown"}, "source_invalid_arguments"),
        ({"folder_path": str(folder)}, "source_invalid_arguments"),
        ({"folder_path": str(tmp_path / "absent"), "privacy_tier": "personal"}, "folder_missing"),
    ):
        with pytest.raises(LiveSourceError, match=code):
            manager.dispatch("sources.saved_item_select", arguments)
```

Create `packages/collector/tests/integration/test_saved_item_capture.py`:

```python
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast

from open_brain_engine.engine import open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_collector import sources_cli
from open_brain_collector.live_manager import LiveSourceManager


def _cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = sources_cli.main(argv)
    return code, cast(dict[str, object], json.loads(output.getvalue()))


def _record(url: str, why: str, body: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "url": url,
            "why": why,
            "received_at": "2026-10-26T14:00:00Z",
            "fetch_status": "complete",
            "title": "Synthetic saved item",
            "body": body,
        }
    )


def test_saved_folder_flows_from_cli_selection_to_search(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    (folder / "first.json").write_text(
        _record(
            "https://example.com/post/123?utm_source=share#comments",
            "Saving for the Quietharbor dinner",
            "Charredlemon pasta with capers",
        )
    )
    state = tmp_path / "collector" / "state.json"
    base = ["--state", str(state), "--brain-root", str(tmp_path / "brain"), "--foreground"]

    code, selected = _cli([*base, "saved-item-select", "--folder-path", str(folder)])
    assert code == 0
    assert cast(dict[str, object], selected["options"])["privacy_tier"] == "personal"
    arguments = tmp_path / "selection.json"
    arguments.write_text(json.dumps(selected))
    code, configured = _cli([*base, "configure", "--arguments-file", str(arguments)])
    assert code == 0
    source_id = cast(str, configured["source_id"])
    code, preview = _cli([*base, "preview", "--source-id", source_id])
    assert code == 0 and preview["count"] == 1
    records = cast(list[dict[str, object]], preview["records"])
    assert records[0]["source_reference"] == "https://example.com/post/123"
    preview_id = cast(str, preview["preview_id"])
    code, imported = _cli(
        [*base, "import", "--source-id", source_id, "--preview-id", preview_id]
    )
    assert code == 0 and imported["captured_count"] == 1
    engine = open_local_engine(profile)
    for query in ("Charredlemon", "Quietharbor"):
        hits = engine.retrieval.search(query)
        assert len(hits) == 1 and hits[0].trust == "third_party"

    # Scheduled path: run-once drives LiveSourceManager.sync_due for enabled sources.
    (folder / "second.json").write_text(
        _record("https://example.com/post/456", "Gift idea for Brightmoss", "Ceramic teapot")
    )
    enable = ["control", "--source-id", source_id, "--action", "enable"]
    code, _enabled = _cli([*base, *enable, "--interval-seconds", "900"])
    assert code == 0
    results = LiveSourceManager(state.parent / "live", tmp_path / "brain").sync_due(
        process_developments=False
    )
    captured = [item.get("captured_count") for item in results if item["source_id"] == source_id]
    assert captured == [1]
    assert len(open_local_engine(profile).retrieval.search("Brightmoss")) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest -q packages/collector/tests/integration/test_live_manager.py::test_saved_item_select_derives_the_selection_and_configure_accepts_it packages/collector/tests/integration/test_saved_item_capture.py::test_saved_folder_flows_from_cli_selection_to_search`
Expected: FAIL. The manager test raises `LiveSourceError: source_invalid_operation`; the CLI test ends with `SystemExit: 2` (argparse: `invalid choice: 'saved-item-select'`).

- [ ] **Step 3: Add the operation and subcommand**

In `live_manager.py`, after the `SavedItemLiveSource` import add:

```python
from open_brain_collector.saved_item_folder import select_folder
```

(keep the two `saved_item_*` imports in alphabetical order: `saved_item_folder` before `saved_item_live`). In `OPERATIONS`, after `"sources.session_apply",` add:

```python
        "sources.saved_item_select",
```

In `dispatch`, immediately before

```python
        if operation == "sources.session_preview":
            return self._session_preview(args)
```

add

```python
        if operation == "sources.saved_item_select":
            _shape(args, {"folder_path", "privacy_tier"})
            tier = args["privacy_tier"]
            if tier not in {"public", "work", "personal", "secret"}:
                raise LiveSourceError("source_invalid_arguments")
            selection, folder = select_folder(safe_text(args["folder_path"], maximum=4096))
            return {
                "selection": asdict(selection),
                "options": {"folder_path": str(folder), "privacy_tier": tier},
            }
```

In `sources_cli.py`, after

```python
    commands.add_parser("session-apply").add_argument("--preview-id", required=True)
```

add

```python
    saved_item = commands.add_parser("saved-item-select")
    saved_item.add_argument("--folder-path", required=True)
    saved_item.add_argument(
        "--privacy-tier", choices=("public", "work", "personal", "secret"), default="personal"
    )
```

`_command_arguments` already turns these into `{"folder_path": ..., "privacy_tier": ...}`, and the command name maps to `sources.saved_item_select`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -q packages/collector/tests/integration/test_live_manager.py packages/collector/tests/integration/test_sources_cli.py packages/collector/tests/integration/test_saved_item_capture.py`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck and commit**

```sh
uv run --frozen ruff check --fix packages/collector/src/open_brain_collector/live_manager.py packages/collector/src/open_brain_collector/sources_cli.py packages/collector/tests/integration/test_live_manager.py packages/collector/tests/integration/test_saved_item_capture.py
uv run --frozen mypy packages/collector/src/open_brain_collector packages/collector/tests/integration/test_live_manager.py packages/collector/tests/integration/test_saved_item_capture.py
git add packages/collector/src/open_brain_collector/live_manager.py \
  packages/collector/src/open_brain_collector/sources_cli.py \
  packages/collector/tests/integration/test_live_manager.py \
  packages/collector/tests/integration/test_saved_item_capture.py
printf 'feat(collector): select a saved item folder from the sources CLI\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: sonnet:high -->
### Task 9: Integration guide, doc test, contributor line and gotcha CAPTURE-004

**Files:**
- Create: `docs/integrations/saved-items.md`
- Create: `tests/release/test_saved_items_docs.py`
- Modify: `CLAUDE.md:34` (one line after the Drive sentence)
- Modify: `docs/engineering/gotchas/README.md:5` (new entry above `### DOCS-005`)

**Interfaces:**
- Consumes: `RECORD_REFUSAL_CODES`, `TRACKING_RULE_VERSION` (Tasks 1-2).
- Produces: documentation only.

- [ ] **Step 1: Write the failing test**

Create `tests/release/test_saved_items_docs.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from open_brain_connectors.runtime.saved_item import RECORD_REFUSAL_CODES, TRACKING_RULE_VERSION

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/integrations/saved-items.md"
CONTRIBUTOR_GUIDE = ROOT / "CLAUDE.md"
GOTCHAS = ROOT / "docs/engineering/gotchas/README.md"
PROPER_NOUNS = ("Open", "Brain", "iOS", "Shortcut", "iCloud", "Drive", "JSON", "URL")
TELLS = ("not just", "seamless", "robust", "powerful", "effortless", "dive in")
FENCE = chr(96) * 3
THIN = {
    "schema_version": 1,
    "url": "https://example.com/post/123",
    "why": "Recipe to try this weekend",
    "received_at": "2026-10-26T14:00:00Z",
    "fetch_status": "not_attempted",
}
SECTIONS = (
    "## Record format",
    "## What a capture contains",
    "## The stated reason",
    "## Identity and tracking parameters",
    "## First save wins",
    "## Select a folder you control",
    "## Set up the source",
    "## Refusals and reports",
    "## Example iOS Shortcut",
)


def _check_headings_and_tone(text: str) -> None:
    headings = [line for line in text.splitlines() if line.startswith("#")]
    for heading in headings[1:]:
        words = heading.lstrip("# ").split()
        assert words[0][0].isupper() and all(
            word == word.lower() or word.startswith(PROPER_NOUNS) for word in words[1:]
        ), heading
    lowered = text.lower()
    for tell in TELLS:
        assert tell not in lowered, tell
    assert "/Users/" not in text


def _json_block_containing(text: str, needle: str) -> object:
    # FENCE is built from chr(96) so this file never contains a literal Markdown fence.
    for block in text.split(FENCE + "json")[1:]:
        body = block.split(FENCE, 1)[0]
        if needle in body:
            parsed: object = json.loads(body)
            return parsed
    raise AssertionError(f"no fenced json block contains {needle!r}")


def test_saved_items_guide_documents_the_record_contract() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    _check_headings_and_tone(text)
    positions = [text.index(section) for section in SECTIONS]
    assert positions == sorted(positions)
    assert _json_block_containing(text, '"fetch_status": "not_attempted"') == THIN
    for row in (
        "| Any | `utm_*`, `fbclid`, `gclid` |",
        "| `instagram.com`, `threads.net`, `threads.com` | `igsh`, `igshid` |",
        "| `youtube.com`, `youtu.be`, `spotify.com` | `si` |",
    ):
        assert text.count(row) == 1, row
    assert f"Tracking rule version {TRACKING_RULE_VERSION} " in text
    codes = RECORD_REFUSAL_CODES | {
        "already_captured",
        "not_regular_file",
        "folder_missing",
        "folder_unsafe",
        "folder_over_limit",
    }
    for code in sorted(codes):
        assert f"`{code}`" in text, code
    for phrase in (
        "`Stated reason`",
        "first writer of a URL wins",
        "`[truncated]`",
        "131,072 bytes",
        "65,536 bytes",
        "at most 25 items",
        "1 MiB",
        "Why are you saving this?",
        "Share Sheet",
    ):
        assert phrase in text, phrase


def test_saved_items_guide_gives_the_exact_selection_commands() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "--foreground saved-item-select --folder-path /absolute/path/to/saved-items" in text
    assert "--privacy-tier personal > saved-items-selection.json" in text
    assert '--foreground configure --arguments-file "$PWD/saved-items-selection.json"' in text
    assert "not on any revision allow-list" in text


def test_contributor_guide_names_the_saved_item_source() -> None:
    text = CONTRIBUTOR_GUIDE.read_text(encoding="utf-8")
    sentence = (
        "Saved item capture reads producer-written JSON records from one selected folder "
        "(`docs/integrations/saved-items.md`)."
    )
    assert text.count(sentence) == 1
    drive = text.index("Drive capture selects a file, a folder of Docs")
    assert text.index("## Product boundary") < drive < text.index(sentence)
    assert text.index(sentence) < text.index("## Architecture")


def test_gotcha_registry_records_the_redaction_breadth() -> None:
    text = GOTCHAS.read_text(encoding="utf-8")
    assert text.count("### CAPTURE-004:") == 1
    entry = text[text.index("### CAPTURE-004:") :]
    entry = entry[: entry.index("\n### ", 1)]
    for token in (
        "Category: Capture / Saved items.",
        "Symptom:",
        "Action:",
        "Discovered: 2026-10-26",
        "`has_redaction_finding`",
        "`redact_text`",
        "`url_secret`",
        "`fbclid`",
        "docs/integrations/saved-items.md",
    ):
        assert token in entry, token
    assert text.index("### CAPTURE-004:") < text.index("### DOCS-005:")
    assert "/Users/" not in entry
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -q tests/release/test_saved_items_docs.py::test_saved_items_guide_documents_the_record_contract tests/release/test_saved_items_docs.py::test_contributor_guide_names_the_saved_item_source`
Expected: FAIL with `FileNotFoundError` for `docs/integrations/saved-items.md`, and an assertion failure (`0 == 1`) for the missing `CLAUDE.md` sentence.

- [ ] **Step 3: Write the guide, the contributor line and the gotcha**

Create `docs/integrations/saved-items.md` with exactly this content:

````markdown
# Saved items

> Optional collector source. It is not part of the core install; it needs the
> `open-brain-collector` distribution and runs only after you configure it.

A saved item is a URL you chose to keep and the reason you kept it: a social post, an article, a
video, an event page, a product or a place. A producer, such as an iOS Shortcut, a bookmarklet or a
script, writes one small JSON file per item into a folder you select. The collector reads the folder
and captures each new item as a searchable record whose text includes your reason.

The collector never opens the URL. Text the producer already extracted, such as a caption, a
transcript or a summary, is captured as text. Media files never enter Open Brain.

## Record format

Each item is one UTF-8 JSON object in its own `*.json` file of at most 131,072 bytes. The file name
carries no meaning. Write each item once, atomically, under a name that does not change.

| Field | Type | Rule |
|---|---|---|
| `schema_version` | int | `1` |
| `url` | string | `https` only |
| `why` | string | Required; after trimming, 1 to 280 characters, one line, no control characters |
| `received_at` | string | RFC 3339 UTC time the producer received the save, ending in `Z` |
| `fetch_status` | string | `not_attempted`, `complete`, `partial` or `failed` |
| `source_type` | string or null | `youtube`, `social`, `web` or `text`; derived from the URL host when null |
| `content_kind` | string or null | `event`, `article`, `product`, `place`, `post`, `video` or `other`; `other` when null |
| `platform` | string or null | Lowercase site or app name, at most 40 characters |
| `title` | string or null | At most 300 characters |
| `author` | string or null | At most 200 characters |
| `body` | string | Extracted text or caption; may be empty |
| `media_text` | string | Transcript or image description; may be empty |
| `shared_text` | string | Text the capture surface passed along; may be empty |
| `summary` | string or null | Producer summary |
| `summary_origin` | string or null | Required when `summary` is set, `producer:<name>` |

Only `schema_version`, `url`, `why`, `received_at` and `fetch_status` are required; optional fields
may be left out. Unknown fields are refused, so a later format version is never half-read. A
minimal record:

```json
{"schema_version": 1, "url": "https://example.com/post/123", "why": "Recipe to try this weekend", "received_at": "2026-10-26T14:00:00Z", "fetch_status": "not_attempted"}
```

A record whose fetch failed, or was never attempted, is still captured with its URL, reason and
shared text.

## What a capture contains

The capture text is a list of sections in this order, and empty sections are left out:
`Stated reason`, `Summary (<origin>)`, `Body`, `Media text`, `Shared text`, `Kind` and
`Fetch status`. The title is the record title. Without one it is `<author> on <platform>`, and
without those it is the stored URL. Titles are cut to 200 characters.

Before anything is stored, the title and every section are masked with the capture redaction
policy: email addresses, phone numbers, `token=` values and any run of 32 or more letters, digits,
`_` or `-` are replaced. The save still lands.

Long records are cut to fit the collector's plain capture path: at most 60,000 characters of text
and 65,536 bytes of encoded payload, where one non-Latin character can cost six bytes. `Media text`
is cut first, then `Body`, then `Shared text`, each ending in a visible `[truncated]` marker. A
record is refused as `payload_too_large` only if it still does not fit after that.

## The stated reason

Your reason is stored as the `Stated reason` section of the capture text, so search finds it. It
is not an owner-authored capture reason. Every collector delivery goes through the public-job
capture path, which cannot claim that the owner wrote the text.

## Identity and tracking parameters

An item's identity comes from its URL, within one selected folder. The collector lowercases the
host, drops the default port, uppercases percent escapes and removes tracking parameters. The rest
of the query and the fragment stay in the identity, because some apps put the item id in the
fragment: `https://app.example/#/note/123` and `https://app.example/#/note/456` are different
items. The fragment is never stored. The stored and searchable URL is the identity without it.

Tracking rule version 1 removes these parameters:

| Hosts (and subdomains) | Removed parameters |
|---|---|
| Any | `utm_*`, `fbclid`, `gclid` |
| `instagram.com`, `threads.net`, `threads.com` | `igsh`, `igshid` |
| `youtube.com`, `youtu.be`, `spotify.com` | `si` |

Generic names such as `ref` and `si` are kept on every other host. Host aliases and short links are
not expanded, so `youtu.be/<id>` and `youtube.com/watch?v=<id>` are two items. A change to this
table is a new rule version and affects new saves only.

If the query that remains after removal, or the fragment, looks like a secret (a `token=` value, an
email address, or a run of 32 or more letters, digits, `_` or `-`), the record is refused as
`url_secret`. The URL path is never checked, because long ids are normal there.

## First save wins

The first record for an identity in a folder is captured. A later record with the same identity
and the same admitted text is skipped. One with different text is refused as `already_captured`
and counted in the scan report; it does not replace the first capture. When two records for the
same URL arrive in one scan, the earlier `received_at` wins. Revisions of a saved item are not
supported yet.

## Select a folder you control

Anyone who can write into the folder can add saves, and the first writer of a URL wins. Select a
folder that only you can write to. The collector refuses a folder that is a symlink, is not owned
by you, or is writable by group or others.

A cloud-synced folder works. Names starting with `.` or ending in `.icloud`, `.tmp` or `.partial`
are skipped, and on macOS a file that is still a cloud placeholder is skipped without being read,
so a scan never forces a download. Skipped files are retried on a later scan.

The collector never deletes or moves files; clean the folder up on the producer side. Two selected
folders are two sources: the same URL in both becomes two captures, each with its own tier.

## Set up the source

Turn a folder into a selection. The tier defaults to `personal`; pass `--privacy-tier` with
`public`, `work`, `personal` or `secret` to choose another.

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground saved-item-select --folder-path /absolute/path/to/saved-items \
  --privacy-tier personal > saved-items-selection.json
```

The output is the arguments file for `configure`. From there the usual live-source commands drive
the source:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground configure --arguments-file "$PWD/saved-items-selection.json"
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground preview --source-id REPLACE_FROM_CONFIGURE
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground import --source-id REPLACE_FROM_CONFIGURE --preview-id REPLACE_FROM_PREVIEW
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground control --source-id REPLACE_FROM_CONFIGURE --action enable --interval-seconds 900
```

One preview holds at most 25 items and at most 1 MiB of encoded text. When more items wait, the
preview reports `has_more`; repeat preview and import, or let a scheduled run continue one second
later. `saved_item` uses the plain capture path and is not on any revision allow-list. See
[priority-capture-operator.md](priority-capture-operator.md) for state paths and
[collector-recovery.md](collector-recovery.md) for custody failures.

## Refusals and reports

| Code | Meaning |
|---|---|
| `invalid_json` | Not UTF-8 JSON, not an object, a duplicate key or a non-finite number |
| `unknown_field` | A field outside the format |
| `missing_field` | A required field is absent |
| `invalid_field` | Wrong type, length, enum value, time format or summary origin |
| `invalid_url` | Not an `https` URL, or not a valid one |
| `invalid_why` | Blank after trimming, over 280 characters, or more than one line |
| `url_secret` | The remaining query or the fragment looks like a secret |
| `payload_too_large` | Still too large after truncation |
| `file_too_large` | The file is over 131,072 bytes |
| `not_regular_file` | A symlink, pipe, socket, device or directory named `*.json` |
| `already_captured` | Same identity as a captured item, different text |

A refused file is not retried until it changes. Three scan-level refusals deliver nothing:
`folder_missing`, `folder_unsafe` (a symlink, another owner, or a group- or world-writable mode)
and `folder_over_limit` (more than 10,000 `*.json` entries).

A scan reports counts per outcome as `saved_item_count:<outcome>:<n>` notices and names a refused
file only as `saved_item_refused:<code>:<prefix>`, where the prefix is the first 12 characters of
the SHA-256 of its name. File names are never reported, because other writers control them.

The collector keeps a private index of the files it has finished with, so an unchanged file is not
opened again. If that index is lost, every file is read again; identical items deduplicate in
custody, and a changed item becomes a conflict that the usual collector recovery handles.

## Example iOS Shortcut

This Shortcut saves a thin record from the Share Sheet into a folder in iCloud Drive. It uses only
standard Shortcuts actions.

1. Create a Shortcut that shows in the Share Sheet and receives URLs.
2. Add Ask for Input with the prompt "Why are you saving this?".
3. Add Adjust Date to shift Current Date to UTC (subtract your offset from UTC), then Format Date
   with the custom format `yyyy-MM-dd'T'HH:mm:ss'Z'`.
4. Add a Dictionary with `schema_version` (Number 1), `url` (Shortcut Input), `why` (Provided
   Input), `received_at` (Formatted Date) and `fetch_status` (Text `not_attempted`).
5. Add Save File with the Dictionary, Ask Where to Save turned off, and a subpath of
   `saved-items/` followed by the date formatted as `yyyyMMdd-HHmmss` and `.json`.

On the Mac that runs the collector, select the `saved-items` folder inside the Shortcuts folder of
iCloud Drive. A wrong UTC offset only changes which of two saves of the same URL in one scan wins;
it never drops a save.
````

In `CLAUDE.md`, replace

```
Drive capture selects a file, a folder of Docs, or account Docs by title suffix.
```

with

```
Drive capture selects a file, a folder of Docs, or account Docs by title suffix.
Saved item capture reads producer-written JSON records from one selected folder (`docs/integrations/saved-items.md`).
```

In `docs/engineering/gotchas/README.md`, replace

```
## Registry

### DOCS-005: The Drive guide said an edited single file re-imports; the engine quarantines it
```

with

```
## Registry

### CAPTURE-004: The capture redaction check flags any 32-character run

Category: Capture / Saved items.

Symptom: the first saved item design refused any record with a redaction finding.
`has_redaction_finding` (`open_brain_engine/capture/redaction.py`) is broader than
secrets: any run of 32 or more letters, digits, `_` or `-` counts, as do email
addresses, phone numbers and `token=` assignments. Measured on 2026-10-26: a
hyphenated 70-character article slug, a 36-hex `id` query value and an `fbclid`
value all trigger it. Refusing on it would drop ordinary article saves and most
links shared from Facebook.

Action: saved items mask the title and every text section with `redact_text` and
keep the save. Only the URL query that survives tracking-parameter removal and the
fragment are checked, and a hit refuses the record as `url_secret`; the URL path is
never checked. See docs/integrations/saved-items.md. Use the finding check as a
refusal gate only on values that should never carry long identifiers.

Discovered: 2026-10-26, saved item capture spec review.

### DOCS-005: The Drive guide said an edited single file re-imports; the engine quarantines it
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --frozen pytest -q tests/release/test_saved_items_docs.py tests/release/test_drive_docs_sets_docs.py`
Expected: PASS (the Drive doc test still finds its sentence and DOCS-005 below BUILD-021).

- [ ] **Step 5: Commit**

```sh
uv run --frozen ruff check --fix tests/release/test_saved_items_docs.py
git add docs/integrations/saved-items.md tests/release/test_saved_items_docs.py CLAUDE.md \
  docs/engineering/gotchas/README.md
printf 'docs: saved item integration guide and redaction gotcha\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

---

<!-- model: sonnet:high -->
### Task 10: Final gates

**Files:** none changed unless a gate fails.

- [ ] **Step 1: Run the full verification**

Run: `make verify`
Expected: PASS (`ruff check .`, `mypy`, the full `pytest -q -n auto` suite, build and plugin tests).

- [ ] **Step 2: Whitespace and workflow checks**

Run: `git diff --check main...HEAD && actionlint .github/workflows/ci.yml`
Expected: no output, exit 0.

- [ ] **Step 3: Confirm no packaging surface moved**

Run: `git diff --stat main...HEAD -- release packages/*/pyproject.toml pyproject.toml uv.lock tools/open_brain_dev`
Expected: no files listed. If any appear, also run `make native-audit` and `make homebrew-smoke` and record the results.

- [ ] **Step 4: Confirm no private data entered the branch**

Run: `git diff main...HEAD | rg -n '^\+.*(/Users/[A-Za-z]|/home/[A-Za-z]|\.ts\.net|@gmail\.com)' ; test $? -eq 1`
Expected: exit 0 (no added line names a home directory, tailnet host or personal address; the doc tests' `"/Users/" not in text` guards do not match because no username follows).

- [ ] **Step 5: Fix-up commit only if a gate required a change**

If Steps 1-4 needed an edit, run the failing gate again until it passes, then:

```sh
git add <the files the fix touched>
printf 'fix: satisfy saved item capture gates\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
  > "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
git commit -F "$(git rev-parse --git-dir)/SI_COMMIT_MSG"
```

## Self-review: spec requirement to task

| Spec requirement | Task |
|---|---|
| Connector source, no engine/schema/Portable change, coordinates 23/16/18, one app catalog entry | 3, 10 |
| `source_type`/`content_kind` reuse engine values; social post is `social`/`post` | 2 |
| Reason stored as `Stated reason` text, searchable; no `capture_why` | 4, 8 (search by reason) |
| Thin records first-class; failed fetch still captures | 2, 4 |
| First save wins; identical skipped; different `already_captured`, counted | 7 |
| Identity within one folder; two folders are two sources | 5 (selection per folder), 9 |
| Tier per folder, default personal, as `privacy_tier` option | 7, 8 |
| Record format v1 fields, 131,072-byte file, unknown fields refused | 2, 5 |
| Identity URL rules, tracking table with subdomains, `ref`/`si` kept, fragment hashed never stored | 1, 4 |
| `item_key` = 32 hex of SHA-256 | 1 |
| Adapter key, URL, title fallbacks, 200-character title, section layout, `local_source_privacy()` | 4 |
| Bounds: 60,000 characters, 65,536 encoded bytes, truncation order with `[truncated]`, `payload_too_large` | 4 (deviation 4) |
| `redact_text` masking; URL path unchecked; query/fragment `url_secret` | 1, 4 (deviation 1) |
| Local refusals never become intakes | 2, 4, 7 |
| Descriptor, connectors catalog, app catalog, pin tests | 3 |
| Live-source runtime (`LiveSourceManager`, `LiveCaptureService`, `PriorityRuntime`), `agent_session` template, `_PROVIDERS`, path in options and checkpoint | 7 |
| Selection from resolved path plus `st_dev`/`st_ino`, computed before `configure` | 5, 8 |
| Folder opened once `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`, owner and mode by `fstat` | 5 |
| `scandir` on descriptor, `dir_fd` opens `O_NOFOLLOW|O_NONBLOCK`, regular files only | 5, 7 |
| Skipped names and `SF_DATALESS` placeholders, retried later | 5, 7 |
| Read up to 131,072 + 1 with before/after stat match | 5 |
| Index in its own `PrivateJsonStore` file, name-hash keys, fingerprint plus terminal state, delivered `item_key`s | 6 (deviation 3c) |
| 10,000 files and 10,000 delivered under half of 4 MiB; `folder_over_limit` | 6, 7 (deviation 3b) |
| Matching fingerprint skipped without opening; pruning on complete scans | 5, 7 |
| Terminal state only after final outcome; index write after checkpoint commit; crash rescans | 7 (deviation 2) |
| Lost index rescans; custody dedupes | 7 (docs in 9) |
| One run = one `LiveBatch` of at most 25 under 1 MiB; `has_more`; progress outside the checkpoint | 7 (deviation 3a) |
| Within-page dedupe: earliest `received_at`, then lowest name hash | 7 |
| Never deletes or moves files | 7 (no write path), 9 |
| Report counts per outcome and code; entries only by 12-character hash prefix | 7 |
| One CLI step from folder path to selection with tier; not on any revision allow-list | 7, 8 |
| Error handling table rows | 2, 4, 5, 7 |
| Testing: adapter contract tests (mapping, refusals, tracking rows, fragments, `why`, titles, bounds, masking) | 1, 2, 4 |
| Testing: folder to scan, custody and temporary Brain, search by body and reason | 8 |
| Testing: first-save-wins three cases; index ordering; pipe and symlink; dataless and `.icloud`; 501 unchanged; 10,001 files; 30 files two pages; 20 non-Latin records; index size; hash-only refusal report | 6, 7 |
| Release doc test (Drive Docs pattern) | 9 |
| Gates: `make verify`, `git diff --check`, `actionlint` | 10 |
| `docs/integrations/saved-items.md` contents and iOS Shortcut; one `CLAUDE.md` line | 9 |
