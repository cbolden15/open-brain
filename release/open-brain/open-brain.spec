import sysconfig
from pathlib import Path

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

from tools.open_brain_dev.native_metadata import relocated_sysconfig, runtime_metadata

ROOT = Path(SPECPATH).parents[1]
ENTRYPOINT = ROOT / "packages/app/src/open_brain/services/local_native_entrypoint.py"

datas = sorted(
    collect_data_files(
        "open_brain_engine",
        includes=[
            "portable/**/*.json",
            "portability/**/*.json",
        ],
    )
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
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "argon2",
        "cryptography",
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
        "open_brain_connectors",
        "open_brain_engine.portability.secure_node",
        "open_brain_engine.protocol.custody",
        "open_brain_legacy",
        "sqlcipher3",
        "starlette",
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
