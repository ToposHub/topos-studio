# batch-062 — src/features/composite/（export_provider / facade / face_workflow / ml_roto / module）

编号区间：AUD-6200–AUD-6299。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。所有发现均基于实际读取的行号与代码；跨模块行为（export renderer 自清理、KeyframeTrack 去重、AICacheManager 写路径、deliver_controller 调用点）已只读核对相应源文件。

---

## src/features/composite/export_provider.py (行数: 161)

### 发现

- [AUD-6200] [Suggestion] render_full_res_passes 全库无调用方且缺少渲染线程路由，standalone-only 设计一旦接线可能在调用线程（含 UI 线程）创建 GL 上下文
  - 位置: src/features/composite/export_provider.py:126-147
  - 证据:
    ```python
    def render_full_res_passes(
        self,
        snapshot: GraphSnapshot,
        render_passes: Optional[tuple[str, ...] | list[str]] = None,
        interactive_scheduler: object | None = None,
    ) -> ExportResult:
        ...
        renderer = _Renderer(interactive_scheduler=interactive_scheduler)
    ```
    render_full_res（58-99 行）有 render_thread 优先路径，本方法没有 color_image/render_thread 参数，直接走 `_Renderer(...)` standalone 路径；grep 全库（src+tests）仅命中本定义处。standalone 渲染器 `_ensure_gpu_resources`（composite_export_renderer.py:459-463）在 self.ctx 为 None 时会在调用线程执行 `moderngl.create_context()`。
  - 影响: 当前为死代码。若未来从 app 层（UI 线程）接线调用，将在 UI 线程创建并操作 OpenGL 上下文，违反"UI 线程不得操作 OpenGL"铁律；与 render_full_res 的线程模型不一致。
  - 建议: 若保留，补 render_thread/color_image 路由参数与 render_full_res 对齐；若多通道交付已由其它路径承担，删除死代码。
  - 置信度: 高

- [AUD-6201] [Suggestion] 宽泛 except Exception 丢失异常类型与堆栈，仅保留 str(e)
  - 位置: src/features/composite/export_provider.py:122-124, 159-161
  - 证据:
    ```python
        except Exception as e:
            logger.error(f"Export provider render error: {e}")
            return ExportResult(error=str(e))
    ```
  - 影响: TypeError/AttributeError 等程序性错误被折叠为一条错误字符串，无 traceback（logger 未带 exc_info），线上难以定位；违反"不得宽泛 except 掩盖领域错误"精神。
  - 建议: logger.error(..., exc_info=True)；或分域捕获（GL/求值/读回）后保留原始异常链 `return ExportResult(error=f"{type(e).__name__}: {e}")`。
  - 置信度: 高

- [AUD-6202] [Suggestion] 弱类型注解：核心依赖以 object 鸭子类型标注，泛型风格混用
  - 位置: src/features/composite/export_provider.py:54, 60-64, 37
  - 证据:
    ```python
    def __init__(self, config_manager: object | None = None) -> None:
    ...
        interactive_scheduler: object | None = None,
        render_thread: object | None = None,
    ...
        passes: dict[str, np.ndarray] = field(default_factory=dict)
    ```
    同文件其余处用 `Optional[np.ndarray]`（32 行），37 行却用内建泛型 `dict[...]`，风格不一致。
  - 影响: 适配层接口契约不可静态检查，重构 render_export/CompositeExportRenderer 签名时无法获得类型工具保护。
  - 建议: 为 config_manager/render_thread 定义 Protocol（如 `HasRenderExport`），统一泛型写法。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：standalone 路径不调用 cleanup 是安全的——CompositeExportRenderer.render_and_readback / render_and_readback_passes 内部 try/finally 在非 persistent 模式自清理（composite_export_renderer.py:411/778/1042/398-401 已核对）。
- 线程安全：适配器无共享可变状态，每次调用新建 renderer。
- HDR 正确性：默认 linear_f32 读回链路未被适配层改动或 clamp；无 [0,1] 截断。
- render_thread 路径结果映射字段齐全（image/error/used_tiling/tile_count/render_time_ms）。

---

## src/features/composite/facade.py (行数: 218)

### 发现

