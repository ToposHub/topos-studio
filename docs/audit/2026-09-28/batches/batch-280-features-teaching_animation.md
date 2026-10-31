# batch-280 审查报告 — src/features/teaching_animation/module.py

审查范围：`src/features/teaching_animation/module.py`（5,895 行，全文件通读）。
九维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能模式 / 类型注解。
问题编号区间：AUD-28000–AUD-28099。

## src/features/teaching_animation/module.py (行数: 5895)

### 发现

- [AUD-28001] [Critical] 缓存的 TeachingAnimationAIService 持有过期场景快照，HTML 确认写入会用旧场景覆盖共享 store 的新版本（静默丢失用户编辑）
  - 位置: src/features/teaching_animation/module.py:812-824, 2190, 2247-2288
  - 证据:
    ```python
    def _get_html_service_for(self, scene_id: str) -> Any | None:
        scene = get_shared_scene_store().get(str(scene_id))
        if scene is None:
            return None
        service = self._html_services.get(scene.scene_id)
        if service is None:
            service = TeachingAnimationAIService(scene)
            self._html_services[scene.scene_id] = service
        return service
    ```
    ```python
    # _confirm_html_generate_request 内：
    service = self._get_html_service_for(current.scene_id)   # 可能命中持有旧场景的缓存
    ...
    layer = service.apply_html_layer(...)                     # service.py:192: SetHtmlLayerCommand(layer).apply(self.scene)
    updated = service.scene                                   # 基于旧快照派生
    ...
    store.register(updated)                                   # 无条件覆盖 store 中更新的版本
    ```
    交叉验证：ai/service.py:26-38（scene 由构造时捕获的 context_provider._scene 提供，仅 update_scene 会替换）、ai/service.py:192-193（apply 基于自身快照）、ai/context_provider.py:92-103（_scene 为捕获对象，不回读 store）、scene_store.py:47-54（register 按对象替换并递增 revision）。
  - 影响: 服务按 scene_id 永久缓存。第一轮 AI HTML 编辑后，用户在编辑器中保存的场景修改（store.register 新对象，scene_store.py:47-54）或场景命令编辑（_confirm_scene_edit_request 的 store.register）不会同步进缓存服务。下一轮 AI 编辑确认时，line 2181 的 revision 检查只比较 store 与 plan 快照（两者一致、检查通过），但 service.apply_html_layer 仍把新 HTML 层应用到旧场景快照上，随后 store.register(updated) 用"旧场景 + 新 HTML 层"覆盖 store——用户在两次 AI 编辑之间所做的元素/时长/页面等修改被静默回滚（数据丢失）。编辑器窗口打开且 editor_controller.scene is current 时走编辑器命令路径可避免，但聊天驱动的纯时间线编辑（主流程）正是不开编辑器的场景。
  - 建议: _get_html_service_for 命中缓存时先校验 service.scene 与 store 当前对象是否同一（或 revision 一致），不一致则调用 service.update_scene(store.get(scene_id)) 刷新；或在每次 confirm 前无条件以 store 场景重建/刷新服务。
  - 置信度: 高

- [AUD-28002] [Major] 场景编辑确认缺少 revision/陈旧校验，提案会应用到已变化的场景上
  - 位置: src/features/teaching_animation/module.py:3326-3358, 2504-2512
  - 证据:
    ```python
    # _confirm_scene_edit_request（确认时）：
    current = store.get(plan["scene_id"])
    if current is None:
        raise RuntimeError("目标场景已不存在，请重新打开 Motion Graphic Clip")
    ...
    updated = registry.apply(proposal, controller, confirmed=True)
    ```
    ```python
    # _handle_model_scene_edit_response（解析时即与旧元素合并）：
    if element is not None and isinstance(updates.get("style"), dict):
        merged_style = dict(element.style)
        merged_style.update(updates["style"])
        updates["style"] = merged_style
    ```
    对比 HTML 路径有显式陈旧检查（module.py:2180-2189 `store.revision(current.scene_id) != plan["scene_revision"]` 则拒绝）。
  - 影响: 卡片展示期间用户若在编辑器里改了场景（颜色、字号等），确认时命令仍按解析时与旧元素合并出的完整 style 字典执行 update_element（命令契约为整表替换），会把用户更新的属性静默回滚为旧值；仅当元素被删除等硬错误才会抛错被捕获。与 HTML 路径的 MG-12/revision 防护不对称。
  - 建议: 在计划创建时记录 store.revision(scene_id)，确认时像 _confirm_html_generate_request 一样比较并拒绝陈旧提案。
  - 置信度: 中

