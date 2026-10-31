# batch-022 审查报告：src/app/controllers//

编号区间：AUD-2200–AUD-2299。审查日期：2026-09-28。
两个文件均已全文通读（ai_editing_mixin.py 1,902 行分两段读完；ai_workflow_composite_render.py 359 行一次读完），
跨模块论断（CompositeFrameRenderer 生命周期、GenerationBin 线程安全、导出颜色编码路径）均已 Read/grep 到真实代码佐证。

---

## src/app/controllers/ai_editing_mixin.py (行数: 1902)

### 发现

- [AUD-2200] [Major] 装配失败的外层 except 不停用已启动的 MCP host / tool facade / providers
  - 位置: src/app/controllers/ai_editing_mixin.py:1059（MCP host 赋值点 676-679）
  - 证据:
    ```python
    except Exception:
        # 装配失败不吞：记录错误，让 UI 保持未装配（面板仍是空壳状态）
        logger.error("Failed to set up AI editing subsystem", exc_info=True)
        self._ai_edit_controller = None
        self._ai_assistant_service = None
        self._ai_host_bridge = None
        self._ai_approval_service = None
        self._ai_session_store = None
    ```
    而此前 line 676-679 已执行 `socket_path = local_host.start(); self._ai_mcp_host = local_host; self._ai_mcp_dispatcher = dispatcher`，line 647 已赋值 `self._ai_tool_facade = facade`。
  - 影响: 若 MCP host 启动成功后、后续装配（如 controller 构造 837 行）抛异常，本地 Unix socket 继续对外服务，ToolFacade 持有的 session/analysis_service/approval_service 指向已被置 None 的半拆除装配；外部 stdio 客户端可在该窗口内对不一致状态调用工具。`_generation_providers`、`_comfy_generation_executors` 同样残留，直到下一次 `_teardown_ai_editing` 才清理。
  - 建议: 外层 except 中复用 `_teardown_ai_editing()`（或在 except 里显式 stop MCP host、close generation service、清 comfy executors），保证错误路径与正常 teardown 资源状态一致。
  - 置信度: 高

- [AUD-2201] [Suggestion] KillSwitch（自主编辑急停）构造失败被完全静默吞掉，无任何日志
  - 位置: src/app/controllers/ai_editing_mixin.py:331-335
  - 证据:
    ```python
    try:
        from src.features.edit.ai_editing.kill_switch import KillSwitch
        self._ai_kill_switch = KillSwitch()
    except Exception:  # noqa: BLE001
        self._ai_kill_switch = None
    ```
  - 影响: 注释自述这是自主编辑的独立紧急停止（incident response 依赖）。静默置 None 后急停功能不可用且无任何痕迹，违反"宽泛 except 不得掩盖领域错误"精神（本文件其余降级点均带日志，唯此处无）。
  - 建议: 至少 `logger.error("AI kill switch unavailable", exc_info=True)`；考虑在 UI 上暴露急停不可用状态。
  - 置信度: 高

- [AUD-2202] [Suggestion] teardown 中 `EditSession.unbind()` 用裸 `except Exception: pass` 且无日志
  - 位置: src/app/controllers/ai_editing_mixin.py:1583-1589
  - 证据:
    ```python
    # 解绑 EditSession（service 持有的 session 不再有效）
    try:
        from src.features.edit.ai_editing.edit_api import EditSession

        EditSession.unbind()
    except Exception:
        pass
    ```
  - 影响: 与模块 docstring「装配失败不 broad `except: pass`」的自律不一致；unbind 失败（如 import 期错误）完全不可观测，残留的全局 session 绑定可能在下一工程被误用。
  - 建议: 改为 `except Exception: logger.debug(..., exc_info=True)`，与本文件其他 teardown 分支一致。
  - 置信度: 高

- [AUD-2203] [Suggestion] shared_runtime 解析失败为 None 时仍构造 `LegacyHarnessAdapter(service, None)`，故障被推迟到首次运行
  - 位置: src/app/controllers/ai_editing_mixin.py:690-694、828-836（adapter 构造在 build_p1_dependencies 内 line 135）
  - 证据:
    ```python
    try:
        shared_runtime = resolve_shared_runtime(self)
    except Exception:
        logger.debug("shared runtime resolve failed", exc_info=True)
        shared_runtime = None
    ...
    p1_deps = build_p1_dependencies(
        service=service, runtime=shared_runtime, ...
    ```
    而 `LegacyHarnessAdapter.__init__(self, service: Any, runtime: HarnessRuntime)`（legacy_adapters.py:112）不接受 None，`run()` 内 `self._runtime.start(inv)` 将 AttributeError。
  - 影响: HarnessRuntime 导入失败的罕见路径下，带坏 adapter 的 controller 照常装配成功（debug 日志），用户首次执行 skill 时才崩溃/失败——装配期本可显式拒绝。
  - 建议: `shared_runtime is None` 时跳过 p1_deps 的 adapter 注入（controller 已声明 runtime=None 向后兼容），或让 build_p1_dependencies 对 runtime=None 显式返回降级配置。
  - 置信度: 中