- [AUD-6203] [Suggestion] build_overlay_objects 对 meta 字典直接下标访问，缺键/非 dict 时抛 KeyError/AttributeError；SimpleNamespace 回退假设属性齐全
  - 位置: src/features/composite/facade.py:133-150
  - 证据:
    ```python
        if fmt_data:
            fmt = ImageFormat(
                width=fmt_data["width"], height=fmt_data["height"],
                pixel_aspect=fmt_data.get("pixel_aspect", 1.0),
    ...
        else:
            # Fallback: construct simple namespace objects from the dict data
            if fmt_data:
                fmt = types.SimpleNamespace(**fmt_data)
    ```
  - 影响: meta 来自渲染器元数据信号（composite_controller._on_composite_metadata_ready），当前为内部契约；一旦上游字段改名或传入畸形 dict，覆盖层构建直接抛异常，且回退分支的 SimpleNamespace 缺 width/height 属性时下游同样 AttributeError。
  - 建议: 与其余键一致改用 .get + 类型校验（isinstance(fmt_data, dict) 且必备键存在），缺失时返回 None 并记 warning。
  - 置信度: 中

- [AUD-6204] [Suggestion] 返回类型注解不精确（裸 tuple / 裸 dict）
  - 位置: src/features/composite/facade.py:98, 114
  - 证据:
    ```python
    def get_format_classes() -> Optional[dict]:
    ...
    def build_overlay_objects(meta: dict) -> tuple:
    ```
  - 影响: 元素类型不可见，调用方与 IDE 均无契约可依。
  - 建议: `Optional[dict[str, type]]`、`tuple[Optional[Any], Optional[Any]]`。
  - 置信度: 高

### 通过项
- 线程安全：可选节点 provider 注册表用模块级 RLock 保护，get_node_providers 返回浅拷贝快照，register/unregister 幂等语义明确（73-89 行）。
- 异常路径：各 get_* 以 ImportError 降级为 None + warning，符合"特性可选"边界定位；open_teaching_animation_node_editor 的 KeyError 有意穿透（场景未注册属调用方错误），未被 except 吞掉。
- 懒加载导入全部位于函数体内，模块导入无重依赖副作用，与文件头声明一致。
- 已核对 teaching_animation 的 ModernGLEffectRegistry.register(effect_id, *, parameters) 签名与 42-44 行调用匹配。

---

## src/features/composite/face_workflow.py (行数: 144)

### 发现

- [AUD-6205] [Suggestion] "forehead" 多边形注释与索引几何不符：表格实为含下巴(152)的全脸轮廓
  - 位置: src/features/composite/face_workflow.py:37-41
  - 证据:
    ```python
        # Forehead band between brows and hairline estimate.
        "forehead": [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361,
                     288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149,
                     150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103,
                     67, 109, 10],
    ```
    468 点拓扑中 10=发际中点、152=下巴，该序列从头顶沿右侧脸颊绕到下巴再回到左侧，是完整脸椭圆而非"眉间到发际线"。
  - 影响: 纯文档误导：维护者按注释理解会误判皮肤区域构造；实际运行时该表与 cheek/nose 求并后行为正确（排除区另由 EYE/BROW/LIP 处理）。
  - 建议: 更正注释为"full face oval (hairline → right jaw → chin → left jaw)"或将表名改为 face_oval。
  - 置信度: 高

- [AUD-6206] [Suggestion] _polygon_mask 假设 landmark 为二元组，与本模块 serialize_landmarks 输出的三元组不对称
  - 位置: src/features/composite/face_workflow.py:69, 107
  - 证据:
    ```python
            if 0 <= int(index) < len(landmarks):
                x, y = landmarks[int(index)]
    ...
            payload[str(int(frame))] = [
                [float(x), float(y), float(conf)] for x, y in points]
    ```
  - 影响: 若调用方把检测器原始 3 元组（含 confidence）或序列化 JSON 数组直接当 landmarks 传入，解包抛 ValueError（`too many values to unpack`）；只有经过 deserialize_landmarks（111-133 行，正确只取前两元）才安全。
  - 建议: `x, y = point[0], point[1]` 或解包前取 `tuple(p[:2])`，容忍多余分量。
  - 置信度: 中

### 通过项
- 线程安全：纯函数、无共享状态。
- 异常路径：cv2 缺失时显式 RuntimeError（62、83 行），不裸抛 ImportError；deserialize_landmarks 对坏 JSON/坏键/坏点逐项容错，符合"外部数据须验证"。
- HDR/取值域：build_skin_mask 的 clip(0,1) 作用于 mask 语义域（非 HDR 图像数据），合理；双 mask 先模糊再相减的注释与实现一致（88-95 行）。
- 性能模式：fillPoly 批量传点、GaussianBlur 单次调用；feather 卷积核 `round(feather*2)|1` 保证奇数核。
- landmarks_at 的"就近帧回退"语义有 docstring 明示。