- [AUD-28003] [Major] 文本"确认"对 html_generate 类待处理请求路由到错误处理器：预览卡被清除且确认不生效
  - 位置: src/features/teaching_animation/module.py:5461-5468, 3397-3405
  - 证据:
    ```python
    if pending and self._is_confirmation_text(text):
        self._append_user_message(main_window, text)
        request_id, kind = pending[-1]
        return (
            self._confirm_scene_edit_request(main_window, request_id)
            if kind == "scene_edit"
            else self._confirm_motion_graphic_request(main_window, request_id)
        )
    ```
    ```python
    # _confirm_motion_graphic_request（html_generate 的 id 不在 motion 字典中）：
    plan = self._pending_motion_graphic_requests.pop(str(request_id), None)
    clear = getattr(panel, "clear_motion_graphic_proposal", None)
    if callable(clear):
        clear(str(request_id))          # 卡片被清除
    if plan is None:
        return False                    # 确认未应用
    ```
  - 影响: `_trigger_html_generate` 展示的"HTML Motion 动效预览"卡（module.py:1495-1499）尚在待确认时，用户输入"确认/可以"会被误派发给 `_confirm_motion_graphic_request`：请求 id 前缀不同（html-generate- vs motion-graphic-）查不到 plan，但卡片仍被清除，随后返回 False 使文本落入普通 LLM 聊天。用户既丢了确认卡，确认也未写入场景，只能重新生成。按钮路径（_confirm_motion_graphic_plan, module.py:3241）分发正确，仅文本路径损坏。
  - 建议: 文本确认分发处增加 `kind == "html_generate"` 分支调用 `_confirm_html_generate_request`；并避免在 plan 为 None 时清除卡片。
  - 置信度: 高

- [AUD-28004] [Major] 构建失败后的 plan 未清理，后续全新创作请求被吞掉或被误当作失败文档的修订
  - 位置: src/features/teaching_animation/module.py:5415-5459, 4192-4236
  - 证据:
    ```python
    if plan is not None and plan.get("phase") == "build":
        if (plan.get("requires_button_confirmation") and self._is_confirmation_text(text)):
            ...  # 提示点击按钮
        if not self._is_confirmation_text(text) and not self._is_cancellation_text(text):
            ...
            if plan.get("render_mode") == "html" and plan.get("raw_html"):
                plan["html_edit_context"] = str(plan.get("raw_html", ""))  # 失败文档被当作修订基础
                ...
            self._append_assistant_message(
                main_window,
                "已保留为草稿的场景没有执行。请先完成网页设计对话，"
                "完成后会重新显示确认执行按钮。",
            )
            return True
    ```
    失败卡片文案（module.py:4224-4231）却引导用户"重新描述需求后再次发起"。
  - 影响: `_mark_motion_graphic_build_failed` 只标记 build_failed/显示仅含 cancel 的失败卡，不弹出 plan、不重置 phase。此后用户按失败卡提示发送全新需求（非"取消"、非确认词）：若失败发生在任何成功暂存之前（raw_html 为空），请求被"已保留为草稿的场景没有执行"吞掉且 return True，什么也不发生；若 raw_html 残留上次失败输出（_retry_motion_graphic_html_build 只重置 scene 不清 raw_html，module.py:5553-5555），新需求被误当作对失败文档的修订。用户必须先手动点取消才能发起新任务，与失败卡指引矛盾。
  - 建议: build_failed 的 plan 在 _handle_ai_request 顶部即视为可被新创作请求取代（弹出并继续路由），或失败时将 phase 重置/清理 raw_html；至少让"重新描述再次发起"路径真正创建新 plan。
  - 置信度: 高