- [AUD-2204] [Suggestion] `_all_analysis_fresh` 中 `isinstance(aid, tuple)` 分支两臂完全相同（死代码）
  - 位置: src/app/controllers/ai_editing_mixin.py:1760-1766
  - 证据:
    ```python
    for aid, asset in asset_items:
        # assets 可能是 dict[asset_id -> MediaAsset] 或其他形态
        if isinstance(aid, tuple):
            # 非 dict.items() 形态（即 (asset_id, asset) 对），取第一项
            aid_value, asset_obj = aid, asset
        else:
            aid_value, asset_obj = aid, asset
    ```
  - 影响: 注释声称处理"非 dict.items() 形态"，但两分支赋值完全一样，该形态实际从未被处理（若 `for aid, asset in` 遇到嵌套 tuple 会直接 ValueError）；误导后续维护。
  - 建议: 删除死分支，或在进入循环前规范 `asset_items` 的形态。
  - 置信度: 高

- [AUD-2205] [Suggestion] `bind_comfy_workflow`/`test_comfy_connection` 在宿主方法内同步阻塞 HTTP（默认 30s 超时）
  - 位置: src/app/controllers/ai_editing_mixin.py:1681-1693、1706-1710
  - 证据:
    ```python
    client = getattr(executor, "_server", None)
    try:
        caps = client.capabilities()
        executor.set_server_capabilities(caps)
        executor.bind_workflow(snapshot, profile=profile)
    ```
    `capabilities()` 为 `GET /object_info`（comfyui_client.py:232-238），`_request_json` 默认 `request_timeout_s: float = 30.0`（comfyui_client.py:121）。
  - 影响: 若从 GUI 线程调用（宿主公开 API，目前仓库内无生产调用方，风险为前瞻性），/object_info 在大模型库的 ComfyUI server 上响应可达数 MB，UI 最长冻结 30 秒。
  - 建议: 移到 worker/任务队列执行，或至少把超时显式调小并向调用方文档化线程约束。
  - 置信度: 中

- [AUD-2206] [Suggestion] 大量跨类/跨模块私有属性访问（`_empty_capabilities`、`_server`、`_svc`、`_catalog`、`_refresh_history_menu`、`_timeline`）
  - 位置: src/app/controllers/ai_editing_mixin.py:786-787、1279、1306-1309、1560-1561、1680、1704、382
  - 证据:
    ```python
    self._comfy_generation_executors[...] = ComfyWorkflowExecutor(
        server=client,
        server_capabilities=client._empty_capabilities(),
    )
    ...
    store._catalog = SessionCatalog.from_dict(catalog_data)
    ...
    generation_service = getattr(generation_adapter, "_svc", None)
    ```
    另有 `getattr(executor, "_server", None)`（1680/1704）、`getattr(controller, "_refresh_history_menu", None)`（1307）、`getattr(view, "_timeline", None)`（382）。
  - 影响: 装配根深度耦合被装配方的私有内部名，任何一方重命名（如 `_svc`、`_catalog`）都会让降级路径静默失联（大多裹在 try/except 里只留 debug 日志）。
  - 建议: 为 RealComfyClient/GenerationHarnessAdapter/AssistantSessionStore/controller 暴露公开访问器（如 `empty_capabilities()`、`generation_service` property、`restore_catalog` 已存在则直接依赖），消除私有名依赖。
  - 置信度: 高

- [AUD-2207] [Suggestion] 类型注解缺失/退化：`_SessionHostShim` 多方法无返回注解，闭包返回裸 `list`
  - 位置: src/app/controllers/ai_editing_mixin.py:172、1813-1835
  - 证据:
    ```python
    def provider(asset_ids: tuple[str, ...], kind: str) -> list:
    ...
    def get_state(self):
    ...
    def get_manifest(self):
    ...
    def approval_service(self):
    ```
  - 影响: 组合根是理解装配关系的关键路径，裸 `list` 与无注解方法削弱可读性与静态检查价值（.clinerules 强类型要求）。
  - 建议: 补 `list[SemanticSegment]`、`-> ProjectSequences | None` 等精确注解。
  - 置信度: 高

