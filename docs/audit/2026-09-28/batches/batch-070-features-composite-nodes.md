# batch-070 审查报告 — src/features/composite/nodes/

批次：batch-070 [P1]，编号区间 AUD-7000–AUD-7099。
审查方式：8 个文件全部 Read 通读；关键疑点对照下游分发端实现验证
（`evaluators/filter_evaluator.py`、`evaluators/keyer_evaluator.py`、`evaluators/base_evaluator.py`、
`core/node_engine.py`、`core/node_capabilities.py`、`core/shaders.py`、`render3d/animation.py`、
`render3d/lod_builder.py`、`nodes/blur_node.py`、`nodes/erode_dilate_node.py`、`nodes/base_node.py`）。
统计：Critical 0 / Major 4 / Suggestion 11。

---

## src/features/composite/nodes/luma_keyer_node.py (行数: 109)

### 发现
- [AUD-7000] [Suggestion] HELP_TEXT 参数名与实际参数定义不一致（文档过期）
  - 位置: src/features/composite/nodes/luma_keyer_node.py:27-34（对照 :46-52）
  - 证据:
    ```python
    HELP_TEXT = """亮度抠像。基于亮度值生成 alpha matte。
    ...
    参数：
      - low_threshold: 低阈值（低于此值完全透明）
      - high_threshold: 高阈值（高于此值完全不透明）
      - softness: 过渡软度
    ```
    而 `PARAM_DEFS` 实际定义的是 `black_point` / `white_point` / `softness` / `invert` / `output_mode`。
  - 影响: 帮助文本引用不存在的参数名（low_threshold/high_threshold），误导用户与二次开发者；`_effect_params` 与分发端（`keyer_evaluator._apply_luma_keyer`）实际使用 black_point/white_point。
  - 建议: 将 HELP_TEXT 更新为与 PARAM_DEFS 一致的参数名，或删除过期段落。
  - 置信度: 高

### 通过项
- 线程安全：纯标记（marker）模式求值，无跨线程状态。
- GPU 资源生命周期：不持有 GL 对象，占位 TextureResult(texture_id=-1) 由 `_resolve_output` 替换，符合协议。
- GLSL·HDR 正确性：参数直传分发端；shader 侧已对 black/white 反转做 `max(range, 0.0001)` 防护（shaders.py:2228-2235 已验证），无需节点侧重复校验。
- 信号防递归：无信号/控件，不适用。
- 状态提交一致性：经 `set_parameter`/`param_revision` 基类路径，无自定状态。
- 异常与取消路径：无效输入早退返回 `{}`，符合协议。
- 输入验证：`is_valid` 检查到位。
- 性能模式：无逐像素 CPU 工作。

---

## src/features/composite/nodes/lut_node.py (行数: 180)

### 发现
- [AUD-7001] [Suggestion] `_lut_mtime` 只写不读，LUT 文件内容变化不会触发任何缓存失效
  - 位置: src/features/composite/nodes/lut_node.py:64-65、124-128
  - 证据:
    ```python
    self._lut_path: str = ""
    self._lut_mtime: float = 0.0
    ...
    if value != self._lut_path:
        self._lut_path = value
        if value and os.path.exists(value):
            self._lut_mtime = os.path.getmtime(value)
        else:
            self._lut_mtime = 0.0
    ```
  - 影响: 全库 grep 确认 `_lut_mtime` 无任何读取点。注释声称"Track LUT file for cache invalidation"，但 mtime 变化（LUT 文件被外部程序覆盖）既不失效节点缓存也不通知分发端；字段是误导性死状态。
  - 建议: 要么在 `_do_evaluate` 中比对当前 mtime 并在变化时 `bump_output_revision()`/置脏，要么删除该字段与注释，避免暗示不存在的失效机制。
  - 置信度: 高
