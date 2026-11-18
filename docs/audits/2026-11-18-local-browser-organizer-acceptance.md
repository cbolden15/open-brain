# Local Chrome bookmark import and filing, November 18, 2026

Chrome for Testing 153.0.8010.12 on macOS 26.3 arm64 exercised the unpacked extension and installed native host from candidate `889ed82`. The run used a disposable home, Chrome profile and Brain. Keyboard activation opened the actual file chooser. The harness supplied a local synthetic file to its intercepted Chrome file-selection event.

| Owner action | Observed |
| --- | --- |
| Choose a space after saving | Revoking write before filing preserved the saved page. A fresh confirmation with write restored filed the original capture. The selected space was still visible after Portable export and restore. |
| Choose an oversized file | The popup displayed the size limit before calling the native upload operation. |
| Preview HTML | Nine rows included seven ready bookmarks, one tracking-parameter duplicate and one non-HTTPS skip. The displayed URL contained literal markup. The preview created no markup elements and triggered no external request. |
| Revoke the grant after preview | Confirmation received a canonical denial. Owner search returned zero captures for the import reason. Canceling after reauthorization also left zero. |
| Restart during a confirmed import | The test relay withheld the first active step's reply after four captures. The reopened popup resumed the original batch and produced seven distinct searchable captures. All seven IDs survived a verified export and fresh restore. |

The contributor harness uses Playwright and a local Chrome for Testing executable. From the repository root:

```sh
python3.14 -m tools.open_brain_dev.browser_organizer_acceptance --chrome "$CHROME"
```

The run exercised keyboard activation of the filing, file selection, preview, cancellation, confirmation and resume controls. Screen-reader behavior and other platform targets have separate qualification records.
