"""CV-B01：Color Viewer 能力宿主——聚合真实事实并回填 chrome／菜单。

职责（计划 CV-B01）：

- 由 app host 聚合事实（页面／工作模式／呈现路径／引擎与控制器在场、
  菜单服务位），经纯领域注册表评估后回填 UI；UI 只发请求、收结果；
- ``context_revision`` 在切 clip／换工程／换素材时推进，评估期间上下文
  再次变化时丢弃旧结果（旧回填不覆盖新状态）；
- 无 handler 的动作（Split Screen 多图、Unmix、Mute、Power Window 按钮
  等）即使领域位为真也保持禁用，原因 ``no_handler``——不虚亮。

本宿主不创建工程、播放时钟或 renderer；旧 View／Wipe／Mask／Guides/
Workspace 行为仍走原信号链。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject

from src.features.color.core.color_viewer_capabilities import (
    PRESENTATION_COMPOSITE,
    PRESENTATION_IMAGE,
    PRESENTATION_SOURCE_GL,
    PRESENTATION_TIMELINE_PREVIEW,
    PRESENTATION_NONE,
    TIME_DOMAIN_CLIP_LOCAL,
    TIME_DOMAIN_SOURCE,
    TIME_DOMAIN_TIMELINE,
    ColorViewerCapability,
    ColorViewerCapabilityContext,
    ColorViewerState,
    color_capability_by_action,
    default_time_domain_for,
    evaluate_color_viewer_capabilities,
)

logger = logging.getLogger(__name__)

# 已接线到真实 handler 的动作清单：注册表 supported 之外的第二道门。
# 未列出的动作一律 no_handler 禁用（诚实禁用，不虚亮）。
_HANDLED_ACTIONS = frozenset({
    "view.zoom_in", "view.zoom_out", "view.fit", "view.actual_size",
    "view.guides", "view.fullscreen", "view.focus_mode",
    "view.highlight", "view.split_wipe", "view.ab_toggle",
    "view.capture_reference", "view.clear_reference",
    "view.capture_still", "view.auto_color", "view.workspace",
    "timeline.switch",
    "transport.play", "transport.first", "transport.last",
    "transport.next", "transport.prev", "transport.scrub",
    "transport.loop", "transport.timecode",
    # CV-B05：Power Window 线框 overlay 开关（chrome 按钮→
    # Canvas.set_overlay_visible，真实后端）。
    "overlay.power_windows",
    # CV-B06：监看静音（chrome mute 按钮 → EditPage Master 总线）。
    "monitor.mute",
})

_NO_HANDLER_REASON = "This action has no handler wired yet."


class ColorViewportCapabilityHost(QObject):
    """聚合 Color 视口真实状态 → 能力注册表 → chrome 回填。"""

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self._window = window
        self._revision = 0
        self._canvas = None
        self._chrome = None
        self._toolbar = None
        self._connected = False
        # CV-B10：关闭后一切刷新请求失效（晚到回调不再触碰销毁中的
        # chrome/控件）。
        self._shutdown = False
        # 最近一次成功回填的状态快照（诊断/测试用；可序列化）。
        self.last_state: ColorViewerState | None = None
        self.last_capabilities: dict[str, ColorViewerCapability] = {}

    def mark_shutdown(self) -> None:
        """关门：断开刷新请求，此后 refresh()/install() 均为 no-op。"""
        self._shutdown = True
        chrome = self._chrome
        if chrome is not None:
            try:
                chrome.capability_refresh_requested.disconnect(self.refresh)
            except TypeError:
                pass
            try:
                chrome.monitor_mute_toggled.disconnect(
                    self._on_monitor_mute_requested)
            except TypeError:
                pass
        toolbar = self._toolbar
        if toolbar is not None:
            try:
                toolbar.view_menu.aboutToShow.disconnect(self.refresh)
            except TypeError:
                pass

    # ── 安装与上下文 ───────────────────────────────────────────────────

    def install(self) -> bool:
        """绑定 canvas chrome；失败返回 False（宿主缺席时不假装可用）。"""
        if self._shutdown:
            return False
        canvas = getattr(self._window, "canvas", None)
        chrome = getattr(canvas, "color_viewport_chrome", None)
        toolbar = getattr(canvas, "viewport_menu", None)
        if chrome is None or toolbar is None:
            return False
        self._canvas = canvas
        self._chrome = chrome
        self._toolbar = toolbar
        if not self._connected:
            chrome.capability_refresh_requested.connect(self.refresh)
            # 菜单打开时重新评估：checked 只反映后端实际生效状态。
            toolbar.view_menu.aboutToShow.connect(self.refresh)
            # CV-B06：监看静音请求路由到 EditPage Master 总线服务。
            chrome.monitor_mute_toggled.connect(self._on_monitor_mute_requested)
            self._connected = True
        self.refresh()
        return True

    def _on_monitor_mute_requested(self, muted: bool) -> None:
        """chrome 静音按钮 → 真实服务；失败如实（不虚报 checked）。"""
        if self._shutdown:
            return
        setter = getattr(self._window, "_set_color_monitor_mute", None)
        if not callable(setter):
            return
        try:
            setter(bool(muted))
        except Exception:  # noqa: BLE001 —— 服务失败不崩 UI，回填将如实
            logger.warning("monitor mute request failed", exc_info=True)
        self.refresh()

    def bump_context_revision(self) -> int:
        """切 clip／换素材／换工程时推进；进行中的旧回填将被丢弃。"""
        self._revision += 1
        return self._revision

    # ── 事实聚合 ───────────────────────────────────────────────────────

    def _timeline_controller(self):
        """共享 Edit 时间线播放控制器（TimelinePreview／Composite 权威）。"""
        panel = getattr(self._window, "timeline_panel", None)
        getter = getattr(panel, "get_playback_controller", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001 —— 控制器缺席＝该域事实缺席
            return None

    def _timeline_total_frames(self) -> int:
        panel = getattr(self._window, "timeline_panel", None)
        for name in ("get_total_frames", "total_frames"):
            getter = getattr(panel, name, None)
            if callable(getter):
                try:
                    value = getter()
                    if value:
                        return int(value)
                except Exception:  # noqa: BLE001
                    continue
        return 0

    def _clip_local_position(self, source_frame, clip) -> int:
        """源帧 → clip-local 位置（权威映射经 FileOps；缺席按偏移近似）。"""
        if source_frame is None:
            return 0
        to_local = getattr(self._window, "_color_source_frame_to_local_frame",
                           None)
        if callable(to_local):
            try:
                return max(0, int(to_local(int(source_frame))))
            except Exception:  # noqa: BLE001 —— 映射失败回落偏移推导
                pass
        start = int(getattr(clip, "source_start", 0) or 0)
        return max(0, int(source_frame) - start)

    def _clip_local_total(self, clip, source_total) -> int:
        """clip-local 总长（与 transport bar 同源：_color_timeline_clip_total_frames）。"""
        getter = getattr(self._window, "_color_timeline_clip_total_frames",
                         None)
        if callable(getter):
            try:
                total = getter()
                if total:
                    return int(total)
            except Exception:  # noqa: BLE001
                pass
        for attr in ("timeline_duration", "duration", "source_duration"):
            value = getattr(clip, attr, None)
            if value:
                return int(value)
        return int(source_total or 0)

    def _gather_state(self) -> ColorViewerState:
        window = self._window
        page = str(getattr(window, "current_page", "Color") or "Color")
        mute_active = getattr(window, "_color_monitor_mute_is_active", None)
        monitor_muted = bool(mute_active()) if callable(mute_active) else False

        work_mode = "all"
        get_mode = getattr(window, "_get_work_mode", None)
        if callable(get_mode):
            try:
                work_mode = str(get_mode() or "all")
            except Exception:  # noqa: BLE001 —— 配置读取失败回落 all
                work_mode = "all"

        canvas = self._canvas
        engine = getattr(canvas, "engine", None)
        video_controller = getattr(window, "video_controller", None)
        bar = getattr(canvas, "video_control_bar", None)

        if page == "Composite":
            path = PRESENTATION_COMPOSITE
        elif bool(getattr(canvas, "_timeline_preview_active", False)):
            path = PRESENTATION_TIMELINE_PREVIEW
        elif (video_controller is not None
              and getattr(window, "current_file_is_video", False)):
            path = PRESENTATION_SOURCE_GL
        elif (getattr(engine, "img_full_original", None) is not None
              or getattr(window, "current_file_path", None) is not None):
            path = PRESENTATION_IMAGE
        else:
            path = PRESENTATION_NONE

        context = getattr(window, "_active_color_timeline_clip_playback_context", None)
        clip_id: str | None = None
        timeline_id: str | None = None
        active_clip = None
        if callable(context):
            try:
                active = context()
            except Exception:  # noqa: BLE001 —— 上下文读取失败不阻塞快照
                active = None
            if active:
                timeline_id = str(active.get("timeline_id") or "") or None
                active_clip = active.get("clip")
                clip_id = (str(getattr(active_clip, "clip_id", "") or "")
                           or None)

        # R05（2026-11-09 复查）：transport 事实按呈现路径取**唯一权威**
        # 控制器与域——不再把源 video_controller 的全域帧数混进声明为
        # clip-local 的快照，也不再用源控制器判定 TimelinePreview／
        # Composite 的可播性。
        playing = False
        loop_enabled = False
        current_frame = 0
        total_frames = 0
        frame_rate = float(getattr(bar, "_fps", 24.0) or 24.0)
        time_domain = default_time_domain_for(path)

        if path == PRESENTATION_SOURCE_GL:
            playing = bool(getattr(video_controller, "is_playing", False))
            loop_enabled = bool(
                getattr(video_controller, "loop_playback", False))
            source_current = getattr(video_controller, "current_frame", None)
            source_total = getattr(video_controller, "total_frames", None)
            if active_clip is not None:
                # 有 clip：位置/总长映射为 clip-local（与 bar 同域同源）。
                time_domain = TIME_DOMAIN_CLIP_LOCAL
                current_frame = self._clip_local_position(
                    source_current, active_clip)
                total_frames = self._clip_local_total(
                    active_clip, source_total)
            else:
                # 独立源：显式 source 帧域。
                time_domain = TIME_DOMAIN_SOURCE
                current_frame = int(source_current or 0)
                total_frames = int(source_total or 0)
            if video_controller is None and bar is not None:
                playing = bool(getattr(bar, "is_playing", False))
                loop_enabled = bool(
                    getattr(bar.btn_loop, "isChecked", lambda: False)())
            if source_current is None and bar is not None:
                current_frame = int(getattr(bar, "current_frame", 0) or 0)
            if source_total is None and bar is not None:
                total_frames = int(getattr(bar, "total_frames", 0) or 0)
        elif path in (PRESENTATION_TIMELINE_PREVIEW, PRESENTATION_COMPOSITE):
            # TimelinePreview／Composite：共享时间线控制器是权威（源
            # video_controller 与此域无关，不得参与判定）。
            controller = self._timeline_controller()
            if controller is not None:
                playing = bool(getattr(controller, "is_playing", False))
                loop_enabled = bool(
                    getattr(controller, "loop_enabled", False))
                current_frame = int(
                    getattr(controller, "current_frame", 0) or 0)
                total_frames = self._timeline_total_frames()
                time_domain = TIME_DOMAIN_TIMELINE
            elif bar is not None:
                # 无时间线控制器：回落 bar 事实（可能为 0——如实，不假装）。
                playing = bool(getattr(bar, "is_playing", False))
                loop_enabled = bool(
                    getattr(bar.btn_loop, "isChecked", lambda: False)())
                current_frame = int(getattr(bar, "current_frame", 0) or 0)
                total_frames = int(getattr(bar, "total_frames", 0) or 0)

        # Zero is an authoritative controller value (start / unloaded source),
        # not a reason to reuse a previous transport position or duration.
        current_frame = int(current_frame or 0)
        total_frames = int(total_frames or 0)

        return ColorViewerState(
            page=page,
            work_mode=work_mode,
            presentation_path=path,
            time_domain=time_domain,
            context_revision=self._revision,
            has_media=path != PRESENTATION_NONE,
            playing=playing,
            loop_enabled=loop_enabled,
            current_frame=current_frame,
            total_frames=total_frames,
            frame_rate=frame_rate,
            clip_id=clip_id,
            timeline_id=timeline_id,
            has_reference_still=bool(getattr(engine, "reference_is_still", False)),
            split_view_active=bool(getattr(engine, "split_view_active", False)),
            show_mask_mode=bool(getattr(engine, "show_mask_mode", False)),
            overlays_visible=bool(getattr(canvas, "_show_mask_overlays", True)),
            monitor_muted=monitor_muted,
        )

    def _gather_context(self, state: ColorViewerState
                        ) -> ColorViewerCapabilityContext:
        toolbar = self._toolbar
        focus_host_available = False
        resolver = getattr(toolbar, "_focus_mode_host", None)
        if callable(resolver):
            try:
                focus_host_available = resolver() is not None
            except Exception:  # noqa: BLE001 —— 宿主缺席＝能力缺席
                focus_host_available = False
        selector = getattr(toolbar, "_timeline_selector", None)
        try:
            timeline_choice_count = int(selector.count())
        except Exception:  # noqa: BLE001
            timeline_choice_count = 0

        # R05：transport 可用性按呈现路径取该域的权威控制器——
        # TimelinePreview／Composite 看共享时间线控制器（只有 Timeline
        # 控制器在场时 transport 不再被误禁用）；Source-GL 看源控制器。
        if state.presentation_path in (PRESENTATION_TIMELINE_PREVIEW,
                                       PRESENTATION_COMPOSITE):
            transport_available = self._timeline_controller() is not None
        else:
            transport_available = (
                getattr(self._window, "video_controller", None) is not None)

        return ColorViewerCapabilityContext(
            state=state,
            engine_available=getattr(self._canvas, "engine", None) is not None,
            video_controller_available=transport_available,
            focus_host_available=focus_host_available,
            workspace_service_available=(
                getattr(toolbar, "_workspace_provider", None) is not None),
            timeline_choice_count=timeline_choice_count,
            # CV-B05：源 metadata 的 alpha 旗标（CV-B02 探测路径顺手记录；
            # 无媒体/未探测时为 False）。
            media_has_alpha=bool(getattr(
                self._window, "_color_media_has_alpha", False)),
            # CV-B06：监看静音服务在 FileOpsMixin（EditPage Master 总线）。
            mute_service_available=callable(getattr(
                self._window, "_set_color_monitor_mute", None)),
        )

    # ── 评估与回填 ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """聚合 → 评估 → 回填；评估期间 revision 变化则丢弃结果。"""
        if self._shutdown:
            return
        chrome = self._chrome
        if chrome is None:
            return
        revision = self._revision
        try:
            state = self._gather_state()
            context = self._gather_context(state)
            caps = color_capability_by_action(
                evaluate_color_viewer_capabilities(context))
        except Exception:  # noqa: BLE001 —— 聚合失败不崩 UI，但如实记录
            logger.warning("color viewer capability refresh failed",
                           exc_info=True)
            return
        if revision != self._revision:
            # 上下文在聚合期间再次变化：旧回填不覆盖新状态。
            return
        caps = {
            action: (cap if cap.supported and action in _HANDLED_ACTIONS
                     else ColorViewerCapability(
                         action, False,
                         cap.reason if not cap.supported
                         else _NO_HANDLER_REASON,
                         cap.checked))
            for action, cap in caps.items()
        }
        chrome.refresh_capabilities(caps)
        self.last_state = state
        self.last_capabilities = caps


def refresh_color_viewer_capabilities(window) -> None:
    """宿主已安装时刷新一次；未安装时静默跳过（getattr 守卫调用点用）。"""
    host = getattr(window, "_color_viewport_capability_host", None)
    refresh = getattr(host, "refresh", None)
    if callable(refresh):
        refresh()


def bump_color_viewer_context(window) -> None:
    """切 clip／换素材／换工程：推进 revision 并刷新。"""
    host = getattr(window, "_color_viewport_capability_host", None)
    bump = getattr(host, "bump_context_revision", None)
    if callable(bump):
        bump()
    refresh_color_viewer_capabilities(window)


def install_color_viewport_capability_host(window
                                           ) -> ColorViewportCapabilityHost:
    """创建宿主并尝试安装；canvas 缺席时宿主仍返回（install 为 False）。"""
    from PyQt6.QtCore import QObject as _QObject

    parent = window if isinstance(window, _QObject) else None
    host = ColorViewportCapabilityHost(window, parent=parent)
    try:
        host.install()
    except Exception:  # noqa: BLE001 —— 安装失败不阻断宿主创建
        logger.warning("color viewport capability host install failed",
                       exc_info=True)
    return host


__all__ = [
    "ColorViewportCapabilityHost",
    "install_color_viewport_capability_host",
    "refresh_color_viewer_capabilities",
    "bump_color_viewer_context",
]
