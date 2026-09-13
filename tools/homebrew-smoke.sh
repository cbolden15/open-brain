#!/usr/bin/env bash
# Run under the native-build uv environment. Only the marked reserved tap is disposable.
set -euo pipefail
repo_root=$1
manifest=$2
archive_directory=$3
cd "$repo_root"
command -v brew >/dev/null || { echo 'Homebrew is required for contributor-check' >&2; exit 2; }
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1
smoke_tap=open-brain-local/smoke
smoke_formula=$smoke_tap/open-brain-smoke
smoke_root=$(mktemp -d "${TMPDIR:-/tmp}/open-brain-homebrew.XXXXXX")
guard() { python -m tools.open_brain_dev.homebrew_smoke "$@"; }
cleanup() {
    result=$?
    trap - EXIT INT TERM
    set +e
    if [ -f "$smoke_root/product.json" ]; then
        guard cleanup || result=1
        guard verify "$smoke_root/product.json" || result=1
    fi
    rm -rf "$smoke_root"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
guard snapshot "$smoke_root/product.json"
guard cleanup
mkdir -p "$smoke_root/tap/Formula"
guard mark "$smoke_root/tap"
python -m tools.open_brain_dev.base_native formula --smoke \
    --manifest "$manifest" --output "$smoke_root/tap/Formula/open-brain-smoke.rb" \
    --base-url "file://$archive_directory"
git -C "$smoke_root/tap" init --quiet --initial-branch=main
git -C "$smoke_root/tap" add Formula/open-brain-smoke.rb .open-brain-smoke-owned
printf '%s\n' 'Local Open Brain Homebrew smoke' > "$smoke_root/commit-message"
git -C "$smoke_root/tap" -c user.name='Open Brain CI' \
    -c user.email='ci@open-brain.invalid' -c commit.gpgsign=false \
    commit --quiet -F "$smoke_root/commit-message"
brew tap "$smoke_tap" "file://$smoke_root/tap"
brew install "$smoke_formula"
smoke_prefix=$(brew --prefix "$smoke_formula")
case "$smoke_prefix" in
    /*) ;;
    *) echo 'Homebrew returned a non-absolute smoke prefix' >&2; exit 2 ;;
esac
python -m tools.open_brain_dev.base_native smoke \
    --root "$repo_root" --artifact "$smoke_prefix/bin/open-brain"
