"""P3-01 orchestrator 调度层边界钉死测试。

计划 §3.1 / P3-01：AsyncRenderer 保留为 orchestrator，RenderCommand、
lane 合并、revision 行为不变。本文件以「公开 API 冻结」方式钉住调度层
边界——任何人新增公开入口（尤其可能绕过 submit 直接触碰 GL 的入口）时，
必须显式更新本文件，防止并行 GL 路径悄悄出现。
"""

from __future__ import annotations

import inspect

from PyQt6.QtCore import QThread

from src.features.color.core.async_renderer import (
    AsyncRenderer,
    RenderCommand,
    RenderLane,
)
from src.shared.video.render_commands import RenderCommand as ExternalRenderCommand
from src.shared.video.video_async_renderer import VideoAsyncRenderer


# AsyncRenderer 自身公开方法（P3-01 冻结，2026-09-11）。
# 分类：
# - submit/run/stop：命令队列入口与线程生命周期
# - set_*/reset_video_denoise_history/discard_*：UI 线程状态投递（线程安全）
# - get_*/reset_renderer_stats/log_*：诊断与统计
ORCHESTRATOR_PUBLIC_API = frozenset({
    'submit',
    'run',
    'stop',
    'set_video_denoise_params',
    'reset_video_denoise_history',
    'set_video_denoise_window',
    'discard_pending_grading_refresh',
    'discard_pending_preview_refreshes',
    'discard_pending_gpu_scope',
    'get_original_texture_id',
    'get_baked_texture',
    'get_renderer_stats',
    'get_render_stats',
    'reset_render_stats',
    'get_lane_states',
    'get_video_texture_pool_stats',
    'get_yuv_gpu_status',
    'reset_renderer_stats',
    'log_video_path_decision',
    'get_gpu_backend_report',
    # bce29271（dev 合并）：可取消的全分辨率渲染请求
    'register_full_resolution',
    'cancel_full_resolution',
    'forget_full_resolution',
})

LANE_VALUES = {
    'video_playback',
    'viewport_refresh',
    'grading_refresh',
    'aux_tex',
    'color_output',
    'scope_snapshot',
    'gpu_scope',
}


class TestOrchestratorBoundary:
    def test_async_renderer_is_qthread_orchestrator(self):
        """orchestrator 拥有渲染线程（GL 只在该线程执行）。"""
        assert issubclass(AsyncRenderer, QThread)

    def test_public_api_surface_frozen(self):
        """公开 API 面冻结：新增公开方法必须显式更新 ORCHESTRATOR_PUBLIC_API。"""
        own_public = {
            name
            for name, member in vars(AsyncRenderer).items()
            if not name.startswith('_') and inspect.isfunction(member)
        }
        assert own_public == set(ORCHESTRATOR_PUBLIC_API), (
            "AsyncRenderer 公开 API 面发生变化："
            f"新增 {sorted(own_public - set(ORCHESTRATOR_PUBLIC_API))} / "
            f"移除 {sorted(set(ORCHESTRATOR_PUBLIC_API) - own_public)}。"
            "调度层边界（P3-01）要求新入口必须是有意识的决定——"
            "若新入口触碰 GL，请改为 submit 命令并在 _do_* 渲染线程处理。"
        )

    def test_render_command_is_shared_module_reexport(self):
        """RenderCommand 单一来源：async_renderer 只是再导出（旧入口兼容）。"""
        assert RenderCommand is ExternalRenderCommand

    def test_lane_model_stable(self):
        """7 条 lane 及取值冻结（lane 合并行为的基石，见 lane_merge 测试）。"""
        assert {lane.value for lane in RenderLane} == LANE_VALUES
        assert len(RenderLane) == 7

    def test_legacy_video_renderer_still_importable_as_qthread(self):
        """P3-05：v2 遗留 VideoAsyncRenderer 保留为 QThread（未删除）。"""
        assert issubclass(VideoAsyncRenderer, QThread)
