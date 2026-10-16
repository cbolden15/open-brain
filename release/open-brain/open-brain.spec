import _decimal
import sysconfig
from decimal import Decimal
from pathlib import Path

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

from tools.open_brain_dev.native_metadata import relocated_sysconfig, runtime_metadata

# Supported native interpreters must provide C decimal. Keep the unused Python
# fallback out of the shared closure without changing artifact audit budgets.
if Decimal is not _decimal.Decimal:
    raise ValueError("native build requires compiled decimal")

ROOT = Path(SPECPATH).parents[1]
ENTRYPOINT = ROOT / "packages/app/src/open_brain/services/local_native_entrypoint.py"

datas = sorted(
    collect_data_files(
        "open_brain_engine",
        includes=[
            "engine/t03_schema.json",
            "engine/facet_schema.json",
            "engine/facet_evidence_schema.json",
            "portable/**/*.json",
            "portability/**/*.json",
        ],
    )
    + collect_data_files("open_brain", includes=["assets/*.mjs"])
    + copy_metadata("open-brain")
    + copy_metadata("open-brain-engine")
    + copy_metadata("rfc8785"),
    key=lambda item: (item[1], item[0]),
)

analysis = Analysis(
    [str(ENTRYPOINT)],
    pathex=[
        str(ROOT / "packages/app/src"),
        str(ROOT / "packages/engine/src"),
    ],
    binaries=[],
    datas=datas,
    hiddenimports=["open_brain.services.harness_native_entrypoint"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "_pydecimal",
        "argon2",
        "capng",
        "cryptography",
        "docker",
        "http.server",
        "keyring",
        "open_brain.capture",
        "open_brain.cli",
        "open_brain.extensions",
        "open_brain.integrations",
        "open_brain.services.appliance_application",
        "open_brain.services.appliance_auth",
        "open_brain.services.appliance_daemon",
        "open_brain.services.appliance_entrypoints",
        "open_brain.services.appliance_history",
        "open_brain.services.appliance_init",
        "open_brain.services.appliance_lifecycle",
        "open_brain.services.appliance_recovery",
        "open_brain.services.appliance_scheduler",
        "open_brain.services.appliance_status",
        "open_brain.services.appliance_supervisors",
        "open_brain.services.composition",
        "open_brain.services.connectors",
        "open_brain.services.http_server",
        "open_brain.services.mcp_stdio",
        "open_brain.services.native_artifacts",
        "open_brain.services.native_entrypoint",
        "open_brain.services.phase1_application",
        "open_brain.services.phase1_entrypoints",
        "open_brain.services.runtime",
        "open_brain.services.secure_node_entrypoints",
        "open_brain_collector",
        "open_brain_connectors",
        "open_brain_engine.portability.secure_node",
        "open_brain_engine.engine.authority",
        "open_brain_engine.engine.backup",
        "open_brain_engine.ledger",
        "open_brain_engine.protocol",
        "open_brain_legacy",
        "podman",
        "prctl",
        "pyroute2",
        "socketserver",
        "sqlcipher3",
        "starlette",
        "systemd",
        "uvicorn",
    ],
    noarchive=False,
    optimize=0,
)
analysis.datas = runtime_metadata(analysis.datas)
# Preserve PyInstaller's other module-graph code transformations. Its pinned PYZ
# implementation consumes this cache before reading module source paths.
code_cache = CONF["code_cache"].get(id(analysis.pure))
if code_cache is None:
    raise ValueError("native sysconfig code cache is unavailable")
sysconfig_modules = [entry for entry in analysis.pure if entry[0].startswith("_sysconfigdata_")]
if len(sysconfig_modules) != 1 or sysconfig_modules[0][0] != sysconfig._get_sysconfigdata_name():
    raise ValueError("native sysconfig module inventory is unexpected")
name, source, kind = sysconfig_modules[0]
if name not in code_cache or kind != "PYMODULE":
    raise ValueError("native sysconfig cached module is unavailable")
code_cache[name] = relocated_sysconfig(Path(source).read_text(encoding="utf-8"), name)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="open-brain",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Optional integration commands have strict startup budgets. Keep their shared
# interpreter files installed rather than extracting a one-file bundle per hook.
# This executable remains an unprivileged foreground command with the same
# audited dependency closure as the core; there is no resident worker or listener.
harness_scripts = [entry for entry in analysis.scripts if entry[1] != str(ENTRYPOINT)]
harness_scripts.append(
    (
        "harness_native_entrypoint",
        str(ROOT / "packages/app/src/open_brain/services/harness_native_entrypoint.py"),
        "PYSOURCE",
    )
)
harness_executable = EXE(
    pyz,
    harness_scripts,
    [],
    exclude_binaries=True,
    name="open-brain-harness",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
harness_runtime = COLLECT(
    harness_executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="open-brain-harness",
)
