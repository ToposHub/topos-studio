"""Viewport3D 性能统计 — 3D 视口专用性能诊断数据层（Milestone A / P0）。

把 3D 视口渲染管线里散落的计时/计数集中成一个可序列化的 dataclass，
让 Scene3D 每帧把"慢在哪里"写出来，供 viewer overlay、perf panel、benchmark
消费。

设计原则（对应 master_plan_2026-07-07.md §4）：
- 本模块**只定义数据 + 轻量采集工具**，不含 Qt / GL 调用。
- Viewport3DPerfStats 是单帧统计快照；Scene3D 渲染线程填充，UI 线程只读。
- 字段命名与计划 §4.3 的指标字段建议对齐（frame_id/quality_profile/...
  total_cpu_ms/perspective_ms/top_ms/.../draw_calls/triangles_submitted/
  texture_binds/.../navigation_active/lod_level/internal_resolution_scale）。
- 计数器线程不安全但单线程写：Scene3D 在 render thread 独占写入。
- 日志节流：DebugTimer 提供 per-second 节流输出，避免每帧刷日志（P0-7）。

字段覆盖计划 §4.3：
    frame_id, quality_profile, viewport_layout, active_view,
    total_cpu_ms, perspective_ms, top_ms, right_ms, front_ms,
    draw_calls, triangles_submitted, meshes_visible, meshes_culled,
    texture_binds, texture_uploads, material_switches, shader_switches,
    uniform_sets, fbo_switches,
    model_cache_hit, texture_cache_hit, vbo_cache_hit,
    navigation_active, lod_level, internal_resolution_scale
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Dict, Optional


# ──────────────────────────────────────────────────────────────────────────
# 单帧性能快照
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Viewport3DPerfStats:
    """单帧 3D 视口渲染性能统计（render thread 写，UI thread 读）。

    所有 *_ms 字段单位毫秒；计数器字段为整数次数。
    缺省值代表"未采集"（None / 0），序列化时保留以便工具识别。

    字段含义见模块 docstring 与 master_plan_2026-07-07.md §4.3。
    """
    # ── 帧标识 / 上下文 ──
    frame_id: int = 0
    quality_profile: str = "final"          # final/preview/navigation/fast_solid
    viewport_layout: str = "single"         # single/quad
    active_view: str = "perspective"        # perspective/top/right/front

    # ── 计时（ms）──
    total_cpu_ms: float = 0.0               # 整个 _do_evaluate 的 CPU 墙钟
    perspective_ms: float = 0.0             # 透视视图主渲染耗时
    top_ms: float = 0.0                     # 四视图 Top 正交耗时（非 quad 时 0）
    right_ms: float = 0.0                   # 四视图 Right 正交耗时
    front_ms: float = 0.0                   # 四视图 Front 正交耗时

    # ── 绘制计数 ──
    draw_calls: int = 0                     # vao.render() 调用次数
    triangles_submitted: int = 0            # 提交三角形数（index_count/3 累加）
    meshes_visible: int = 0                 # 通过视锥剔除的 mesh 数
    meshes_culled: int = 0                  # 被视锥剔除的 mesh 数

    # ── 状态切换计数 ──
    texture_binds: int = 0                  # 材质贴图 bind 次数
    texture_uploads: int = 0                # 新贴图上传次数（cache miss）
    material_switches: int = 0              # 材质切换（u_albedo/orm 等改值）
    shader_switches: int = 0                # program 切换次数
    uniform_sets: int = 0                   # uniform 赋值次数（估算）
    fbo_switches: int = 0                   # FBO bind/切换次数

    # ── 缓存命中 ──
    model_cache_hit: Optional[bool] = None  # 导入模型 path+mtime 缓存命中
    texture_cache_hit: Optional[bool] = None  # 材质贴图缓存命中（本帧）
    vbo_cache_hit: Optional[bool] = None    # MeshVBOPool 命中（本帧）
    # P6：场景内容缓存命中——仅相机/视图变化时为 True（资源未重建，只重发 draw call）。
    # True = 复用了 mesh VAO/材质贴图/IBL/render target，相机导航的核心加速指标。
    scene_content_cache_hit: Optional[bool] = None

    # ── 交互 / LOD ──
    navigation_active: bool = False         # 是否处于导航 LOD（interaction_active）
    lod_level: int = 0                      # LOD 等级（0=full，1/2/3=降级）
    internal_resolution_scale: float = 1.0  # 内部分辨率缩放（1.0=全分辨率）

    # ── 内容规模（来自 payload metadata，P0-5）──
    mesh_count: int = 0                     # flat mesh 总数
    triangle_count: int = 0                 # 全场景三角形总数
    material_count: int = 0                 # 材质数
    texture_count: int = 0                  # 贴图数

    # ── 输出 ──
    resolution: tuple = (0, 0)              # (w, h)

    def to_dict(self) -> Dict:
        """序列化为可放入 TextureResult.render_diagnostics 的 dict。"""
        return asdict(self)

    def reset(self, frame_id: int = 0) -> None:
        """清零所有计数器，开始新一帧采集。frame_id 用于追踪。

        上下文描述字段（quality_profile / viewport_layout / active_view）一并
        恢复 dataclass 默认值（AUD-9105：调用方每帧 reset 后会重新赋值；
        若未赋值，overlay/日志读到的将是默认值而非上一帧的陈旧值）。
        """
        self.frame_id = frame_id
        # AUD-9105：上下文描述字段复位为默认（final/single/perspective）
        self.quality_profile = "final"
        self.viewport_layout = "single"
        self.active_view = "perspective"
        self.total_cpu_ms = 0.0
        self.perspective_ms = 0.0
        self.top_ms = 0.0
        self.right_ms = 0.0
        self.front_ms = 0.0
        self.draw_calls = 0
        self.triangles_submitted = 0
        self.meshes_visible = 0
        self.meshes_culled = 0
        self.texture_binds = 0
        self.texture_uploads = 0
        self.material_switches = 0
        self.shader_switches = 0
        self.uniform_sets = 0
        self.fbo_switches = 0
        # 缓存命中标志保留给本帧设置；reset 时先清 None
        self.model_cache_hit = None
        self.texture_cache_hit = None
        self.vbo_cache_hit = None
        self.scene_content_cache_hit = None
        self.navigation_active = False
        self.lod_level = 0
        self.internal_resolution_scale = 1.0
        self.mesh_count = 0
        self.triangle_count = 0
        self.material_count = 0
        self.texture_count = 0
        self.resolution = (0, 0)


# ──────────────────────────────────────────────────────────────────────────
# 轻量计时工具（context manager）
# ──────────────────────────────────────────────────────────────────────────

class ScopedTimer:
    """with 作用域计时器，结束时把毫秒累加到目标 stats 字段。

    用法::

        stats = Viewport3DPerfStats()
        with ScopedTimer(stats, 'perspective_ms'):
            ...  # 透视渲染
        # stats.perspective_ms 已累加该段耗时

    设计为极低开销：仅一次 perf_counter() 调用 + 一次赋值，避免污染测量。
    """

    __slots__ = ("_stats", "_field", "_t0")

    def __init__(self, stats: Viewport3DPerfStats, field_name: str) -> None:
        self._stats = stats
        self._field = field_name
        self._t0 = 0.0

    def __enter__(self) -> "ScopedTimer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        dt = (time.perf_counter() - self._t0) * 1000.0
        # 累加（同一段代码可能嵌套或多次进入，如 quad 各视图）
        setattr(self._stats, self._field,
                getattr(self._stats, self._field) + dt)
        return False


# ──────────────────────────────────────────────────────────────────────────
# 日志节流（P0-7：避免每帧刷日志）
# ──────────────────────────────────────────────────────────────────────────

class StatsLogger:
    """每秒最多输出一次 stats 摘要的节流日志器。

    Scene3D 每帧调 log(stats)；只有距上次输出 >= interval 秒才真正写日志。
    这样 debug 开关下能看到稳定的"每秒快照"，不淹没控制台。

    用法::

        _stats_log = StatsLogger(logger, interval=1.0)
        def _do_evaluate(...):
            ...
            _stats_log.log(stats)  # 每帧调，内部自动节流
    """

    __slots__ = ("_logger", "_interval", "_last", "_enabled")

    def __init__(self, logger, interval: float = 1.0, enabled: bool = True) -> None:
        self._logger = logger
        self._interval = max(0.1, float(interval))
        self._last = None  # None = 从未输出过，首次调用必定输出
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def log(self, stats: Viewport3DPerfStats) -> None:
        """每 interval 秒输出一次 stats 摘要（debug 用）。"""
        if not self._enabled or self._logger is None:
            return
        now = time.monotonic()
        # 首次调用（_last is None）直接输出；之后按 interval 节流。
        if self._last is not None and (now - self._last) < self._interval:
            return
        self._last = now
        self._logger.debug(
            "Viewport3D perf | frame=%d profile=%s layout=%s "
            "total=%.2fms persp=%.2f top=%.2f right=%.2f front=%.2f | "
            "draws=%d tris=%d visible=%d/%d | "
            "tex_binds=%d uploads=%d mat_sw=%d shader_sw=%d fbo=%d | "
            "lod=%d nav=%s res_scale=%.2f meshes=%d tris_total=%d"
            % (
                stats.frame_id, stats.quality_profile, stats.viewport_layout,
                stats.total_cpu_ms, stats.perspective_ms,
                stats.top_ms, stats.right_ms, stats.front_ms,
                stats.draw_calls, stats.triangles_submitted,
                stats.meshes_visible, stats.meshes_visible + stats.meshes_culled,
                stats.texture_binds, stats.texture_uploads,
                stats.material_switches, stats.shader_switches, stats.fbo_switches,
                stats.lod_level, stats.navigation_active,
                stats.internal_resolution_scale,
                stats.mesh_count, stats.triangle_count,
            ))


# ──────────────────────────────────────────────────────────────────────────
# Overlay 文本生成（viewer 用一行小字显示当前帧关键指标）
# ──────────────────────────────────────────────────────────────────────────

def format_overlay(stats: Viewport3DPerfStats, fps: Optional[float] = None) -> str:
    """生成 viewer 角落单行 perf overlay 文本。

    格式示例::

        FPS 60.0 | 16.5ms | 12 draws | 524k tris | nav:LOD1 0.65x

    fps 为 None 时不显示 FPS（由调用方按帧间隔单独算）。
    """
    fps_str = f"FPS {fps:5.1f} | " if fps is not None else ""
    tris = stats.triangles_submitted
    if tris >= 1_000_000:
        tris_str = f"{tris / 1_000_000:.1f}M tris"
    elif tris >= 1000:
        tris_str = f"{tris / 1000:.0f}k tris"
    else:
        tris_str = f"{tris} tris"
    nav = ""
    if stats.navigation_active or stats.lod_level > 0:
        nav = f" | nav:LOD{stats.lod_level} {stats.internal_resolution_scale:.2f}x"
    return (f"{fps_str}{stats.total_cpu_ms:5.1f}ms | "
            f"{stats.draw_calls} draws | {tris_str}{nav}")


def format_quad_overlay(stats: Viewport3DPerfStats) -> str:
    """四视图额外行：各视图耗时（非 quad 时返回空串）。"""
    if stats.viewport_layout != "quad":
        return ""
    return (f"P:{stats.perspective_ms:4.1f} T:{stats.top_ms:4.1f} "
            f"R:{stats.right_ms:4.1f} F:{stats.front_ms:4.1f}")
