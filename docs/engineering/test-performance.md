# Python test performance

This page covers the test-tier commands, how to profile the Python suite, and the
measurements behind the October 2026 test-performance pass. `make test` and `make verify`
still run the complete suite. The tiers are for faster local feedback only.

## Commands

| Command | What it runs | Measured budget (2 workers) |
|---|---|---|
| `make test` | Complete Python suite | 1,474.9 s at `86cb78c` |
| `make test-fast` | Tests outside the integration tier | 106.5 s (4,725 passed, 5 skipped) |
| `make test-integration` | Integration tier only | Not timed separately; about 94% of the suite's cumulative test time |
| `make test-tiers-audit` | Proves the two tiers partition the full collection | 5.2 s warm (three serial collections) |
| `make test-profile` | Complete suite with the timing reporter, then a summary | Complete-suite time; reporter overhead not isolated |

Run `make test-fast` while iterating, then `make test` or `make verify` before handoff. The
fast tier never replaces the complete suite. Set the worker count with
`PYTEST_XDIST_AUTO_NUM_WORKERS=N`; the targets use `-n auto`.

`make test-profile` requires `OPEN_BRAIN_PYTEST_TIMING_DIR`, an absolute path to a fresh
directory outside the checkout:

```sh
OPEN_BRAIN_PYTEST_TIMING_DIR="$(mktemp -d)/timing" make test-profile
```

It writes `summary.json` and `nodes.json` next to the raw records. The reporter refuses a
directory that already holds timing records, so separate runs are never merged.

## Tiers

`tools/open_brain_dev/pytest_tiers.py` adds the `integration` marker to every test function
listed in `tests/tiers/integration.txt`. The list was generated from a measured two-worker
profile of the full suite, not from directory names. A function is in the integration tier
if any of its parametrizations spawned a process, called `time.sleep`, or took 0.5 s or more
(setup, call and teardown together). A test can also carry `@pytest.mark.integration`
explicitly; the tier test modules do this for every case that starts a subprocess.

Unlisted tests default to the fast tier. To refresh the list after the suite changes, profile
the complete suite and regenerate:

```sh
OPEN_BRAIN_PYTEST_TIMING_DIR="$(mktemp -d)/timing" make test-profile
uv run --frozen python -m tools.open_brain_dev.pytest_tiers generate \
  "$OPEN_BRAIN_PYTEST_TIMING_DIR/nodes.json" --output tests/tiers/integration.txt
make test-tiers-audit
```

The audit collects the suite three times (all, `-m integration`, `-m "not integration"`)
from a structured node list. It clears inherited `PYTEST_ADDOPTS` and `PYTEST_PLUGINS`,
refuses selection options in configured `addopts`, and fails when the tiers overlap, miss a
collected test, or name a stale function. Pass `--baseline FILE` with one node ID per line to
also prove that every earlier test is still collected.

## Profiling fields

`tools/open_brain_dev/pytest_timing.py` records, per test phase: wall duration, process CPU,
CPU of reaped child processes, voluntary context switches, and audited counts of process
spawns, SQLite connections and `time.sleep` calls. Fixture records hold setup-hook elapsed
time per fixture definition; a fixture that calls `request.getfixturevalue()` can be counted
together with the fixture it requested. The summary reports cumulative phase time (summed
across workers), per-worker busy time and gaps between phases, and the controller's session
wall time. Records hold node IDs and numbers only, never test output.

## Measurements, 15 October 2026

Machine: Apple M1 Pro (8 performance and 2 efficiency cores), 16 GiB memory, macOS 26.3,
Python 3.14.7, pytest 9.1.1, pytest-xdist 3.8.0. Each run used a fresh short `TMPDIR`
outside the checkout and pytest temporary retention `failed`/1. The machine was a shared
workstation: swap stayed at 17 to 19 GiB used, and other processes (an hourly backup,
a VM, file indexing) added load. Every run recorded load and free disk every 15 seconds.

Baseline: `5afee33`, which differs from `8ef1d06` only by the opt-in reporter.
Candidate: `86cb78c`. All profiled runs had the reporter enabled.

### Complete suite

| | Baseline, 2 workers | Candidate, 2 workers | Candidate, 4 workers | Candidate, 4 workers, repeat |
|---|---:|---:|---:|---:|
| Result | 7,480 passed, 5 skipped | 7,535 passed, 5 skipped | 7,535 passed, 5 skipped | 7,535 passed, 5 skipped |
| Pytest wall time | 2,330.7 s | 1,474.9 s | 897.7 s | 819.3 s |
| Collection per worker | 1.5 s | 1.4 s | 1.8 to 2.0 s | 1.5 to 1.6 s |
| Cumulative setup | 238.4 s | 147.4 s | 179.9 s | 165.8 s |
| Cumulative call | 4,364.6 s | 2,759.3 s | 3,333.3 s | 3,036.5 s |
| Cumulative teardown | 22.8 s | 25.8 s | 39.5 s | 32.8 s |
| Cumulative total | 4,625.8 s | 2,932.6 s | 3,552.7 s | 3,235.1 s |
| Worker-process CPU | 4,034.9 s | 2,385.2 s | 2,814.0 s | 2,636.0 s |
| Child-process CPU | 410.3 s | 350.8 s | 382.9 s | 367.5 s |
| Idle between phases, per worker | 2.1 to 2.4 s | 2.1 to 2.4 s | 1.1 to 1.5 s | 0.8 to 1.6 s |
| Median load (1 min) | 6.5 | 7.6 | 29.1 | 12.6 |

