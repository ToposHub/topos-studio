# batch-270 审查报告 — src/features/teaching_animation/ai//

区间：AUD-27000–AUD-27099。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解。
本批 8 个文件均为 Qt-free 数据/服务层，无 GLSL 与 Qt 信号代码；重点落在状态一致性、undo 语义、输入验证与死代码。

---

## src/features/teaching_animation/ai/service.py (行数: 278)

### 发现

- [AUD-27018] [Critical] replace_clip 在命令栈外预删除原 clip，undo 后原 clip 永久丢失（数据丢失）
  - 位置: src/features/teaching_animation/ai/service.py:75
  - 证据:
    ```python
    clip = timeline.get_clip_by_id(clip_id)
    if clip is None:
        raise KeyError(f"Clip not found: {clip_id}")
    updated = _without_manifest(timeline.with_clip_removed(clip_id), (clip_id,))
    result = insert_motion_graphic_clip(
        updated, self.scene, ..., mode="overwrite", command_stack=command_stack)
    ```
    而 host_adapters.py 的 `MotionGraphicClipCommand.apply` 在 overwrite 模式下会把被覆盖 clip 记入 `self._removed_clips`，`undo` 只恢复这些记录的 clip（host_adapters.py:216-221）。服务在命令执行前已手动 `with_clip_removed`，原 clip 不在 `_removed_clips` 中。
  - 影响: 用户执行"替换 clip"后撤销：插入的新 clip 被移除，但被替换的原 clip 不会被恢复——时间线上该位置永久丢失 clip（及其 manifest 引用），属于数据丢失。
  - 建议: 删去手动 `with_clip_removed`/`_without_manifest`，直接以 `mode="overwrite"` 插入（命令自身会记录并恢复被覆盖 clip）；或将删除也包装为命令经 `command_stack.execute` 执行。
  - 置信度: 高

- [AUD-27019] [Major] apply_html_layer/apply_html_template 直接 apply 后丢弃 SetHtmlLayerCommand，撤销状态丢失，与"undoable and recorded"的文档承诺不符
  - 位置: src/features/teaching_animation/ai/service.py:192
  - 证据:
    ```python
    updated = SetHtmlLayerCommand(layer).apply(self.scene)
    self.update_scene(updated)
    return layer
    ```
    `SetHtmlLayerCommand.apply` 把 `_previous_layer/_previous_pages/_previous_elements` 记录在命令实例上供 undo（html_layer_commands.py:61-74），但该实例在函数返回后即被丢弃；两方法均无 `command_stack` 参数（对比 `insert_into_timeline`/`replace_clip` 有）。docstring 写着 "SetHtmlLayerCommand makes the write reversible and recorded"（service.py:154-156、257-259）。
  - 影响: HTML 图层写入无法通过宿主 undo 系统撤销；docstring 误导调用方以为可撤销，实际前一图层状态随命令实例一起丢失。
  - 建议: 增加 `command_stack` 参数并走 `command_stack.execute(SetHtmlLayerCommand(layer), scene)`；无栈时至少在 docstring 中明确"此路径不记录撤销"。
  - 置信度: 高

- [AUD-27020] [Suggestion] render_preview 中 renderer.prepare 位于 try/finally 之外，prepare 失败不清理
  - 位置: src/features/teaching_animation/ai/service.py:53
  - 证据:
    ```python
    renderer = select_scene_renderer(self.scene)
    renderer.prepare(self.scene, context)
    try:
        return renderer.render_frame(AnimationFrame(frame, context.fps))
    finally:
        renderer.cleanup()
    ```
  - 影响: SVG 渲染器的 prepare 主要是校验、风险低；但 HTML 适配器的 prepare 可能启动/连接后端，若在部分初始化后抛出（如 HtmlRenderError），cleanup 被跳过，可能遗留进程/句柄资源。（注：scene_renderer_router.py:366-372 的示例文档也是同样写法，属项目惯用模式，故降为 Suggestion。）
  - 建议: 将 `prepare` 移入 try 块内（prepare 前置校验失败时 cleanup 空操作无害），或在 prepare 异常路径补一次 cleanup。
  - 置信度: 中

