# Local Chrome page capture, November 18, 2026

The unpacked candidate at `770354136e6465ec6ddf2f2c25070d7664de432b` passed the local page-capture round trip on macOS 26.3 arm64, Chrome for Testing 153.0.8010.12, Python 3.14.7 and Playwright 1.58.0. The browser used its full headless executable, a disposable home and profile, and an intercepted synthetic HTTPS origin. Chrome opened the real action popup through its debugging pipe. The harness clicked the popup's save button and checked a separately installed `open-brain search` command against the disposable Brain.

| Check | Observed |
| --- | --- |
| First save through the registered executable | One canonical capture found by its reason. Reopening the toolbar showed the saved page. |
| Accepted reply dropped by a test relay | Browser restart issued status with the original operation ID. The receipt matched the capture returned by search. One mutation reached the host. |
| Navigation before dispatch | Worker paused before its final document check. After navigation, popup teardown and resume, its pending entry cleared. Zero save frames reached the host and search found zero captures for that reason. |
| Navigation after dispatch | The relay held the original request while Chrome navigated and the popup closed. The accepted receipt matched the original URL, title, selection and independent search capture ID. The replacement page remained absent. |
| Visibility and markup | A second source missed the first capture while owner search still found it. Markup-like title and selection displayed literally, with zero markup descendants or intercepted external requests. |

Three different reasons each resolved to one distinct capture. The unpacked extension ID was `gdeinollcipkdcfglknalgijfkhiojhn`. The Git tree IDs were `61c7b019ed72eca115d315da75fd43dc68f6e166` for the extension and `e7dc468be5a7f582372c5f63313fa0d5fc87ae4a` for the browser-host package. A successful run prints per-file extension SHA-256 digests and counts.

From the repository root, use a Python environment with Playwright installed and the contributor environment with the optional native host installed. Set `CHROME` to the absolute Chrome for Testing executable path.

```sh
python3.14 tools/open_brain_dev/browser_acceptance.py local --chrome "$CHROME"
```

This record covers the local page workflow. Annotation replay, bookmark import preview, Linux, remote transport and release packaging require their own acceptance runs.
