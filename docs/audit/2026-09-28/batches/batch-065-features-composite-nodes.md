# batch-065 审查报告 — src/features/composite/nodes/（8 文件，1,996 行）

编号区间：AUD-6500–AUD-6599（实际使用 AUD-6500–AUD-6513）
统计：Critical 1 / Major 1 / Suggestion 12
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
跨文件核对：marker 协议逐节点与其消费方（base_evaluator/color_evaluator/channel_evaluator/filter_evaluator/transform_evaluator）及 core/shaders.py 的 uniform 声明做了比对；`cleanup()`/`set_color_result()` 的调用线程已确认（均在求值线程）。

---

## src/features/composite/nodes/color_correct_node.py (行数: 165)

### 发现

- [AUD-6500] [Critical] color_correct 效果参数集与 GPU 分发器/着色器契约不匹配，任一参数改动即导致节点输出为空
  - 位置: src/features/composite/nodes/color_correct_node.py:140
  - 证据（节点发送的参数，行 140-164）:
    ```python
    "_effect_params": {
        "exposure": self.parameters.get("exposure", 0.0),
        "contrast": self.parameters.get("contrast", 1.0),
        ...
        "channel_selector": channel_selector,
        "mix": self.parameters.get("mix", 1.0),
    },
    ```
    消费方 base_evaluator.py:2115-2116 将其路由到 `_apply_color_correct`，而 color_evaluator.py:153-156 只读取：
    ```python
    prog['u_saturation'].value = params.get("saturation", 1.0)
    prog['u_contrast'].value = params.get("contrast", 1.0)
    prog['u_brightness'].value = params.get("brightness", 0.0)
    prog['u_mix'].value = params.get("mix", 1.0)
    ```
    着色器 core/shaders.py:1811-1825 声明的 uniform 是 u_exposure/u_contrast/u_saturation/u_hue_shift/u_temperature/u_tint/u_shadows_lifting/u_midtones_gamma/u_highlights_gain/u_channel_selector/u_mix —— 不存在 u_brightness。
  - 影响: 只要任一参数偏离默认值（has_changes 为真，行 115-121），节点进入效果路径；分发器查找着色器中不存在的 `u_brightness` uniform 触发 KeyError，被 base_evaluator.py:1034-1039 捕获后 `cache[node_id] = {}` → 节点输出为空、下游内容消失（渲染错误）。即便绕过 KeyError，exposure/hue_shift/temperature/tint/shadows/midtones/highlights/channel_selector 也全部被丢弃，与节点宣称的 DAVINCI 式专业校正功能完全不符。现有测试（tests/composite/test_nodes/test_color_nodes.py:122-147）只测参数赋值，未覆盖 node→dispatcher 贯通路径，故缺陷未被拦截。
  - 建议: 根因在分发器（color_evaluator.py，非本批次文件）：`_apply_color_correct` 应按 `_COLOR_CORRECT_FRAGMENT` 的 uniform 全集从 `_effect_params` 取值（含 vec3/vec4 数组），删除 u_brightness 赋值（或给着色器补该 uniform）。节点侧可保持现状，但应补一条贯通 node→dispatcher 的回归测试。
  - 置信度: 高

- [AUD-6501] [Suggestion] 评估时未按 PARAM_DEFS 边界对参数做 clamp/类型验证
  - 位置: src/features/composite/nodes/color_correct_node.py:141
  - 证据:
    ```python
    "exposure": self.parameters.get("exposure", 0.0),
    "contrast": self.parameters.get("contrast", 1.0),
    "saturation": self.parameters.get("saturation", 1.0),
    ```
    PARAM_DEFS 声明 exposure∈[-4,4]、midtones_gamma_*≥0.01、highlights_gain≥0（行 36-55），但 `_do_evaluate` 直接取值转发；同目录 CompareNode 全部走 `_get_param_clamped`，本节点未用。
  - 影响: from_dict 装载外部工程参数不做范围校验，手改/损坏文件可把越界值（如 exposure=1e9）送入着色器路径；shader 内 midtones_gamma 有 `max(gamma, 0.001)` 兜底（shaders.py:1893）但 exposure 等无兜底，可产生 inf/NaN 像素。
  - 建议: 与 CompareNode 一致使用 `_get_param_clamped` / 数值化 helper。
  - 置信度: 高