### 通过项
- confirmed 门禁：insert_into_timeline/replace_clip 未确认时抛 PermissionError，符合"显式确认"约束
- apply_html_layer 的 writable 门禁：非 writable outcome 抛 ValueError 且附 blocking_reasons
- apply_html_template 输入验证：KeyError（未知模板）+ validate_params 错误合并为 ValueError
- 无除零风险：TeachingSceneDocument.__post_init__ 强制 fps_num/fps_den/duration_frames 为正（model.py:421-424），apply_html_layer 的 `duration_frames / self.scene.fps` 安全；detect_page_count 返回值 max(1,…)（html_page_deck.py:387）
- 类型注解完整；跨模块签名（insert_motion_graphic_clip、build_html_layer_from_pipeline、promote_html_layer、generate_html_with_llm、HtmlTemplateRegistry.get/register/list_templates）逐一核对一致

---

## src/features/teaching_animation/ai/revision.py (行数: 520)

### 发现

- [AUD-27010] [Major] accept() 仅拦截 REJECTED，终态 SUPERSEDED/FAILED 版本可被接受，违反自述不变量
  - 位置: src/features/teaching_animation/ai/revision.py:386
  - 证据:
    ```python
    if revision.status == STATUS_REJECTED:
        raise MotionGraphicFailure(
            MOTION_GRAPHIC_REVISION_CONFLICT,
            "已拒绝的版本不能再接受。",
            details={"revision_id": revision_id},
        )
    ```
    类 docstring 声明 "terminal revisions cannot transition back"（revision.py:228），`TERMINAL_STATUSES = {REJECTED, SUPERSEDED, FAILED}`（revision.py:144），但 accept 只挡 REJECTED。
  - 影响: 生成失败（FAILED）或已被新版本取代（SUPERSEDED）的 revision 可被 accept 成为当前采纳版本——用户可能确认一个已知失败的生成结果，破坏版本图的终态语义。
  - 建议: accept 的守卫改为 `if revision.status in TERMINAL_STATUSES: raise ...`（对 SUPERSEDED 可视产品语义选择是否允许"回退接受"，但 FAILED 必须拦截）。
  - 置信度: 高

- [AUD-27011] [Suggestion] attach_plan 不校验 plan 与 revision 的一致性（proposal_ref/brief_hash）
  - 位置: src/features/teaching_animation/ai/revision.py:348
  - 证据:
    ```python
    def attach_plan(self, revision_id: str, plan: MotionGraphicPlan) -> MotionGraphicRevision:
        revision = self._revisions.get(revision_id)
        ...
        if revision.status not in ACTIVE_STATUSES:
            raise ...
        updated = MotionGraphicRevision(..., plan=plan, ...)
    ```
    未检查 `plan.proposal_ref == revision.proposal.proposal_id` 或 brief_hash 一致（对比 add_feedback 有 brief 一致性守卫，revision.py:319-330）。
  - 影响: 编译自另一 proposal 的 plan 可挂到本 revision 上；审批作用域绑定 plan_hash 能兜底，但版本图内部记录会自相矛盾（revision 的 proposal 与 plan 来源不同）。
  - 建议: attach 时校验 `plan.proposal_ref == revision.proposal.proposal_id` 与 brief_hash 相等，不一致抛 MOTION_GRAPHIC_REVISION_CONFLICT。
  - 置信度: 中

- [AUD-27012] [Suggestion] MotionGraphicRevisionLog 无锁，add_feedback 的 latest 检查为 check-then-act
  - 位置: src/features/teaching_animation/ai/revision.py:306
  - 证据:
    ```python
    latest_id = self._lineage_latest.get(parent.lineage_id)
    if latest_id != parent_id:
        raise MotionGraphicFailure(...)
    ```
    `_revisions`/`_lineage_latest` 为普通 dict（revision.py:232-233），无 threading 锁。
  - 影响: 若生成回调在 worker 线程完成、UI 线程同时发起反馈，两个 add_feedback 可同时通过 latest 检查，产生隐式分叉（违反"只能基于最新版本修改"不变量）。当前休眠模块（文件头 DORMANT 标记）多为 UI 线程单线程使用，故降为 Suggestion。
  - 建议: 接线前为变更方法加一把 `threading.RLock`，或将 latest 检查与 `_store` 放入同一原子段。
  - 置信度: 中

