"""E06: 播放状态信号桥接。

审计实测：``TimelinePanel.playback_state_changed`` 有接收者（MG 缓存互斥）
但 ``_on_controller_state_changed`` 从不 emit——播放期间外部监听者收到
0 次状态。

回归锁定：控制器状态 PLAYING/PAUSED/STOPPED 变化触发信号（去重：相同
状态不重复发射）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os_env_offscreen = True  # noqa: F841 语义标记：conftest 已设 offscreen

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.features.edit.core.edit_playback_controller import (  # noqa: E402
    PlaybackState,
)
from src.features.edit.ui.timeline_panel import TimelinePanel  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _make_panel() -> TimelinePanel:
    panel = TimelinePanel()
    # 视图在 offscreen 下未显示；_on_controller_state_changed 只触达
    # timeline_view 的缩略图门控与 timer/服务，不要求可见。
    return panel


class TestPlaybackStateBridge:
    def test_state_changes_emit_signal_with_dedup(self) -> None:
        panel = _make_panel()
        received: list[tuple[bool, float, int]] = []
        panel.playback_state_changed.connect(
            lambda playing, speed, direction: received.append(
                (playing, speed, direction)))

        panel._on_controller_state_changed(PlaybackState.PLAYING.value)
        panel._on_controller_state_changed(PlaybackState.PLAYING.value)  # 去重
        panel._on_controller_state_changed(PlaybackState.PAUSED.value)
        panel._on_controller_state_changed(PlaybackState.STOPPED.value)  # 同为非播放→去重

        assert [r[0] for r in received] == [True, False]
        assert received[0][2] == 1  # forward

    def test_initial_state_is_not_playing(self) -> None:
        panel = _make_panel()
        assert panel._last_emitted_playback_state is False