Each candidate run also counts 55 new tests (tier, timing and cache regression tests). On
the 7,485 tests both versions share, cumulative time fell from 4,626 s to 2,913 s (37%).
The first 4-worker run overlapped the hourly backup and other jobs (median load 29.1),
so it was repeated; both are shown. Cumulative time rises with more workers because each test
runs slower under contention, while wall time falls. Serial collection
(`pytest --collect-only`, 7,485 or 7,540 IDs) took 7.36 s on the first, cold run after
environment creation and 1.32 s warm on the candidate; that gap is bytecode caching, not a
collection change.

The five skips are the same filesystem cases in both versions
(`test_markdown_import_fs.py`: non-UTF-8 and NFC/NFD names). The single warning is the
existing fork-in-threaded-process `DeprecationWarning` from `test_writer_transaction.py`.

### Representative mix, three runs each

A fixed 407-test mix was declared before any candidate run. It contains storage-heavy
portability tests, the two largest setup fixtures, scoped stdio MCP with cross-process
contention, the Slack sharing race file, historical read snapshots and revocation, the
cross-process revocation case, bounded lock deadlines, and fork tests.

| | 2 workers, median (range) | 4 workers, median (range) | 4 vs 2 workers |
|---|---:|---:|---:|
| Baseline | 195.8 s (193.4 to 197.6) | 111.2 s (105.6 to 118.9) | 1.76x |
| Candidate | 125.5 s (118.7 to 126.3) | 69.2 s (68.0 to 72.0) | 1.81x |
| Candidate vs baseline | 36% less | 38% less | |

The 158 race, revocation, read-snapshot, lock and fork cases in the mix passed in all 12
mix runs and in all four complete runs.

### Where the time went, and what changed

The baseline suite was CPU-bound: worker processes used 4,035 s of CPU across 4,626 s of
cumulative test time, workers were busy 99% of their span, and fixed sleeps totalled only
29 s. Profiles of representative tests put 58 to 75% of their time in
`local_schema.classify_local_schema`. Every database open normalizes each `sqlite_master`
statement through `_normalized_sql`, which had a 128-entry LRU cache. A scan walks about
180 statements in the same order every time, so the cache evicted each entry before it was
reused and the hit rate was 47%. Raising the bound to 1024 (supported schemas hold 197
distinct statements) gave a 99.99% hit rate with identical results. That is the only
production change, and it was approved separately.

Three other opportunities were measured and not changed:

- Copying a prepared Brain instead of building one per test. A prepared Brain records its
  absolute root path in `.open-brain/cursors/identity.json` and keeps live WAL and lease
  files, so a copy at another path would not behave like a fresh Brain. After the cache fix,
  the largest setup fixtures still cost 0.1 to 0.8 s each, about 150 s cumulative.
- Fixed sleeps: 28 to 29 s cumulative across the suite, under 1%.
- xdist scheduling: workers sat idle less than 2.5 s per run at either worker count.

### Worker count

Four workers finished the complete candidate suite 1.80 times faster than two (819.3 s
against 1,474.9 s), and the mix agreed (1.81 times, with tight ranges). Outcomes were
identical at both counts, each worker peaked at 306 MiB resident memory or less, swap did not
grow during the runs, and free disk stayed at 4 GiB or more.

For local complete runs on a machine like this one (eight performance cores, 16 GiB),
`PYTEST_XDIST_AUTO_NUM_WORKERS=4 make test` is the measured faster choice. Acceptance
receipts keep the established two-worker configuration: it matches earlier receipts, and an
eight-worker run once crossed the storage watermark and a helper deadline (BUILD-004). The
Makefile still uses `-n auto`; no default changed.

## Limits

- One complete run per configuration; the mix supplies the repeat measurements.
- The reporter adds an audit hook and two `getrusage` calls per phase. Its own overhead was
  not isolated; baseline and candidate were both measured with it enabled.
- During the baseline run a summary-tool source file was added to the checkout. Pytest never
  imported it, but that run's input hashes are not frozen; every candidate run's are.
- These are macOS arm64 numbers on one shared workstation. CI runners have fewer cores and
  were not measured.