- [AUD-7002] [Suggestion] `get_lut_info`/`_parse_lut_size` 全库无调用者；整文件 `readlines()` 且以宽 except 吞掉解析错误
  - 位置: src/features/composite/nodes/lut_node.py:131-180
  - 证据:
    ```python
    def get_lut_info(self) -> Dict[str, any]:
        ...
        try:
            size = self._parse_lut_size(lut_path)
        except Exception:
            return {"loaded": False, "name": "", "size": 0}
    ...
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    ```
  - 影响: grep 确认 `get_lut_info` 无外部调用（死代码）。若未来被 UI 线程调用，`readlines()` 对 64³ .cube（约 26 万行、十几 MB）会同步整读阻塞 UI；`except Exception` 把编码错误/权限错误统一折叠为"未加载"，掩盖领域错误（违反 .clinerules 宽泛 except 约束的精神）。
  - 建议: 删除死代码，或改为只读文件头部若干行解析 `LUT_3D_SIZE`（找到即 break），并收窄异常类型记录日志。
  - 置信度: 高（无调用者与整读为事实；性能影响为条件性）
- [AUD-7003] [Suggestion] 返回类型注解 `Dict[str, any]` 使用内置函数 `any` 而非 `typing.Any`
  - 位置: src/features/composite/nodes/lut_node.py:131
  - 证据:
    ```python
    def get_lut_info(self) -> Dict[str, any]:
    ```
  - 影响: 注解对象是内置函数 `any`，类型检查器（mypy/pyright）报错；与全库 `Any` 约定不一致。
  - 建议: 改为 `Dict[str, Any]` 并从 typing 导入 Any。
  - 置信度: 高

### 通过项
- 线程安全：无共享可变状态（`_lut_path` 仅经 UI 线程 set_parameter 写、渲染线程只读 path 参数）。
- GPU 资源生命周期：无节点持有 GL 对象。
- GLSL·HDR 正确性：HDR 域映射参数（input/output_range_max 上限 4.0）直传分发端，无节点侧 clamp(0,1)。
- 信号防递归：不适用。
- 状态提交一致性：`set_parameter` 覆写后仍调用 `super().set_parameter`，param_revision 路径正确。
- 异常与取消路径：无 LUT/不支持扩展名/intensity<=0 均走无标记直通（符合协议"passthrough 不带 _effect_* 键"）。
- 输入验证：`set_parameter("lut_path")` 对 PathLike/str/其他类型做了归一化（:117-120），是本批次的正面样板。
- 性能模式：`os.path.exists` 为廉价 stat，渲染线程可接受。

---

## src/features/composite/nodes/mask_merge_node.py (行数: 141)

### 发现
- [AUD-7004] [Major] 输出 bbox/尺寸声明与分发端实际输出不一致；尺寸选择含恒真死条件
  - 位置: src/features/composite/nodes/mask_merge_node.py:70-81、115-125
  - 证据:
    ```python
    # compute_output_bbox：输出 bbox 是 union
    return bbox_a.union(bbox_b)
    ...
    # Determine output dimensions (use B's format if available, else A's)
    width = input_b.width if input_b.is_valid else input_a.width
    height = input_b.height if input_b.is_valid else input_a.height
    ```
    分发端实际实现（filter_evaluator.py:1179-1181、1208-1211，已对照验证）：
    ```python
    out_w = max(1, int(max(input_a.width or 1, input_b.width or 1)))
    out_h = max(1, int(max(input_a.height or 1, input_b.height or 1)))
    ...
    bbox=input_a.bbox or input_b.bbox,
    ```
  - 影响: 三方各说一套：(1) 调度期 `compute_output_bbox` 声明 union(A,B)；(2) 标记占位 TextureResult 尺寸恒取 B（此处 `input_b.is_valid` 经上方早退后恒为 True，else 分支为死代码，与注释"else A's"矛盾）；(3) 实际渲染纹理为 max(A,B) 尺寸、bbox 取 A 或 B。当 A/B 尺寸或数据窗口不同时，ROI 规划的输出区域可超出实际纹理/bbox 覆盖范围，ROI 渲染存在边缘缺数据或错位风险；union 也可能大于 max 尺寸（两 bbox 有偏移时）。
  - 建议: 统一契约——`compute_output_bbox` 与标记尺寸都改为与分发端一致的 max 尺寸 + 明确的 bbox 选择规则（或让分发端遵循 union）；删除恒真条件，直接 `width, height = input_b.width, input_b.height` 并修正注释。
  - 置信度: 高（三方不一致为代码事实）；实际渲染错位的影响为中（占位符最终被 `_resolve_output` 替换，但调度期 bbox 声明已被消费）