---

## src/features/composite/ml_roto.py (行数: 319)

### 发现

- [AUD-6207] [Major] apply_keyframes 仅动画化 pos(x/y) 通道，c1/c2 贝塞尔柄无轨道：中间帧的控制柄冻结在末帧
  - 位置: src/features/composite/ml_roto.py:283-293
  - 证据:
    ```python
        roto_node.set_parameter(
            "points", keyframe.to_points_json())
        roto_node._sync_point_parameters(keyframe.points)
        for index, point in enumerate(keyframe.points):
            names = type(roto_node)._point_param_names(index)
            values = (names["x"], point["pos"][0]), (names["y"],
                                                     point["pos"][1])
            for name, value in values:
                tracks.setdefault(name, []).append(...)
    ```
    轨道只为 x/y 建；循环内对同一参数 "points" 反复 set_parameter，静态值最终等于最后一个关键帧的路径。求值端（roto_shape_node.apply_point_parameter_overrides）只对存在动画轨道的通道用插值覆盖，c1/c2 取静态 parameters——即末帧的柄坐标（绝对坐标，非相对偏移）。
  - 影响: 生成的 roto 动画在两个关键帧之间，位置插值而柄仍锚定在最后一帧的位置，运动幅度大时中间帧形状明显畸变——该服务的核心产物（"editable ordinary roto keyframes"）质量缺陷。
  - 建议: 与 x/y 一致地为 c1_x/c1_y/c2_x/c2_y 建 4 条轨道（_point_param_names 已提供全部通道名）。
  - 置信度: 高

- [AUD-6208] [Suggestion] ml_roto 在 src/ 内零调用方，属未接线服务（仅 tests/composite/test_ml_roto.py、test_advanced_plan_audit.py 引用）
  - 位置: src/features/composite/ml_roto.py:1-46
  - 证据: `grep -rn "ml_roto\|mask_to_path\|RotoKeyframe\|PropagationResult" src --include="*.py"` 仅命中本文件；tests 下命中两个测试文件。
  - 影响: 功能未暴露给用户；AUD-6207 等缺陷因此暂无现网影响面，但接线路径上的缺陷会在接线时集中爆发。
  - 建议: 接线前先修复 AUD-6207；若功能搁置，在模块 docstring 标注状态，避免被误认为可用路径。
  - 置信度: 高

- [AUD-6209] [Suggestion] propagate 锚帧关键帧丢弃 decimated 标志，simplified_for_point_cap 恒为 False
  - 位置: src/features/composite/ml_roto.py:216-219
  - 证据:
    ```python
        previous_path = mask_to_path(anchor_mask, max_points)
        if previous_path is not None:
            result.keyframes[int(anchor_frame)] = RotoKeyframe(
                frame=int(anchor_frame), points=previous_path[0])
    ```
    mask_to_path 返回 (path, decimated)，只取 [0]。
  - 影响: 锚帧若被均匀抽稀，调用方收不到"建议拆分形状"信号，与其余帧语义不一致。
  - 建议: `points=previous_path[0]`，同时 `simplified_for_point_cap=previous_path[1]`。
  - 置信度: 高

- [AUD-6210] [Suggestion] propagate 的 stopped_reason 被反向传播覆盖、mask_to_keyframe 为 None 时静默 break；长序列传播无取消钩子
  - 位置: src/features/composite/ml_roto.py:243-261（反向），237-238/256-257（None break），224-261（整体）
  - 证据:
    ```python
    previous_mask = anchor_mask
    for index in range(anchor_index - 1, -1, -1):
        frame = ordered[index]
        mask = detector(frame, previous_mask)
        if mask is None:
            result.stopped_reason = f"no detection at frame {frame}"
    ```
    正向原因在 228/232 行写入后被反向同名赋值覆盖；`if keyframe is None: break` 不写原因。
  - 影响: 用户只看到最后一个方向（反向）的停止原因，正向停止信息丢失；中途空轮廓帧无诊断；frames 很长且 detector 是模型推理时整段同步循环不可中断。
  - 建议: 区分 stopped_reason_forward/backward（或仅在为空时写入）；None-break 补充原因；循环内接受可选 cancel_check 回调。
  - 置信度: 高