- [AUD-28005] [Major] 确认路径与同步兜底在 UI 线程执行全尺寸真实浏览器验证，阻塞主线程
  - 位置: src/features/teaching_animation/module.py:2233-2246, 1921-1948, 3592-3607; 交叉 render/html_validation.py:152-164
  - 证据:
    ```python
    # _confirm_html_generate_request（用户点击确认/自动应用时，主线程）：
    if _os.environ.get("TOPOS_TEST_MODE") != "1":
        try:
            self._validate_motion_graphic_scene(candidate_scene)
        except Exception as exc:  # noqa: BLE001 - preserve the render diagnosis
    ```
    ```python
    # _validate_motion_graphic_scene 传入 64x36，但被 html_validation.py 按层视口替换：
    width = int(getattr(layer, "viewport_width", 0) or ...)
    render_context = replace(context, width=width, height=height)   # 1920x1080
    ```
  - 影响: 生成阶段已专门引入后台 QThread postprocess（module.py:1689-1717）避免浏览器验证卡 UI，但确认写入前的复验（validate_html_scene 在 1920×1080 视口上多候选帧 + 每页多探针 + 透明/对比背景两轮捕获）仍在主线程同步执行；controller 无 start_motion_graphic_postprocess 时的同步兜底（module.py:1921-1948）与场景模式的 5 帧故事板渲染（module.py:4658-4714）同样同步。主线程可冻结数秒，违背 4K 实时交互基准。
  - 建议: 确认路径复用 start_motion_graphic_postprocess 后台通道（或仅以小视口+页级探针复验），故事板渲染移入 worker。
  - 置信度: 中（阻塞时长取决于浏览器后端；代码路径确定）

- [AUD-28006] [Suggestion] `_ensure_feedback_store` 为死代码且缓存逻辑失效、条件表达式混乱
  - 位置: src/features/teaching_animation/module.py:5014-5023
  - 证据:
    ```python
    @classmethod
    def _ensure_feedback_store(cls) -> Any:
        """Lazily create the FeedbackStore (avoids import at module load)."""
        if cls._feedback_store is not None if hasattr(cls, "_feedback_store") else False:
            return cls._feedback_store
        try:
            from src.features.edit.ai_editing.feedback_store import FeedbackStore
            return FeedbackStore()
        except ImportError:
            return None
    ```
  - 影响: 全文件无调用点（_record_user_feedback 直接用 self._feedback_store，module.py:5064-5065）。且 `_feedback_store` 是实例属性（module.py:82），classmethod 经 `cls` 永远看不到（hasattr 恒 False），新建的 FeedbackStore 也不回存——即使将来被调用也无法缓存；`if A if B else C` 嵌套条件可读性差。
  - 建议: 删除该方法，或改为实例方法并在创建后写回 self._feedback_store。
  - 置信度: 高

- [AUD-28007] [Suggestion] `_html_services` 与待处理请求字典无淘汰，长会话内存持续增长
  - 位置: src/features/teaching_animation/module.py:64-67, 812-824, 4969-5007
  - 证据:
    ```python
    self._pending_motion_graphic_requests: dict[str, dict[str, Any]] = {}
    self._pending_scene_edit_requests: dict[str, dict[str, Any]] = {}
    self._pending_html_generate_requests: dict[str, dict[str, Any]] = {}
    self._html_services: dict[str, Any] = {}
    ```
    `_mark_motion_graphic_build_failed` 不弹出 plan（仅设 `_failure_reported`），服务按 scene_id 永久缓存且各持有整份场景快照。
  - 影响: 构建失败的 plan 只能靠用户点取消清理；每次创建/打开场景都会新增一个永不释放的 service+场景快照，长会话（多次生成、多项目）内存单调增长，同时放大 AUD-28001 的陈旧面。
  - 建议: 失败/完成的请求在超时后自动弹出；服务缓存按 LRU 或项目切换时清理。
  - 置信度: 高

