# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


def _collect_pkgs(*names):
    datas = []
    binaries = []
    hiddenimports = []
    for n in names:
        d, b, h = collect_all(n)
        datas += d
        binaries += b
        hiddenimports += h
    return datas, binaries, hiddenimports


extra_datas, extra_binaries, extra_hiddenimports = _collect_pkgs(
    "easyocr",
    "torch",
    "torchvision",
)


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=extra_binaries,
    datas=[('ico.ico', '.')] + extra_datas,
    hiddenimports=extra_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TxtOnScrn',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['ico.ico'],
)
