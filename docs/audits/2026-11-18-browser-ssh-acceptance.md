# Chrome page capture across two Macs

On November 18, 2026, Chrome for Testing 153.0.8010.12 on an arm64 macOS 26.3 laptop saved a synthetic HTTPS page to a separate arm64 macOS 15.8.1 host over SSH. Both machines used disposable Brains. The extension ZIP (0.1.0), native host (0.2.5), and Core archive matched the [installed macOS candidate](2026-11-18-installed-browser-macos.md).

The laptop used the shipping SSH client configuration. The server's Ed25519 key matched the existing trusted host key. The temporary client identity was restricted to the laptop's source address, an expiry, and a forced command. Requesting an unrelated command with the key was refused.

| Check | Observation |
| --- | --- |
| Destination unavailable | The server-side test handler refused a connection. The popup named the configured destination. Both Brains were empty. |
| Reply interrupted | Chrome saved from the toolbar. A server-side test wrapper forwarded the real handler's ready frame, waited for its accepted reply, then closed the SSH channel before delivering the reply. The laptop reported an unknown outcome. |
| Reconnect and replay | After Chrome restarted, it recovered the original capture ID through operation status. Replaying the original save with the same operation ID returned that same receipt. The final capture counts were one on the server and zero on the laptop, checked with search and capture-file counts. |
| Reduced permissions | Rebinding the same workflow to public-tier lookup and status made the saved personal page indistinguishable from an absent page. A new save was refused. |
| Cleanup | The temporary key was removed. The SSH authorization file matched its original SHA-256, and a fresh login with the removed key failed. The test grant was revoked, and both machines' temporary fixtures were removed. |

This qualifies page capture and recovery on this Mac-to-Mac configuration. Linux installation, signed Chrome Web Store delivery, and live screen-reader speech have separate qualification records.
