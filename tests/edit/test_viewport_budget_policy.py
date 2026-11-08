"""VP-B03：双视图预算政策——活动/播放侧优先、隐藏侧静帧、请求尺寸上限。

缺口（计划 VP-B03）：两侧（Timeline/Source）各自有背压与队列，但没有
跨监视器的共享预算政策——谁优先、隐藏侧是否继续周期请求、预览请求按
什么尺寸上限，全靠各处零散判断；也没有一份可序列化的双路诊断快照。

契约（``DualViewBudgetPolicy``，纯领域；实测性能归 B12 真机报告）：

- 播放中的 Timeline 优先：Timeline playing → PLAYHEAD 优先级 + 周期
  请求；Source 非活动 → 静帧策略（禁周期请求，精确一次性 seek 仍走
  B02 的显式路径）；
- Source 播放（B01 单声政策下唯一连续播放侧）→ Source PLAYHEAD，
  Timeline 暂停侧保持静帧策略；
- 隐藏（单视图切换走/页面切走）→ 永远禁周期请求，优先级最低；
- 请求尺寸上限 = min(视口尺寸, 素材原生尺寸)，绝不放大超过原生，未知
  视口回退原生；
- 队列上限：播放侧小（及时合并），静帧侧更小（只留最新）；
- ``collect_dual_view_diagnostics`` 汇总两路的 submit/discard/ack 计数
  与预算决策为一份可 JSON 序列化的快照（饥饿/泄漏可诊断）。
"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.viewport_playback_budget import (
    DualViewBudgetPolicy,
    MonitorLoadState,
    collect_dual_view_diagnostics,
)

pytestmark = pytest.mark.edit_core


def _state(monitor: str, *, active=True, playing=False, hidden=False,
           viewport=None, native=None) -> MonitorLoadState:
    return MonitorLoadState(
        monitor=monitor, active=active, playing=playing, hidden=hidden,
        viewport_size=viewport, native_size=native)


POLICY = DualViewBudgetPolicy()


# ── 播放侧优先 ─────────────────────────────────────────────────────


def test_b03_timeline_playing_gets_priority_source_still_frame():
    decisions = POLICY.decide(
        timeline=_state("timeline", active=True, playing=True,
                        viewport=(1920, 1080), native=(3840, 2160)),
        source=_state("source", active=False, playing=False,
                      viewport=(640, 360), native=(3840, 2160)))
    by = {d.monitor: d for d in decisions}
    assert by["timeline"].allow_periodic_requests is True
    assert by["timeline"].priority < by["source"].priority  # 数值小=优先
    assert by["source"].allow_periodic_requests is False   # 非活动静帧
    assert by["source"].queue_cap < by["timeline"].queue_cap


def test_b03_source_playing_single_voice_timeline_still():
    decisions = POLICY.decide(
        timeline=_state("timeline", active=True, playing=False,
                        viewport=(1920, 1080), native=(3840, 2160)),
        source=_state("source", active=True, playing=True,
                      viewport=(640, 360), native=(3840, 2160)))
    by = {d.monitor: d for d in decisions}
    assert by["source"].allow_periodic_requests is True
    assert by["source"].priority < by["timeline"].priority
    assert by["timeline"].allow_periodic_requests is False


def test_b03_hidden_side_never_periodic_lowest_priority():
    decisions = POLICY.decide(
        timeline=_state("timeline", active=True, playing=True,
                        viewport=(1920, 1080), native=(3840, 2160)),
        source=_state("source", hidden=True, viewport=(640, 360),
                      native=(3840, 2160)))
    by = {d.monitor: d for d in decisions}
    assert by["source"].allow_periodic_requests is False
    assert by["source"].priority == max(
        d.priority for d in decisions)  # 最低优先
    assert by["source"].reason  # 政策原因可解释


def test_b03_both_idle_single_view():
    decisions = POLICY.decide(
        timeline=_state("timeline", active=True, playing=False,
                        viewport=(1920, 1080), native=(1920, 1080)),
        source=_state("source", hidden=True, viewport=(640, 360),
                      native=(1920, 1080)))
    by = {d.monitor: d for d in decisions}
    assert by["timeline"].allow_periodic_requests is False  # 空闲也非周期
    assert by["source"].allow_periodic_requests is False


# ── 请求尺寸上限 ───────────────────────────────────────────────────


def test_b03_request_size_capped_to_viewport_never_upscaled():
    decisions = POLICY.decide(
        timeline=_state("timeline", active=True, playing=True,
                        viewport=(960, 540), native=(3840, 2160)),
        source=_state("source", active=False, viewport=(640, 360),
                      native=(1280, 720)))
    by = {d.monitor: d for d in decisions}
    assert by["timeline"].request_size_cap == (960, 540)   # 视口封顶
    assert by["source"].request_size_cap == (640, 360)
    # 视口大于原生 → 不放大
    decisions = POLICY.decide(
        timeline=_state("timeline", active=True, playing=True,
                        viewport=(3840, 2160), native=(1920, 1080)),
        source=_state("source", hidden=True, viewport=None, native=None))
    by = {d.monitor: d for d in decisions}
    assert by["timeline"].request_size_cap == (1920, 1080)
    assert by["source"].request_size_cap is None  # 未知 → 不设限（原生未知）


# ── 诊断快照 ───────────────────────────────────────────────────────


def test_b03_diagnostics_snapshot_serializable():
    timeline_state = _state("timeline", active=True, playing=True,
                            viewport=(1920, 1080), native=(3840, 2160))
    source_state = _state("source", hidden=True, viewport=(640, 360),
                          native=(3840, 2160))
    decisions = POLICY.decide(timeline=timeline_state, source=source_state)
    snapshot = collect_dual_view_diagnostics(
        decisions,
        counters={
            "timeline": {"decode_submitted": 120, "results_discarded": 4,
                         "slots_acked": 118},
            "source": {"decode_submitted": 1, "results_discarded": 0,
                       "slots_acked": 1},
        },
        states={"timeline": timeline_state, "source": source_state})
    data = json.loads(json.dumps(snapshot))
    assert data["timeline"]["playing"] is True
    assert data["timeline"]["counters"]["decode_submitted"] == 120
    assert data["source"]["hidden"] is True
    assert data["source"]["policy"]["allow_periodic_requests"] is False
    assert set(data) == {"timeline", "source"}
