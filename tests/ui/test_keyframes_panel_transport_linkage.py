"""E2E 测试：Keyframes 面板播放按钮必须与 viewport 播放条联动。

背景：Composite 页有三条播放路径——Edit 时间线控制器（时间线联动合成）、
comp-clip 独立定时器、面板内部定时器。旧实现把面板播放按钮固定接到
comp-clip 定时器上，而时间线联动场景里 viewport 播放条由
EditPlaybackController 驱动——两边各自为政，面板播放时 viewport 播放条
纹丝不动，用户观感就是"无法播放"。

修复后：app 层把与 viewport 播放条同款的引擎以 transport delegate 注入
CompositePage（``set_keyframes_transport``），面板播放/暂停优先作用在该
引擎上；无委托时才回退 comp-clip 引擎。
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.app.controllers.page_controller import _KeyframesTimelineTransport
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel


CLIP_START = 1000   # comp clip 在时间线上的起始帧（全局坐标）
CLIP_FRAMES = 100


class FakeTimelineController:
    """模拟 EditPlaybackController 的最小接口。"""

    def __init__(self) -> None:
        self.is_playing = False
        self.calls: list[str] = []
        self.playback_range = None
        self.current_frame = 0
        self.seeks: list[int] = []

    def play(self) -> None:
        self.is_playing = True
        self.calls.append("play")

    def pause(self) -> None:
        self.is_playing = False
        self.calls.append("pause")

    def set_playback_range(self, start, end) -> None:
        self.playback_range = (start, end)

    def seek(self, frame: int) -> None:
        self.current_frame = int(frame)
        self.seeks.append(int(frame))


@pytest.fixture
def page_with_panel(qapp):
    page = CompositePage()
    page._composite_duration_frames = CLIP_FRAMES
    panel = KeyframesPanel()
    page.set_keyframes_panel(panel)
    # P1-UI-02 规格裁决：空 Composite 工作区会把页面与面板都置为
    # _composition_active=False（空态 CTA，播放被门禁）；本组测试验证的是
    # "组合已打开"的联动场景，经页面级入口激活（会同步传播到面板），
    # 对齐真实接线（app 层打开组合后面板才可交互）。
    page.set_composition_active(True)
    return page, panel


# ── 面板播放驱动 viewport 同款引擎 ──────────────────────────────────────

def test_panel_play_drives_timeline_transport(page_with_panel):
    page, panel = page_with_panel
    ctrl = FakeTimelineController()
    page.set_keyframes_transport(
        _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES))

    panel._act_play.setChecked(True)

    assert ctrl.calls == ["play"], "面板播放必须驱动时间线控制器"
    # 播放范围约束到 clip 区间（全局帧坐标），与播放条 _on_play 一致
    assert ctrl.playback_range == (CLIP_START, CLIP_START + CLIP_FRAMES - 1)
    assert panel._act_play.isChecked()
    # comp-clip 定时器不应被启动
    assert not getattr(page, "_comp_playing", False)


def test_panel_pause_stops_timeline_transport(page_with_panel):
    page, panel = page_with_panel
    ctrl = FakeTimelineController()
    page.set_keyframes_transport(
        _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES))

    panel._act_play.setChecked(True)
    panel._act_play.setChecked(False)

    assert ctrl.calls == ["play", "pause"]
    assert not ctrl.is_playing
    assert not panel._act_play.isChecked()


def test_external_play_state_syncs_panel_button(page_with_panel):
    """用户在 viewport 播放条点播放 → 面板按钮状态同步（双向联动）。"""
    page, panel = page_with_panel

    page.notify_external_play_state(True)
    assert panel._act_play.isChecked()
    page.notify_external_play_state(False)
    assert not panel._act_play.isChecked()


def test_no_transport_falls_back_to_comp_engine(page_with_panel):
    """独立 comp clip（无时间线控制器）：回退 comp-clip 定时器。"""
    page, panel = page_with_panel
    page.set_keyframes_transport(None)

    panel._act_play.setChecked(True)
    assert getattr(page, "_comp_playing", False)
    page.comp_pause()


def test_broken_transport_falls_back_to_comp_engine(page_with_panel):
    """委托抛异常时自动降级到 comp-clip 引擎，播放不中断。"""
    page, panel = page_with_panel

    class Broken:
        def play(self):
            raise RuntimeError("boom")

        def pause(self):
            raise RuntimeError("boom")

        def is_playing(self):
            raise RuntimeError("boom")

    page.set_keyframes_transport(Broken())
    panel._act_play.setChecked(True)
    assert getattr(page, "_comp_playing", False)
    assert page._keyframes_transport is None  # 坏委托被摘除
    page.comp_pause()


# ── 播放头拖拽 seek 链路 ─────────────────────────────────────

def test_adapter_seek_converts_local_to_global(page_with_panel):
    """适配器 seek：clip 本地帧 + clip 起点 → 时间线全局帧。"""
    ctrl = FakeTimelineController()
    t = _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES)
    t.seek(42)
    assert ctrl.seeks == [CLIP_START + 42]


def test_adapter_seek_pauses_while_playing(page_with_panel):
    """与 viewport 播放条 scrub 语义一致：播放中拖拽先暂停。"""
    ctrl = FakeTimelineController()
    ctrl.is_playing = True
    t = _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES)
    t.seek(10)
    assert not ctrl.is_playing
    assert ctrl.calls == ["pause"]


def test_panel_playhead_drag_seeks_transport(page_with_panel):
    """拖拽面板播放头 → 驱动时间线控制器（而不是只改内部帧计数）。"""
    page, panel = page_with_panel
    ctrl = FakeTimelineController()
    page.set_keyframes_transport(
        _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES))

    panel.seek_to_frame(30)  # 用户拖拽播放头（会 emit current_frame_changed）

    assert ctrl.seeks == [CLIP_START + 30], "拖拽必须 seek 时间线控制器"


def test_timeline_echo_syncs_panels_without_reseek(page_with_panel):
    """时间线回显：更新面板播放头且不反向 seek（无回环，播放中安全）。"""
    page, panel = page_with_panel
    ctrl = FakeTimelineController()
    ctrl.is_playing = True  # 模拟播放中
    page.set_keyframes_transport(
        _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES))

    # 模拟时间线 tick 回显（set_timeline_clip_context 的核心行为）
    page._apply_composite_frame(55)

    assert page._composite_current_frame == 55
    assert panel._state.current_frame == 55
    assert ctrl.seeks == [], "回显绝不能反向 seek 时间线（否则打断播放）"
    assert ctrl.is_playing, "播放状态不受回显影响"


def test_seek_roundtrip_single_shot(page_with_panel):
    """完整往返：面板 seek → 控制器 → 回显 → 只 seek 一次。"""
    page, panel = page_with_panel
    ctrl = FakeTimelineController()
    page.set_keyframes_transport(
        _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES))

    panel.seek_to_frame(20)
    page._apply_composite_frame(20)  # 控制器回显

    assert ctrl.seeks == [CLIP_START + 20], "往返只应产生一次 seek"
    assert page._composite_current_frame == 20
    assert panel._state.current_frame == 20
