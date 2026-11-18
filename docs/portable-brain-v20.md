# Portable Brain 20

Portable 20 pairs local schema 25 with runtime session 16. It adds saved-item admission evidence and browser operation receipts to the [Portable 19](portable-brain-v19.md) families.

The required file `history/saved-item-admission/admissions-v1.json` records which producer won a saved-item admission and the digest of its accepted request. Browser operations carry their stable workflow principal, source identity, operation ID, request digest, state and delivery reference. These fields support reconciliation when the connection drops after the Brain has accepted a save.

After restore, owner setup must issue a new local grant before a browser can look up an operation. The original workflow principal and source still identify its receipt. Access to that receipt depends on the current grant and visibility of the saved page. Local endpoint configuration, browser grant secrets and the installation generation stay outside the archive.
