# Installed macOS Chrome candidate, November 18, 2026

On macOS 26.3 arm64, Chrome for Testing 153.0.8010.12 loaded the packaged 0.1.0 extension and launched the separately packaged 0.2.5 native host. The test unpacked the ZIP and verified the host archive against the local formula's SHA-256 before staging its executable, libexec runtime and bin symlinks in a disposable prefix. It used a temporary home, one disposable Chrome profile and synthetic Brains.

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

The release candidates were rebuilt from revision `4938ce1`. The installed run upgraded the prior Core from schema 23 to schema 28. All five Chrome scenarios passed with the rebuilt binaries. Page capture and highlight tests verified that the extension files matched the packaged ZIP. The organizer recovered seven imported bookmarks and the original filing space after a fresh restore. Keyboard tests covered capture, import, filing, highlight, a restored due date, dismissal and clearing. Chrome exposed the polite live region.

| Artifact | SHA-256 |
| --- | --- |
| Native host archive | `9c18b8f80fd74e5002ab562d9ce3f6211e78e020f8686f57154e82b3f42bd6a9` |
| Native host executable | `1db9013b19f617ee29555d4b3025cc867ab4d27409309dc4e0861532ff57c94b` |
| Extension ZIP | `11ae63465c1b2e4a5e7aeb5654efee741cccc814f92597142206312b03db4ee1` |
| Core executable | `4aae313fca539cc57bace996d28a87a3f0e4d983ce422ad48c95ac77d2f7597b` |
| Core archive | `a283e4f265ec707273a8dc31e09f8dd0eb31cd33dbc8b7a0a210209e0c45b845` |

The native audit passed. The Homebrew integration smoke test used the same audited Core, verified the owner journey and preserved the existing product. The optional embedding component was absent and its archive audit was skipped.

The Graphify CI probe built and exercised the helper on macOS arm64. Its executable SHA-256 was `85ef503811bdb419e898aa90065d42d1b2f2c9066caadcfc7f68e4721aec1ac1`. The startup measurement stopped at its disposable GitHub-hosted runner check before evicting any system caches. Cold-start timing remains pending in hosted CI.
