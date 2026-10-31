# 批次报告：batch-168 [P1] src/features/edit/core/

编号区间：AUD-16800–AUD-16899。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

汇总：Critical 0 · Major 4 · Suggestion 14。

---

## src/features/edit/core/effect_instance.py (行数: 803)

### 发现

- [AUD-16800] [Major] `AnimatableParam.add_keyframe` 原地修改共享 `KeyframeTrack`，破坏 copy-on-write 与 undo/历史一致性
  - 位置: src/features/edit/core/effect_instance.py:106
  - 证据:
    ```python
    if self.keyframes is None:
        track = KeyframeTrack(param_name="", ...)
    else:
        track = self.keyframes          # ← 复用同一对象，未复制

    new_kf = Keyframe(frame, value, interpolation)
    track.add_keyframe(new_kf)          # ← 原地 append/sort
    ```
    而 `src/shared/keyframe.py:150-155`（`KeyframeTrack.add_keyframe`）确实原地改 `self.keyframes` 列表：`self.keyframes[i] = keyframe ... self.keyframes.append(keyframe); self.keyframes.sort()`。
  - 影响: `remove_keyframe`/`update_keyframe` 都新建轨道（正确的 COW），唯独 `add_keyframe` 复用并原地修改旧轨道。frozen 的 `EffectInstance` 经 `with_keyframe_added` 产生新实例，但新旧实例（及 undo/历史/其他 clip 引用）共享同一个可变 track——加关键帧后，历史中旧 `EffectInstance` 的关键帧也被污染，undo/redo、历史跳转看到的旧状态包含新关键帧；同时 UI 线程 `add_keyframe` 的 `sort()` 与渲染线程 `KeyframeTrack.get_value` 的遍历在共享 list 上竞态（timeline_v2.py:794 起 `ClipInstance.with_keyframe_added` 与 commands.py:3041 `_apply_to_effect` 均走此路径）。
  - 建议: 与 remove/update 保持一致——先复制轨道再修改（`KeyframeTrack(param_name=..., keyframes=list(self.keyframes.keyframes), ...)` 后对副本 `add_keyframe`），或给 `KeyframeTrack` 提供 `with_keyframe_added` 纯函数。
  - 置信度: 高

- [AUD-16801] [Suggestion] `AnimatableParam` 未加 `frozen=True`，违反模块自身"不可变保证线程安全"的设计原则
  - 位置: src/features/edit/core/effect_instance.py:35
  - 证据:
    ```python
    @dataclass
    class AnimatableParam:
        ...
        base_value: float
        keyframes: Optional[KeyframeTrack] = None
    ```
    模块头（第 8 行）声明 "Immutable data class (frozen=True) for thread safety"，`EffectInstance`/`EffectDefinition` 均 frozen，唯独内嵌的 `AnimatableParam`（及其内 `KeyframeTrack`）可变。
  - 影响: frozen 容器内藏可变对象，防误改/跨线程共享只读的约定失效；AUD-16800 正是通过这条缝隙发生的。
  - 建议: 至少给 `AnimatableParam` 加 `frozen=True`（方法已全部返回新实例，改造成本低）；`KeyframeTrack` 的共享问题随 AUD-16800 的复制修复缓解。
  - 置信度: 高

- [AUD-16802] [Suggestion] `EffectInstance.__eq__/__hash__` 仅按 `effect_id` 判等，同 id 不同参数被判为相等
  - 位置: src/features/edit/core/effect_instance.py:635
  - 证据:
    ```python
    def __hash__(self) -> int:
        return hash(self.effect_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EffectInstance):
            return NotImplemented
        return self.effect_id == other.effect_id
    ```
  - 影响: 任何用 `==`/set/dict 做变更检测、去重或缓存的调用方，对"同 id 参数已改"的实例会漏判（例如判断效果参数是否变化以决定是否重渲染）。
  - 建议: 保持身份语义则应在文档中显式警告"变更检测不得用 =="；或提供 `params_equal()` 辅助。审查调用方是否依赖 `==` 做变更检测。
  - 置信度: 中

- [AUD-16803] [Suggestion] `from_dict` 对工程文件（外部数据）的迁移转换无容错，坏值直接抛异常中断加载
  - 位置: src/features/edit/core/effect_instance.py:611
  - 证据:
    ```python
    if effect_type == "gaussian_blur":
        effect_type = "blur"
        intensity = float(params.pop("intensity", 0.0) or 0.0)
        params.setdefault("blur_type", "gaussian")
        params.setdefault("mix", max(0.0, min(1.0, intensity / 100.0)))
    elif effect_type == "sharpen" and "intensity" in params:
        intensity = float(params.pop("intensity", 0.0) or 0.0)
    ```
  - 影响: 损坏/手改的工程文件中 `intensity` 若为非数值字符串，`float()` 抛 ValueError 且无上下文，整个工程加载失败；`order` 为负或非数值同样经 `__post_init__`/解包抛错。
  - 建议: 迁移分支用 try/except 包裹并回退默认值 + 记日志（保留原 params 以便排查），使单个坏效果不拖垮整个工程。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：纯数据模型，无 GL/GPU 对象。
