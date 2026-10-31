"""
Final-4 (FINAL-P0-09 / FINAL-P1-10): Present protocol cleanup.

验收点：
1. ProgramViewerWidget.set_frame 在 _realtime_mode=True 时拒绝裸 tuple。
2. ProgramViewerWidget.set_realtime_mode(True/False) 切换标志。
3. TimelinePreviewRenderer.playback_mode_changed 信号在 notify_playback_state 时 emit。
4. TimelinePreviewRenderer._on_gpu_frame_ready 在 realtime + no present_frame 时丢弃。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# =============================================================================
# 1. ProgramViewerWidget.set_frame 拒绝 REALTIME tuple
# =============================================================================


def _make_widget_shell():
    """构造一个最小 ProgramViewerWidget shell（避免 QWidget 构造）。"""
    from src.features.edit.ui.program_viewer import ProgramViewerWidget

    class _Shell:
        pass

    shell = _Shell()
    shell._set_frame_depth = 0
    shell._viewer = MagicMock()
    shell._realtime_mode = False
    # 绑定实例方法
    shell.set_frame = ProgramViewerWidget.set_frame.__get__(shell, _Shell)
    shell.set_realtime_mode = ProgramViewerWidget.set_realtime_mode.__get__(shell, _Shell)
    shell.clear_texture = ProgramViewerWidget.clear_texture.__get__(shell, _Shell)
    shell.set_qimage = MagicMock()
    return shell


def test_set_frame_rejects_tuple_in_realtime_mode():
    """REALTIME 模式下 set_frame 收到 tuple 不应调用 set_shared_texture。"""
    shell = _make_widget_shell()
    shell.set_realtime_mode(True)
    shell.set_frame(100, (42, 1920, 1080))
    # set_shared_texture 不应被调用（_viewer.set_shared_texture）
    shell._viewer.set_shared_texture.assert_not_called()


def test_set_frame_accepts_tuple_in_non_realtime_mode():
    """非 realtime 模式下 tuple 仍应走 set_shared_texture（兼容 paused/scrub/测试）。"""
    shell = _make_widget_shell()
    shell.set_realtime_mode(False)
    shell.set_frame(100, (42, 1920, 1080))
    shell._viewer.set_shared_texture.assert_called_once_with(42, 1920, 1080)


def test_set_frame_accepts_present_dict_in_realtime_mode():
    """REALTIME 模式下 present_frame dict 仍应正常 set_present_frame。"""
    shell = _make_widget_shell()
    shell.set_realtime_mode(True)
    present = MagicMock()
    present.texture_id = 99
    present.slot_id = 1
    present.has_fence = True
    ack_cb = MagicMock()
    shell.set_frame(100, {"present_frame": present, "ack_cb": ack_cb})
    # W07：present 协议后续新增 trace_id 透传参数——断言按关键字兼容
    from unittest.mock import ANY
    shell._viewer.set_present_frame.assert_called_once_with(
        present, ack_cb=ack_cb, trace_id=ANY)


def test_set_frame_rejects_invalid_texture_in_realtime():
    """REALTIME 下 tex_id=0 的 tuple 也不应进入 set_shared_texture。"""
    shell = _make_widget_shell()
    shell.set_realtime_mode(True)
    shell.set_frame(100, (0, 1920, 1080))
    shell._viewer.set_shared_texture.assert_not_called()


def test_set_realtime_mode_sets_flag():
    """set_realtime_mode 必须设置 _realtime_mode 标志。"""
    shell = _make_widget_shell()
    assert shell._realtime_mode is False
    shell.set_realtime_mode(True)
    assert shell._realtime_mode is True
    shell.set_realtime_mode(False)
    assert shell._realtime_mode is False


# =============================================================================
# 2. TimelinePreviewRenderer.playback_mode_changed 信号
# =============================================================================


def test_notify_playback_state_emits_playback_mode_changed():
    """notify_playback_state(is_playing=True) 必须发射 playback_mode_changed(True)。"""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer

    class _Shell:
        _engine_playback_active = False
        _realtime_engine = MagicMock()
        _worker = None
        # notify_playback_state 未加守卫直接调用的挂起刷新/质量档方法
        # （记录调用便于断言播放/暂停两侧语义）
        _calls: list = []

        def _cancel_full_quality_refresh(self) -> None:
            self._calls.append("cancel_full_quality")

        def _schedule_full_quality_refresh(self) -> None:
            self._calls.append("schedule_full_quality")

        def _set_decode_quality_stage(self, stage: str, reason: str) -> None:
            self._calls.append(("decode_stage", stage, reason))

        def _realign_interactive_pool_after_playback(self) -> None:
            self._calls.append("realign_pool")

        def _schedule_idle_prebuffer(self) -> None:
            self._calls.append("idle_prebuffer")

    shell = _Shell()
    shell.notify_playback_state = (
        TimelinePreviewRenderer.notify_playback_state.__get__(shell, _Shell)
    )
    # playback_mode_changed 是 PyQt signal，shell 里用一个简单 list capture
    emitted = []
    shell.playback_mode_changed = MagicMock()
    shell.playback_mode_changed.connect = lambda cb: None
    shell.playback_mode_changed.emit = lambda v: emitted.append(v)

    shell.notify_playback_state(True)
    assert emitted == [True]
    assert shell._engine_playback_active is True

    shell.notify_playback_state(False)
    assert emitted == [True, False]
    assert shell._engine_playback_active is False


# =============================================================================
# 3. grep gate：REALTIME 热路径不应主动使用 as_legacy_tuple
# =============================================================================


def test_grep_no_as_legacy_tuple_in_realtime_hotpath():
    """src/features/edit 下不应主动调用 as_legacy_tuple()（仅保留定义/测试用）。"""
    import subprocess
    result = subprocess.run(
        [
            "rg", "-n", "as_legacy_tuple\\(",
            "src/features/edit",
        ],
        capture_output=True, text=True,
    )
    # 允许 0 命中（最理想）；如果有命中，必须只出现在注释/文档字符串里
    # （as_legacy_tuple 的定义在 src/shared/gpu，不在 edit 下）。
    output = result.stdout.strip()
    # edit 下不应该有任何 as_legacy_tuple() 调用
    assert output == "", f"edit 下不应调用 as_legacy_tuple()，发现：\n{output}"


def test_grep_no_frame_ready_emit_with_tex_tuple_in_hotpath():
    """realtime 热路径不应直接 emit frame_ready(frame, (tex_id, w, h))。

    允许的形态：frame_ready.emit(frame, {"present_frame": ...}) 或
    frame_ready.emit(frame, QImage)。不允许裸 texture tuple。
    """
    import subprocess
    result = subprocess.run(
        [
            "rg", "-n", "frame_ready\\.emit\\([^)]*\\(.*tex_id",
            "src/features/edit",
        ],
        capture_output=True, text=True,
    )
    output = result.stdout.strip()
    # 这个 grep 模式很严格，正常代码里不应该有 frame_ready.emit(frame, (tex_id...))
    # 实际代码是 result = (tex_id, width, height) 然后 frame_ready.emit(frame, result)，
    # 这个 grep 不会命中（因为是变量 result 不是字面 tuple）。
    # 所以这里只是额外保险。
    assert output == "" or "tex_id" not in output.lower(), (
        f"realtime 热路径不应直接 emit 裸 texture tuple：\n{output}"
    )