- [AUD-2208] [Suggestion] `make_editorial_provider` 的闭包吞掉 AnalysisService 所有异常并返回空证据，无日志
  - 位置: src/app/controllers/ai_editing_mixin.py:178-184
  - 证据:
    ```python
    out: list = []
    try:
        for aid in asset_ids or ():
            for seg in analysis_service.get_segments(aid):
                if seg.kind == want:
                    out.append(seg)
    except Exception:  # noqa: BLE001
        return []
    ```
  - 影响: 分析服务内部错误（如数据损坏、锁问题）与"确实没有 transcript 证据"不可区分，editorial skill 会以 "No evidence" 降级，领域错误被掩盖且不可观测。
  - 建议: 捕获后 `logger.debug/warning(..., exc_info=True)`；可区分 per-asset 失败（保留已取得的部分）与整体失败。
  - 置信度: 中

### 通过项
- 信号防递归：`configure_models_requested` 采用先 disconnect（容忍 TypeError/RuntimeError）再 connect 的幂等重连（416-423），重复 `_setup_ai_editing` 不会重复触发。
- 状态提交一致性：`_teardown_ai_editing` 优先从旧 service 的启动配置取 project_id 再撤 grants，避免工程切换后撤错对象（1520-1531）；`apply_composite_edit` 走 approval grant consume + `record_external_composite_edit` 记录外部编辑供 undo（1864-1898）。
- 线程红线（MCP）：socket 线程请求经 `QtMcpRequestDispatcher.dispatch` 回 GUI 线程后才触达 ToolFacade（655-675 装配方式与注释一致）；`_mcp_context` 仅做属性读取。
- 异常路径：`_setup_ai_editing` 外层 except 记录 error 并进入确定未装配状态（1059-1066）；媒体分析子系统有独立 except 且失败时清空全部相关引用（1050-1058）。
- 输入验证：`make_localization_media_provider` 对外部输入做 strip/空判/int+max(0) 校验，异常显式返回 None（199-224）；`_resolve_output_resolution` 类似（见另一文件）。
- GPU/GLSL/HDR：本文件不含 GLSL 与渲染数值处理，不直接持有 GL 对象。
- 装配设计：`EditSession.unbind()` 后再 bind、teardown 先行，重复 setup 的重建顺序正确（426-444）。

---

## src/app/controllers/ai_workflow_composite_render.py (行数: 359)

### 发现

- [AUD-2209] [Critical] `_write_png` 把 scene-linear 帧直接量化为 8bit PNG，缺少 sRGB/显示变换，产出的合成渲染图系统性偏暗
  - 位置: src/app/controllers/ai_workflow_composite_render.py:263-269（调用点 238-239、quality 选择 254-260）
  - 证据:
    ```python
    def _write_png(frame_bgr_float: Any, output_path: str) -> bool:
        """BGR float32 [0,1] → 8bit PNG（cv2 与 composite 模块同源依赖）。"""
        import cv2
        import numpy as np

        array = np.clip(np.asarray(frame_bgr_float) * 255.0 + 0.5, 0, 255)
        return bool(cv2.imwrite(output_path, array.astype(np.uint8)))
    ```
    而图像走 `quality="export"` → readback 为 **linear_f32**（composite_frame_renderer.py:198-199 `"srgb_u8" if quality == "preview" else "linear_f32"`）；主导出路径对 png/jpg 显式先 `_encode_linear_to_srgb(img, config_manager=...)` 再量化（composite_render_output.py:1196-1201，docstring「8/16-bit display formats ... expect sRGB-encoded values to match what the viewer shows」）；本文件视频路径也经 `render_video` 内部 `transform_color(rgb_linear, LINEAR_SCENE, ...)`（composite_render_output.py:483-486）。
  - 影响: AI 工作流产出的合成渲染 PNG 是 scene-linear 数值直接乘 255——与预览器/正式导出相比整体偏暗、高饱和色错位（仓库自己的注释：「otherwise linear pinks become dark saturated red」）。这是交付产物的渲染错误，且 HDR>1.0 值被硬 clip。
  - 建议: `_write_png` 前复用导出侧同一颜色管线：`transform_color`/`_encode_linear_to_srgb`（带 config_manager，尊重 OCIO），与 FileOutput/视频路径保持同源；顺带消除无理由 clip。
  - 置信度: 高