- GLSL·HDR 正确性：无着色器代码；迁移中的 clamp 仅作用于参数域映射（mix/amount），非 HDR 数据截断。
- 信号防递归：无 Qt 信号。
- 类型注解：公共 API 注解完整。
- 序列化对称性：`to_dict`/`from_dict` 字段对应，`plugin_identity` 仅在存在时写出（向后兼容处理正确）。

---

## src/features/edit/core/effect_registry.py (行数: 886)

### 发现

- [AUD-16804] [Major] catalog 加载失败回退分支引用已被删除的 `GAUSSIAN_BLUR_EFFECT`/`SHARPEN_EFFECT`，触发即 NameError 崩溃
  - 位置: src/features/edit/core/effect_registry.py:577
  - 证据:
    ```python
        except Exception as catalog_err:  # noqa: BLE001
            # Catalog must never block the registry — fall back to the
            # legacy hardcoded entries so the panel still shows something.
            logger.warning(...)
            self.register(GAUSSIAN_BLUR_EFFECT)
            self.register(SHARPEN_EFFECT)
    ```
    全库检索确认 `GAUSSIAN_BLUR_EFFECT`/`SHARPEN_EFFECT` 仅在本文件注释（559-560 行）与这两行出现，无定义无导入（T-P1-20 已删除这两个 legacy 定义）。
  - 影响: 一旦 `src.features.shared.effects` 导入/构建失败（循环导入、插件异常等），except 分支本身抛 `NameError`，`EffectRegistry.__init__` → `get_effect_registry()` 单例构造直接崩溃——"优雅回退"变成必然崩溃，Effects 面板等所有调用点连带失败。
  - 建议: 删除死回退分支（catalog 失败时仅告警并维持已注册条目），或注册两个现存的占位 `EffectDefinition`。
  - 置信度: 高

- [AUD-16805] [Suggestion] 宽泛 except 使单个畸形 spec 中断整个 catalog 注册循环（并叠加 AUD-16804 变成崩溃）
  - 位置: src/features/edit/core/effect_registry.py:564
  - 证据:
    ```python
        try:
            from src.features.shared.effects import get_effect_catalog
            catalog = get_effect_catalog()
            for spec in catalog.get_all_specs():
                self.register(_spec_to_definition(spec))
        except Exception as catalog_err:  # noqa: BLE001
    ```
    `_paramdef_to_schema`（85-90 行）执行 `float(param_def.min_val)`、`float(param_def.step)`，spec 作者传入 `None`（`ParamDef` 字段无强校验，见 base_node.py:226-228）即 TypeError。
  - 影响: 一个坏 spec 让其余全部视频滤镜从面板消失（或经 AUD-16804 直接崩溃），故障面被不必要地放大。
  - 建议: 循环体逐 spec try/except + 告警跳过；`_paramdef_to_schema` 对 None 做 `float(x or 0.0)` 防御。
  - 置信度: 中

- [AUD-16806] [Suggestion] 类与方法的 docstring 与实际目录结构漂移（文档腐化）
  - 位置: src/features/edit/core/effect_registry.py:519
  - 证据:
    ```python
    Categories:
        - transform: Transform, Crop, Opacity
        - blur: Gaussian Blur
        - sharpen: Sharpen
        - stylize: Vignette
    ```
    而 559-563 行与 `_CATEGORY_ORDER`（188-195 行，含 generator、无 blur）表明这些 legacy 条目已被移除；`get_categories` docstring（629 行）也仍写 "transform → blur → filter"。
  - 影响: 误导后续维护者与 Agent（本批次审阅时即产生噪音）；目录契约真实来源是 `_CATEGORY_ORDER`。
  - 建议: 更新两处 docstring 与 `_CATEGORY_ORDER` 对齐。
  - 置信度: 高

