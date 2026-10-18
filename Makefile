.PHONY: dev build test test-fast test-integration test-tiers-audit test-profile lint typecheck plugin-build plugin-test desktop-install desktop-test desktop-runtime desktop-native desktop-native-proof audit audit-history native native-audit embedding-native smoke homebrew-smoke native-integration-smoke verify contributor-check

NATIVE_OUTPUT ?= build/native
NATIVE_ARTIFACT = $(NATIVE_OUTPUT)/dist/open-brain/open-brain
NATIVE_GRAPHIFY_ARTIFACT = $(NATIVE_OUTPUT)/dist/open-brain-graphify
NATIVE_MANIFEST = $(NATIVE_OUTPUT)/release/open-brain-component-manifest-v1.txt
EMBEDDING_OUTPUT ?= build/embedding
# Release directory of the static ONNX Runtime build (tools/g4_embedding_probe/ort-build-recipe.json).
EMBEDDING_RUNTIME_BUILD ?= $(if $(filter Darwin,$(shell uname -s)),build/g4-ort-macos-static,build/g4-ort-linux-static)/Release
EMBEDDING_RUNTIME_SOURCE ?= build/g4-src/onnxruntime
EMBEDDING_ASSETS ?= build/g4-assets-1/minilm
EMBEDDING_PLATFORM ?= $(if $(filter Darwin,$(shell uname -s)),macos-arm64,linux-x86_64)
EMBEDDING_VERSION ?= $(shell uv run --frozen python -c "from pathlib import Path; from tools.open_brain_dev.base_native import product_version; print(product_version(Path('.')))")
EMBEDDING_ARCHIVE ?= $(EMBEDDING_OUTPUT)/open-brain-components-v2-embedding-$(EMBEDDING_VERSION)-$(EMBEDDING_PLATFORM).tar.gz
EMBEDDING_ARCHIVE_OPTION = $(if $(wildcard $(EMBEDDING_ARCHIVE)),--embedding-archive "$(EMBEDDING_ARCHIVE)")
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
	uv build --no-sources --project packages/collector --out-dir dist

test:
	uv run --frozen pytest -q -n auto

# Development tiers only; `make test` and `make verify` always run the complete suite.
test-fast:
	uv run --frozen pytest -q -n auto -p tools.open_brain_dev.pytest_tiers -m "not integration"

test-integration:
	uv run --frozen pytest -q -n auto -p tools.open_brain_dev.pytest_tiers -m integration

test-tiers-audit:
	uv run --frozen python -m tools.open_brain_dev.pytest_tiers audit

test-profile:
	@test -n "$(OPEN_BRAIN_PYTEST_TIMING_DIR)" || (echo "OPEN_BRAIN_PYTEST_TIMING_DIR must name an absolute directory outside the checkout" >&2; exit 2)
	@uv run --frozen pytest -q -n auto -p tools.open_brain_dev.pytest_timing
	@uv run --frozen python -m tools.open_brain_dev.pytest_timing_report "$(OPEN_BRAIN_PYTEST_TIMING_DIR)" --nodes-out "$(OPEN_BRAIN_PYTEST_TIMING_DIR)/nodes.json" > "$(OPEN_BRAIN_PYTEST_TIMING_DIR)/summary.json"

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
	cargo test --locked --manifest-path $(DESKTOP_TAURI_DIR)/Cargo.toml -- --test-threads=1

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

# Build order with the embedding resource: `make embedding-native` writes the archive, then
# `make native` pins its inventory SHA-256 into the base before freezing, then `make native-audit`
# re-audits the archive against that pin. Without the archive both targets skip the embedding step.
native: plugin-build
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native build --root . --output $(NATIVE_OUTPUT) $(EMBEDDING_ARCHIVE_OPTION)

native-audit: native
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native audit --artifact $(NATIVE_ARTIFACT) --graphify-artifact $(NATIVE_GRAPHIFY_ARTIFACT) $(EMBEDDING_ARCHIVE_OPTION)

embedding-native:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.embedding_native --version "$(EMBEDDING_VERSION)" --platform $(EMBEDDING_PLATFORM) --runtime-build "$(EMBEDDING_RUNTIME_BUILD)" --runtime-source "$(EMBEDDING_RUNTIME_SOURCE)" --assets "$(EMBEDDING_ASSETS)" --output "$(EMBEDDING_OUTPUT)"

smoke:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native smoke --root . --artifact $(NATIVE_ARTIFACT) --graphify-artifact $(NATIVE_GRAPHIFY_ARTIFACT)

homebrew-smoke: native
	uv run --frozen --python 3.14 --no-dev --group native-build bash tools/homebrew-smoke.sh "$(CURDIR)" "$(abspath $(NATIVE_MANIFEST))" "$(abspath $(NATIVE_OUTPUT)/release)" $(if $(wildcard $(EMBEDDING_ARCHIVE)),"$(abspath $(EMBEDDING_ARCHIVE))")

native-integration-smoke: homebrew-smoke

contributor-check:
	$(MAKE) verify
	$(MAKE) native-integration-smoke
	$(MAKE) desktop-native

verify: lint typecheck test build plugin-test desktop-test