- [AUD-27013] [Suggestion] mark_failed 无状态守卫，可将 ACCEPTED 版本静默翻转为 FAILED
  - 位置: src/features/teaching_animation/ai/revision.py:438
  - 证据:
    ```python
    def mark_failed(self, revision_id: str, *, reason: str = "") -> MotionGraphicRevision:
        revision = self._revisions.get(revision_id)
        if revision is None:
            raise ...
        updated = MotionGraphicRevision(..., status=STATUS_FAILED, ...)
    ```
    对比 accept/reject 都有状态守卫；mark_failed 对任何状态（含 ACCEPTED）直接覆盖。
  - 影响: 异步生成迟到回调可把用户已接受的版本标为 FAILED，且不产生任何冲突错误。
  - 建议: 对 ACCEPTED 状态拒绝 mark_failed（或要求显式 force 参数）。
  - 置信度: 中

### 通过项
- 状态机其余不变量：add_feedback 的"必须基于最新版本"守卫与 brief 一致性守卫正确；accept 自动取代旧 accepted 版本
- lineage() 带 seen 环保护，父链遍历不会死循环
- revision 对象不可变（frozen），状态变更是替换而非原地修改，符合 append-only 精神
- FeedbackPatch.__post_init__ 校验未知 category，from_dict 复用同一校验路径
- 所有失败均抛类型化 MotionGraphicFailure(MOTION_GRAPHIC_REVISION_CONFLICT)，无宽泛 except
- 类型注解完整；文件头 DORMANT 标记清晰

---

## src/features/teaching_animation/ai/quality_gate.py (行数: 320)

### 发现

- [AUD-27006] [Major] has_transparency 输入被静默忽略，承诺的透明度检查从未实现
  - 位置: src/features/teaching_animation/ai/quality_gate.py:128
  - 证据:
    ```python
    @dataclass(frozen=True)
    class RenderFrameResult:
        label: str
        frame_index: int
        succeeded: bool
        is_blank: bool = False
        has_transparency: bool = False
    ```
    模块 docstring 声明 gate 运行 "transparency, text overflow, safe area, flicker, lens blank…" 检查（quality_gate.py:5-8），`CHECK_TRANSPARENCY = "transparency"` 也在 ALL_CHECKS 中（quality_gate.py:41,52-58），但 `run()`（164-287 行）通篇未引用 `has_transparency`，也未产出任何 transparency finding。
  - 影响: 宿主按接口传入 `has_transparency=True`（如透明底渲染异常）时，门禁既不报错也不计入 checks_run，"可交付"结论可能覆盖透明度缺陷——功能缺口而非崩溃，定 Major。
  - 建议: 在帧检查循环中实现 transparency 判定（例如 has_transparency 与场景是否应透明不一致时出 finding），或在未实现前从 RenderFrameResult/ALL_CHECKS 中移除该字段并在 checks_skipped 中登记。
  - 置信度: 高

- [AUD-27007] [Suggestion] checks_skipped 永远为空，未实现/未触发的检查无法与"通过"区分
  - 位置: src/features/teaching_animation/ai/quality_gate.py:286
  - 证据:
    ```python
    checks_skipped: list[str] = []
    ...
    return MotionGraphicQualityReport(
        findings=tuple(findings),
        checks_run=tuple(dict.fromkeys(checks_run)),
        checks_skipped=tuple(checks_skipped),
    )
    ```
    全文除初始化与返回外无任何 append。
  - 影响: 报告消费者（卡片/审计日志）无法区分"某检查通过"与"某检查根本没跑"（如未提供音频数据时 audio_duration 静默缺席）。
  - 建议: 在每个条件分支的 else 路径登记 skip 原因（缺少输入/后端不可用）。
  - 置信度: 高

