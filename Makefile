.PHONY: dev build test lint typecheck audit audit-history phase4-contracts ob1w0-focused ob1w0-preflight ob1w0-native ob1w1-focused ob1w1-preflight ob1w1-native p4w5-focused p4w5-native-config p4w5-native-contracts p4w5-preflight p4w5-native p4w6-focused p4w6-native-config p4w6-preflight p4w6-python p4w6-linux p4w6-macos-compatibility p4w6-macos p4w6-clean-host p4w6-assemble p4w6-verify verify-artifacts verify

OB1W0_FOCUSED_TESTS = \
	packages/app/tests/unit/test_local_data.py \
	packages/app/tests/unit/test_secure_node_entrypoints.py \
	packages/app/tests/integration/services/test_entrypoints.py \
	packages/app/tests/integration/services/test_local_entrypoints.py \
	packages/app/tests/integration/services/test_local_native_entrypoint.py \
	tests/integration/release/test_v0_artifact_policy.py \
	tests/phase4/test_acceptance_harness.py \
	tests/phase4/test_app_distribution.py \
	tests/phase4/test_base_native.py \
	tests/phase4/test_m1_compatibility_baseline.py \
	tests/phase4/test_move_manifest.py \
	tests/security/test_architecture_imports.py \
	tests/security/test_open_brain_product_split.py

OB1W0_TOUCHED_PYTHON = \
	packages/app/src/open_brain/local_data.py \
	packages/app/src/open_brain/profile.py \
	packages/app/src/open_brain/services/local_bootstrap.py \
	packages/app/src/open_brain/services/local_entrypoints.py \
	packages/app/src/open_brain/services/local_native_entrypoint.py \
	packages/app/src/open_brain/services/secure_node_entrypoints.py \
	packages/engine/src/open_brain_engine/engine/local.py \
	tools/open_brain_dev/base_native.py \
	tools/phase4/acceptance_harness.py \
	tools/phase4/move_manifest.py

OB1W0_NATIVE_OUTPUT ?= build/ob1-w0-native

OB1W1_FOCUSED_TESTS = \
	packages/app/tests/integration/services/test_local_entrypoints.py \
	packages/app/tests/integration/services/test_local_native_entrypoint.py \
	tests/integration/release/test_v0_artifact_policy.py \
	tests/phase4/test_base_native.py \
	tests/security/test_open_brain_product_split.py

OB1W1_TOUCHED_PYTHON = \
	packages/app/src/open_brain/services/local_bootstrap.py \
	packages/app/src/open_brain/services/local_entrypoints.py \
	packages/app/tests/integration/services/test_local_entrypoints.py \
	tests/integration/release/test_v0_artifact_policy.py \
	tests/phase4/test_base_native.py \
	tools/open_brain_dev/base_native.py

OB1W1_NATIVE_OUTPUT ?= build/ob1-w1-native

P4W5_FOCUSED_TESTS = \
	packages/app/tests/integration/services/test_appliance_entrypoints.py \
	packages/app/tests/contract/test_connector_worker_protocol.py \
	packages/app/tests/integration/engine/test_portability.py::test_populated_live_root_round_trips_with_stable_identity_bytes_and_results \
	packages/app/tests/integration/services/test_appliance_daemon.py::test_restart_preserves_accepted_capture_identity_without_duplicate_state \
	packages/app/tests/integration/services/test_appliance_recovery.py::test_appliance_recovery_verifies_backup_restores_to_empty_root_and_generates_fresh_credential \
	packages/app/tests/integration/services/test_appliance_recovery.py::test_appliance_recovery_processes_portable_export_and_import_requests_through_scheduler \
	packages/app/tests/integration/services/test_native_entrypoint.py \
	packages/app/tests/integration/services/test_native_artifacts.py \
	packages/app/tests/integration/services/test_appliance_supervisors.py \
	packages/app/tests/integration/services/test_appliance_uninstall.py \
	packages/app/tests/integration/services/test_appliance_upgrade.py \
	tests/integration/release/test_v0_artifact_policy.py \
	tests/phase4/test_connector_distribution.py::test_connector_isolation_runs_on_every_supported_python_in_ci \
	tests/phase4/test_move_manifest.py::test_canonical_move_manifest_is_complete_and_valid \
	tests/phase4/test_move_manifest.py::test_generated_move_and_import_reports_are_exact \
	tests/phase4/test_move_manifest.py::test_manifest_json_is_deterministically_formatted \
	tests/phase4/test_native_build.py \
	tests/phase4/test_p4w5_contracts.py \
	tests/phase4/test_readiness_preflight.py

