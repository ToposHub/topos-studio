"""
Effect Preview Quality Module - 播放时效果降级系统

P3-4: 播放 preview 默认跳过或降级重型效果

设计原则：
- 播放时：跳过或降级重型效果以保持流畅性
- 暂停/seek/master时：恢复完整质量
- 导出时：不受影响，完整质量（由 begin_export()/end_export()/
  export_full_quality() 强制保证，而非仅靠调用方约定）

重型效果类型：
1. film_grain: Shader 级别，可跳过或降低采样
2. AI matte: 推理计算密集，播放时锁定
3. denoise/deblur: ONNX 推理，播放时跳过
4. 高半径 blur: Shader 性能敏感，播放时降级
5. AI 插件 (.tai): 推理计算，播放时跳过
6. full-res mask rasterization: GPU 带宽密集，播放时降级

参考：
- docs/edit_page_timeline_architecture.md P3-4
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator, Optional, Dict, Any, List
import logging
import threading

logger = logging.getLogger(__name__)


# =============================================================================
# Preview Quality Level 枚举
# =============================================================================

class PreviewQualityLevel(Enum):
    """
    效果预览质量级别。

    用于标记效果在播放时的降级策略。

    Values:
        FULL: 完整质量，播放和暂停都执行
        HIGH: 高质量效果，播放时轻度降级
        MEDIUM: 中等质量，播放时中度降级
        LOW: 低质量效果，播放时跳过
        SKIP: 播放时完全跳过（AI 推理等重型操作）
    """
    FULL = auto()     # 不降级（基础调色、mixer等）
    HIGH = auto()     # 轻度降级（film grain降低采样）
    MEDIUM = auto()   # 中度降级（blur限制半径）
    LOW = auto()      # 重度降级（高半径blur）
    SKIP = auto()     # 播放时跳过（AI推理、denoise/deblur）

    @property
    def is_playback_compatible(self) -> bool:
        """是否在播放时执行（非 SKIP）"""
        return self != PreviewQualityLevel.SKIP

    @property
    def needs_degradation(self) -> bool:
        """是否需要降级（非 FULL）"""
        return self != PreviewQualityLevel.FULL


# =============================================================================
# Effect Cost Category - 效果成本类别
# =============================================================================

class EffectCostCategory(Enum):
    """
    效果计算成本类别。

    用于识别重型效果。

    Values:
        SHADER_LIGHT: GPU shader 轻量计算（< 1ms/frame）
        SHADER_MEDIUM: GPU shader 中等计算（1-5ms/frame）
        SHADER_HEAVY: GPU shader 重型计算（> 5ms/frame）
        AI_INFERENCE: ONNX/CPU 推理计算（> 50ms/frame）
        MASK_RASTERIZATION: Mask 栅格化（GPU 带宽密集）
    """
    SHADER_LIGHT = auto()
    SHADER_MEDIUM = auto()
    SHADER_HEAVY = auto()
    AI_INFERENCE = auto()
    MASK_RASTERIZATION = auto()


# =============================================================================
# Heavy Effect Definition - 重型效果定义
# =============================================================================

@dataclass(frozen=True)
class HeavyEffectDefinition:
    """
    重型效果定义。

    标记特定效果在播放时的降级策略。

    Attributes:
        effect_type: 效果类型标识（如 'film_grain', 'ai_denoise'）
        cost_category: 计算成本类别
        preview_quality: 预览质量级别
        degradation_params: 降级时覆盖合并的参数（保留原始参数的其余键）
        degradation_caps: 降级钳制上限。键存在于合并结果时取
            ``min(当前值, 上限)``——降级只会收紧参数，不会把原本
            更小/更廉价的值反向放大
        ui_label: UI 显示标签（用于降级指示）
        description: 效果描述
    """

    effect_type: str
    cost_category: EffectCostCategory
    preview_quality: PreviewQualityLevel
    degradation_params: Dict[str, Any] = field(default_factory=dict)
    degradation_caps: Dict[str, float] = field(default_factory=dict)
    ui_label: str = ""
    description: str = ""

    def get_degraded_params(self, original_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        获取降级后的参数。

        所有降级级别（SKIP/HIGH/MEDIUM/LOW）共用同一合并规则：

        1. 以原始参数为基底叠加 ``degradation_params``（覆盖式）——
           SKIP 级别同样保留原始参数的其余键，且注册的降级参数
           （如 ``{"ai_matte_enabled": False}``）真正生效；
        2. 再按 ``degradation_caps`` 逐键钳制（``min(当前值, 上限)``）——
           半径/分辨率等"越小越廉价"的参数不会被降级配置反向抬升。

        Args:
            original_params: 原始参数

        Returns:
            降级后的参数字典（新字典；FULL 级别原样返回原始参数）
        """
        if self.preview_quality == PreviewQualityLevel.FULL:
            return original_params

        degraded: Dict[str, Any] = {**original_params, **self.degradation_params}
        for key, cap in self.degradation_caps.items():
            value = degraded.get(key)
            # 仅对数值键钳制；bool 是开关语义，不参与大小比较
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                degraded[key] = min(value, cap)
        return degraded


