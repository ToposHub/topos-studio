"""
Test: 3D 预览视图模式切换（Tab / Scene3D 面板开关）。

之前的「视口比例渲染」方案已废弃（导致 2D/3D 视角对不上 + resize 变形）。
现在 2D/3D 用同一套项目比例 letterbox 显示，切换模式只改交互（Alt 导航/gizmo），
不改渲染。本测试聚焦视图模式切换的状态同步：

- _on_view_mode_changed：Tab 切 2D/3D 时同步 _viewer3d_node_id（焦点）+ 面板高亮 + 重渲染。
- _on_view_mode_toggle_requested：面板开关 → viewer.set_view_mode（与 Tab 同路径）。
- 选中 scene3d 不自动切模式（_on_node_selected 仅记录焦点）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_page():
    """CompositePage with minimal mocking — only the bits the handlers read."""
    from src.features.composite.ui.composite_page import CompositePage
    # Bypass __init__ (which builds heavy Qt UI); we only need the methods + state.
    page = CompositePage.__new__(CompositePage)
    page._viewer3d_node_id = None
    page._viewer3d_active_camera_id = None
    page._is_active = True
    page._eval_debounce_timer = MagicMock()
    page._do_evaluate_graph = MagicMock()
    page._properties_panel = None
    page._viewer = MagicMock()
    page._viewer.get_view_mode.return_value = "3d"
    page._node_graph = None
    page._node_editor = MagicMock()
    # 桩保真度（T-P1-71）：MagicMock 的 is_in_group_view() 默认真值会让
    # 处理器误入 _exit_group_view 的 undo 记录路径——__new__ 桩没有
    # _undo_stack，属性查找落到未初始化的 Qt 包装上抛 RuntimeError。
    page._node_editor.is_in_group_view.return_value = False
    page._preview_node_id = None
    # P1-5：nav3d 恢复 timer + active flag（_on_viewer3d_nav_started 读）
    page._nav3d_restore_timer = MagicMock()
    page._nav3d_active = False
    return page


def _make_3d_node(node_type="scene3d"):
    n = MagicMock()
    n.node_type = node_type
    return n


class _PreviewNode:
    def __init__(self, node_type):
        self.node_type = node_type
        self.parameters = {}

    def set_parameter(self, key, value):
        self.parameters[key] = value


class TestOnViewModeChanged:
    """_on_view_mode_changed: Tab 切 2D/3D 时同步状态 + 触发重渲染。"""

    def test_switch_to_2d_keeps_3d_node_focus(self):
        """Tab 切到 2D → 不清空活动 3D 节点（保留焦点便于切回）+ 触发重渲染。"""
        page = _make_page()
        page._viewer3d_node_id = "s1"
        page._eval_debounce_timer.isActive.return_value = False
        page._on_view_mode_changed("2d")
        # 切 2D 不清空焦点（仅退出 3D 交互）
        assert page._viewer3d_node_id == "s1"
        assert page._force_next_view_refresh is True
        page._do_evaluate_graph.assert_called_once()

    def test_switch_to_3d_keeps_existing_3d_node(self):
        """Tab 切到 3D 且已选中 3D 节点 → 保持不变 + 触发重渲染。"""
        page = _make_page()
        page._viewer3d_node_id = "s1"
        page._eval_debounce_timer.isActive.return_value = False
        page._on_view_mode_changed("3d")
        assert page._viewer3d_node_id == "s1"
        assert page._force_next_view_refresh is True
        page._do_evaluate_graph.assert_called_once()

    def test_switch_to_3d_no_selection_uses_virtual_viewer_scene(self):
        """Tab 切到 3D 不再要求 Scene3D context。"""
        page = _make_page()
        page._viewer3d_node_id = None
        page._eval_debounce_timer.isActive.return_value = False
        page._node_graph = MagicMock()
        page._node_graph.nodes.items.return_value = [
            ("m1", _make_3d_node("merge")),
            ("s1", _make_3d_node("scene3d")),
        ]

        page._on_view_mode_changed("3d")
        assert page._viewer3d_node_id is None
        page._do_evaluate_graph.assert_called_once()

    def test_switch_to_3d_no_3d_nodes_in_graph_stays_none(self):
        """Tab 切到 3D 但图里没有 3D 节点 → 仍 None（但仍触发重渲染）。"""
        page = _make_page()
        page._viewer3d_node_id = None
        page._eval_debounce_timer.isActive.return_value = False
        page._node_graph = MagicMock()
        page._node_graph.nodes.items.return_value = [("m1", _make_3d_node("merge"))]

        page._on_view_mode_changed("3d")
        assert page._viewer3d_node_id is None
        page._do_evaluate_graph.assert_called_once()

    def test_switch_to_3d_syncs_panel_view_mode(self):
        """切到 3D → 同步 Scene3D 参数面板的开关高亮（set_view_mode("3d")）。"""
        page = _make_page()
        page._viewer3d_node_id = "s1"
        page._properties_panel = MagicMock()
        page._eval_debounce_timer.isActive.return_value = False
        page._on_view_mode_changed("3d")
        page._properties_panel.set_view_mode.assert_called_once_with("3d")

    def test_switch_to_2d_syncs_panel_view_mode(self):
        """切到 2D → 同步面板开关高亮（set_view_mode("2d")）。"""
        page = _make_page()
        page._viewer3d_node_id = "s1"
        page._properties_panel = MagicMock()
        page._eval_debounce_timer.isActive.return_value = False
        page._on_view_mode_changed("2d")
        page._properties_panel.set_view_mode.assert_called_once_with("2d")

    def test_inactive_page_no_eval(self):
        """页面非 active → 不触发评估。"""
        page = _make_page()
        page._viewer3d_node_id = "s1"
        page._is_active = False
        page._on_view_mode_changed("2d")
        page._eval_debounce_timer.start.assert_not_called()
        page._do_evaluate_graph.assert_not_called()


class TestOnViewModeToggleRequested:
    """_on_view_mode_toggle_requested: Scene3D 面板开关 → viewer.set_view_mode（与 Tab 同路径）。"""

    def test_toggle_3d_calls_viewer_set_view_mode(self):
        """面板点 3D → viewer.set_view_mode("3d")（与 Tab 同路径）。"""
        page = _make_page()
        page._on_view_mode_toggle_requested("3d")
        page._viewer.set_view_mode.assert_called_once_with("3d")

    def test_toggle_2d_calls_viewer_set_view_mode(self):
        """面板点 2D → viewer.set_view_mode("2d")。"""
        page = _make_page()
        page._on_view_mode_toggle_requested("2d")
        page._viewer.set_view_mode.assert_called_once_with("2d")

    def test_toggle_unknown_mode_normalizes_to_2d(self):
        """未知模式 → 归一化为 2d（防御）。"""
        page = _make_page()
        page._on_view_mode_toggle_requested("weird")
        page._viewer.set_view_mode.assert_called_once_with("2d")


class TestTabShortcutFallback:
    """Page-level Tab fallback: focus outside viewport still toggles mode."""

    def test_shortcut_toggles_3d_to_2d(self):
        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"
        page._toggle_view_mode_from_shortcut()
        page._viewer.set_view_mode.assert_called_once_with("2d")

    def test_shortcut_toggles_2d_to_3d(self):
        page = _make_page()
        page._viewer.get_view_mode.return_value = "2d"
        page._toggle_view_mode_from_shortcut()
        page._viewer.set_view_mode.assert_called_once_with("3d")

    def test_shortcut_ignored_when_inactive(self):
        page = _make_page()
        page._is_active = False
        page._toggle_view_mode_from_shortcut()
        page._viewer.set_view_mode.assert_not_called()


class TestViewer3DNavContext:
    """3D navigation writes selected Camera3D params without stealing Scene3D context."""

    def test_set_viewer_sets_page_back_reference_for_virtual_nav_target(self):
        class _Signal:
            def connect(self, _slot):
                pass

        viewer = SimpleNamespace(
            interaction_started=_Signal(),
            interaction_updated=_Signal(),
            interaction_ended=_Signal(),
            scene3d_nav_started=_Signal(),
            scene3d_nav_preview=_Signal(),
            scene3d_nav_committed=_Signal(),
            view_mode_changed=_Signal(),
            quad_view_changed=_Signal(),
            active_camera_requested=_Signal(),
        )
        page = _make_page()

        page.set_viewer(viewer)

        assert viewer._composite_page is page

    def test_viewer_alt_nav_targets_virtual_scene_not_selected_camera(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._interaction_state = SimpleNamespace(selected_node_id="cam1")
        viewer._composite_page = SimpleNamespace(
            _VIEWER3D_VIRTUAL_NODE_ID="__viewer3d_scene__",
        )

        assert viewer._nav_target_node_id() == "__viewer3d_scene__"

    def test_nav_target_does_not_redirect_scene3d_to_connected_camera(self):
        scene = SimpleNamespace(node_type="scene3d")
        cam = SimpleNamespace(node_type="camera3d")
        nodes = {"s1": scene, "cam1": cam}
        page = _make_page()
        page._node_graph = SimpleNamespace(
            get_node=lambda nid: nodes.get(nid),
            connections=[
                SimpleNamespace(
                    src_node_id="cam1",
                    dst_node_id="s1",
                    dst_port_name="in",
                )
            ],
        )

        assert page._resolve_nav_target("s1") == "s1"

    def test_selecting_scene3d_does_not_sync_viewer_camera(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer.sync_scene3d_params = MagicMock()
        viewer._update_gizmo_visibility = MagicMock()
        viewer._update_tool_handles = MagicMock()
        viewer.update = MagicMock()

        viewer.set_node_info(
            "scene3d",
            {"cam_dist": 99.0, "cam_orbit_yaw": 42.0},
            None,
            node_id="s1",
        )

        viewer.sync_scene3d_params.assert_not_called()

    def test_camera_nav_started_keeps_scene3d_context(self):
        page = _make_page()
        page._viewer3d_node_id = "s1"
        cam = SimpleNamespace(
            node_type="camera3d",
            parameters={
                "cam_eye_x": 4.0,
                "cam_eye_y": 3.0,
                "cam_eye_z": 5.0,
                "cam_target_x": 0.0,
                "cam_target_y": 0.0,
                "cam_target_z": 0.0,
            },
        )
        scene = SimpleNamespace(node_type="scene3d", parameters={})
        nodes = {"cam1": cam, "s1": scene}
        page._node_graph = SimpleNamespace(
            nodes=nodes,
            connections=[
                SimpleNamespace(
                    src_node_id="cam1",
                    dst_node_id="s1",
                    dst_port_name="in",
                )
            ],
            get_node=lambda nid: nodes.get(nid),
        )

        page._on_viewer3d_nav_started("cam1")

        assert page._viewer3d_node_id == "s1"
        assert page._viewer3d_nav_before["cam_eye_x"] == 4.0

    def test_3d_viewer_resolves_to_virtual_scene(self):
        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"

        assert page._resolve_viewer_node_id() == page._VIEWER3D_VIRTUAL_NODE_ID

    def test_virtual_viewer_nav_preview_updates_default_scene_camera(self):
        page = _make_page()
        page._node_graph = SimpleNamespace(_viewer_state={})
        page._eval_debounce_timer.isActive.return_value = False

        page._on_viewer3d_nav_preview(page._VIEWER3D_VIRTUAL_NODE_ID, {
            "cam_orbit_yaw": 48.0,
            "cam_orbit_pitch": 12.0,
            "cam_dist": 7.5,
            "cam_target_x": 1.0,
            "cam_target_y": 2.0,
            "cam_target_z": 3.0,
        })

        default_scene = page._node_graph._viewer_state["default_3d_scene"]
        assert default_scene["cam_orbit_yaw"] == 48.0
        assert default_scene["cam_orbit_pitch"] == 12.0
        assert default_scene["cam_dist"] == 7.5
        assert default_scene["cam_target_x"] == 1.0
        assert default_scene["cam_target_y"] == 2.0
        assert default_scene["cam_target_z"] == 3.0
        # P6：nav preview 不再设 force_refresh（相机变化由 view_signature 捕获）；
        # 3D 视角拖动需要即时反馈，所以 preview 走 now 而不是 debounce。
        page._eval_debounce_timer.stop.assert_called_once()
        page._do_evaluate_graph.assert_called_once()
        page._eval_debounce_timer.start.assert_not_called()

    def test_virtual_viewer_nav_started_records_default_scene_camera(self):
        page = _make_page()
        page._node_graph = SimpleNamespace(_viewer_state={
            "default_3d_scene": {
                "cam_orbit_yaw": 10.0,
                "cam_orbit_pitch": 20.0,
                "cam_dist": 3.0,
                "cam_target_x": 4.0,
                "cam_target_y": 5.0,
                "cam_target_z": 6.0,
            }
        })

        page._on_viewer3d_nav_started(page._VIEWER3D_VIRTUAL_NODE_ID)

        assert page._viewer3d_nav_before == {
            "cam_orbit_yaw": 10.0,
            "cam_orbit_pitch": 20.0,
            "cam_dist": 3.0,
            "cam_target_x": 4.0,
            "cam_target_y": 5.0,
            "cam_target_z": 6.0,
        }

    def test_activate_camera_sets_viewer_active_camera(self):
        cam = SimpleNamespace(node_type="camera3d", parameters={
            "cam_eye_x": 4.0,
            "cam_eye_y": 3.0,
            "cam_eye_z": 5.0,
            "cam_target_x": 0.0,
            "cam_target_y": 0.0,
            "cam_target_z": 0.0,
        })
        nodes = {"cam1": cam}
        page = _make_page()
        page._node_graph = SimpleNamespace(
            nodes=nodes,
            connections=[],
            get_node=lambda nid: nodes.get(nid),
        )
        page._properties_panel = MagicMock()
        page._eval_debounce_timer.isActive.return_value = False

        page._on_camera_activation_toggled("cam1", True)

        assert page._viewer3d_active_camera_id == "cam1"
        page._viewer.set_view_mode.assert_called_with("3d")
        page._viewer.look_through_camera_params.assert_called_once_with(cam.parameters)
        page._properties_panel.set_active_camera_id.assert_called_once_with("cam1")

    def test_deactivate_camera_restores_default_viewer_camera(self):
        page = _make_page()
        page._viewer3d_active_camera_id = "cam1"
        page._node_graph = SimpleNamespace(_viewer_state={
            "default_3d_scene": {
                "cam_orbit_yaw": 10.0,
                "cam_orbit_pitch": 20.0,
                "cam_dist": 3.0,
                "cam_target_x": 4.0,
                "cam_target_y": 5.0,
                "cam_target_z": 6.0,
            }
        })
        page._properties_panel = MagicMock()

        page._on_camera_activation_toggled("cam1", False)

        assert page._viewer3d_active_camera_id is None
        page._viewer.sync_scene3d_params.assert_called_once_with({
            "cam_orbit_yaw": 10.0,
            "cam_orbit_pitch": 20.0,
            "cam_dist": 3.0,
            "cam_target_x": 4.0,
            "cam_target_y": 5.0,
            "cam_target_z": 6.0,
        })
        page._properties_panel.set_active_camera_id.assert_called_once_with("")

    def test_active_camera_nav_does_not_overwrite_default_scene_camera(self):
        page = _make_page()
        page._viewer3d_active_camera_id = "cam1"
        page._node_graph = SimpleNamespace(_viewer_state={
            "default_3d_scene": {"cam_orbit_yaw": 10.0}
        })
        page._eval_debounce_timer.isActive.return_value = False

        page._on_viewer3d_nav_preview(page._VIEWER3D_VIRTUAL_NODE_ID, {
            "cam_orbit_yaw": 80.0,
            "cam_dist": 9.0,
        })

        default_scene = page._node_graph._viewer_state["default_3d_scene"]
        assert default_scene["cam_orbit_yaw"] == 10.0
        assert "cam_dist" not in default_scene

    def test_virtual_viewer_connects_only_active_camera_to_renderer(self):
        from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot

        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"
        page._viewer._nav_params_dict.return_value = {}
        page._viewer3d_active_camera_id = "cam1"
        page._node_graph = SimpleNamespace(_viewer_state={})
        snapshot = GraphSnapshot(
            generation=1,
            nodes={
                "cam1": NodeSnapshot(
                    node_id="cam1", node_type="camera3d", parameters={},
                    disabled=False, input_ports=[], output_ports=[("display", "scene")]),
                "cam2": NodeSnapshot(
                    node_id="cam2", node_type="camera3d", parameters={},
                    disabled=False, input_ports=[], output_ports=[("display", "scene")]),
            },
            connections=[],
            eval_order=["cam1", "cam2"],
        )

        snapshot = page._inject_viewer3d_scene_snapshot(snapshot)

        camera_connections = [
            c for c in snapshot.connections
            if c.dst_node_id == page._VIEWER3D_VIRTUAL_NODE_ID
            and c.dst_port_name == "in"
            and c.src_port_name == "display"
        ]
        assert [c.src_node_id for c in camera_connections] == ["cam1"]

    def test_active_viewer_camera_icon_is_hidden(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        cam1 = SimpleNamespace(
            id="cam1",
            node_type="camera3d",
            parameters={
                "camera_mode": "free",
                "cam_eye_x": 1.0,
                "cam_eye_y": 2.0,
                "cam_eye_z": 3.0,
            },
        )
        cam2 = SimpleNamespace(
            id="cam2",
            node_type="camera3d",
            parameters={
                "camera_mode": "free",
                "cam_eye_x": 4.0,
                "cam_eye_y": 5.0,
                "cam_eye_z": 6.0,
            },
        )
        light = SimpleNamespace(
            id="light1",
            node_type="light3d",
            parameters={},
        )
        page = SimpleNamespace(
            _viewer3d_active_camera_id="cam1",
            _node_graph=SimpleNamespace(nodes={
                "cam1": cam1,
                "cam2": cam2,
                "light1": light,
            }),
        )
        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._composite_page = page

        icons = viewer._collect_node_icons()

        assert {icon["node_id"] for icon in icons} == {"cam2", "light1"}

    def test_active_viewer_camera_gizmo_is_hidden(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._view_mode = "3d"
        viewer._selected_node_type = "camera3d"
        viewer._selected_node_params = {"camera_mode": "free"}
        viewer._interaction_state = SimpleNamespace(selected_node_id="cam1")
        viewer._composite_page = SimpleNamespace(_viewer3d_active_camera_id="cam1")
        viewer._transform_gizmo = MagicMock()
        viewer._gizmo_visible = True
        viewer._gizmo_drag_active = True
        viewer.update = MagicMock()

        viewer._update_gizmo_visibility()

        assert viewer._gizmo_visible is False
        assert viewer._gizmo_drag_active is False
        viewer._transform_gizmo.end_drag.assert_called_once()

    def test_non_active_camera_gizmo_still_shows(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._view_mode = "3d"
        viewer._selected_node_type = "camera3d"
        viewer._selected_node_params = {
            "camera_mode": "free",
            "cam_eye_x": 1.0,
            "cam_eye_y": 2.0,
            "cam_eye_z": 3.0,
        }
        viewer._interaction_state = SimpleNamespace(selected_node_id="cam2")
        viewer._composite_page = SimpleNamespace(_viewer3d_active_camera_id="cam1")
        viewer._transform_gizmo = MagicMock()
        viewer._gizmo_visible = False
        viewer._gizmo_drag_active = False
        viewer.update = MagicMock()

        viewer._update_gizmo_visibility()

        assert viewer._gizmo_visible is True
        assert viewer._gizmo_obj_pos == (1.0, 2.0, 3.0)

    def test_deleting_active_camera_clears_viewer_state(self):
        page = _make_page()
        page._viewer3d_active_camera_id = "cam1"
        page._node_graph = SimpleNamespace(_viewer_state={
            "default_3d_scene": {
                "cam_orbit_yaw": 11.0,
                "cam_orbit_pitch": 22.0,
                "cam_dist": 7.0,
                "cam_target_x": 1.0,
                "cam_target_y": 2.0,
                "cam_target_z": 3.0,
            }
        })
        page._properties_panel = MagicMock()
        page._viewer.get_selected_node.return_value = "cam1"

        page._clear_deleted_node_viewer_state("cam1")

        assert page._viewer3d_active_camera_id is None
        page._viewer.sync_scene3d_params.assert_called_once_with({
            "cam_orbit_yaw": 11.0,
            "cam_orbit_pitch": 22.0,
            "cam_dist": 7.0,
            "cam_target_x": 1.0,
            "cam_target_y": 2.0,
            "cam_target_z": 3.0,
        })
        page._viewer.set_selected_node.assert_called_with(None)
        page._properties_panel.set_active_camera_id.assert_called_with("")

    def test_deleting_focused_scene3d_clears_3d_context(self):
        page = _make_page()
        page._viewer3d_node_id = "scene1"
        page._last_selected_node_id = "scene1"
        page._properties_panel = MagicMock()
        page._viewer.get_selected_node.return_value = None

        page._clear_deleted_node_viewer_state("scene1")

        assert page._viewer3d_node_id is None
        assert page._last_selected_node_id is None
        page._viewer.update.assert_called()

    def test_deleting_selected_3d_object_clears_viewer_selection(self):
        page = _make_page()
        page._viewer.get_selected_node.return_value = "light1"
        page._properties_panel = MagicMock()

        page._clear_deleted_node_viewer_state("light1")

        page._viewer.set_selected_node.assert_called_once_with(None)
        page._viewer.set_node_info.assert_called_once_with(
            None, {}, None, node_id=None)

    def test_inject_viewer3d_scene_connects_renderable_3d_sources(self):
        from src.features.composite.core.graph_snapshot import (
            GraphSnapshot, NodeSnapshot)

        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"
        page._viewer._nav_params_dict.return_value = {
            "cam_orbit_yaw": 12.0,
            "cam_orbit_pitch": 34.0,
            "cam_dist": 6.0,
            "cam_target_x": 1.0,
            "cam_target_y": 2.0,
            "cam_target_z": 3.0,
        }
        snapshot = GraphSnapshot(
            generation=1,
            nodes={
                "cam1": NodeSnapshot(
                    node_id="cam1", node_type="camera3d", parameters={},
                    disabled=False, input_ports=[], output_ports=[("display", "scene")]),
                "light1": NodeSnapshot(
                    node_id="light1", node_type="light3d", parameters={},
                    disabled=False, input_ports=[], output_ports=[("display", "scene")]),
                "geo1": NodeSnapshot(
                    node_id="geo1", node_type="geo3d", parameters={},
                    disabled=False, input_ports=[], output_ports=[("display", "scene")]),
                "blur1": NodeSnapshot(
                    node_id="blur1", node_type="blur", parameters={},
                    disabled=False, input_ports=[], output_ports=[("result", "image")]),
            },
            eval_order=["cam1", "light1", "geo1", "blur1"],
        )

        snapshot = page._inject_viewer3d_scene_snapshot(snapshot)

        virtual_id = page._VIEWER3D_VIRTUAL_NODE_ID
        assert snapshot.nodes[virtual_id].parameters["primitive"] == "none"
        assert snapshot.nodes[virtual_id].parameters["cam_orbit_yaw"] == 12.0
        assert snapshot.eval_order[-1] == virtual_id
        assert {
            c.src_node_id
            for c in snapshot.connections
            if c.dst_node_id == virtual_id
        } == {"light1", "geo1"}

    def test_inject_viewer3d_scene_does_not_auto_look_through_camera(self):
        from src.features.composite.core.graph_snapshot import (
            GraphSnapshot, NodeSnapshot)

        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"
        page._viewer._nav_params_dict.return_value = {
            "cam_orbit_yaw": 12.0,
            "cam_orbit_pitch": 34.0,
            "cam_dist": 6.0,
        }
        snapshot = GraphSnapshot(
            generation=1,
            nodes={
                "cam1": NodeSnapshot(
                    node_id="cam1", node_type="camera3d", parameters={
                        "cam_orbit_yaw": 90.0,
                        "cam_orbit_pitch": 0.0,
                        "cam_dist": 20.0,
                    },
                    disabled=False, input_ports=[], output_ports=[("display", "scene")]),
            },
            eval_order=["cam1"],
        )

        snapshot = page._inject_viewer3d_scene_snapshot(snapshot)

        virtual_id = page._VIEWER3D_VIRTUAL_NODE_ID
        assert not any(
            c.src_node_id == "cam1" and c.dst_node_id == virtual_id
            for c in snapshot.connections
        )
        params = snapshot.nodes[virtual_id].parameters
        assert params["cam_orbit_yaw"] == 12.0
        assert params["cam_orbit_pitch"] == 34.0
        assert params["cam_dist"] == 6.0

    def test_inject_viewer3d_scene_applies_default_scene_settings(self):
        from src.features.composite.core.graph_snapshot import GraphSnapshot

        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"
        page._viewer._nav_params_dict.return_value = {}
        page._node_graph = SimpleNamespace(
            _viewer_state={
                "default_3d_scene": {
                    "show_grid": False,
                    "show_axes": False,
                    "bg_r": 0.25,
                    "bg_g": 0.5,
                    "bg_b": 0.75,
                    "msaa": 2,
                    "light_intensity": 4.5,
                }
            }
        )
        snapshot = GraphSnapshot(generation=1, nodes={}, eval_order=[])

        snapshot = page._inject_viewer3d_scene_snapshot(snapshot)

        params = snapshot.nodes[page._VIEWER3D_VIRTUAL_NODE_ID].parameters
        assert params["primitive"] == "none"
        assert params["show_grid"] is False
        assert params["show_axes"] is False
        assert params["bg_r"] == 0.25
        assert params["bg_g"] == 0.5
        assert params["bg_b"] == 0.75
        assert params["msaa"] == 2
        assert params["light_intensity"] == 4.5

    def test_inject_viewer3d_scene_uses_viewport_content_size(self):
        from src.features.composite.core.graph_snapshot import GraphSnapshot

        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"
        page._viewer._nav_params_dict.return_value = {}
        page._viewer.viewer3d_content_size.return_value = (1600, 900)
        page._node_graph = SimpleNamespace(_viewer_state={})
        snapshot = GraphSnapshot(
            generation=1,
            nodes={},
            eval_order=[],
        )

        snapshot = page._inject_viewer3d_scene_snapshot(snapshot)

        params = snapshot.nodes[page._VIEWER3D_VIRTUAL_NODE_ID].parameters
        assert params["width"] == 1600
        assert params["height"] == 900

    def test_viewer3d_content_size_change_forces_refresh(self):
        page = _make_page()
        page._viewer.get_view_mode.return_value = "3d"
        page._evaluate_graph = MagicMock()
        page._force_next_view_refresh = False

        page._on_viewer3d_content_size_changed(1600, 900)

        assert page._force_next_view_refresh is True
        page._evaluate_graph.assert_called_once()

    def test_new_camera_suggestion_is_visible_not_current_view_eye(self):
        from src.features.composite.render3d import math_utils as m3d
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._cam_target_x = 0.0
        viewer._cam_target_y = 0.0
        viewer._cam_target_z = 0.0
        viewer._cam_dist = 5.0
        viewer._orbit_yaw = 30.0
        viewer._orbit_pitch = 20.0

        params = viewer.suggested_camera3d_params_for_new_node()
        viewer_eye = m3d.orbit_to_eye(
            5.0, 30.0, 20.0, target=m3d.vec3(0.0, 0.0, 0.0))
        cam_eye = (
            params["cam_eye_x"],
            params["cam_eye_y"],
            params["cam_eye_z"],
        )

        assert params["camera_mode"] == "free"
        assert cam_eye != pytest.approx(tuple(float(v) for v in viewer_eye))

    def test_page_initializes_new_camera_from_viewer_suggestion(self):
        from src.features.composite.ui.composite_page import CompositePage

        page = CompositePage.__new__(CompositePage)
        page._viewer = SimpleNamespace(
            suggested_camera3d_params_for_new_node=lambda: {
                "camera_mode": "free",
                "cam_eye_x": 1.25,
            }
        )
        written = {}
        node = SimpleNamespace(
            node_type="camera3d",
            set_parameter=lambda key, value: written.__setitem__(key, value)
        )

        page._initialize_new_3d_helper_node(node)

        assert written == {"camera_mode": "free", "cam_eye_x": 1.25}

    def test_new_light_suggestion_places_icon_and_light_at_origin(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._cam_target_x = 0.0
        viewer._cam_target_y = 0.0
        viewer._cam_target_z = 0.0
        viewer._cam_dist = 5.0
        viewer._orbit_yaw = 30.0
        viewer._orbit_pitch = 20.0

        params = viewer.suggested_light3d_params_for_new_node()
        icon_pos = (
            params["icon_pos_x"],
            params["icon_pos_y"],
            params["icon_pos_z"],
        )
        light_pos = (
            params["light_pos_x"],
            params["light_pos_y"],
            params["light_pos_z"],
        )

        assert icon_pos == pytest.approx(light_pos)
        assert icon_pos == pytest.approx((0.0, 0.0, 0.0))

    def test_page_initializes_new_light_from_viewer_suggestion(self):
        from src.features.composite.ui.composite_page import CompositePage

        page = CompositePage.__new__(CompositePage)
        page._viewer = SimpleNamespace(
            suggested_light3d_params_for_new_node=lambda: {
                "icon_pos_x": 2.5,
                "light_pos_x": 2.5,
            }
        )
        written = {}
        node = SimpleNamespace(
            node_type="light3d",
            set_parameter=lambda key, value: written.__setitem__(key, value)
        )

        page._initialize_new_3d_helper_node(node)

        assert written == {"icon_pos_x": 2.5, "light_pos_x": 2.5}

    def test_light_gizmo_maps_directional_move_to_icon_position(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._selected_node_type = "light3d"
        viewer._selected_node_params = {"light_type": "directional"}
        viewer._gizmo_drag_source_params = {
            "light_type": "directional",
            "icon_pos_x": 1.0,
            "icon_pos_y": 2.0,
            "icon_pos_z": 3.0,
        }
        viewer._gizmo_drag_anchor_start = (1.0, 2.0, 3.0)

        result = viewer._light_gizmo_result_to_params({"object_pos_x": 4.0})

        assert result == {
            "icon_pos_x": 4.0,
            "icon_pos_y": 2.0,
            "icon_pos_z": 3.0,
        }

    def test_light_gizmo_maps_point_move_to_light_position(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._selected_node_type = "light3d"
        viewer._selected_node_params = {"light_type": "point"}
        viewer._gizmo_drag_source_params = {
            "light_type": "point",
            "light_pos_x": 1.0,
            "light_pos_y": 2.0,
            "light_pos_z": 3.0,
        }
        viewer._gizmo_drag_anchor_start = (1.0, 2.0, 3.0)

        result = viewer._light_gizmo_result_to_params({"object_pos_z": 6.0})

        assert result == {
            "light_pos_x": 1.0,
            "light_pos_y": 2.0,
            "light_pos_z": 6.0,
        }

    def test_selected_camera_fov_does_not_drive_default_viewer_projection(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._tex_width = 1920
        viewer._tex_height = 1080
        viewer._selected_node_type = "camera3d"
        viewer._selected_node_params = {"cam_fov": 120.0, "width": 3840}
        graph = SimpleNamespace(
            _viewer_state={"default_3d_scene": {"cam_fov": 50.0}},
        )
        page = SimpleNamespace(
            _viewer3d_active_camera_id=None,
            _node_graph=graph,
        )
        viewer._composite_page = page

        fov, width, height = viewer._viewer3d_projection_params()

        assert fov == 50.0
        assert (width, height) == (1920, 1080)

    def test_active_camera_fov_drives_viewer_projection(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._tex_width = 1920
        viewer._tex_height = 1080
        active_camera = SimpleNamespace(
            node_type="camera3d",
            parameters={"cam_fov": 85.0, "width": 2048, "height": 858},
        )
        graph = SimpleNamespace(
            _viewer_state={"default_3d_scene": {"cam_fov": 50.0}},
            get_node=lambda node_id: active_camera if node_id == "cam1" else None,
        )
        page = SimpleNamespace(
            _viewer3d_active_camera_id="cam1",
            _node_graph=graph,
        )
        viewer._composite_page = page

        fov, width, height = viewer._viewer3d_projection_params()

        assert fov == 85.0
        assert (width, height) == (2048, 858)

    def test_active_camera_nav_targets_camera_node(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        cam = SimpleNamespace(node_type="camera3d")
        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._composite_page = SimpleNamespace(
            _viewer3d_active_camera_id="cam1",
            _VIEWER3D_VIRTUAL_NODE_ID="__viewer3d_scene__",
            _node_graph=SimpleNamespace(
                get_node=lambda nid: cam if nid == "cam1" else None),
        )

        assert viewer._nav_target_node_id() == "cam1"

    def test_stale_active_camera_nav_falls_back_to_default_viewer(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        page = SimpleNamespace(
            _viewer3d_active_camera_id="missing_cam",
            _VIEWER3D_VIRTUAL_NODE_ID="__viewer3d_scene__",
            _node_graph=SimpleNamespace(get_node=lambda nid: None),
        )
        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._composite_page = page

        assert viewer._nav_target_node_id() == "__viewer3d_scene__"
        assert page._viewer3d_active_camera_id is None

    def test_active_free_camera_nav_params_write_eye_and_target(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        cam = SimpleNamespace(
            node_type="camera3d",
            parameters={
                "camera_mode": "free",
                "cam_eye_x": 4.0,
                "cam_eye_y": 3.0,
                "cam_eye_z": 5.0,
                "cam_target_x": 0.0,
                "cam_target_y": 0.0,
                "cam_target_z": 0.0,
            },
        )
        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._cam_dist = 6.0
        viewer._orbit_yaw = 45.0
        viewer._orbit_pitch = 15.0
        viewer._cam_target_x = 1.0
        viewer._cam_target_y = 2.0
        viewer._cam_target_z = 3.0
        viewer._composite_page = SimpleNamespace(
            _viewer3d_active_camera_id="cam1",
            _node_graph=SimpleNamespace(get_node=lambda nid: cam if nid == "cam1" else None),
        )

        params = viewer._nav_params_dict()

        assert {"cam_eye_x", "cam_eye_y", "cam_eye_z",
                "cam_target_x", "cam_target_y", "cam_target_z"} <= set(params)
        assert "cam_orbit_yaw" not in params
        assert params["cam_target_x"] == 1.0

    def test_auto_orbit_center_records_selected_3d_object_without_recentering(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import Viewport3DState

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._viewport3d_ctrl = SimpleNamespace(state=Viewport3DState())
        viewer._selected_node_type = "geo3d"
        viewer._selected_node_params = {
            "object_pos_x": 2.0,
            "object_pos_y": 3.0,
            "object_pos_z": 4.0,
        }
        viewer._cam_target_x = 0.0
        viewer._cam_target_y = 0.0
        viewer._cam_target_z = 0.0
        viewer._cam_dist = 8.0
        viewer._orbit_yaw = 35.0
        viewer._orbit_pitch = 25.0

        viewer._prepare_orbit_center_for_drag()

        assert viewer._orbit_pivot_world == pytest.approx((2.0, 3.0, 4.0))
        assert (
            viewer._cam_target_x,
            viewer._cam_target_y,
            viewer._cam_target_z,
        ) == pytest.approx((0.0, 0.0, 0.0))

    def test_selected_pivot_orbit_preserves_off_center_view_target(self):
        import numpy as np
        from src.features.composite.render3d import math_utils as m3d
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import Viewport3DState

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._viewport3d_ctrl = SimpleNamespace(state=Viewport3DState())
        viewer._selected_node_type = "geo3d"
        viewer._selected_node_params = {
            "object_pos_x": 3.0,
            "object_pos_y": 0.0,
            "object_pos_z": 0.0,
        }
        viewer._cam_target_x = 1.0
        viewer._cam_target_y = 0.0
        viewer._cam_target_z = 0.0
        viewer._cam_dist = 8.0
        viewer._orbit_yaw = 20.0
        viewer._orbit_pitch = 15.0
        old_target = m3d.vec3(
            viewer._cam_target_x, viewer._cam_target_y, viewer._cam_target_z)
        old_pivot = m3d.vec3(3.0, 0.0, 0.0)
        old_eye = m3d.orbit_to_eye(
            viewer._cam_dist, viewer._orbit_yaw, viewer._orbit_pitch,
            target=old_target)

        viewer._prepare_orbit_center_for_drag()
        viewer._orbit_camera_around_pivot(24.0, -12.0)

        new_target = m3d.vec3(
            viewer._cam_target_x, viewer._cam_target_y, viewer._cam_target_z)
        new_eye = m3d.orbit_to_eye(
            viewer._cam_dist, viewer._orbit_yaw, viewer._orbit_pitch,
            target=new_target)
        assert tuple(new_target) != pytest.approx(tuple(old_pivot))
        assert np.linalg.norm(new_target - old_pivot) == pytest.approx(
            np.linalg.norm(old_target - old_pivot), abs=1e-5)
        assert np.linalg.norm(new_eye - old_pivot) == pytest.approx(
            np.linalg.norm(old_eye - old_pivot), abs=1e-5)

    def test_auto_orbit_center_after_manual_pan_uses_selected_pivot_without_recentering(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import Viewport3DState

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._viewport3d_ctrl = SimpleNamespace(state=Viewport3DState())
        viewer._selected_node_type = "geo3d"
        viewer._selected_node_params = {
            "object_pos_x": 9.0,
            "object_pos_y": 8.0,
            "object_pos_z": 7.0,
        }
        viewer._cam_target_x = 1.0
        viewer._cam_target_y = 2.0
        viewer._cam_target_z = 3.0
        viewer._orbit_auto_screen_center_after_pan = True

        viewer._prepare_orbit_center_for_drag()

        assert viewer._orbit_pivot_world == pytest.approx((9.0, 8.0, 7.0))
        assert (
            viewer._cam_target_x,
            viewer._cam_target_y,
            viewer._cam_target_z,
        ) == pytest.approx((1.0, 2.0, 3.0))

    def test_selecting_node_reenables_auto_orbit_selected_center(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import Viewport3DState

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._viewport3d_ctrl = SimpleNamespace(state=Viewport3DState())
        viewer._interaction_state = SimpleNamespace(selected_node_id=None)
        viewer._orbit_auto_screen_center_after_pan = True
        viewer._update_tool_handles = lambda: None
        viewer.update = lambda: None

        viewer.set_selected_node("geo1")

        assert viewer._orbit_auto_screen_center_after_pan is False

    def test_reselecting_same_node_preserves_screen_center_after_pan(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import Viewport3DState

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._viewport3d_ctrl = SimpleNamespace(state=Viewport3DState())
        viewer._interaction_state = SimpleNamespace(selected_node_id="geo1")
        viewer._selected_node_type = "geo3d"
        viewer._selected_node_params = {
            "object_pos_x": 9.0,
            "object_pos_y": 8.0,
            "object_pos_z": 7.0,
        }
        viewer._cam_target_x = 1.0
        viewer._cam_target_y = 2.0
        viewer._cam_target_z = 3.0
        viewer._cam_dist = 8.0
        viewer._orbit_yaw = 35.0
        viewer._orbit_pitch = 25.0
        viewer._orbit_auto_screen_center_after_pan = True
        viewer._update_tool_handles = lambda: None
        viewer.update = lambda: None

        viewer.set_selected_node("geo1")
        viewer._prepare_orbit_center_for_drag()

        assert viewer._orbit_auto_screen_center_after_pan is True
        assert viewer._orbit_pivot_world == pytest.approx((9.0, 8.0, 7.0))
        assert (
            viewer._cam_target_x,
            viewer._cam_target_y,
            viewer._cam_target_z,
        ) == pytest.approx((1.0, 2.0, 3.0))

    def test_screen_orbit_center_keeps_existing_view_target(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import (
            OrbitCenterMode, Viewport3DState)

        viewer = CompositeViewer.__new__(CompositeViewer)
        state = Viewport3DState()
        state.orbit_center_mode = OrbitCenterMode.SCREEN
        viewer._viewport3d_ctrl = SimpleNamespace(state=state)
        viewer._selected_node_type = "geo3d"
        viewer._selected_node_params = {
            "object_pos_x": 9.0,
            "object_pos_y": 8.0,
            "object_pos_z": 7.0,
        }
        viewer._cam_target_x = 1.0
        viewer._cam_target_y = 2.0
        viewer._cam_target_z = 3.0

        viewer._prepare_orbit_center_for_drag()

        assert viewer._orbit_pivot_world is None
        assert (
            viewer._cam_target_x,
            viewer._cam_target_y,
            viewer._cam_target_z,
        ) == pytest.approx((1.0, 2.0, 3.0))

    def test_auto_orbit_center_ignores_scene3d_renderer_selection(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import Viewport3DState

        viewer = CompositeViewer.__new__(CompositeViewer)
        viewer._viewport3d_ctrl = SimpleNamespace(state=Viewport3DState())
        viewer._selected_node_type = "scene3d"
        viewer._selected_node_params = {}
        viewer._cam_target_x = 1.0
        viewer._cam_target_y = 2.0
        viewer._cam_target_z = 3.0

        viewer._prepare_orbit_center_for_drag()

        assert viewer._orbit_pivot_world is None
        assert (
            viewer._cam_target_x,
            viewer._cam_target_y,
            viewer._cam_target_z,
        ) == pytest.approx((1.0, 2.0, 3.0))

    def test_cursor_orbit_center_uses_custom_point(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        from src.features.composite.ui.viewport3d_state import (
            OrbitCenterMode, Viewport3DState)

        viewer = CompositeViewer.__new__(CompositeViewer)
        state = Viewport3DState()
        state.orbit_center_mode = OrbitCenterMode.CURSOR
        state.orbit_cursor_world = (-1.0, 2.5, 6.0)
        viewer._viewport3d_ctrl = SimpleNamespace(state=state)
        viewer._selected_node_type = None
        viewer._selected_node_params = {}
        viewer._cam_target_x = 0.0
        viewer._cam_target_y = 0.0
        viewer._cam_target_z = 0.0
        viewer._cam_dist = 10.0
        viewer._orbit_yaw = 30.0
        viewer._orbit_pitch = 20.0

        viewer._prepare_orbit_center_for_drag()

        assert viewer._orbit_pivot_world == pytest.approx((-1.0, 2.5, 6.0))
        assert (
            viewer._cam_target_x,
            viewer._cam_target_y,
            viewer._cam_target_z,
        ) == pytest.approx((0.0, 0.0, 0.0))


class TestCameraIconOverlayGeometry:
    """Camera3D viewport icon should react to camera parameters."""

    def _renderer_basis(self):
        from src.features.composite.render3d.node_icon_overlay import (
            ICON_PX_SIZE, NodeIconRenderer)

        renderer = NodeIconRenderer()
        renderer.update(
            proj=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            view=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            tex_w_px=1000,
            tex_h_px=1000,
            cam_dist=5.0,
            cam_fov=50.0,
        )
        import numpy as np

        eye = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        forward = np.array([0.0, 0.0, -1.0], dtype=np.float32)
        right = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        size = renderer._world_per_tex_px() * ICON_PX_SIZE
        return renderer, eye, forward, right, up, size

    def _far_rect_width(self, fov):
        import numpy as np

        renderer, eye, forward, right, up, size = self._renderer_basis()
        groups = renderer._camera_frustum_geometry(
            eye, forward, right, up, size, (0.2, 0.7, 1.0),
            fov=fov, aspect=1.0, camera_mode="free", ortho_scale=5.0,
            shift_x=0.0, shift_y=0.0, focus_distance=5.0,
            dof_enable=False)
        far_rect = groups[1][0]
        return float(np.linalg.norm(np.asarray(far_rect[1]) - np.asarray(far_rect[0])))

    def test_camera_icon_fov_opens_frustum(self):
        narrow = self._far_rect_width(28.0)
        wide = self._far_rect_width(82.0)

        assert wide > narrow * 2.0

    def test_camera_icon_lens_shift_offsets_far_plane(self):
        import numpy as np

        renderer, eye, forward, right, up, size = self._renderer_basis()
        centered = renderer._camera_frustum_geometry(
            eye, forward, right, up, size, (0.2, 0.7, 1.0),
            fov=50.0, aspect=1.0, camera_mode="free", ortho_scale=5.0,
            shift_x=0.0, shift_y=0.0, focus_distance=5.0,
            dof_enable=False)
        shifted = renderer._camera_frustum_geometry(
            eye, forward, right, up, size, (0.2, 0.7, 1.0),
            fov=50.0, aspect=1.0, camera_mode="free", ortho_scale=5.0,
            shift_x=0.6, shift_y=-0.4, focus_distance=5.0,
            dof_enable=False)

        centered_center = np.mean(np.asarray(centered[1][0]), axis=0)
        shifted_center = np.mean(np.asarray(shifted[1][0]), axis=0)

        assert shifted_center[0] > centered_center[0]
        assert shifted_center[1] < centered_center[1]

    def test_camera_icon_focus_distance_moves_focus_plane(self):
        import numpy as np

        renderer, eye, forward, right, up, size = self._renderer_basis()
        near_focus = renderer._camera_frustum_geometry(
            eye, forward, right, up, size, (0.2, 0.7, 1.0),
            fov=50.0, aspect=1.0, camera_mode="free", ortho_scale=5.0,
            shift_x=0.0, shift_y=0.0, focus_distance=1.0,
            dof_enable=True)
        far_focus = renderer._camera_frustum_geometry(
            eye, forward, right, up, size, (0.2, 0.7, 1.0),
            fov=50.0, aspect=1.0, camera_mode="free", ortho_scale=5.0,
            shift_x=0.0, shift_y=0.0, focus_distance=18.0,
            dof_enable=True)

        near_focus_center = np.mean(np.asarray(near_focus[-2][0]), axis=0)
        far_focus_center = np.mean(np.asarray(far_focus[-2][0]), axis=0)

        assert far_focus_center[2] < near_focus_center[2]


class TestViewer3DGizmoPreview:
    """Viewport gizmo previews should update the 3D scene without debounce lag."""

    def test_geo3d_transform_preview_evaluates_immediately(self):
        page = _make_page()
        geo = _PreviewNode("geo3d")
        page._node_graph = SimpleNamespace(get_node=lambda nid: geo if nid == "geo1" else None)

        page._on_viewer3d_nav_preview("geo1", {"object_pos_x": 2.0})

        assert geo.parameters["object_pos_x"] == 2.0
        page._do_evaluate_graph.assert_called_once()
        page._eval_debounce_timer.start.assert_not_called()
        assert page._force_next_view_refresh is True

    def test_geo3d_transform_preview_uses_graph_setter(self):
        page = _make_page()
        geo = _PreviewNode("geo3d")
        calls = []

        def set_node_parameter(nid, key, value):
            calls.append((nid, key, value))
            geo.set_parameter(key, value)
            return True

        page._node_graph = SimpleNamespace(
            get_node=lambda nid: geo if nid == "geo1" else None,
            set_node_parameter=set_node_parameter,
        )

        page._on_viewer3d_nav_preview("geo1", {"object_pos_x": 2.0})

        assert calls == [("geo1", "object_pos_x", 2.0)]
        assert geo.parameters["object_pos_x"] == 2.0

    def test_camera_transform_preview_evaluates_immediately(self):
        page = _make_page()
        cam = _PreviewNode("camera3d")
        page._node_graph = SimpleNamespace(get_node=lambda nid: cam if nid == "cam1" else None)

        page._on_viewer3d_nav_preview("cam1", {"cam_eye_x": 6.0})

        assert cam.parameters["cam_eye_x"] == 6.0
        page._do_evaluate_graph.assert_called_once()
        page._eval_debounce_timer.start.assert_not_called()

    def test_non_3d_preview_keeps_debounced_evaluation(self):
        page = _make_page()
        blur = _PreviewNode("blur")
        page._eval_debounce_timer.isActive.return_value = False
        page._node_graph = SimpleNamespace(get_node=lambda nid: blur if nid == "b1" else None)

        page._on_viewer3d_nav_preview("b1", {"size": 20.0})

        assert blur.parameters["size"] == 20.0
        page._do_evaluate_graph.assert_not_called()
        page._eval_debounce_timer.start.assert_called_once()

    def test_properties_scrub_geo3d_transform_evaluates_immediately(self):
        page = _make_page()
        geo = _PreviewNode("geo3d")
        page._user_edit_fired = False
        page._current_composite_id = None
        page._pending_param_change = None
        page._prefetch_enabled = False
        page._param_debounce_timer = MagicMock()
        page._undo_stack = MagicMock()
        page.update_viewer_handles = MagicMock()

        def _set_node_parameter(_node_id, key, value):
            geo.set_parameter(key, value)

        page._node_graph = SimpleNamespace(
            get_node=lambda nid: geo if nid == "geo1" else None,
            set_node_parameter=_set_node_parameter,
        )

        page._on_parameter_changed("geo1", "object_pos_x", 3.0)

        assert geo.parameters["object_pos_x"] == 3.0
        assert page._force_next_view_refresh is True
        page._do_evaluate_graph.assert_called_once()
        page._eval_debounce_timer.start.assert_not_called()
        page._param_debounce_timer.start.assert_called_once()
        page.update_viewer_handles.assert_called_once()

    def test_active_camera_property_scrub_syncs_live_viewer_camera(self):
        page = _make_page()
        cam = _PreviewNode("camera3d")
        page._viewer3d_active_camera_id = "cam1"
        page._user_edit_fired = False
        page._current_composite_id = None
        page._pending_param_change = None
        page._prefetch_enabled = False
        page._param_debounce_timer = MagicMock()
        page._undo_stack = MagicMock()
        page.update_viewer_handles = MagicMock()

        def _set_node_parameter(_node_id, key, value):
            cam.set_parameter(key, value)

        page._node_graph = SimpleNamespace(
            get_node=lambda nid: cam if nid == "cam1" else None,
            set_node_parameter=_set_node_parameter,
        )

        page._on_parameter_changed("cam1", "cam_fov", 72.0)

        page._viewer.look_through_camera_params.assert_called_once()
        synced_params = page._viewer.look_through_camera_params.call_args.args[0]
        assert synced_params["cam_fov"] == 72.0

    def test_camera_optics_preview_evaluates_immediately(self):
        page = _make_page()
        cam = _PreviewNode("camera3d")
        page._node_graph = SimpleNamespace(get_node=lambda nid: cam if nid == "cam1" else None)

        page._on_viewer3d_nav_preview("cam1", {"cam_fov": 72.0})

        assert cam.parameters["cam_fov"] == 72.0
        page._do_evaluate_graph.assert_called_once()
        page._eval_debounce_timer.start.assert_not_called()

    def test_light_parameter_preview_evaluates_immediately(self):
        page = _make_page()
        light = _PreviewNode("light3d")
        page._node_graph = SimpleNamespace(get_node=lambda nid: light if nid == "light1" else None)

        page._on_viewer3d_nav_preview("light1", {"intensity": 4.5})

        assert light.parameters["intensity"] == 4.5
        page._do_evaluate_graph.assert_called_once()
        page._eval_debounce_timer.start.assert_not_called()
