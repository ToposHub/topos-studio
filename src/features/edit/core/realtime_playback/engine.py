"""
Realtime Playback Engine
专业 NLE 实时播放引擎（缺陷计划 RP-P0-05 / §3.2）。

## 目标

把 Edit 播放收敛为**唯一热路径**，避免"新架构写了，但真实播放仍走老路径"。
所有播放 tick 必须经过 `RealtimePlaybackEngine.request_present()`，由 engine 统一：
- 调用 RealtimePlaybackPolicy 决定 source（PLACEHOLDER 仅作 telemetry，
  不阻塞解码——普通 H264/H265 无代理时仍走降采样 master）。
- 调度解码（TimelineDecodeScheduler / 或委托现有 decode path）。
- 驱动 GPU 合成（TimelineGpuCompositor / 或委托现有 GPU renderer）。
- 处理 miss/hold/rebuffer/generation。

## 实际行为说明（重要）

engine 通过 SourceResolver 每帧生成 SourceDecision，但 SourceDecision 目前
**只影响 telemetry 和后台 proxy 触发，不阻塞当前帧解码**。原因：
- 真正的 master 阻止需要可靠的格式/分辨率探测 + 重型格式判断，当前
  DiskProxyServiceAdapter 无法区分"会卡的 ProRes 4K"和"流畅的 H264"。
- 一刀切拒绝 PLACEHOLDER 会导致普通素材画面完全不动（已修复）。
- 所以 get_frame_with_policy 透传到 get_frame，由 _resolve_preview_decode_source
  内部决定走 disk_proxy 还是 original_downscaled。

这是有意的设计权衡，不是 bug。未来要真正实现"realtime 严格禁止重型 master"，
需要在 SourceResolver 层加格式感知的策略（按 codec/分辨率判断是否允许 master）。

## 集成策略（渐进式）

第一阶段（本批次）：engine 作为编排层，内部委托给现有的
`TimelinePreviewRenderer` 执行路径（_try_gpu_render / CPU composite），
但所有决策（mode、source policy、generation、miss handling）由 engine 统一。
旧旁路通过 engine 的 `delegate_render` 回调执行，但 engine 强制：
- miss 时 hold/rebuffer，不静默 CPU fallback。
- generation 守卫统一在 engine 层。

第二阶段（当前批次）：engine 编排 TimelineDecodeScheduler + FrameRingBuffer
的 bounded prefetch，worker 在 GPU 热路径消费完整 ready bundle；复杂效果
仍保留 _try_gpu_render 的任务组装，以确保现有渲染语义不变。

## 状态机（规格书 §14.2）

IDLE -> WARMING -> READY -> PLAYING -> REBUFFERING -> PLAYING -> PAUSED -> SHUTDOWN
"""

from __future__ import annotations

import logging
import os
import threading
import time
from enum import Enum
from typing import Any, Callable, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .policy import RealtimePlaybackPolicy, get_default_policy
from .adaptive_quality import AdaptiveQualityController
from .source_resolver import LayerSourceQuery, SourceResolver
from .telemetry import PlaybackFrameTelemetry, TelemetryAggregator
from .types import DecodeMiss, MissReason, PlaybackMode, SourceDecision, SourceKind, classify_miss_reason
from src.shared.video.frame_trace_schema import next_frame_trace_id

logger = logging.getLogger(__name__)

# P1-07: 变速 lookahead 扩展上限（规格书 §9.3 正向 lookahead 8-30 的上界
# 附近取值；窗口实际仍受每流 ring 容量约束，超容量部分只是徒劳解码）。
_MAX_SCHEDULER_RADIUS_AFTER = 32


# =============================================================================
# EngineState（规格书 §14.2）
# =============================================================================


class EngineState(Enum):
    """实时播放引擎状态。"""
    IDLE = "idle"
    WARMING = "warming"
    READY = "ready"
    PLAYING = "playing"
    REBUFFERING = "rebuffering"
    PAUSED = "paused"
    SHUTDOWN = "shutdown"


# =============================================================================
# RealtimePlaybackEngine
# =============================================================================


