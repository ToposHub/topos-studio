"""PyInstaller spec for Topos Studio (all features).

Topos Studio includes: color, edit, composite, deliver.
No features are excluded.
"""

import importlib.util as _ilu
import os
import sys

# R7：spec 由 PyInstaller 以 exec 执行——不能用 `from packaging.pyinstaller
# .base_spec import ...`（pip 的 site-packages `packaging` 已在 sys.modules
# 缓存时该导入会解析到错误包）。按文件路径加载，命名空间免疫。
# PyInstaller spec 执行命名空间无 __file__——官方变量 SPECPATH = spec 所在目录
try:
    _SPEC_DIR = SPECPATH  # noqa: F821（PyInstaller 注入）
except NameError:  # 直接 python 执行（测试/调试）时回退
    _SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SPEC_DIR))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_bs_spec = _ilu.spec_from_file_location(
    "_topos_base_spec", os.path.join(_SPEC_DIR, "base_spec.py"))
_bs = _ilu.module_from_spec(_bs_spec)
_bs_spec.loader.exec_module(_bs)
generate_analysis = _bs.generate_analysis
get_product_entry_module = _bs.get_product_entry_module

PRODUCT_NAME = "topos-studio"

a = generate_analysis(
    product_name=PRODUCT_NAME,
    entry_module=get_product_entry_module(PRODUCT_NAME),
    # R7：排除表从产品预设推导（base_spec.get_preset_exclude_dirs）
)

# --- PyInstaller block definitions ---
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=PRODUCT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name=PRODUCT_NAME,
)
