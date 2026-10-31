# Batch 011 — shared/effects 预览画质分级 静态审查

- 审查日期: 2026-09-28
- 审查范围: `src/shared/effects/__init__.py`、`src/shared/effects/preview_quality.py`
- 问题编号区间: AUD-1100 – AUD-1199
- 交叉验证（只读）: 全库 grep 管理器的全部调用点（`src/features/color/core/plugin_manager.py`、`src/shared/video/video_playback.py`、`src/shared/ui/effect_degradation_indicator.py`）以确认接线与线程场景

---

## src/shared/effects/__init__.py (行数: 28)

### 发现

（无）

### 通过项
- 仅做 re-export，import 列表与 `__all__` 完全一致，无遗漏/多余导出
- 无循环导入风险（preview_quality.py 仅依赖标准库）
- 无状态、无信号、无资源持有，无线程安全面
- 无 GPU/GLSL 相关内容，无 HDR 语义影响

---

## src/shared/effects/preview_quality.py (行数: 587)

### 发现

- [AUD-1100] [Major] 播放降级系统从未激活：全库无任何 `set_playback_state(True)` 调用
  - 位置: src/shared/effects/preview_quality.py:330
  - 证据:
    ```python
    def set_playback_state(self, is_playing: bool) -> None:
        ...
        self._state.is_playing = is_playing
    ```
    全库 grep `get_effect_preview_quality_manager` 全部调用点后确认：唯一调用管理器级 `set_playback_state` 的代码是 src/shared/video/video_playback.py:2118（`get_effect_preview_quality_manager().set_playback_state(False)`，且位于 v2 桩引擎的 `stop_playback` 中，其 `start_playback` 已是 "GStreamer pipeline removed" 的报错桩）。管理器 `_state.is_playing` 初始为 False 且无任何 True 写入路径；effect_degradation_indicator.py:183 注释宣称 "PlaybackCoordinator 已经设置了状态"，但 PlaybackCoordinator（src/shared/video/playback_state.py）从未触碰该管理器。
  - 影响: P3-4 整个播放降级功能为死代码——真实播放期间 `get_degraded_params`/`should_skip_effect`/`is_playback_degradation_active` 永远返回"不降级"，film_grain/AI matte/denoise 等重型效果在播放时照常执行，4K 实时流畅性目标失效；UI 指示器也永远显示灰色"暂停"态。
  - 建议: 在 v3 PlaybackCoordinator 的 start/stop（或 engine 的 play_state_changed 信号）处调用 `manager.set_playback_state(...)`，并补一条端到端验证（播放中 `is_playback_degradation_active()` 应为 True）。
  - 置信度: 高（对 src/ 全量 grep 穷尽管理器全部引用点后确认）

- [AUD-1101] [Major] 无锁共享可变状态 + 非线程安全的懒汉式全局单例
  - 位置: src/shared/effects/preview_quality.py:546
  - 证据:
    ```python
    _manager_instance: Optional[EffectPreviewQualityManager] = None

    def get_effect_preview_quality_manager() -> EffectPreviewQualityManager:
        global _manager_instance
        if _manager_instance is None:
            _manager_instance = EffectPreviewQualityManager()
        return _manager_instance
    ```
    同时 `clear_degraded()`（:293 `self.degraded_effects.clear()`）与 UI 500ms QTimer 轮询中的遍历（:517 `for effect_type in self._state.degraded_effects:`）无锁并发；`register_heavy_effect`（:437 写 `_heavy_effects`）与 `_apply_playback_degradation`（:365 `for effect_type, definition in self._heavy_effects.items():`）同样无锁；`get_degradation_state()`（:352-354）直接返回内部可变对象。
  - 影响: 单例在 UI 线程（指示器轮询/复选框、plugin_manager 注册）与播放/渲染参数构建路径（video_playback.py:1978，项目渲染走独立线程/任务队列）之间共享且全部无锁。遍历 `degraded_effects`/`_heavy_effects.items()` 时被并发 `clear()`/`pop()`/注册会抛 `RuntimeError: ... changed size during iteration`；首次并发初始化可能产生两个管理器实例导致 UI 与渲染降级判断不一致。
  - 建议: 单例创建加 `threading.Lock`（double-checked）；管理器内部读写加同一把锁，或把 `PlaybackDegradationState` 改为不可变快照（替换而非原地修改），`get_degradation_state`/`get_degradation_summary` 返回拷贝。
  - 置信度: 中（无锁事实确定；实际触发取决于渲染参数构建所在线程，但 UI 轮询 + 插件注册 + 播放切换三类并发入口已确认存在）

