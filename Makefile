.PHONY: dev build test lint typecheck plugin-build plugin-test desktop-install desktop-test desktop-runtime desktop-native desktop-native-proof audit audit-history native native-audit smoke homebrew-smoke native-integration-smoke verify contributor-check

NATIVE_OUTPUT ?= build/native
NATIVE_ARTIFACT = $(NATIVE_OUTPUT)/dist/open-brain
NATIVE_GRAPHIFY_ARTIFACT = $(NATIVE_OUTPUT)/dist/open-brain-graphify
NATIVE_MANIFEST = $(NATIVE_OUTPUT)/release/open-brain-component-manifest-v1.txt
DESKTOP_DIR = packages/desktop
DESKTOP_TAURI_DIR = $(DESKTOP_DIR)/src-tauri
DESKTOP_TARGET ?= $(shell rustc -vV | sed -n 's/^host: //p')
DESKTOP_MACOS_APP = $(DESKTOP_TAURI_DIR)/target/release/bundle/macos/Open Brain Desktop.app

dev:
	PYTHONPATH=packages/app/src:packages/connectors/src:packages/engine/src uv run python -m open_brain --version

build: plugin-build
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

plugin-build:
	npm --prefix packages/obsidian-plugin ci --ignore-scripts --no-audit --no-fund
	npm --prefix packages/obsidian-plugin run typecheck
	npm --prefix packages/obsidian-plugin run build

plugin-test: plugin-build
	npm --prefix packages/obsidian-plugin test

desktop-install:
	npm --prefix $(DESKTOP_DIR) ci --ignore-scripts --no-audit --no-fund

desktop-test: desktop-install
	npm --prefix $(DESKTOP_DIR) run typecheck
	npm --prefix $(DESKTOP_DIR) test
	npm --prefix $(DESKTOP_DIR) run build
	uv run --frozen pytest -q $(DESKTOP_DIR)/tests
	cargo fmt --manifest-path $(DESKTOP_TAURI_DIR)/Cargo.toml -- --check
	cargo test --locked --manifest-path $(DESKTOP_TAURI_DIR)/Cargo.toml

desktop-runtime: native
	node $(DESKTOP_DIR)/scripts/prepare-runtime.mjs $(NATIVE_ARTIFACT) $(NATIVE_GRAPHIFY_ARTIFACT) $(NATIVE_MANIFEST) $(DESKTOP_TAURI_DIR)/binaries $(DESKTOP_TARGET)

desktop-native: desktop-test desktop-runtime
	@if test "$$(uname -s)" = Darwin; then \
		npm --prefix $(DESKTOP_DIR) run tauri -- build --bundles app && \
		codesign --verify --deep --strict --verbose=2 "$(DESKTOP_MACOS_APP)"; \
	else \
		npm --prefix $(DESKTOP_DIR) run tauri -- build --bundles appimage; \
	fi

desktop-native-proof: desktop-native
	@test "$$(uname -s)" = Darwin || (echo "desktop-native-proof currently requires macOS arm64" >&2; exit 2)
	"$(DESKTOP_MACOS_APP)/Contents/MacOS/open-brain-desktop" --d0-proof-json

audit:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.release_audit --root . --private-denylist "$(PRIVATE_DENYLIST)"

audit-history:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.public_history_audit --repository . --private-denylist "$(PRIVATE_DENYLIST)"

native: plugin-build
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native build --root . --output $(NATIVE_OUTPUT)

native-audit: native
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native audit --artifact $(NATIVE_ARTIFACT) --graphify-artifact $(NATIVE_GRAPHIFY_ARTIFACT)

smoke:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native smoke --root . --artifact $(NATIVE_ARTIFACT) --graphify-artifact $(NATIVE_GRAPHIFY_ARTIFACT)

homebrew-smoke: native
	uv run --frozen --python 3.14 --no-dev --group native-build bash tools/homebrew-smoke.sh "$(CURDIR)" "$(abspath $(NATIVE_MANIFEST))" "$(abspath $(NATIVE_OUTPUT)/release)"

native-integration-smoke: homebrew-smoke

contributor-check:
	$(MAKE) verify
	$(MAKE) native-integration-smoke
	$(MAKE) desktop-native

verify: lint typecheck test build plugin-test desktop-test
