"""Viewport3D State — 3D 视口专用状态层（Phase 3）。

把原本揉在 CompositeViewer 里的 3D 视图状态（布局模式、活动视图、3D 工具模式、
组件模式、吸附/量化设置）独立成纯数据模块，避免 2D 合成工具与 3D DCC 工具的
状态纠缠。

设计原则（对应 audit plan "Recommended 3D View Architecture"）：
- 本模块**只定义数据**（enum + dataclass），不含 Qt 控件、不含渲染逻辑。
- CompositeViewer 持有一个 Viewport3DState 实例；viewport3d_controller 读写它。
- 2D 的 ViewerToolMode（viewer_tool_system.py）保持独立；3D 工具模式用本模块的
  Viewer3DToolMode（SELECT/MOVE/ROTATE/SCALE）——两者命名空间分开。
- 视图布局/工具模式是**纯视图状态**，不进节点图、不脏化项目（audit plan 约定）。

后续阶段（Phase 4 四视图、Phase 5 变换工具、Phase 6 组件/吸附）会扩展本模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────
# 视图布局
# ──────────────────────────────────────────────────────────────────────────

class ViewportLayoutMode(Enum):
    """视口布局模式（C4D 风格：单视图 / 四视图）。

    SINGLE: 单视图（默认，透视）。
    QUAD:   四视图（透视 + 顶/右/前正交）。MMB click 切换。
    """
    SINGLE = "single"
    QUAD = "quad"


# ──────────────────────────────────────────────────────────────────────────
# 渲染质量档位（Milestone B / P1：Navigation LOD）
# ──────────────────────────────────────────────────────────────────────────

class ViewportQualityProfile(Enum):
    """3D 视口渲染质量档位（对应 master_plan §2.2 渲染质量档位表）。

    各档位的降载策略在 Scene3DNode._apply_quality_profile() 里实现：

    FINAL:        Scene3D 合成输出 / 停止操作后恢复。完整材质 + IBL/PBR +
                  用户 MSAA + 全分辨率。
    PREVIEW:      普通静止 viewport。与 FINAL 同参：完整 PBR(full) + IBL +
                  用户 MSAA + 全分辨率（AUD-11410：旧文档"简化 PBR + 可选
                  MSAA"与参数表不符，参数表为准；PREVIEW 与 FINAL 的区别
                  仅在选档时机，见下方选择逻辑）。
    NAVIGATION:   鼠标拖动视图。baseColor/solid + 简化 PBR(diffuse) + 关 MSAA +
                  降分辨率(0.5~0.75x)。
    FAST_SOLID:   大模型极限交互。solid 灰模 / 顶点色 + 无 IBL + 关 MSAA +
                  降分辨率。非活动四视图默认。

    选择逻辑（master_plan §5.4 + §18）：
        - interaction_active=True → NAVIGATION（大模型可降为 FAST_SOLID）
        - 停止后 restore_delay ms → PREVIEW/FINAL
        - 合成输出（render_mode=full/export）→ FINAL（不受导航影响）
    """
    FINAL = "final"
    PREVIEW = "preview"
    NAVIGATION = "navigation"
    FAST_SOLID = "fast_solid"


# 各档位对应的降载参数（Scene3D 读，见 _apply_quality_profile）。
# 字段：msaa 降级系数、IBL 开关、分辨率缩放、PBR 简化等级。
# - msaa_factor: 用户 MSAA × 此系数（≤0 → 强制 0）；NAVIGATION 直接关 MSAA。
# - use_ibl: 是否保留 IBL（NAVIGATION 关以省 cubemap 采样）。
# - resolution_scale: 内部分辨率缩放（1.0=全分辨率）。
# - pbr_mode: "full" / "diffuse" / "solid" —— full=Cook-Torrance+IBL，
#   diffuse=只 diffuse Lambert（关 specular/IBL），solid=纯顶点/常量色。
QUALITY_PROFILE_PARAMS = {
    ViewportQualityProfile.FINAL: {
        "msaa_factor": 1.0, "use_ibl": True,
        "resolution_scale": 1.0, "pbr_mode": "full",
        "backface_culling": False,  # 静止态保守：避免法线/winding 不一致丢面
    },
    ViewportQualityProfile.PREVIEW: {
        "msaa_factor": 1.0, "use_ibl": True,
        "resolution_scale": 1.0, "pbr_mode": "full",
        "backface_culling": False,
    },
    ViewportQualityProfile.NAVIGATION: {
        "msaa_factor": 0.0, "use_ibl": False,
        "resolution_scale": 0.65, "pbr_mode": "diffuse",
        # P8-3：导航态开启 backface culling——闭网格背面片元不可见，
        # GPU 早期光栅化阶段剔除，省 ~50% 片元着色。速度优先。
        "backface_culling": True,
    },
    ViewportQualityProfile.FAST_SOLID: {
        "msaa_factor": 0.0, "use_ibl": False,
        "resolution_scale": 0.5, "pbr_mode": "solid",
        "backface_culling": True,
    },
}


# 大模型自动降级到 FAST_SOLID 的三角形阈值（master_plan §15.1 Large Model
# Triangle Threshold = 500k）。超过且处于导航态时用 FAST_SOLID。
LARGE_MODEL_TRIANGLE_THRESHOLD = 500_000


class ViewportViewKind(Enum):
    """四视图中的单个视图类型。

    PERSPECTIVE: 透视相机（轨道）。
    TOP:         顶视图（Y 轴向下看，正交）。
    RIGHT:       右视图（X 轴向右看，正交）。
    FRONT:       前视图（Z 轴向前看，正交）。
    """
    PERSPECTIVE = "perspective"
    TOP = "top"
    RIGHT = "right"
    FRONT = "front"


class OrbitCenterMode(Enum):
    """Default pivot used by Alt+Left 3D view orbit.

    AUTO: selected 3D object center if available; otherwise current screen/view
        center. This matches the usual C4D feel.
    SCREEN: keep the current navigation target, i.e. orbit around the visible
        screen/view center.
    WORLD: orbit around the world origin.
    SELECTED: orbit around the selected 3D object's transform center.
    CURSOR: orbit around the stored 3D cursor/custom world point.
    """
    AUTO = "auto"
    SCREEN = "screen"
    WORLD = "world"
    SELECTED = "selected"
    CURSOR = "cursor"


# 四视图中各视图在 QUAD 布局里的子矩形顺序（左上/右上/左下/右下）。
# 与 viewport_overlays 的绘制顺序一致（Phase 4 实现）。
QUAD_VIEW_ORDER = (
    ViewportViewKind.PERSPECTIVE,
    ViewportViewKind.TOP,
    ViewportViewKind.RIGHT,
    ViewportViewKind.FRONT,
)

# 视图标签（Phase 4 绘制用；i18n 键可后续接入）。
VIEW_KIND_LABELS = {
    ViewportViewKind.PERSPECTIVE: "Perspective",
    ViewportViewKind.TOP: "Top",
    ViewportViewKind.RIGHT: "Right",
    ViewportViewKind.FRONT: "Front",
}

ORBIT_CENTER_LABELS = {
    OrbitCenterMode.AUTO: "Auto (Selected, else Screen)",
    OrbitCenterMode.SCREEN: "Screen Center",
    OrbitCenterMode.WORLD: "World Origin",
    OrbitCenterMode.SELECTED: "Selected Object",
    OrbitCenterMode.CURSOR: "3D Cursor / Custom Point",
}


# ──────────────────────────────────────────────────────────────────────────
# 3D 工具模式（与 2D ViewerToolMode 分开）
# ──────────────────────────────────────────────────────────────────────────

class Viewer3DToolMode(Enum):
    """3D 视口工具模式（C4D 风格）。

    SELECT: 选择/查看（默认）。
    MOVE:   移动 gizmo（W）。
    ROTATE: 旋转 gizmo（E）。
    SCALE:  缩放 gizmo（R）。

    与 2D 的 ViewerToolMode（SELECT/PAN/TRANSFORM/CROP/MASK/ROTO/CORNERPIN）分开，
    避免 2D 合成工具与 3D DCC 工具的模式语义混淆。
    """
    SELECT = "select3d"
    MOVE = "move3d"
    ROTATE = "rotate3d"
    SCALE = "scale3d"


# 3D 工具模式默认快捷键（Phase 4 工具栏接线时用；可后续接 settings 覆盖）。
TOOL3D_MODE_SHORTCUTS = {
    Viewer3DToolMode.SELECT: "Q",   # C4D: 选择
    Viewer3DToolMode.MOVE: "W",     # C4D: 移动
    Viewer3DToolMode.ROTATE: "E",   # C4D: 旋转
    Viewer3DToolMode.SCALE: "R",    # C4D: 缩放
}

TOOL3D_MODE_LABELS = {
    Viewer3DToolMode.SELECT: "Select",
    Viewer3DToolMode.MOVE: "Move",
    Viewer3DToolMode.ROTATE: "Rotate",
    Viewer3DToolMode.SCALE: "Scale",
}


# ──────────────────────────────────────────────────────────────────────────
# 组件选择模式（Phase 6 组件工具用）
# ──────────────────────────────────────────────────────────────────────────

class ComponentMode(Enum):
    """组件选择模式（C4D 风格：对象 / 点 / 边 / 面）。

    OBJECT: 对象级（默认，整个 mesh 一起选/变换）。
    POINT:  点级（需要 mesh picking，Phase 6）。
    EDGE:   边级（Phase 6）。
    POLYGON: 面级（Phase 6）。
    """
    OBJECT = "object"
    POINT = "point"
    EDGE = "edge"
    POLYGON = "polygon"


# ──────────────────────────────────────────────────────────────────────────
# 显示模式（F2：Mesh 显示模式）
# ──────────────────────────────────────────────────────────────────────────

class ViewportDisplayMode(Enum):
    """3D 视口 shading / 显示模式（master_plan §5.1 / §21.5）。

    TEXTURED:    带材质贴图的完整 PBR 渲染（默认）。
    SOLID:       无贴图，纯灰模 + 光照（快速检查形体）。
    WIREFRAME:   纯线框（edge buffer + GL_LINES，查拓扑）。
    SHADED_WIRE: 材质 pass + 叠加线框（带深度 bias，查拓扑+形体）。
    POINTS:      只显示顶点（GL_POINTS，查顶点分布）。
    UV_DEBUG:    UV 坐标可视化（color = vec3(uv,0)，查 UV 布局）。
    NORMAL_DEBUG: 法线方向可视化（线段 overlay，查法线）。
    """
    TEXTURED = "textured"
    SOLID = "solid"
    WIREFRAME = "wireframe"
    SHADED_WIRE = "shaded_wire"
    POINTS = "points"
    UV_DEBUG = "uv_debug"
    NORMAL_DEBUG = "normal_debug"


DISPLAY_MODE_LABELS = {
    ViewportDisplayMode.TEXTURED: "Textured",
    ViewportDisplayMode.SOLID: "Solid",
    ViewportDisplayMode.WIREFRAME: "Wireframe",
    ViewportDisplayMode.SHADED_WIRE: "Shaded Wire",
    ViewportDisplayMode.POINTS: "Points",
    ViewportDisplayMode.UV_DEBUG: "UV Debug",
    ViewportDisplayMode.NORMAL_DEBUG: "Normals",
}


# ──────────────────────────────────────────────────────────────────────────
# 吸附 / 量化设置（Phase 6）
# ──────────────────────────────────────────────────────────────────────────

class SnapMode(Enum):
    """3D 吸附模式（Phase 6，从网格吸附起步）。

    NONE:    不吸附（默认）。
    GRID:    网格吸附（吸附到 u_cell_size 网格）。
    VERTEX:  顶点吸附（Phase 6+）。
    OBJECT:  对象吸附（Phase 6+）。
    """
    NONE = "none"
    GRID = "grid"
    VERTEX = "vertex"
    OBJECT = "object"


@dataclass
class SnapQuantizeSettings:
    """吸附 + 量化设置（Phase 6）。

    snap_mode: 当前吸附模式（NONE 默认）。
    snap_distance: 吸附生效距离（屏幕像素，超出则不吸附）。
    quantize_enabled: 是否启用步进量化。
    move_step:    移动量化步长（世界单位）。
    rotate_step:  旋转量化步长（度）。
    scale_step:   缩放量化步长（比例）。
    """
    snap_mode: SnapMode = SnapMode.NONE
    snap_distance: float = 12.0  # px
    quantize_enabled: bool = False
    move_step: float = 0.25
    rotate_step: float = 5.0
    scale_step: float = 0.1


# ──────────────────────────────────────────────────────────────────────────
# 3D 视口状态聚合（CompositeViewer 持有此实例）
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Viewport3DState:
    """3D 视口的完整状态聚合（纯数据，无 Qt/渲染）。

    CompositeViewer 持有一个实例；viewport3d_controller 读写它；
    viewport3d_toolbar（Phase 4）绑定它做 UI 反映。

    字段：
        layout_mode: 单视图 / 四视图（默认 SINGLE）。
        active_view: QUAD 布局下当前活动子视图（默认 PERSPECTIVE）。
        tool_mode: 当前 3D 工具模式（SELECT/MOVE/ROTATE/SCALE，默认 SELECT）。
        component_mode: 组件选择模式（默认 OBJECT）。
        snap: 吸附/量化设置。
        axis_edit_enabled: 轴/枢轴编辑独立于物体变换（Phase 6，默认 False）。

    注意：这些都是**视图状态**，不进节点图、不脏化项目（audit plan 约定）。
    相机参数（cam_dist/yaw/pitch/target）仍由 CompositeViewer 持有并回写节点，
    不在此 dataclass（它们与节点图耦合，非纯视图状态）。
    """
    layout_mode: ViewportLayoutMode = ViewportLayoutMode.SINGLE
    active_view: ViewportViewKind = ViewportViewKind.PERSPECTIVE
    tool_mode: Viewer3DToolMode = Viewer3DToolMode.SELECT
    component_mode: ComponentMode = ComponentMode.OBJECT
    snap: SnapQuantizeSettings = field(default_factory=SnapQuantizeSettings)
    axis_edit_enabled: bool = False
    # M2: SINGLE 模式下显示的正交视图（None=透视）。
    # 从 QUAD 切到 SINGLE 时，若当时 active_view 是正交视图，记录下来，让单视图
    # 继续显示该正交视角（Scene3D 据此用正交投影渲染主纹理）。
    single_ortho_view: Optional[ViewportViewKind] = None
    # Alt+Left orbit pivot preference. AUTO matches C4D: selected object center
    # when possible, otherwise the current screen/view center.
    orbit_center_mode: OrbitCenterMode = OrbitCenterMode.AUTO
    # Stored custom orbit point for OrbitCenterMode.CURSOR.
    orbit_cursor_world: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # P7：隔离模式节点 id——只渲染该节点的 mesh（Shift+I toggle）。
    # None = 不隔离（渲染全部连接的 Geo3D/Model3D）。
    isolate_node_id: Optional[str] = None
    # F2：主显示模式（textured/solid/wire/shaded_wire/points/uv_debug/normal_debug）。
    # TEXTURED 默认。影响 Scene3D shader/pass 选择 + viewport overlay 叠加。
    display_mode: ViewportDisplayMode = ViewportDisplayMode.TEXTURED
    # F2：overlay 开关（与 display_mode 正交，可叠加在任何模式下）。
    overlay_show_wireframe: bool = False     # 叠加线框（不切换主模式）
    overlay_show_normals: bool = False       # 叠加法线线段
    overlay_show_tangents: bool = False      # 叠加切线线段（normal map 调试）
    overlay_show_skeleton: bool = False      # 叠加骨架（F6 联动）
    overlay_show_bounds: bool = False        # 叠加包围盒

    def toggle_layout(self) -> ViewportLayoutMode:
        """切换 SINGLE ↔ QUAD（MMB click 触发）。返回切换后的模式。"""
        self.layout_mode = (ViewportLayoutMode.QUAD
                            if self.layout_mode == ViewportLayoutMode.SINGLE
                            else ViewportLayoutMode.SINGLE)
        if self.layout_mode == ViewportLayoutMode.QUAD:
            # 切到 QUAD：重置活动视图为透视，清空单视图正交 override
            self.active_view = ViewportViewKind.PERSPECTIVE
            self.single_ortho_view = None
        else:
            # 切到 SINGLE：保留 QUAD 时选中的正交视图作为单视图 override。
            # 若 active 是透视则 single_ortho_view=None（普通透视单视图）。
            if self.active_view == ViewportViewKind.PERSPECTIVE:
                self.single_ortho_view = None
            else:
                self.single_ortho_view = self.active_view
        return self.layout_mode

    def is_quad(self) -> bool:
        """是否处于四视图布局。"""
        return self.layout_mode == ViewportLayoutMode.QUAD

    def effective_view_kind(self) -> ViewportViewKind:
        """返回当前应显示/编辑的视图类型（统一查询）。

        QUAD 模式 = active_view；SINGLE 模式 = single_ortho_view 或 PERSPECTIVE。
        viewer/Scene3D/gizmo 都用这个决定投影/渲染路径。
        """
        if self.is_quad():
            return self.active_view
        if self.single_ortho_view is not None:
            return self.single_ortho_view
        return ViewportViewKind.PERSPECTIVE

    def is_ortho_effective(self) -> bool:
        """当前有效视图是否为正交（quad 的正交象限 或 单正交视图）。"""
        return self.effective_view_kind() != ViewportViewKind.PERSPECTIVE

    def reset_tool(self) -> None:
        """重置工具状态到默认（SELECT/OBJECT）。切回 2D 或失焦时调。"""
        self.tool_mode = Viewer3DToolMode.SELECT
        self.component_mode = ComponentMode.OBJECT
