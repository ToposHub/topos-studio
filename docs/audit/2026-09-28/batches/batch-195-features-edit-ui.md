# 批次报告：batch-195 [P1] src/features/edit/ui//

编号区间：AUD-19500–AUD-19599。审查日期：2026-09-28。
通用说明：docs/audit/2026-09-28/assignments/INSTRUCTIONS.md。

## src/features/edit/ui/ai_edit_assistant_panel.py (行数: 3,784)

AI 编辑助手主 Dock 面板（QDockWidget）：斜杠命令/媒体 @ 候选、消息流渲染、附件预览、Run Drawer/Composer footer 状态注入、分析/Motion Graphic 提议卡。纯 QWidget 层，通过细粒度 pyqtSignal 与 controller 通信。

### 发现

- [AUD-19500] [Major] "改用内置模板"按钮与"确认生成"发出完全相同的信号，内置模板路径永远无法触发
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:3583-3595（对照 3578-3581）
  - 证据:
    ```python
    # 确认按钮（3578-3581）
    confirm.clicked.connect(
        lambda _checked=False, rid=request_id:
        self.motion_graphic_confirm_requested.emit(rid)
    )
    # "改用内置模板"按钮（3583-3594）——同一个信号、同一个 payload
    if "choose_template" in actions_dict:
        template_btn = QPushButton(
            tr("ai_assistant.motion_use_template", "改用内置模板")
        )
        template_btn.clicked.connect(
            lambda _checked=False, rid=request_id:
            self.motion_graphic_confirm_requested.emit(rid)
        )
    ```
  - 影响: 订阅方（src/features/teaching_animation/module.py:3236-3253 `_confirm_motion_graphic_plan`）仅按 request_id + 内部 phase 分派；MG-05 守卫（module.py:3424-3435）要求 `plan["use_template"]` 为 True 才构建内置模板场景，但全仓库检索 `use_template` 只在 module.py 被读取、从未在生产路径写入（仅测试直接注入）。因此点击"改用内置模板"实际走普通确认流，scene 为 None 时只会弹出"场景未生成成功"错误——该按钮是死功能。
  - 建议: 为模板动作发独立信号（如 `motion_graphic_template_requested(str)`）或在信号 payload 中携带 action_kind；module 侧据此设置 `plan["use_template"] = True` 再走确认。
  - 置信度: 高

- [AUD-19501] [Major] 每次 refresh_state 全量销毁重建全部消息卡，含 base64 附件解码与 Markdown 重解析
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:2008-2043（签名只用于滚动决策）；2226（每次重建重复解码图片）
  - 证据:
    ```python
    message_signature = tuple(
        (getattr(message, "message_id", ""), ...)
        for message in s.messages
    )
    messages_changed = message_signature != self._last_rendered_message_signature
    self._last_rendered_message_signature = message_signature
    # 动态卡片不是 snapshot.messages 的一部分，不能随状态刷新被删除。
    for widget in self._message_widgets:
        self._messages_layout.removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()
    self._message_widgets = []
    ...
    for m in messages:
        card = self._build_message_card(m)
    ```
    以及 2226：`pm.loadFromData(_decode_data_url_bytes(getattr(att, "data_url", "")))`
  - 影响: `src/app/controllers/ai_edit_controller.py:817` 在 `_on_service_state` 中对每次 service 状态回调调用 `refresh_state`（注释明确"对话每次追加/恢复消息都会经过 service 状态回调"）。即使 `messages_changed == False` 也无提前 return：全部消息卡被 deleteLater 后重建，每条消息重新 `setMarkdown` 解析、每张图片附件重新 b64decode + QPixmap.loadFromData + scaledToHeight。长会话/多图附件时 UI 线程周期性卡顿，挤占 4K/60FPS 基准下的 UI 线程预算，并造成可见闪烁。`messages_changed` 标志已计算却只用于滚动决策。
  - 建议: 签名未变化时提前 return；或按消息做增量 diff（只重建新增/变化的消息，复用未变卡片）。
  - 置信度: 高

