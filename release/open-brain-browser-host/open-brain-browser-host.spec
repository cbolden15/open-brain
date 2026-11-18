from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parents[1]
analysis = Analysis(
    [str(root / "release/open-brain-browser-host/entry.py")],
    pathex=[str(root / "packages" / name / "src") for name in ("app", "engine", "connectors", "browser-host")],
    datas=collect_data_files("open_brain_engine", includes=["**/*.json"])
        + copy_metadata("rfc8785") + copy_metadata("open-brain"),
    excludes=["pytest", "open_brain_collector", "open_brain.integrations", "open_brain.cli"],
)
executable = EXE(
    PYZ(analysis.pure), analysis.scripts, [], exclude_binaries=True,
    name="open-brain-browser-host", console=True, strip=False, upx=False,
)

collection = COLLECT(
    executable, analysis.binaries, analysis.datas,
    name="open-brain-browser-host", strip=False, upx=False,
)