- [AUD-2210] [Major] `_render_single_frame` 每次新建 `CompositeFrameRenderer` 但从不调用 `cleanup()`，GL context/纹理池依赖 GC 释放
  - 位置: src/app/controllers/ai_workflow_composite_render.py:250-260
  - 证据:
    ```python
    renderer = CompositeFrameRenderer()
    return renderer.render_comp_clip_frame(
        graph_data,
        edit_project=edit_project,
        timeline_sequences=timeline_sequences,
        quality="export",
    )
    ```
    `CompositeFrameRenderer.cleanup()`（composite_frame_renderer.py:571-591）负责释放 async PBO、纹理池与内部 export renderer 的 GL 资源；`_get_export_renderer` 会跨调用缓存 context（666+）。
  - 影响: 每次 AI 图像渲染泄漏一个 worker 线程 GL context + 资源池直到 GC（存在回调引用环时更久），多次渲染后显存/句柄累积，违反项目 deferred-release/GPU 资源显式释放铁律。
  - 建议: `try/finally` 中调用 `renderer.cleanup()`；或复用线程绑定的长生命周期 renderer 并在会话结束时统一清理。
  - 置信度: 高（cleanup 未被调用是事实；泄漏时长取决于 GC，标高）

- [AUD-2211] [Major] worker 线程 provider 活读 `main_window._current_edit_project/_current_timeline_sequences`，违反本模块自述的"worker 只消费 dict 快照"红线
  - 位置: src/app/controllers/ai_workflow_composite_render.py:156-162（模块契约声明见 5-13 行 docstring）
  - 证据:
    ```python
    edit_project = getattr(main_window, "_current_edit_project", None)
    timeline_sequences = getattr(main_window, "_current_timeline_sequences", None)
    config_manager = getattr(main_window, "config_manager", None)
    output_path, modality, duration_seconds = _render_headless(
        graph_data, edit_project, timeline_sequences, config_manager,
        is_cancelled,
    )
    ```
  - 影响: 渲染期间主线程切换/关闭工程会把 worker 的 edit_project/timeline_sequences 换成新引用或 None——旧图对着新工程资产解析（素材错配）或中途 None（渲染失败）；且主线程可能并发变异 project dict，worker 遍历时存在竞态。docstring 承诺的隔离（"只消费 dict 快照"）未兑现。
  - 建议: 在主线程 resolver 阶段把 edit_project/timeline_sequences 所需的不可变数据一并快照进 `invocation.inputs`，worker 只读快照；至少对工程切换设置 cancel 令牌使旧渲染作废。
  - 置信度: 中

- [AUD-2212] [Major] `gen_bin.add_candidates` 在 worker 线程无锁写入，GUI 线程同时迭代 GenerationBin 可触发 RuntimeError
  - 位置: src/app/controllers/ai_workflow_composite_render.py:314（bin 实现 generation_bin.py:154-159，读侧 generation_bin.py:126-146）
  - 证据:
    ```python
    gen_bin.add_candidates((asset,))
    return asset_id
    ```
    GenerationBin 为纯 Python dict/list、无锁：`def add_candidates(self, candidates): for c in candidates: if c.asset_id not in self._assets: self._assets[c.asset_id] = c`；而 `all_assets/by_state` 用生成器遍历 `self._assets.values()`。
  - 影响: 生成素材箱面板在 GUI 线程刷新（遍历 bin）与 worker 插入候选并发时，CPython dict 迭代中 resize 会抛 "dictionary changed size during iteration"，UI 刷新路径异常。
  - 建议: GenerationBin 内加 `threading.Lock`（或把 worker 产物经信号/队列投递回 GUI 线程再入箱）。
  - 置信度: 中（竞态窗口真实存在；触发频率取决于面板刷新时机）

- [AUD-2213] [Major] provider 整体无异常防护：`_resolve_output_resolution` 的 int() 与渲染异常会逃出"失败返回 None"契约
  - 位置: src/app/controllers/ai_workflow_composite_render.py:135-181、341-351
  - 证据:
    ```python
    def provider(invocation: Any, cancel_requested: Any = None) -> Any:
        ...
        output_path, modality, duration_seconds = _render_headless(
            graph_data, edit_project, timeline_sequences, config_manager,
            is_cancelled,
        )
    ...
        if params.get("format_mode") in ("custom", "project"):
            w = int(params.get("width", 1920) or 1920)
            h = int(params.get("height", 1080) or 1080)
    ```
    `provider` 函数体没有 try/except；`inputs["composite_graph"]` 来自工程/AI 可控的图数据，`width`/`height` 为非数字字符串（如 "hd"）时 `int()` 直接 ValueError。
  - 影响: 与 docstring「渲染失败 / 被取消 → 返回 None，editorial 层诚实降级」的契约不符——异常逃逸到 harness 的调用点，行为取决于外层捕获，可能中断整个 run 而非降级为 `COMPOSITE_RENDER_NO_OUTPUT`。
  - 建议: provider 主体包 try/except（log + return None）；分辨率解析对非法值回退 1920x1080。
  - 置信度: 中

