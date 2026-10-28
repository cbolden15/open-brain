# Contributing

Open Brain is pre-production. Discuss substantial behavior changes before implementation so capture,
search, storage, and Portable export remain coherent.

## Try the source checkout

For the Python CLI trial, install Git and
[uv](https://docs.astral.sh/uv/getting-started/installation/). The workspace requires Python 3.14;
the command below lets uv select and download it if needed. The full native/contributor toolchain
belongs to the [development checks](#development-checks), not this capture/search trial.

From the repository root, run these four commands in the same shell to install the environment,
save a synthetic note in a disposable Brain, and retrieve it:

```sh
uv sync --frozen --python 3.14 --group dev --group native-build
export DEMO_BRAIN="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/open-brain-demo.XXXXXX")" && pwd -P)/brain"
uv run --frozen open-brain capture 'Synthetic project Copper Orchard meets on Tuesday.' --data-dir "$DEMO_BRAIN" --json
uv run --frozen open-brain search 'Copper Orchard' --data-dir "$DEMO_BRAIN" --json
```

Search should return the sample. These commands leave your persistent personal Brain alone; the
first capture creates the disposable Brain automatically. A checkout does not put `open-brain` on
`PATH`, so keep the `uv run --frozen` prefix. Keep `DEMO_BRAIN` set for the optional interactive
source setup described in the [first-use guide](docs/first-use.md). That setup command is current-source
functionality and is not in the published `v0.1.0` Homebrew release.

## Development checks

Supported contributor hosts are macOS arm64 and Linux x86_64. Full checks require Git, GNU Make,
uv, Node.js 24.15+ or 26+, npm, stable Rust, and [Homebrew](https://brew.sh/). On macOS, install the
Xcode Command Line Tools (`xcode-select --install`). On Linux, install Homebrew's build prerequisites
(a C/C++ toolchain, curl, file, Git, and Make) using your distribution's package manager. Put Homebrew
on `PATH` using the `brew shellenv` command printed by its installer. CI uses the latest Node.js 24.
Rust is part of the native component toolchain, not a dependency of the Python capture/search trial.

Before submitting a change, run:

```sh
make contributor-check
```

`make contributor-check` runs `make verify` and `make native-integration-smoke`. Expect Ruff's
`All checks passed!`, MyPy's `Success: no issues found`, a passing pytest summary (some filesystem
checks can skip on unsupported hosts), passing Obsidian plugin type/build/test checks, successful
wheel/source builds, native smoke JSON, and
`existing_product: preserved` or `existing_product: absent`. A nonzero exit means the check failed.
CI runs the same checks, with the Python suite split across parallel shards (`make test-shard`).
No credentials or private access are required.

While iterating on Python changes, `make test-fast` runs the fast test tier (about two minutes
with two workers) and `make test-integration` runs the rest. Neither replaces `make verify`.
Tier rules, profiling, and measured budgets are in
[the test performance guide](docs/engineering/test-performance.md).

The native integration check builds the base and Graphify executables, audits their dependency
inventories, stages the compiled plugin assets, runs the local command-line product journey, and
writes a component digest manifest. It installs a test-only, keg-only `open-brain-smoke`
formula in the temporary `open-brain-local/smoke` tap and runs its unlinked binary by absolute path.
It checks that an existing `open-brain` installation's prefix, version, link, and binary digest stay
unchanged. If no product is installed, the check leaves it absent. Each normal exit and INT/TERM interruption
cleans up smoke-owned Homebrew state; the next run recovers marked tap/formula residue after a forced
termination. Do not run concurrent Homebrew smoke checks against the same Homebrew installation.

Common failures:

- A Python version rejection or `SyntaxError: multiple exception types must be parenthesized`:
  the source tree requires Python 3.14. Run
  `uv sync --frozen --python 3.14 --group dev --group native-build`, then check
  `uv run --frozen --python 3.14 python --version`. Do not use an older system interpreter
  directly or override `Requires-Python`. See [source installation](docs/install.md#build-from-source).
- `uv: command not found`, `make: command not found`, or `Homebrew is required for contributor-check`:
  install the missing prerequisite and open a shell with it on `PATH`.
- `unsupported native build platform`: use macOS arm64 or Linux x86_64.
- `reserved smoke tap is not smoke-owned; refusing cleanup` or `reserved smoke tap has another
  installed formula; refusing cleanup`: inspect `brew --repository open-brain-local/smoke` and
  `brew list --formula --full-name`. Move your own unrelated tap work to another name before retrying.
- `existing Open Brain installation changed during smoke`: check for another Homebrew operation or
  manual change during the run; the smoke intentionally fails if preservation cannot be verified.

Private release auditing is **not required for normal contributions** and is excluded from
`make contributor-check` and CI. The owner runs `make audit` and `make audit-history` separately with
an uncommitted `PRIVATE_DENYLIST`; contributors do not need that file.

The contributor check is command-line integration coverage. It does not claim that Obsidian opened,
the plugin activated, a real provider accepted a request, source navigation worked in the GUI, or the
five-minute journey met its elapsed-time target. Record those observations separately against exact
release-candidate digests.

Contributions must use synthetic fixtures. Never include private notes, captures, transcripts, credentials, hostnames, infrastructure addresses, logs, databases, or generated private configuration.

By intentionally submitting a contribution for inclusion, you agree that it is provided under the Apache License, Version 2.0, unless you explicitly state otherwise.