- [AUD-19502] [Major] set_motion_graphic_proposal_card 只有 finally 没有 except，畸形 card_data 的异常直接逃逸到 Qt 槽
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:3253-3267（try/finally 结构）；3304、3309-3313、3459、3528（多处未校验的 dict 访问）
  - 证据:
    ```python
    def set_motion_graphic_proposal_card(
        self, request_id: str, card_data: dict,
    ) -> None:
        ...
        self._updating = True
        try:
            ...
            canvas_w, canvas_h = card_data.get("canvas_size", [1920, 1080])  # 3304
    ...
        finally:                       # 3607-3608：无 except 分支
            self._updating = False
    ```
  - 影响: `canvas_size` 非 2 元序列 → ValueError；`quality_warnings`/`proposals`/`elements` 为 None 或元素非 dict → TypeError/AttributeError；`duration_seconds` 为字符串时 `"{duration:g}"` → ValueError。该方法由 controller 在 Qt 槽内调用，异常逃逸到 PyQt6 槽回调可触发 qFatal/abort；兄弟方法（set_skill_recommendation 3625、set_result_card 3646、set_mission_workspace 3757、set_skill_form 3781）都有 `except Exception → _show_component_error` 兜底，唯独此方法漏掉；`_updating` 虽由 finally 复位，但半渲染卡片会残留在消息流布局中。
  - 建议: 补 `except Exception as exc: self._show_component_error("Motion Graphic 方案卡", exc)`；对 canvas_size/duration/列表字段做类型与长度校验后再解包/格式化。
  - 置信度: 中（card_data 正常来自 `MotionGraphicProposalCard.to_dict()`，但 dict 边界无契约保障，防御缺失确定存在）

- [AUD-19503] [Major] AI/模型生成的文本未做 HTML 转义直接插入 RichText/AutoText 标签
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:1958-1959；2347-2350；3201-3205；3288
  - 证据:
    ```python
    title = QLabel(f"<b>{v.label}</b>")                      # 1958 变体 label 来自 planner
    title.setTextFormat(Qt.TextFormat.RichText)              # 1959
    ...
    head = QLabel(                                            # 2347 上下文块 kind 来自 ContextBundle
        f"<b>{kind}</b> · {priority_label} · {tokens} tokens{trunc_mark}"
    )
    head.setTextFormat(Qt.TextFormat.RichText)                # 2350
    ...
    heading = QLabel(heading_text)                            # 3288 MG 方案 title（模型输出）
    ```
  - 影响: 变体 label、上下文块 kind、Motion Graphic 方案 title/summary 均为模型/外部 bundle 产生的字符串，含 `<`、`&`、`<a href>` 等字符时会被当作富文本标记，导致显示错乱或注入任意标记（如隐藏后续内容、伪造粗体/标题）。违反"外部数据须验证"铁律；同文件 `_render_static_activity_status`（1721-1727）已示范正确的 `_html.escape` 用法，此处不一致。
  - 建议: 对所有来自模型/外部的字符串先 `_html.escape()` 再拼入富文本模板；纯文本展示的 QLabel 显式 `setTextFormat(Qt.TextFormat.PlainText)`。
  - 置信度: 高（代码模式确定；影响为显示层破坏，非崩溃）

- [AUD-19504] [Major] QTimer.singleShot 滚动 lambda 未防护面板销毁竞态（同类定时器路径已有防护）
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:2908-2914（对照 2126-2136）
  - 证据:
    ```python
    if has_activity:
        QTimer.singleShot(
            0,
            lambda: self._messages_area.verticalScrollBar().setValue(
                self._messages_area.verticalScrollBar().maximum()
            ),
        )
    ```
  - 影响: 代码自身在 `_attempt_messages_scroll`（2126-2136）注释并捕获了"a queued timeout can still race a native dock teardown"导致的 RuntimeError（wrapped C/C++ object deleted）。此 singleShot lambda 在下一轮事件循环访问 `_messages_area.verticalScrollBar()`，同样暴露于该竞态却无 try/except；异常逃出定时器回调在 PyQt6 中可导致未处理异常/qFatal。同一问题两个调用点防护不一致。
  - 建议: lambda 内包 `try/except RuntimeError`，或统一复用带防护的 `_messages_scroll_timer` 路径。
  - 置信度: 中（窗口小，但竞态类别被代码注释明确承认）

- [AUD-19505] [Suggestion] 超长输入被静默截断到 2000 字符，用户无感知
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:2382、2422-2423
  - 证据:
    ```python
    _MAX_REQUEST_LEN = 2000  # 防止超长 prompt（真实 token 预算在 M5）
    ...
    if len(text) > self._MAX_REQUEST_LEN:
        text = text[: self._MAX_REQUEST_LEN]
    self.submit_request(text)
    ```
  - 影响: 超出部分直接丢弃且无任何提示，用户以为完整需求已发送，模型只收到前 2000 字符，产生难以排查的"AI 没理会后半段"问题。
  - 建议: 截断时用 `set_agent_notice` 提示被截断的字符数，或在输入层限制并显示计数。
  - 置信度: 高

