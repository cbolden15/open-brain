.PHONY: dev build test lint typecheck audit audit-history native smoke homebrew-smoke verify

NATIVE_OUTPUT ?= build/native
NATIVE_ARTIFACT = $(NATIVE_OUTPUT)/dist/open-brain
NATIVE_MANIFEST = $(NATIVE_OUTPUT)/release/open-brain-release-manifest-v1.txt
HOMEBREW_SMOKE_TAP = open-brain-local/smoke

dev:
	PYTHONPATH=packages/app/src:packages/connectors/src:packages/engine/src uv run python -m open_brain --version

build:
	mkdir -p dist
	uv build --no-sources --project packages/engine --out-dir dist
	uv build --no-sources --project packages/app --out-dir dist
	uv build --no-sources --project packages/connectors --out-dir dist

test:
	uv run --frozen pytest -q

lint:
	uv run --frozen ruff check .

typecheck:
	uv run --frozen mypy

audit:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.release_audit --root . --private-denylist "$(PRIVATE_DENYLIST)"

audit-history:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.public_history_audit --repository . --private-denylist "$(PRIVATE_DENYLIST)"

native:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native build --root . --output $(NATIVE_OUTPUT)

smoke:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native smoke --root . --artifact $(NATIVE_ARTIFACT)

homebrew-smoke: native
	@set -eu; \
		command -v brew >/dev/null; \
		if brew list --formula open-brain >/dev/null 2>&1; then \
			echo "open-brain is already installed by Homebrew; refusing to replace it" >&2; \
			exit 2; \
		fi; \
		smoke_tap="$(HOMEBREW_SMOKE_TAP)"; \
		smoke_tap_path="$$(brew --repository "$$smoke_tap")"; \
		if [ -d "$$smoke_tap_path" ]; then \
			echo "$$smoke_tap is already tapped; refusing to replace it" >&2; \
			exit 2; \
		fi; \
		smoke_root="$$(mktemp -d "$${TMPDIR:-/tmp}/open-brain-homebrew.XXXXXX")"; \
		cleanup() { \
			HOMEBREW_NO_AUTO_UPDATE=1 brew uninstall --force "$$smoke_tap/open-brain" >/dev/null 2>&1 || true; \
			HOMEBREW_NO_AUTO_UPDATE=1 brew untap --force "$$smoke_tap" >/dev/null 2>&1 || true; \
			rm -rf "$$smoke_root"; \
		}; \
		trap cleanup EXIT INT TERM; \
		mkdir -p "$$smoke_root/tap/Formula"; \
		uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native formula \
			--manifest $(NATIVE_MANIFEST) \
			--output "$$smoke_root/tap/Formula/open-brain.rb" \
			--base-url file://$(abspath $(NATIVE_OUTPUT)/release); \
		git -C "$$smoke_root/tap" init --quiet --initial-branch=main; \
		git -C "$$smoke_root/tap" add Formula/open-brain.rb; \
		printf '%s\n' "Local Open Brain Homebrew smoke" > "$$smoke_root/commit-message"; \
		git -C "$$smoke_root/tap" \
			-c user.name="Open Brain CI" \
			-c user.email="ci@open-brain.invalid" \
			-c commit.gpgsign=false \
			commit --quiet -F "$$smoke_root/commit-message"; \
		HOMEBREW_NO_AUTO_UPDATE=1 brew tap "$$smoke_tap" "file://$$smoke_root/tap"; \
		HOMEBREW_NO_AUTO_UPDATE=1 brew install "$$smoke_tap/open-brain"; \
		uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native smoke \
			--root . \
			--artifact "$$(brew --prefix "$$smoke_tap/open-brain")/bin/open-brain"; \
		cleanup; \
		trap - EXIT INT TERM

verify: lint typecheck test build