- [AUD-2214] [Suggestion] `composite_renders` 缓存目录无任何清理/淘汰策略，产物无限累积
  - 位置: src/app/controllers/ai_workflow_composite_render.py:35-36、216、237
  - 证据:
    ```python
    #: composite 渲染候选的缓存目录（与 generated_media 缓存同源）。
    _RENDER_CACHE_SUBDIR = "composite_renders"
    ...
    output_path = str(out_dir / f"composite-{uuid.uuid4().hex[:10]}.mp4")
    ...
    output_path = str(out_dir / f"composite-{uuid.uuid4().hex[:10]}.png")
    ```
  - 影响: 每次渲染用 uuid 新名落盘且从不删除（仅 `discard` 的候选也不删文件）；视频为 h264 mp4，长会话/4K 下缓存目录可增长到 GB 级。
  - 建议: 生成素材箱 discard/import 时同步删除 preview 文件，或按总大小/年龄做 LRU 清理。
  - 置信度: 高

- [AUD-2215] [Suggestion] `_generation_bin` 穿透 `bridge._generation_service` 私有属性取 bin
  - 位置: src/app/controllers/ai_workflow_composite_render.py:318-321
  - 证据:
    ```python
    def _generation_bin(main_window: Any) -> Any:
        bridge = getattr(main_window, "_ai_host_bridge", None)
        service = getattr(bridge, "_generation_service", None)
        return getattr(service, "generation_bin", None)
    ```
  - 影响: 依赖 bridge 的私有字段名与 `set_generation_runtime` 是否被调过（dock 装配顺序变化即静默拿不到 bin，渲染成功但候选丢失，仅留 warning）。
  - 建议: bridge 暴露公开 `generation_bin` property，或 host 直接持有 GenerationService 引用注入 provider。
  - 置信度: 高

- [AUD-2216] [Suggestion] 显式 composite id 路径的 `to_dict()` 未包异常（与活动图路径的降级处理不一致）；`_render_single_frame` 的 `render_output` 参数未使用
  - 位置: src/app/controllers/ai_workflow_composite_render.py:64-68（对比 74-78）、243-248
  - 证据:
    ```python
    if hasattr(data, "to_dict"):
        return dict(data.to_dict())
    if isinstance(data, dict):
        return dict(data)
    return None
    ```
    而活动图路径 `getter()` 包了 `except Exception: _LOG.warning(...)`（74-78）。另 `_render_single_frame(render_output, ...)` 形参在函数体内从未引用。
  - 影响: 工程内的 composition 对象 `to_dict()` 抛异常会把 resolver 主线程调用点打崩（Run 流程异常），与相邻路径的"诚实降级"不一致；死参数误导读者以为 image 模式复用了该实例。
  - 建议: 显式 id 路径同样 try/except 返回 None；删除未用的 `render_output` 参数（调用点 233-235 同步修改）。
  - 置信度: 中

### 通过项
- 取消路径：`is_cancelled` 贯穿——provider 入口检查（149-154）、传入 `render_video`（217-223）、渲染后复查（224-225、235-236），视频长任务可中断。
- 红线遵守：候选只入 GenerationBin，`media_asset_id=""`（未导入媒体池前不可被 plan 放置）、不写时间线（166-180）。
- provenance：只记录图内容 hash（`composite_graph_hash`，303 行），不记 secret/全文，符合 §0 红线。
- 视频颜色路径正确：`render_video` 内部做 LINEAR_SCENE→输出描述符的 transform_color + 位深打包（composite_render_output.py:479-487），无需本文件干预。
- 范围校验：`end < start or fps <= 0` 显式拒绝并 warning（212-215）；duration 公式与 `resolve_video_range` 的 inclusive end 语义一致。
- 主线程/worker 职责分离（除 AUD-2211 所述活读外）：resolver 阶段才触碰 `composite_page`，worker 不触碰 Qt 部件。
- 模块无 GLSL 代码；不涉及信号连接（无防递归面）。
