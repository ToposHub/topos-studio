"""VP-B01：SourceViewerWidget 公开播放状态（权威监视器快照的输入）。

缺口（计划 VP-B01/B04）：Source 只有私有 ``_is_playing``，无公开查询
面——仲裁器的状态反馈（``report_playback_state``）与 B04 的"公开状态
快照"都没有输入来源。补一个只读 property，行为零变化。
"""

from __future__ import annotations

import pytest

from src.features.edit.ui.source_viewer import SourceViewerWidget

pytestmark = pytest.mark.edit_ui


class _StubAsset:
    """无视频轨道的轻量素材：play() 的守卫链全程可走、不触发音频。"""

    is_video = False

    def __getattr__(self, name: str):  # 兜住 toolbar/viewer 侧的属性探测
        return None


def test_b01_source_viewer_exposes_is_playing_property(qtbot):
    widget = SourceViewerWidget()
    qtbot.addWidget(widget)
    widget._current_asset = _StubAsset()  # 公开路径 play() 的前置条件

    assert widget.is_playing is False

    widget.play()
    assert widget.is_playing is True

    widget.pause()
    assert widget.is_playing is False