- [AUD-27008] [Suggestion] 非渲染类失败全部复用 MOTION_GRAPHIC_RENDER_FAILED 错误码
  - 位置: src/features/teaching_animation/ai/quality_gate.py:226
  - 证据:
    ```python
    findings.append(QualityFinding(
        check=CHECK_AUDIO_DURATION,
        severity=severity,
        code=MOTION_GRAPHIC_RENDER_FAILED,
        message=f"音频时长 {inputs.audio_duration_seconds:.2f}s 与片段 ... 不一致。",
    ```
    同样模式见 loudness（239）、flicker（250）、final encoding（262）。
  - 影响: 音频时长不匹配、响度偏离、闪烁、编码失败在错误码层面与"渲染失败"不可区分，`suggested_next_step` 等按 code 路由的恢复建议会给出错误指引，统计聚合也失真。
  - 建议: 为 audio/loudness/flicker/encoding 各定义独立错误码（error_codes.py 已有先例），或在 code 中拼接 check 名。
  - 置信度: 高

- [AUD-27009] [Suggestion] 三个错误码导入未使用（CANVAS_OUT_OF_BOUNDS/LOW_CONTRAST/TEXT_OVERFLOW）
  - 位置: src/features/teaching_animation/ai/quality_gate.py:24
  - 证据:
    ```python
    from .error_codes import (
        MOTION_GRAPHIC_ASSET_LICENSE_MISSING,
        MOTION_GRAPHIC_ASSET_UNRESOLVED,
        MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS,
        MOTION_GRAPHIC_LOW_CONTRAST,
        MOTION_GRAPHIC_RENDER_FAILED,
        MOTION_GRAPHIC_TEXT_OVERFLOW,
    )
    ```
    正文仅使用 ASSET_LICENSE_MISSING / ASSET_UNRESOLVED / RENDER_FAILED（画布越界、低对比、文字溢出由 design_validator 的 finding.code 透传，273-281 行）。
  - 影响: 死导入，误导读者以为本模块直接产出这三类 finding。
  - 建议: 删除未使用的三个导入。
  - 置信度: 高

### 通过项
- 线程安全：gate 无实例状态，run() 纯函数
- 设计验证器聚合正确：finding 的 severity/code/element_id/path/suggestion 字段与 DesignFinding 定义一致（design_validator.py:114-128）
- is_deliverable 仅由 error 级 finding 决定，severity 语义清晰
- checks_run 用 dict.fromkeys 去重，重复帧标签不会重复计数
- 类型注解完整

---

## src/features/teaching_animation/ai/preview.py (行数: 436)

### 发现

- [AUD-27000] [Suggestion] sample_storyboard_frames 计算 fps_value 后从未使用，fps 参数为死参数
  - 位置: src/features/teaching_animation/ai/preview.py:276
  - 证据:
    ```python
    fps_value = float(fps or scene.fps)

    frames: set[int] = {0, duration - 1}
    ```
    函数其余部分（278-292 行）无任何对 fps_value 的引用。
  - 影响: 误导性 API：调用方以为采样与 fps 相关并传参，实际返回值只依赖 (duration_frames, count)；`fps or scene.fps` 还隐含把 0 视为未传。
  - 建议: 删除 fps_value 与 fps 参数（保持"纯函数 (duration_frames, count)"的文档承诺），或真正用于时间戳换算。
  - 置信度: 高

- [AUD-27001] [Suggestion] SceneDiffSummary 文档称 "hashable"，实际 hash() 会抛 TypeError
  - 位置: src/features/teaching_animation/ai/preview.py:136
  - 证据:
    ```python
    * :class:`SceneDiffSummary` — a compact, hashable description of the
      :class:`~...SceneDiff` ...
    ...
    timeline_insertion: Mapping[str, Any] = field(default_factory=dict)
    ```
    frozen dataclass 会生成 __hash__，但字段含 dict/Mapping，`hash(summary)` 运行时抛 `TypeError: unhashable type: 'dict'`。
  - 影响: 按文档把 SceneDiffSummary（或同样含 Mapping 的 MotionGraphicPreview/MotionGraphicPlan）用作 dict 键或放入 set 会崩溃；当前代码路径（compute_preview_hash 走 to_dict）未触发。
  - 建议: 将 Mapping 字段转为 tuple-of-pairs 不可变结构，或修正文档去掉 "hashable" 表述。
  - 置信度: 高