- [AUD-16807] [Suggestion] 模块底部延迟 `import EffectInstance`、单例与惰性全局缓存无锁
  - 位置: src/features/edit/core/effect_registry.py:776
  - 证据:
    ```python
    # Import EffectInstance for type hint in create_effect_instance
    from src.features.edit.core.effect_instance import EffectInstance  # noqa: E402
    ```
    另：`_registry_instance`（709-722 行）与 `_AUDIO_DEFAULT_PARAMS`（147-168 行）的惰性初始化均无锁。
  - 影响: 底部 import 属风格问题（顶部已从同一模块导入 EffectDefinition/ParamSchema，可合并）；无锁惰性初始化在多线程首调时存在良性竞态（重复构建、结果幂等），当前影响低。
  - 建议: import 上移合并；若未来非 UI 线程调用注册表，补 `threading.Lock`。
  - 置信度: 高（事实），影响低

### 通过项
- 状态提交一致性：注册表为启动期只读字典，无 undo/持久化参与。
- 输入验证：`ParamSchema.__post_init__` 校验 type/choices；`is_attachable_video_effect` 对 ghost/generator 类型拦截正确。
- 健康检查（849-886 行）：只读探测、异常有日志与结构化 reason，未掩盖领域错误。
- GPU/GLSL/信号维度：不涉及。

---

## src/features/edit/core/effect_render_operations.py (行数: 177)

### 发现

- [AUD-16808] [Major] FX-C02 修复计划（render-operation plan）无任何生产消费者，GPU 预览渲染器仍走 legacy 全屏后置合成路径
  - 位置: src/features/edit/core/effect_render_operations.py:97
  - 证据: 模块自述为增量过渡：
    ```python
    The builder is additive: it does NOT replace ``FrameRenderPlan`` (whose
    ``layers`` still include adjustment clips for backwards compatibility).
    Consumers may opt into the operation plan; the legacy
    ``adjustment_layers`` post-composite path remains until P2's GPU
    renderer migration consumes this plan.
    ```
    全库检索：`build_render_operations` 仅被 `tests/edit/test_effect_render_operations.py` 引用，src/ 下零消费者；生产渲染器 `src/features/edit/core/edit_gpu_preview_renderer.py:2377-2380` 仍在用 `task.adjustment_layers` 的后置合成。
  - 影响: 本模块要修的 FX-C02 正确性错误（Adjustment Clip 影响其上方轨道）在运行时依旧存在——正确性修复只落在数据结构与测试上，产品行为未变。导出/CPU 回退同样未消费该计划。
  - 建议: 将 P2 迁移提级或在 legacy 路径上按 z 序内联应用 adjustment；至少在任务表跟踪"FX-C02 运行时未生效"。
  - 置信度: 高（无生产消费者为检索事实；运行时行为错误依据 legacy 路径即旧行为）

- [AUD-16809] [Suggestion] 循环体内 import + 弱类型字段 `tuple[Any, ...]`
  - 位置: src/features/edit/core/effect_render_operations.py:147
  - 证据:
    ```python
    for layer in frame_plan.layers:
        ...
        if asset_type == _ADJUSTMENT_TYPE:
            ...
            from src.features.edit.core.adjustment_composite import transform_is_identity
    ```
    另 `ApplyAdjustment.effects: tuple[Any, ...]`、`transform: tuple[Any, ...]`（79-82 行）。
  - 影响: 每层触发一次 import 缓存查找（微小开销 + 风格问题，且 import 失败会在循环内反复抛错）；`Any` 弱化了纯数据计划的类型保障。
  - 建议: import 上移到模块顶部；effects 标注为 `tuple[EffectInstance, ...]`（TYPE_CHECKING 导入避免循环依赖）。
  - 置信度: 高

### 通过项
- 线程安全：纯数据构建，`effects_snapshot` 默认路径 `tuple(...)` 复制序列快照，符合"渲染线程无竞态"意图（EffectInstance 本身不可变；残余风险在 AUD-16800 的共享 track）。
- GPU 资源生命周期：不涉及。
- 正确性细节：`abs(opacity - 1.0) <= 1e-4` 的 ε 比较正确——已核实 `ClipLayerInfo.opacity` 为 0-1 浮点（timeline_render_plan.py:940-950）；恒等 adjustment 跳过逻辑正确；无操作时 `continue` 语义正确。
- 输入验证：resolver 为 None 时退化为 legacy 行为，文档明确。

---

## src/features/edit/core/export_manifest.py (行数: 435)

### 发现