### 通过项
- 线程安全：节点无共享可变 GPU 状态，求值只读 parameters。
- GPU 资源生命周期：返回 effect marker（texture_id=-1），不持有 GL 资源。
- 信号防递归：无信号/UI 代码。
- 状态提交一致性：参数经基类 set_parameter（revision/dirty 正确）；`has_changes` 用 PARAM_DEFS 动态取默认值（行 113-118），与定义同源。
- Marker 协议：`_effect_type`/`_effect_input`/`_effect_mask`（可选）/`_effect_params` 符合 base_node.py:9-35 约定。
- 输入验证：image 缺失/无效时返回 {}（行 107-109）。

---

## src/features/composite/nodes/color_node.py (行数: 116)

### 发现

- [AUD-6502] [Suggestion] to_dict 覆盖为无操作，属误导性死代码
  - 位置: src/features/composite/nodes/color_node.py:113-116
  - 证据:
    ```python
    def to_dict(self) -> dict:
        data = super().to_dict()
        # Don't serialize texture ID - it's runtime state
        return data
    ```
  - 影响: 基类 to_dict（base_node.py:1185-1191）本就不序列化纹理 ID（COLORNode 也无纹理参数）；此覆盖无任何行为差异，仅暗示“基类会序列化纹理 ID”的过时假设，增加维护噪音。
  - 建议: 删除该覆盖，注释移入类 docstring。
  - 置信度: 高

### 通过项
- 线程安全：set_color_result 仅由求值线程的 _sync_render_nodes 调用（base_evaluator.py:342-357），与 _do_evaluate 同线程，无竞态。
- GPU 资源生命周期：节点不拥有 Color 页纹理（仅透传 glo/revision）；运行态纹理 ID 不入序列化。
- 输入验证：texture_id None/≤0 与尺寸 ≤0 均返回空（行 61-66）。
- GLSL·HDR 正确性：ColorSpace.SRGB 标签 + alpha_premultiplied=False 与 IDT/ODT 单次往返的色彩管理设计一致（行 84-109 注释与代码相符）。
- 异常与取消路径：无外部 IO，无吞异常。
- 状态提交一致性：纹理更新带变更检测并置 _dirty（行 47-56）。

---

## src/features/composite/nodes/compare_node.py (行数: 133)

### 发现

- [AUD-6503] [Suggestion] 单输入透传分支可返回未校验 is_valid 的 TextureResult
  - 位置: src/features/composite/nodes/compare_node.py:105-106
  - 证据:
    ```python
    # Pass through single input
    if input_a is None or not input_a.is_valid:
        return {"result": input_b} if input_b else {}
    if input_b is None or not input_b.is_valid:
        return {"result": input_a}
    ```
  - 影响: input_a 无效而 input_b 存在但无效（texture_id≤0）时，把无效 TextureResult 下发给下游；与分发器 `_apply_compare` 的守卫（filter_evaluator.py:1101-1102 `if input_b and input_b.is_valid else {}`）行为不一致。
  - 建议: 透传前统一校验 `input_b and input_b.is_valid`。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源生命周期：纯 marker 输出，无 GL 状态。
- Marker 协议：顶层 `_input_a`/`_input_b` 键与 _apply_compare（filter_evaluator.py:1099-1100）匹配。
- 输入验证：split/wipe/angle/scale 全部经 `_get_param_clamped`/`_get_param_bool` 钳制（行 94-98），分发端二次钳制（filter_evaluator.py:1128-1136）。
- 几何契约：compute_output_bbox 并集 + 单输入回退正确（行 68-79）；ROI 直通两输入（行 81-85）。
- 状态提交一致性：参数经基类 set_parameter；BYPASS_MAPPING 指向 A（行 39）禁用语义正确。

---

## src/features/composite/nodes/constant_node.py (行数: 680)

### 发现

- [AUD-6504] [Major] width/height 只钳制下限，未按 ParamDef 上限截断，可触发超大 GPU 纹理分配
  - 位置: src/features/composite/nodes/constant_node.py:568-569
  - 证据:
    ```python
    w = max(1, int(self.parameters.get("width", 1920)))
    h = max(1, int(self.parameters.get("height", 1080)))
    ```
    PARAM_DEFS 声明 width/height min_val=1、max_val=16384（行 47-48）；from_dict 装载参数不校验；行 509 `ctx.texture((w, h), 4, dtype='f4')` 与行 642-645 直接按 w/h 分配 GPU 纹理。
  - 影响: 手改/损坏工程文件写入超大尺寸（如 100000×100000 RGBA32F ≈ 160 GB）时，求值线程直接向 GL 请求超大纹理 → 分配异常或驱动级错误（base_evaluator.py:1034 虽兜底为空输出，但 GL 分配失败可能伴随上下文状态恶化）。违反“外部数据须验证”铁律。
  - 建议: `min(16384, max(1, int(...)))`（上限引用 ParamDef.max_val 而非硬编码），from_dict 装载时同步归一化。
  - 置信度: 高