- [AUD-28008] [Suggestion] dispose() 未清理 `_main_window`、motion/scene_edit 待处理字典与最近目标引用
  - 位置: src/features/teaching_animation/module.py:205-221
  - 证据:
    ```python
    def dispose(self) -> None:
        ...
        self._pending_html_generate_requests.clear()
        self._html_services.clear()
    ```
  - 影响: `self._main_window`（module.py:225 赋值）与 `_pending_motion_graphic_requests`、`_pending_scene_edit_requests`、`_last_motion_graphic_target` 在卸载后仍持有宿主窗口与场景引用，形成跨模块引用链，阻碍对象回收。
  - 建议: dispose 中同步置空 `_main_window`、清空其余 pending 字典与 `_last_motion_graphic_target`。
  - 置信度: 高

- [AUD-28009] [Suggestion] 两个响应处理器的返回注解为 `bool` 但有意返回 None
  - 位置: src/features/teaching_animation/module.py:1263-1265, 1568-1570
  - 证据:
    ```python
    def _handle_model_html_generate_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool:
    ```
    ```python
    def _handle_edit_plan_response(
        self, main_window: Any, request_id: str, response: str,
    ) -> bool:
        """... Returns ``None`` to the controller-side caller ..."""
    ```
  - 影响: 两方法多处 `return None` 表示"自驱动/异步挂起"，controller 也显式判 `handled_result is None`（ai_edit_controller.py:3760-3764）；注解与契约不符会误导调用方与静态检查。同文件的 `_handle_model_motion_graphic_html_response` 已正确标注 `bool | None`。
  - 建议: 将两处注解改为 `bool | None`。
  - 置信度: 高

- [AUD-28010] [Suggestion] `_extract_proposal_selection` 正则会把含 A/B/C 的短文本误判为方案选择
  - 位置: src/features/teaching_animation/module.py:5220-5234, 5291-5297
  - 证据:
    ```python
    m = _re.search(r"(?:方案|选|选择)?\s*([ABC])(?:方案)?", cleaned)
    if m:
        return m.group(1)
    ```
  - 影响: proposals 阶段任何 ≤12 字符、含字母 A/B/C 的输入（如 "BAD"、"CAN"、"ABOUT"（恰 12 字符内的变体））经 search 命中后被当作选择方案 A，跳过 `_is_cancellation_text` 等后续分支直接执行 `_select_creative_proposal`，用户本意的批评/修改意见被误执行为选定方案。
  - 建议: 用 `fullmatch` 限定整串（如 `(?:方案|选|选择)?\s*([ABC])(?:方案)?$`）或要求首尾边界。
  - 置信度: 中

- [AUD-28011] [Suggestion] `open_motion_graphic_asset` 对持久化 metadata 直接 float()/int()，损坏项目数据会抛未捕获异常
  - 位置: src/features/teaching_animation/module.py:5678-5688
  - 证据:
    ```python
    fps = float(metadata.get("fps", 25.0) or 25.0)
    fps_num = max(1, round(fps))
    duration_frames = max(1, int(metadata.get("duration_frames", round(fps * 15)) or 1))
    scene = TeachingSceneDocument.create_default(
        ...
        width=max(1, int(metadata.get("width", 1920) or 1920)),
    ```
  - 影响: metadata 来自项目文件（外部数据，铁律要求验证）；字段为非数字字符串时 float()/int() 抛 ValueError/TypeError，沿 UI 打开路径未捕获，素材无法打开且以异常形式暴露。同文件 `_restore_motion_graphic_task_memory`（module.py:429-436）对同类数据已做 try/except 校验，此处不对称。
  - 建议: 参照 restore 路径用 try/except (TypeError, ValueError) 包裹并回退默认值。
  - 置信度: 中