- [AUD-16810] [Suggestion] `write_manifest_file` 失败时遗留 `.tmp` 文件、无 fsync、固定 tmp 名
  - 位置: src/features/edit/core/export_manifest.py:429
  - 证据:
    ```python
    def write_manifest_file(path: str, manifest: Dict[str, Any]) -> str:
        """manifest 原子落盘（临时文件 + os.replace），返回路径。"""
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return path
    ```
  - 影响: `json.dump` 中途抛错（如 to_dict 内出现不可序列化对象）时 `.tmp` 残留在交付目录；断电/崩溃窗口内 os.replace 前无 fsync，跨崩溃原子性不完整；同路径并发写会共用同一 tmp 名互相覆盖。
  - 建议: try/except 中清理 tmp；`fh.flush()+os.fsync(fh.fileno())` 后再 replace；tmp 名加 pid/uuid。
  - 置信度: 高

- [AUD-16811] [Suggestion] 输出文件被完整读取两遍（SHA-256 + 全量解码 probe），4K 长导出的后台 IO/CPU 成倍
  - 位置: src/features/edit/core/export_manifest.py:412
  - 证据:
    ```python
    manifest["output"] = _output_snapshot(output_path)      # 全文件 SHA-256
    output_probe = (
        probe_output_media(output_path)                     # 再全量 demux+decode
        if probe_media and manifest["output"].get("present") else None)
    ```
    `probe_output_media`（260-273 行）对全部 packet `decode()` 逐帧计数（仅跳过 AudioFrame 计数，音频包仍解码）。
  - 影响: 同一交付文件读两遍 + 全解码，虽在后台 daemon 线程（docstring 已声明），对大文件交付机器仍有可感知磁盘/CPU 占用；音频解码对"首尾帧可解码"结论非必需。
  - 建议: probe 仅 demux 视频 stream（`container.demux(video_stream)`）计数；或复用一次遍历同时算哈希（demux 字节流喂 sha256）。
  - 置信度: 高

### 通过项
- 异常路径：`collect_asset_records`/`_output_snapshot` 的 OSError 均落为如实状态（missing/hash_failed/hash_error）+ 警告，未伪造字段；`probe_output_media` 失败写 `probe_failed`，符合"宁缺毋滥"。
- 降级记录（`_collect_degradations`）：blockers/根因/preflight 降级/效果跳过/音频缺失均入 manifest，不只写日志。
- 线程安全：纯函数 + 显式说明后台线程写入，无共享可变状态。
- 输入验证：对 Mock/鸭子类型工程 `getattr` 全兜底，fps 除零有守卫（`if fps_den else 0.0`）。
- GPU/GLSL/信号维度：不涉及；类型注解完整（防御性 Any 为有意设计）。

---

## src/features/edit/core/generator_clip_transaction.py (行数: 171)

### 发现

- [AUD-16812] [Major] `_remove_asset_and_bin_entry` 逐字段重建 `EditProject` 漏传 `revision`，生成器资产 undo/GC 后工程 revision 归零
  - 位置: src/features/edit/core/generator_clip_transaction.py:153
  - 证据:
    ```python
    import time
    return EditProject(
        project_id=project.project_id,
        name=project.name,
        assets=new_assets,
        bins=new_bins,
        ...
        created_at=project.created_at,
        modified_at=time.time(),
    )
    ```
    而 `EditProject`（src/features/edit/core/project.py:1006）有 `revision: int = 0`，注释明确其为 TAEP P0 乐观锁、供 AI plan `base_revision` 一致性比较。重建字段列表缺 `revision`，新实例取默认 0。
  - 影响: 每次 undo 生成器 drop（timeline_view.py:5359）或 `reclaim_orphan_generators`（edit_project_io.py:790 加载时 GC）后，工程 revision 静默重置为 0——单调性被破坏，基于 revision 的乐观锁/并发一致性比较将误判。
  - 建议: 补 `revision=project.revision`；或整体改用 `dataclasses.replace(project, assets=..., bins=..., modified_at=...)` 根除逐字段漂移风险。
  - 置信度: 高

- [AUD-16813] [Suggestion] `undo_generator_clip_transaction(referenced_asset_ids=None)` 无条件删除资产，安全默认依赖调用方自觉
  - 位置: src/features/edit/core/generator_clip_transaction.py:116
  - 证据:
    ```python
        referenced_asset_ids: the set of asset ids still referenced by any
            clip on the active timeline(s). When provided, the asset is kept
            if it appears here. When None, the asset is removed unconditionally
            (the caller has already confirmed the clip is gone).
    ...
    if referenced_asset_ids is not None and asset_id in referenced_asset_ids:
        return project
    ```
  - 影响: 任何调用方漏传/传 None 时，若同一资产上还有第二个 clip（复制/再拖引用），该 clip 立即变悬空引用（正是 FX-H03 要消灭的孤儿问题的镜像）。当前调用点（timeline_view、guard GC）用法正确。
  - 建议: 将"扫描全部时间线计算引用"设为默认路径，删除无校验的 None 快捷方式；或至少让 None 分支记 warning。
  - 置信度: 高（事实），风险取决于未来调用方

