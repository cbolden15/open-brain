# Save pages from Chrome

Open the **Save to Open Brain** toolbar action on an HTTPS page. Enter a reason and choose **Save page**. Select text first to keep a passage, or use Chrome’s selection menu. Reopening a saved page shows the original save and lets you add highlights, file it in a visible space, or schedule a revisit.

## Install the local candidate

The extension and native host are optional. The candidate uses Chrome for Testing with a disposable Chrome user-data directory. Consumer installation through the Chrome Web Store is pending.

1. In a contributor checkout, run `make browser-candidate`. The build writes the extension ZIP, native host, and a platform archive under `build/browser/`. The generated Homebrew formula pins that local archive’s SHA-256. Keep the native executable and its sibling `_internal` directory together in a stable, owner-controlled location.
2. Load the unpacked extension from `packages/browser-extension/extension` in the intended Chrome test profile. Its development ID is `gdeinollcipkdcfglknalgijfkhiojhn`. The manifest public key keeps that ID stable between unpacked upgrades.
3. Bind the host to the intended Brain. The following example creates a separate saved-item source. Substitute the absolute path to your chosen Brain. Full-domain write access includes all present and future spaces in its three supported tiers.

   ```sh
   HOST="$PWD/build/browser/native/open-brain-browser-host/open-brain-browser-host"
   "$HOST" setup --data-dir "$BRAIN" --extension-id gdeinollcipkdcfglknalgijfkhiojhn \
     --new-source --complete-domain --unassigned --tier personal --tier work --tier public \
     --operation lookup --operation save --operation status
   ```

4. Register the executable with the test profile and confirm the reported destination and exact manifest path. Set `PROFILE` to the absolute Chrome user-data directory under your home. Omitting this flag selects the platform’s normal Chrome registration directory.

   ```sh
   "$HOST" install --chrome-user-data-dir "$PROFILE"
   "$HOST" diagnose --chrome-user-data-dir "$PROFILE"
   ```

For a read-only installation, select specific tiers and spaces at setup and grant `lookup` and, if needed, `status`. The popup applies current source, space, tier and lifecycle rules to lookups, children and recovery.

## Highlights, filing and revisit dates

The first accepted save keeps its reason and trust classification. Adding a highlight creates a separate child. **Show saved highlights** lists the quotes and owner notes. **Highlight these passages on the page** paints unique matches. If a match is ambiguous, the saved text stays readable in the popup. Notes stay in the popup.

Use **Choose filing space** on a saved page to file it. Leave the control closed to keep a new capture unassigned. Capture and filing have separate receipts, so a refused filing can be retried after the page is saved.

Under **Revisit this page**, enter a date and time zone. **Due revisits** compares that date with the calendar in the stored zone. Due work comes first in the daily brief, followed by visible due revisits. A zone missing after restore stays available for correction. Revisit settings never start a timer or scheduled service.

## Import bookmarks

Open **Import bookmarks**. Choose a bookmark HTML file or a UTF-8 list with one HTTPS URL per line, and enter a reason for the batch. Review the rows before confirming. The host parses file content as data and never fetches bookmark URLs.

A file can contain up to 1,000 entries and occupy up to 1 MiB. Split larger lists into smaller UTF-8 files and reuse the reason. The host permits four active uploads, previews or jobs per workflow. An unconfirmed upload expires after 30 idle minutes or one hour total. Confirmed imports can resume after that time. Cancellation stops unfinished work and preserves already admitted outcomes.

## Restore, upgrade and remove

Export and restore the Brain with its ordinary CLI. A Portable 23 archive carries source identities, captures, annotations, filing history, operation receipts and revisit dates. The filing evidence lets a freshly authorized browser recover a filed folder item even when its original URL had a private fragment. Browser grants, tokens, SSH settings, registration paths and unconfirmed uploads are installation-local. Keep the workflow principal and source tuple from the owner setup output if you want to recover the original browser workflow.

After restore, use `"$HOST" sources --data-dir "$BRAIN"` to inspect the available tuples. Run setup again with `--source CONNECTION_ID RESOURCE_ID --workflow-principal UUID` in place of `--new-source`, choosing the desired permissions. Older Portable 18 saved items need their original producer source tuple and a known saved URL for legacy rebinding. Do not derive those identifiers from a page title.

On upgrade, rerun `install` with the new executable against the same profile. Before removal, check **Unresolved saves** and **Recover saved attempts** in the popup. Recovery reads receipts under the current grant and can recover opaque IDs after a profile loses its extension storage. A pending or unknown outcome calls for checking status before starting a new save.

Use `"$HOST" unregister --chrome-user-data-dir "$PROFILE"` to remove its verified registration. If the registration has been replaced by another application, removal refuses. Run `"$HOST" revoke` to withdraw a local grant. Remove the extension from the intended Chrome profile and remove the optional host through its installation method. Your Brain retains its saved pages.

## Optional remote destination

The laptop can be configured to use one owner-pinned SSH destination. Its label is shown in the popup, including when disconnected. Operations stay bound to the configured Brain and source. Page capture and recovery passed on [two arm64 Macs](../audits/2026-11-18-browser-ssh-acceptance.md). The host guide contains the dedicated key, forced-command and known-host requirements. [Native host guide](../../packages/browser-host/README.md)
