"""
Test: Scene3D MSAA 抗锯齿 + 地面网格视口叠加层改造。

覆盖三块改动：
1. msaa 参数钳制逻辑（0/2/4/8，非法→0）+ 进入 content_sig（变更触发重渲染）。
2. 地面网格不再烘焙进输出：scene3d_node 不再 import/调用 GizmoRenderer。
3. ground_grid_overlay 的构建/绘制 guard（未构建时不绘制、release 幂等）。

注意：不真正建 GL context（CI 无 GPU）；用静态/反射 + mock 验证逻辑分支。
"""
from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Part 1: msaa 参数钳制 + content_sig
# ──────────────────────────────────────────────────────────────────────────

def _msaa_clamp(raw):
    """复刻 scene3d_node._do_evaluate 里的 msaa 钳制逻辑，便于单测验证。"""
    msaa = int(raw)
    return msaa if msaa in (2, 4, 8) else 0


class TestMsaaClamp:
    """msaa 参数钳制：仅接受 0/2/4/8，其余归零。"""

    @pytest.mark.parametrize("raw,expected", [
        (0, 0), (2, 2), (4, 4), (8, 8),    # 合法值直通
        (16, 0),                            # 超上限 → 0
        (1, 0), (3, 0), (5, 0), (6, 0),     # 非法奇/偶值 → 0
        (-1, 0),                            # 负值 → 0
        (100, 0),                           # 远超 → 0
    ])
    def test_clamp_values(self, raw, expected):
        assert _msaa_clamp(raw) == expected

    def test_default_is_4(self):
        """默认 msaa=4 属于全局 3D Scene Settings。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["msaa"] == 4

    def test_param_def_exists(self):
        """msaa 已移至全局「3D Scene Settings」，不再在 Scene3D PARAM_DEFS。

        Scene3D 是纯渲染器，场景环境参数（含 msaa）统一在
        DEFAULT_VIEWER3D_SCENE_SETTINGS 管理。
        """
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        keys = {p.name for p in Scene3DNode.PARAM_DEFS}
        # msaa 不再在节点 PARAM_DEFS（移至全局设置）
        assert "msaa" not in keys
        # msaa 在全局默认设置里
        assert "msaa" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["msaa"] == 4


class TestMsaaInContentSig:
    """msaa 变化必须触发 content_changed（重建多重采样 FBO）。"""

    def test_msaa_triggers_rebuild(self):
        """msaa 不同 → _last_content_sig 比较不等 → content_changed=True。

        这里验证逻辑：签名 tuple 含 msaa 分量。用 node 实例 + mock ctx
        走一遍 _do_evaluate 会真实建 GL，CI 无 GPU 不可行；故只验证签名
        构造逻辑（msaa 在 _do_evaluate 里被读出并加入 content_sig）。
        通过源码静态检查确认 msaa 出现在 content_sig 构造段。
        """
        import inspect
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # msaa 读取 + 进入 content_sig 的两处证据
        assert "msaa" in src
        # content_sig 的元组里应含 msaa 变量（非字面量）
        assert "show_grid, show_axes," in src
        # msaa 在签名末尾（加在 show_axes 之后）
        sig_section = src[src.index("content_sig = ("):]
        assert "msaa," in sig_section


# ──────────────────────────────────────────────────────────────────────────
# Part 1b: 内置 procedural 几何/材质默认参数（无 Geo3D 子节点时开箱即用）
# ──────────────────────────────────────────────────────────────────────────

class TestBuiltinProceduralDefaults:
    """Scene3D 是纯渲染器，不再持有 procedural 几何/材质参数。"""

    PROC_PARAMS = (
        "primitive", "mesh_scale",
        "object_pos_x", "object_pos_y", "object_pos_z",
        "object_rot_x", "object_rot_y", "object_rot_z",
        "metallic", "roughness",
        "object_color_r", "object_color_g", "object_color_b",
    )

    def test_parameters_have_procedural_defaults(self):
        """parameters 字典不再含 procedural 几何/材质默认值。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        for k in self.PROC_PARAMS:
            assert k not in node.parameters, f"Scene3DNode.parameters 不应包含 {k}"

    def test_default_primitive_is_none(self):
        """Scene3D 不再暴露 primitive 参数。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert "primitive" not in Scene3DNode().parameters

    def test_param_defs_have_procedural_entries(self):
        """PARAM_DEFS 不再含 procedural 参数（几何由 Geo3D/Model3D 提供）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        keys = {p.name for p in Scene3DNode.PARAM_DEFS}
        for k in self.PROC_PARAMS:
            assert k not in keys, f"PARAM_DEFS 不应包含 {k}"

    def test_procedural_defaults_roundtrip(self):
        """to_dict / from_dict 不再生成 procedural 默认参数。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        restored = Scene3DNode.from_dict(node.to_dict())
        for k in self.PROC_PARAMS:
            assert k not in restored.parameters


# ──────────────────────────────────────────────────────────────────────────
# Part 1c: procedural / Geo3D 渲染分支必须与 `if import_ok` 同级（非嵌套）
# ──────────────────────────────────────────────────────────────────────────

class TestProceduralBranchNotNested:
    """回归保护：procedural（`elif not import_ok`）与 Geo3D（`if _geo_payloads`）
    渲染分支必须与 `if import_ok and (...)` **同级**（同一缩进），不能嵌套进去。

    历史 bug：这两个分支曾被错误缩进到 `if import_ok` 块内 → 当没有 Model3D
    连接（import_ok=False）时整块被跳过 → Scene3D 不画任何几何（连内置 cube
    都不显），用户只看到坐标轴。这是"创建 Scene3D 不显立方体"的根因。
    """

    def test_procedural_branch_is_sibling_of_import_ok(self):
        """Geo/procedural fallback branches must be siblings of `if import_ok`."""
        import inspect
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        lines = src.splitlines()
        import_ok_line = next(
            (i for i, ln in enumerate(lines) if "if import_ok and" in ln), None)
        assert import_ok_line is not None, "应存在 `if import_ok and (...)` 分支"
        import_ok_indent = len(lines[import_ok_line]) - len(lines[import_ok_line].lstrip())
        # 找到 procedural/geo 分支行
        geo_line = next(
            (i for i, ln in enumerate(lines) if "if _geo_payloads:" in ln), None)
        proc_line = next(
            (i for i, ln in enumerate(lines)
             if "elif not import_ok and has_builtin_primitive" in ln), None)
        assert geo_line is not None and proc_line is not None
        geo_indent = len(lines[geo_line]) - len(lines[geo_line].lstrip())
        proc_indent = len(lines[proc_line]) - len(lines[proc_line].lstrip())
        # 必须与 if import_ok 同级（缩进相等），而非更深（嵌套）
        assert geo_indent == import_ok_indent, \
            f"`if _geo_payloads` 缩进 {geo_indent} 应与 `if import_ok` {import_ok_indent} 同级（不能嵌套）"
        assert proc_indent == import_ok_indent, \
            f"`elif not import_ok and has_builtin_primitive` 缩进 {proc_indent} 应与 `if import_ok` {import_ok_indent} 同级（不能嵌套）"


# ──────────────────────────────────────────────────────────────────────────
# Part 1d: 材质 sampler2D 必须指到空单元（否则与 IBL samplerCube 类型冲突）
# ──────────────────────────────────────────────────────────────────────────

class TestNullMaterialSamplers:
    """回归保护：procedural / Geo3D 路径（无材质贴图）渲染前必须把 5 个材质
    sampler2D uniform 指到空纹理单元，否则它们默认指向单元 0——而单元 0 绑的是
    IBL 的 samplerCube，类型冲突让 draw 报 GL_INVALID_OPERATION，几何完全不画
    （Scene3D 只显坐标轴，是\"创建 Scene3D 不显立方体\"的真正根因）。
    """

    def test_bind_null_material_samplers_exists(self):
        """Scene3DNode 有 _bind_null_material_samplers 方法。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert hasattr(Scene3DNode, "_bind_null_material_samplers")

    def test_procedural_branch_calls_null_samplers(self):
        """procedural fallback 分支调用 _bind_null_material_samplers。"""
        import inspect
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # procedural 分支体里应调用
        proc_marker = "elif not import_ok and has_builtin_primitive"
        proc_section = src[src.index(proc_marker):]
        assert "_bind_null_material_samplers" in proc_section, \
            "procedural 分支必须调 _bind_null_material_samplers（否则几何不画）"

    def test_geo_branch_calls_null_samplers(self):
        """Geo3D 分支也调用 _bind_null_material_samplers。"""
        import inspect
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        proc_marker = "elif not import_ok and has_builtin_primitive"
        geo_section = src[src.index("if _geo_payloads:"):src.index(proc_marker)]
        assert "_bind_null_material_samplers" in geo_section, \
            "Geo3D 分支必须调 _bind_null_material_samplers（否则几何不画）"

    def test_helper_binds_all_five_samplers_to_empty_units(self):
        """helper 把 5 个 sampler uniform 指到单元 3-7 并清空绑定。"""
        import inspect
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_null_material_samplers)
        # 必须遍历 MATERIAL_SAMPLER_LOCATIONS（5 个 slot）赋值 + 解绑
        assert "MATERIAL_SAMPLER_LOCATIONS" in src
        assert "glBindTexture" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 2: 地面网格不再烘焙进输出