- [AUD-16814] [Suggestion] 手工逐字段重建 + `"adjustment"/"solid"` 字面量与 guard 模块重复
  - 位置: src/features/edit/core/generator_clip_transaction.py:129
  - 证据:
    ```python
    if asset.media_type not in ("adjustment", "solid"):
        return project
    ```
    guard 模块另有 `_GENERATOR_MEDIA_TYPES = frozenset({"adjustment", "solid"})`（generator_transaction_guard.py:158）；字段重建的脆弱性已由 AUD-16812 实证。
  - 影响: 字面量两处维护，新增生成器类型时易漏改；逐字段重建是 AUD-16812 的结构性根源。
  - 建议: 提取共享常量（如 `GENERATOR_MEDIA_TYPES`）；重建改 `dataclasses.replace`。
  - 置信度: 高

### 通过项
- 状态提交一致性：事务为纯数据（返回新 project + clip，不触碰调用方状态），两阶段提交契约清晰；`build_...` 对未知 generator_type / 不存在 track 抛 ValueError，验证充分（资产先建后验 track 的顺序因纯数据而无泄漏）。
- 线程安全/GPU/信号：纯函数，不涉及。
- bin 清理：遍历所有 bin 移除引用，`with_asset_removed` 复用既有 API，正确。
- 类型注解完整。

---

## src/features/edit/core/generator_transaction_guard.py (行数: 363)

### 发现

- [AUD-16815] [Suggestion] 未使用的导入：`field`、`Dict`、`Optional`、`Tuple`
  - 位置: src/features/edit/core/generator_transaction_guard.py:34
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple
    ```
    全文件检索：`field(`、`Dict[`、`Optional[`、`Tuple[` 均无使用（`Any/FrozenSet/List/Sequence/Set` 有使用）。
  - 影响: lint 噪音、误导读者以为存在可变默认值构造。
  - 建议: 删除未用导入。
  - 置信度: 高

- [AUD-16816] [Suggestion] `reclaim_orphan_generators` 在扫描与移除间存在 TOCTOU（以空集跳过引用校验）
  - 位置: src/features/edit/core/generator_transaction_guard.py:271
  - 证据:
    ```python
    current = project
    reclaimed: Set[str] = set()
    for asset_id in check.orphaned_asset_ids:
        new_project = undo_generator_clip_transaction(
            current, asset_id, referenced_asset_ids=set(),
        )
    ```
  - 影响: `preflight_orphan_check` 的引用快照与逐个移除之间，若时间线被并发修改（未来多线程/AI 提交场景），可能误删新引用的生成器资产。当前单线程 UI 加载路径（edit_project_io.py:790）安全。
  - 建议: 循环内基于 `current` 重新收集引用，或在文档标注"必须持有提交锁调用"。
  - 置信度: 中

- [AUD-16817] [Suggestion] `GeneratorTransactionGuard` 只回滚 project 引用，不回滚 timeline 与已提交到服务层的状态，保护是约定式的
  - 位置: src/features/edit/core/generator_transaction_guard.py:92
  - 证据:
    ```python
    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is not None:
            # Rollback: restore original project state.
            self._current = self._original
            self._rolled_back = True
    ```
  - 影响: 若调用方在异常前已把 phase-1 project 推给 service/store，或 timeline 命令部分落地，guard 无法自动撤回——正确性完全依赖 docstring 里的用法约定（异常后调用方必须改用 `guard.project`）。docstring 已如实说明，属可接受的显式契约。
  - 建议: 在 docstring 增加"不得在 with 块内对外发布 project 引用"的硬性警告，或提供 `commit()`/`abort()` 强制两阶段 API。
  - 置信度: 高（契约事实）

### 通过项
- 孤儿检查/GC 一致性：`preflight_orphan_check` 只读、分类正确（orphan/missing/missing_bin/legacy），GC 只动 adjustment/solid 且未引用者、对 legacy 一律保留（保守策略正确）。
- 加载后校验（313-363 行）：missing_asset 标不可恢复、missing_bin_entry 标可恢复，分类合理；`recoverable` 语义清晰。
- 线程安全/GPU/信号：纯数据，不涉及；类型注解完整（除 AUD-16815 的未用导入）。
- 未修改输入对象：所有函数返回新实例，契约满足。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-16800, AUD-16804, AUD-16808, AUD-16812 |
| Suggestion | 14 | AUD-16801/02/03/05/06/07/09/10/11/13/14/15/16/17 |