- [AUD-6211] [Suggestion] apply_keyframes 含冗余同步、死变量与不去重的轨道追加
  - 位置: src/features/composite/ml_roto.py:283-286, 294/310/319, 306-309
  - 证据:
    ```python
        roto_node.set_parameter(
            "points", keyframe.to_points_json())
        roto_node._sync_point_parameters(keyframe.points)   # set_parameter 内部已同步
    ...
        count = 0
    ...
            track["keyframes"].append(
                {"frame": int(frame), "value": float(value)})
    ...
        return len(ordered)                                   # count 从未使用
    ```
  - 影响: RotoShapeNode.set_parameter("points") 已调用 _sync_point_parameters（roto_shape_node.py:182-183），重复执行多余参数写入；count 累加后弃用；向已有轨道 append 不按帧去重，二次运行会在工程数据中留下同帧重复条目（运行时求值经 KeyframeTrack.from_dict 的 bulk 去重兜底，"后写胜出"语义恰好正确，已核对 src/shared/keyframe.py:381-398，但持久化文件膨胀且时间线 UI 可能显示重复点）。
  - 建议: 删除显式 _sync_point_parameters 与 count；append 前按 frame 替换或先清除同名轨道。
  - 置信度: 高

### 通过项
- 状态提交一致性：裸写 keyframe_tracks 后显式镜像失效（316-318 行 `_dirty=True; param_revision+=1; bump_output_revision()`），与 base_node.set_keyframe_track/remove_keyframe_track 的失效序列一致（已核对 base_node.py:604-612）。
- GPU/GL：本模块纯 numpy+cv2，无 GL 资源，符合声明。
- 输入验证：mask None / cv2 缺失 / 空轮廓均有 None 哨兵返回；mask_to_path 的容差爬升循环有 max_tolerance 上界保证终止，抽稀步长自增消除差一错误（160-167 行注释属实）。
- intersection_over_area 对 prev_area<=0 返回 0.0，避免除零。
- 序列化：to_points_json 使用 ensure_ascii=False，产物不含 AI 元数据，符合"产物即普通 roto 关键帧"设计。

---

## src/features/composite/module.py (行数: 533)

### 发现

- [AUD-6212] [Major] AI 缓存回调每次写缓存都新建 AICacheManager：每实例独立锁 + manifest 读改写竞态 + 每写重载清单
  - 位置: src/features/composite/module.py:71-76
  - 证据:
    ```python
            def _save_to_cache(cache_key: str, result) -> None:
                cache_manager = AICacheManager()
                cache_manager.put(cache_key, result)

            engine = get_inference_engine()
            engine.register_cache_fn(_save_to_cache)
    ```
    已核对：AICacheManager.__init__ 每次 makedirs + 从磁盘 load manifest（ai_cache_manager.py:30-42），put() 在实例级 threading.Lock 内读改写并整份重写 manifest.json（151-183 行）；推理 worker 为按 cache_key 起的 QThread（inference_engine.py:50），engine._save_to_cache 调用回调（980-983 行）。
  - 影响: 两个推理并发完成时，两个临时实例各自基于旧 manifest 写回，后写者覆盖先写者的条目——AI 缓存条目静默丢失（需重推理，浪费算力）；即使无并发，每次写缓存都全量加载/重写 manifest.json 是确定的磁盘 I/O 开销。
  - 建议: 在注册时创建一个长生命周期 AICacheManager（或模块级单例）供闭包复用，锁与 manifest 状态随之归一。
  - 置信度: 高

- [AUD-6213] [Major] _save_composite_handler 计算 graph_data 后直接丢弃并返回成功（假保存处理器，数据丢失陷阱）
  - 位置: src/features/composite/module.py:165-175
  - 证据:
    ```python
    def _save_composite_handler(self) -> bool:
        """Handle saving Composite data."""
        if self._page is None:
            return True

        try:
            graph_data = self._page.node_graph.to_dict()
            return True
    ```
    graph_data 无任何消费；当前 register_save_handlers（150-163 行）明确不注册它，全库无引用（已 grep）。
  - 影响: 现阶段是死代码；但以该名称与签名存在的处理器一旦被接线，将"保存成功"却什么都没落盘——用户 Composite 数据静默丢失，属明确风险。
  - 建议: 删除该方法，或补全真实持久化（将 graph_data 交给 SaveRouter/工程写入）后再暴露。
  - 置信度: 高

