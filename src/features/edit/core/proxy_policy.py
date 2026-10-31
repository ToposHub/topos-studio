"""项目级代理策略（P1-14：代理/渲染缓存统一生产策略——策略与诊断部分）。

把散落在 media_pool_panel 的硬编码代理决策（1080p 阈值、仅视频、自动触发）
收拢为工程级可持久化设置 + 纯函数评估：

- ``ProxyPolicySettings``：frozen dataclass，随工程持久化（EditProject.proxy_policy，
  None = 默认策略）；
- ``evaluate_proxy_action``：给定素材特征返回 generate/skip 与**原因字符串**
  （诊断基础：每个素材为何有/没有代理可解释）；
- ``export_source_path``：导出取源策略——默认强制 master 原片
  （素材代理是 720p H.264 预览层，进导出链会降低成片质量/HDR 丢失）。

不改变播放热路径的磁盘代理优先级（DiskProxyService/realtime policy 既有行为）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROXY_POLICY_MODES = ("off", "auto", "all")
"""``off``：完全关闭自动生成；``auto``：超过原生阈值的视频自动生成；
``all``：所有在线视频自动生成（不论分辨率）。"""

#: T-02 默认档位证据（docs/topos_422_family_benchmark_2026-09-16.md）：
#: Topos 家族 native decode 4K 75.6fps / 2K ≥140fps / proxy 212-740fps，
#: 本身就是编辑优化编码——任何分辨率都不需要再套代理。
_TOPOS_CODEC_MARKER = "topos"

#: 长 GOP 编码标记（4K+ 实测 seek p95 519ms / 稳态 44fps，代理收益最大）。
_LONG_GOP_MARKERS = ("h264", "h.264", "avc", "h265", "h.265", "hevc",
                     "av1", "vvc", "h266")

#: 重型编辑编码标记（all-intra mezzanine；4K 实测 ProRes HQ 32fps）。
_HEAVY_EDIT_MARKERS = ("prores", "dnxhr", "dnx", "v210", "v308", "r444")


@dataclass(frozen=True)
class ProxyPolicySettings:
    """项目级代理策略设置。

    Attributes:
        mode: 自动生成模式（off/auto/all）
        max_native_width: 「auto」模式下视为原生的最大宽度（超过才生成）
        max_native_height: 「auto」模式下视为原生的最大高度
        target_height: 磁盘代理目标高度（用于配置桥接 DiskProxyService）
        auto_generate: 导入时自动触发生成（mode 之上的总开关）
        export_use_master: 导出强制使用原片（不走素材代理）
    """

    mode: str = "auto"
    max_native_width: int = 1920
    max_native_height: int = 1080
    target_height: int = 1080
    auto_generate: bool = True
    export_use_master: bool = True

    def __post_init__(self) -> None:
        if self.mode not in PROXY_POLICY_MODES:
            raise ValueError(f"proxy policy mode must be one of {PROXY_POLICY_MODES}, got {self.mode!r}")
        if self.max_native_width <= 0 or self.max_native_height <= 0:
            raise ValueError("max_native_width/height must be positive")
        if self.target_height <= 0:
            raise ValueError("target_height must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "max_native_width": self.max_native_width,
            "max_native_height": self.max_native_height,
            "target_height": self.target_height,
            "auto_generate": self.auto_generate,
            "export_use_master": self.export_use_master,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "ProxyPolicySettings":
        if not data:
            return ProxyPolicySettings()
        return ProxyPolicySettings(
            mode=str(data.get("mode", "auto")),
            max_native_width=int(data.get("max_native_width", 1920)),
            max_native_height=int(data.get("max_native_height", 1080)),
            target_height=int(data.get("target_height", 1080)),
            auto_generate=bool(data.get("auto_generate", True)),
            export_use_master=bool(data.get("export_use_master", True)),
        )


@dataclass(frozen=True)
class ProxyActionDecision:
    """单个素材的代理生成决策（action + 可读原因，供诊断）。"""

    action: str  # "generate" | "skip"
    reason: str

    @property
    def should_generate(self) -> bool:
        return self.action == "generate"


def _codec_class(codec: str) -> str:
    """codec 名 → 风险类别（P2-02 格式感知：topos/long_gop/heavy_edit/other）。"""
    name = str(codec or "").strip().lower()
    if not name:
        return "unknown"
    if _TOPOS_CODEC_MARKER in name:
        return "topos"
    if any(m in name for m in _LONG_GOP_MARKERS):
        return "long_gop"
    if any(m in name for m in _HEAVY_EDIT_MARKERS):
        return "heavy_edit"
    return "other"


def evaluate_proxy_action(
    policy: ProxyPolicySettings,
    *,
    media_type: str,
    width: int,
    height: int,
    is_offline: bool = False,
    codec: str = "",
) -> ProxyActionDecision:
    """评估是否应为该素材自动生成磁盘代理。

    纯函数：磁盘代理服务的 ready/generating 状态检查由调用方在触发前做
    （避免服务交互混入策略判断）。

    codec 感知（P2-02/T-02，证据见 docs/topos_422_family_benchmark）：
    - topos 家族任何分辨率都不生成（native 已是编辑优化编码）；
    - 其余类别沿用分辨率阈值，原因码区分 long_gop / heavy_edit。
    尺寸未知（0）显式返回 "dimensions_unknown"——不能把未知当成
    "低于阈值"（诊断语义），也不能盲生成（P2-04 排序需要尺寸）。
    """
    if policy.mode == "off":
        return ProxyActionDecision("skip", "policy_off")
    if not policy.auto_generate:
        return ProxyActionDecision("skip", "auto_generate_disabled")
    if media_type != "video":
        return ProxyActionDecision("skip", "not_video")
    if is_offline:
        return ProxyActionDecision("skip", "offline")
    # AUD-17251：尺寸未知检查必须先于 mode=="all" 分支——"未知尺寸不盲
    # 生成"对 auto/all 两种模式都成立（P2-04 排序需要尺寸），否则 all
    # 模式会把 metadata 缺失（width/height=0）的素材盲目入队。
    if width <= 0 or height <= 0:
        return ProxyActionDecision("skip", "dimensions_unknown")
    if policy.mode == "all":
        if _codec_class(codec) == "topos":
            # "all" 也不给 topos 套代理：原生就是优化中间片编码。
            return ProxyActionDecision("skip", "topos_edit_codec_no_proxy_needed")
        return ProxyActionDecision("generate", "policy_all")
    codec_class = _codec_class(codec)
    if codec_class == "topos":
        return ProxyActionDecision("skip", "topos_edit_codec_no_proxy_needed")
    if width > policy.max_native_width or height > policy.max_native_height:
        if codec_class == "long_gop":
            return ProxyActionDecision("generate", "long_gop_above_threshold")
        if codec_class == "heavy_edit":
            return ProxyActionDecision("generate", "heavy_edit_above_threshold")
        return ProxyActionDecision("generate", "above_native_threshold")
    return ProxyActionDecision("skip", "below_threshold")


def export_source_path(project: Any, asset: Any) -> str:
    """导出链取源：默认强制 master 原片，仅当策略显式允许才走素材代理。"""
    policy = _policy_of(project)
    path = str(getattr(asset, "path", "") or "")
    if policy.export_use_master:
        if path:
            return path
        effective = getattr(asset, "effective_path", None)
        return effective if isinstance(effective, str) and effective else ""
    effective = getattr(asset, "effective_path", None)
    if isinstance(effective, str) and effective:
        return effective
    return path


def _policy_of(project: Any) -> ProxyPolicySettings:
    if project is None:
        return ProxyPolicySettings()
    getter = getattr(project, "get_proxy_policy", None)
    if callable(getter):
        try:
            policy = getter()
            if isinstance(policy, ProxyPolicySettings):
                return policy
        except Exception:  # noqa: BLE001 — 诊断/取源路径不允许抛
            pass
    return ProxyPolicySettings()
