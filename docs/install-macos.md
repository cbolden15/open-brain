# Open Brain installation on macOS

The default product is one local Brain with automatic private-directory setup and direct SQLite
access. It does not require Docker, certificates, a daemon, a storage-root choice, or a database
service. It does not claim application-level encryption.

## Public one-command target

The frozen release command is:

```sh
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/vora-technology/open-brain/releases/latest/download/install.sh | sh
```

No release has been published yet. `OB1-W2` will publish and run that command on clean supported
hosts. The exact 300-second contract is in
[`acceptance/five-minute-install.md`](acceptance/five-minute-install.md).

The installer source is `release/open-brain/install.sh`. It selects the macOS arm64 archive,
validates its release-manifest record, verifies the archive SHA-256 digest, and installs without
sudo. The payload goes to
`$HOME/.local/lib/open-brain`; the launcher goes to `$HOME/.local/bin/open-brain`.

## Verify the base product from a checkout

Source and wheel development requires Python 3.14 and `uv`:

```sh
uv sync --frozen --package open-brain --no-dev
uv run --frozen --package open-brain --no-dev open-brain --version
uv run --frozen --package open-brain --no-dev open-brain init --json
```

The final command creates
`$HOME/Library/Application Support/open-brain/brain` with owner-only permissions. Running it again
reopens the same owner and Brain identity. It starts no daemon or listener.

Build and exercise the unpublished base-native artifact with:

```sh
make ob1w0-native
```

That target builds the dedicated base spec, audits its frozen module inventory, bootstraps a
disposable Brain twice, creates a reproducible archive and release manifest, and installs the
archive through the checksum-verifying script in a disposable home directory.

## Secure Node precursor

The retained appliance is advanced-product regression evidence. Install the explicit extra, choose
its Brain root, and use the explicit Secure Node command:

```sh
uv sync --frozen --package open-brain --extra secure-node
export OPEN_BRAIN_ROOT="$HOME/open-brain-secure-node"
uv run --frozen --package open-brain --extra secure-node open-brain-secure-node init --json
uv run --frozen --package open-brain --extra secure-node open-brain-secure-node daemon
```

Its daemon, generated credential, HTTP/UI surface, scoped MCP process, and service controls do not
belong to the default Open Brain installation.
