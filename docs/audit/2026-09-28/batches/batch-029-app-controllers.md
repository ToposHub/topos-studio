# batch-029 审查报告 — src/app/controllers//

- 批次：batch-029 [P1]（编号区间 AUD-2900–AUD-2999）
- 文件：host_help_sources.py (304) / layer_controller.py (1,910) / media_analysis_controller.py (275)，共 2,489 行
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 关键交叉验证（只读）：`async_renderer._do_update_aux_tex` / `_bind_aux_textures_single_layer`、`image_engine.remove_layer/discard_changes/reset_all_layers/add_layer`、`save_router.load_single_asset_data → topos_project_read_service._color_stack_dict_to_layers`、`lut_gallery.set_selected_lut`、`color_wheel.set_params`、`analysis_service.get_status/execute_analysis`、`analysis_scheduler.asynchronous`

---

## src/app/controllers/layer_controller.py (行数: 1,910)

### 发现

- [AUD-2900] [Critical] UPDATE_AUX_TEX 用 `id(layer)` 作 layer_id，与渲染端 `layer.id`(UUID) 键不匹配：应用预设/库调色后 LUT 与 Warper 静默不生效，且产生无法回收的孤儿 GPU 纹理
  - 位置: src/app/controllers/layer_controller.py:307-321（同类：421-433、725-738）
  - 证据:
    ```python
    for layer in new_layers:
        l_id = id(layer)
        # LUT 3D
        if layer.active_lut_path and self.engine.renderer:
            self.engine.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                'layer_id': l_id, 'type': 'lut3d', 'path': layer.active_lut_path
            })
    ```
    渲染端绑定键（src/features/color/core/async_renderer.py:3870-3871）：`def _bind_aux_textures_single_layer(self, prog, layer): ... l_id = layer.get('id')`；引擎权威约定（src/features/color/core/image_engine.py:1077）：`layer_id = layer.id  # Use UUID string (matches keys in baked_textures/ai_plugin_textures/layer_textures/matte_cache)`。`Layer.id = str(uuid.uuid4())`（layer.py:356）与 `id(layer)`（int 内存地址）永不相等。
  - 影响: `on_apply_grade_from_library` / `on_apply_grade_from_preset` / `on_apply_preset_categories_requested` 提交的 LUT3D/warper 纹理被存入 `layer_textures[id(layer)]`，渲染绑定按 UUID 查找落空 → 回退 `identity_lut`/`default_warper_tex`，应用后的调色 LUT 与变形网格不参与渲染（渲染错误）。且这些条目永远不会被 `REMOVE_LAYER`/`CLEAR_AUX_TEX`（均按 UUID 键清理）释放 → 每次应用泄漏一张 3D LUT/warper 纹理。已核实 `lut_gallery.set_selected_lut`（lut_gallery.py:310-322）全程 `blockSignals(True)`，`update_from_layer` 不会触发纠正性的 engine.apply_lut 重提交。
  - 建议: 三处统一改用 `layer.id`（与 image_engine.py:1181/1346-1384 的提交键一致）；可为 `UPDATE_AUX_TEX` 数据侧加断言/类型检查（非 str 键直接告警）以防回归。
  - 置信度: 高

- [AUD-2901] [Major] `_composite_startup_fallback` 异常分支引用未定义局部名 `_logger`，触发即 NameError
  - 位置: src/app/controllers/layer_controller.py:1292-1295
  - 证据:
    ```python
                try:
                    self.enter_page_hook("Composite")
                except Exception as e:
                    _logger.warning(f"Composite fallback enter_page_hook failed: {e}")
    ```
    `_logger` 仅在 `_on_color_output_changed` 内定义为局部变量（layer_controller.py:1211 `import logging; _logger = logging.getLogger(__name__)`），模块级只有 `logger`（line 22），本函数内无定义。
  - 影响: 启动回退路径中 `enter_page_hook("Composite")` 一旦抛异常，except 分支自身抛 `NameError`，沿 QTimer 回调向上传播为未处理异常——本应"记录并继续"的容错路径反而中断启动流程。
  - 建议: 改用模块级 `logger.warning(...)`；同时复查同文件其它局部 `_logger` 使用点。
  - 置信度: 高