# =============================================================================
# Built-in Heavy Effects Registry - 内置重型效果注册表
# =============================================================================

# P3-4: 播放期高半径 blur 的钳制上限（单一来源；消费方应引用本常量，
# 避免阈值在多个文件中硬编码漂移）
BLUR_HIGH_RADIUS_PLAYBACK_CAP: float = 20.0

# P3-4: 根据方案定义的重型效果列表
BUILTIN_HEAVY_EFFECTS: Dict[str, HeavyEffectDefinition] = {
    # Film Grain - Shader 重型计算（可降低采样）
    "film_grain": HeavyEffectDefinition(
        effect_type="film_grain",
        cost_category=EffectCostCategory.SHADER_HEAVY,
        preview_quality=PreviewQualityLevel.HIGH,
        degradation_params={
            "intensity": 0.0,  # 播放时强度降为 0（完全跳过）
            # 或使用降低采样版本：
            # "sample_count": 4,  # 从默认 12 降到 4
        },
        ui_label="Film Grain",
        description="Realistic film grain simulation - skipped during playback",
    ),

    # AI Matte - 推理计算密集
    "ai_matte": HeavyEffectDefinition(
        effect_type="ai_matte",
        cost_category=EffectCostCategory.AI_INFERENCE,
        preview_quality=PreviewQualityLevel.SKIP,
        degradation_params={"ai_matte_enabled": False},
        ui_label="AI Matte",
        description="AI-based matte extraction - locked during playback",
    ),

    # AI Matte Update - 动态更新锁定
    "ai_matte_update": HeavyEffectDefinition(
        effect_type="ai_matte_update",
        cost_category=EffectCostCategory.AI_INFERENCE,
        preview_quality=PreviewQualityLevel.SKIP,
        degradation_params={"update_enabled": False},
        ui_label="AI Matte Update",
        description="AI matte dynamic update - locked during playback",
    ),

    # Denoise - ONNX 推理
    "denoise": HeavyEffectDefinition(
        effect_type="denoise",
        cost_category=EffectCostCategory.AI_INFERENCE,
        preview_quality=PreviewQualityLevel.SKIP,
        degradation_params={"enabled": False},
        ui_label="Denoise",
        description="AI denoising - skipped during playback",
    ),

    # AI Denoise Node - Node Engine 中的 AI 降噪
    "ai_denoise": HeavyEffectDefinition(
        effect_type="ai_denoise",
        cost_category=EffectCostCategory.AI_INFERENCE,
        preview_quality=PreviewQualityLevel.SKIP,
        degradation_params={"enabled": False},
        ui_label="AI Denoise",
        description="AI-based noise reduction - skipped during playback",
    ),

    # Deblur - ONNX 推理
    "deblur": HeavyEffectDefinition(
        effect_type="deblur",
        cost_category=EffectCostCategory.AI_INFERENCE,
        preview_quality=PreviewQualityLevel.SKIP,
        degradation_params={"enabled": False},
        ui_label="Deblur",
        description="AI deblurring - skipped during playback",
    ),

    # High Radius Blur - Shader 重型计算
    # 高半径（> BLUR_HIGH_RADIUS_PLAYBACK_CAP）blur 在播放时钳制
    "blur_high_radius": HeavyEffectDefinition(
        effect_type="blur_high_radius",
        cost_category=EffectCostCategory.SHADER_HEAVY,
        preview_quality=PreviewQualityLevel.MEDIUM,
        degradation_caps={
            "blur_radius": BLUR_HIGH_RADIUS_PLAYBACK_CAP,  # 限制最大半径（不放大已更小的值）
            "blur_iterations": 1,  # 限制迭代次数
        },
        ui_label="High Radius Blur",
        description="Large radius blur - radius limited during playback",
    ),

    # Full-res Mask Rasterization - GPU 带宽密集
    "full_res_mask": HeavyEffectDefinition(
        effect_type="full_res_mask",
        cost_category=EffectCostCategory.MASK_RASTERIZATION,
        preview_quality=PreviewQualityLevel.MEDIUM,
        degradation_caps={
            "mask_scale": 0.5,  # 降级到半分辨率（不抬升已更低的分辨率）
        },
        ui_label="Full-res Mask",
        description="Full resolution mask rasterization - downscaled during playback",
    ),

    # AI Plugins (generic) - 任何 .tai 插件
    "ai_plugin": HeavyEffectDefinition(
        effect_type="ai_plugin",
        cost_category=EffectCostCategory.AI_INFERENCE,
        preview_quality=PreviewQualityLevel.SKIP,
        degradation_params={"enabled": False},
        ui_label="AI Plugin",
        description="AI-based plugin effects - skipped during playback",
    ),
}


