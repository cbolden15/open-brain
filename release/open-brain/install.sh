#!/bin/sh

set -eu
umask 077

repository="vora-technology/open-brain"
manifest_name="open-brain-release-manifest-v1.txt"

fail() {
    printf '%s\n' 'Open Brain installation failed.' >&2
    exit 1
}

case "${HOME:-}" in
    /*) ;;
    *) fail ;;
esac

case "$(uname -s):$(uname -m)" in
    Darwin:arm64) platform="macos-arm64" ;;
    Linux:x86_64|Linux:amd64) platform="linux-x86_64" ;;
    *) fail ;;
esac

if [ -n "${OPEN_BRAIN_RELEASE_BASE_URL:-}" ]; then
    case "$OPEN_BRAIN_RELEASE_BASE_URL" in
        https://*) release_base=${OPEN_BRAIN_RELEASE_BASE_URL%/} ;;
        *) fail ;;
    esac
elif [ -n "${OPEN_BRAIN_ACCEPTANCE_VERSION:-}" ]; then
    case "$OPEN_BRAIN_ACCEPTANCE_VERSION" in
        *[!0-9A-Za-z._-]*|'') fail ;;
        *)
            release_base="https://github.com/$repository/releases/download/v$OPEN_BRAIN_ACCEPTANCE_VERSION"
            ;;
    esac
else
    release_base="https://github.com/$repository/releases/latest/download"
fi

temporary=$(mktemp -d "${TMPDIR:-/tmp}/open-brain-install.XXXXXX") || fail
manifest=$temporary/$manifest_name
archive=$temporary/open-brain.tar.gz
members=$temporary/archive-members.txt
links=$temporary/archive-links.txt
stage=$HOME/.local/lib/.open-brain-install.$$
payload=$HOME/.local/lib/open-brain
launcher=$HOME/.local/bin/open-brain
next_launcher=$HOME/.local/bin/.open-brain.$$
payload_activated=false

cleanup() {
    rm -rf "$temporary" "$stage"
    rm -f "$next_launcher"
    if [ "$payload_activated" = true ]; then
        rm -rf "$payload"
    fi
}
trap cleanup EXIT HUP INT TERM

curl --proto '=https' --tlsv1.2 -LsSf "$release_base/$manifest_name" -o "$manifest" || fail
[ "$(wc -c < "$manifest" | tr -d ' ')" -le 65536 ] || fail
[ "$(sed -n '1p' "$manifest")" = "open-brain-release-manifest-v1" ] || fail
awk '
    NR == 1 && $0 == "open-brain-release-manifest-v1" { next }
    $1 == "version" && NF == 2 { next }
    $1 == "artifact" && NF == 4 { next }
    { exit 1 }
    END { if (NR < 3) exit 1 }
' "$manifest" || fail

version=$(awk '$1 == "version" && NF == 2 { count += 1; value = $2 } END { if (count == 1) print value }' "$manifest")
record=$(awk -v selected="$platform" '$1 == "artifact" && $2 == selected && NF == 4 { count += 1; value = $3 " " $4 } END { if (count == 1) print value }' "$manifest")
[ -n "$version" ] && [ -n "$record" ] || fail
case "$version" in
    *[!0-9A-Za-z._-]*|'') fail ;;
esac
if [ -n "${OPEN_BRAIN_ACCEPTANCE_VERSION:-}" ]; then
    [ "$version" = "$OPEN_BRAIN_ACCEPTANCE_VERSION" ] || fail
fi

expected_sha256=${record%% *}
artifact_name=${record#* }
[ "$artifact_name" = "open-brain-$version-$platform.tar.gz" ] || fail
case "$expected_sha256" in
    *[!0-9a-f]*|'') fail ;;
esac
[ "${#expected_sha256}" -eq 64 ] || fail

curl --proto '=https' --tlsv1.2 -LsSf "$release_base/$artifact_name" -o "$archive" || fail
if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256=$(sha256sum "$archive" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    actual_sha256=$(shasum -a 256 "$archive" | awk '{print $1}')
else
    fail
fi
[ "$actual_sha256" = "$expected_sha256" ] || fail

tar -tzf "$archive" > "$members" || fail
[ -s "$members" ] || fail
while IFS= read -r member; do
    case "$member" in
        open-brain|open-brain/*) ;;
        *) fail ;;
    esac
    case "/$member/" in
        */../*|*/./*) fail ;;
    esac
done < "$members"

for directory in "$HOME/.local" "$HOME/.local/lib" "$HOME/.local/bin"; do
    [ ! -L "$directory" ] || fail
    if [ -e "$directory" ]; then
        [ -d "$directory" ] || fail
    else
        mkdir "$directory" || fail
    fi
done
[ ! -e "$payload" ] && [ ! -L "$payload" ] || fail
[ ! -e "$launcher" ] && [ ! -L "$launcher" ] || fail
[ ! -e "$stage" ] && [ ! -L "$stage" ] || fail
[ ! -e "$next_launcher" ] && [ ! -L "$next_launcher" ] || fail

mkdir "$stage" || fail
tar -xzf "$archive" -C "$stage" || fail
[ -d "$stage/open-brain" ] && [ ! -L "$stage/open-brain" ] || fail
[ -f "$stage/open-brain/open-brain" ] || fail
[ ! -L "$stage/open-brain/open-brain" ] || fail
[ -x "$stage/open-brain/open-brain" ] || fail

find "$stage/open-brain" -type l -print > "$links" || fail
while IFS= read -r link; do
    target=$(readlink "$link") || fail
    case "$target" in
        /*|../*|*/../*|*/..) fail ;;
    esac
done < "$links"

"$stage/open-brain/open-brain" __open-brain-self-check >/dev/null 2>&1 || fail
mv "$stage/open-brain" "$payload" || fail
payload_activated=true
rmdir "$stage" || fail
ln -s "../lib/open-brain/open-brain" "$next_launcher" || fail
mv "$next_launcher" "$launcher" || fail
payload_activated=false
trap - EXIT HUP INT TERM
rm -rf "$temporary"

printf '%s\n' '{"command":"open-brain","status":"installed"}'
