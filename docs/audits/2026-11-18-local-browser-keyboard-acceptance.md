# Local Chrome keyboard and revisit acceptance

Chrome for Testing 153.0.8010.12 on macOS ARM64 opened the unpacked extension in a disposable profile. The user path operated the popup with Tab, Enter and keyboard text input. Chrome exposed the actual file picker. The harness supplied a synthetic file to that picker and read the resulting Brain state.

| Action | Result |
| --- | --- |
| Save the selected page | Keyboard submission saved the page and moved focus to the highlight action. |
| Save and restore a passage | The selection became a durable highlight and Chrome located it on the page. |
| File the page | The allowed space selector and submit action filed it in the chosen space. |
| Preview and confirm a bookmark file | The visible confirmation imported one searchable bookmark. |
| Set a revisit | The native date control accepted January 2, 2024 by keyboard. The due view showed the saved page with a link. |
| Restore and dismiss | The fresh profile and rebound grant showed the same due date. Dismiss removed the item from the due list, and clear removed its saved date. |

Chrome’s accessibility tree exposed a polite live status region and the named clear action. This run covers programmatic screen-reader semantics. A listening test with an OS screen reader remains pending.

Run from the repository root with Python 3.14, Playwright, the installed contributor host, and a Chrome for Testing executable.

```sh
python3.14 -m tools.open_brain_dev.browser_keyboard_acceptance --chrome "$CHROME"
```