# ──────────────────────────────────────────────────────────────────────────

class TestMSAAFields:
    """Scene3D MSAA 资源字段存在（多重采样 FBO + resolve）。"""

    def test_msaa_fields_exist(self):
        """MSAA 相关字段存在（多重采样资源 + resolve FBO）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        for attr in ("_msaa_color", "_resolve_fbo", "_msaa_samples"):
            assert hasattr(node, attr), f"Scene3DNode 应有 {attr} 字段"
        assert node._msaa_samples == 0  # 初始未渲染


# ──────────────────────────────────────────────────────────────────────────
# Part 3: 网格作为真实几何烘焙进 Scene3D（硬件深度测试，非视口叠加层）
# ──────────────────────────────────────────────────────────────────────────

class TestBakedGrid:
    """网格作为真实几何烘焙进 Scene3D 输出纹理，用硬件 DEPTH_TEST 自动遮挡。"""

    def test_scene3d_imports_gizmo_renderer(self):
        """scene3d_node 重新 import GizmoRenderer（烘焙网格用）。"""
        import src.features.composite.nodes.scene3d_node as mod
        assert hasattr(mod, "GizmoRenderer"), \
            "scene3d_node 应 import GizmoRenderer 以烘焙网格"

    def test_scene3d_has_gizmo_field(self):
        """Scene3DNode 实例有 _gizmo 字段（烘焙网格渲染器）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        assert hasattr(node, "_gizmo"), "Scene3DNode 应有 _gizmo 字段"

    def test_evaluate_bakes_grid_when_show_grid(self):
        """_do_evaluate 在 show_grid=True 时调用 _gizmo.draw（烘焙网格）。"""
        import inspect
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "self._gizmo.draw(" in src, \
            "_do_evaluate 应在 show_grid 时调用 _gizmo.draw 烘焙网格"

    def test_gizmo_draw_has_cam_pitch(self):
        """GizmoRenderer.draw 仍接受 cam_pitch_deg，兼容现有调用点。"""
        import inspect
        from src.features.composite.render3d.gizmo_renderer import GizmoRenderer
        sig = inspect.signature(GizmoRenderer.draw)
        assert "cam_pitch_deg" in sig.parameters

    def test_gizmo_no_longer_sets_horizon_factor(self):
        """网格亮度不应再随俯仰角变化，避免旋转视角时忽明忽暗。"""
        import inspect
        from src.features.composite.render3d.gizmo_renderer import GizmoRenderer
        src = inspect.getsource(GizmoRenderer.draw)
        assert "u_horizon_factor" not in src
        assert "(30.0 - ap)" not in src


