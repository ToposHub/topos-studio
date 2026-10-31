"""P0-02：音频路由图校验作为激活门。

2026-08-27 剪辑页审计：此前 EditPage._sync_audio_tracks_from_timeline 在
snapshot 无效（环/未知 Bus/未知 Send 目标）时只 logger.warning 后继续重建
并激活音频——非法路由被静默当作输出到 Master 播放。

现在路由门行为：
- 新图无效：保留上一张有效图的音频状态（未激活则保持不激活），
  audio_routing_errors 暴露可定位错误；
- 图恢复合法（修复/撤销）：错误清除，同步恢复。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_ui

from src.features.edit.core.timeline_v2 import TimelineSequence, EditTrack, ClipInstance
from src.features.edit.core.clip_params import SpeedParams


def _audio_clip(clip_id: str, track_id: str) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset_{clip_id}",
        track_id=track_id,
        timeline_start=0,
        timeline_duration=48,
        source_start=0,
        source_duration=48,
        speed=SpeedParams(speed=1.0),
    )


def _timeline(output_bus_id=None) -> TimelineSequence:
    a1 = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        clips=(_audio_clip("clipA", "a1"),),
        output_bus_id=output_bus_id,
    )
    return TimelineSequence(timeline_id="seq", name="T", audio_tracks=(a1,))


@pytest.fixture
def no_real_audio(monkeypatch):
    """避免测试创建真实 AudioPlaybackManager（Qt Multimedia 设备探测）。

    门行为断言只依赖 _audio_active/签名/错误元组，均在校验阶段产生，
    早于音频管理器使用；置 None 不影响断言有效性，同时避免长测试进程
    中真实音频设备探测与其他音频测试叠加导致的段错误（P0-08）。
    """
    from src.features.edit.ui.edit_page import EditPage as _EP
    monkeypatch.setattr(_EP, "_ensure_audio_manager", lambda self: None)


def test_invalid_routing_blocks_audio_activation(qapp, no_real_audio) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    # 未知 Bus 目标 → snapshot 无效
    page._current_timeline = _timeline(output_bus_id="no_such_bus")
    page._sync_audio_tracks_from_timeline()

    assert page.audio_routing_errors != ()
    assert any("no_such_bus" in e for e in page.audio_routing_errors)
    # 激活门：非法图不得激活音频（不静默回退 Master）
    assert page._audio_active is False


def test_valid_routing_activates_and_clears_errors(qapp, no_real_audio) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    # 先非法
    page._current_timeline = _timeline(output_bus_id="no_such_bus")
    page._sync_audio_tracks_from_timeline()
    assert page.audio_routing_errors != ()

    # 修复（output 回到 Master）→ 门放行
    page._current_timeline = _timeline(output_bus_id=None)
    page._sync_audio_tracks_from_timeline()
    assert page.audio_routing_errors == ()
    assert page._audio_active is True


def test_last_valid_graph_kept_when_new_revision_invalid(qapp, no_real_audio) -> None:
    """已激活后出现非法 revision：保留旧轨道集合，不移除不重建。"""
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    page._current_timeline = _timeline(output_bus_id=None)
    page._sync_audio_tracks_from_timeline()
    assert page._audio_active is True
    signature_before = page._audio_timeline_signature

    # 新 revision 引入非法路由
    page._current_timeline = _timeline(output_bus_id="ghost_bus")
    page._sync_audio_tracks_from_timeline()

    # 门：保留上一有效图（签名不变），错误可定位
    assert page._audio_timeline_signature == signature_before
    assert any("ghost_bus" in e for e in page.audio_routing_errors)


def test_empty_timeline_clears_gate_errors(qapp, no_real_audio) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    page._current_timeline = _timeline(output_bus_id="no_such_bus")
    page._sync_audio_tracks_from_timeline()
    assert page.audio_routing_errors != ()

    # 移除全部音频 clip（空图合法）
    empty = TimelineSequence(timeline_id="seq2", name="Empty")
    page._current_timeline = empty
    page._sync_audio_tracks_from_timeline()
    assert page.audio_routing_errors == ()
    assert page._audio_active is False
