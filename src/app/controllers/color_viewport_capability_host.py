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
        # 最近一次成功回填的状态快照（诊断/测试用；可序列化）。
        self.last_state: ColorViewerState | None = None
        self.last_capabilities: dict[str, ColorViewerCapability] = {}

    # ── 安装与上下文 ───────────────────────────────────────────────────

    def install(self) -> bool:
        """绑定 canvas chrome；失败返回 False（宿主缺席时不假装可用）。"""
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
            self._connected = True
        self.refresh()
        return True

    def bump_context_revision(self) -> int:
        """切 clip／换素材／换工程时推进；进行中的旧回填将被丢弃。"""
        self._revision += 1
        return self._revision

    # ── 事实聚合 ───────────────────────────────────────────────────────

    def _gather_state(self) -> ColorViewerState:
        window = self._window
        page = str(getattr(window, "current_page", "Color") or "Color")

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
        if callable(context):
            try:
                active = context()
            except Exception:  # noqa: BLE001 —— 上下文读取失败不阻塞快照
                active = None
            if active:
                timeline_id = str(active.get("timeline_id") or "") or None
                clip_id = str(getattr(active.get("clip"), "clip_id", "") or "") or None

        playing = bool(getattr(video_controller, "is_playing", False))
        loop_enabled = bool(getattr(video_controller, "loop_playback", False))
        if video_controller is None and bar is not None:
            playing = bool(getattr(bar, "is_playing", False))
            loop_enabled = bool(getattr(bar.btn_loop, "isChecked", lambda: False)())

        current_frame = int(getattr(video_controller, "current_frame", None)
                            or getattr(bar, "current_frame", 0) or 0)
        total_frames = int(getattr(video_controller, "total_frames", None)
                           or getattr(bar, "total_frames", 0) or 0)
        frame_rate = float(getattr(bar, "_fps", 24.0) or 24.0)

        return ColorViewerState(
            page=page,
            work_mode=work_mode,
            presentation_path=path,
            time_domain=default_time_domain_for(path),
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

        return ColorViewerCapabilityContext(
            state=state,
            engine_available=getattr(self._canvas, "engine", None) is not None,
            video_controller_available=(
                getattr(self._window, "video_controller", None) is not None),
            focus_host_available=focus_host_available,
            workspace_service_available=(
                getattr(toolbar, "_workspace_provider", None) is not None),
            timeline_choice_count=timeline_choice_count,
        )

    # ── 评估与回填 ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """聚合 → 评估 → 回填；评估期间 revision 变化则丢弃结果。"""
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