- [AUD-1102] [Major] 导出路径无任何保护，preview/export 语义一致性仅靠调用方约定
  - 位置: src/shared/effects/preview_quality.py:384
  - 证据:
    ```python
        # 检查是否在播放状态
        if not self._state.is_playing:
            return original_params

        # 检查降级是否启用
        if not self._degradation_enabled:
            return original_params
    ```
    模块头声明（:9）"导出时：不受影响，完整质量"，但全文件无任何 export 相关 API/标志；`:272` 的文档反而把 `export` 列为降级原因之一（"degradation_reason: 降级原因（playback, seek_recovery, export）"），与 :9 自相矛盾。
  - 影响: 降级唯一门控是 `is_playing`。若导出在播放未停止时启动（或导出管线复用 `get_degraded_effect_params` 便捷函数），降级参数会泄漏进导出帧，违反 .clinerules 的 preview/export 语义一致性要求。当前因 AUD-1100（is_playing 永远为 False）被掩盖，一旦接线修复即暴露。
  - 建议: 提供 `begin_export()/end_export()` 上下文管理（导出期间强制 FULL 并冻结状态切换），或至少在导出管线断言 `not is_playback_degradation_active()`；同时修正 :272 文档中 "export" 作为降级原因的错误描述。
  - 置信度: 中（模块内确无导出保护为事实；是否实际触发取决于导出管线实现，属后续批次）

- [AUD-1103] [Major] 播放中重新启用降级后 `degraded_effects` 不重建，状态追踪与决策路径脱钩
  - 位置: src/shared/effects/preview_quality.py:464
  - 证据:
    ```python
        self._degradation_enabled = enabled

        if not enabled and self._state.is_playing:
            # 关闭降级时清除所有降级效果
            self._state.clear_degraded()
    ```
    只处理了关闭分支；而决策路径 `get_degraded_params`（:385/:389/:393）与 `should_skip_effect`（:411）只检查 `is_playing`+`_degradation_enabled`，不查 `degraded_effects`，`is_effect_degraded`（:420-422）却只查列表。消费端 src/shared/video/video_playback.py:2011 依赖列表：`if blur_radius > 20.0 and quality_manager.is_effect_degraded("blur_high_radius"):`
  - 影响: 播放中取消勾选"降级"再重新勾选后：`degraded_effects` 保持为空 → 高半径 blur 半径限制永久失效，而 film_grain/AI skip（走 `should_skip_effect`/`get_degraded_params`）在同一播放会话内继续降级——同帧内策略分裂；UI 摘要同时显示 0 个降级效果（指示器回到灰色态）。
  - 建议: `set_degradation_enabled(True)` 且 `is_playing` 时调用 `_apply_playback_degradation()` 重建列表；统一决策入口，让 `get_degraded_params`/`should_skip_effect` 也以 `degraded_effects`（或同一快照）为准。
  - 置信度: 高

- [AUD-1104] [Major] 降级参数为覆盖式合并而非钳制，可能反向增大计算量/改变画面
  - 位置: src/shared/effects/preview_quality.py:140
  - 证据:
    ```python
            if self.preview_quality == PreviewQualityLevel.MEDIUM:
                # 中度降级：应用更激进的替代
                return {**original_params, **self.degradation_params}
    ```
    定义处注释与取值（:227-229）：
    ```python
        degradation_params={
            "blur_radius": 20.0,  # 限制最大半径
            "blur_iterations": 1,  # 限制迭代次数
        },
    ```
    以及 `full_res_mask`（:241 `"mask_scale": 0.5,  # 降级到半分辨率`）。
  - 影响: 原始 `blur_radius=10` 经通用 API `get_degraded_effect_params("blur_high_radius", ...)` 降级后变成 20（半径与成本反而升高）；`mask_scale=0.25`（已低于半分辨率）被抬升到 0.5。"限制最大半径"的语义应为 `min(original, cap)`。当前 video_playback.py:2011 以 `blur_radius > 20.0` 前置判断规避了 blur 场景，但通用 API 与 `full_res_mask` 无任何保护；且阈值 20.0 在 preview_quality.py:228 与 video_playback.py:2011 两处硬编码，双源易漂移。
  - 建议: 在 `get_degraded_params` 中按参数语义做 clamp（radius/scale 取 min），或为 `degradation_params` 支持 `min`/`max` 修饰；阈值统一收敛到本模块（见 AUD-1107）。
  - 置信度: 高（覆盖语义可从代码直接证明；当前主消费端有规避，实际影响面中等）

- [AUD-1105] [Major] SKIP 路径硬编码 `{"enabled": False}`：丢弃全部原始参数，且使已注册的 `degradation_params` 成为死配置
  - 位置: src/shared/effects/preview_quality.py:132
  - 证据:
    ```python
        if self.preview_quality == PreviewQualityLevel.SKIP:
            # 完全跳过：设置 enabled=False
            return {"enabled": False}
    ```
    而注册表中 `ai_matte` 定义的是另一个键（:176）：
    ```python
        degradation_params={"ai_matte_enabled": False},
    ```
    （`ai_matte_update` 的 `update_enabled`（:186）同理）
  - 影响: 对参数键不是 `enabled` 的效果（如 ai_matte 的 `ai_matte_enabled`），通用降级 API 的"跳过"不生效或注入未知键；返回字典仅含 `enabled`，调用方若整体替换参数集会丢失其余参数。三个 SKIP 定义注册的 `degradation_params` 永远不会被读取（死配置）。当前消费端用 `should_skip_effect` 规避了该路径，属公共 API 的明确风险。
  - 建议: SKIP 分支改为 `{**original_params, **self.degradation_params}`（与 plugin_manager.py:259 注册的 `{"enabled": False}` 保持一致），删除硬编码。
  - 置信度: 高（死配置事实可证；实际触发取决于消费方是否走通用 API，影响面中等）