- [AUD-6505] [Suggestion] legacy sRGB 迁移在已含原子 color/color2 的文件上会被 _sync_color_channel_mirrors 撤销
  - 位置: src/features/composite/nodes/constant_node.py:199-210
  - 证据（normalize_color_parameters 只转换通道镜像、保留原子色）:
    ```python
    for key in ("color_r", "color_g", "color_b",
                "color2_r", "color2_g", "color2_b"):
        ...
        normalized[key] = srgb_to_linear(value) if 0.0 <= value <= 1.0 else value
    normalized[CONSTANT_COLOR_SPACE_PARAM] = "linear"
    if not has_color:
        normalized["color"] = (...)
    ```
    from_dict 行 247 随后调用 `node._sync_color_channel_mirrors()`，该方法（行 264-272）用原子 "color" 值反向覆盖通道镜像。
  - 影响: 若 legacy 文件同时带 CONSTANT_COLOR_SPACE_PARAM≠linear 与原子 "color"（sRGB 域），通道先被转 linear、随后又被原子值（sRGB）覆盖 → 迁移静默失效、颜色偏亮。旧版正式工程通常只存通道参数，可达性低，但中间版本/手改文件可命中。
  - 建议: normalize 中对原子 color/color2 一并 srgb→linear，或迁移后无条件从通道重建原子值。
  - 置信度: 中（逻辑路径确定；触发条件少见）

- [AUD-6506] [Suggestion] _content_hash() 为死代码，与实际使用的 content_sig 并存两套口径
  - 位置: src/features/composite/nodes/constant_node.py:405-423
  - 证据: 全仓 grep `._content_hash(` 仅命中 text_node.py:1162、particle_node.py:728（各自调用自身同名方法）；ConstantNode 的定义无任何调用点——本类 `_do_evaluate` 实际使用内联 content_sig（行 600-608）。
  - 影响: 维护成本与误导（两套“内容签名”并存，且本套已无人消费）。
  - 建议: 删除，或统一由 content_sig 调用它以免口径漂移。
  - 置信度: 高

- [AUD-6507] [Suggestion] _render_gradient_gpu 更改全局 GL 状态（FBO 绑定/viewport）后不恢复
  - 位置: src/features/composite/nodes/constant_node.py:514-517
  - 证据:
    ```python
    self._render_fbo = ctx.framebuffer(color_attachments=[self._gpu_texture])
    self._render_fbo.use()
    ctx.viewport = (0, 0, w, h)
    ctx.clear(0.0, 0.0, 0.0, 0.0)
    ```
  - 影响: 函数返回后既未解绑 FBO 也未恢复 viewport；当前依赖每个后续 dispatcher 自行 `out_fbo.use()` + viewport 重设（现状确实如此），属脆弱的隐式约定，任何假设默认帧缓冲的新路径会读到脏状态。
  - 建议: 渲染完毕恢复默认帧缓冲/viewport，或在使用处集中管理 GL 状态。
  - 置信度: 中（当前无实证故障，属状态卫生问题）

### 通过项
- GPU 资源生命周期（主链路）：纹理重建前释放旧纹理并同步释放绑定它的 _render_fbo（行 611-619）；program/VBO 一次编译复用、cleanup() 全量释放（行 667-680）；`_cache_invalidated_needed` 失效标记与求值器消费闭环（base_evaluator.py:989-990）；cleanup 在求值线程调用（base_evaluator.py:336、composite_renderer.py:1275），不违反 UI 线程 GL 铁律。
- 线程安全：渲染实例参数由求值线程 set_parameter 同步，原子 color 写入与通道镜像派生保持一致（行 274-297）。
- 状态提交一致性：content_sig 替代 param_revision 防同步竞态（行 160-164 注释与实现一致）；每次重建 revision 自增（行 650）。
- GLSL·HDR 正确性：颜色参数无 clamp（上限 65535 支持 HDR）；LINEAR 标签与 Output 节点显示变换职责划分一致（行 94-98、572-574）；checker_size 下限 2 防除零（行 589）。
- 性能模式：gradient/radial 走 GPU uniform 热路径、solid/checker 走 numpy 一次性上传，签名缓存避免每帧重建（行 456-467、600-610）。
- 输入验证（色彩）：_color_value 对畸形 RGBA 容错回退（行 250-262）；hex 解析失败返回文档化的默认黑（行 379-401）。

---

## src/features/composite/nodes/copy_channel_node.py (行数: 96)

### 发现

