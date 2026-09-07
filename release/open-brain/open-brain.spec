from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

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
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
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
artifact = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="open-brain",
)