- [AUD-2902] [Major] `request_render` 用宽泛 except 吞掉整个渲染管线异常且无堆栈
  - 位置: src/app/controllers/layer_controller.py:1022-1042
  - 证据:
    ```python
    def request_render(self, is_full_res=False, render_reason='grading'):
        try:
            ...
            self.engine.render_pipeline(is_full_res=False, render_reason=render_reason)
            ...
        except Exception as e: logger.error(f"Error: {e}")
    ```
  - 影响: 所有渲染入口（调色、遮罩、图层操作）的异常——包括引擎不变量破坏、提交失败——被压缩为一行无堆栈的 "Error: ..."，画布停留在旧帧且无用户可见提示，违反"不得宽泛 except 掩盖领域错误"铁律，且严重妨碍排障。
  - 建议: 至少 `logger.exception(...)` 保留堆栈；对已知可恢复类别（如无上下文）单独窄化处理，其余上抛或上报状态栏。
  - 置信度: 高

- [AUD-2903] [Major] 图层结构操作（增/删/移/可见性/重命名/全局重置）全程不压 history，撤销粒度与持久化脱节
  - 位置: src/app/controllers/layer_controller.py:90-120、1820-1840
  - 证据:
    ```python
    def on_layer_moved(self, old_idx, new_idx):
        if 0 <= old_idx < len(self.engine.layers) and 0 <= new_idx < len(self.engine.layers):
            layer = self.engine.layers.pop(old_idx)
            self.engine.layers.insert(new_idx, layer)
            self.engine.active_layer_index = new_idx
            ...
    ```
    `on_layer_added/on_layer_removed` 直接调 `engine.add_layer()/remove_layer()`（image_engine.py:1038-1092 已核：`add_layer/remove_layer/discard_changes/reset_all_layers` 均无 push_state）；`on_adjustments_reset`（line 1820-1823）与 `on_reset_all_click`（1825-1840）同样不压栈。对照 `on_layer_reset`（1843-1851）和关键帧操作（1546、1583）均先 `push_state`。
  - 影响: 这些操作后按 Ctrl+Z 会跳过结构变更回退到更早快照（如删层后撤销会连带回滚更早的调色）；删除图层的 GPU 纹理已被 `REMOVE_LAYER` 释放，撤销恢复后需依赖重上传路径。同时 `_trigger_autosave` 已把不可撤销的结构变更持久化，撤销与磁盘状态不一致。
  - 建议: 与 `on_layer_reset` 对齐——结构/重置操作前统一 `push_state`（可用 `copy_image=False` 降低成本）；或明确产品决策并在文档标注这些操作不可撤销。
  - 置信度: 高

- [AUD-2904] [Suggestion] Composite 页激活期间每帧执行 `print` + 多条 `logger.info` 调试输出
  - 位置: src/app/controllers/layer_controller.py:1212-1216、1230-1249
  - 证据:
    ```python
    if self.current_page == "Composite" or getattr(self, "_composite_startup_pending", False):
        print(f"[COMPOSITE-STARTUP] color_output_changed: tex={texture_id}, "
              f"size={width}x{height}, page={self.current_page}", flush=True)
        _logger.info(f"[COMPOSITE-DEBUG] color_output_changed: tex_id={texture_id}, ...")
    ```
    同函数 1232-1248 还有 5 处 `[COMPOSITE-DEBUG]` info 级日志在每次 `color_output_changed`（每帧）执行。
  - 影响: 4K/60fps 实时基准下的每帧同步 stdout/日志 I/O 与 f-string 构造开销；控制台被调试噪声淹没。属遗留调试插桩。
  - 建议: 降为 `logger.debug` 并删除 `print`，或用一次性开关控制。
  - 置信度: 高

- [AUD-2905] [Suggestion] `on_capture_still` 在保存结果未知前先写缩略图/参考帧 sidecar，保存失败留下孤儿文件
  - 位置: src/app/controllers/layer_controller.py:551-584
  - 证据:
    ```python
    from src.features.color.core.preset_meta import write_still_images
    write_still_images(topos_path, rendered_img)
    ...
    saved = self.save_router._save_single_asset_project(...)
    if saved:
        ...
    if not saved:
        self.status_bar.showMessage(I18N.tr("presets.capture_failed", "静帧保存失败"), 3000)
        return
    ```
  - 影响: `_save_single_asset_project` 失败时 `write_still_images` 已写入的 `.thumb.jpg`/参考帧文件残留在预设目录，Gallery 扫描（`scan_presets`）可能列出无工程数据的坏条目。
  - 建议: 失败分支清理 sidecar，或先保存工程成功后再写 sidecar。
  - 置信度: 高

