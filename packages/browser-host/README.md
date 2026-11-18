# Open Brain browser host

Install the optional `open-brain-browser-host` package in a trusted Python 3.14
environment. To try it from a contributor checkout, use
`uv run --frozen open-brain-browser-host --help`. The core installation runs
independently of this package.

1. Configure a grant for one Chrome extension ID and one local Brain. For a
   disposable Brain, specify an absolute `--data-dir`, `--initialize-brain`
   and `--new-source`. For an existing source, run `sources --data-dir PATH`
   and pass its IDs to `setup --source CONNECTION_ID RESOURCE_ID`. Use one
   `--operation` and one `--tier` per permission, and specify `--space ID`,
   `--unassigned` or both. To enable creation, explicitly authorize
   `--complete-domain` with all three tiers and `--unassigned`. This covers
   the selected source across all current and future spaces.
2. Run `open-brain-browser-host install` to register
   `com.open_brain.saved_items` for the configured extension in Google Chrome.
   To register under a separate Chrome user data directory inside your home
   directory, pass its absolute path with `--chrome-user-data-dir`.
3. Run `open-brain-browser-host revoke` to withdraw the configured grant. After
   a restore, run `setup` again with the original source. Specify the
   original `--workflow-principal` if you want the new grant to reconcile
   earlier operation receipts. The setup command reports this principal.

The host stores its private policy in `~/.open-brain-browser/policy.json`. The
policy binds the selected executable's requests to an extension origin, one
local Brain and a grant with current operations, source, tiers and spaces.
Chrome uses the registration to restrict which extensions can connect. The
origin argument is a local configuration check. Gesture validation belongs
in the Chrome extension, and the operating-system account controls access to
the local executable and policy.

## Native message

Chrome starts one foreground process for one request with
`chrome.runtime.sendNativeMessage`. Each direction carries a native-endian
unsigned 32-bit byte count followed by that many bytes of UTF-8 JSON. The
maximum request is 256 KiB and the foreground deadline is ten seconds.

All requests have `version: "browser.v1"`, `operation` and `args`.
Include `operation_id`, a lowercase UUIDv4, on save and status.

| Operation | Exact arguments | Result |
| --- | --- | --- |
| `binding.describe` | Empty object | `{"binding_id":"…","destination":"This computer"}` after validating the current installation grant. |
| `page.lookup` | `url` | `{"state":"absent"}` or `{"state":"saved","capture_id":"…","space_id":null}`. A filed save has its space ID in place of null. |
| `page.save` | `url`, `reason`, optional `title` and `passage` | State is `accepted`, `pending` or `refused`. An accepted result includes `capture_id`. |
| `operation.status` | Empty object | Same result shape as save, filtered by current scope. |

The extension pins the ID returned by `binding.describe` in the top-level
`binding_id` field on lookup, save and status. It is a 64-character lowercase
hexadecimal string. A changed ID returns `binding_changed` before the operation
runs. The ID represents the canonical Brain, saved-item source and workflow,
and survives explicit rebinding to that workflow on a restored Brain. The
current grant still controls each operation. Existing v1 calls may omit the ID.

URLs must be HTTPS and at most 8,192 characters. Reasons are required and
limited to 280 characters. Titles and passages are limited to 300 and 4,096
characters respectively. Saved-item intake stores redacted passage text
and drops URL fragments from the stored source URL. A saved page may retain
its fragment in the URL identity digest so a lookup resolves the same record.

Each response has `version: "browser.v1"` and either a `result` object or an
`error` object with a bounded `code`. After an interrupted save, preserve
the operation ID and the same arguments for retry. A pending response
indicates durable admission with reconciliation still outstanding.

[Chrome native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)

## Remote destination (qualification pending)

The local setup above is the default. The remote transport has a synthetic
two-installation test. Real Chrome over SSH still requires a separately
authorized disposable laptop and canonical host. No remote platform has
qualified for release.

The canonical host runs the same owner setup and holds the grant. Its setup
output supplies the canonical `binding_id`. The restricted SSH credential
must be dedicated to one installation and forced on the server to execute
only the absolute installed host executable with argument `remote-handler`,
with forwarding, PTY and user startup commands disabled. The owner must
verify the canonical host's Ed25519 public key out of band.

On the laptop, `setup-remote --help` lists the five owner-supplied fields.
They identify the extension, fixed SSH user and hostname, complete Ed25519
public host key, absolute path to the dedicated identity file, and canonical
binding ID. Setup writes the private installation policy without connecting.
Switching an existing local installation to remote revokes its local grant.
Use the ordinary `install` action for Chrome registration. Withdraw a remote
grant with `revoke` on the canonical host.

The popup can read the configured destination while offline. A remote request
must carry its pinned binding ID. Before sending page data, the helper checks
the pinned SSH host key and a ready frame naming the expected canonical
binding and extension. Each operation checks the canonical grant again.
Remote frames start with ASCII `OBR1` and a four-byte big-endian body length
(up to 256 KiB). Each terminal reply identifies the connection's binding,
request nonce and digest. A failed connection leaves the page on the laptop.
A missing acknowledgement after submission leaves the outcome unknown. Check
status and retry with the original operation ID and destination.

Run the synthetic outage, dropped-acknowledgement, replay and scope scenario
with `uv run --frozen pytest -q packages/browser-host/tests/test_remote.py`.
It uses two disposable Brain roots and a process-pipe adapter.

For the actual Chrome popup on the same two-root fixture, run
`python3.14 tools/open_brain_dev/browser_remote_acceptance.py --chrome "$CHROME"`
from the repository root with Playwright installed. It verifies the configured
destination while disconnected, a lost reply, restart, and the canonical
capture count. Two-machine SSH qualification is pending.
