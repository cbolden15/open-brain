# Doctor checks

`doctor` runs one observed check and exits nonzero when that check fails. The four supported checks
below use the synthetic Brain created by [First use](first-use.md). Each runnable block is included
in the exact-artifact documentation gate.

<!-- open-brain-example:doctor-private-data-directory -->
```sh
DOCTOR_PRIVATE="$("$OPEN_BRAIN" doctor --check private-data-directory --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$DOCTOR_PRIVATE" | jq -r '.check')" = private-data-directory
test "$(printf '%s' "$DOCTOR_PRIVATE" | jq -r '.status')" = ok
```

<!-- open-brain-example:doctor-foreground-runtime -->
```sh
DOCTOR_FOREGROUND="$("$OPEN_BRAIN" doctor --check foreground-runtime --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$DOCTOR_FOREGROUND" | jq -r '.check')" = foreground-runtime
test "$(printf '%s' "$DOCTOR_FOREGROUND" | jq -r '.status')" = ok
```

<!-- open-brain-example:doctor-base-dependency-closure -->
```sh
DOCTOR_DEPENDENCIES="$("$OPEN_BRAIN" doctor --check base-dependency-closure --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$DOCTOR_DEPENDENCIES" | jq -r '.check')" = base-dependency-closure
test "$(printf '%s' "$DOCTOR_DEPENDENCIES" | jq -r '.status')" = ok
```

<!-- open-brain-example:doctor-search-index -->
```sh
DOCTOR_SEARCH="$("$OPEN_BRAIN" doctor --check search-index --data-dir "$DATA_DIR" --json)"
test "$(printf '%s' "$DOCTOR_SEARCH" | jq -r '.check')" = search-index
test "$(printf '%s' "$DOCTOR_SEARCH" | jq -r '.status')" = ok
"$OPEN_BRAIN" doctor --help | grep -q 'private-data-directory,foreground-runtime,base-dependency-closure,search-index'
```
