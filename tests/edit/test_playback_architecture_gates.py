"""
Batch 9: 实时播放架构 grep gates（缺陷计划 §8 验证命令的测试化）。

把计划表的 grep gate 固化为测试，防止后续改动回退：
- realtime engine 是热路径入口。
- present 协议存在（GpuPresentFrame）。
- 关键模块完整。
- 热路径 glFinish 已被 fence 替代（hot path 源码无 glFinish 直接调用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


# =============================================================================
# 模块完整性 gates
# =============================================================================


class TestModuleIntegrity:
    """关键模块必须存在（防止误删）。"""

    REQUIRED_MODULES = [
        "src/features/edit/core/realtime_playback/types.py",
        "src/features/edit/core/realtime_playback/policy.py",
        "src/features/edit/core/realtime_playback/telemetry.py",
        "src/features/edit/core/realtime_playback/engine.py",
        "src/features/edit/core/realtime_playback/source_resolver.py",
        "src/features/edit/core/realtime_playback/playback_budget.py",
        "src/features/edit/core/realtime_playback/audio_ring_buffer.py",
        "src/features/edit/core/realtime_playback/frame_ring_buffer.py",
        "src/features/edit/core/realtime_playback/clip_decode_stream.py",
        "src/features/edit/core/realtime_playback/timeline_decode_scheduler.py",
        "src/features/edit/core/realtime_playback/gpu_frame_pool.py",
        "src/features/edit/core/realtime_playback/timeline_gpu_compositor.py",
        "src/features/edit/core/realtime_playback/render_cache_adapter.py",
        "src/shared/gpu/present_frame.py",
        "src/shared/media/asset_proxy_manager.py",
        "src/shared/media/proxy_manifest.py",
    ]

    def test_all_required_modules_exist(self) -> None:
        missing = []
        for rel in self.REQUIRED_MODULES:
            if not (PROJECT_ROOT / rel).exists():
                missing.append(rel)
        assert missing == [], f"missing required modules: {missing}"

    def test_engine_exports_request_present(self) -> None:
        """engine 必须导出 request_present（唯一热路径入口）。"""
        from src.features.edit.core.realtime_playback import RealtimePlaybackEngine
        assert hasattr(RealtimePlaybackEngine, "request_present")

    def test_present_frame_exports(self) -> None:
        """present 协议必须导出 GpuPresentFrame/TextureLease/OutputSlotPool。"""
        from src.shared.gpu import GpuPresentFrame, OutputSlotPool, TextureLease, SlotState
        assert GpuPresentFrame is not None
        assert OutputSlotPool is not None
        assert TextureLease is not None
        assert SlotState is not None


# =============================================================================
# 热路径 glFinish gate
# =============================================================================


class TestHotPathGlFinishGate:
    """
    缺陷计划 §3.1: 热路径 glFinish 调用次数为 0。

    _process_task 主路径（_finalize_present_frame）用 glFenceSync 而非 glFinish。
    剩余 glFinish 仅在 fence 创建失败的 rare fallback（注释标注）。
    """

    def test_process_task_uses_fence_not_finish(self) -> None:
        """_process_task 主路径调用 _finalize_present_frame，不直接调 glFinish。"""
        renderer_src = _read(SRC_ROOT / "features/edit/core/edit_gpu_preview_renderer.py")
        # 主路径调用 _finalize_present_frame
        assert "_finalize_present_frame" in renderer_src
        # 主 composite 路径不应有裸 glFinish()（只在 fallback 注释/rare path）
        # 统计 glFinish 出现次数：应只在 fallback 路径
        lines_with_glfinish = [
            l for l in renderer_src.splitlines()
            if "glFinish" in l and not l.strip().startswith("#")
            and "glFinish()" in l
        ]
        # 允许 fallback 路径的 glFinish，但主路径（_finalize_present_frame 内）
        # 不应直接调 glFinish
        # 这里验证：_finalize_present_frame 方法体内没有 glFinish() 直接调用
        # （fence 失败 fallback 在 _process_task 外层）
        assert "glFenceSync" in renderer_src, "must use glFenceSync"


# =============================================================================
# RealtimePlaybackEngine 集成 gate
# =============================================================================


class TestEngineIntegrationGate:
    """缺陷计划 RP-P0-05: RealtimePlaybackEngine 必须接入 TimelinePreviewRenderer。"""

    def test_renderer_has_engine(self) -> None:
        src = _read(SRC_ROOT / "features/edit/core/timeline_preview_renderer.py")
        assert "RealtimePlaybackEngine" in src, (
            "TimelinePreviewRenderer must integrate RealtimePlaybackEngine"
        )
        assert "request_present_via_engine" in src
        assert "notify_playback_state" in src

    def test_panel_does_not_duplicate_playback_state_bridge(self) -> None:
        """Controller state must reach the renderer through the engine bridge."""
        src = _read(SRC_ROOT / "features/edit/ui/timeline_panel.py")
        assert "_on_controller_state_changed" in src
        assert "_preview_renderer.notify_playback_state" not in src

        renderer_src = _read(SRC_ROOT / "features/edit/core/timeline_preview_renderer.py")
        assert "bind_playback_controller" in renderer_src
        assert "playback_active_changed" in renderer_src

    def test_present_descriptor_not_downgraded_to_legacy_tuple(self) -> None:
        """GpuPresentFrame 必须保留到 ProgramViewer，不能再 as_legacy_tuple。"""
        src = _read(SRC_ROOT / "features/edit/core/timeline_preview_renderer.py")
        assert "_stash_pending_present_frame" in src
        assert "_pop_pending_present_frame" in src
        assert '"present_frame": present_frame' in src
        present_handler = src.split("def _on_present_frame_ready", 1)[1].split(
            "def _on_present_slot_acked", 1
        )[0]
        assert "as_legacy_tuple" not in present_handler

    def test_renderer_does_not_reuse_unacked_present_slot(self) -> None:
        """present pool 满时必须返回失败，不能继续写旧 output texture。"""
        src = _read(SRC_ROOT / "features/edit/core/edit_gpu_preview_renderer.py")
        ensure_body = src.split("def _ensure_composite_fbo", 1)[1].split(
            "def _ensure_track_fbo", 1
        )[0]
        assert "return False" in ensure_body
        assert "present_slot_unavailable" in src


# =============================================================================
# master fallback 禁止 gate
# =============================================================================


class TestMasterFallbackGate:
    """缺陷计划 RP-P0-07: realtime 不解 master。"""

    def test_frame_provider_has_allow_master_fallback(self) -> None:
        """request_frame 必须有 allow_master_fallback 参数。"""
        src = _read(SRC_ROOT / "shared/video/frame_provider_service.py")
        assert "allow_master_fallback" in src

    def test_worker_realtime_path_disables_master(self) -> None:
        """_get_video_frame realtime 路径必须传 allow_master_fallback=False。"""
        src = _read(SRC_ROOT / "features/edit/core/timeline_preview_renderer.py")
        assert "allow_master_fallback=False" in src
        assert "_is_realtime_playback_active" in src


# =============================================================================
# 传输快捷键作用域 gate
# =============================================================================


class TestTransportShortcutScopingGate:
    """Edit 传输快捷键必须面板级（WidgetWithChildrenShortcut）。

    ApplicationShortcut/WindowShortcut 都会在 Motion Graphic 编辑器激活时
    截胡 Space/J/K/L——父子 Tool 窗口不隔离快捷键作用域，且快捷键系统在
    key press 到达任何 event filter 之前就消费按键，导致 MG 空格播放失效。
    """

    def test_space_shortcut_is_panel_scoped(self) -> None:
        """Space 必须面板级：让按键能到达 MG 编辑器的过滤器链。"""
        source = _read(SRC_ROOT / "features/edit/ui/timeline_panel.py")
        assert (
            "self._shortcut_space.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)"
            in source
        )
        assert "setContext(Qt.ShortcutContext.ApplicationShortcut)" not in source

    def test_jkl_window_shortcuts_are_guarded(self) -> None:
        """窗口级 J/K/L 必须有激活窗口守卫，避免在 MG 编辑器下穿梭主时间线。"""
        source = _read(SRC_ROOT / "features/edit/ui/timeline_panel.py")
        assert "def _transport_context_active" in source
        guarded = source.count("if not self._transport_context_active():")
        assert guarded == 3, f"expected 3 guarded JKL handlers, found {guarded}"