- [AUD-2906] [Suggestion] 类型注解缺失/错误：`index: int = None` 与无注解的信号槽
  - 位置: src/app/controllers/layer_controller.py:126、135、149、982、995、1434、1022
  - 证据:
    ```python
    def on_layer_copy(self, index: int = None):
    ...
    def on_grading_changed(self, *args):
    ...
    def request_render(self, is_full_res=False, render_reason='grading'):
    ```
  - 影响: `int = None` 与运行时语义（None 表示"取活动层"）不符，静态检查失效；核心信号槽缺参数/返回注解，Mixin 契约只能靠运行时发现。
  - 建议: 改为 `Optional[int] = None`；为 `on_grading_changed` 注明 12 元组顺序（已有 `_GRADING_SIGNAL_KEYS` 可引用）。
  - 置信度: 高

- [AUD-2907] [Suggestion] 分类粘贴对话框任何异常都静默退化为"应用全部类别"
  - 位置: src/app/controllers/layer_controller.py:674-680
  - 证据:
    ```python
                try:
                    from src.features.color.ui.grade_paste_dialog import (
                        GradePasteDialog,
                    )
                    chosen = GradePasteDialog.get_categories(None, available)
                except Exception:
                    chosen = set(available)
    ```
  - 影响: 对话框抛出的任何错误（不只无头环境的导入失败）都被解释为"用户选择全部"，用户可能被应用了未确认的全部调色类别；与 `_ask_auto_color`（834-839，文档明确"无头退化"）不同，此处无文档说明且无日志。
  - 建议: 至少窄化到 ImportError 并记录日志；运行期异常应中止而非全量应用。
  - 置信度: 高

- [AUD-2908] [Suggestion] 预设图层剥离逻辑重复实现：库应用路径内联复制了 `_prepare_preset_layers`
  - 位置: src/app/controllers/layer_controller.py:277-298 与 627-643
  - 证据:
    ```python
    # on_apply_grade_from_library 内联版（277-298）：
    layer.ai_matte = None
    layer.ai_matte_enabled = True
    layer.is_baked = False
    ...
    # _prepare_preset_layers（627-643）逐行相同
    ```
  - 影响: 剥离规则（AI 蒙版/烘焙/插件/BITMAP·BRUSH 遮罩）后续演进需要双处同步，已经出现漂移风险。
  - 建议: `on_apply_grade_from_library` 复用 `self._prepare_preset_layers(source_layers)`。
  - 置信度: 高

- [AUD-2909] [Suggestion] UI 线程同步磁盘 I/O：逐个加载 AI 插件 EXR 结果与全量缓存目录扫描
  - 位置: src/app/controllers/layer_controller.py:1786-1814、1771-1784
  - 证据:
    ```python
    for layer in self.engine.layers:
        for plugin in getattr(layer, 'plugins', []):
            if plugin.get("type") == "ai" and plugin.get("ai_result_file"):
                filename = plugin["ai_result_file"]
                result_array = FileIO.load_ai_plugin_result(filename)
    ...
    FileIO.cleanup_orphaned_cache(active_files)
    ```
  - 影响: 多层多 AI 插件的 4K EXR 逐个同步读 + 缓存目录遍历都在 Qt 主线程执行，工程恢复/清理时 UI 卡顿（掉帧/无响应观感）。
  - 建议: 移入后台任务（项目已有 worker/QThread 模式），完成后经信号回主线程提交 GPU 上传。
  - 置信度: 高（阻塞事实确定；卡顿幅度取决于缓存规模，标中低影响）

- [AUD-2910] [Suggestion] 外部工程数据的 `active_layer_index` 未验证，且库应用路径忽略源活动层索引（恒 0）与预设路径行为不一致
  - 位置: src/app/controllers/layer_controller.py:255、302 与 398-399、417
  - 证据:
    ```python
    # library 路径：
    source_active_idx = 0          # line 255，从不读源数据
    ...
    self.engine.active_layer_index = min(source_active_idx, len(new_layers) - 1)
    # preset 路径：
    source_active_idx = project_data.get('active_layer_index', 0)   # line 399，未验证
    self.engine.active_layer_index = min(source_active_idx, len(new_layers) - 1)  # 负数可透传
    ```
  - 影响: 外部 .topos 文件为不可信输入：`active_layer_index` 为负数/非 int 时 `get_active_layer()` 返回 None，后续面板同步被跳过；两条应用路径活动层选择规则不一致（同一功能语义分叉）。
  - 建议: 读取后 clamp 到 `[0, len-1]` 并验证类型；统一两处来源。
  - 置信度: 高