P4W5_NATIVE_CONTRACTS = \
	packages/app/tests/contract/test_connector_worker_protocol.py \
	packages/app/tests/integration/engine/test_portability.py::test_populated_live_root_round_trips_with_stable_identity_bytes_and_results \
	packages/app/tests/integration/services/test_appliance_daemon.py::test_restart_preserves_accepted_capture_identity_without_duplicate_state \
	packages/app/tests/integration/services/test_appliance_recovery.py::test_appliance_recovery_verifies_backup_restores_to_empty_root_and_generates_fresh_credential \
	packages/app/tests/integration/services/test_appliance_recovery.py::test_appliance_recovery_processes_portable_export_and_import_requests_through_scheduler \
	packages/app/tests/integration/services/test_native_artifacts.py \
	packages/app/tests/integration/services/test_native_entrypoint.py \
	tests/phase4/test_native_build.py \
	tests/phase4/test_p4w5_contracts.py

P4W5_TOUCHED_PYTHON = \
	packages/app/src/open_brain/services/appliance_entrypoints.py \
	packages/app/src/open_brain/services/native_entrypoint.py \
	packages/app/src/open_brain/services/appliance_lifecycle.py \
	packages/app/src/open_brain/services/appliance_supervisors.py \
	packages/app/src/open_brain/services/native_artifacts.py \
	packages/app/src/open_brain/extensions/connector_worker_v1.py \
	packages/app/tests/contract/test_connector_worker_protocol.py \
	packages/app/tests/integration/services/test_appliance_entrypoints.py \
	packages/app/tests/integration/services/test_appliance_supervisors.py \
	packages/app/tests/integration/services/test_appliance_uninstall.py \
	packages/app/tests/integration/services/test_appliance_upgrade.py \
	packages/app/tests/integration/services/test_native_entrypoint.py \
	packages/app/tests/integration/services/test_native_artifacts.py \
	tests/integration/release/test_v0_artifact_policy.py \
	tests/phase4/test_connector_distribution.py \
	tests/phase4/test_move_manifest.py \
	tests/phase4/test_native_build.py \
	tests/phase4/test_p4w5_contracts.py \
	tools/phase4/native_build.py \
	tools/phase4/move_manifest.py \
	tools/phase4/readiness_preflight.py

P4W5_NATIVE_OUTPUT ?= build/p4w5-native
P4W5_SOURCE_SHA ?= $(shell git rev-parse HEAD)

P4W6_FOCUSED_TESTS = \
	tests/phase4/test_p4w6_release.py \
	tests/phase4/test_p4w6_assembly.py \
	tests/phase4/test_p4w6_contracts.py

P4W6_TOUCHED_PYTHON = \
	tests/phase4/test_p4w6_release.py \
	tests/phase4/test_p4w6_assembly.py \
	tests/phase4/test_p4w6_contracts.py \
	tools/phase4/clean_host_fixture.py \
	tools/phase4/native_build.py \
	tools/phase4/release_assembly.py \
	tools/phase4/release_candidate.py

