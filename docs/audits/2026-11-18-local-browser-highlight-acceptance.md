# Local Chrome highlight acceptance, November 18, 2026

Chrome for Testing 153.0.8010.12 on macOS 26.3 arm64 passed the highlight and page scenarios against a frozen offline installation of the contributor candidate at `889ed82`. Each run used a disposable macOS home, Chrome profile and local Brain. Python 3.14.7 and Playwright 1.58.0 drove the real action popup through the debugging pipe.

| Check | Observed |
| --- | --- |
| Interrupted child after parent acceptance | The first annotation never reached the native host. A fresh confirmation added the child to the existing parent. |
| Accepted child with dropped reply | Two unresolved IDs survived restart while status was offline. On reconnect, the original operation and annotation receipt matched. The never-delivered attempt remained unresolved. |
| Intentional repeat | Two distinct quotes and an intentional repeat produced three different annotation IDs on one parent. Owner CLI search found that parent by a note kept outside the page. |
| Gesture-driven painting | Three visible DOM ranges crossed adjacent text nodes. The CSS highlight color matched the installed stylesheet. Context selected the intended occurrence of a repeated phrase. Decomposed Unicode retained its original DOM range. |
| Ambiguity after editing | Cloning the matching context removed all existing paint. The next gesture located two ranges and left the ambiguous quote readable in the popup. |
| Portable round trip | A verified export restored into a separate Brain. Its native host rejected the old token. A fresh grant and Chrome profile listed the original three annotation IDs and painted all three passages. Search still found the parent by note. No restore-time save or annotation mutation occurred. |

The separate page regression also passed with three distinct searchable captures, zero stale dispatches, the original page after a post-dispatch navigation, and one hidden-source miss. Each scenario prints its counts and the SHA-256 of every file in the tested extension snapshot.

With a contributor installation, Playwright, and `CHROME` set to the Chrome for Testing executable, run:

```sh
python3.14 tools/open_brain_dev/browser_acceptance.py highlights --chrome "$CHROME"
python3.14 tools/open_brain_dev/browser_acceptance.py local --chrome "$CHROME"
```

These checks cover local page capture, highlights and Portable restore. Remote deployment and distribution have separate qualification gates.
