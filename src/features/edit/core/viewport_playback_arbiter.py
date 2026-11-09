"""VP-B01：Viewport 活动监视器仲裁器（edit 领域，纯 Python 无 Qt）。

计划：docs/plans/2026-11-09-edit-viewport-backend-plan.md §VP-B01。

背景缺口：时间线（``EditPlaybackController`` + edit_page 的
``_audio_manager``）与 Source（``SourceViewerWidget`` 自己的播放时钟 +
``_scrub_audio_manager``）是两条互不感知的播放链——任何一侧起播都不
暂停另一侧，两个 QAudioSink 可同时出声；激活/单双模式/跨页也没有统一
的权威快照（``EditViewport.active_monitor_changed`` 在 app 层无消费者）。

本模块只做领域仲裁，不 import Qt、不持有 QWidget/texture/decoder：

- 快照 ``MonitorSnapshot`` 只含可序列化状态与稳定 ID（source/timeline）；
- 固定策略：单/双布局切换与激活只改显示；任一侧**开始连续播放前**先
  暂停另一侧（音频互斥），**保留对方播放头**（仲裁器绝不发 seek 命令）；
- 播放状态的唯一事实来源是控制器反馈（``report_playback_state``），
  反馈绝不触发新命令（同 revision 不重复请求），也不反向切 active；
  请求被拒/失败时快照如实保持"未播放"，不用 boolean 默认值掩盖失效；
- 拒绝必须带稳定原因键（``REASON_*``），宿主据此回填状态栏；
- 一次交互恰好一次领域请求：每个公开请求入口恰好使
  ``request_revision`` 递增 1（含被拒绝的请求）；命令携带同一 revision，
  适配层可据此丢弃过期批次；
- Deliver 页强制 Timeline 监看（Source 激活/播放被拒）；切其他页按政策
  只暂停 Source，不越权触碰时间线；
- ``mark_shutdown`` 后拒绝一切新请求（B11 关闭序列的第一步）。

适配层见 ``src/features/edit/ui/viewport_playback_coordinator.py``；
本模块不得反向依赖 UI。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Union

# ── 稳定 ID 与枚举值（快照/序列化里只出现这些字符串） ────────────────

SOURCE = "source"
TIMELINE = "timeline"
_MONITORS = (SOURCE, TIMELINE)

LAYOUT_SINGLE = "single"
LAYOUT_DUAL = "dual"
_LAYOUTS = (LAYOUT_SINGLE, LAYOUT_DUAL)

PAGE_EDIT = "edit"
PAGE_DELIVER = "deliver"
PAGE_OTHER = "other"
_PAGES = (PAGE_EDIT, PAGE_DELIVER, PAGE_OTHER)

VIA_EXPLICIT = "explicit"
VIA_MEDIA_POOL_SINGLE_CLICK = "media_pool_single_click"
VIA_MEDIA_POOL_DOUBLE_CLICK = "media_pool_double_click"
VIA_OPEN_IN_SOURCE = "open_in_source"
VIA_TIMELINE_INTERACTION = "timeline_interaction"

# ── 拒绝原因（稳定键；宿主映射到 i18n 文案） ─────────────────────────

REASON_NO_SOURCE_ASSET = "no_source_asset"
REASON_NO_TIMELINE = "no_timeline"
REASON_TEXT_INPUT_ACTIVE = "text_input_active"
REASON_MENU_OPEN = "menu_open"
REASON_CONTENT_ONLY = "content_only"
REASON_PAUSE_FAILED = "pause_failed"
REASON_START_FAILED = "start_failed"
REASON_DELIVER_MODE = "deliver_mode"
REASON_SHUTDOWN = "arbiter_shutdown"
REASON_UNKNOWN_MONITOR = "unknown_monitor"
REASON_UNKNOWN_LAYOUT = "unknown_layout"
REASON_UNKNOWN_PAGE = "unknown_page"

_REASON_TEXT = {
    REASON_NO_SOURCE_ASSET:
        "No source media is open in the viewer.",
    REASON_NO_TIMELINE:
        "No timeline is open for playback.",
    REASON_TEXT_INPUT_ACTIVE:
        "A text input has focus; transport is disabled.",
    REASON_MENU_OPEN:
        "A menu is open; transport is disabled.",
    REASON_CONTENT_ONLY:
        "Single click only updates source content; it does not activate.",
    REASON_PAUSE_FAILED:
        "The other monitor could not be paused; playback was not started.",
    REASON_START_FAILED:
        "The monitor rejected the start command; state is unchanged.",
    REASON_DELIVER_MODE:
        "Deliver page forces Timeline monitoring; Source is hidden.",
    REASON_SHUTDOWN:
        "The viewer arbiter is shutting down; new requests are rejected.",
    REASON_UNKNOWN_MONITOR:
        "Unknown monitor id (expected 'source' or 'timeline').",
    REASON_UNKNOWN_LAYOUT:
        "Unknown layout mode (expected 'single' or 'dual').",
    REASON_UNKNOWN_PAGE:
        "Unknown page mode (expected 'edit', 'deliver' or 'other').",
}


def describe_reason(reason: str) -> str:
    """稳定原因键 → 调试/状态栏文案（宿主可用自己的 i18n 覆盖）。"""
    return _REASON_TEXT.get(reason, reason)


# ── 可序列化状态 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlaybackTrackState:
    """单侧播放状态（只由控制器反馈填充）。"""

    playing: bool = False
    direction: str = "forward"
    speed: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "playing": self.playing,
            "direction": self.direction,
            "speed": self.speed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaybackTrackState":
        return cls(
            playing=bool(data["playing"]),
            direction=str(data.get("direction", "forward")),
            speed=float(data.get("speed", 1.0)),
        )


@dataclass(frozen=True)
class MonitorSnapshot:
    """权威监视器快照：只含可序列化状态与稳定 ID。"""

    layout_mode: str = LAYOUT_SINGLE
    active_monitor: str = TIMELINE
    source_asset_id: str | None = None
    page_mode: str = PAGE_EDIT
    source_playback: PlaybackTrackState = field(
        default_factory=PlaybackTrackState)
    timeline_playback: PlaybackTrackState = field(
        default_factory=PlaybackTrackState)
    request_revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout_mode": self.layout_mode,
            "active_monitor": self.active_monitor,
            "source_asset_id": self.source_asset_id,
            "page_mode": self.page_mode,
            "source_playback": self.source_playback.to_dict(),
            "timeline_playback": self.timeline_playback.to_dict(),
            "request_revision": self.request_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MonitorSnapshot":
        return cls(
            layout_mode=str(data["layout_mode"]),
            active_monitor=str(data["active_monitor"]),
            source_asset_id=data["source_asset_id"],
            page_mode=str(data["page_mode"]),
            source_playback=PlaybackTrackState.from_dict(
                data["source_playback"]),
            timeline_playback=PlaybackTrackState.from_dict(
                data["timeline_playback"]),
            request_revision=int(data["request_revision"]),
        )


# ── 命令（适配层应用到真实控制器；携带 revision 供过期丢弃） ─────────


@dataclass(frozen=True)
class PauseMonitor:
    monitor: str
    revision: int


@dataclass(frozen=True)
class StartMonitor:
    monitor: str
    revision: int


ArbiterCommand = Union[PauseMonitor, StartMonitor]

CommandSink = Callable[[ArbiterCommand], bool]


def _opposite(monitor: str) -> str:
    return TIMELINE if monitor == SOURCE else SOURCE


class ViewportPlaybackArbiter:
    """两路播放互斥与显示策略的单一权威（纯领域，线程亲和由适配层保证）。"""

    def __init__(self, *, command_sink: CommandSink | None = None) -> None:
        self._command_sink: CommandSink | None = command_sink
        self._layout_mode: str = LAYOUT_SINGLE
        self._active_monitor: str = TIMELINE
        self._source_asset_id: str | None = None
        self._page_mode: str = PAGE_EDIT
        self._states: dict[str, PlaybackTrackState] = {
            SOURCE: PlaybackTrackState(),
            TIMELINE: PlaybackTrackState(),
        }
        self._request_revision: int = 0
        self._has_timeline: bool = True
        self._text_input_active: bool = False
        self._menu_open: bool = False
        self._shutdown: bool = False

    # ── 配置 ────────────────────────────────────────────────────────

    def set_command_sink(self, sink: CommandSink | None) -> None:
        """命令出口：返回 True 表示已应用；False 视为控制器失效。"""
        self._command_sink = sink

    def set_availability(self, *, has_timeline: bool | None = None) -> None:
        """可用性输入（Source 可用性由 source_asset_id 推导）。"""
        if has_timeline is not None:
            self._has_timeline = bool(has_timeline)

    def set_source_asset_id(self, asset_id: str | None) -> None:
        """上下文同步：宿主在决策前读取真实 widget 的当前素材。

        与 ``open_source_asset``（一次用户交互、递增 revision）不同，这是
        反馈类输入：适配层在每个决策点把 widget 的 ``current_asset`` 同步
        进快照，避免绕过宿主入口的打开路径（时间线双击 Match Frame、
        程序内换源等）让 ``no_source_asset`` 门控失真。零命令、零递增。
        """
        self._source_asset_id = asset_id

    def set_ui_gates(self, *, text_input_active: bool | None = None,
                     menu_open: bool | None = None) -> None:
        """UI 门（文本框焦点/菜单展开）：只拦截"起播"，不拦截暂停。"""
        if text_input_active is not None:
            self._text_input_active = bool(text_input_active)
        if menu_open is not None:
            self._menu_open = bool(menu_open)

    def mark_shutdown(self) -> None:
        """B11 关闭序列第一步：拒绝一切新请求。"""
        self._shutdown = True

    def is_shutdown(self) -> bool:
        """是否已进入关闭序列（延迟回调据此放弃重启播放）。"""
        return self._shutdown

    # ── 查询 ────────────────────────────────────────────────────────

    def snapshot(self) -> MonitorSnapshot:
        return MonitorSnapshot(
            layout_mode=self._layout_mode,
            active_monitor=self._active_monitor,
            source_asset_id=self._source_asset_id,
            page_mode=self._page_mode,
            source_playback=self._states[SOURCE],
            timeline_playback=self._states[TIMELINE],
            request_revision=self._request_revision,
        )

    @property
    def at_most_one_playing(self) -> bool:
        return not (self._states[SOURCE].playing
                    and self._states[TIMELINE].playing)

    # ── 显示策略（零播放命令） ──────────────────────────────────────

    def set_layout_mode(self, mode: str) -> "ArbiterDecision":
        self._request_revision += 1
        if mode not in _LAYOUTS:
            return self._reject(REASON_UNKNOWN_LAYOUT)
        if self._shutdown:
            return self._reject(REASON_SHUTDOWN)
        self._layout_mode = mode
        return self._accept(())

    def request_activate(self, monitor: str, *,
                         via: str = VIA_EXPLICIT) -> "ArbiterDecision":
        self._request_revision += 1
        if monitor not in _MONITORS:
            return self._reject(REASON_UNKNOWN_MONITOR)
        if self._shutdown:
            return self._reject(REASON_SHUTDOWN)
        if self._page_mode == PAGE_DELIVER and monitor == SOURCE:
            return self._reject(REASON_DELIVER_MODE)
        # 媒体池单击只更新内容，不激活（不抢焦点）——防御性拒绝
        if via == VIA_MEDIA_POOL_SINGLE_CLICK:
            return self._reject(REASON_CONTENT_ONLY)
        self._active_monitor = monitor
        return self._accept(())

    # ── 内容入口（媒体池单击/双击、Open in Source 等统一走这里） ─────

    def open_source_asset(self, asset_id: str | None, *, activate: bool,
                          via: str = VIA_MEDIA_POOL_DOUBLE_CLICK,
                          ) -> "ArbiterDecision":
        self._request_revision += 1
        if self._shutdown:
            return self._reject(REASON_SHUTDOWN)
        if via == VIA_MEDIA_POOL_SINGLE_CLICK and activate:
            # 宿主把单击当激活是接线错误：状态零变化并回因
            return self._reject(REASON_CONTENT_ONLY)
        commands: list[ArbiterCommand] = []
        if asset_id is None:
            # 关闭素材：正在播放的 Source 先停（命令走正常出口）
            if self._states[SOURCE].playing:
                pause = self._pause_side(SOURCE)
                if pause is None:
                    return self._reject(REASON_PAUSE_FAILED)
                commands.append(pause)
            self._source_asset_id = None
            self._states = dict(self._states)
            self._states[SOURCE] = PlaybackTrackState()
            return self._accept(tuple(commands))
        self._source_asset_id = asset_id
        if activate:
            if self._page_mode == PAGE_DELIVER:
                # Deliver 强制 Timeline：内容照常更新，但激活被拒并回因
                return self._reject(REASON_DELIVER_MODE)
            self._active_monitor = SOURCE
        return self._accept(tuple(commands))

    # ── transport 请求 ──────────────────────────────────────────────

    def authorize_play(self, monitor: str) -> "ArbiterDecision":
        """入口自决起播授权：门控 + 暂停对侧，但**不发** Start 命令。

        I01（复查计划）：真实入口链（按钮／Space／JKL／scrub 恢复）各自
        带准备步骤（``_sync_playback_duration``、JKL 档位、音频桥、动态
        trim），不能改成由仲裁器代发 ``play()``。仲裁器只裁决两件事：
        "现在可以播吗"（门控）与"先把对方暂停"（PauseMonitor 命令照常走
        sink 并校验）；起播由入口链本身完成，随后经
        ``report_playback_state`` 回填。``accepted`` 即"入口可以继续"。

        其余语义与 ``request_play`` 一致：同样恰好递增 revision 一次（含
        拒绝），幂等短路（本侧已在播放 → 接受且零命令），对侧暂停失败 →
        ``pause_failed``（本侧零写入）。
        """
        self._request_revision += 1
        if monitor not in _MONITORS:
            return self._reject(REASON_UNKNOWN_MONITOR)
        if self._shutdown:
            return self._reject(REASON_SHUTDOWN)
        gate = self._transport_gate(monitor)
        if gate is not None:
            return self._reject(gate)
        if self._states[monitor].playing:
            return self._accept(())  # 幂等：已在播放，零命令
        other = _opposite(monitor)
        if self._states[other].playing:
            pause = self._pause_side(other)
            if pause is None:
                return self._reject(REASON_PAUSE_FAILED)
            return self._accept((pause,))
        return self._accept(())

    def request_play(self, monitor: str) -> "ArbiterDecision":
        self._request_revision += 1
        if monitor not in _MONITORS:
            return self._reject(REASON_UNKNOWN_MONITOR)
        if self._shutdown:
            return self._reject(REASON_SHUTDOWN)
        gate = self._transport_gate(monitor)
        if gate is not None:
            return self._reject(gate)
        if self._states[monitor].playing:
            return self._accept(())  # 幂等：已在播放，零命令
        commands: list[ArbiterCommand] = []
        other = _opposite(monitor)
        if self._states[other].playing:
            pause = self._pause_side(other)
            if pause is None:
                return self._reject(REASON_PAUSE_FAILED)
            commands.append(pause)
        start = self._start_side(monitor)
        if start is None:
            # 目标端零写入（唯一可能已生效的是对侧 pause，由反馈如实回填）
            return self._reject(
                REASON_START_FAILED if commands else REASON_PAUSE_FAILED)
        commands.append(start)
        return self._accept(tuple(commands))

    def request_pause(self, monitor: str) -> "ArbiterDecision":
        self._request_revision += 1
        return self._pause_request(monitor)

    def request_stop_in_place(self, monitor: str) -> "ArbiterDecision":
        """B05：Viewer 的 Stop = 原位暂停（播放头保留，绝不归零）。"""
        self._request_revision += 1
        return self._pause_request(monitor)

    def _pause_request(self, monitor: str) -> "ArbiterDecision":
        if monitor not in _MONITORS:
            return self._reject(REASON_UNKNOWN_MONITOR)
        if self._shutdown:
            return self._reject(REASON_SHUTDOWN)
        if not self._states[monitor].playing:
            return self._accept(())  # 幂等
        pause = self._pause_side(monitor)
        if pause is None:
            return self._reject(REASON_PAUSE_FAILED)
        return self._accept((pause,))

    # ── 跨页策略 ────────────────────────────────────────────────────

    def set_page_mode(self, page: str) -> "ArbiterDecision":
        self._request_revision += 1
        if page not in _PAGES:
            return self._reject(REASON_UNKNOWN_PAGE)
        if self._shutdown:
            return self._reject(REASON_SHUTDOWN)
        commands: list[ArbiterCommand] = []
        if page == PAGE_DELIVER:
            self._active_monitor = TIMELINE
        if page in (PAGE_DELIVER, PAGE_OTHER):
            # 页面政策：离开 Edit 只暂停 Source（时间线由页面控制器自理）
            if self._states[SOURCE].playing:
                pause = self._pause_side(SOURCE)
                if pause is None:
                    return self._reject(REASON_PAUSE_FAILED)
                commands.append(pause)
        self._page_mode = page
        return self._accept(tuple(commands))

    # ── 反馈（唯一的状态事实来源；绝不发命令、不切 active） ──────────

    def report_playback_state(self, monitor: str, *, playing: bool,
                              direction: str = "forward",
                              speed: float = 1.0) -> MonitorSnapshot:
        if monitor not in _MONITORS:
            raise ValueError(
                f"unknown monitor id: {monitor!r} (expected "
                f"{SOURCE!r} or {TIMELINE!r})")
        self._states = dict(self._states)
        self._states[monitor] = PlaybackTrackState(
            playing=bool(playing), direction=direction, speed=float(speed))
        return self.snapshot()

    # ── 内部 ────────────────────────────────────────────────────────

    def _transport_gate(self, monitor: str) -> str | None:
        if self._page_mode == PAGE_DELIVER and monitor == SOURCE:
            return REASON_DELIVER_MODE
        if monitor == SOURCE and self._source_asset_id is None:
            return REASON_NO_SOURCE_ASSET
        if monitor == TIMELINE and not self._has_timeline:
            return REASON_NO_TIMELINE
        if self._text_input_active:
            return REASON_TEXT_INPUT_ACTIVE
        if self._menu_open:
            return REASON_MENU_OPEN
        return None

    def _pause_side(self, monitor: str) -> ArbiterCommand | None:
        command = PauseMonitor(monitor=monitor,
                               revision=self._request_revision)
        if self._command_sink is None:
            return command
        return command if self._command_sink(command) else None

    def _start_side(self, monitor: str) -> ArbiterCommand | None:
        command = StartMonitor(monitor=monitor,
                               revision=self._request_revision)
        if self._command_sink is None:
            return command
        return command if self._command_sink(command) else None

    def _accept(self, commands: tuple[ArbiterCommand, ...]
                ) -> "ArbiterDecision":
        # revision 已在请求入口恰好递增一次（含拒绝），此处不再动
        return ArbiterDecision(
            accepted=True, reason=None, commands=commands,
            snapshot=self.snapshot())

    def _reject(self, reason: str) -> "ArbiterDecision":
        return ArbiterDecision(
            accepted=False, reason=reason, commands=(),
            snapshot=self.snapshot())


@dataclass(frozen=True)
class ArbiterDecision:
    """一次领域请求的结果：命令批次（按序应用）+ 拒绝原因 + 新快照。"""

    accepted: bool
    reason: str | None
    commands: tuple[ArbiterCommand, ...]
    snapshot: MonitorSnapshot

    @property
    def reason_text(self) -> str:
        return describe_reason(self.reason or "")