P4W6_SOURCE_SHA ?= $(shell git rev-parse HEAD)
P4W6_PYTHON_OUTPUT ?= build/p4w6-python
P4W6_LINUX_OUTPUT ?= build/p4w6-linux
P4W6_MACOS_COMPAT_OUTPUT ?= build/p4w6-macos-compatibility
P4W6_MACOS_OUTPUT ?= build/p4w6-macos
P4W6_CLEAN_HOST_INPUT ?= build/p4w6-clean-hosts
P4W6_FINAL_OUTPUT ?= build/p4w6-release-candidate
P4W6_PACKAGE ?=
P4W6_CHECKSUM ?=
P4W6_FIXTURE ?=
P4W6_EVIDENCE ?=
P4W6_HOST ?=
P4W6_ARCHITECTURE ?=
P4W6_EXACT_SIGNED ?= false

dev:
	PYTHONPATH=packages/app/src:packages/connectors/src:packages/engine/src uv run python -m open_brain --version

build:
	mkdir -p dist
	uv build --no-sources --project packages/engine --out-dir dist
	uv build --no-sources --project packages/app --out-dir dist
	uv build --no-sources --project packages/connectors --out-dir dist

test:
	uv run --package open-brain --extra secure-node pytest -q

lint:
	uv run ruff check .

typecheck:
	uv run --package open-brain --extra secure-node mypy

audit:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.release_audit --root . --private-denylist "$(PRIVATE_DENYLIST)"

audit-history:
	@test -n "$(PRIVATE_DENYLIST)" || (echo "PRIVATE_DENYLIST is required" >&2; exit 2)
	uv run python -m tools.open_brain_dev.public_history_audit --repository . --private-denylist "$(PRIVATE_DENYLIST)"

phase4-contracts:
	uv run --package open-brain --extra secure-node pytest -q tests/phase4 tests/security/test_architecture_imports.py
	uv run ruff check tools/phase4 tests/phase4 tests/security/test_architecture_imports.py
	uv run --package open-brain --extra secure-node mypy
	uv run python -m tools.phase4.move_manifest validate --root .

ob1w0-focused:
	uv run --package open-brain --extra secure-node pytest -q $(OB1W0_FOCUSED_TESTS)

ob1w0-preflight: ob1w0-focused
	shellcheck release/open-brain/install.sh
	uv run python -m tools.phase4.move_manifest validate --root .
	uv run ruff check $(OB1W0_TOUCHED_PYTHON)
	uv run --package open-brain --extra secure-node mypy --strict $(OB1W0_TOUCHED_PYTHON)
	git diff --check

ob1w0-native:
	uv run --frozen --python 3.14 --group native-build python -m tools.open_brain_dev.base_native build --root . --output $(OB1W0_NATIVE_OUTPUT)

ob1w1-focused:
	uv run --package open-brain --extra secure-node pytest -q $(OB1W1_FOCUSED_TESTS)

ob1w1-preflight: ob1w1-focused
	shellcheck release/open-brain/install.sh
	uv run python -m tools.phase4.move_manifest validate --root .
	uv run ruff check $(OB1W1_TOUCHED_PYTHON)
	uv run --package open-brain --extra secure-node mypy --strict $(OB1W1_TOUCHED_PYTHON)
	git diff --check

ob1w1-native:
	uv run --frozen --python 3.14 --group native-build python -m tools.open_brain_dev.base_native build --root . --output $(OB1W1_NATIVE_OUTPUT)

p4w5-focused:
	uv run --package open-brain --extra secure-node pytest -q $(P4W5_FOCUSED_TESTS)

p4w5-native-config:
	uv run --frozen --isolated --python 3.12 --group native-build python -m tools.phase4.native_build validate-config --root .

p4w5-native-contracts:
	uv run --package open-brain --extra secure-node pytest -q $(P4W5_NATIVE_CONTRACTS)

p4w5-preflight: p4w5-focused p4w5-native-config
	actionlint .github/workflows/ci.yml
	uv run python -m tools.phase4.move_manifest validate --root .
	uv run ruff check $(P4W5_TOUCHED_PYTHON)
	uv run --package open-brain --extra secure-node mypy $(P4W5_TOUCHED_PYTHON)
	git diff --check