- [AUD-6214] [Suggestion] _runtime 未在 __init__ 初始化，hasattr 掩盖缺失；get_composite_page/get_viewer 在 runtime 无页时返回 None 而忽略 self._page
  - 位置: src/features/composite/module.py:36-42, 207, 212-216, 394-404
  - 证据:
    ```python
    def __init__(self):
        self._page: Optional[Any] = None
        self._viewer: Optional[Any] = None
        ...（无 self._runtime）
    ...
        if hasattr(self, '_runtime') and self._runtime and self._runtime.is_available():
    ...
        def get_composite_page(self) -> Optional[Any]:
            if hasattr(self, '_runtime') and self._runtime:
                return self._runtime.get_page()
            return self._page
    ```
    _runtime 仅在 create_runtime（207 行）赋值；register_pages 的工厂在 runtime 存在但 runtime.page 为 None 时会把新页写进 self._page（124-128 行），而 get_composite_page 随后仍返回 runtime.get_page()（None），新页被孤儿化。
  - 影响: 属性生命周期隐晦、依赖创建顺序；边缘路径下页实例两份或取到 None。
  - 建议: __init__ 中声明 `self._runtime: Optional[Any] = None`；get_composite_page 改为 `return (runtime.get_page() if runtime else None) or self._page`。
  - 置信度: 中

- [AUD-6215] [Suggestion] 用 ImportError 探测特性可用性会吞掉传递依赖的真实导入错误；两处宽泛 except 返回降级值掩盖领域错误
  - 位置: src/features/composite/module.py:44-49, 80-81, 106-108
  - 证据:
    ```python
        try:
            from src.features.composite.ui.composite_page import CompositePage
            self._available = True
        except ImportError as e:
            logger.warning(f"Composite module unavailable: {e}")
            self._available = False
    ...
        except Exception as e:
            logger.warning(f"Composite plugin registration failed: {e}")
            return 0
    ```
  - 影响: composite_page 依赖链上任何无关 ImportError（拼写错、缺失可选依赖）都会把整个 Composite 特性静默降级为不可用（仅 warning），难以与"特性合法缺席"区分；插件注册/AI 缓存注册失败同样只留 warning，后续行为（节点缺失、缓存不生效）无显性错误。
  - 建议: 探测时区分预期缺席（按特性标志/白名单模块名）与非预期 ImportError（re-raise 或 error 级别+诊断码）；宽泛 except 至少附 exc_info 并上报结构化诊断。
  - 置信度: 中

- [AUD-6216] [Suggestion] 类型注解缺失/参数无类型
  - 位置: src/features/composite/module.py:36, 306, 406
  - 证据:
    ```python
    def __init__(self):
    ...
        def register_preference_pages(self):
    ...
        def register_panels(self, panel_registry) -> None:
    ```
  - 影响: 与 .clinerules 强类型约束不符；panel_registry/action_registry/shortcut_manager 均为隐式协议。
  - 建议: 补 `-> None` 与参数注解（可用 Protocol 描述 registry 接口）。
  - 置信度: 高

### 通过项
- GPU/GL：本文件不直接触碰 OpenGL 资源；页面工厂仅做实例复用/创建。
- 信号防递归：无控件编程式更新路径（纯注册模块），不适用。
- 状态提交一致性：register_save_handlers 注释明确走 SaveRouter 回调避免空保存，与本批次行为一致（AUD-6213 为遗留死方法本身）。
- i18n：标题/菜单/快捷键文案统一走 I18N.tr 且带 fallback（132、236-299 行）。
- composite.delete_node 注册空快捷键的注释如实记录了 Delete 按键被窗口级 QAction 抢占的历史原因与规避决策（360-364 行）。
- PageDescriptor/PanelDescriptor 字段用法已对照 src/app/page_contract.py:18-23 与 src/shared/panels/registry.py:63-70 核实匹配。
- teaching_animation 注册的 ModernGLEffectRegistry.register 签名匹配（facade.py 侧已验证）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-6207, AUD-6212, AUD-6213 |
| Suggestion | 14 | AUD-6200–6206, AUD-6208–6211, AUD-6214–6216 |

Major 简述：
- AUD-6207（ml_roto.apply_keyframes）：roto 关键帧只动画化 pos，c1/c2 柄冻结在末帧，中间帧形状畸变。
- AUD-6212（module._save_to_cache）：每次缓存写新建 AICacheManager，实例级锁 + manifest 读改写竞态可丢缓存条目，且每写全量重载清单。
- AUD-6213（module._save_composite_handler）：计算 graph_data 后丢弃并返回成功，假保存处理器（当前未接线，属数据丢失陷阱）。
