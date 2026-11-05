# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('./index.html', '.'),
        ('./pdf-viewer.html', '.'),
        ('./chembio', 'chembio'),
        ('./comms', 'comms'),
        ('./engineering', 'engineering'),
        ('./hacking', 'hacking'),
        ('./isos', 'isos'),
        ('./libs', 'libs'),
        ('./manuals', 'manuals'),
        ('./maps', 'maps'),
        ('./medical', 'medical'),
        ('./prepper', 'prepper'),
        ('./water_food', 'water_food')
    ],
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name='DarkSec-Database',
    icon='guide.png',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DarkSec-Database',
)
