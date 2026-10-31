# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 冒烟 spec（W7-03）：验证 base_spec._collect_pyav_dlls 的
av.libs/ 布局在真实构建产物中可用（本机可重复执行，见计划 §11）。

用法（仓库根目录）：
    .venv/Scripts/python.exe -m PyInstaller --noconfirm \\
        --distpath "%TEMP%\\pyinstaller_smoke\\dist" \\
        --workpath "%TEMP%\\pyinstaller_smoke\\build" \\
        packaging/pyinstaller/smoke_av.spec
"""

import sys
from pathlib import Path

# PyInstaller exec spec 时提供 SPECPATH（本文件所在目录）
sys.path.insert(0, str(SPECPATH))
from base_spec import _collect_pyav_dlls  # noqa: E402

av_binaries = _collect_pyav_dlls()
print(f"[smoke_av.spec] collected {len(av_binaries)} av binaries",
      flush=True)
if not av_binaries:
    raise SystemExit("smoke_av.spec: _collect_pyav_dlls() returned 0 items")

a = Analysis(
    [str(Path(SPECPATH) / "smoke_av_main.py")],
    pathex=[str(SPECPATH)],
    binaries=av_binaries,
    hiddenimports=["av"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PyQt6", "PyQt5", "moderngl", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="smoke_av",
    debug=False,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="smoke_av",
)