- [AUD-2911] [Suggestion] 多处窄范围 `except Exception: pass` / debug 级吞异常，失败不可观测
  - 位置: src/app/controllers/layer_controller.py:1163-1167、1732-1737、794-798、1149-1150
  - 证据:
    ```python
        if not visible:
            try:
                self.engine.discard_pending_gpu_scope()
            except Exception:
                pass
    ...
                try:
                    provider_obj.clear()
                except Exception:
                    pass
    ```
  - 影响: scope 取消失败、降噪窗口清理失败、参考菜单同步失败等均无日志；虽单点影响小，但与"不得宽泛 except 掩盖领域错误"方向相悖，排障时盲区。
  - 建议: 统一改为 `logger.debug(..., exc_info=True)` 级别的记录。
  - 置信度: 高

### 通过项
- 线程安全：UI 侧仅经 `renderer.submit()` 队列投递命令；`on_frame_ready`/`on_gpu_scope_ready` 经信号回到主线程；`grab_viewport_image→render_full_resolution` 已核为提交渲染线程 + 嵌套事件循环等待，UI 线程无直接 OpenGL 调用
- 信号防递归：`update_from_layer` 触发的 `grading_panel.set_params` 已核带 `_syncing_ui` + `blockSignals` 双保护；`curves_widget.reset(emit_changed=False)` 显式抑制；`lut_gallery.set_selected_lut` 全程 blockSignals
- GLSL·HDR 正确性：本文件无 GLSL；`_update_scope_panel` 的 uint8/uint16 → float 归一化（/255.0、/65535.0）显式 .0，无 HDR clamp
- 取消路径：`_on_video_play_state_changed` 停止 debounce 定时器并清理降噪窗口请求，防陈旧渲染
- 性能（渲染）：播放中调色统一走 `_debounce_grading_render`（25ms 合并）；`request_render` 对 `mask_dirty` 正确升级 'full'；GPU scope demand 按播放/交互/空闲分档节流

---

## src/app/controllers/media_analysis_controller.py (行数: 275)

### 发现

- [AUD-2920] [Major] `get_asset_status` 契约与实现不符：scheduler 从未被查询，stale/running/failed 查询态不可达
  - 位置: src/app/controllers/media_analysis_controller.py:182-195
  - 证据:
    ```python
        """取某 asset 的分析状态（unknown/pending/running/ready/stale/failed）。

        优先用 scheduler 的 pending/running；其次 AnalysisService.get_status。
        """
        # scheduler 里有 pending → pending
        # （scheduler 不跟踪 per-asset，这里用 service 的记录）
        svc_status = self._service.get_status(asset_id)
        if svc_status == "ready":
            return STATUS_READY
        ...
        # 检查是否 stale（fingerprint 不匹配）—— 由 service.is_valid 判定，需 current fp
        return STATUS_UNKNOWN if svc_status == "unknown" else svc_status
    ```
    已核 `AnalysisService.get_status`（analysis_service.py:567-572）只返回 `"unknown"/"ready"/"pending"`，且 `is_valid` 从未被本 controller 调用。
  - 影响: UI badge 通过查询接口永远拿不到 running/failed（运行中显示 pending）；`STATUS_STALE` 在本 controller 完全不可达——素材变更后 badge 仍显示 ready，用户基于过期分析结果操作（§17 状态机承诺的 stale 态落空）。
  - 建议: 实现 stale 检测（`service.is_valid(asset_id, current_fingerprint)`，fingerprint 经注入的 getter 获取）；running/failed 可由本 controller 已有的信号状态缓存补齐；同时修正 docstring。
  - 置信度: 高

- [AUD-2921] [Suggestion] `_on_request_done` 失败消息硬编码中文，未走 i18n
  - 位置: src/app/controllers/media_analysis_controller.py:256-257
  - 证据:
    ```python
        else:
            self.analysis_failed.emit(request_id, "一个或多个素材分析失败")
    ```
  - 影响: 该字符串经信号直达 UI 展示，英文/其他语言环境下出现中文错误文案；与项目 `I18N.tr` 约定不符（同文件其余用户可见文本走状态常量由 UI 翻译，此处是直接消息）。
  - 建议: 发结构化错误码（如 `"partial_failure"`），由 UI 层 i18n 渲染。
  - 置信度: 高

