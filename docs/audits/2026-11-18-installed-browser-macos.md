# Installed macOS Chrome candidate, November 18, 2026

On macOS 26.3 arm64, Chrome for Testing 153.0.8010.12 loaded the packaged 0.1.0 extension and launched the separately packaged 0.2.4 native host. The test unpacked the ZIP and verified the host archive against the local formula's SHA-256 before staging its executable, libexec runtime and bin symlinks in a disposable prefix. It used a temporary home, one disposable Chrome profile and synthetic Brains.

| Stage | Observation |
| --- | --- |
| Core installation and upgrade | A fresh Core initialized without a daemon or browser policy. The prior Homebrew Core wrote an owner capture at schema 23. The candidate Core migrated that Brain, found the original capture ID by search and verified a Portable export. |
| Install and use | The host registered only extension ID `gdeinollcipkdcfglknalgijfkhiojhn`. An actual toolbar action saved the parent. A test relay dropped one accepted highlight reply before Chrome received it. |
| Replace and remove | Re-registering the same host version at a second Cellar path updated the owned manifest. Diagnostics recognized it. Removal was repeatable and preserved a sibling manifest. Both install and unregister refused an unrelated manifest and a byte-identical file without an ownership receipt. |
| Reinstall with empty extension storage | Chrome uninstalled and reinstalled the unpacked artifact under the same extension ID. Its opaque pending ID had been erased. A fresh grant for the original source and workflow recovered two canonical receipts through the popup. The old token was refused. Replaying the highlight returned its original receipt and left exactly one highlight. |
| Process lifetime | The active host showed zero listening TCP sockets during the check. All native processes exited. Core search still found the highlight after removal of the optional package and extension. |

The test uses the generated formula layout without modifying Homebrew's live prefix. The optional host has no previously released version for a cross-version upgrade. The extension was loaded from its upload ZIP; signed Web Store update delivery requires a separate test.

With Playwright installed in the Python environment, point the following variables to Chrome for Testing, an installed older Core, the candidate Core, and the local host archive next to its generated formula:

```sh
python3.14 -m tools.open_brain_dev.browser_installed_acceptance \
  --chrome "$CHROME" --prior-core "$PRIOR_CORE" --brain-cli "$CANDIDATE_CORE" \
  --host-archive "$HOST_ARCHIVE" --zip build/browser/open-brain-bookmarks.zip
```

The first full installed run upgraded Core to schema 27. The native host archive SHA-256 was `c4263584fc2eac5968d242d96461b1d9897fc11e326eb08653dc9f3831c5f19d`. The extension ZIP SHA-256 was `42d7434ac465cbdd5f249a8387e52301856db30ca249847ddfab84d25b91a3fe`. Other platform and remote-host qualifications are separate.