# =============================================================================
# Preview Quality Manager - 预览质量管理器
# =============================================================================

@dataclass
class PlaybackDegradationState:
    """
    播放降级状态。

    追踪当前播放时的降级效果状态。

    Attributes:
        degraded_effects: 当前被降级的效果列表
        degradation_reason: 降级原因（"playback" / "paused" / "none"；
            导出不是降级原因——导出期间强制完整质量）
        is_playing: 是否正在播放
        last_degradation_time: 最近降级时间戳
    """

    degraded_effects: List[str] = field(default_factory=list)
    degradation_reason: str = "none"
    is_playing: bool = False
    last_degradation_time: float = 0.0

    def add_degraded(self, effect_type: str) -> None:
        """添加降级效果"""
        if effect_type not in self.degraded_effects:
            self.degraded_effects.append(effect_type)

    def remove_degraded(self, effect_type: str) -> None:
        """移除降级效果"""
        self.degraded_effects = [e for e in self.degraded_effects if e != effect_type]

    def clear_degraded(self) -> None:
        """清除所有降级效果"""
        self.degraded_effects.clear()

    def is_degraded(self, effect_type: str) -> bool:
        """检查效果是否被降级"""
        return effect_type in self.degraded_effects


class EffectPreviewQualityManager:
    """
    效果预览质量管理器。

    管理播放时的效果降级策略。

    Threading:
        所有公共方法内部以单一 ``RLock`` 串行化：UI 线程（指示器轮询/
        复选框、插件注册）与播放/渲染/导出线程可并发调用，遍历降级
        列表或注册表不会被并发修改打断。:meth:`get_degradation_state`
        返回快照拷贝，修改返回值不影响管理器内部状态。

    Registration:
        内置重型效果来自模块级 ``BUILTIN_HEAVY_EFFECTS``（拷贝后可变）。
        插件可在运行期通过 :meth:`register_heavy_effect` /
        :meth:`unregister_heavy_effect` 增删，立即生效于后续查询。

    Export:
        :meth:`begin_export` / :meth:`end_export`（或上下文管理器
        :meth:`export_full_quality`）在导出期间强制完整质量并冻结降级
        状态切换，保证 preview/export 语义一致。

    使用示例：
        manager = EffectPreviewQualityManager()
        manager.set_playback_state(True)  # 开始播放

        # 获取降级后的 film_grain 参数
        degraded_params = manager.get_degraded_params(
            "film_grain",
            {"enabled": True, "intensity": 0.5}
        )

        manager.set_playback_state(False)  # 暂停/停止
    """

    def __init__(self) -> None:
        """初始化管理器"""
        self._lock: threading.RLock = threading.RLock()
        self._heavy_effects: Dict[str, HeavyEffectDefinition] = BUILTIN_HEAVY_EFFECTS.copy()
        self._state: PlaybackDegradationState = PlaybackDegradationState()
        self._degradation_enabled: bool = True  # 全局开关
        self._export_active: bool = False  # 导出模式（强制完整质量）

    # =========================================================================
    # 播放状态管理
    # =========================================================================

    def set_playback_state(self, is_playing: bool) -> None:
        """
        设置播放状态。

        播放时启用降级，暂停时恢复完整质量。导出模式下仅记录播放标志，
        降级列表的重建/清空冻结至 :meth:`end_export`（导出强制完整质量）。

        Args:
            is_playing: 是否正在播放
        """
        with self._lock:
            self._state.is_playing = is_playing

            if self._export_active:
                self._state.degradation_reason = "playback" if is_playing else "paused"
                return

            if is_playing:
                self._state.degradation_reason = "playback"
                self._apply_playback_degradation()
            else:
                self._state.degradation_reason = "paused"
                self._state.clear_degraded()

    def is_playing(self) -> bool:
        """是否正在播放"""
        with self._lock:
            return self._state.is_playing

    def get_degradation_state(self) -> PlaybackDegradationState:
        """
        获取当前降级状态（快照拷贝）。

        返回值为独立副本，调用方修改不会影响管理器内部状态。
        """
        with self._lock:
            return PlaybackDegradationState(
                degraded_effects=list(self._state.degraded_effects),
                degradation_reason=self._state.degradation_reason,
                is_playing=self._state.is_playing,
                last_degradation_time=self._state.last_degradation_time,
            )

    # =========================================================================
    # 效果降级管理
    # =========================================================================

    def _apply_playback_degradation(self) -> None:
        """应用播放时降级策略（调用方需持有 ``self._lock``）"""
        if not self._degradation_enabled:
            return

        for effect_type, definition in self._heavy_effects.items():
            if definition.preview_quality.needs_degradation:
                self._state.add_degraded(effect_type)

    def get_degraded_params(
        self,
        effect_type: str,
        original_params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        获取降级后的效果参数。

        决策路径与状态追踪共用同一降级列表：效果未列入
        ``degraded_effects`` 时一律返回原始参数，避免同帧内策略分裂。
        导出期间强制返回原始参数（完整质量）。

        Args:
            effect_type: 效果类型
            original_params: 原始参数

        Returns:
            降级后的参数（如果在播放且效果被降级）
        """
        with self._lock:
            # 导出强制完整质量
            if self._export_active:
                return original_params

            # 检查是否在播放状态
            if not self._state.is_playing:
                return original_params

            # 检查降级是否启用
            if not self._degradation_enabled:
                return original_params

            # 检查效果是否需要降级
            definition = self._heavy_effects.get(effect_type)
            if definition is None:
                # 未知效果，不降级
                return original_params

            # 与 is_effect_degraded/状态追踪保持同一判定来源
            if not self._state.is_degraded(effect_type):
                return original_params

            # 应用降级策略
            return definition.get_degraded_params(original_params)

    def should_skip_effect(self, effect_type: str) -> bool:
        """
        判断效果是否应该在播放时跳过。

        Args:
            effect_type: 效果类型

        Returns:
            True 表示播放时跳过该效果
        """
        with self._lock:
            if (not self._state.is_playing
                    or not self._degradation_enabled
                    or self._export_active):
                return False

            definition = self._heavy_effects.get(effect_type)
            if definition is None:
                return False

            # 与 get_degraded_params/is_effect_degraded 保持同一判定来源
            if not self._state.is_degraded(effect_type):
                return False

            return definition.preview_quality == PreviewQualityLevel.SKIP

    def is_effect_degraded(self, effect_type: str) -> bool:
        """检查效果是否被降级"""
        with self._lock:
            return self._state.is_degraded(effect_type)

    # =========================================================================
    # 自定义效果注册
    # =========================================================================

    def register_heavy_effect(self, definition: HeavyEffectDefinition) -> None:
        """
        注册自定义重型效果。

        用于插件系统注册自己的重型效果。播放中注册时同步纳入降级
        列表，保证决策路径（get_degraded_params/should_skip_effect）
        与状态追踪立即一致。

        Args:
            definition: 重型效果定义
        """
        with self._lock:
            self._heavy_effects[definition.effect_type] = definition
            if (self._state.is_playing
                    and self._degradation_enabled
                    and definition.preview_quality.needs_degradation):
                self._state.add_degraded(definition.effect_type)
        logger.debug(f"Registered heavy effect: {definition.effect_type} "
                    f"(quality={definition.preview_quality.name})")

    def unregister_heavy_effect(self, effect_type: str) -> None:
        """注销重型效果"""
        with self._lock:
            self._heavy_effects.pop(effect_type, None)
            self._state.remove_degraded(effect_type)

    def get_all_heavy_effects(self) -> Dict[str, HeavyEffectDefinition]:
        """获取所有注册的重型效果"""
        with self._lock:
            return self._heavy_effects.copy()

    # =========================================================================
    # 配置管理
    # =========================================================================

    def set_degradation_enabled(self, enabled: bool) -> None:
        """
        设置降级功能全局开关。

        用户可以通过 UI 关闭降级功能，在播放时也保持完整质量。

        播放中重新启用降级时重建 ``degraded_effects``，保证状态追踪
        （is_effect_degraded/UI 摘要）与决策路径
        （get_degraded_params/should_skip_effect）一致。导出模式下仅
        记录开关，状态重建冻结至 :meth:`end_export`。

        Args:
            enabled: 是否启用降级
        """
        with self._lock:
            self._degradation_enabled = enabled

            if self._export_active or not self._state.is_playing:
                return

            if enabled:
                # 播放中重新启用：重建降级列表（此前只处理了关闭分支）
                self._apply_playback_degradation()
            else:
                # 关闭降级时清除所有降级效果
                self._state.clear_degraded()

    def is_degradation_enabled(self) -> bool:
        """降级功能是否启用"""
        with self._lock:
            return self._degradation_enabled

    # =========================================================================
    # 导出保护（preview/export 语义一致性）
    # =========================================================================

    def begin_export(self) -> None:
        """
        进入导出模式：导出期间强制完整质量。

        - 降级决策（get_degraded_params/should_skip_effect）一律返回
          "不降级"；
        - 降级列表清空（UI 摘要显示 0 个降级效果）；
        - 播放/开关状态仍可更新，但降级列表的重建冻结至
          :meth:`end_export` 按最新状态统一重算。

        幂等：已处于导出模式时再次调用无副作用（注意嵌套作用域会随
        内层 end_export 提前退出，导出作用域不应嵌套）。
        """
        with self._lock:
            self._export_active = True
            self._state.clear_degraded()

    def end_export(self) -> None:
        """
        退出导出模式。

        未处于导出模式时调用为无操作。退出后按最新播放/开关状态
        重建降级列表。
        """
        with self._lock:
            if not self._export_active:
                return
            self._export_active = False
            if self._state.is_playing and self._degradation_enabled:
                self._apply_playback_degradation()
            else:
                self._state.clear_degraded()

    def is_export_active(self) -> bool:
        """是否处于导出模式（导出期间强制完整质量）"""
        with self._lock:
            return self._export_active

    @contextmanager
    def export_full_quality(self) -> Iterator[None]:
        """
        导出作用域上下文管理器（异常安全）。

        用法::

            with manager.export_full_quality():
                export_frames(...)  # 期间强制完整质量

        作用域内抛出异常时同样会退出导出模式。
        """
        self.begin_export()
        try:
            yield
        finally:
            self.end_export()

    # =========================================================================
    # UI 指示器支持
    # =========================================================================

    def get_degraded_effects_display(self) -> List[str]:
        """
        获取被降级效果的 UI 显示列表。

        Returns:
            降级效果名称列表（用于 UI 指示器）
        """
        with self._lock:
            display_list: List[str] = []
            for effect_type in self._state.degraded_effects:
                definition = self._heavy_effects.get(effect_type)
                if definition:
                    display_list.append(definition.ui_label)
            return display_list

    def get_degradation_summary(self) -> Dict[str, Any]:
        """
        获取降级状态摘要（用于 UI 显示）。

        Returns:
            包含降级信息的字典：
            - 'is_playing': 是否播放
            - 'degradation_enabled': 降级是否启用
            - 'degraded_count': 降级效果数量
            - 'degraded_effects': 降级效果列表
        """
        with self._lock:
            return {
                "is_playing": self._state.is_playing,
                "degradation_enabled": self._degradation_enabled,
                "degraded_count": len(self._state.degraded_effects),
                "degraded_effects": self.get_degraded_effects_display(),
            }


# =============================================================================
# Global Instance
# =============================================================================

_manager_instance: Optional[EffectPreviewQualityManager] = None
_manager_instance_lock = threading.Lock()


def get_effect_preview_quality_manager() -> EffectPreviewQualityManager:
    """
    获取全局效果预览质量管理器实例。

    懒初始化的进程级单例，双重检查加锁保证并发首次访问也只创建
    一个实例（实例内部方法另有自己的锁，见类文档 Threading 节）。

    Returns:
        EffectPreviewQualityManager singleton
    """
    global _manager_instance
    if _manager_instance is None:
        with _manager_instance_lock:
            if _manager_instance is None:
                _manager_instance = EffectPreviewQualityManager()
    return _manager_instance


def is_playback_degradation_active() -> bool:
    """
    检查播放降级是否激活（导出模式下一律视为未激活）。

    Returns:
        True 表示当前有效果被降级
    """
    manager = get_effect_preview_quality_manager()
    return (
        manager.is_playing()
        and manager.is_degradation_enabled()
        and not manager.is_export_active()
    )


def get_degraded_effect_params(
    effect_type: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    便捷函数：获取降级后的效果参数。

    Args:
        effect_type: 效果类型
        params: 原始参数

    Returns:
        降级后的参数
    """
    return get_effect_preview_quality_manager().get_degraded_params(effect_type, params)