- [AUD-28012] [Suggestion] 两处宽泛 `except Exception: pass` 未记录日志，掩盖领域错误
  - 位置: src/features/teaching_animation/module.py:5760-5764, 5806-5814
  - 证据:
    ```python
                try:
                    worker.cancel()
                except Exception:
                    pass
    ```
    ```python
        except Exception:  # noqa: BLE001 — a settings hiccup must not block caching
            pass
    ```
  - 影响: 取消缓存 worker 失败与缓存设置写入失败被完全吞掉（连 logger.debug 都没有），违反"不得宽泛 except 掩盖领域错误"铁律；同文件同类兜底（如 module.py:3210-3221）均有日志，风格不一致。
  - 建议: 至少补 logger.debug(..., exc_info=True)。
  - 置信度: 高

### 通过项
- 信号防递归：timeline/media_pool/composite/审批卡挂接均有 `_teaching_animation_attached`、`_teaching_motion_graphic_attached`、`_teaching_motion_approval_attached` 守卫标志，避免重复 connect；卡片/状态发布为方法调用，不构成信号回路（module.py:614-634, 317-343, 5880-5886）
- GPU 资源生命周期：本文件不直接持有 ModernGL/纹理对象；渲染经 host_adapters 的 TimelineSceneFrameProvider，验证用局部 scene 映射（不跨线程订阅共享 store）且 finally 中 provider.close()（host_adapters.py:355-389, module.py:2071）
- GLSL·HDR：本文件为纯编排层，无 GLSL 代码与像素/HDR 数据处理
- 线程安全（后台浏览器后处理）：生成阶段经 controller 的 QThread worker 执行，回调由 Qt 信号队列回主线程后才能触碰 panel/项目状态；processor（_prepare_motion_graphic_html_response）确无 panel/timeline/assistant 调用（module.py:1984-2082; ai_edit_controller.py:3800-3832）
- 异常与取消路径（宿主侧防护）：模块在 _queue_motion_graphic_plan/_handle_ai_request 中抛出的 RuntimeError 被 controller 的 try/except 捕获并转为用户可见提示，不会裸逃逸进 Qt 事件循环（ai_edit_controller.py:916-942）
- 输入验证（任务记忆恢复）：_restore_motion_graphic_task_memory 对 version、Mapping 类型、数值范围（seconds/fps/frame/variant）与文本清洗（_safe_motion_graphic_task_text 截断+脱敏）校验完整（module.py:400-471）
- 重试预算：HTML 构建（_MAX_HTML_BUILD_RETRIES=2）、定向编辑（edit_retries>=1）、创意简报（creative_brief_retries>=1）与 JSON 解析（json_retries<1）均有界，无无限循环
- 状态提交一致性（时间线写入）：Motion GraphicClipCommand 经 view.execute_command 走撤销栈，写入后校验 clip.asset_id/duration/store 引用；HTML 确认路径有 revision 陈旧检查与 approved_scene_hash 哈希复核（module.py:3443-3461, 2180-2189）
- 持久化边界：_export_motion_graphic_task_memory 只保存 brief/用户文本（经 sanitize+截断），不保存模型输出或授权，任务数/字符数有上限（module.py:50-60, 361-398）

## 汇总

| 严重度 | 数量 | 编号 |
| --- | --- | --- |
| Critical | 1 | AUD-28001 |
| Major | 4 | AUD-28002, AUD-28003, AUD-28004, AUD-28005 |
| Suggestion | 7 | AUD-28006 – AUD-28012 |