### 通过项
- 线程安全：无状态标记模式。
- GPU 资源生命周期：无节点持有 GL 对象。
- GLSL·HDR 正确性：操作语义（add/subtract/…）在 matte 域 [0,1]，分发端对 opacity 做 clamp，无 HDR 数据损坏路径。
- 信号防递归：不适用。
- 状态提交一致性：无自定状态。
- 异常与取消路径：单输入无效时直通（返回真实 TextureResult、无标记键），双无效返回 `{}`，符合协议。
- 输入验证：`is_valid` 检查完备。
- 性能模式：单 pass 双纹理合成，无问题。
- 备注：单输入直通时同一 TextureResult 同时作为 `result` 与 `matte` 输出（:103-106）——TextureResult 按不可变约定使用（`with_updates` 复制），别名共享安全，不算缺陷。

---

## src/features/composite/nodes/mask_node.py (行数: 186)

### 发现
- [AUD-7005] [Suggestion] ParamDef 默认值（"matte"）与实例默认值（"result"）不一致，属性面板"重置默认"行为与新建节点不同
  - 位置: src/features/composite/nodes/mask_node.py:83-84（对照 :116）
  - 证据:
    ```python
    ParamDef("output_mode", "option", "matte", labels=OUTPUT_MODES,
             group="Output"),
    ...
    self.parameters: Dict[str, Any] = {
        ...
        "output_mode": "result",
    ```
    属性面板重置逻辑（ui/node_properties_panel.py:1400-1402，已对照验证）：
    ```python
    if pdef and pdef.default is not None:
        self.parameters[key] = pdef.default
        self.parameter_changed.emit(self.node_id, key, pdef.default)
    ```
  - 影响: 新建节点 output_mode 默认 "result"（白 matte），用户在属性面板点击重置后变为 "matte"（alpha only），同一参数出现两套"默认"，行为不可预期且难排查。
  - 建议: 将 ParamDef default 改为 "result" 与实例初始值对齐（或反之），保持单一事实来源。
  - 置信度: 高

### 通过项
- 线程安全：无状态标记模式。
- GPU 资源生命周期：无节点持有 GL 对象。
- GLSL·HDR 正确性：参数经 `_get_param_clamped` 限幅后传分发端。
- 信号防递归：不适用。
- 状态提交一致性：无自定状态；hidden 参数 output_width/height 用 hidden=True 合理。
- 异常与取消路径：无异常路径；无输入时按生成器语义产出独立尺寸。
- 输入验证：center/size/rotation/feather/sides 全部经 clamp/限幅（:142-151），是本批次验证最完整的节点。
- 性能模式：`max(1, int(...))` 防零尺寸。
- 生成器模式下 `compute_output_bbox` 返回 None 与 ramp/constant/roto 等生成器节点惯例一致（已对照），非遗漏。

---

## src/features/composite/nodes/matte_cleanup_node.py (行数: 133)