- [AUD-1106] [Suggestion] `_apply_playback_degradation` 把注册表全部条目标记为已降级，UI 指示器误报
  - 位置: src/shared/effects/preview_quality.py:365
  - 证据:
    ```python
        for effect_type, definition in self._heavy_effects.items():
            if definition.preview_quality.needs_degradation:
                self._state.add_degraded(effect_type)
    ```
  - 影响: 播放一开始，无论当前时间线是否使用这些效果，`degraded_effects` 都包含全部注册项（film_grain、AI Matte、Denoise…），指示器（effect_degradation_indicator.py `_update_display`）会显示"跳过: Film Grain, AI Matte..."，即使工程中一个都没有；`is_effect_degraded` 语义同样失真。
  - 建议: 由渲染/参数构建路径在实际触发降级时回报 `add_degraded`，管理器只维护汇报值而非预填全表。
  - 置信度: 高

- [AUD-1107] [Suggestion] 自定义阈值 API 与降级决策完全断链，设置阈值静默无效
  - 位置: src/shared/effects/preview_quality.py:484
  - 证据:
    ```python
    def check_threshold_degradation(
        self,
        effect_type: str,
        current_value: float
    ) -> bool:
        ...
        threshold = self._custom_thresholds.get(effect_type)
    ```
    `_custom_thresholds` 在 `get_degraded_params`/`should_skip_effect`/`_apply_playback_degradation` 中均未被读取（grep 全库亦无调用 `check_threshold_degradation`）。
  - 影响: `set_custom_threshold("blur_high_radius", 10.0)` 之后系统行为完全不变；blur 阈值 20.0 硬编码在定义（:228）与 video_playback.py:2011 两处，无法通过阈值 API 调整。
  - 建议: 在 `get_degraded_params` 内结合 `_custom_thresholds` 与当前参数值判断是否降级，或删除该 API 避免误导。
  - 置信度: 高

- [AUD-1108] [Suggestion] `last_degradation_time` 声明后永不更新（恒为 0.0）
  - 位置: src/shared/effects/preview_quality.py:280
  - 证据:
    ```python
        degraded_effects: List[str] = field(default_factory=list)
        degradation_reason: str = "none"
        is_playing: bool = False
        last_degradation_time: float = 0.0
    ```
    `set_playback_state`/`_apply_playback_degradation`/`add_degraded` 均不写该字段。
  - 影响: 任何依赖该时间戳的逻辑（如降级恢复延迟）拿到的是无效值；死字段误导维护者。
  - 建议: 在 `add_degraded` 或 `set_playback_state(True)` 中写入 `time.monotonic()`，或删除该字段。
  - 置信度: 高

- [AUD-1109] [Suggestion] HIGH/MEDIUM/LOW 三个降级分支实现完全相同，末尾 `return` 不可达；返回值所有权不一致
  - 位置: src/shared/effects/preview_quality.py:136
  - 证据:
    ```python
        if self.preview_quality == PreviewQualityLevel.HIGH:
            # 轻度降级：应用部分替代参数
            return {**original_params, **self.degradation_params}

        if self.preview_quality == PreviewQualityLevel.MEDIUM:
            # 中度降级：应用更激进的替代
            return {**original_params, **self.degradation_params}
        ...
        return original_params
    ```
    全枚举已覆盖（FULL/SKIP 提前返回），:148 的 `return original_params` 不可达。另外 `get_degraded_params` 未降级路径（:386/:396）返回调用方原 dict 引用，降级路径返回新 dict，所有权不一致，调用方原地修改会污染原始参数。
  - 建议: 合并为单一 `return {**original_params, **self.degradation_params}`（如未来需分级差异化再展开）；统一返回拷贝（`dict(original_params)`）并注明。另：:516 `display_list = []` 缺类型注解（`List[str]`），全文件其余注解完整。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：模块为纯策略层，不持有任何 ModernGL 纹理/program/buffer，画质分级不直接触发 GPU 资源创建或释放，无泄漏面
- GLSL/HDR 正确性：无 GLSL 代码；降级参数中的浮点字面量（0.0/20.0/0.5）书写规范；策略层不触碰像素数据，对 HDR 数据无 clamp/精度损失操作（film_grain 降级为 intensity=0.0 属禁用而非截断）
- 信号防递归：模块为纯 Python（无 Qt 信号），不存在信号成环风险（UI 侧指示器已用 blockSignals 防递归，属其他批次范围）
- 异常与取消路径：全文件无 try/except，无宽泛 except 吞错；无资源获取故无失败路径泄漏
- 输入验证：`_heavy_effects.get`/`_custom_thresholds.get` 对未知键安全返回 None 并回退到"不降级"；`pop(effect_type, None)` 安全；`isinstance` 不需要（消费方传入 dict 已约定）
- 性能模式：`get_degraded_params` 为 O(1) 字典合并，无每帧重复探测或无效缓存的明显项
- 导出的公共 API 与 `__init__.py` re-export 一致，无悬空导出