### 通过项
- 哈希绑定完整：preview_hash 覆盖 plan_hash/scene_hash/render_profile/storyboard_indices/diff_summary，canonical JSON（sort_keys + separators）稳定
- 采样确定性：去重后按序排序、短场景按最后帧确定性补齐，满足"同一 plan 同一 storyboard"
- 输入验证：MotionGraphicPreview.__post_init__ 四个必填非空校验；is_failed 对空 storyboard/全错帧判定正确
- 线程安全：纯数据类，无共享可变状态（default_factory 每实例新 dict）
- 类型注解完整

---

## src/features/teaching_animation/ai/proposal.py (行数: 548)

### 发现

- [AUD-27002] [Suggestion] NarrativeBeat.from_dict 的 asset_refs 未做 str 强转，违反 tuple[str, ...] 契约
  - 位置: src/features/teaching_animation/ai/proposal.py:147
  - 证据:
    ```python
    raw_refs = data.get("asset_refs", ())
    return cls(
        ...
        asset_refs=tuple(raw_refs) if isinstance(raw_refs, (list, tuple)) else (),
    )
    ```
    对比同文件 CreativeProposal.from_dict 对 estimated_risks 做了 `tuple(str(r) for r in ...)`（proposal.py:314）。
  - 影响: 外部（LLM）数据里 asset_refs 混入 int/None 时原样保留，类型注解失效；后续 to_dict/JSON 序列化虽不崩溃，但依赖 asset id 的查找（素材解析、质量门禁）会拿非字符串 id 匹配失败。
  - 建议: 与 estimated_risks 一致地 `tuple(str(r) for r in raw_refs)`。
  - 置信度: 高

### 通过项
- 状态一致性：plan_hash 绑定 commands+scene_hash+diff_hash+brief 三元组+target 定位；build_plan 显式排除 plan_id 后哈希并注明
- 异常路径：build_plan 的 `except Exception` 带 noqa 且 `raise ... from exc` 转为类型化 MotionGraphicFailure(MOTION_GRAPHIC_INVALID_SCENE_PLAN)，空 diff 也稳定报错，符合"不掩盖领域错误"约束
- 输入验证：CreativeProposal/MotionGraphicPlan 的 __post_init__ 必填/非负校验完整；from_dict 对非 Mapping 项过滤
- provenance 设计：FALLBACK_SOURCES 封闭集合 + is_fallback_source，回退不可伪装为 SOURCE_LLM
- _copy_json 深拷贝防止调用方字典与冻结对象共享引用
- 类型注解完整

---

## src/features/teaching_animation/ai/proposal_card.py (行数: 364)

### 发现

- [AUD-27003] [Suggestion] 模块级未使用导入：json 与四个 FALLBACK_* 常量
  - 位置: src/features/teaching_animation/ai/proposal_card.py:28
  - 证据:
    ```python
    from dataclasses import dataclass, field
    import json
    ...
    from .fallback import (
        ACTION_CANCEL, ACTION_CHOOSE_TEMPLATE, ACTION_EDIT_BRIEF, ACTION_RETRY,
        FALLBACK_INVALID_SCENE_PLAN, FALLBACK_NONE,
        FALLBACK_OPTIONAL_TEMPLATE, FALLBACK_PROVIDER_UNAVAILABLE,
        MotionGraphicFallbackDecision,
    )
    ```
    全文无 `json.` 引用；FALLBACK_* 四常量未被引用（fallback 状态映射走函数内延迟导入的 `source_for_fallback`）。另 `field` 也未使用（所有字段均有默认值字面量）。
  - 影响: 死导入增加噪音；ACTION_* 被使用属正常，FALLBACK_* 易误导读者以为有本地分支。
  - 建议: 删除 json、field 与 FALLBACK_* 导入。
  - 置信度: 高