### 发现
- [AUD-7006] [Major] blur 模式 ROI 扩张量约为实际高斯半径的一半，ROI/分块渲染边缘欠采样
  - 位置: src/features/composite/nodes/matte_cleanup_node.py:89-90
  - 证据:
    ```python
    if operation == "blur":
        return int(params.get("blur_amount", 2.0)) // 2 + 1
    ```
    分发端把 blur_amount 原值直接作为高斯半径（keyer_evaluator.py:479-481，已对照验证）：
    ```python
    result = self._apply_blur(input_tex, {
        "blur_type": "gaussian",
        "radius_x": max(0.0, float(params.get("blur_amount", 2.0))),
        "radius_y": max(0.0, float(params.get("blur_amount", 2.0))),
    ```
    而同项目 BlurNode 对相同语义的 padding 约定是完整半径（blur_node.py:275-276，已对照验证）：
    ```python
    radius_x, radius_y = self._get_effective_radius(params)
    return int(max(radius_x, radius_y))
    ```
  - 影响: blur_amount=50 时 padding 仅 26 像素（应 50）。ROI 渲染/平铺时高斯核在请求窗口边缘采到未提供的区域，产生边缘发暗/错误过渡；与 BlurNode 的既有约定不一致，属明确渲染风险。
  - 建议: 改为 `return int(max(0.0, float(params.get("blur_amount", 2.0))))`，与 BlurNode._tile_padding 对齐；erode/dilate 分支 `(size // 2 + 1) * iterations` 语义正确无需改（分发端 iterations 固定 1）。
  - 置信度: 高（半径映射与两处 padding 约定均已验证）

### 通过项
- 线程安全：无状态标记模式。
- GPU 资源生命周期：无节点持有 GL 对象。
- GLSL·HDR 正确性：levels 分发端对 black/white/gamma 有限幅（keyer_evaluator.py:533-536 已验证）。
- 信号防递归：不适用。
- 状态提交一致性：无自定状态。
- 异常与取消路径：无效输入早退。
- 输入验证：`is_valid` 检查到位。
- 性能模式：erode/dilate 的 ROI 扩张与 ErodeDilateNode 约定一致。

---

## src/features/composite/nodes/matte_node.py (行数: 139)

### 发现
- [AUD-7007] [Suggestion] 与 LumaKeyerNode 功能重复且参数命名分叉
  - 位置: src/features/composite/nodes/matte_node.py:49-60（对照 luma_keyer_node.py:46-52）
  - 证据:
    ```python
    # matte_node: low_threshold / high_threshold / low_softness / high_softness / invert / output_mode
    ParamDef("low_threshold", "float", 0.0, ...)
    ParamDef("high_threshold", "float", 1.0, ...)
    # luma_keyer_node: black_point / white_point / softness / invert / output_mode
    ParamDef("black_point", "float", 0.0, ...)
    ParamDef("white_point", "float", 1.0, ...)
    ```
  - 影响: 两个节点都是"亮度阈值 → matte"（各自走 `_apply_matte` 与 `_apply_luma_keyer` 两个近似 shader），参数名与软度模型（单 softness vs 低/高双 softness）分叉，维护时容易只改一处；用户面对两个近义节点（Matte / Luma Keyer）语义不清。
  - 建议: 收敛为一个节点（或让其一成为另一的参数预设别名），统一参数命名与 shader 实现。
  - 置信度: 高（重复为事实；收敛方案属产品决策）

### 通过项
- 线程安全：无状态标记模式。
- GPU 资源生命周期：无节点持有 GL 对象。
- GLSL·HDR 正确性：shader 侧对阈值反转/软度越界有 `max(range, 0.0001)` 与 `clamp(soft, 0.0, 0.5)` 防护（shaders.py:2274-2302 已验证），节点侧注释"validated in shader"属实。
- 信号防递归：不适用。
- 状态提交一致性：无自定状态。
- 异常与取消路径：无效输入早退。
- 输入验证：`is_valid` 检查到位。
- 性能模式：无问题。

---

## src/features/composite/nodes/model3d_node.py (行数: 1162)

