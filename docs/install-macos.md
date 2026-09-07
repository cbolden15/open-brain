# Current pre-alpha appliance installation on macOS

This guide documents the retained source/wheel appliance path. It requires an explicit Brain root
and daemon, so it is Secure Node precursor and regression evidence after the product split. It is
not the target Open Brain quickstart.

The target default is the one-command, five-minute path specified in
[`acceptance/five-minute-install.md`](acceptance/five-minute-install.md). That path creates its
private data directory automatically, starts no daemon or service, and does not claim
application-level encryption.

The retained appliance supports macOS 14 or newer on Apple Silicon with Python 3.14 and `uv`. You can run
the versioned source checkout or install the matching app and engine wheels. The DMG is deferred
to a later release.

No release tag or wheel has been published yet. The commands below define the v0 installation
path and work with locally built artifacts during pre-release verification.

## Option 1: versioned source

Clone the repository and select the release tag named in the release notes:

```bash
git clone https://github.com/vora-technology/open-brain.git
cd open-brain
git checkout <release-tag>
uv sync --frozen --package open-brain --no-dev
```

Choose one private Brain directory and initialize it:

```bash
export OPEN_BRAIN_ROOT="$HOME/open-brain-data"
uv run --frozen --package open-brain --no-dev open-brain init --json
```

Start the daemon in the foreground:

```bash
uv run --frozen --package open-brain --no-dev open-brain daemon
```

Keep that terminal open. Press `Control-C` to stop the daemon.

## Option 2: wheels

Place the matching app and engine wheels from one release in the same directory. Install them into
an isolated uv tool environment without contacting a package index:

```bash
uv tool install --offline --no-python-downloads --python 3.14 \
  --with ./open_brain_engine-0.1.0-py3-none-any.whl \
  ./open_brain-0.1.0-py3-none-any.whl
export PATH="$(uv tool dir --bin):$PATH"
```

Initialize the Brain and start the daemon:

```bash
export OPEN_BRAIN_ROOT="$HOME/open-brain-data"
open-brain init --json
open-brain daemon
```

## Confirm the installation

Open another terminal, set the same root, and inspect the running service:

```bash
export OPEN_BRAIN_ROOT="$HOME/open-brain-data"
open-brain --version
open-brain status --json
open-brain spaces create "Projects" --delivery=setup-projects --json
open-brain capture quick text "Review the roadmap" --delivery=capture-roadmap --json
open-brain query roadmap --json
```

The default provider is `none`, cloud access is off, and the HTTP service binds to loopback. The
Brain root contains the generated local credential and user data; keep it private.