- [AUD-6508] [Suggestion] HELP_TEXT 参数名与实际参数名不一致
  - 位置: src/features/composite/nodes/copy_channel_node.py:30-31
  - 证据:
    ```python
    参数：
      - source_channel: 源通道（r/g/b/a）
      - dest_channel: 目标通道（r/g/b/a）
    ```
    实际参数为 `src_channel`/`dst_channel`（PARAM_DEFS 行 47-50、evaluate 行 93-94）。
  - 影响: 帮助文本与真实参数名脱节，误导用户与二次开发（例如按文档写表达式/脚本会引用不存在的参数）。
  - 建议: 更新 HELP_TEXT 为 src_channel/dst_channel。
  - 置信度: 高

### 通过项
- Marker 协议：`_effect_params["input_b"]` 与分发器消费匹配（channel_evaluator.py:64 `params.get("input_b")`）；src/dst 通道索引带 CHANNELS 成员校验与兜底（行 93-94），与 u_src_channel/u_dst_channel 消费一致（channel_evaluator.py:80-81）。
- 输入验证：A 无效返回 {}、B 无效透传 A（行 71-74）；同纹理同通道恒等短路正确（行 78-79，glo 相同即逐字节同源，拷贝必为恒等）。
- 线程安全 / GPU 生命周期：纯 marker，无 GL 状态。
- 状态提交一致性：BYPASS_MAPPING {"image": "A"} 禁用时回退主输入，语义正确（base_node.py:948-984）。

---

## src/features/composite/nodes/cornerpin_node.py (行数: 343)

### 发现

- [AUD-6509] [Suggestion] _compute_inverse_homography 与 _apply_homography_point 无生产调用（死代码）
  - 位置: src/features/composite/nodes/cornerpin_node.py:129-132
  - 证据:
    ```python
    @staticmethod
    def _compute_inverse_homography(src_points: List[Tuple[float, float]],
                                    dst_points: List[Tuple[float, float]]) -> Tuple[float, ...]:
        """Compute inverse homography (dst → src) for ROI propagation."""
        return CornerPinNode._compute_homography(dst_points, src_points)
    ```
    全仓 grep：`_compute_inverse_homography` 无任何调用点（compute_input_roi 注释行 193-196 明确旧模型已废弃）；`_apply_homography_point`（行 321-343）仅被 tests/composite/test_nodes/test_upgraded_nodes.py:33,159 引用。
  - 影响: 死代码/仅测试代码残留，暗示 ROI 反向映射仍在使用，误导后续维护。
  - 建议: 删除 `_compute_inverse_homography`；`_apply_homography_point` 若仅测试使用则注明或移入测试工具。
  - 置信度: 高

### 通过项
- 数学正确性：DLT 8×8 方程构建逐项核对与标准单应方程一致（行 110-116）；Gauss 消元带部分主元 + 奇异阈值回退 identity（行 285-318、122-124）；退化点列安全。
- Marker 协议：homography 8 元组与 u_h00..u_h21 消费匹配（transform_evaluator.py:261-272）；interpolation 索引带成员校验兜底（行 256-257）。
- 几何契约：固定画布 bbox 策略与 compute_input_roi 的 ROI+padding 模型自洽（行 168-202）；tile padding bilinear=1/bicubic=2 合理。
- GLSL·HDR：纯矩阵运算、无 clamp，HDR 安全。
- 输入验证：image 无效返回 {}；identity 短路（容差 0.001，行 160-166）。
- 线程安全 / GPU 生命周期：纯 marker，无 GL 状态。
- 类型注解：完整。

---

## src/features/composite/nodes/crop_node.py (行数: 175)

### 发现

- [AUD-6510] [Suggestion] 非重采样路径 crop 区与输入不相交时回退到未裁剪 bbox，宣称不存在的数据窗口
  - 位置: src/features/composite/nodes/crop_node.py:171
  - 证据:
    ```python
    new_bbox = BBox(x=crop_x, y=crop_y, width=crop_w, height=crop_h)
    in_bbox = image.effective_bbox()
    clipped_bbox = new_bbox.intersection(in_bbox)
    ...
    bbox=clipped_bbox if not clipped_bbox.is_empty else new_bbox,
    ```
  - 影响: crop 区完全落在输入数据窗口之外时，输出 TextureResult 的 bbox 采用整个 crop 区，声称该区域有数据；实际纹理内容为空/未定义，下游按 bbox 做布局或 ROI 决策会出错。
  - 建议: 交集为空时返回空 bbox（width/height=0）或直接返回 {}（无有效数据）。
  - 置信度: 高（行为为代码事实；下游具体后果未逐一追踪，描述已保守）

