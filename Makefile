.PHONY: dev build test lint typecheck audit audit-history native native-audit smoke homebrew-smoke verify contributor-check

NATIVE_OUTPUT ?= build/native
NATIVE_ARTIFACT = $(NATIVE_OUTPUT)/dist/open-brain
NATIVE_MANIFEST = $(NATIVE_OUTPUT)/release/open-brain-release-manifest-v1.txt

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

native-audit: native
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native audit --artifact $(NATIVE_ARTIFACT)

smoke:
	uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.open_brain_dev.base_native smoke --root . --artifact $(NATIVE_ARTIFACT)

homebrew-smoke: native
	uv run --frozen --python 3.14 --no-dev --group native-build bash tools/homebrew-smoke.sh "$(CURDIR)" "$(abspath $(NATIVE_MANIFEST))" "$(abspath $(NATIVE_OUTPUT)/release)"

contributor-check:
	$(MAKE) verify
	$(MAKE) homebrew-smoke

verify: lint typecheck test build
