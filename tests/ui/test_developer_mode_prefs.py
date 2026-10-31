"""开发者模式偏好开关测试。

验收：
- ``developer_mode`` 有默认配置（False）。
- 偏好设置 Advanced > Developer 的开关能正确加载/保存。
- 切换开发者模式后保存会触发"需要重启"标记（菜单在启动时装配，重启生效）。
"""

from __future__ import annotations

import pytest

from src.app.ui.preferences_dialog import PreferencesDialog
from src.shared.config.config_manager import ConfigManager


class _StubConfig:
    """ConfigManager 替身：不落盘，set 直接写内存。"""

    def __init__(self, initial=None):
        self.DEFAULT_CONFIG = ConfigManager.DEFAULT_CONFIG
        self.config = {**self.DEFAULT_CONFIG, **(initial or {})}

    def get(self, key):
        return self.config.get(key)

    def set(self, key, value, auto_save=True):
        self.config[key] = value

    def save_config(self):
        pass


@pytest.fixture
def make_dialog(qapp):
    created = []

    def _make(initial=None):
        dlg = PreferencesDialog(_StubConfig(initial), module_pages=[])
        created.append(dlg)
        return dlg

    yield _make
    for dlg in created:
        dlg.deleteLater()


def test_developer_mode_defaults_to_off():
    assert ConfigManager.DEFAULT_CONFIG.get("developer_mode") is False


def test_developer_mode_checkbox_loads_and_applies(make_dialog):
    dlg = make_dialog({"developer_mode": True})
    assert dlg._initial_developer_mode is True
    assert dlg._widgets["developer_mode"].isChecked()

    dlg._widgets["developer_mode"].setChecked(False)
    dlg._apply_settings()
    assert dlg._config_manager.get("developer_mode") is False


def test_toggling_developer_mode_requires_restart(make_dialog, monkeypatch):
    monkeypatch.setattr(PreferencesDialog, "_show_restart_prompt", lambda self: None)

    # 未变化 -> 不要求重启
    dlg = make_dialog({"developer_mode": False})
    dlg._on_save()
    assert dlg.needs_restart() is False

    # 切换后保存 -> 要求重启
    dlg2 = make_dialog({"developer_mode": False})
    dlg2._widgets["developer_mode"].setChecked(True)
    dlg2._on_save()
    assert dlg2.needs_restart() is True
