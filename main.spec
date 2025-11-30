# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_all

# 1. SETUP: Get base Playwright dependencies
# This gathers the internal python drivers (node.exe wrappers)
datas, binaries, hiddenimports = collect_all('playwright')

# 2. SETUP: Add your app specific files
datas.append(('settings.css', '.'))

# 3. CRITICAL: Add the actual Browser Binary
# We assume the path based on your error log. 
# If this fails, check C:\Users\neoni\AppData\Local\ms-playwright\ for the exact folder name.
source_browser_path = r"C:\Users\neoni\AppData\Local\ms-playwright\chromium_headless_shell-1194"
dest_browser_path = "playwright/browsers/chromium_headless_shell-1194"

if os.path.exists(source_browser_path):
    datas.append((source_browser_path, dest_browser_path))
else:
    raise FileNotFoundError(f"Could not find browser at {source_browser_path}. Please run 'playwright install chromium'")

# 4. Clean up hidden imports
hiddenimports += [
    'flask', 
    'flask.cli', 
    'werkzeug.middleware.dispatcher', 
    'pystray', 
    'PIL._tkinter_finder'
]

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pandas'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='NeonSpotOBS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico'
)