- [AUD-2922] [Suggestion] `request_analysis_draft` 不校验空/重复 asset_ids
  - 位置: src/app/controllers/media_analysis_controller.py:95-133
  - 证据:
    ```python
        pid = project_id or self._resolve_project_id() or ""
        if not pid:
            raise ValueError(...)
        request = AnalysisRequest.create(
            project_id=pid,
            asset_ids=tuple(asset_ids),
    ```
  - 影响: `asset_ids=()` 或 `["a","a"]` 时仍会 propose 并登记：空请求在 service/scheduler 侧产生无意义 job 或直接失败；重复项导致同一 asset 状态信号重复发射。pid 有校验而 asset_ids 无，输入验证不对称。
  - 建议: 入口处校验非空、去重（保序），空集合直接返回错误/None。
  - 置信度: 高

- [AUD-2923] [Suggestion] `_current_project_id` 只写不读；工程切换未清理 service 中未确认的 propose
  - 位置: src/app/controllers/media_analysis_controller.py:203-217
  - 证据:
    ```python
        self._current_project_id = project_id
        cancelled = self._scheduler.cancel_all()
        ...
        # 清空 request→asset 映射（旧工程的不保留）
        self._request_assets.clear()
    ```
  - 影响: `_current_project_id` 为死状态（全文件仅此一处写）；scheduler 已取消，但 service 注册表中未确认的 request 仍在——切换工程后对旧 request_id 调 `confirm_and_execute` 仍会执行旧工程的分析（此时 `_request_assets` 已清空，无状态回显，行为隐蔽）。与模块 docstring "无 orphan worker" 的精神不完全一致（propose 不是 worker，但属残留可执行状态）。
  - 建议: 切换时对未确认 request 逐个 `cancel_request`；删除或真正使用 `_current_project_id`（如 execute 前校验 request 归属工程）。
  - 置信度: 中（service 侧可能有独立清理逻辑，未在本批次范围内核实）

### 通过项
- 线程安全：异步回调经 Qt 信号转发（pyqtSignal.emit 跨线程安全，连接为 queued）；`_request_assets` 的读写均为 GIL 原子的单操作
- 三步流红线：propose/confirm/execute 分离清晰，未确认请求依赖 service 红线拒绝执行，符合 §17.3
- 异常与取消路径：`confirm_and_execute` 对 confirm 失败（KeyError/ValueError）窄化捕获并回报；execute 失败统一标记 FAILED + `analysis_failed`
- API 契约：`propose_analysis/confirm_analysis/execute_analysis/get_request/cancel_request/get_status` 与 scheduler `asynchronous` 属性均已核实存在
- 类型注解：公开方法参数/返回注解完整

---

## src/app/controllers/host_help_sources.py (行数: 304)

### 发现

- [AUD-2930] [Suggestion] 全文件以 `except Exception` + debug 级日志兜底，意外（非导入类）失败在生产环境不可见
  - 位置: src/app/controllers/host_help_sources.py:62-97、112-116、150-159、187-198、223-227
  - 证据:
    ```python
        try:
            for code_enum in TaepDiagnosticCode:
                ...
        except Exception:  # noqa: BLE001
            logger.debug("list_diagnostics iterate failed", exc_info=True)
            return None
    ```
  - 影响: "单源失败不阻断"是文档化设计，import 失败返回 None 合理；但遍历/读取阶段的真实缺陷（如 MESSAGE_TEMPLATES 结构变更导致的 AttributeError）同样只留 debug 日志，默认日志级别下完全不可见，帮助源静默消失难以排查。
  - 建议: 区分预期失败（ImportError → debug）与意外失败（迭代期异常 → warning 以上）；保持返回 None 不阻断的语义不变。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR：纯数据适配模块，无 Qt 控件、无 OpenGL、无浮点计算，`不引入 moderngl/composite.core` 的红线达成（import 均为编辑域/共享层）
- 输入验证：所有 host 属性经 `getattr(..., None)` 防御，单条目失败逐项兜底（get_shortcut 异常回退 default_shortcut）
- "不编造"红线：label/suggested_action 仅取自既有模板/解释表，缺失用空串或 id 本身
- 性能模式：5 个 list_* 均为一次遍历的轻量枚举，无热路径
- 类型注解：方法签名完整（`list[dict[str, Any]] | None` 等）
- `_strip_placeholders` 括号深度计数实现正确，空串/无占位符路径无异常