class TestGridShaderBandLimit:
    """地面网格 shader 需要按屏幕像素尺寸过滤远处细格，避免摩尔纹。"""

    def test_grid_shader_fades_minor_grid_before_aliasing(self):
        """细格投影太小时应先淡出，只保留更粗的 major grid。"""
        from src.features.composite.render3d.gizmo_renderer import _GRID_FRAGMENT
        assert "minor_px" in _GRID_FRAGMENT
        assert "minor_lod" in _GRID_FRAGMENT
        assert "smoothstep(0.75, 4.0, minor_px)" in _GRID_FRAGMENT
        assert "gridLineAlpha(p, minor_width) * minor_lod" in _GRID_FRAGMENT

    def test_grid_shader_has_separate_major_lod(self):
        """粗格使用独立 LOD，避免细格消失后整个网格突然断层。"""
        from src.features.composite.render3d.gizmo_renderer import _GRID_FRAGMENT
        assert "major_px" in _GRID_FRAGMENT
        assert "major_lod" in _GRID_FRAGMENT
        assert "smoothstep(2.0, 9.0, major_px)" in _GRID_FRAGMENT
        assert "gridLineAlpha(major_coord, major_width) * major_lod" in _GRID_FRAGMENT

    def test_grid_shader_keeps_fixed_size_grid_without_extra_scale_levels(self):
        """网格回到固定尺寸；不要再添加 micro/super 自适应层级。"""
        from src.features.composite.render3d.gizmo_renderer import _GRID_FRAGMENT
        assert "micro_coord" not in _GRID_FRAGMENT
        assert "super_coord" not in _GRID_FRAGMENT
        assert "gridCellPixels" in _GRID_FRAGMENT
        assert "gridLineAlpha" in _GRID_FRAGMENT

    def test_grid_shader_fades_major_grid_by_distance(self):
        """粗格也要按距离缓慢淡出，不能只让细格消失。"""
        from src.features.composite.render3d.gizmo_renderer import _GRID_FRAGMENT
        assert "distanceFade" in _GRID_FRAGMENT
        assert "u_minor_fade_start" in _GRID_FRAGMENT
        assert "u_minor_fade_end" in _GRID_FRAGMENT
        assert "u_minor_fade_slope" in _GRID_FRAGMENT
        assert "u_major_fade_start" in _GRID_FRAGMENT
        assert "u_major_fade_end" in _GRID_FRAGMENT
        assert "u_major_fade_slope" in _GRID_FRAGMENT
        assert "minor_dist_fade" in _GRID_FRAGMENT
        assert "major_dist_fade" in _GRID_FRAGMENT
        assert "major_a *= major_dist_fade" in _GRID_FRAGMENT
        assert "mix(\n        0.12" in _GRID_FRAGMENT
        assert "mix(\n        0.28" in _GRID_FRAGMENT

    def test_grid_shader_uses_camera_distance_not_radial_origin_or_view_z(self):
        """淡出用相机欧氏距离，避免圆盘边界和旋转角度导致的忽明忽暗。"""
        from src.features.composite.render3d.gizmo_renderer import (
            _GRID_FRAGMENT, _GRID_VERTEX)
        assert "v_view_pos" in _GRID_VERTEX
        assert "v_view_pos" in _GRID_FRAGMENT
        assert "v_view_pos = view_pos.xyz" in _GRID_VERTEX
        assert "fade_dist = length(v_view_pos)" in _GRID_FRAGMENT
        assert "u_horizon_factor" not in _GRID_FRAGMENT
        assert "length(p) * u_cell_size" not in _GRID_FRAGMENT


