"""CV-B01：Color Viewer 状态／能力快照——纯领域单一事实源。

缺口（计划 CV-B01）：Color 视口的菜单／transport／chrome 按钮可用性
散落各处（部分硬编码 disabled、部分只看控件自身状态），没有以
``presentation_path``／``time_domain``／``context_revision`` 为口径的
展示状态快照和 ``{enabled, checked, reason}`` 能力注册表。

契约（``ColorViewerCapabilityContext`` →
``evaluate_color_viewer_capabilities``）：

- 输入只含可序列化快照（页面／工作模式、呈现路径、媒体在场、引擎／
  控制器能力位），不放 QWidget／GL 对象／secret；
- 输出 ``ColorViewerCapability(action, supported, reason, checked)``——
  unsupported 必带稳定 reason 键，宿主据此 disable ＋ tooltip；
- 已落地后端能力位如实标注：wipe／A/B／reference（P1-COLOR-05）、
  matte 预览、capture still、workspace、transport。Split Screen 多图、
  Unmix、Freeze、输入 Alpha 等未设计项显式 False ＋ 原因，不冒充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# ── 呈现路径（CV-B01 §"输入→输出"：presentation_path） ────────────────
PRESENTATION_NONE = "none"
PRESENTATION_IMAGE = "image"
PRESENTATION_SOURCE_GL = "source_gl"
PRESENTATION_TIMELINE_PREVIEW = "timeline_preview"
PRESENTATION_COMPOSITE = "composite"

# ── 时间域（CV-B02 将深化换算；B01 先声明显示域口径） ─────────────────
TIME_DOMAIN_NONE = "none"
TIME_DOMAIN_CLIP_LOCAL = "clip_local"
TIME_DOMAIN_SOURCE = "source_frame"
TIME_DOMAIN_TIMELINE = "timeline_frame"

_VIDEO_PRESENTATIONS = (
    PRESENTATION_SOURCE_GL,
    PRESENTATION_TIMELINE_PREVIEW,
    PRESENTATION_COMPOSITE,
)


def presentation_supports_video(path: str) -> bool:
    """该呈现路径是否承载视频 transport（图片／空视口不承载）。"""
    return path in _VIDEO_PRESENTATIONS


def default_time_domain_for(path: str) -> str:
    """各呈现路径的 transport 滑杆帧域（B02 接入精确换算前先声明口径）。"""
    if path == PRESENTATION_SOURCE_GL:
        return TIME_DOMAIN_CLIP_LOCAL
    if path in (PRESENTATION_TIMELINE_PREVIEW, PRESENTATION_COMPOSITE):
        return TIME_DOMAIN_TIMELINE
    return TIME_DOMAIN_NONE


@dataclass(frozen=True)
class ColorViewerState:
    """展示状态快照：只含可序列化字段，供 header／footer／菜单回填。"""

    page: str = "Color"
    work_mode: str = "all"
    presentation_path: str = PRESENTATION_NONE
    time_domain: str = TIME_DOMAIN_NONE
    context_revision: int = 0
    has_media: bool = False
    playing: bool = False
    loop_enabled: bool = False
    current_frame: int = 0
    total_frames: int = 0
    frame_rate: float = 24.0
    asset_id: Optional[str] = None
    clip_id: Optional[str] = None
    timeline_id: Optional[str] = None
    has_reference_still: bool = False
    split_view_active: bool = False
    show_mask_mode: bool = False
    overlays_visible: bool = True
    monitor_muted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "work_mode": self.work_mode,
            "presentation_path": self.presentation_path,
            "time_domain": self.time_domain,
            "context_revision": self.context_revision,
            "has_media": self.has_media,
            "playing": self.playing,
            "loop_enabled": self.loop_enabled,
            "current_frame": self.current_frame,
            "total_frames": self.total_frames,
            "frame_rate": self.frame_rate,
            "asset_id": self.asset_id,
            "clip_id": self.clip_id,
            "timeline_id": self.timeline_id,
            "has_reference_still": self.has_reference_still,
            "split_view_active": self.split_view_active,
            "show_mask_mode": self.show_mask_mode,
            "overlays_visible": self.overlays_visible,
            "monitor_muted": self.monitor_muted,
        }


@dataclass(frozen=True)
class ColorViewerCapabilityContext:
    """评估输入：状态快照 ＋ 已落地的后端能力位（全部可序列化）。"""

    state: ColorViewerState = field(default_factory=ColorViewerState)
    engine_available: bool = False
    video_controller_available: bool = False
    focus_host_available: bool = False
    workspace_service_available: bool = False
    timeline_choice_count: int = 0
    # 未设计／未接线服务位：如实 False，注册表返回稳定原因。
    compare_grid_designed: bool = False
    proxy_service_available: bool = False
    resolution_service_available: bool = False
    bypass_service_available: bool = False
    unmix_designed: bool = False
    mute_service_available: bool = False
    input_alpha_available: bool = False
    media_has_alpha: bool = False
    freeze_designed: bool = False


@dataclass(frozen=True)
class ColorViewerCapability:
    """单个菜单／按钮能力：supported False 必带稳定 reason。"""

    action: str
    supported: bool
    reason: Optional[str] = None
    checked: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "supported": self.supported,
            "reason": self.reason,
            "checked": self.checked,
        }


_REASONS = {
    "no_media": "No media is open in the viewer.",
    "no_engine": "The rendering engine is not available yet.",
    "no_reference_still": "No reference still is loaded.",
    "no_focus_host": "Focus mode host is not reachable from this viewer.",
    "no_workspace_service": "No workspace preset service is installed.",
    "no_timeline_choices": "The project has no timelines to switch.",
    "no_video_controller": "No video playback controller is available.",
    "not_video_presentation": "The current view is not presenting video.",
    "compare_undesigned": "Multi-view Split Screen comparison is not "
                          "designed yet; Wipe stays available.",
    "proxy_service_missing": "Proxy handling is not wired to the pipeline "
                             "yet.",
    "resolution_service_missing": "Preview resolution policy is not wired "
                                  "yet.",
    "bypass_service_missing": "Monitor-scoped grade bypass is not wired "
                              "yet.",
    "unmix_undesigned": "Unmix (transition isolation) is not designed "
                        "yet.",
    "mute_service_missing": "Page-scoped monitor mute is not wired yet.",
    "alpha_unavailable": "This media has no alpha channel to preview.",
    "alpha_preview_missing": "This media has an alpha channel, but an "
                             "alpha preview display path is not "
                             "implemented.",
    "freeze_undesigned": "Grade Freeze has no independent snapshot "
                         "semantics yet.",
    "reverse_unsupported": "Reverse playback is not implemented for "
                           "Color's source or timeline paths.",
}


def _cap(action: str, supported: bool, reason_key: Optional[str] = None,
         checked: Optional[bool] = None) -> ColorViewerCapability:
    return ColorViewerCapability(
        action=action, supported=supported,
        reason=None if supported else _REASONS.get(reason_key, reason_key),
        checked=checked)


def evaluate_color_viewer_capabilities(
        context: ColorViewerCapabilityContext
) -> Tuple[ColorViewerCapability, ...]:
    """单一事实源：Color 视口菜单／按钮的 enabled／checked／reason。"""
    state = context.state
    has_media = state.has_media
    media_ready = has_media and context.engine_available
    is_video = presentation_supports_video(state.presentation_path)
    transport_ready = is_video and context.video_controller_available

    caps: list[ColorViewerCapability] = []

    # ── View：画布变换类操作不依赖媒体（保留旧行为） ──────────────────
    caps.append(_cap("view.zoom_in", True))
    caps.append(_cap("view.zoom_out", True))
    caps.append(_cap("view.fit", True))
    caps.append(_cap("view.actual_size", True))
    caps.append(_cap("view.guides", True))
    caps.append(_cap("view.fullscreen", True))
    caps.append(_cap("view.focus_mode", context.focus_host_available,
                     "no_focus_host"))

    # ── 监看类（需媒体＋引擎；checked 反映引擎实际生效状态） ───────────
    caps.append(_cap("view.highlight", media_ready,
                     "no_media" if not has_media else "no_engine",
                     checked=state.show_mask_mode))
    caps.append(_cap("view.split_wipe", media_ready,
                     "no_media" if not has_media else "no_engine",
                     checked=state.split_view_active))
    caps.append(_cap("view.ab_toggle", media_ready,
                     "no_media" if not has_media else "no_engine"))
    caps.append(_cap("view.capture_reference", media_ready,
                     "no_media" if not has_media else "no_engine"))
    caps.append(_cap("view.clear_reference", state.has_reference_still,
                     "no_reference_still"))

    # ── 抓帧／自动调色／workspace ──────────────────────────────────────
    caps.append(_cap("view.capture_still", has_media, "no_media"))
    caps.append(_cap("view.auto_color", has_media, "no_media"))
    caps.append(_cap("view.workspace", context.workspace_service_available,
                     "no_workspace_service"))

    # ── 时间线选择器 ───────────────────────────────────────────────────
    caps.append(_cap("timeline.switch",
                     context.timeline_choice_count > 0,
                     "no_timeline_choices"))

    # ── Transport（图片／空视口诚实禁用） ──────────────────────────────
    caps.append(_cap("transport.play", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation"))
    caps.append(_cap("transport.first", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation"))
    caps.append(_cap("transport.last", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation"))
    caps.append(_cap("transport.next", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation"))
    caps.append(_cap("transport.prev", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation"))
    caps.append(_cap("transport.scrub", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation"))
    caps.append(_cap("transport.loop", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation",
                     checked=state.loop_enabled))
    caps.append(_cap("transport.timecode", is_video,
                     "not_video_presentation"))
    # CV-B03：停止原位（两路径真实：video_controller.stop_playback /
    # timeline stop_in_place）与倒放（两路径均未实现——timeline 侧仅
    # JKL 逐帧回退的视觉近似）。声明先于按钮：无 handler 保持禁用。
    caps.append(_cap("transport.stop_in_place", transport_ready,
                     "no_video_controller" if is_video
                     else "not_video_presentation"))
    caps.append(_cap("transport.reverse_play", False,
                     "reverse_unsupported"))

    # ── 未设计／未接线（如实 False ＋ 稳定原因） ────────────────────────
    caps.append(_cap("compare.split_screen", context.compare_grid_designed,
                     "compare_undesigned"))
    caps.append(_cap("monitor.proxy", context.proxy_service_available,
                     "proxy_service_missing"))
    caps.append(_cap("monitor.resolution",
                     context.resolution_service_available,
                     "resolution_service_missing"))
    caps.append(_cap("monitor.bypass", context.bypass_service_available,
                     "bypass_service_missing"))
    caps.append(_cap("monitor.unmix", context.unmix_designed,
                     "unmix_undesigned"))
    # CV-B06：监看静音（Master 总线）——服务在 EditPage 音频桥上，真实
    # 接入；checked 反映实际生效状态。
    caps.append(_cap("monitor.mute", context.mute_service_available,
                     "mute_service_missing",
                     checked=state.monitor_muted))
    # CV-B05：输入 Alpha 预览——素材有/无 alpha 分开归因（都不会把
    # qualifier matte 灰度图冒充输入 alpha；显示路径未实现）。
    caps.append(_cap("monitor.alpha", context.input_alpha_available,
                     "alpha_preview_missing" if context.media_has_alpha
                     else "alpha_unavailable"))
    caps.append(_cap("grade.freeze", context.freeze_designed,
                     "freeze_undesigned"))
    # Power Window 图形 overlay 的显示开关是既有真实能力（B05 接按钮）。
    caps.append(_cap("overlay.power_windows", media_ready,
                     "no_media" if not has_media else "no_engine",
                     checked=state.overlays_visible))

    return tuple(caps)


def color_capability_by_action(
        caps: Tuple[ColorViewerCapability, ...]
) -> Dict[str, ColorViewerCapability]:
    return {cap.action: cap for cap in caps}
