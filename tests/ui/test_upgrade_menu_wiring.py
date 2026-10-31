"""菜单实体挂载回归（MenuToolbarMixin 回调 + edit 模块描述符注册）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core


class _FakeMainWindow:
    """MenuToolbarMixin 的宿主替身（getattr(self, callback) 解析路径）。"""

    def __init__(self, project=None, timeline=None, engine=None,
                 tracker_node=None, proxy_manager=None):
        self._current_edit_project = project
        self.current_edit_project = project
        self.engine = engine
        self._tracker_node = tracker_node
        self._proxy_manager = proxy_manager
        self.shown_panels = []
        self.information_messages = []

    # 被测 mixin 方法经多继承进入 MainWindow；这里手动混入
    from src.app.controllers.menu_toolbar_controller import (  # noqa: E402
        MenuToolbarMixin,
    )


class TestMenuCallbacks:
    def _host(self, **kw):
        # 构造混入宿主
        host = _FakeMainWindow(**kw)
        # 绑定 mixin 方法（避免真实 QMainWindow）
        import inspect
        from src.app.controllers.menu_toolbar_controller import (
            MenuToolbarMixin,
        )
        for name, fn in inspect.getmembers(MenuToolbarMixin,
                                           predicate=inspect.isfunction):
            if name.startswith(("on_open_", "_open_upgrade_panel",
                                "_upgrade_panel", "_active_color_stack_id",
                                "_selected_tracker_node")):
                setattr(host, name, fn.__get__(host))
        return host

    def test_callbacks_resolve_by_name(self):
        host = self._host()
        for callback in ("on_open_shot_match_panel",
                         "on_open_version_tools_panel",
                         "on_open_filler_removal_panel",
                         "on_open_snapshot_compare_panel",
                         "on_open_proxy_cleanup_panel",
                         "on_open_tracker_corrections_panel",
                         "on_open_bake_dialog",
                         "on_open_multi_version_qc_panel"):
            assert callable(getattr(host, callback)), callback

    def test_shot_match_requires_project(self):
        host = self._host(project=None)
        reasons = []
        host._upgrade_panel_unavailable = reasons.append
        host.on_open_shot_match_panel()  # 无工程必须给出用户可见理由
        assert any("镜头匹配" in r for r in reasons)

    def test_descriptors_registered_by_edit_module(self):
        from src.shared.actions import ActionRegistry
        from src.features.edit.module import EditModule
        mod = EditModule.__new__(EditModule)
        mod._available = True
        registry = ActionRegistry()
        mod.register_menu_actions(registry)
        ids = set(registry._actions.keys())
        for expected in ("color.shot_match.open",
                         "color.version_tools.open",
                         "edit.filler_removal.open",
                         "project.snapshot_compare.open",
                         "project.proxy_cleanup.open",
                         "composite.tracker_corrections.open",
                         "composite.bake.open",
                         "deliver.multi_version_qc.open"):
            assert expected in ids, expected