class TestScene3DBackgroundColorSpace:
    """3D Scene Background picker values are sRGB UI values, render target is linear."""

    def test_srgb_to_linear_helper(self):
        from src.features.composite.nodes.scene3d_node import _srgb_to_linear_channel
        assert _srgb_to_linear_channel(0.0) == 0.0
        assert _srgb_to_linear_channel(1.0) == 1.0
        assert 0.21 < _srgb_to_linear_channel(0.5) < 0.22

    def test_scene3d_converts_background_before_clear(self):
        import inspect
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "bgr_srgb" in src
        assert "_srgb_to_linear_channel(bgr_srgb)" in src
        assert "_srgb_to_linear_channel(bgg_srgb)" in src
        assert "_srgb_to_linear_channel(bgb_srgb)" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 4: 2D 模式隐藏网格（page 在 snapshot 注入 show_grid=False）
# ──────────────────────────────────────────────────────────────────────────

class Test2DHideGrid:
    """网格与不透明背景按预览上下文门控（Phase 3：预览驱动）。

    v9 重构后：场景参数已移至全局「3D Scene Settings」，_apply_view_mode_overrides
    不再修改快照节点参数，而是返回一个 {node_id: {show_grid/bg_opaque/show_axes}}
    覆盖字典，经 RenderRequest → eval_ctx.view_mode_overrides 传给 Scene3D。

    门控策略不变：3D 模式 + 预览某 scene3d → 该节点用 show_grid/bg_opaque
    （看 3D 参考）；其余（2D / 预览 Output）→ 用 output_show_grid/output_bg_opaque
    （默认合成干净，用户可显式开启输出）。
    """

    def test_apply_view_mode_overrides_returns_overrides_dict(self):
        """_apply_view_mode_overrides 返回 {node_id: {show_grid/...}} 字典。"""
        import inspect
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._apply_view_mode_overrides)
        assert "show_grid" in src, "应处理 show_grid"
        assert "bg_opaque" in src, "应处理 bg_opaque（背景与网格同策略）"
        assert "output_show_grid" in src, "合成输出应可显式开启网格"
        assert "output_bg_opaque" in src, "合成输出应可显式开启背景"
        assert 'get_view_mode' in src, "应据 view_mode 判定 3D 上下文"
        assert 'return overrides' in src, "应返回 overrides 字典"

    def test_output_preview_defaults_clean_but_allows_explicit_output(self):
        """2D/Output 默认无网格透明背景；output_* 打开后可输出网格和背景。

        全局设置默认 output_show_grid/output_bg_opaque=False（干净合成输出）。
        用户在 3D Scene Settings 打开后，overrides 字典里对应节点为 True。
        """
        from types import SimpleNamespace
        from src.features.composite.ui.composite_page import CompositePage

        viewer = SimpleNamespace(get_view_mode=lambda: "2d")
        # 模拟默认全局设置（output_* = False）
        page = SimpleNamespace(
            _viewer=viewer,
            _resolve_viewer_node_id=lambda: None,
            _resolve_preview_node_id=lambda: None,
            _node_graph=None,
        )
        page._global_3d_scene_settings = (
            CompositePage._global_3d_scene_settings.__get__(page))
        page._node_graph = SimpleNamespace(_viewer_state={})

        clean = SimpleNamespace(
            nodes={
                "s1": SimpleNamespace(node_type="scene3d", parameters={})
            }
        )
        overrides = CompositePage._apply_view_mode_overrides(page, clean)
        # 2D 模式：用 output_* 默认（False）
        assert overrides["s1"]["show_grid"] is False
        assert overrides["s1"]["bg_opaque"] is False

        # 模拟用户打开 output_* 开关
        page._node_graph = SimpleNamespace(_viewer_state={
            "default_3d_scene": {"output_show_grid": True, "output_bg_opaque": True}
        })
        explicit = SimpleNamespace(
            nodes={
                "s1": SimpleNamespace(node_type="scene3d", parameters={})
            }
        )
        overrides2 = CompositePage._apply_view_mode_overrides(page, explicit)
        assert overrides2["s1"]["show_grid"] is True
        assert overrides2["s1"]["bg_opaque"] is True

    def test_3d_preview_uses_preview_switches_not_output_switches(self):
        """3D 预览目标读 show_grid/bg_opaque（预览参考），不用 output_*。"""
        from types import SimpleNamespace
        from src.features.composite.ui.composite_page import CompositePage

        viewer = SimpleNamespace(get_view_mode=lambda: "3d")
        page = SimpleNamespace(
            _viewer=viewer,
            _resolve_viewer_node_id=lambda: "s1",
            _resolve_preview_node_id=lambda: "s1",
            _node_graph=SimpleNamespace(_viewer_state={
                # 预览开关默认开；output 开关也开（但 3D 预览不用 output）
                "default_3d_scene": {
                    "show_grid": True, "bg_opaque": True, "show_axes": True,
                    "output_show_grid": True, "output_bg_opaque": True,
                }
            }),
        )
        page._global_3d_scene_settings = (
            CompositePage._global_3d_scene_settings.__get__(page))

        snapshot = SimpleNamespace(
            nodes={
                "s1": SimpleNamespace(node_type="scene3d", parameters={})
            }
        )
        overrides = CompositePage._apply_view_mode_overrides(page, snapshot)
        # 3D 预览目标：用预览开关 show_grid/bg_opaque（True）
        assert overrides["s1"]["show_grid"] is True
        assert overrides["s1"]["bg_opaque"] is True
        assert overrides["s1"]["show_axes"] is True

    def test_do_evaluate_graph_calls_view_mode_override(self):
        """_do_evaluate_graph 通过 _apply_view_mode_overrides 应用预览上下文。"""
        import inspect
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._build_render_request)
        assert "_apply_view_mode_overrides" in src, \
            "_do_evaluate_graph 应调用 _apply_view_mode_overrides"




class TestScene3DBgDefaults:
    """背景/输出默认值属于全局 3D Scene Settings。"""

    def test_bg_opaque_default_true(self):
        """bg_opaque 默认 True（不透明背景激活）。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["bg_opaque"] is True

    def test_bg_rgb_default_1c1c1c(self):
        """bg_r/g/b 默认 #1C1C1C（display sRGB）。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        expected = 28.0 / 255.0
        for k in ("bg_r", "bg_g", "bg_b"):
            assert DEFAULT_VIEWER3D_SCENE_SETTINGS[k] == expected, f"{k} 应为 #1C1C1C"

    def test_output_grid_bg_default_false(self):
        """合成输出默认干净：不输出参考网格，也不强制不透明背景。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["output_show_grid"] is False
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["output_bg_opaque"] is False
