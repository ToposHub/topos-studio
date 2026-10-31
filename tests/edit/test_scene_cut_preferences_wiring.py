"""E07: 场景检测偏好接线。

审计实测：偏好页保存 sensitivity/use_proxy/min_scene_frames，但生产代码
只读硬编码 0.55/True/8；``get_scene_cut_settings_from_config`` 与
``set_default_settings`` 在 src 中零调用。

回归锁定：
- 偏好 → SceneCutSettings 的映射正确（含范围钳制）；
- 注入后控制器默认设置随偏好；
- EditPage 创建/复用控制器时都刷新偏好（源码断言）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.project import EditProject
from src.features.edit.core.scene_cut.models import SceneCutSettings
from src.features.edit.core.scene_cut_controller import SceneCutController
from src.features.edit.preferences import get_scene_cut_settings_from_config

_EDIT_PAGE = (
    Path(__file__).resolve().parents[2] / "src" / "features" / "edit" /
    "ui" / "edit_page.py"
)


class TestPreferencesMapping:
    def test_config_values_map_to_settings(self) -> None:
        settings = get_scene_cut_settings_from_config({
            "scene_cut_sensitivity": 0.70,
            "scene_cut_use_proxy": False,
            "scene_cut_min_scene_frames": 2,
        })
        assert settings["sensitivity"] == 0.70
        assert settings["use_proxy"] is False
        assert settings["min_scene_frames"] == 2

    def test_out_of_range_clamped(self) -> None:
        settings = get_scene_cut_settings_from_config({
            "scene_cut_sensitivity": 99.0,
            "scene_cut_min_scene_frames": 9999,
        })
        assert settings["sensitivity"] <= 0.75
        assert settings["min_scene_frames"] <= 120


class TestControllerDefaults:
    def test_set_default_settings_wins_over_hardcoded(self) -> None:
        project = EditProject(project_id="p", name="P")
        controller = SceneCutController(project_getter=lambda: project)
        # 未注入时是硬编码默认
        assert controller._get_default_settings().sensitivity == 0.55

        controller.set_default_settings(SceneCutSettings(
            sensitivity=0.70, min_scene_frames=2, use_proxy=False))
        defaults = controller._get_default_settings()
        assert defaults.sensitivity == 0.70
        assert defaults.min_scene_frames == 2
        assert defaults.use_proxy is False


class TestEditPageWiring:
    def test_ensure_controller_applies_preferences(self) -> None:
        text = _EDIT_PAGE.read_text(encoding="utf-8")
        assert "_apply_scene_cut_preferences" in text
        # 创建路径与已存在路径都要刷新（偏好保存后下一次检测生效）
        assert text.count("self._apply_scene_cut_preferences(") >= 2
