# Install Open Brain

Status: target instructions; the public release and tap are not published yet

## Requirement

Install Homebrew first and make sure this succeeds:

```sh
brew --version
```

Open Brain supports macOS on Apple Silicon and Linux on x86_64. There is no curl installer, Docker
image, Python runtime requirement, or manual database setup for end users.

## Install

After the first release is published:

```sh
brew install cbolden15/tap/open-brain
```

Homebrew downloads the platform archive from an immutable GitHub Release URL and verifies the
SHA-256 stored in the formula.

## First use

```sh
open-brain capture "A note to remember"
open-brain import /absolute/path/to/markdown --yes
open-brain search "remember"
open-brain export "$PWD/brain-export" --verify
open-brain status --json
```

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

Set up the managed vault and stage the packaged desktop plugin:

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

Use the Obsidian command palette for capture, search, graph refresh, source navigation, suggestion
review, conflict resolution, and semantic exclusions. **Open Brain: Configure semantic provider**
supports OpenAI, Anthropic, and Gemini API keys. Each setup requires explicit consent for eligible
managed notes. Store the key for the current Obsidian session, or use macOS Keychain or Linux Secret
Service when available. Raw keys are never saved in plugin settings. Claude subscription access is
currently unavailable because an unprivileged client-isolation path has not been proven.

Automatic graph refresh batches Markdown edits. **Open Brain: Pause or resume automatic inference**
persists the pause preference; **Open Brain: Refresh graph now** remains available while paused.
Closing or disabling the plugin stops its foreground Open Brain child process.

To remove the staged plugin files without deleting the managed vault or its settings:

```sh
open-brain obsidian-plugin remove
```

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

End users should use Homebrew. Contributors can follow [`../CONTRIBUTING.md`](../CONTRIBUTING.md) and
run `make verify`, `make native`, and `make homebrew-smoke` from a checkout.
