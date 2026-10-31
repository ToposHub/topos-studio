"""M19.2 · 三页统一工程色彩上下文回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

from src.shared.color.color_space_descriptor import (
    apply_project_color_context,
    clear_project_color_overrides,
    get_project_color_overrides,
)


class TestProjectColorContext:
    def test_apply_sets_overrides(self):
        payload = {"color.preview.timeline_transfer": "pq",
                   "color.output.timeline_primaries": "bt2020"}
        apply_project_color_context(payload)
        assert get_project_color_overrides() == payload

    def test_empty_clears(self):
        apply_project_color_context({"color.smart_default": False})
        apply_project_color_context(None)
        assert get_project_color_overrides() == {}

    def test_none_values_dropped(self):
        apply_project_color_context(
            {"color.preview.hdr_tonemap": None, "color.smart_default": True})
        ov = get_project_color_overrides()
        assert "color.preview.hdr_tonemap" not in ov
        assert ov.get("color.smart_default") is True
        clear_project_color_overrides()


# ================================================================
# M19.2b · 注入时机生命周期（打开/关闭/切换）契约
# ================================================================

from src.shared.color.color_context_lifecycle import (  # noqa: E402
    on_project_closed,
    on_project_loaded,
    on_project_switched,
)


class TestContextLifecycle:
    def setup_method(self):
        clear_project_color_overrides()

    def teardown_method(self):
        clear_project_color_overrides()

    def test_loaded_injects_payload_and_returns_snapshot(self):
        payload = {"color.working_space": "acescct",
                   "color.display": "rec709"}
        assert on_project_loaded(payload) == payload
        assert get_project_color_overrides() == payload

    def test_loaded_empty_clears_previous(self):
        on_project_loaded({"color.working_space": "acescct"})
        assert on_project_loaded(None) == {}
        # 新建空文档语义：无作者设定 → 用户全局偏好基线
        on_project_loaded({"color.smart_default": True})
        assert on_project_loaded({}) == {}

    def test_closed_clears(self):
        on_project_loaded({"color.working_space": "acescct"})
        assert on_project_closed() == {}
        assert get_project_color_overrides() == {}

    def test_switched_full_replace_no_linger(self):
        on_project_switched({"color.working_space": "acescct",
                             "color.display": "rec709"})
        on_project_switched({"color.display": "p3d65"})
        # 全量替换而非 merge：A 独有键不残留
        assert get_project_color_overrides() == {"color.display": "p3d65"}


class TestCloseWiring:
    """工程关闭路径清空覆盖层（EditControllerMixin._close_current_project）。"""

    def test_close_current_project_clears_overrides(self):
        from unittest.mock import MagicMock
        from src.app.controllers.edit_controller import EditControllerMixin
        apply_project_color_context({"color.working_space": "acescct"})
        assert get_project_color_overrides()
        host = MagicMock()
        host._current_edit_project = object()
        host._has_unsaved_changes.return_value = False
        host._current_edit_project_filepath = None
        host.edit_project_io = None
        assert EditControllerMixin._close_current_project(host) is True
        assert get_project_color_overrides() == {}