### 发现
- [AUD-7008] [Major] 导入失败后无失败粘滞/退避：损坏资产会在每次求值时重新提交完整导入
  - 位置: src/features/composite/nodes/model3d_node.py:394-408、458-461
  - 证据:
    ```python
    if not result.succeeded:
        self._model_error = result.error or (...)
        self._model_cache_status = (...)
        ...
        return None            # 失败后句柄已置 None（:385-386）
    ...
    if root is None:
        try:
            self._model_import_request = request
            self._model_import_handle = get_shared_model_import_service().submit(request)
    ```
  - 影响: 文件存在（通过 :447 isfile 检查）但内容损坏/后端解析失败时，`_model_root` 保持 None → 缓存命中分支永不成立 → 下一帧求值再次 submit 新请求，完整重解析（FBX 可能触发 assimp CLI 转换）。预览循环下反复排队失败导入：CPU 持续空转、日志刷屏、共享 worker 被无效任务占用。文件缺失路径（:447-457）有结构化粘滞错误，内容损坏路径没有对应处理，不对称。
  - 建议: 失败结果按 (path, mtime, request) 记忆粘滞错误状态（同键直接返回错误、不再 submit），或对失败重试做时间退避；下次路径/mtime 变化时清除。
  - 置信度: 中高（节点侧控制流已逐行验证；共享服务内部无跨 submit 的失败缓存证据，但不排除上游有部分去重）
- [AUD-7009] [Major] LOD 变体一次性同步构建 3 级，其中 LOD3 在本节点不可达，首次导航帧在渲染线程白白多做一次全网格简化
  - 位置: src/features/composite/nodes/model3d_node.py:776-786（对照 :698-705、:112-114）
  - 证据:
    ```python
    # 生成 LOD1/LOD2（LOD0=原 mesh，不存）   ← 注释只说两级
    variants = {
        1: build_lod(base_mesh, target_ratio=lod_ratio_for_level(1)),
        2: build_lod(base_mesh, target_ratio=lod_ratio_for_level(2)),
        3: build_lod(base_mesh, target_ratio=lod_ratio_for_level(3)),
    }
    ```
    `_resolve_lod_level` 的全部返回路径只有 0/1/2（:698-745），`viewport_lod_mode` 也只暴露 force_lod0/1/2（:112-114）。
  - 影响: 首次进入导航态时，对每个可简化 mesh 同步执行 3 次顶点聚类简化（LOD3=10% 比例的一次完整计算永远无人消费），全部发生在渲染线程的交互关键帧上；大模型（≥500k 三角）导航首帧卡顿被放大约 50%。注释与代码也不一致（说两级、建三级）。
  - 建议: 只按需构建当前 `lod_level` 的变体（或预建 1/2 两级），删除 level 3 分支；若确需 LOD3，先在 `_resolve_lod_level` 与参数枚举中让它可达。
  - 置信度: 高（不可达已验证；卡顿幅度为估算）
- [AUD-7010] [Major] `_do_evaluate` 对约 20 个参数直接 `float()`/`int()` 强转，非数值入参抛异常后节点静默输出空 payload
  - 位置: src/features/composite/nodes/model3d_node.py:827、843、878-881、968-974、1025-1026
  - 证据:
    ```python
    unit_scale = max(0.0001, float(self.parameters.get("unit_scale", 1.0)))
    ...
    anim_speed = float(self.parameters.get("anim_speed", 1.0))
    ...
    anim_clip = int(self.parameters.get("anim_clip", 0))
    ```
    参数来源含外部工程文件：`Node.from_dict` 将磁盘 parameters 原样灌入且不做类型校验（base_node.py:1243-1245）：
    ```python
    loaded_params = data.get("parameters", {})
    for key, val in loaded_params.items():
        node.parameters[key] = val
    ```
    求值异常在上层仅被吞为 warning（node_engine.py:1338-1347 已对照验证）：
    ```python
    except Exception as e:
        logger.warning(f"Error evaluating node {node_id} ...")
        self._evaluation_cache[node_id] = {}
    ```
  - 影响: 手工编辑/损坏/版本错位的 .tcomp 中任一数值参数为字符串时，`float()` 抛 ValueError → 模型静默消失、只有一条 warning 日志，无面向用户的错误定位；违反 .clinerules"外部数据须验证"。同类数值参数未用基类现成的 `_get_param_clamped`/`_get_param_int` 防御。
  - 建议: 对所有数值参数统一走 `_get_param_clamped`/`_get_param_int`（带 ParamDef 边界），或在 from_dict 后做一次按 ParamDef 的类型校正；至少对强转包 try/except 并写入 `diagnostics`。
  - 置信度: 中（异常路径与静默吞错已验证；正常 UI 流不易触发，故不定 Critical）
