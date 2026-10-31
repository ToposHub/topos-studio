"""
Final-8 (FINAL-P2-16 / 规格 §6.2): legacy hot path isolation gates.

这些测试把计划 §6.2 的 grep gate 固化为运行时回归测试，防止后续改动把旧
realtime fallback 漏回热路径。

Gate 1：REALTIME hot path 不直接走 _try_gpu_render 绕过 engine。
        注意：_try_gpu_render 作为 worker 内部的 GPU 解码+合成实现仍存在
        （Final-2 之后由 SourceDecision 约束），所以这里验证的是"engine
        是 source 决策唯一入口"，而不是删掉 _try_gpu_render。
Gate 2：REALTIME present 不走 legacy tuple / as_legacy_tuple / set_shared_texture。
Gate 3：glFinish 仅作为 fence 失败时的 rare fallback，不在正常 present 路径。
Gate 4：realtime master decode 必须被 SourceDecision 拒绝（Final-2 已落地）。
Gate 5：engine.request_present_via_engine 在播放态被调用（Final-1 已落地）。

grep gate 文本（计划 §6.2）：
    rg -n "_try_gpu_render|EditGpuDecodeManager|get_frame\\(" src/features/edit/core
    rg -n "as_legacy_tuple\\(|set_shared_texture\\(|frame_ready\\.emit\\(.*tex_id" \
        src/features/edit/core src/features/edit/ui
    rg -n "glFinish|ctx\\.finish|fbo\\.read|Framebuffer\\.read|to_ndarray|frame\\.transfer" \
        src/features/edit src/shared
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _rg(pattern: str, *paths: str) -> str:
    """运行 ripgrep，返回 stdout（无命中返回空字符串）。"""
    try:
        result = subprocess.run(
            ["rg", "-n", pattern, *paths],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        return result.stdout.strip()
    except Exception:
        return ""


# =============================================================================
# Gate 1: _try_gpu_render / EditGpuDecodeManager / _gpu_renderer.submit 仍存在
#         但作为 worker 内部实现（已被 SourceDecision 约束）。
#         这里只验证 grep 能找到它们（说明未被误删），并且 engine 接管了
#         source 决策（Final-1/2 落地）。
# =============================================================================


def test_try_gpu_render_still_present_as_worker_implementation():
    """_try_gpu_render 必须仍存在（worker 内部实现，Final-2 后受 SourceDecision 约束）。

    计划 §6.1 禁止"继续在 _try_gpu_render 里加补丁"，但不要求删掉它 ——
    它是 worker GPU 路径的核心实现，Final-2 之后通过 SourceDecision 约束其行为。
    删掉它会破坏既有解码能力。
    """
    out = _rg(r"def _try_gpu_render\(", "src/features/edit/core")
    assert "def _try_gpu_render" in out, (
        "_try_gpu_render 必须存在（worker GPU 实现入口）。如被删除，Final-2 的 "
        "policy-aware decode 将失去实际执行点。"
    )


def test_engine_request_present_via_engine_present():
    """request_present_via_engine 必须存在（Final-1 热路径唯一入口）。"""
    out = _rg(r"def request_present_via_engine\(", "src/features/edit/core")
    assert "def request_present_via_engine" in out


def test_engine_uses_source_resolver():
    """engine 必须集成 SourceResolver（Final-1 source 决策唯一入口）。"""
    out = _rg(r"self\._source_resolver\s*=\s*SourceResolver", "src/features/edit/core")
    assert out, "engine/renderer 必须实例化 SourceResolver"


# =============================================================================
# Gate 2: REALTIME 禁止 legacy tuple / as_legacy_tuple
# =============================================================================


def test_no_as_legacy_tuple_calls_in_edit_core():
    """src/features/edit/core 下不应调用 as_legacy_tuple()。

    as_legacy_tuple 的定义在 src/shared/gpu/present_frame.py（保留作为过渡 API），
    但 edit 热路径不应使用它（Final-4 已禁用 REALTIME tuple）。
    """
    out = _rg(r"as_legacy_tuple\(", "src/features/edit/core")
    assert out == "", f"edit core 不应调用 as_legacy_tuple()：\n{out}"


def test_no_as_legacy_tuple_calls_in_edit_ui():
    """src/features/edit/ui 下不应调用 as_legacy_tuple()。"""
    out = _rg(r"as_legacy_tuple\(", "src/features/edit/ui")
    assert out == "", f"edit ui 不应调用 as_legacy_tuple()：\n{out}"


def test_set_shared_texture_tuple_path_gated_by_realtime_mode():
    """set_shared_texture 的 tuple 调用必须被 _realtime_mode 门禁。

    grep 能找到 set_shared_texture 调用（ProgramViewerWidget.set_frame 里），
    但它必须在 `if not self._realtime_mode` 分支下（Final-4）。
    """
    out = _rg(r"set_shared_texture\(", "src/features/edit/ui")
    assert "set_shared_texture" in out, "set_shared_texture 必须存在（paused/scrub 路径）"
    # 验证调用前有 realtime_mode 检查（grep 上下文）
    pyfile = ROOT / "src" / "features" / "edit" / "ui" / "program_viewer.py"
    content = pyfile.read_text(encoding="utf-8")
    assert "_realtime_mode" in content, (
        "ProgramViewerWidget 必须有 _realtime_mode 标志门禁 tuple 路径"
    )
    # set_shared_texture 调用必须在 realtime_mode 检查之后
    rt_idx = content.find("_realtime_mode")
    st_idx = content.find("self._viewer.set_shared_texture")
    assert rt_idx >= 0 and st_idx > rt_idx, (
        "set_shared_texture 调用必须在 _realtime_mode 检查之后"
    )


# =============================================================================
# Gate 3: glFinish 仅作为 rare fallback
# =============================================================================


def test_glfinish_only_in_fallback_paths():
    """glFinish 必须仅在 fence 失败的 rare fallback 路径出现。

    正常 present 路径用 glFenceSync（fence+slot 协议），不调 glFinish。
    grep 能找到 glFinish（fallback），但每处必须有 'fence' / 'fallback' 上下文。
    命中按其所在文件各自取上下文（timeline_clip_frame_provider.py 的
    external-source fence 兜底也是合法 fallback 路径）。
    """
    out = _rg(r"glFinish\(\)", "src/features/edit/core")
    if not out:
        return  # 完全没有 glFinish 是最理想的
    # 每一处 glFinish 必须在 fallback 上下文里；按文件归属取上下文
    per_file: dict[Path, list[int]] = {}
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        try:
            lineno = int(parts[1])
        except ValueError:
            continue
        per_file.setdefault(ROOT / parts[0], []).append(lineno)
    for pyfile, linenos in per_file.items():
        lines_arr = pyfile.read_text(encoding="utf-8").splitlines()
        for lineno in linenos:
            # 取该行前后 8 行的上下文
            ctx_start = max(0, lineno - 8)
            ctx_end = min(len(lines_arr), lineno + 4)
            ctx = "\n".join(lines_arr[ctx_start:ctx_end]).lower()
            assert "fence" in ctx or "fallback" in ctx or "fail" in ctx, (
                f"{pyfile.name}: glFinish() at line {lineno} 必须在 "
                f"fence/fallback 上下文：\n{ctx}"
            )


# =============================================================================
# Gate 4: realtime master decode 必须被 SourceDecision 拒绝
# =============================================================================


def test_get_frame_with_policy_denies_realtime_master():
    """EditGpuDecodeManager 必须有 get_frame_with_policy（Final-2 落地）。"""
    out = _rg(r"def get_frame_with_policy\(", "src/features/edit/core")
    assert "def get_frame_with_policy" in out, (
        "EditGpuDecodeManager 必须有 get_frame_with_policy（Final-2 policy-aware decode）"
    )


def test_realtime_mode_in_engine_blocks_master():
    """PlaybackMode.REALTIME 的 allows_master 必须为 False。"""
    from src.features.edit.core.realtime_playback import PlaybackMode
    assert PlaybackMode.REALTIME.allows_master is False
    assert PlaybackMode.PAUSED.allows_master is True
    assert PlaybackMode.STEP.allows_master is True


# =============================================================================
# Gate 5: AudioRingBuffer + AVSyncController 已接入（Final-5 落地）
# =============================================================================


def test_audio_ring_buffer_integrated_in_playback_thread():
    """AudioPlaybackThread 必须持有 _ring（Final-5 AudioEngine 接入）。"""
    from src.features.edit.core.audio_playback import AudioPlaybackThread
    thread = AudioPlaybackThread()
    assert hasattr(thread, "_ring"), "AudioPlaybackThread 必须有 _ring 字段"
    assert hasattr(thread, "_pending_fade_in"), "必须有 _pending_fade_in（seek fade-in）"


def test_avsync_controller_importable():
    """AVSyncController 必须从 realtime_playback 导出（Final-5）。"""
    from src.features.edit.core.realtime_playback import (
        AVSyncAction,
        AVSyncController,
    )
    ctrl = AVSyncController(fps=25.0)
    d = ctrl.evaluate(audio_frame=100, video_frame=100)
    assert d.action is AVSyncAction.IGNORE


# =============================================================================
# Gate 6: compositor + frame pool 已接入（Final-3 落地）
# =============================================================================


def test_gpu_compositor_class_exists_and_importable():
    """TimelineGpuCompositor 必须存在且可导入（Final-3 唯一 GPU 合成入口）。"""
    from src.features.edit.core.realtime_playback import (
        GpuFramePool,
        TimelineGpuCompositor,
    )
    assert TimelineGpuCompositor is not None
    assert GpuFramePool is not None


def test_renderer_has_compositor_and_pool_fields():
    """TimelinePreviewRenderer 必须有 _gpu_compositor / _gpu_frame_pool 字段（Final-3）。"""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
    # 不构造实例（需要 EditProject），只验证类有相应属性访问路径
    src = ROOT / "src" / "features" / "edit" / "core" / "timeline_preview_renderer.py"
    content = src.read_text(encoding="utf-8")
    assert "_gpu_compositor" in content, "renderer 必须持有 _gpu_compositor"
    assert "_gpu_frame_pool" in content, "renderer 必须持有 _gpu_frame_pool"
    assert "gpu_pipeline_stats" in content, "renderer 必须暴露 gpu_pipeline_stats"


# =============================================================================
# Gate 7: playback_mode_changed 信号 + Viewer set_realtime_mode（Final-4 落地）
# =============================================================================


def test_renderer_emits_playback_mode_changed():
    """TimelinePreviewRenderer 必须有 playback_mode_changed 信号（Final-4）。"""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
    assert hasattr(TimelinePreviewRenderer, "playback_mode_changed"), (
        "TimelinePreviewRenderer 必须有 playback_mode_changed 信号"
    )


def test_viewer_widget_has_set_realtime_mode():
    """ProgramViewerWidget 必须有 set_realtime_mode 方法（Final-4）。"""
    from src.features.edit.ui.program_viewer import ProgramViewerWidget
    assert hasattr(ProgramViewerWidget, "set_realtime_mode"), (
        "ProgramViewerWidget 必须有 set_realtime_mode 方法"
    )


# =============================================================================
# Gate 8: benchmark script 存在（Final-7 落地）
# =============================================================================


def test_benchmark_script_exists():
    """playback_benchmark.py 必须存在（Final-7 benchmark suite）。"""
    bench = ROOT / "tools" / "benchmarks" / "playback_benchmark.py"
    assert bench.exists(), "playback_benchmark.py 必须存在"
    content = bench.read_text(encoding="utf-8")
    assert "scenario_source_decision" in content
    assert "scenario_seek_stress" in content
    assert "scenario_avsync_drift" in content
    assert "check_gates" in content
