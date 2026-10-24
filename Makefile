.PHONY: dev build test test-shard test-fast test-integration test-tiers-audit test-profile lint typecheck plugin-build plugin-test audit audit-history native native-audit embedding-native smoke homebrew-smoke native-integration-smoke verify verify-without-tests contributor-check contributor-check-without-tests

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
# Without the archive the native targets build and audit no embedding role. That is announced,
# and EMBEDDING_REQUIRED=1 turns it into a failure.
EMBEDDING_REQUIRED ?= 0
EMBEDDING_GUARD = $(if $(wildcard $(EMBEDDING_ARCHIVE)),:,$(if $(filter 1,$(EMBEDDING_REQUIRED)),$(error embedding archive missing: $(EMBEDDING_ARCHIVE)),echo "embedding role skipped: no archive at $(EMBEDDING_ARCHIVE); set EMBEDDING_REQUIRED=1 to fail"))

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

# One of COUNT parallel CI jobs; shards 1 through COUNT together run the complete suite.
test-shard:
	@test -n "$(SHARD)" || (echo "SHARD must be INDEX/COUNT, for example SHARD=1/4" >&2; exit 2)
	uv run --frozen pytest -q -n auto -p tools.open_brain_dev.pytest_shard --shard "$(SHARD)"

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

audit:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.release_audit --root . --private-denylist "$(PRIVATE_DENYLIST)"

audit-history:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.public_history_audit --repository . --private-denylist "$(PRIVATE_DENYLIST)"

# Build order with the embedding resource: `make embedding-native` writes the archive, then
# `make native` pins its inventory SHA-256 into the base before freezing, then `make native-audit`
# re-audits the archive against that pin. Without the archive they skip the embedding step and say so.
native: plugin-build
	@$(EMBEDDING_GUARD)
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native build --root . --output $(NATIVE_OUTPUT) $(EMBEDDING_ARCHIVE_OPTION)

native-audit: native
	@$(EMBEDDING_GUARD)
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native audit --artifact $(NATIVE_ARTIFACT) --graphify-artifact $(NATIVE_GRAPHIFY_ARTIFACT) $(EMBEDDING_ARCHIVE_OPTION)

embedding-native:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.embedding_native --version "$(EMBEDDING_VERSION)" --platform $(EMBEDDING_PLATFORM) --runtime-build "$(EMBEDDING_RUNTIME_BUILD)" --runtime-source "$(EMBEDDING_RUNTIME_SOURCE)" --assets "$(EMBEDDING_ASSETS)" --output "$(EMBEDDING_OUTPUT)"

smoke:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native smoke --root . --artifact $(NATIVE_ARTIFACT) --graphify-artifact $(NATIVE_GRAPHIFY_ARTIFACT)

homebrew-smoke: native
	@$(EMBEDDING_GUARD)
	uv run --frozen --python 3.14 --no-dev --group native-build bash tools/homebrew-smoke.sh "$(CURDIR)" "$(abspath $(NATIVE_MANIFEST))" "$(abspath $(NATIVE_OUTPUT)/release)" $(if $(wildcard $(EMBEDDING_ARCHIVE)),"$(abspath $(EMBEDDING_ARCHIVE))")

native-integration-smoke: homebrew-smoke

contributor-check:
	$(MAKE) verify
	$(MAKE) native-integration-smoke

# CI runs these beside parallel `test-shard` jobs instead of `contributor-check`.
contributor-check-without-tests:
	$(MAKE) verify-without-tests
	$(MAKE) native-integration-smoke

verify: lint typecheck test verify-without-tests

verify-without-tests: lint typecheck build plugin-test