- [AUD-7011] [Suggestion] `_resolve_lod_level` 中 interactive_lod 设置检查重复，第二处为不可达死代码
  - 位置: src/features/composite/nodes/model3d_node.py:713-717、731-734
  - 证据:
    ```python
    gs = getattr(eval_ctx, "global_3d_scene_settings", None) or {}
    if not bool(gs.get("interactive_lod", True)):
        self._current_lod_level = 0
        return 0
    if eval_ctx is None or not _is_nav:
        ... return ...
    # 到这里必然 _is_nav 且 interactive_lod 已确认开启
    gs = getattr(eval_ctx, "global_3d_scene_settings", None) or {}
    if not bool(gs.get("interactive_lod", True)):
        return 0
    ```
  - 影响: 第二次检查永不为真（同参数同结果），死代码误导阅读，疑似合并冲突残留。
  - 建议: 删除 :731-734 的重复块。
  - 置信度: 高
- [AUD-7012] [Suggestion] `__init__` 中 `import time as _time_mod` 未使用
  - 位置: src/features/composite/nodes/model3d_node.py:233-234
  - 证据:
    ```python
    import time as _time_mod
    self._lod_last_nav_time: float = 0.0
    self._lod_hold_ms: float = 200.0
    ```
  - 影响: `_time_mod` 全文件无引用（实际计时在 `_resolve_lod_level` 内另行 `import time as _time`），死导入。
  - 建议: 删除该行。
  - 置信度: 高
- [AUD-7013] [Suggestion] `cleanup()` 未清空 `_mesh_lod_variants` 与部分失败状态字段
  - 位置: src/features/composite/nodes/model3d_node.py:1133-1162
  - 证据:
    ```python
    def cleanup(self):
        ...
        self._model_path_cached = None
        self._morph_weights_sig = ()
        self._animation_sampler_cache.clear()
        self._model_flip_uv_v_cached = False
        self._model_cache_status = None
        ...
    ```
    （`self._mesh_lod_variants`、`self._model_error`、`self._model_mtime_cached`、`self._current_lod_level` 均未重置；`_ensure_model` 路径变化分支 :427-433 有清 `_mesh_lod_variants`，cleanup 没有。）
  - 影响: 节点对象若在 cleanup 后仍被引用（undo 栈/剪贴板/诊断面板常见），LOD 的三份 MeshData numpy 顶点数组继续驻留内存；大模型可达数十 MB。
  - 建议: cleanup 中补 `self._mesh_lod_variants = {}`、`self._model_error = None`、`self._model_mtime_cached = 0.0`。
  - 置信度: 高
- [AUD-7014] [Suggestion] 关闭 `use_imported_model` 或清空路径后，已加载模型的 GL 资源（VAO/VBO/程序）不释放
  - 位置: src/features/composite/nodes/model3d_node.py:892-926
  - 证据:
    ```python
    # 无路径/未启用：emit 空 payload（Scene3D 退化为 procedural）
    if not use_imported_model or not model_path:
        self._emit_scene_payload(ctx, "display", ModelPayload(has_model=False, ...))
        return {}
    ```
    `_release_flat_meshes_gl` 只在 `_ensure_model` 的路径/选项变化分支（:427）与 `cleanup`（:1141）调用。
  - 影响: 用户关闭导入开关（或断开使用）后，`_flat_meshes_gl` 的 VAO/池化 VBO 与三个 mesh program 继续占用 VRAM，直到节点删除或路径变化；多模型场景反复开关会积累显存占用。
  - 建议: 在该早退分支检测"先前 has_model 且现在未启用"时调用 `_release_flat_meshes_gl(self._last_gl_ctx)` 并清相关缓存。
  - 置信度: 高（代码路径明确；实际占用规模取决于模型）
