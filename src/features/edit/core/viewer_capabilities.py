"""VP-B07：Viewer capability 注册表——菜单真实接线的单一事实源（纯领域）。

缺口（计划 VP-B07）：Viewport 菜单（Proxy/Resolution/Bypass/Source Tape/
Reverse/Match Frame/Fullscreen…）的可用性散落各处或干脆硬编码 disabled，
没有以 capability 为单一事实源的注册表——"启用控件须以实际 capability
为依据，不能只切换 checked 样式"。

契约（``ViewerCapabilityContext`` → ``evaluate_viewer_capabilities``）：

- 输入只含可序列化快照（gpu 可用、代理状态、时间线/素材在场、页面模
  式、已落地后端能力位），不放 QWidget/texture/decoder；
- 输出 ``ViewerCapability(action, supported, reason, checked)``——
  unsupported 必带稳定 reason 键，宿主据此 disable + tooltip；
- 已落地后端能力位如实标注：源循环（B05）/倒放视频（B05）/Match
  Frame（B06）/预算诊断（B03）；Source Tape、源 marker、反向音频、
  Source 外部监看等未设计项显式 False + 原因，不冒充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class ProxyStateSnapshot:
    """代理可用性快照（对齐 realtime_playback.policy 的同名协议）。"""

    has_disk_proxy_ready: bool = False
    is_disk_proxy_generating: bool = False
    has_disk_proxy_failed: bool = False


@dataclass(frozen=True)
class ViewerCapabilityContext:
    """评估输入：只含可序列化状态。"""

    has_timeline: bool = True
    source_asset_open: bool = False
    gpu_available: bool = True
    in_deliver: bool = False
    proxy_state: ProxyStateSnapshot = field(
        default_factory=ProxyStateSnapshot)
    monitor_bypass_service_available: bool = False
    source_reverse_audio_supported: bool = False
    source_loop_supported: bool = True
    source_tape_designed: bool = False
    source_marker_model_available: bool = False
    source_external_monitor_available: bool = False


@dataclass(frozen=True)
class ViewerCapability:
    """单个菜单/动作能力：supported False 必带稳定 reason。"""

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
    "no_timeline": "No timeline is open.",
    "no_source_asset": "No source media is open in the viewer.",
    "deliver_mode": "Deliver forces Timeline monitoring.",
    "proxy_generating": "A disk proxy is being generated for this asset.",
    "proxy_unavailable": "No usable proxy for this asset.",
    "bypass_service_missing":
        "Monitor-scoped bypass service is not wired yet.",
    "source_tape_undesigned":
        "Source Tape needs a media collection and virtual time domain; "
        "design pending.",
    "reverse_audio_unsupported":
        "Reverse audio is not supported; reverse plays video only.",
    "source_markers_undesigned":
        "Source markers need a project schema/migration; not enabled.",
    "source_external_monitor_unavailable":
        "External monitoring is wired for Program output only.",
    "gpu_unavailable": "GPU preview is unavailable on this system.",
}


def _cap(action: str, supported: bool, reason_key: Optional[str] = None,
         checked: Optional[bool] = None) -> ViewerCapability:
    return ViewerCapability(
        action=action, supported=supported,
        reason=None if supported else _REASONS.get(reason_key, reason_key),
        checked=checked)


def evaluate_viewer_capabilities(
        context: ViewerCapabilityContext
) -> Tuple[ViewerCapability, ...]:
    """单一事实源：菜单 enabled/checked/reason 全部来自这里。"""
    caps: list[ViewerCapability] = []

    # ── Proxy Handling（Timeline/Source 各自请求域） ────────────────
    proxy_ready = context.proxy_state.has_disk_proxy_ready
    if proxy_ready:
        caps.append(_cap("proxy.prefer_proxy", True))
        caps.append(_cap("proxy.force_proxy", True))
    else:
        reason = ("proxy_generating"
                  if context.proxy_state.is_disk_proxy_generating
                  else "proxy_unavailable")
        caps.append(_cap("proxy.prefer_proxy", False, reason))
        caps.append(_cap("proxy.force_proxy", False, reason))

    # ── Preview/Timeline Resolution（既有 Program 质量档） ──────────
    caps.append(_cap("preview.quality", True))

    # ── Monitor Bypass（仅监看域，需预览 service） ───────────────────
    caps.append(_cap("bypass.color_monitor",
                     context.monitor_bypass_service_available,
                     "bypass_service_missing"))

    # ── Source Tape（独立功能，未设计 → disabled + 原因） ───────────
    caps.append(_cap("source.tape", context.source_tape_designed,
                     "source_tape_undesigned"))

    # ── Source transport 能力位（对齐 B05 落地） ─────────────────────
    caps.append(_cap("source.loop", context.source_loop_supported))
    caps.append(_cap("source.reverse_play", context.source_asset_open,
                     "no_source_asset"))
    caps.append(_cap("source.reverse_play_audio",
                     context.source_reverse_audio_supported,
                     "reverse_audio_unsupported"))
    caps.append(_cap("source.stop_in_place", True))

    # ── Match Frame（B06 落地；Timeline→Source 需时间线） ───────────
    caps.append(_cap("match_frame.timeline_to_source",
                     context.has_timeline and context.source_asset_open
                     or context.has_timeline, "no_timeline"))
    caps.append(_cap("match_frame.source_to_timeline",
                     context.has_timeline, "no_timeline"))

    # ── Markers ─────────────────────────────────────────────────────
    caps.append(_cap("source.markers", context.source_marker_model_available,
                     "source_markers_undesigned"))

    # ── Fullscreen / 外部监看 ────────────────────────────────────────
    caps.append(_cap("fullscreen.program",
                     context.gpu_available, "gpu_unavailable"))
    caps.append(_cap("fullscreen.source",
                     context.source_external_monitor_available,
                     "source_external_monitor_unavailable"))

    # ── Deliver 页：Source 侧一律不可用（强制 Timeline 监看） ───────
    if context.in_deliver:
        caps = [
            (ViewerCapability(
                c.action, False, _REASONS["deliver_mode"], c.checked)
             if c.action.startswith(("source.", "match_frame."))
             and c.action != "match_frame.source_to_timeline" else c)
            for c in caps
        ]
    return tuple(caps)


def capability_by_action(
        caps: Tuple[ViewerCapability, ...]) -> Dict[str, ViewerCapability]:
    return {cap.action: cap for cap in caps}
