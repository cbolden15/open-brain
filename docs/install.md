# Install Open Brain

The published Homebrew release is `v0.1.0`. The current source tree has additional unreleased
features; the draft `v0.2.0` release is not published. Check installed `--help` before using a
candidate-only command. Candidates with `catalog` also expose the [feature/version matrix](core-v01-features.md)
coordinates through that command.

## Requirement

Install [Homebrew](https://brew.sh/) first and make sure this succeeds:

```sh
brew --version
```

The core CLI supports macOS on Apple Silicon and Linux on x86_64. There is no curl installer, Docker
image, Python runtime requirement, or manual database setup for end users.

## Install

Install Open Brain:

```sh
brew install cbolden15/tap/open-brain
```

The [published formula](https://github.com/cbolden15/homebrew-tap/blob/main/Formula/open-brain.rb)
downloads the platform's base and Graphify archives from immutable GitHub
Release URLs and verifies their SHA-256 values. Its version-1 component manifest describes those
resources; the formula installs a single base executable. It has no embedding resource and no
`setup`, `agent setup`, or `catalog` command.

Newer candidate packaging uses a one-folder base and can include a separate embedding resource,
with a version-2 component manifest. The resource contains a pinned local model and a native helper.
It does nothing until explicit enablement. See [offline semantic search](semantic-search.md) only
when your executable and installed resource support it. Searching never downloads a model.

## First use

Use the [first-use guide](first-use.md) to capture and retrieve a note with either the released CLI
or a source checkout. Its optional guided `setup` flow is current-source functionality. The
separately labeled historical executable fixture is retained for its original artifact, not as
the current beginner path. [Doctor examples](doctor.md) also belong to that historical fixture.

The first stateful command creates one owner and one Brain automatically. Data lives at:

| Host | Brain root |
|---|---|
| macOS | `$HOME/Library/Application Support/open-brain/brain` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` |

Directories and private files use owner-only permissions where the host supports POSIX modes. Open
Brain does not start a daemon or service.

The import command recursively reads lowercase `.md` files and does not change the source tree.
The first import of a directory requires confirmation; `--yes` is the explicit non-interactive
form. Imported records are unverified, and immutable prior revisions remain in full export after a
source file changes or disappears. Use `--allow-large-vault` only after the default aggregate scan
limits refuse a directory. It never bypasses the one-file safety limit.

## Use the managed vault in Obsidian

Obsidian is optional. First confirm that your executable offers `workspace` and `obsidian-plugin`
and that matching packaged plugin assets are installed. A source checkout alone does not provide a
native installed resource set. Set up the managed vault and stage the plugin:

```sh
open-brain workspace setup
open-brain obsidian-plugin install
open-brain obsidian-plugin status --json
```

Open the `Open Brain Vault` directory as a vault in Obsidian. It is next to the `brain` directory
listed above. In Obsidian, open **Settings → Community plugins**, enable community plugins if
needed, and enable **Open Brain**. The installer stages only Open Brain's owned plugin files. It
does not enable the plugin, edit Obsidian's enabled-plugin list, or replace other plugins and
settings.

The candidate also exposes inbox routing, draft creation, full inspection and token-bound
publication, plus paged search and complete current-record reads in the existing plugin.
Use the matching managed vault. Packaged assets and CLI staging do not prove GUI activation;
exact-candidate Obsidian GUI acceptance remains open.

Use the Obsidian command palette for capture, search, graph refresh, source navigation, suggestion
review, conflict resolution, and semantic exclusions. **Open Brain: Configure semantic provider**
supports OpenAI, Anthropic, and Gemini API keys. Each setup requires explicit consent for eligible
managed notes. Review note and folder exclusions before setup if the default eligible-note scope is
too broad. Store the key for the current Obsidian session, or use macOS Keychain or Linux Secret
Service when available. Raw keys are never saved in plugin settings, vault files, command arguments,
logs, exports, or provider results. Open Brain uses no ambient provider credential and never falls
back to another provider. Removing access revokes consent and deletes the selected saved or session
key. It cannot retract content already sent to the provider.

Semantic refresh is intentional network egress to the selected provider. Ordinary local capture,
import, search, workspace, structural graph, export, status, and doctor operations remain offline,
including [offline semantic search](semantic-search.md), which runs a local model and is not egress.
Claude subscription access is unavailable with `subscription_isolation_unproven` because an
unprivileged, tool-free client path has not been proven. Do not run Open Brain with root, capabilities,
namespaces, alternate fixture ownership, or broader host access to bypass that block.

Automatic graph refresh batches Markdown edits. **Open Brain: Pause or resume automatic inference**
persists the pause preference; **Open Brain: Refresh graph now** remains available while paused.
Structural links and inferred suggestions appear in the plugin-owned `Open Brain Graph.canvas`.
Inferred edges include review evidence but do not change Markdown. **Open Brain: Review graph
suggestions** previews a current suggestion before an explicit acceptance writes one permanent link.
Generated Canvas and graph cache data are rebuildable views and are not exported as Brain content.

The plugin uses `open-brain-client` protocol version 1 over bounded inherited stdio. It never opens
the SQLite database directly. Closing or disabling the plugin stops its foreground Open Brain child
process; Obsidian itself may remain open.

To remove the staged plugin files without deleting the managed vault or its settings:

```sh
open-brain obsidian-plugin remove
```

## Interrupted legacy workspace writes

A matching runtime that reports `workspace_recovery_required` provides owner inspection and explicit
abandonment through `workspace recover`. Start with its read-only preview and follow the
[managed workspace recovery guide](managed-workspace-recovery.md). Abandonment requires other
clients to stop and affects only the selected pending write; it does not rewrite note bytes.

## Privacy

Open Brain relies on the operating-system user account, disk encryption, backups, and physical
security. It does not provide application-level encryption, compartments, certified purge, or
multi-user service operation. Those requirements need a different product with a separate install
and security boundary.

## Uninstall

```sh
brew uninstall open-brain
```

Homebrew removes the executable. The Brain data directory survives by default. Removing user data is
a separate explicit action; do not delete it unless a verified export or another retained copy exists.

## Build from source

Source packages require Python 3.14. The Homebrew executables bundle their runtime, so end users
do not need to install Python separately. Python 3.12 and 3.13 cannot run the current source tree.

From the repository root, run these four commands in the same shell. Let uv select the supported
interpreter, then capture and retrieve a note in a separate disposable Brain:

```sh
uv sync --frozen --python 3.14 --group dev --group native-build
export DEMO_BRAIN="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/open-brain-demo.XXXXXX")" && pwd -P)/brain"
uv run --frozen open-brain capture 'Synthetic project Copper Orchard meets on Tuesday.' --data-dir "$DEMO_BRAIN" --json
uv run --frozen open-brain search 'Copper Orchard' --data-dir "$DEMO_BRAIN" --json
```

A source checkout does not put `open-brain` on your `PATH`. Run it through uv from the repository
root. Keep `DEMO_BRAIN` set for the optional unreleased interactive setup trial in the
[first-use guide](first-use.md). Omitting `--data-dir` would use your persistent personal Brain,
which a newer source runtime may upgrade; use that path only deliberately with older sessions stopped.

If an older interpreter reports `SyntaxError: multiple exception types must be parenthesized`,
check the interpreter used by that command. Python 3.14 allows `except ValueError, OSError:` and
catches either type without rebinding their names, as specified by
[PEP 758](https://peps.python.org/pep-0758/). Use the supported runtime instead of rewriting the
handlers or overriding the package's Python requirement.

### Agent setup and sources

Claude Code and Codex are configured through the [headless CLI](agent-setup.md). Source connections
and collection exist in optional source packages, but Core v0.1 does not certify their public
onboarding, continued source updates or services. See the
[source guide](integrations/core-sources.md). The earlier Tauri desktop companion is archived under
`archive/open-brain-desktop` and is not built or released.

### Core contributor checks

End users should use Homebrew. Contributors can follow [`../CONTRIBUTING.md`](../CONTRIBUTING.md) and
run `make contributor-check` from a checkout. The command-line check does not replace the separate
real-provider and Obsidian GUI timing record in the five-minute acceptance test.
