"""VP-B01：活动监视器仲裁器——互斥、单请求、拒绝回因（纯领域，无 Qt）。

缺口（计划 VP-B01）：时间线与 Source 两条播放链各持独立音频/时钟，
互相不感知——edit_page 的 `_on_playback_state_for_audio` 与
source_viewer 的 `_start_source_audio` 可同时启动两个 QAudioSink；
激活、单/双模式、跨页没有权威快照，`EditViewport.active_monitor_changed`
在 app 层无人消费。

契约：

- 快照只含可序列化状态与稳定 ID（layout/active/asset/page/两路播放态/
  request_revision），to_dict/from_dict 可 JSON 往返；
- 任一侧开始连续播放前先暂停另一侧，**保留对方播放头**（绝不发 seek）；
- pause 失败 → 目标端保持未播放并回 ``pause_failed``（目标端零写入）；
- 单/双切换与激活只改显示（零播放命令）；媒体池单击不激活
  （``content_only``）；
- 空素材/无时间线/文本框焦点/菜单展开 → 拒绝且带原因，零命令；
- 状态反馈不触发命令（同 revision 不重复请求），也不反向切 active；
- Deliver 强制 Timeline 监看并暂停 Source；切其他页按政策只暂停
  Source（时间线状态不动）；
- 一次交互恰好一次领域请求（revision 恰好 +1，拒绝也算一次）；
  shutdown 后全部请求拒绝。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.features.edit.core.viewport_playback_arbiter import (
    LAYOUT_DUAL,
    LAYOUT_SINGLE,
    PAGE_DELIVER,
    PAGE_EDIT,
    PAGE_OTHER,
    REASON_CONTENT_ONLY,
    REASON_DELIVER_MODE,
    REASON_MENU_OPEN,
    REASON_NO_SOURCE_ASSET,
    REASON_NO_TIMELINE,
    REASON_PAUSE_FAILED,
    REASON_SHUTDOWN,
    REASON_START_FAILED,
    REASON_TEXT_INPUT_ACTIVE,
    SOURCE,
    TIMELINE,
    MonitorSnapshot,
    PauseMonitor,
    StartMonitor,
    ViewportPlaybackArbiter,
)

pytestmark = pytest.mark.edit_core


class _RecordingSink:
    """记录命令；fail_on 中的命令类型返回 False（模拟控制器失效）。"""

    def __init__(self, *fail_on: type) -> None:
        self.calls: list[Any] = []
        self.fail_on = set(fail_on)

    def __call__(self, command: Any) -> bool:
        self.calls.append(command)
        return type(command) not in self.fail_on


class _World:
    """命令真实生效并同步回反馈的假世界（两路播放态 + 播放头）。"""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.playing = {SOURCE: False, TIMELINE: False}
        self.playhead = {SOURCE: 12, TIMELINE: 34}
        self.pause_calls: list[str] = []
        self.start_calls: list[str] = []
        self.fail_start = fail_start
        self.fail_pause = False

    def sink(self, arbiter: ViewportPlaybackArbiter, command: Any) -> bool:
        if isinstance(command, PauseMonitor):
            if self.fail_pause:
                return False  # 控制器拒绝暂停（状态保持播放）
            self.playing[command.monitor] = False
            self.pause_calls.append(command.monitor)
            arbiter.report_playback_state(command.monitor, playing=False)
            return True
        if isinstance(command, StartMonitor):
            if self.fail_start:
                return False  # 控制器拒绝启动（状态保持未播放）
            self.playing[command.monitor] = True
            self.start_calls.append(command.monitor)
            arbiter.report_playback_state(command.monitor, playing=True)
            return True
        raise AssertionError(f"unexpected command {command!r}")


def _arbiter(world: _World | None = None,
             sink: Any | None = None) -> tuple[ViewportPlaybackArbiter, Any]:
    if world is not None:
        arb = ViewportPlaybackArbiter()
        arb.set_command_sink(lambda cmd: world.sink(arb, cmd))
        return arb, world
    rec = sink if sink is not None else _RecordingSink()
    return ViewportPlaybackArbiter(command_sink=rec), rec


# ── 快照：可序列化 + 稳定 ID ────────────────────────────────────────


def test_b01_snapshot_serializable_json_roundtrip():
    arb, _ = _arbiter()
    arb.set_layout_mode(LAYOUT_DUAL)
    arb.open_source_asset("asset-1", activate=True)
    arb.report_playback_state(TIMELINE, playing=True, speed=2.0)

    snap = arb.snapshot()
    data = json.loads(json.dumps(snap.to_dict()))
    revived = MonitorSnapshot.from_dict(data)

    assert revived == snap
    assert snap.source_asset_id == "asset-1"
    assert snap.active_monitor == SOURCE
    assert snap.layout_mode == LAYOUT_DUAL
    assert snap.timeline_playback.playing is True
    assert snap.timeline_playback.speed == 2.0
    assert snap.source_playback.playing is False
    # 快照里没有 QWidget/texture/decoder 一类的不可序列化负担
    assert set(data) == {
        "layout_mode", "active_monitor", "source_asset_id", "page_mode",
        "source_playback", "timeline_playback", "request_revision"}


# ── 互斥：先暂停另一侧，保留播放头 ──────────────────────────────────


def test_b01_play_source_pauses_timeline_first_keeps_head():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    decision = arb.request_play(TIMELINE)
    assert decision.accepted
    assert world.playing[TIMELINE]

    decision = arb.request_play(SOURCE)

    assert decision.accepted
    # 顺序铁律：先 Pause(timeline) 再 Start(source)
    assert isinstance(decision.commands[0], PauseMonitor)
    assert decision.commands[0].monitor == TIMELINE
    assert isinstance(decision.commands[1], StartMonitor)
    assert decision.commands[1].monitor == SOURCE
    assert world.playing == {SOURCE: True, TIMELINE: False}
    # 播放头不动（仲裁器绝不发 seek）
    assert world.playhead == {SOURCE: 12, TIMELINE: 34}
    assert arb.at_most_one_playing


def test_b01_pause_failure_zero_write_on_target():
    rec = _RecordingSink(PauseMonitor)
    arb, rec = _arbiter(sink=rec)
    arb.open_source_asset("asset-1", activate=False)
    arb.report_playback_state(TIMELINE, playing=True)

    decision = arb.request_play(SOURCE)

    assert not decision.accepted
    assert decision.reason == REASON_PAUSE_FAILED
    # 目标端零写入：唯一的命令是那次失败的 pause，没有 Start
    assert [type(c) for c in rec.calls] == [PauseMonitor]
    # 快照不显示正在播放（不掩盖控制器失效）
    assert arb.snapshot().source_playback.playing is False


def test_b01_start_failure_after_pause_is_honest():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(TIMELINE)
    assert world.playing[TIMELINE]
    world.fail_start = True  # 此后控制器拒绝启动（模拟失效）

    decision = arb.request_play(SOURCE)

    assert not decision.accepted
    assert decision.reason == REASON_START_FAILED
    # pause 已真实生效（反馈回填为已暂停），目标端如实保持未播放
    assert world.playing[TIMELINE] is False
    assert arb.snapshot().timeline_playback.playing is False
    assert arb.snapshot().source_playback.playing is False


def test_b01_play_already_playing_target_is_noop():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(TIMELINE)
    before = world.start_calls[:]

    decision = arb.request_play(TIMELINE)

    assert decision.accepted
    assert world.start_calls == before  # 不重复 Start
    assert arb.at_most_one_playing


def test_b01_rapid_alternating_50x_at_most_one_playing():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    target = TIMELINE
    for _ in range(50):
        decision = arb.request_play(target)
        assert decision.accepted
        assert arb.at_most_one_playing, "互斥在每一步都必须成立"
        assert not (world.playing[SOURCE] and world.playing[TIMELINE])
        target = TIMELINE if target == SOURCE else SOURCE


# ── 显示策略：模式/激活零播放命令 ───────────────────────────────────


def test_b01_layout_mode_toggle_is_display_only():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    arb.open_source_asset("asset-1", activate=True)
    arb.report_playback_state(TIMELINE, playing=True)
    before = arb.snapshot()

    for mode in (LAYOUT_DUAL, LAYOUT_SINGLE, LAYOUT_DUAL):
        decision = arb.set_layout_mode(mode)
        assert decision.accepted
        after = arb.snapshot()
        # 只有 layout_mode（和 revision）变化；播放态/active/asset 不动
        assert after.layout_mode == mode
        assert after.active_monitor == before.active_monitor
        assert after.source_asset_id == before.source_asset_id
        assert after.source_playback == before.source_playback
        assert after.timeline_playback == before.timeline_playback
    assert rec.calls == []  # 零播放命令


def test_b01_activate_changes_active_only_no_commands():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    assert arb.snapshot().active_monitor == TIMELINE  # 默认 Timeline

    decision = arb.request_activate(SOURCE)

    assert decision.accepted
    assert arb.snapshot().active_monitor == SOURCE
    assert rec.calls == []


def test_b01_media_pool_single_click_never_steals_active():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)

    decision = arb.open_source_asset(
        "asset-1", activate=True, via="media_pool_single_click")

    assert not decision.accepted
    assert decision.reason == REASON_CONTENT_ONLY
    assert rec.calls == []
    after = arb.snapshot()
    assert after.active_monitor == TIMELINE  # 不抢焦点
    assert after.source_asset_id is None     # 状态零变化（防御性拒绝）


def test_b01_open_in_source_activates_media_pool_double_click():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)

    decision = arb.open_source_asset("asset-1", activate=True)

    assert decision.accepted
    assert arb.snapshot().active_monitor == SOURCE
    assert arb.snapshot().source_asset_id == "asset-1"
    # 只更新内容不激活（单击语义）
    arb.open_source_asset("asset-2", activate=False)
    snap = arb.snapshot()
    assert snap.source_asset_id == "asset-2"
    assert snap.active_monitor == SOURCE


def test_b01_closing_source_asset_pauses_source_playback():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(SOURCE)
    assert world.playing[SOURCE]

    decision = arb.open_source_asset(None, activate=False)

    assert decision.accepted
    assert world.playing[SOURCE] is False
    assert arb.snapshot().source_asset_id is None
    assert arb.snapshot().source_playback.playing is False


# ── 拒绝：空素材/无时间线/焦点/菜单 ────────────────────────────────


def test_b01_play_source_without_asset_rejected():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)

    decision = arb.request_play(SOURCE)

    assert not decision.accepted
    assert decision.reason == REASON_NO_SOURCE_ASSET
    assert rec.calls == []


def test_b01_play_timeline_without_timeline_rejected():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    arb.set_availability(has_timeline=False)
    arb.open_source_asset("asset-1", activate=False)

    decision = arb.request_play(TIMELINE)

    assert not decision.accepted
    assert decision.reason == REASON_NO_TIMELINE
    assert rec.calls == []


def test_b01_text_input_and_menu_gates_block_play_not_pause():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    arb.open_source_asset("asset-1", activate=False)

    arb.set_ui_gates(text_input_active=True)
    blocked = arb.request_play(SOURCE)
    assert not blocked.accepted
    assert blocked.reason == REASON_TEXT_INPUT_ACTIVE

    arb.set_ui_gates(text_input_active=False, menu_open=True)
    blocked = arb.request_play(SOURCE)
    assert not blocked.accepted
    assert blocked.reason == REASON_MENU_OPEN

    # pause 不受门限制（安全侧操作永远放行）
    arb.set_ui_gates(menu_open=True)
    allowed = arb.request_pause(SOURCE)
    assert allowed.accepted
    assert rec.calls == []


# ── 反馈：不重复请求、不递归切 active ──────────────────────────────


def test_b01_feedback_never_emits_commands_or_activation():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    active_before = arb.snapshot().active_monitor

    for _ in range(5):
        arb.report_playback_state(SOURCE, playing=True, speed=4.0)
        arb.report_playback_state(TIMELINE, playing=False)

    assert rec.calls == []
    assert arb.snapshot().active_monitor == active_before
    snap = arb.snapshot()
    assert snap.source_playback.playing is True
    assert snap.source_playback.speed == 4.0


def test_b01_one_interaction_one_domain_request():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    arb.open_source_asset("asset-1", activate=False)

    rev0 = arb.snapshot().request_revision
    arb.request_play(TIMELINE)       # 一次交互（含 pause+start 两条命令）
    rev1 = arb.snapshot().request_revision
    assert rev1 == rev0 + 1

    arb.request_play(TIMELINE)       # 已在播放 → 仍是一次领域请求
    assert arb.snapshot().request_revision == rev1 + 1

    arb.request_play(SOURCE)         # 拒绝也算一次请求（有据可查）
    assert arb.snapshot().request_revision == rev1 + 2

    arb.report_playback_state(SOURCE, playing=True)  # 反馈不算请求
    assert arb.snapshot().request_revision == rev1 + 2


# ── 跨页：Deliver 与其他页 ──────────────────────────────────────────


def test_b01_deliver_mode_forces_timeline_and_pauses_source():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=True)
    arb.request_play(SOURCE)
    assert world.playing[SOURCE]

    decision = arb.set_page_mode(PAGE_DELIVER)

    assert decision.accepted
    snap = arb.snapshot()
    assert snap.page_mode == PAGE_DELIVER
    assert snap.active_monitor == TIMELINE
    assert world.playing[SOURCE] is False
    # Deliver 中 Source 侧激活与播放都被拒绝并回因
    assert arb.request_activate(SOURCE).reason == REASON_DELIVER_MODE
    rejected = arb.request_play(SOURCE)
    assert not rejected.accepted
    assert rejected.reason == REASON_DELIVER_MODE

    back = arb.set_page_mode(PAGE_EDIT)
    assert back.accepted
    assert arb.snapshot().active_monitor == TIMELINE  # 不自动切回


def test_b01_other_page_pauses_source_only():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(TIMELINE)
    arb.request_play(SOURCE)  # 互斥：source 起播会把 timeline 暂停
    assert world.playing[SOURCE]

    decision = arb.set_page_mode(PAGE_OTHER)

    assert decision.accepted
    assert world.playing[SOURCE] is False
    # 时间线播放头/状态归时间线策略管辖，仲裁器不越权发命令
    assert arb.snapshot().timeline_playback.playing is False


# ── shutdown ────────────────────────────────────────────────────────


def test_b01_shutdown_rejects_everything():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    arb.open_source_asset("asset-1", activate=True)
    arb.mark_shutdown()

    for request in (
        lambda: arb.request_play(SOURCE),
        lambda: arb.request_pause(TIMELINE),
        lambda: arb.request_activate(SOURCE),
        lambda: arb.open_source_asset("asset-2", activate=True),
        lambda: arb.set_layout_mode(LAYOUT_DUAL),
        lambda: arb.set_page_mode(PAGE_DELIVER),
    ):
        decision = request()
        assert not decision.accepted
        assert decision.reason == REASON_SHUTDOWN
    assert rec.calls == []


# ── 停止语义：原位（B05 的领域侧入口） ──────────────────────────────


def test_b01_stop_in_place_pauses_without_seeking():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(SOURCE)

    decision = arb.request_stop_in_place(SOURCE)

    assert decision.accepted
    assert [type(c) for c in decision.commands] == [PauseMonitor]
    assert world.playhead[SOURCE] == 12  # 播放头保留（对照 stop() 归零）
    assert arb.snapshot().source_playback.playing is False


def test_b01_unknown_monitor_rejected():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    decision = arb.request_play("program")  # 前端叫法不得混入领域 ID
    assert not decision.accepted
    assert decision.reason == "unknown_monitor"
    with pytest.raises(ValueError):
        arb.report_playback_state("program", playing=True)


# ── I01：入口自决起播授权（authorize_play） ─────────────────────────


def test_i01_authorize_play_pauses_other_without_starting():
    """入口链自带准备步骤：仲裁器只授权 + 暂停对侧，绝不代发起播。"""
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(SOURCE)
    assert world.playing[SOURCE]

    decision = arb.authorize_play(TIMELINE)

    assert decision.accepted
    assert [type(c) for c in decision.commands] == [PauseMonitor]
    assert all(not isinstance(c, StartMonitor) for c in decision.commands)
    assert world.playing[SOURCE] is False   # 对侧已被真实暂停
    assert world.playing[TIMELINE] is False  # 入口自己起播，仲裁器不代发
    assert world.playhead[SOURCE] == 12      # 播放头保留


def test_i01_authorize_play_rejects_when_other_pause_fails():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(SOURCE)
    starts_before = list(world.start_calls)
    world.fail_pause = True

    decision = arb.authorize_play(TIMELINE)

    assert not decision.accepted
    assert decision.reason == REASON_PAUSE_FAILED
    assert world.playing[SOURCE] is True       # 对侧暂停失败，原状保留
    assert world.start_calls == starts_before  # 本侧零启动


def test_i01_authorize_play_idempotent_when_already_playing():
    world = _World()
    arb, world = _arbiter(world)
    arb.open_source_asset("asset-1", activate=False)
    arb.request_play(TIMELINE)

    decision = arb.authorize_play(TIMELINE)  # L 再按 = 变速，不是新起播

    assert decision.accepted
    assert decision.commands == ()
    assert world.pause_calls == []  # 不重复打扰对侧


def test_i01_authorize_play_honors_gates():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    # 无素材 → no_source_asset；文本框聚焦 → text_input_active
    no_asset = arb.authorize_play(SOURCE)
    assert not no_asset.accepted and no_asset.reason == REASON_NO_SOURCE_ASSET
    arb.set_source_asset_id("asset-1")
    arb.set_ui_gates(text_input_active=True)
    gated = arb.authorize_play(SOURCE)
    assert not gated.accepted and gated.reason == REASON_TEXT_INPUT_ACTIVE


def test_i01_authorize_play_counts_as_one_request():
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    arb.set_source_asset_id("asset-1")
    before = arb.snapshot().request_revision

    arb.authorize_play(SOURCE)
    accepted_rev = arb.snapshot().request_revision
    arb.set_ui_gates(menu_open=True)
    arb.authorize_play(SOURCE)

    assert accepted_rev - before == 1          # 接受也算一次请求
    assert arb.snapshot().request_revision - accepted_rev == 1  # 拒绝也算


def test_i01_set_source_asset_id_is_context_not_request():
    """决策前上下文同步：零命令、零 revision（对照 open_source_asset）。"""
    rec = _RecordingSink()
    arb, rec = _arbiter(sink=rec)
    arb.open_source_asset("asset-1", activate=False)
    rev = arb.snapshot().request_revision

    arb.set_source_asset_id(None)  # 绕过宿主入口的换源/关闭

    assert arb.snapshot().request_revision == rev
    assert rec.calls == []
    assert arb.snapshot().source_asset_id is None