class RealtimePlaybackEngine(QObject):
    """
    实时播放引擎：Edit 播放的唯一热路径入口。

    信号：
        frame_presented(timeline_frame): 一帧成功 present。
        state_changed(state_name): 状态机变化。
        playback_warning(reason, detail): miss/rebuffer/proxy_required 等警告。
        telemetry_recorded(trace): 每帧 telemetry。

    用法：
        engine = RealtimePlaybackEngine()
        engine.configure(renderer=timeline_preview_renderer)
        engine.set_mode(PlaybackMode.REALTIME)
        # 播放 tick
        engine.request_present(timeline_frame=100, deadline_ns=...)
    """

    frame_presented = pyqtSignal(int)
    state_changed = pyqtSignal(str)
    # Controller-derived activity state.  TimelinePreviewRenderer subscribes
    # to this signal for UI/GPU policy side effects; the panel must not mirror
    # the controller state into the renderer through a second direct path.
    playback_active_changed = pyqtSignal(bool)
    playback_warning = pyqtSignal(str, object)
    telemetry_recorded = pyqtSignal(object)
    # P1-10: 负载驱动的自适应质量档位变化（level 值 + 详情 dict）。
    # renderer 订阅后把档位映射为 auto2k 基准上的 decode scale 系数。
    quality_pressure_changed = pyqtSignal(str, object)

    def __init__(
        self,
        policy: Optional[RealtimePlaybackPolicy] = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._policy = policy or get_default_policy()
        self._telemetry = TelemetryAggregator(window=120)

        # Batch 6 (缺陷计划 RP-P1-14): 播放预算控制器（back-pressure）。
        # 多轨超预算时 hold/drop，不阻塞 UI tick。
        from .playback_budget import PlaybackBudgetController
        self._budget = PlaybackBudgetController(target_fps=25.0)

        # P1-10 (缺陷计划 RP-P1-10): 负载驱动自适应质量（迟滞 + 驻留 +
        # 用户门控）。档位变化经 quality_pressure_changed 信号广播。
        self._adaptive_quality = AdaptiveQualityController()

        # 状态
        self._state = EngineState.IDLE
        self._mode = PlaybackMode.PAUSED
        self._generation_id = 0
        self._lock = threading.RLock()

        # A1.1：GC 停顿观测（opt-in，env TOPOS_GC_PAUSE_OBSERVER=1）。
        # 只观测不改策略；停顿推送进 FrameTraceCollector 做 deadline miss
        # 重叠关联，shutdown 时输出摘要。会话级 GC 策略试验需关联成立后
        # 另行立项（计划 A1.1 纪律）。
        self._gc_observer = None
        if os.environ.get("TOPOS_GC_PAUSE_OBSERVER"):
            try:
                from src.shared.video.gc_pause_observer import GcPauseObserver
                from src.shared.video.playback_frame_trace import (
                    get_trace_collector,
                )
                # sink=collector：每条停顿即时推送，summary()['gc'] 可做
                # deadline miss 重叠关联。
                self._gc_observer = GcPauseObserver(
                    sink=get_trace_collector())
                self._gc_observer.start()
                logger.info("RealtimePlaybackEngine: GC pause observer enabled")
            except Exception as e:
                logger.warning(f"GC pause observer init failed: {e}")
                self._gc_observer = None

        # EditPlaybackController is the authoritative transport state.  The
        # engine owns this one signal bridge so every consumer observes the
        # same transition (including Color-page playback which reuses Edit's
        # renderer).  ``None`` means no controller has been attached yet.
        self._playback_controller: Any = None
        self._playback_controller_state_signal: Any = None
        self._controller_playback_active: Optional[bool] = None

        # miss / rebuffer 跟踪
        self._consecutive_misses = 0
        self._rebuffer_threshold = 3
        # P1-05: worker/异步呈现路径上报的真实 miss 归因（同 generation 内
        # 单槽消费）。delegate 只能报告 bool；GPU 结果 drop、解码失败等
        # 归因由渲染侧在 drop 点调用 note_playback_miss() 写入，下一次
        # engine miss 时消费并取代伪造的 delegate_miss 分类。
        self._noted_miss: tuple[int, str] | None = None  # (generation_id, reason)

        # 委托：实际渲染由 TimelinePreviewRenderer 执行（渐进式集成）。
        # delegate_render(timeline_frame, generation_id, deadline_ns, decisions) -> bool
        # 返回 True 表示 present 成功，False 表示 miss。
        self._delegate_render: Optional[Callable[..., bool]] = None
        # P1-01: scrub 通道委托（configure 时注入）。
        self._delegate_scrub_render: Optional[Callable[..., bool]] = None
        # P0-01：delegate 是否接受 trace_id 关键字（configure 时探测一次，
        # 避免每帧 inspect.signature）。False 时退回旧签名调用。
        self._delegate_accepts_trace_id = False
        self._renderer: Any = None  # TimelinePreviewRenderer 引用

        # Final-1 (FINAL-P0-03): SourceResolver 是 engine 的 source 决策唯一入口。
        # engine 在每帧 request_present() 内部调用 SourceResolver.resolve_layers()
        # 得到 SourceDecision，把 source_kind 记录到 telemetry，并交给 delegate。
        self._source_resolver: Optional[SourceResolver] = None
        # layer_query_provider: () -> List[LayerSourceQuery]
        # 由 TimelinePreviewRenderer 提供，从 render plan 提取当前帧可见 layer。
        self._layer_query_provider: Optional[Callable[[int], List[LayerSourceQuery]]] = None
        # Optional Stage E scheduler.  It is deliberately opt-in while the
        # legacy renderer is still the presentation delegate; once attached,
        # the engine owns its window cadence and generation handoff.
        self._decode_scheduler: Any = None
        self._scheduler_radius_before = 2
        self._scheduler_radius_after = 8
        self._scheduler_direction_provider: Optional[Callable[[], int]] = None
        # P1-07: J/K/L 变速播放时按倍率扩展行进方向 lookahead（窗口仍受
        # ring 容量与 MAX_RADIUS_AFTER 约束），避免 4x/8x 播放预取饿死。
        self._scheduler_rate_provider: Optional[Callable[[], float]] = None
        # Renderer-owned callback that turns the current render plan and
        # source decisions into scheduler LayerSpec/source-factory config.
        # Keeping this outside the tick lets the engine remain timeline-model
        # agnostic while still making the scheduler a real playback consumer.
        self._scheduler_config_provider: Optional[Callable[..., Any]] = None
        self._scheduler_config_signature: Any = None

        # 统计
        self._total_requests = 0
        self._total_presented = 0
        self._total_misses = 0
        self._total_holds = 0
        self._total_source_resolutions = 0

    # ------------------------------------------------------------------
    # configure
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        renderer: Any = None,
        delegate_render: Optional[Callable[..., bool]] = None,
        delegate_scrub_render: Optional[Callable[..., bool]] = None,
        rebuffer_threshold: int = 3,
        source_resolver: Optional[SourceResolver] = None,
        layer_query_provider: Optional[Callable[[int], List[LayerSourceQuery]]] = None,
        decode_scheduler: Any = None,
        scheduler_radius_before: int = 2,
        scheduler_radius_after: int = 8,
        scheduler_direction_provider: Optional[Callable[[], int]] = None,
        scheduler_rate_provider: Optional[Callable[[], float]] = None,
        scheduler_config_provider: Optional[Callable[..., Any]] = None,
    ) -> None:
        """
        配置引擎。

        Args:
            renderer: TimelinePreviewRenderer 实例（用于委托渲染）。
            delegate_render: 自定义渲染委托（覆盖 renderer 的默认行为）。
                签名 (timeline_frame, generation_id, deadline_ns, decisions) -> bool。
            delegate_scrub_render: P1-01 scrub 通道委托。签名
                (frame, deadline_ns, trace_id, payload) -> bool；payload 是
                renderer 私有的派发参数（plan/token/decode size 等），
                engine 不解释其内容。
            rebuffer_threshold: 连续 miss 多少帧后进入 REBUFFERING。
            source_resolver: SourceResolver 实例（Final-1：每帧 source 决策入口）。
            layer_query_provider: 当前帧的 layer 查询构造器。
                签名 (timeline_frame) -> List[LayerSourceQuery]。
            decode_scheduler: 可选 TimelineDecodeScheduler。接入后，REALTIME
                tick 会异步推进其 bounded prefetch window。
            scheduler_config_provider: 可选 renderer 回调，接收
                ``(timeline_frame, decisions_by_clip)``，返回 scheduler
                配置字典；配置仅在 layer/source 签名变化时更新。
        """
        self._renderer = renderer
        self._delegate_render = delegate_render
        # P1-01: scrub 通道委托（renderer 保留自己的单飞/token 状态机，
        # engine 只做 trace id 分配 + telemetry 记录的外壳）。
        self._delegate_scrub_render = delegate_scrub_render
        self._rebuffer_threshold = max(1, int(rebuffer_threshold))
        # P0-01：探测 delegate 是否支持 trace_id 关键字（每帧零开销）。
        self._delegate_accepts_trace_id = False
        if delegate_render is not None:
            try:
                import inspect
                sig = inspect.signature(delegate_render)
                self._delegate_accepts_trace_id = (
                    "trace_id" in sig.parameters
                    or any(
                        p.kind is p.VAR_KEYWORD
                        for p in sig.parameters.values()
                    )
                )
            except (TypeError, ValueError):
                self._delegate_accepts_trace_id = False
        # Final-1 (FINAL-P0-03)：SourceResolver 成为 engine 的 source 决策唯一入口。
        self._source_resolver = source_resolver
        self._layer_query_provider = layer_query_provider
        self._decode_scheduler = decode_scheduler
        self._scheduler_radius_before = max(0, int(scheduler_radius_before))
        self._scheduler_radius_after = max(0, int(scheduler_radius_after))
        self._scheduler_direction_provider = scheduler_direction_provider
        self._scheduler_rate_provider = scheduler_rate_provider
        self._scheduler_config_provider = scheduler_config_provider
        self._scheduler_config_signature = None
        if source_resolver is not None:
            source_resolver.bind_engine(self)
        # 只要 renderer 或 delegate_render 之一就绪即可进入 READY。
        self._set_state(
            EngineState.READY if (renderer is not None or delegate_render is not None)
            else EngineState.IDLE
        )
        logger.info(
            f"RealtimePlaybackEngine configured: renderer={'yes' if renderer else 'no'}, "
            f"rebuffer_threshold={self._rebuffer_threshold}, "
            f"source_resolver={'yes' if source_resolver else 'no'}"
        )

    # ------------------------------------------------------------------
    # mode / state
    # ------------------------------------------------------------------

    @staticmethod
    def _controller_is_playing(state: Any) -> bool:
        """Return the transport activity bit without importing the controller.

        Keeping this conversion duck-typed avoids a dependency from the
        realtime kernel back to the UI-facing controller module.
        """
        return getattr(state, "value", state) == "playing"

    def _on_playback_controller_state_changed(self, state: Any) -> None:
        """Mirror the controller's state into the engine exactly once."""
        is_playing = self._controller_is_playing(state)
        with self._lock:
            if self._state is EngineState.SHUTDOWN:
                return
            previous = self._controller_playback_active
            self._controller_playback_active = is_playing

        # set_mode owns the engine state machine transition.  Emit the
        # derived activity signal only on an actual transport transition so
        # repeated controller notifications cannot replay prewarm/quality
        # side effects.
        self.set_mode(
            PlaybackMode.REALTIME if is_playing else PlaybackMode.PAUSED
        )
        if previous is None or previous != is_playing:
            self.playback_active_changed.emit(is_playing)

    def bind_playback_controller(self, controller: Any = None) -> None:
        """Bind the single authoritative Edit transport state source.

        The bridge is intentionally owned by the engine rather than by
        TimelinePanel.  Rebinding disconnects the old QObject signal and
        synchronizes the new controller's current state immediately, which is
        important when a Color-page renderer is attached after playback has
        already begun.
        """
        old_signal = self._playback_controller_state_signal
        if old_signal is not None:
            try:
                old_signal.disconnect(self._on_playback_controller_state_changed)
            except (TypeError, RuntimeError):
                pass

        self._playback_controller = controller
        self._playback_controller_state_signal = None
        self._controller_playback_active = None

        if controller is None:
            self._on_playback_controller_state_changed("paused")
            return

        state_signal = getattr(controller, "state_changed", None)
        if state_signal is not None and hasattr(state_signal, "connect"):
            try:
                state_signal.connect(self._on_playback_controller_state_changed)
                self._playback_controller_state_signal = state_signal
            except (TypeError, RuntimeError):
                logger.debug("RealtimePlaybackEngine controller bind failed", exc_info=True)

        # A renderer can be attached after the transport is already playing;
        # do not wait for the next controller event to enter realtime mode.
        self._on_playback_controller_state_changed(
            getattr(controller, "state", "paused")
        )

    @property
    def playback_controller(self) -> Any:
        """Currently bound transport controller, if any."""
        return self._playback_controller

    def set_mode(self, mode: PlaybackMode) -> None:
        """设置播放模式（决定 source policy 与 master 允许性）。"""
        # AUD-17301：锁内只做状态判定与赋值；状态迁移（会发射
        # state_changed，直连槽可能执行外部代码）必须延迟到锁外执行。
        # 旧实现 set_mode 持有 self._lock 期间调用 _set_state——RLock
        # 可重入使 _set_state 的锁外发射形同虚设，直连槽实际在持锁状态
        # 下运行（死锁/重入风险）。
        pending_state: Optional[EngineState] = None
        with self._lock:
            previous_mode = self._mode
            self._mode = mode
            if mode is PlaybackMode.REALTIME:
                if self._state in (EngineState.READY, EngineState.PAUSED, EngineState.REBUFFERING):
                    pending_state = EngineState.PLAYING
            elif mode is PlaybackMode.PAUSED:
                pending_state = EngineState.PAUSED
        if pending_state is not None:
            self._set_state(pending_state)
        # P1-10: 每次进入 REALTIME 从 NORMAL 档重新评估（可见可回退；
        # 若负载仍在，迟滞窗口内会重新降档），不携带上次会话的降档。
        if mode is PlaybackMode.REALTIME and previous_mode is not PlaybackMode.REALTIME:
            event = self._adaptive_quality.reset("playback_restart")
            if event is not None:
                self._emit_quality_event(event)

    @property
    def mode(self) -> PlaybackMode:
        with self._lock:
            return self._mode

    @property
    def state(self) -> EngineState:
        with self._lock:
            return self._state

    @property
    def generation_id(self) -> int:
        with self._lock:
            return self._generation_id

    @property
    def policy(self) -> RealtimePlaybackPolicy:
        return self._policy

    @property
    def telemetry(self) -> TelemetryAggregator:
        return self._telemetry

    @property
    def budget(self):
        """Batch 6: 播放预算控制器（back-pressure）。"""
        return self._budget

    @property
    def adaptive_quality(self) -> AdaptiveQualityController:
        """P1-10: 自适应质量控制器（负载观测与档位状态机）。"""
        return self._adaptive_quality

    def configure_adaptive_quality(
        self,
        *,
        enabled: bool | None = None,
        down_streak: int | None = None,
        up_streak: int | None = None,
        min_dwell_frames: int | None = None,
    ) -> None:
        """P1-10: 运行时调整自适应质量参数（用户偏好 / 实机调参入口）。

        关闭开关时若当前不在 NORMAL 档，立即回退并广播事件。
        """
        controller = self._adaptive_quality
        if enabled is not None:
            event = controller.set_enabled(bool(enabled))
            if event is not None:
                self._emit_quality_event(event)
        if down_streak is not None:
            controller._down_streak = max(1, int(down_streak))
        if up_streak is not None:
            controller._up_streak = max(1, int(up_streak))
        if min_dwell_frames is not None:
            controller._min_dwell_frames = max(0, int(min_dwell_frames))

    def _emit_quality_event(self, event) -> None:
        """广播一次档位变化（信号 + 终端 trace，可见可回退）。"""
        detail = {
            "old_level": event.old_level.value,
            "reason": event.reason,
            "pressure_streak": event.pressure_streak,
            "healthy_streak": event.healthy_streak,
            "scale": event.new_level.scale,
        }
        try:
            self.quality_pressure_changed.emit(event.new_level.value, detail)
        except Exception:
            logger.debug("quality_pressure_changed emit failed", exc_info=True)
        from src.features.edit.core.playback_debug import playback_trace
        playback_trace(
            "engine.quality_pressure_change",
            level=event.new_level.value,
            old_level=event.old_level.value,
            reason=event.reason,
            scale=event.new_level.scale,
        )

    def _observe_adaptive_quality(
        self,
        presented: bool,
        miss_reason: str | None,
    ) -> None:
        """P1-10: 每播放 tick 喂一帧负载观测给自适应质量控制器。"""
        controller = self._adaptive_quality
        if controller is None:
            return
        try:
            event = controller.observe(
                presented=presented,
                back_pressured=self._budget.is_back_pressured,
                miss_reason=miss_reason,
            )
        except Exception:
            logger.debug("adaptive quality observe failed", exc_info=True)
            return
        if event is not None:
            self._emit_quality_event(event)

    @property
    def source_resolver(self) -> Optional[SourceResolver]:
        """Final-1: SourceResolver（每帧 source 决策唯一入口）。"""
        return self._source_resolver

    def configure_source_resolver(
        self,
        resolver: SourceResolver,
        layer_query_provider: Optional[Callable[[int], List[LayerSourceQuery]]] = None,
    ) -> None:
        """Final-1: 运行时注入/替换 SourceResolver（支持序列/asset 变化时重建）。"""
        self._source_resolver = resolver
        if layer_query_provider is not None:
            self._layer_query_provider = layer_query_provider
        resolver.bind_engine(self)

    def configure_decode_scheduler(
        self,
        scheduler: Any,
        *,
        radius_before: int = 2,
        radius_after: int = 8,
        direction_provider: Optional[Callable[[], int]] = None,
        rate_provider: Optional[Callable[[], float]] = None,
        config_provider: Optional[Callable[..., Any]] = None,
    ) -> None:
        """接入实时解码调度器（Stage E engine handoff）。

        scheduler 的 layer/source 配置由 renderer 在非热路径完成；engine
        只负责播放期间的 latest-only 窗口推进和 seek generation 同步，
        不在 tick 内打开媒体源或等待解码。
        """
        self._decode_scheduler = scheduler
        self._scheduler_radius_before = max(0, int(radius_before))
        self._scheduler_radius_after = max(0, int(radius_after))
        self._scheduler_direction_provider = direction_provider
        self._scheduler_rate_provider = rate_provider
        self._scheduler_config_provider = config_provider
        self._scheduler_config_signature = None

    def sync_generation_id(self, generation_id: int) -> None:
        """Synchronize an externally-owned renderer generation.

        The renderer also bumps generations for sequence/project changes that
        are not initiated by ``engine.seek``.  Those changes must invalidate
        scheduler rings just as strongly, otherwise a ready frame from the
        previous render plan could be consumed by the new plan.
        """
        generation_id = int(generation_id)
        with self._lock:
            changed = generation_id != self._generation_id
            self._generation_id = generation_id
            if changed:
                self._consecutive_misses = 0
        if not changed:
            return
        scheduler = self._decode_scheduler
        if scheduler is not None:
            try:
                scheduler.reset_generation(generation_id)
            except Exception as e:
                logger.debug("RealtimePlaybackEngine generation sync failed: %s", e)
        self._scheduler_config_signature = None

    def _configure_scheduler_for_frame(
        self,
        timeline_frame: int,
        decisions_by_clip: List[tuple],
    ) -> None:
        """Refresh scheduler layer/source config without decoding on the tick."""
        scheduler = self._decode_scheduler
        provider = self._scheduler_config_provider
        if scheduler is None or provider is None:
            return
        try:
            config = provider(int(timeline_frame), list(decisions_by_clip))
        except Exception:
            logger.debug("RealtimePlaybackEngine scheduler config failed", exc_info=True)
            return
        if not config:
            if self._scheduler_config_signature is not None:
                try:
                    scheduler.configure(layers=[])
                except Exception:
                    logger.debug("scheduler clear config failed", exc_info=True)
                self._scheduler_config_signature = None
            return
        signature = config.get("signature") if isinstance(config, dict) else None
        if signature == self._scheduler_config_signature:
            return
        if not isinstance(config, dict):
            logger.debug("scheduler config provider returned non-dict")
            return
        try:
            scheduler.configure(
                layers=list(config.get("layers") or []),
                capacity_per_stream=config.get("capacity_per_stream"),
                media_source_factory=config.get("media_source_factory"),
                source_frame_provider=config.get("source_frame_provider"),
            )
            self._scheduler_config_signature = signature
        except Exception:
            logger.debug("RealtimePlaybackEngine scheduler configure failed", exc_info=True)

    def _schedule_decode_window(self, timeline_frame: int, mode: PlaybackMode) -> None:
        """在实时 tick 中提交 bounded async prefetch，不阻塞 delegate。"""
        scheduler = self._decode_scheduler
        if scheduler is None or mode is not PlaybackMode.REALTIME:
            return
        prepare = getattr(scheduler, "prepare_window_async", None)
        if not callable(prepare):
            return
        direction = 1
        provider = self._scheduler_direction_provider
        if provider is not None:
            try:
                direction = -1 if int(provider()) < 0 else 1
            except Exception:
                direction = 1
        # P1-07: 变速播放（J/K/L）按倍率扩展行进方向 lookahead。
        # 只放大不缩小（慢放不比常速更需要前瞻），并设上限防止窗口
        # 超出 ring 容量意义之外的白白解码。
        radius_after = self._scheduler_radius_after
        rate_provider = self._scheduler_rate_provider
        if rate_provider is not None:
            try:
                rate = float(rate_provider())
            except Exception:
                rate = 1.0
            if rate > 1.0:
                radius_after = min(
                    _MAX_SCHEDULER_RADIUS_AFTER,
                    int(round(radius_after * min(rate, 8.0))),
                )
        try:
            prepare(
                center_frame=int(timeline_frame),
                direction=direction,
                radius_before=self._scheduler_radius_before,
                radius_after=radius_after,
                mode=mode,
                generation_id=self._generation_id,
            )
        except Exception as e:
            logger.debug("RealtimePlaybackEngine scheduler prefetch failed: %s", e)

    def build_layer_queries(self, timeline_frame: int) -> List[LayerSourceQuery]:
        """
        Final-1 (FINAL-P0-03)：构造当前帧的 layer source queries。

        调用注入的 layer_query_provider；如未注入，返回空列表（兼容旧测试）。
        """
        provider = self._layer_query_provider
        if provider is None:
            return []
        try:
            queries = provider(timeline_frame)
        except Exception as e:
            logger.error(f"layer_query_provider failed at frame {timeline_frame}: {e}")
            return []
        return list(queries)

    def _set_state(self, new_state: EngineState) -> None:
        # AUD-17300：状态迁移统一在本方法内持锁完成——旧实现仅 set_mode
        # 持锁，seek/_handle_miss/request_present 等无锁入口直接读写
        # self._state，多线程入口下存在数据竞争。RLock 对已持锁调用方
        # （set_mode）可重入；state_changed 信号在锁外发射，直连槽不会
        # 在持锁状态下执行外部代码。
        with self._lock:
            if self._state is new_state:
                return
            old = self._state
            self._state = new_state
        self.state_changed.emit(new_state.value)
        logger.debug(f"EngineState: {old.value} -> {new_state.value}")

    # ------------------------------------------------------------------
    # generation / seek
    # ------------------------------------------------------------------

    def seek(self, timeline_frame: int) -> None:
        """seek：递增 generation，清空 miss 计数。"""
        # AUD-17300：状态字段的读改写与 generation 递增同锁完成，
        # 避免无锁读取 self._state 与并发 set_mode 竞争。
        with self._lock:
            self._generation_id += 1
            self._consecutive_misses = 0
            generation = self._generation_id
            was_playing = self._state is EngineState.PLAYING
        # seek 后重置 budget back-pressure（避免 seek 后立即 drop）
        self._budget.reset_pressure()
        scheduler = self._decode_scheduler
        if scheduler is not None:
            try:
                scheduler.flush_seek(int(timeline_frame))
                scheduler.reset_generation(generation)
            except Exception as e:
                logger.debug("RealtimePlaybackEngine scheduler seek sync failed: %s", e)
        # P1-10: seek 后回到 NORMAL 档重新评估（不携带旧会话降档）。
        event = self._adaptive_quality.reset("seek")
        if event is not None:
            self._emit_quality_event(event)
        # seek 后状态回到 READY（等待新帧）
        if was_playing:
            self._set_state(EngineState.WARMING)
        logger.debug(f"Engine seek to frame={timeline_frame}, gen={generation}")

    def warmup(self, timeline_frame: int) -> None:
        """预热（拖入素材/首帧）。"""
        self._set_state(EngineState.WARMING)

    # ------------------------------------------------------------------
    # request_present（唯一热路径入口，规格书 §14.1）
    # ------------------------------------------------------------------

    def request_present(self, timeline_frame: int, deadline_ns: int = 0) -> bool:
        """
        请求 present 一个 timeline frame。

        这是 Edit 播放的**唯一热路径入口**（缺陷计划 RP-P0-05）。
        所有播放 tick 必须经过这里，由 engine 统一决策：
        - mode 判定（realtime 禁止 master，由 policy 在 delegate 内执行）。
        - generation 守卫。
        - miss/hold/rebuffer 处理。
        - telemetry 记录。

        Args:
            timeline_frame: 时间线帧号。
            deadline_ns: 截止时间戳（realtime 模式不阻塞等待）。

        Returns:
            True 表示 present 成功（delegate 返回 True），False 表示 miss/hold。
        """
        # AUD-17300：SHUTDOWN 检查并入持锁快照（旧实现在锁外读 _state）。
        with self._lock:
            if self._state is EngineState.SHUTDOWN:
                return False
            self._total_requests += 1
            gen = self._generation_id
            mode = self._mode

        # P0-01：统一 trace id 在唯一热路径入口分配，随 delegate → worker
        # → frame trace → telemetry 逐段携带，跨通道可对拍。
        trace_id = next_frame_trace_id()

        # Batch 6 (缺陷计划 RP-P1-14): 预算 back-pressure 检查。
        # 多轨超预算时 drop/hold，不阻塞 UI tick。
        render_start_ns = time.time_ns()
        if mode is PlaybackMode.REALTIME and not self._budget.before_render(timeline_frame):
            # back-pressure：drop this frame
            with self._lock:
                self._total_misses += 1
                self._consecutive_misses += 1
            self._handle_miss(timeline_frame, gen, reason="back_pressure")
            # P1-10: 背压 drop 属负载观测（miss_reason=back_pressure）。
            self._observe_adaptive_quality(False, "back_pressure")
            trace = PlaybackFrameTelemetry(
                timeline_frame=timeline_frame, generation_id=gen,
                trace_id=trace_id, mode=mode.value, total_ms=0.0,
                # P1-02: engine 通道只有播放头 tick（scrub 走 renderer 直达）。
                priority="playhead",
                miss_reason="back_pressure",
            )
            self._telemetry.record(trace)
            from src.features.edit.core.playback_debug import playback_trace
            playback_trace(
                "engine.back_pressure_drop",
                frame=timeline_frame,
                generation=gen,
                trace_id=trace_id,
            )
            # A0.1：被背压丢弃的请求在 trace 通道留下显式记录
            # （present_status='dropped'）——丢弃/超时样本单独可数，
            # 不混入成功帧统计。
            try:
                from src.shared.video.playback_frame_trace import (
                    EditPlaybackFrameTrace,
                    get_trace_collector,
                )
                get_trace_collector().record(EditPlaybackFrameTrace(
                    frame_index=timeline_frame,
                    page='edit_timeline',
                    generation_id=gen,
                    trace_id=trace_id,
                    dispatch_channel='engine',
                    priority='playhead',
                    fallback_reason='engine_back_pressure_drop',
                    present_status='dropped',
                ))
            except Exception:
                pass
            self.telemetry_recorded.emit(trace)
            return False

        t0 = time.perf_counter()

        # Final-1 (FINAL-P0-03): SourceResolver 作为每帧 source 决策唯一入口。
        # engine 构造当前帧的 LayerSourceQuery 列表，调用 resolve_layers()
        # 得到 SourceDecision，source_kind 写入 telemetry，并交给 delegate。
        source_decisions: List[SourceDecision] = []
        source_kinds: tuple = ()
        # Final-2: 把 (clip_id, decision) 配对传给 delegate，让 worker
        # 能按 clip_id 取出对应 layer 的 decision。
        decisions_by_clip: list = []
        resolver = self._source_resolver
        if resolver is not None:
            try:
                queries = self.build_layer_queries(timeline_frame)
                if queries:
                    source_decisions = resolver.resolve_layers(mode=mode, queries=queries)
                    source_kinds = tuple(
                        d.source_kind.value for d in source_decisions
                    )
                    for q, d in zip(queries, source_decisions):
                        decisions_by_clip.append((q.clip_id, d))
                        # Do not launch proxy encoding from the realtime tick.
                        # A background ffmpeg transcode steals the same decode,
                        # CPU and I/O budget that this call is protecting.
                        # Explicit media-pool/cache jobs own proxy generation.
                self._total_source_resolutions += 1
            except Exception as e:
                logger.error(f"SourceResolver.resolve_layers failed: {e}")

        # Scheduler prefetch is intentionally independent of source resolution:
        # the configured streams already represent the selected proxy/cache
        # source.  It is submitted before the presentation delegate so the
        # next tick can consume the ring without making this tick wait.
        #
        # AUD-17405：本段任何一步异常上抛都会跳过下面的 after_render，
        # budget 的 in-flight 占位被永久挂起（自愈前所有帧被 drop）。
        # 这里显式 abort 占位后再上抛，交由上层按既有路径处理。
        try:
            if mode is PlaybackMode.REALTIME:
                self._configure_scheduler_for_frame(timeline_frame, decisions_by_clip)
            self._schedule_decode_window(timeline_frame, mode)

            # 执行渲染（委托给 TimelinePreviewRenderer 或自定义 delegate）
            presented = self._execute_delegate(
                timeline_frame, gen, deadline_ns, mode, decisions_by_clip,
                trace_id=trace_id,
            )

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            # 记录预算耗时（before/after）
            self._budget.after_render(timeline_frame, render_start_ns, presented=presented)
        except Exception:
            self._budget.abort_render(timeline_frame)
            raise

        # P1-05: miss 归因在此解析一次（消费 worker 上报槽），telemetry 与
        # hold/rebuffer 决策使用同一 reason；_handle_miss 内部二次 resolve
        # 时槽已空，落回传入的默认值，两者必然一致。
        resolved_miss_reason = None
        if not presented:
            resolved_miss_reason = self._resolve_miss_reason(gen, "delegate_miss")

        # 记录 telemetry（Final-1: source_kinds 来自 SourceResolver 决策）
        trace = PlaybackFrameTelemetry(
            timeline_frame=timeline_frame,
            generation_id=gen,
            trace_id=trace_id,
            mode=mode.value,
            priority="playhead",
            source_kinds=source_kinds,
            total_ms=elapsed_ms,
            miss_reason=resolved_miss_reason,
        )
        self._telemetry.record(trace)
        self.telemetry_recorded.emit(trace)

        # P1-10: 本帧负载观测（miss 归因 + 预算 back-pressure）。
        self._observe_adaptive_quality(presented, resolved_miss_reason)

        # miss / hold / rebuffer 处理
        if presented:
            # AUD-17300：_state 读取并入计数锁窗口，避免与并发 set_mode 竞争。
            with self._lock:
                self._total_presented += 1
                self._consecutive_misses = 0
                warming = self._state in (EngineState.WARMING, EngineState.REBUFFERING)
            if warming:
                self._set_state(
                    EngineState.PLAYING if mode is PlaybackMode.REALTIME else EngineState.READY
                )
            self.frame_presented.emit(timeline_frame)
            return True
        else:
            with self._lock:
                self._total_misses += 1
                self._consecutive_misses += 1
            self._handle_miss(
                timeline_frame,
                gen,
                reason=resolved_miss_reason or "delegate_miss",
            )
            return False

    def request_scrub_present(
        self,
        timeline_frame: int,
        payload: Any,
        deadline_ns: int = 0,
    ) -> bool:
        """P1-01: scrub tick 的 engine 外壳入口。

        renderer 的 scrub 状态机（单飞/token/watchdog/never-freeze）原样
        保留；engine 只做三件事：分配统一 trace id（scrub 帧从此
        trace_id != 0，可与帧 trace 对拍）、把派发委托回
        delegate_scrub_render、记录 priority=scrub_latest 的 telemetry。
        刻意不走 budget/rebuffer 状态机——scrub 的 miss 语义是"显示
        最近帧、永不冻结"，与 REALTIME 的 hold/rebuffer 互斥。

        Args:
            timeline_frame: 目标帧。
            payload: renderer 私有派发参数（plan/token/decode size 等），
                engine 不解释，原样传给 delegate_scrub_render。
            deadline_ns: 单帧 deadline（可选，P1-06 分段归因用）。

        Returns:
            delegate 是否接受了派发（False = 队列拒绝/委托缺失）。
        """
        trace_id = next_frame_trace_id()
        t0 = time.perf_counter()
        ok = False
        delegate = self._delegate_scrub_render
        if delegate is not None:
            try:
                ok = bool(delegate(timeline_frame, deadline_ns, trace_id, payload))
            except Exception as e:
                logger.error(f"RealtimePlaybackEngine scrub delegate failed: {e}")
                ok = False
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self._total_requests += 1
            scrub_gen = self._generation_id
            if ok:
                self._total_presented += 1
            else:
                self._total_misses += 1
        self._telemetry.record(PlaybackFrameTelemetry(
            timeline_frame=timeline_frame,
            # A0.1：scrub 记录此前漏 generation_id（默认 0），切代归因断裂。
            generation_id=scrub_gen,
            trace_id=trace_id,
            mode="scrub",
            priority="scrub_latest",
            total_ms=elapsed_ms,
            miss_reason=None if ok else "scrub_dispatch_rejected",
        ))
        return ok

    def _execute_delegate(
        self,
        timeline_frame: int,
        generation_id: int,
        deadline_ns: int,
        mode: PlaybackMode,
        source_decisions: Optional[List[SourceDecision]] = None,
        trace_id: int = 0,
    ) -> bool:
        """执行渲染委托（TimelinePreviewRenderer.request_render 或自定义）。"""
        delegate = self._delegate_render
        if delegate is not None:
            try:
                # Final-1: 把 source decisions 传给 delegate。
                # P0-01: 支持 trace_id 关键字的 delegate 优先带 trace_id
                # （configure 时已探测签名，热路径零开销）。
                # 签名已确认兼容，此分支的 TypeError 只能来自 delegate
                # 内部业务代码——直接落进外层统一记错上抛，绝不用旧签名
                # 静默重试（重试会重复提交同一帧渲染 + trace 关联错乱）。
                if self._delegate_accepts_trace_id:
                    return bool(
                        delegate(
                            timeline_frame,
                            generation_id,
                            deadline_ns,
                            source_decisions,
                            trace_id=trace_id,
                        )
                    )
                # 兼容旧签名（4 参）的 fallback：只在 delegate 接受 keywords 时传。
                try:
                    return bool(
                        delegate(
                            timeline_frame,
                            generation_id,
                            deadline_ns,
                            source_decisions,
                        )
                    )
                except TypeError:
                    # 旧 3 参 delegate：直接忽略 decisions。
                    return bool(delegate(timeline_frame, generation_id, deadline_ns))
            except Exception as e:
                logger.error(f"RealtimePlaybackEngine delegate failed: {e}")
                return False

        # 默认委托：调用 TimelinePreviewRenderer.request_render
        # 注意：这条路径不带 source decisions（request_render 没有 decisions 参数），
        # 是潜在的退化陷阱。正常配置下 delegate_render 已注入，不会走到这里。
        # 如果走到这里，说明 configure 时没传 delegate_render，source decisions 会丢。
        renderer = self._renderer
        if renderer is None:
            return False
        if source_decisions:
            logger.warning(
                "RealtimePlaybackEngine: falling back to renderer.request_render "
                "without delegate_render — source_decisions will be lost. "
                "Configure delegate_render to preserve source policy."
            )
        try:
            # request_render 是异步的（入队 worker），engine 无法立即知道结果。
            # 渐进式集成：返回 True 表示"已提交"，实际 present 由 frame_ready 信号
            # 异步反馈。Batch 6 接入 scheduler 后改为同步 ready bundle 查询。
            renderer.request_render(timeline_frame)
            return True
        except Exception as e:
            logger.error(f"RealtimePlaybackEngine renderer.request_render failed: {e}")
            return False

    def note_playback_miss(
        self,
        reason: str,
        *,
        generation_id: int | None = None,
        timeline_frame: int | None = None,
    ) -> None:
        """P1-05: 渲染/解码异步路径上报真实 miss 归因。

        delegate 契约只返回 bool；当 worker 真正知道失败模式
        （GPU 结果 drop、解码失败、ring 未命中）时调用本方法，下一次
        engine miss 将消费该归因取代默认的 delegate_miss 分类。
        旧 generation 的上报直接丢弃（seek 后不得污染新会话）。
        """
        if not reason:
            return
        with self._lock:
            if generation_id is not None and int(generation_id) != self._generation_id:
                return
            self._noted_miss = (self._generation_id, str(reason))
        logger.debug(
            "RealtimePlaybackEngine noted miss reason=%s frame=%s",
            reason,
            timeline_frame,
        )

    def _resolve_miss_reason(
        self,
        generation_id: int,
        default_reason: str,
    ) -> str:
        """Resolve this miss's reason, consuming any worker-noted classification."""
        with self._lock:
            noted = self._noted_miss
            self._noted_miss = None
        if noted is not None and noted[0] == int(generation_id) and noted[1]:
            return noted[1]
        return default_reason

    def _handle_miss(
        self,
        timeline_frame: int,
        generation_id: int,
        reason: str = "delegate_miss",
    ) -> None:
        """处理 miss（P1-05 统一语义）。

        归因分类驱动 hold/rebuffer 决策：
        - PROXY_PENDING → 立即 REBUFFERING（等待代理无意义）。
        - UNSUPPORTED_EFFECT → 永不 REBUFFERING，发 playback_warning。
        - 其余瞬态类别 → hold 优先，连续 miss 达阈值才 REBUFFERING。
        """
        resolved_reason = self._resolve_miss_reason(generation_id, reason)
        classified = classify_miss_reason(resolved_reason)
        miss = DecodeMiss(
            timeline_frame=timeline_frame,
            clip_id="",
            asset_id="",
            source_frame=timeline_frame,
            source_kind=(
                SourceKind.PLACEHOLDER
                if classified is MissReason.PROXY_PENDING
                else SourceKind.NONE
            ),
            reason=resolved_reason,
            generation_id=generation_id,
        )
        # AUD-17300：miss 计数与状态读取同锁快照（request_present 已在
        # 锁外递增 _consecutive_misses，此处读取必须持锁），回调/信号在
        # 锁外发射。
        with self._lock:
            consecutive_misses = self._consecutive_misses
            engine_state = self._state
        should_rebuffer = self._policy.should_rebuffer(
            miss,
            consecutive_miss_count=consecutive_misses,
            threshold=self._rebuffer_threshold,
        )
        if should_rebuffer and engine_state is EngineState.PLAYING:
            self._set_state(EngineState.REBUFFERING)
            self.playback_warning.emit("rebuffering", {
                "frame": timeline_frame,
                "consecutive_misses": consecutive_misses,
                "reason": resolved_reason,
            })
        else:
            if consecutive_misses == 1:
                # 首次 miss：hold（不立即 rebuffer）
                with self._lock:
                    self._total_holds += 1
            if classified is MissReason.UNSUPPORTED_EFFECT:
                # rebuffer 不会让无实时路径的效果变好：hold + 可见警告。
                # 每个 miss 段只警告一次（首次），避免逐帧刷屏。
                if consecutive_misses == 1:
                    self.playback_warning.emit("unsupported_effect", {
                        "frame": timeline_frame,
                        "reason": resolved_reason,
                    })

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """关闭引擎。"""
        # A1.1：注销 GC 回调并输出观测摘要（观测期结束的证据落日志）。
        observer = getattr(self, "_gc_observer", None)
        if observer is not None:
            try:
                summary = observer.stop()
                logger.info(f"GC pause observer summary: {summary}")
            except Exception as e:
                logger.debug(f"GC observer stop failed: {e}")
            self._gc_observer = None
        state_signal = self._playback_controller_state_signal
        if state_signal is not None:
            try:
                state_signal.disconnect(self._on_playback_controller_state_changed)
            except (TypeError, RuntimeError):
                pass
        self._playback_controller_state_signal = None
        self._playback_controller = None
        self._controller_playback_active = None
        self._set_state(EngineState.SHUTDOWN)
        self._renderer = None
        self._delegate_render = None
        self._source_resolver = None
        self._layer_query_provider = None
        self._decode_scheduler = None
        self._scheduler_config_provider = None
        self._scheduler_config_signature = None

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "mode": self._mode.value,
                "generation": self._generation_id,
                "total_requests": self._total_requests,
                "total_presented": self._total_presented,
                "total_misses": self._total_misses,
                "total_holds": self._total_holds,
                "total_source_resolutions": self._total_source_resolutions,
                "has_source_resolver": self._source_resolver is not None,
                "has_layer_query_provider": self._layer_query_provider is not None,
                "has_decode_scheduler": self._decode_scheduler is not None,
                "has_scheduler_config_provider": self._scheduler_config_provider is not None,
                "decode_scheduler": (
                    self._decode_scheduler.stats()
                    if self._decode_scheduler is not None
                    and hasattr(self._decode_scheduler, "stats") else None
                ),
                "consecutive_misses": self._consecutive_misses,
                "telemetry": self._telemetry.summary(),
            }


# =============================================================================
# 单例
# =============================================================================

_default_engine: Optional[RealtimePlaybackEngine] = None
# AUD-17305：单例 check-then-act 必须持锁——无锁双检在并发首调下可
# 构造多个 engine（各自 warmup/线程/预算状态），除浪费外还会让"全局
# 默认 engine"契约失效。
_default_engine_lock = threading.Lock()


def get_default_engine() -> RealtimePlaybackEngine:
    """获取默认 engine 单例。"""
    global _default_engine
    if _default_engine is None:
        with _default_engine_lock:
            if _default_engine is None:
                _default_engine = RealtimePlaybackEngine()
    return _default_engine