- [AUD-6511] [Suggestion] crop 尺寸未按 ParamDef 上限截断，越界值直通 GPU 分配
  - 位置: src/features/composite/nodes/crop_node.py:126-128
  - 证据:
    ```python
    crop_w = max(1, int(self.parameters.get("width", 512)))
    crop_h = max(1, int(self.parameters.get("height", 512)))
    ```
    PARAM_DEFS max_val=16384（行 45-46）；crop_w/crop_h 进入 marker 后由分发器 `acquire_texture(crop_w, crop_h)` 分配（transform_evaluator.py:368-371，同样只 max(1,...)）。
  - 影响: 与 AUD-6504 同类外部数据验证缺口：手改工程可触发超大纹理分配（实际分配发生在分发器侧，故较 Constant 降级）。
  - 建议: 节点或分发器侧统一按 16384 上限截断。
  - 置信度: 高

### 通过项
- Marker 协议：crop_x/y/w/h/outside 索引与 _apply_crop 消费完全匹配（transform_evaluator.py:366-381）；outside 值带成员校验与兜底索引 1（行 134-137）。
- 几何契约：compute_output_bbox 的 reformat=True/False 双语义与 _do_evaluate 两条路径一致（行 76-99 vs 131-175）；compute_input_roi 无交集时返回空 BBox（行 109-117）。
- 状态提交一致性：legacy 参数名 affect_format/outside_policy 回退读取（行 92、129、134-135）。
- HDR：passthrough 路径完整保留 color_space/alpha_premultiplied 元数据（行 172-173）。
- 输入验证：x/y 下限 0、w/h 下限 1。
- 线程安全 / GPU 生命周期：纯 marker，无 GL 状态。

---

## src/features/composite/nodes/cryptomatte_select_node.py (行数: 288)

### 发现

- [AUD-6512] [Suggestion] remove_selection 守卫条件把 id 值当作下标比较（值/索引混淆）
  - 位置: src/features/composite/nodes/cryptomatte_select_node.py:146-150
  - 证据:
    ```python
    keep = [i for i in ids if abs(float(object_id) - i) >= 1e-9]
    keep_names = [
        names[ids.index(i)] if i < len(ids) or ids.index(i) < len(names)
        else ""
        for i in keep
    ]
    ```
    `i` 是被保留的 id **值**（float），`i < len(ids)` 却拿它与选择数量比较。
  - 影响: 正常状态下 names 与 ids 等长，两分支恰好都取 `names[ids.index(i)]` 而侥幸正确；一旦 selected_names 与 selected_ids 长度失同步（旧工程只存 ids / 手改 JSON），且命中 float_r 模式下的小数值 id（如 2.0 < len(ids)），条件为真而 `ids.index(i) >= len(names)` → IndexError。语义错误埋雷。
  - 建议: 条件改为 `ids.index(i) < len(names)`，或先补齐 names 再按枚举下标取值。
  - 置信度: 高（逻辑缺陷确定；触发需状态失同步）

- [AUD-6513] [Suggestion] 选择数达 MAX_SELECTED=32 后静默丢弃新选择，无任何反馈通道
  - 位置: src/features/composite/nodes/cryptomatte_select_node.py:133-134
  - 证据:
    ```python
    if len(ids) >= MAX_SELECTED:
        return
    ```
    （add_selection 静默 return；toggle_selection 行 153-160 随后无条件 `return True`，假装添加成功。）
  - 影响: 第 33 个物体拾取静默失败且 UI 无法感知（toggle 返回值失真）；着色器 uniform 数组容量即 32（filter_evaluator.py:1362-1366 `[:32]` + `np.zeros(32)`），超限选择本就无效。
  - 建议: add_selection 达上限返回 False 或抛领域异常供 UI 提示；toggle_selection 透传真实结果。
  - 置信度: 高

### 通过项
- Marker 协议：id_encoding/output_mode/selected_ids 与 _apply_cryptomatte_select 消费匹配（filter_evaluator.py:1357-1362）；MAX_SELECTED=32 与 uniform float[32] 容量一致；编码/模式白名单校验兜底（行 260-265）。
- 输入验证：三处 JSON 解析均 try/except (TypeError, ValueError) 回退空（行 100-120）；hex_id 解析失败归入 offline（行 198-201）。
- 线程安全 / GPU 生命周期：纯 marker，无 GL 状态。
- 状态提交一致性：选择集与 manifest 作为隐藏参数持久化（PARAM_DEFS 行 71-75）；set_manifest 后按名重解析并报告 offline_names（行 165-208）。
- 导入契约：`CryptomatteManifest.manifest` 属性存在（render3d/cryptomatte.py:121-122）。
- 类型注解：完整。
