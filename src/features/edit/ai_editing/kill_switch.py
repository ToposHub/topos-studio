"""TAEP M12 — Kill Switch（§11.1 M12 退出条件 / §11.2 M12 完成检查 / §15.4 安全响应）。

统一的 per-capability 禁用判定层：覆盖所有 AI 能力（``ai_assistant`` / ``media_analysis`` /
``cloud_analysis`` / ``generation`` / ``mcp_server`` / ``skills``），满足 §11.1 M12 退出条件
"可逐能力关闭 provider/Skill/MCP" + §11.2 M12 完成检查"所有启用能力都有 kill switch"。

设计（遵循 .clinerules / §0 / §11.2 M12 / §15.4 / §12.3）：

- **per-capability 状态**（§15.4："能快速关闭生成/网络/第三方工具"）：每个能力有独立 enabled/
  disabled 状态。``emergency_disable_all()`` 一键全关（§15.4 安全响应）。
- **verify_all_killable**（§11.2 M12 完成检查的硬门）："所有启用能力都有 kill switch"——
  ``verify_all_killable`` 检查每个 enabled 能力都能被 ``disable`` 关掉，返回不可关的能力列表。
- **不破坏普通编辑器**（§11.2 M12 完成检查 / §12.3 第四条）："普通编辑器不依赖 AI 才能运行"——
  全关后编辑器/library/color 等核心仍可用（这些不在 KillSwitch 管辖，由 FeatureFlags 保证）。
- **复用既有机制**：``KillSwitch`` 是**判定层**，底层禁用由既有 ``FeatureFlags`` +
  ``SkillRegistry.enable/disable`` 实现（接线批次把判定结果应用到这些底层）。v1 不真实改全局
  配置状态——这是查询/判定 + verify 层。
- **可审计**（§15.4 安全响应）：每次 disable 记 ``KillSwitchEvent``（capability/reason/timestamp），
  便于追溯谁在何时关了什么。

红线：本模块**不**：自己改 timeline / 持久化到工程文件（落地由 FeatureFlags/宿主负责）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------- #
# Capability —— KillSwitch 管辖的能力（§11.1 M12 "可逐能力关闭"）
# ---------------------------------------------------------------------- #


class Capability(str, Enum):
    """KillSwitch 管辖的 AI 能力（§11.1 M12 "provider/Skill/MCP"）。

    对应 ``feature_flags.AI_CAPABILITY_FLAGS``（ai_assistant/media_analysis/cloud_analysis/
    generation/mcp_server）+ skills（Skill registry 层）。
    """

    AI_ASSISTANT = "ai_assistant"        # AI 助手（会话/规划/上下文）
    MEDIA_ANALYSIS = "media_analysis"     # 本地媒体分析（转录/镜头/OCR 等）
    CLOUD_ANALYSIS = "cloud_analysis"     # 云端分析
    GENERATION = "generation"             # 生成媒体（视频/音乐/语音/图像）
    MCP_SERVER = "mcp_server"             # MCP gateway（外部客户端）
    SKILLS = "skills"                     # Skill runtime（官方 + 第三方工作流）


# 所有能力（用于 verify_all_killable 遍历）
ALL_CAPABILITIES: tuple[Capability, ...] = tuple(Capability)


# ---------------------------------------------------------------------- #
# KillSwitchEvent —— 禁用审计（§15.4 安全响应可追溯）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class KillSwitchEvent:
    """一次能力禁用/启用事件（§15.4 安全响应追溯）。"""

    capability: Capability
    action: str               # "disabled" / "enabled" / "emergency_disabled_all"
    reason: str = ""          # 禁用原因（如 "provider 安全事件" / "user request"）
    timestamp: float = 0.0    # epoch seconds（0 = 调用方未填）

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "action": self.action,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------- #
# CapabilityStatus —— 单个能力的状态
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapabilityStatus:
    """单个能力的 kill-switch 状态。"""

    capability: Capability
    enabled: bool = True
    killable: bool = True     # 是否可被 disable 关掉（硬编码能力恒为 True；第三方扩展可标 False）

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "enabled": self.enabled,
            "killable": self.killable,
        }


# ---------------------------------------------------------------------- #
# VerifyResult —— verify_all_killable 产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerifyResult:
    """``verify_all_killable`` 的产物（§11.2 M12 完成检查硬门）。

    ``unkillable`` 非空 = 有 enabled 但不可关的能力 → 违反"所有启用能力都有 kill switch"。
    """

    all_killable: bool
    enabled_count: int = 0
    disabled_count: int = 0
    unkillable: tuple[Capability, ...] = ()   # enabled 但 killable=False 的能力
    diagnostics: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_killable": self.all_killable,
            "enabled_count": self.enabled_count,
            "disabled_count": self.disabled_count,
            "unkillable": [c.value for c in self.unkillable],
            "diagnostics": list(self.diagnostics),
        }


# ---------------------------------------------------------------------- #
# KillSwitch —— 判定层
# ---------------------------------------------------------------------- #


class KillSwitch:
    """统一 kill-switch 判定层（§11.1 M12 / §11.2 M12 / §15.4）。

    用法::

        ks = KillSwitch()
        ks.disable(Capability.GENERATION, reason="provider 安全事件")
        assert not ks.is_enabled(Capability.GENERATION)
        ks.emergency_disable_all(reason="incident response")
        verify = ks.verify_all_killable()
        assert verify.all_killable

    红线：全关后普通编辑器仍可运行（§11.2 M12 完成检查）——核心能力（library/color/edit 等）
    不在 KillSwitch 管辖，由 FeatureFlags 保证（v1 默认 True，不受 KillSwitch 影响）。
    """

    def __init__(self) -> None:
        self._status: dict[Capability, CapabilityStatus] = {
            c: CapabilityStatus(capability=c, enabled=True, killable=True)
            for c in ALL_CAPABILITIES
        }
        self._events: list[KillSwitchEvent] = []

    # ---------------- 查询 ---------------- #

    def is_enabled(self, capability: Capability) -> bool:
        """该能力是否启用（False = 已 kill）。"""
        return self._status[capability].enabled

    def is_killable(self, capability: Capability) -> bool:
        """该能力是否可被 disable 关掉。"""
        return self._status[capability].killable

    def status(self, capability: Capability) -> CapabilityStatus:
        return self._status[capability]

    def all_status(self) -> tuple[CapabilityStatus, ...]:
        return tuple(self._status[c] for c in ALL_CAPABILITIES)

    def enabled_capabilities(self) -> tuple[Capability, ...]:
        return tuple(c for c in ALL_CAPABILITIES if self._status[c].enabled)

    def disabled_capabilities(self) -> tuple[Capability, ...]:
        return tuple(c for c in ALL_CAPABILITIES if not self._status[c].enabled)

    # ---------------- 禁用/启用 ---------------- #

    def disable(self, capability: Capability, *, reason: str = "") -> bool:
        """禁用单个能力（§15.4 "快速关闭"）。返回是否变更。

        ``killable=False`` 的能力不可禁（返回 False）——用于标记"硬绑核心"的扩展。
        """
        s = self._status[capability]
        if not s.killable or not s.enabled:
            return False
        self._status[capability] = CapabilityStatus(
            capability=capability, enabled=False, killable=s.killable,
        )
        self._events.append(KillSwitchEvent(
            capability=capability, action="disabled",
            reason=reason, timestamp=time.time(),
        ))
        return True

    def enable(self, capability: Capability, *, reason: str = "") -> bool:
        """重新启用能力（需人工显式操作；不自动恢复）。返回是否变更。"""
        s = self._status[capability]
        if s.enabled:
            return False
        self._status[capability] = CapabilityStatus(
            capability=capability, enabled=True, killable=s.killable,
        )
        self._events.append(KillSwitchEvent(
            capability=capability, action="enabled",
            reason=reason, timestamp=time.time(),
        ))
        return True

    def emergency_disable_all(self, *, reason: str = "") -> int:
        """一键全关所有 killable 能力（§15.4 安全响应）。返回关闭数。

        红线（§11.2 M12 完成检查）："普通编辑器不依赖 AI 才能运行"——全关后核心编辑器
        仍可用（核心能力不在 KillSwitch 管辖）。
        """
        count = 0
        for c in ALL_CAPABILITIES:
            if self.disable(c, reason=reason):
                count += 1
        if count > 0:
            self._events.append(KillSwitchEvent(
                capability=Capability.AI_ASSISTANT,  # 占位（全关事件）
                action="emergency_disabled_all",
                reason=reason, timestamp=time.time(),
            ))
        return count

    # ---------------- verify（§11.2 M12 完成检查硬门）---------------- #

    def verify_all_killable(self) -> VerifyResult:
        """验证"所有启用能力都有 kill switch"（§11.2 M12 完成检查）。

        ``all_killable=True`` 当且仅当每个 enabled 能力都 ``killable=True``。
        返回 ``unkillable`` 列表（enabled 但不可关的能力）+ 诊断。

        注意：本检查只验证"enabled 能力的 ``killable`` 标志位是否为 True"，**不**验证
        "``disable`` 后对应执行路径是否真的被挡住"——后者由接线批次保证（把
        ``KillSwitch.is_enabled`` 接到 ``FeatureFlags`` / ``SkillRegistry`` /
        ``GenerationService`` 的入口判断）。v1 ``KillSwitch`` 是纯判定层，未与下游
        接线；``all_killable=True`` 表示"判定逻辑上每能力都可关"，不等同于"已真正生效"。
        """
        enabled = [c for c in ALL_CAPABILITIES if self._status[c].enabled]
        disabled = [c for c in ALL_CAPABILITIES if not self._status[c].enabled]
        unkillable = tuple(
            c for c in enabled if not self._status[c].killable
        )
        diags: list[dict[str, Any]] = []
        for c in unkillable:
            diags.append({
                "severity": "error",
                "code": "TAEP_QUALITY_GATE_FAILED",
                "message": (
                    f"capability {c.value} is enabled but not killable "
                    f"(违反 §11.2 M12：所有启用能力都必须有 kill switch)"
                ),
            })
        return VerifyResult(
            all_killable=len(unkillable) == 0,
            enabled_count=len(enabled),
            disabled_count=len(disabled),
            unkillable=unkillable,
            diagnostics=tuple(diags),
        )

    # ---------------- 审计访问（§15.4）---------------- #

    def events(self) -> tuple[KillSwitchEvent, ...]:
        """所有禁用/启用事件（§15.4 安全响应追溯）。"""
        return tuple(self._events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": [s.to_dict() for s in self.all_status()],
            "events": [e.to_dict() for e in self._events],
        }


__all__ = [
    "Capability",
    "ALL_CAPABILITIES",
    "KillSwitchEvent",
    "CapabilityStatus",
    "VerifyResult",
    "KillSwitch",
]