- [AUD-7015] [Suggestion] disabled 直通将图像 TextureResult 映射到 scene 类型的 display 端口，语义错误
  - 位置: src/features/composite/nodes/model3d_node.py:134-145（结合 base_node.py:977-980 默认旁路）
  - 证据:
    ```python
    self.inputs = [
        Port("base_color_in", "input", "image"),
        ...
    ]
    self.outputs = [
        Port("display", "output", "scene"),
    ]
    ```
    基类默认旁路（base_node.py:977-980）：
    ```python
    first_input = next(iter(inputs.values()), None)
    if first_input and first_input.is_valid:
        return {self.outputs[0].name: first_input}
    ```
  - 影响: 节点被禁用且 base_color_in 等图像端口有连接时，求值缓存中 `display`（scene 端口）出现 TextureResult。当前 scene 数据走 `ctx.scene_payloads` 旁路、该纹理不会被消费（无实际崩溃），但类型契约被破坏，未来任何按端口类型取输出的代码都会踩坑；同时禁用后 `_emit_scene_payload` 不再执行，Scene3D 静默丢模型（旁路预期的退化行为，可接受但无 BYPASS_MAPPING 显式声明）。
  - 建议: 设置 `BYPASS_MAPPING: Dict[str, str] = {}`（显式声明"禁用=无输出"），避免默认首入首出映射误用。
  - 置信度: 高（映射行为已验证；当前无实际消费者故仅 Suggestion）

### 通过项
- 线程安全：CPU 解析走共享 worker，`poll()` 非阻塞，句柄切换合作式取消（:368-424）；GL 对象仅在渲染线程创建（`_ensure_flat_meshes_gl` 注释与实现一致）；动画采样缓存只存纯 numpy 本地矩阵（:221-225 注释与实现一致）。
- GPU 资源生命周期：VAO 释放 + MeshVBOPool 引用计数归还路径正确（:610-623）；程序在 cleanup 统一 release（:1156-1162）；VBO 经池共享，morph 重建时先 release 再重取（:540-543）。与 scene3d_node 的程序缓存模式（不追踪 ctx 切换）一致，属全库既有约定，未单列。
- GLSL·HDR 正确性：本节点不直接写 GLSL；skinned/tangent/plain 三种 VAO layout 与对应 program 的匹配有注释防护（F5）。
- 信号防递归：无 Qt 信号/控件，不适用。
- 状态提交一致性：模型缓存键 path+mtime+normalize+flip_uv_v（:351-355）；payload 签名覆盖 transform/morph/纹理 override（:1046-1056）；`requires_frame_number=True` 已在 node_capabilities 注册（node_capabilities.py:244-249 已验证），frame_number 直读不会导致帧缓存陈旧。
- 异常与取消路径：导入取消（IMPORT_CANCELLED）→ 结构化 cancelled 状态；mesh 上传单 mesh 失败隔离并带 exc_info 日志（:604-607）；文件缺失 → `asset.missing` 结构化诊断（:447-457）。
- 输入验证：unit_scale/mesh_scale/mscale/shadow_softness/matte_id 等有下限钳制（:827、874-882、977）；`_should_flip_uv_v`/`_axis_correction_matrix` 对 mode 字符串容错。
- 性能模式：in_memory 快路径零重解析（:356-365）；LOD 去闪烁保持期（P5-5）与导航 LOD 决策分层合理（除 AUD-7009/7011 所述问题外）。

---

## src/features/composite/nodes/node_state.py (行数: 22)

### 发现
（无）

### 通过项
- 通过（无逻辑）：纯 Enum 定义（DIRTY/PROCESSING/CACHED/FAILED/BYPASSED/PLACEHOLDER），被 base_node.py 及各节点引用，无行为代码。