- [AUD-27004] [Suggestion] _canvas_aspect_from_scene 对不匹配比例一律返回默认值，且竖屏分支存在死代码
  - 位置: src/features/teaching_animation/ai/proposal_card.py:351
  - 证据:
    ```python
    if width > height:
        ratio = width / height
        if abs(ratio - 16 / 9) < 0.05:
            return "16:9"
        if abs(ratio - 4 / 3) < 0.05:
            return "4:3"
        if abs(ratio - 21 / 9) < 0.1:
            return "21:9"
        return "16:9"          # 5:4、2:1 等被误标为 16:9
    ratio = height / width
    if abs(ratio - 16 / 9) < 0.05:
        return "9:16"
    return "9:16"              # 与上一分支重复，死代码
    ```
  - 影响: 卡片"格式行"对非标准比例（如 5:4/2:1/4:5）显示错误的画幅，用户确认前看到与实际不符的格式信息。
  - 建议: 不匹配时返回 f"{width}:{height}"（或约分后的最简整数比）；删除竖屏重复 return。
  - 置信度: 高

### 通过项
- 回退溯源：is_fallback 基于 source_label 判定，build_fallback_card 的 source_label 一律来自 source_for_fallback(decision.state)，回退不可能显示为 SOURCE_LLM
- build_fallback_card 对 is_success 的防御分支存在且行为合理
- 输入验证：quality_report 提取 errors/warnings 时统一 str() 化
- MotionGraphicFallbackDecision 接口（is_success/is_failure/state/error_code/message/next_actions/details）与 fallback.py 定义逐一核对一致
- 类型注解完整（_canvas_aspect_from_scene 用 Any 属可接受的鸭子类型）

---

## src/features/teaching_animation/ai/provider_registry.py (行数: 37)

### 发现

- [AUD-27005] [Suggestion] 描述符默认 command_registry 为类级共享实例，未来加状态即成跨实例串改
  - 位置: src/features/teaching_animation/ai/provider_registry.py:16
  - 证据:
    ```python
    @dataclass(frozen=True)
    class TeachingAIProviderDescriptor:
        provider_id: str = "teaching_animation"
        capabilities: tuple[str, ...] = ("scene_context", "scene_commands", "diff_apply_undo")
        command_registry: TeachingAICommandRegistry = TeachingAICommandRegistry()
    ```
    dataclass 允许自定义类作默认值（不像 list/dict 会报错），该实例被所有未显式传参的描述符共享。当前 TeachingAICommandRegistry 无实例状态（commands.py:61-133 全为无状态方法），暂无实际危害。
  - 影响: 一旦 registry 将来持有状态（缓存/统计），所有 provider 描述符会静默共享同一实例，产生难以追踪的串改。
  - 建议: 改为 `field(default_factory=TeachingAICommandRegistry)`。
  - 置信度: 高（机制确定；当前无实际影响故仅 Suggestion）

### 通过项
- 线程安全：register/unregister 为简单字典操作，register_teaching_ai_provider 幂等覆盖，预期 UI/启动线程单线程调用
- 通过（无渲染逻辑）：纯注册表；_PROVIDERS 未列入 __all__ 导出，register/unregister/get 为全部公开面
- 类型注解完整

---

## src/features/teaching_animation/ai/rollout.py (行数: 345)

### 发现

- [AUD-27014] [Major] kill switch 通过 object.__setattr__ 原地篡改冻结的 RolloutPolicy，共享策略对象时波及所有控制器
  - 位置: src/features/teaching_animation/ai/rollout.py:217
  - 证据:
    ```python
    def activate_kill_switch(self, *, reason: str = "") -> None:
        object.__setattr__(self._policy, "kill_switch_active", True)
        # Record as an incident so the runbook has a trail.
        self.record_incident("kill_switch", "error", details={"reason": reason})

    def deactivate_kill_switch(self) -> None:
        object.__setattr__(self._policy, "kill_switch_active", False)
    ```
    RolloutPolicy 为 frozen dataclass；多个 RolloutController 可被构造时传入同一 policy 实例（全局策略是自然用法）。deactivate 无审计记录。
  - 影响: (1) 冻结值对象的不可变契约被绕过，任何持有/快照了该 policy 的代码看到的开关状态被静默改变；(2) 共享 policy 时对一个控制器拉闸会连带关闭无关控制器（若这正是"全局熔断"意图，也应显式建模而非靠破坏冻结实现）；(3) 解除熔断不留 incident 痕迹，与 runbook 的审计要求相悖。
  - 建议: kill switch 独立为 controller 上的普通可变状态（`self._kill_switch_active`，加锁读写），policy 保持不可变；deactivate 也记录 incident。
  - 置信度: 高（行为确定；跨控制器影响取决于 policy 是否共享，标中影响）

