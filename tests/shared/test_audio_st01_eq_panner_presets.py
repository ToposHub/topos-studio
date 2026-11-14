"""ST-01 补齐回归：EQ / Panner 用户预设跨会话持久化。

计划 §11 ST-01 明确"EQ/Panner 同类入口也需一起复核"——此前只接了
Dynamics/Delay/Reverb。本文件锁定：
- 打开对话框时从用户库（~/.topos/effect_presets.json）加载自定义预设；
- "+" 保存动作原子落盘，重启/新工程后可再次加载；
- 用户库不可读时对话框仍可打开（降级不阻断）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture()
def preset_store(tmp_path, monkeypatch):
    """把 effect_preset 存储重定向到临时目录。"""
    import src.features.edit.core.effect_preset as ep

    monkeypatch.setattr(ep, "_store_path",
                        lambda: str(tmp_path / "effect_presets.json"))
    return ep


class TestSt01EqPresetPersistence:
    def test_saved_preset_loads_into_new_dialog(self, qapp, preset_store):
        """保存过的 EQ 预设 → 新对话框预设列表/combo 可见且参数可应用。"""
        from src.shared.ui.audio_mixer.strip_eq_dialog import (
            PARAMETRIC_EQ_PRESETS,
            ParametricEQDialog,
        )

        preset_store.save_user_preset(
            "strip_eq", "My Air", {"master_gain": 2.5, "enabled": True})

        dlg = ParametricEQDialog("a1", "A1", "", None)
        try:
            names = [p["name"] for p in dlg._presets]
            assert "My Air" in names
            # 用户预设排在内置之后，combo 条目数一致
            assert dlg.preset_combo.count() == len(dlg._presets)
            assert dlg.preset_combo.count() > len(PARAMETRIC_EQ_PRESETS)
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_save_new_preset_persists(self, qapp, preset_store, monkeypatch):
        """对话框 "+" 保存动作 → 原子落盘（load_user_presets 能读回）。"""
        from PyQt6.QtWidgets import QInputDialog

        from src.shared.ui.audio_mixer.strip_eq_dialog import ParametricEQDialog

        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("Bass Cut", True)))

        dlg = ParametricEQDialog("a1", "A1", "", None)
        try:
            dlg._save_new_preset()
            loaded = preset_store.load_user_presets("strip_eq")
            assert any(e["name"] == "Bass Cut" for e in loaded)
            entry = next(e for e in loaded if e["name"] == "Bass Cut")
            assert isinstance(entry["params"], dict)
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_unreadable_library_does_not_block_dialog(self, qapp,
                                                      preset_store,
                                                      tmp_path, monkeypatch):
        """库文件损坏 → 对话框仍可构造（内置预设可用）。"""
        path = tmp_path / "effect_presets.json"
        path.write_text("{ not json", encoding="utf-8")

        from src.shared.ui.audio_mixer.strip_eq_dialog import ParametricEQDialog

        dlg = ParametricEQDialog("a1", "A1", "", None)
        try:
            assert dlg.preset_combo.count() >= 1
        finally:
            dlg.close()
            dlg.deleteLater()


class TestSt01PannerPresetPersistence:
    def test_saved_preset_loads_into_new_dialog(self, qapp, preset_store):
        """保存过的声像预设 → 新对话框 combo 携带完整 state。"""
        from src.shared.ui.audio_mixer.panner_dialog import SpatialPannerDialog

        preset_store.save_user_preset(
            "panner", "Wide Left", {"lr": -40.0, "spread": 90.0})

        dlg = SpatialPannerDialog("a1", "A1", -0.4, None, None)
        try:
            names = [p["name"] for p in dlg._custom_presets]
            assert "Wide Left" in names
            # combo 末尾条目携带该 state（itemData）
            idx = dlg.preset_combo.findText("Wide Left")
            assert idx >= 0
            data = dlg.preset_combo.itemData(idx)
            assert isinstance(data, dict) and data.get("lr") == -40.0
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_save_new_preset_persists(self, qapp, preset_store, monkeypatch):
        """对话框 "+" 保存动作 → 原子落盘。"""
        from PyQt6.QtWidgets import QInputDialog

        from src.shared.ui.audio_mixer.panner_dialog import SpatialPannerDialog

        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("Club Spot", True)))

        dlg = SpatialPannerDialog("a1", "A1", 0.25, None, None)
        try:
            dlg._save_new_preset()
            loaded = preset_store.load_user_presets("panner")
            assert any(e["name"] == "Club Spot" for e in loaded)
        finally:
            dlg.close()
            dlg.deleteLater()