- [AUD-19506] [Suggestion] 变体卡列表每次刷新全量重建，无签名/差异检查
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:1914-1947
  - 证据:
    ```python
    def _render_variants(self, s: AssistantStateSnapshot) -> None:
        # 清空旧卡片（保留尾部 stretch）
        while self._variants_layout.count() > 1:
            item = self._variants_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        ...
        for v in variants:
            self._variants_layout.insertWidget(
                self._variants_layout.count() - 1, self._build_variant_card(v, can_apply)
            )
    ```
  - 影响: 与 AUD-19501 同类的重建抖动（量级较小：变体通常 2-4 张）；每次状态回调都销毁重建按钮与标签，造成不必要的光标/焦点闪烁与 CPU 开销。
  - 建议: 比照 `_render_messages` 的签名机制，为 variants 计算 `(variant_id, status, ...)` 签名，未变化时跳过。
  - 置信度: 高

- [AUD-19507] [Suggestion] 多处宽泛 except 静默降级且不记录日志
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:174；1183-1196；1411-1413；1446-1447
  - 证据:
    ```python
    except Exception:  # noqa: BLE001
        return b""                                   # 174 _decode_data_url_bytes

    except Exception:
        # AiContextSummary 装配失败 → 回退到旧 QLabel（不阻断面板）
        self._context_details = QFrame()             # 1183-1185，无任何日志

    except Exception:
        self._run_drawer = None
        self._run_section = None                     # 1411-1413
    ```
  - 影响: 子组件（AiContextSummary/AiRunDrawer/AiAssistantComposer）真实 bug（非 ImportError）被静默吞掉，降级后无日志可查，现场难以诊断"footer 怎么没了"。与铁律"不得宽泛 except 掩盖领域错误"冲突。
  - 建议: 至少 `logger.warning(..., exc_info=True)` 后再降级；能收窄异常类型时优先 (ImportError, AttributeError)。
  - 置信度: 高

- [AUD-19508] [Suggestion] 硬编码中文提示未走 i18n（tr/_trf）
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:2100-2102；2526-2530
  - 证据:
    ```python
    label = QLabel(
        f"⚠️ {component} 暂时无法显示，请重试。"
    )
    ...
    if rejected:
        self.set_agent_notice(
            f"有 {rejected} 个拖入文件无法添加，请检查格式、大小和读取权限。",
            warning=True,
        )
    ```
  - 影响: 面板其余文案全部经 `tr()`，这两处（以及同类直接拼中文的 set_agent_notice 调用点）在非中文 locale 下仍是中文，i18n 契约不一致。
  - 建议: 改用 `_trf("ai_assistant.drop_rejected", "...", count=rejected)` 等 key。
  - 置信度: 高

- [AUD-19509] [Suggestion] QStackedWidget 页码使用魔法数字索引
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:1497；1812-1841（1/2/3/4）；2271（== 5）
  - 证据:
    ```python
    ctx_back_btn.clicked.connect(lambda: self._stack.setCurrentIndex(2))
    ...
    if s.state == AssistantState.NO_PROJECT:
        self._stack.setCurrentIndex(2)
    elif s.state == AssistantState.NO_PROFILE:
        self._stack.setCurrentIndex(1)
    ...
    if self._stack.currentIndex() == 5:
    ```
  - 影响: 新增页面（历史曾发生：ctx_page 是第 6 个 addWidget）时所有硬编码索引需同步修改，漏改即切错页（隐性功能缺陷温床）。
  - 建议: 定义 `_PAGE_EMPTY, _PAGE_NO_PROFILE, _PAGE_MAIN, _PAGE_ERROR, _PAGE_BRIEF, _PAGE_CONTEXT` 常量或在 addWidget 时记录 widget→index 映射。
  - 置信度: 高

- [AUD-19510] [Suggestion] 多个公开/私有方法参数与容器缺类型注解
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:254-306（source 参数）；921（menu: Any）；1547（sessions: object）；1949（v）；2184（m）；2321（b）；3254（card_data: dict）
  - 证据:
    ```python
    def _build_variant_card(self, v, can_apply: bool) -> QWidget:
    def _build_message_card(self, m) -> QWidget:
    def _build_context_block_card(self, b) -> QWidget:
    def set_motion_graphic_proposal_card(
        self, request_id: str, card_data: dict,
    ) -> None:
    ```
  - 影响: 项目要求强类型（.clinerules）；variant/message/context-block/提案卡数据形状完全无类型约束，重构与审查成本高。
  - 建议: 标注为对应模型类型（VariantCard、AssistantMessage、ContextBlock、dict[str, Any]），source 标注 QMimeData。
  - 置信度: 高

