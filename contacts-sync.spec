# PyInstaller spec for a portable, no-venv-needed Windows build.
#
# googleapiclient ships its People API discovery doc as package data and
# certifi ships its CA bundle as package data - neither is picked up by
# PyInstaller's default import scanning, so collect_all() pulls in their
# data files (and submodules/binaries) explicitly. Without this, the frozen
# exe fails at runtime with either a missing discovery doc or SSL cert errors.
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
for package in ("googleapiclient", "certifi", "msal"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["pyinstaller_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="contacts-sync",
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="contacts-sync",
)
