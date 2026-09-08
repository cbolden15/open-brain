# Contributing

Open Brain is pre-production. Discuss substantial behavior changes before implementation so capture,
search, storage, and Portable export remain coherent.

## Development checks

Homebrew is required for the installed-product smoke test.

```sh
uv sync --frozen --group dev --group native-build
make verify
make native
make homebrew-smoke
```

`make homebrew-smoke` creates a temporary local tap, installs the native artifact, runs the product
journey, and removes the formula and tap.

Contributions must use synthetic fixtures. Never include private notes, captures, transcripts, credentials, hostnames, infrastructure addresses, logs, databases, or generated private configuration.

By intentionally submitting a contribution for inclusion, you agree that it is provided under the Apache License, Version 2.0, unless you explicitly state otherwise.