- [AUD-19511] [Suggestion] 图片附件的解码与 JPEG 编码在 UI 线程同步执行
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:2465-2473；2659-2664
  - 证据:
    ```python
    attachment = attachment_from_qimage(qimage, source=source)   # 降采样+JPEG 编码，同步
    ...
    for path in paths:
        image = QImage(path)                                     # 同步解码整图
        if image.isNull():
            continue
        if self.add_image_attachment(image, source="file"):
            added += 1
    ```
  - 影响: 从对话框批量添加或粘贴 4K 截图时，多张图片的解码+缩放+JPEG 编码串行占用 UI 线程，可感知卡顿（项目 UI 响应性基准下应避免）。
  - 建议: 多文件路径限制单次数量或分帧处理（QTimer 分批），大图编码考虑移出 UI 线程后信号回填。
  - 置信度: 中

- [AUD-19512] [Suggestion] @ 媒体候选在每次按键时同步调用 media-pool provider
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:798-810
  - 证据:
    ```python
    query = match.group(1).lower()
    candidates = self._media_pool_candidates()   # 每次textChanged都调用 provider()
    matches = [
        candidate for candidate in candidates
        if not query or query in candidate["display_name"].lower()
        ...
    ]
    ```
  - 影响: provider 由宿主注入（遍历当前工程媒体池）；用户每敲一个字符触发一次全量枚举+字符串匹配，大素材库时输入卡顿。
  - 建议: 对候选列表做短 TTL 缓存（如 500ms）或仅在 `@` 触发时枚举一次、后续按键复用。
  - 置信度: 中

- [AUD-19513] [Suggestion] 无 Run Drawer 时 shimmer 定时器空转
  - 位置: src/features/edit/ui/ai_edit_assistant_panel.py:1679-1684；1734-1750
  - 证据:
    ```python
    if self._activity_status_active:
        if not self._activity_shimmer_timer.isActive():
            self._activity_shimmer_timer.start()   # 90ms 周期
        self._animate_activity_status()
    ...
    def _animate_activity_status(self) -> None:
        if not getattr(self, "_activity_status_active", False):
            return
        if not getattr(self, "_activity_status_in_footer", False):
            drawer = getattr(self, "_run_drawer", None)
            live = getattr(drawer, "set_live_status", None)
            if callable(live):
                ...
            return                                # drawer 为 None 时每 tick 空转
    ```
  - 影响: Run Drawer 装配失败（_run_drawer=None）且活动状态 active 时，90ms 定时器持续空跑直到 clear_activity_status，纯耗电/CPU 噪音。
  - 建议: 启动定时器前检查 drawer 可用性，或 drawer 缺失时不启动。
  - 置信度: 高

### 通过项

- 线程安全：面板仅在 UI 线程操作 QWidget/QTimer/QPixmap；未调用任何 OpenGL/渲染 API；与 service/controller 的全部交互经 Qt 信号（request_submitted、context_block_excluded 等），无跨线程裸调用或共享可变状态。
- GPU 资源生命周期：文件不创建/持有 ModernGL、纹理或缓冲区（纯 QWidget 面板），无 GPU 泄漏面。
- GLSL·HDR 正确性：不涉及 GLSL/HDR 数据处理，不适用。
- 信号防递归：refresh_state（1783-1788）、set_pending_brief、set_composer_footer（2802-2838）、set_run_drawer（2844-2916）及各 set_* 卡片方法均以 `_updating` 锁包裹；_on_send_or_cancel_clicked、_on_send_clicked、submit_request、_on_composer_permission、_update_slash_commands、_update_media_mentions 均检查 `_updating`。
- 取消路径：Escape→cancelled→cancel_requested、busy 时统一按钮转取消、preserve_cancelled_streaming_reply 保留部分回复、run_cancel_requested 逐项取消；_attempt_messages_scroll（2126-2141）正确捕获 RuntimeError 并做有界重试。
- 输入验证（部分通过）：_add_file_attachment（2727-2753）校验 isfile、大小上限 512MB、MIME 分类；_media_pool_asset_ids 捕获 JSON/解码异常；拖拽路径去重（seen_paths）。
- 状态提交一致性：面板不持有 undo/redo/revision 业务状态，仅发信号；submit_request 附带 brief_revision 供 controller 校验快照新旧。
- 资源释放：消息卡/变体卡/预览 chip 销毁均使用 removeWidget + setParent(None) + deleteLater，弹出列表与菜单以 central/self 为父，无明显泄漏。
