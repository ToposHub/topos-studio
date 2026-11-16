"""UI 测试全局隔离：原生 Files 的 QSettings 不得写入开发者本机配置。

``NativeFilesPanel`` 默认用真实 QSettings 记住视图/密度/侧栏；测试若直接
构造面板，会把测试时的开合状态写进用户配置，并让后续用例互相污染。
这里把默认 settings 重定向到每个测试自己的临时 ini 文件。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings


@pytest.fixture(autouse=True)
def _isolate_native_files_preferences(tmp_path, monkeypatch):
    from src.app.ui import native_files_prefs

    ini = tmp_path / "files_native_prefs.ini"
    original_init = native_files_prefs.NativeFilesPreferences.__init__

    def _init(self, settings: QSettings | None = None) -> None:
        original_init(self, settings or QSettings(str(ini), QSettings.Format.IniFormat))

    monkeypatch.setattr(native_files_prefs.NativeFilesPreferences, "__init__", _init)
    yield
