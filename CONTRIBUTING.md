# Contributing

Open Brain is pre-production. Discuss substantial behavior changes before implementation so capture,
search, storage, and Portable export remain coherent.

## Development checks

Supported contributor hosts are macOS arm64 and Linux x86_64. Install Git, GNU Make,
[uv](https://docs.astral.sh/uv/getting-started/installation/), and
[Homebrew](https://brew.sh/). On macOS, install the Xcode Command Line Tools (`xcode-select --install`).
On Linux, install Homebrew's build prerequisites (a C/C++ toolchain, curl, file, Git, and Make)
using your distribution's package manager. Put Homebrew on `PATH` using the `brew shellenv`
command printed by its installer. The workspace uses Python 3.14; uv downloads it if needed.

From the repository root in a fresh clone:

```sh
uv sync --frozen --group dev --group native-build
make contributor-check
```

`make contributor-check` runs `make verify` followed by `make homebrew-smoke`. Expect Ruff's
`All checks passed!`, MyPy's `Success: no issues found`, a passing pytest summary (some filesystem
checks can skip on unsupported hosts), successful wheel/source builds, native smoke JSON, and
`existing_product: preserved` or `existing_product: absent`. A nonzero exit means the check failed.
Both CI jobs run this same target. No credentials or private access are required.

The Homebrew check builds one native executable, audits its dependency inventory, runs the local
product journey, and writes a digest manifest. It installs a test-only, keg-only `open-brain-smoke`
formula in the temporary `open-brain-local/smoke` tap and runs its unlinked binary by absolute path.
It checks that an existing `open-brain` installation's prefix, version, link, and binary digest stay
unchanged. If no product is installed, the check leaves it absent. Each normal or interrupted exit
cleans up smoke-owned Homebrew state; the next run recovers marked tap/formula residue after a forced
termination. Do not run concurrent Homebrew smoke checks against the same Homebrew installation.

Common failures:

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

Contributions must use synthetic fixtures. Never include private notes, captures, transcripts, credentials, hostnames, infrastructure addresses, logs, databases, or generated private configuration.

By intentionally submitting a contribution for inclusion, you agree that it is provided under the Apache License, Version 2.0, unless you explicitly state otherwise.