p4w5-native:
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.native_build build --root . --output $(P4W5_NATIVE_OUTPUT) --source-sha $(P4W5_SOURCE_SHA)
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.native_build smoke --artifact $(P4W5_NATIVE_OUTPUT)/dist/candidate_native-p4w5 --evidence $(P4W5_NATIVE_OUTPUT)/build-evidence.json
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.native_build audit --artifact $(P4W5_NATIVE_OUTPUT)/dist/candidate_native-p4w5

p4w6-focused:
	uv run --package open-brain --extra secure-node pytest -q $(P4W6_FOCUSED_TESTS)

p4w6-native-config:
	uv run --frozen --isolated --python 3.12 --group native-build python -m tools.phase4.native_build validate-config --root .

p4w6-preflight: p4w6-focused p4w6-native-config
	shellcheck release/native/install.sh tools/phase4/clean_host_lifecycle.sh tools/phase4/supervisor_shim.sh
	perl -c tools/phase4/unix_request.pl
	actionlint .github/workflows/ci.yml
	uv run ruff check $(P4W6_TOUCHED_PYTHON)
	uv run --package open-brain --extra secure-node mypy $(P4W6_TOUCHED_PYTHON)
	git diff --check

p4w6-python:
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.release_assembly build-python --root . --output $(P4W6_PYTHON_OUTPUT) --source-sha $(P4W6_SOURCE_SHA)

p4w6-linux:
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.release_assembly build-linux --root . --output $(P4W6_LINUX_OUTPUT) --source-sha $(P4W6_SOURCE_SHA)

p4w6-macos-compatibility:
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.release_assembly build-macos-compatibility --root . --output $(P4W6_MACOS_COMPAT_OUTPUT) --source-sha $(P4W6_SOURCE_SHA)

p4w6-macos:
	@test -n "$$OPEN_BRAIN_NOTARY_PROFILE" || exit 2
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.release_assembly build-macos --root . --output $(P4W6_MACOS_OUTPUT) --source-sha $(P4W6_SOURCE_SHA)

p4w6-clean-host:
	@test -n "$(P4W6_PACKAGE)" || exit 2
	@test -n "$(P4W6_CHECKSUM)" || exit 2
	@test -n "$(P4W6_FIXTURE)" || exit 2
	@test -n "$(P4W6_EVIDENCE)" || exit 2
	@test -n "$(P4W6_HOST)" || exit 2
	@test -n "$(P4W6_ARCHITECTURE)" || exit 2
	@tools/phase4/clean_host_lifecycle.sh "$(abspath $(P4W6_PACKAGE))" "$(abspath $(P4W6_CHECKSUM))" "$(abspath $(P4W6_FIXTURE))" "$(abspath $(P4W6_EVIDENCE))" "$(P4W6_SOURCE_SHA)" "$(P4W6_HOST)" "$(P4W6_ARCHITECTURE)" "$(P4W6_EXACT_SIGNED)"

p4w6-assemble:
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.release_assembly assemble --root . --output $(P4W6_FINAL_OUTPUT) --source-sha $(P4W6_SOURCE_SHA) --python-directory $(P4W6_PYTHON_OUTPUT) --linux-directory $(P4W6_LINUX_OUTPUT) --macos-directory $(P4W6_MACOS_OUTPUT) --clean-host-directory $(P4W6_CLEAN_HOST_INPUT)

p4w6-verify:
	uv run --frozen --python 3.12 --group native-build python -m tools.phase4.release_assembly verify --candidate $(P4W6_FINAL_OUTPUT)

verify-artifacts: build
	uv run python -m tools.open_brain_dev.artifact_policy --policy release/v0-artifact-policy.json --artifacts dist/*

verify: lint typecheck test verify-artifacts