- [AUD-27015] [Suggestion] cohort_allows 对未知 cohort 抛未类型化的 ValueError，权限判定路径可被外部数据打崩
  - 位置: src/features/teaching_animation/ai/rollout.py:169
  - 证据:
    ```python
    def cohort_allows(self, capability: str, cohort: str) -> bool:
        min_cohort = {...}.get(capability, COHORT_GA)
        if self.kill_switch_active:
            return False
        return COHORT_ORDER.index(cohort) >= COHORT_ORDER.index(min_cohort)
    ```
    cohort 来自可插拔 FlagSource.cohort_for（协议文档只约定返回 str）。
  - 影响: flag service 返回新 cohort 名（如 "prod"）时 may_use_autonomous_mode 直接抛 ValueError，而非按最小权限拒绝——权限检查应 fail-closed 而非崩溃。
  - 建议: 未知 cohort 视为最低权限（return False）或先校验 `cohort in COHORT_ORDER` 再比较。
  - 置信度: 中

- [AUD-27016] [Suggestion] incidents/feedback 列表无上限增长，data_retention_days 从未被强制执行
  - 位置: src/features/teaching_animation/ai/rollout.py:197
  - 证据:
    ```python
    self._incidents: list[IncidentReport] = []
    self._feedback: list[UserFeedback] = []
    ...
    with self._lock:
        self._incidents.append(incident)
    ```
    policy 上声明 `data_retention_days: int = 90`（rollout.py:157），但模块内无任何裁剪逻辑；acceptance_rate/satisfaction_average 每次全量遍历。
  - 影响: 长会话/高频遥测下内存无界增长，平均值随历史无限稀释（新事件难以触发 runbook 阈值）。
  - 建议: 按时间窗或最大条数裁剪（ring buffer），retention 落地到 record_* 或定期清理。
  - 置信度: 高

- [AUD-27017] [Suggestion] runbook 触发条件写"最近 20 次"，satisfaction_average 却是全量平均
  - 位置: src/features/teaching_animation/ai/rollout.py:307
  - 证据:
    ```python
    - satisfaction_average() < 0.6 over the last 20 runs
    ```
    而 `satisfaction_average`（287-291 行）对 `self._feedback` 全量求均值，无窗口。
  - 影响: on-call 按手册检查的指标与实际实现的语义不一致，熔断触发时机偏离设计。
  - 建议: 增加 `window: int = 20` 参数取尾部样本，或修正 runbook 措辞。
  - 置信度: 高

### 通过项
- 遥测采集（record_incident/record_feedback/incidents/feedback/统计）均有 RLock 保护
- satisfaction 输入钳位 0..1（max/min），防外部脏值
- DictFlagSource 默认 False（fail-closed），may_use_* 先查 kill switch 再查 flag 再查 cohort，顺序合理
- 类型注解完整；time_fn 可注入便于测试

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-27018 |
| Major | 4 | AUD-27006, AUD-27010, AUD-27014, AUD-27019 |
| Suggestion | 16 | AUD-27000/01/02/03/04/05/07/08/09/11/12/13/15/16/17/20 |

（注：AUD 编号按文件顺序分配——preview 27000-27001、proposal 27002、proposal_card 27003-27004、provider_registry 27005、quality_gate 27006-27009、revision 27010-27013、rollout 27014-27017、service 27018-27020；上表 27020 即 service 的 render_preview 项，共 21 条发现：Critical 1 / Major 4 / Suggestion 16。）

通用说明：本批文件均为 Qt-free 层，无 Qt 信号递归、无 OpenGL/GLSL 代码，这两类铁律不适用；线程安全维度各文件结论已分别在上文"通过项/发现"中给出。
