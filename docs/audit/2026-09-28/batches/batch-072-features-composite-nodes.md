# Batch-072 审查报告：src/features/composite/nodes/（p/r 前缀节点，8 文件）

- 批次：batch-072 [P1]
- 编号区间：AUD-7200–AUD-7299
- 审查人：子智能体（全库审查 2026-09-28）
- 交叉核对的基础设施（只读）：`base_node.py`（marker 协议、缓存键策略）、`base_evaluator.py`（`_dispatch_effect`/`_resolve_output`）、`color_evaluator.py`/`transform_evaluator.py`/`filter_evaluator.py`/`channel_evaluator.py`（各 `_apply_*`）、`core/format.py`（BBox/FORMAT_PRESETS）、`core/time_remap.py`、`core/node_capabilities.py`、`render/render_scheduler.py`（ROI 前向/逆向传播）、`core/shaders.py`（`_ROTO_FRAGMENT`）。

汇总：Critical 0 / Major 5 / Suggestion 8。

---

## src/features/composite/nodes/precomp_node.py (行数: 59)

### 发现
（无）

### 通过项
- 线程安全：纯参数对象 + 无 GPU 调用，`_do_evaluate` 为纯直通，无共享可变状态。
- GPU 资源生命周期：不创建/持有纹理，直通返回输入 `TextureResult`（引用共享，无泄漏点）。
- GLSL·HDR 正确性：不触及着色器；直通保留上游全部元数据。
- 信号防递归：无 UI 信号，不适用。
- 状态提交一致性：`frame_offset/speed/hold_frame` 参数名与 `core/time_remap.py:198-207`（`remap_for_parameters("precomp", ...)`）完全匹配；`child_stack_id` 由 `layer_stack_controller.py`/`graph_preflight.py` 消费，闭环成立。
- 异常与取消路径：`_do_evaluate` 对 `None`/无效输入返回 `{}`，无裸异常。
- 输入验证：`is_valid` 检查到位。
- 性能模式：无计算负载。
- 类型注解：完整。

---

## src/features/composite/nodes/premult_node.py (行数: 58)

### 发现
（无）

### 通过项
- 线程安全：无共享可变状态，无 GPU 调用（marker 模式）。
- GPU 资源生命周期：placeholder `texture_id=-1` 交由 `_apply_premult`（color_evaluator.py:308-332）分配池纹理；直通路径返回原对象，无泄漏。
- GLSL·HDR 正确性：`_apply_premult` 输出 `alpha_premultiplied=True` 并保留 format/bbox/color_space，无 0-1 clamp；已预乘输入（`alpha_premultiplied is True`）正确直通避免双重预乘。
- 信号防递归：不适用。
- 状态提交一致性：无参数（`PARAM_DEFS=[]`），marker 键以 `_` 前缀，`_filter_marker_keys` 会剥除，不会序列化。
- 异常与取消路径：无效输入返回 `{}`。
- 输入验证：`is_valid` + `alpha_premultiplied` 检查到位。
- 性能模式：直通/单次 GPU pass。
- 类型注解：`_do_evaluate` 参数（`ctx, fb_manager, resource_pool, render_mode`）缺注解，但与基类签名一致（Suggestion 级以下，不计）。

---

## src/features/composite/nodes/ramp_node.py (行数: 237)

### 发现
- [AUD-7200] [Suggestion] ramp_type/extend_mode 未做白名单验证，且模块文档与 RAMP_TYPES 列表不一致
  - 位置: src/features/composite/nodes/ramp_node.py:6-11, 140, 143
  - 证据:
    ```python
    Supported ramp types:
      - linear: Straight gradient between two points
      - radial: Circular gradient from center outward
      - angular: Conical/angle gradient rotating around a point
      - box: Four-corner gradient with bilinear interpolation
    ```
    ```python
    RAMP_TYPES = ["linear", "radial", "angular", "box", "diamond"]
    ...
    ramp_type = self.parameters.get("ramp_type", "linear")
    ...
    extend_mode = self.parameters.get("extend_mode", "clamp")
    ```
  - 影响: 反序列化的旧/损坏工程若存有非法 `ramp_type`/`extend_mode` 字符串，节点原样下传，`_apply_ramp`（filter_evaluator.py:432-434）静默按索引 0（linear/clamp）渲染，用户看到的渐变类型与参数面板显示不一致且无告警；文档漏列 "diamond" 误导维护者。
  - 建议: 参照 relight_node.py:143-145 对 `light_type` 的做法，在 `_do_evaluate` 内做 `if ramp_type not in RAMP_TYPES: ramp_type = "linear"`（extend_mode 同理），并补齐 docstring。
  - 置信度: 高

- [AUD-7201] [Suggestion] 输出尺寸只做下限钳制，未按 ParamDef 上限（16384）钳制
  - 位置: src/features/composite/nodes/ramp_node.py:141-142
  - 证据:
    ```python
    width = max(1, int(self.parameters.get("width", 1920)))
    height = max(1, int(self.parameters.get("height", 1080)))
    ```
  - 影响: 程序化 `set_parameter("width", 100000)`（ParamDef max_val 仅约束 UI）会把超大尺寸传入 `_apply_ramp` → `acquire_texture` 分配失败或显存暴涨；`_do_evaluate` 是外部参数进入 GPU 分配前的最后一道防线。同一模式也存在于 reformat_node.py:98/245-246 与 roto_shape_node.py:263-264。
  - 建议: 统一 `max(1, min(16384, int(...)))`，或在 `set_parameter` 层按 ParamDef 边界钳制。
  - 置信度: 高（代码可见；触发需程序化写参，故列为 Suggestion）

### 通过项
- 线程安全：marker 生成无共享可变状态。
- GPU 资源生命周期：placeholder `texture_id=-1`；`_apply_ramp` 的池纹理由缓存管理器按 capability 回收。
- GLSL·HDR 正确性：HDR 色值上限 65535 无 0-1 clamp；输出标记 `color_space="linear"`、`alpha_premultiplied=False` 与 `_apply_ramp` 返回值（filter_evaluator.py:491-497）一致；五种 ramp_type 分发器均支持。
- 信号防递归：不适用。
- 状态提交一致性：所有参数经 `parameters` 字典进入 `_effect_params`，无 marker 键泄漏。
- 异常与取消路径：生成器无输入依赖，无异常路径。
- 性能模式：单 pass GPU 生成，参数读取全量拷贝到 marker（40 项）略冗余但量级可忽略。

---

## src/features/composite/nodes/reformat_node.py (行数: 299)

### 发现
- [AUD-7202] [Major] 同尺寸直通路径新建 TextureResult 丢失 revision/bit_depth/frame_metadata/AOV 等元数据
  - 位置: src/features/composite/nodes/reformat_node.py:251-263, 266-278
  - 证据:
    ```python
    if resize_mode == "no_resize" and image.width == target_w and image.height == target_h:
        new_fmt = ImageFormat(width=target_w, height=target_h, name="Reformatted")
        return {
            "result": TextureResult(
                texture_id=image.texture_id,
                width=image.width,
                height=image.height,
                format=new_fmt,
                bbox=image.bbox if self.parameters.get("preserve_bbox", False) else image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            )
        }
    ```
  - 影响: `revision` 回落为默认 0、`bit_depth` 回落为 8、`frame_metadata/depth_texture_id/aov_texture_ids/render_diagnostics` 全部丢失。base_node.py:58-61 明确要求"Preserve TextureResult metadata when passing textures through / Avoid treating texture_id alone as content identity"（池化纹理 glo 会复用），base_evaluator.py:1792 也专门注释"透传 revision，否则下游缓存键冻结"。后果：(1) 下游节点缓存键中 revision 恒 0，配合池纹理 glo 复用可命中过期缓存（显示旧画面）；(2) 10/12/16-bit HDR 视频帧直通后 bit_depth 变 8，`output_node.py:346` 一路读 `bit_depth` 用于输出，导出/显示的 HDR 降级判定失真。
  - 建议: 直通分支改用 `image.with_updates(format=new_fmt, ...)`（dataclasses.replace 语义，保全部元数据），或至少显式拷贝 `revision/bit_depth/frame_metadata/aov_texture_ids/depth_texture_id`。
  - 置信度: 高

- [AUD-7203] [Major] compute_output_bbox 以 bbox 尺寸为缩放基准且用整除，与实际渲染 _apply_reformat 以整图尺寸缩放不一致
  - 位置: src/features/composite/nodes/reformat_node.py:147-168（对照 transform_evaluator.py:318-346）
  - 证据:
    ```python
    target_w, target_h = self._get_target_dimensions(params, input_bbox.width, input_bbox.height)
    scale = self._compute_scale_factor(params, input_bbox.width, input_bbox.height)
    ...
    scaled_w = int(input_bbox.width * scale)
    scaled_h = int(input_bbox.height * scale)
    ...
    offset_x = (target_w - scaled_w) // 2
    ```
    对照渲染端（transform_evaluator.py:319,333-339）：
    ```python
    src_w, src_h = float(input_tex.width), float(input_tex.height)
    ...
    scale = min(target_w / src_w, target_h / src_h)
    scaled_w = src_w * scale
    offset_x = (target_w - scaled_w) * 0.5
    result_bbox = BBox(x=int(in_bbox.x * scale + offset_x), ...)
    ```
  - 影响: render_scheduler.py:983 用 `compute_output_bbox` 生成 `estimated_bbox` 供 ROI 前向规划；当上游 bbox 是子窗口（非全幅）时，分析端按 `target/bbox_size` 求缩放、渲染端按 `target/全图尺寸` 求缩放，两者估出的数据窗口位置/大小不同，ROI 传播错位（请求区域与实际有效区域不重合 → 边缘裁错或漏算）；即使全幅 bbox，`int()` 截断 + `//2` 与渲染端 `float` 半像素偏移也可差 1px。
  - 建议: 让 `compute_output_bbox` 接受/推导全图尺寸语义（与渲染端同一公式：scale 基于 full-frame，offset 用 `(target - scaled) * 0.5` 后取整），或在仅有 bbox 可用时明确标注近似并保持取整方式与渲染端一致。
  - 置信度: 中（数学不一致确凿；实际触发起于子窗口 bbox 链路，全幅 bbox 下仅 ±1px）

- [AUD-7204] [Major] compute_input_roi 逆映射忽略居中偏移、distort 用平均缩放，且覆盖后丢失 requires_roi_expansion 的滤波 padding
  - 位置: src/features/composite/nodes/reformat_node.py:182-216, 218-225（对照 base_node.py:1149-1150 默认实现）
  - 证据:
    ```python
    resize_mode = self._normalise_resize_mode(params.get("resize_mode", "no_resize"))
    ...
    scale = self._compute_scale_factor(params, input_w, input_h)
    inv_scale = 1.0 / scale if scale > 0 else 1.0
    corners = [...]
    inverse_corners = [(c[0] * inv_scale, c[1] * inv_scale) for c in corners]
    input_roi = BBox.from_corners(inverse_corners)
    return {"image": input_roi}
    ```
    而 `_compute_scale_factor` distort 分支（108-112 行）："use average for ROI estimation"。基类默认实现为 `output_roi.expanded(padding)`（base_node.py:1149-1150），本覆盖未叠加 `requires_roi_expansion`（cubic/lanczos=2，linear=1）。
  - 影响: (1) fit/fill/center_crop 下真实正映射是 `p_in = (p_out - offset) * inv_scale`（offset=(target-scaled)/2），此处漏减 offset，目标画布坐标与输入坐标非同源时（宽高比不同的 letterbox）请求的输入 ROI 整体偏移；(2) distort 模式 x/y 缩放不同却统一乘平均 inv_scale，两个轴向请求量一多一少（少的一轴缺像素）；(3) render_scheduler.py:1026 明示"DO NOT expand ROI here - compute_input_roi() already handles expansion"，本覆盖未加 padding，ROI 预览下 lanczos/cubic 升采样边缘缺采样足迹。三者都属于 ROI/瓦片路径的功能缺陷。
  - 建议: 逆映射实现为 `(p - offset) * inv_scale`；distort 分轴使用 `1/scale_x、1/scale_y`；返回前对 input_roi 叠加 `self.requires_roi_expansion(params)` 的 expand。
  - 置信度: 中（公式缺口确凿；用户可见度取决于 viewer ROI/瓦片渲染启用场景）

- [AUD-7205] [Suggestion] 直通分支中 preserve_bbox 条件两个分支完全相同（死条件）
  - 位置: src/features/composite/nodes/reformat_node.py:259, 274
  - 证据:
    ```python
    bbox=image.bbox if self.parameters.get("preserve_bbox", False) else image.bbox,
    ```
  - 影响: 条件表达式真假分支均为 `image.bbox`，要么是未写完的逻辑（原意应重算/清空 bbox），要么是无意义代码；后续维护者会误以为存在差异化行为。同尺寸直通下结果恰好无害，故仅 Suggestion。
  - 建议: 删除条件直接写 `bbox=image.bbox`，或补上与 `_apply_reformat`（transform_evaluator.py:315-316）一致的 preserve_bbox 分支。
  - 置信度: 高

- [AUD-7206] [Suggestion] resize_mode 缺省值在各辅助函数间不一致（fit vs no_resize）
  - 位置: src/features/composite/nodes/reformat_node.py:186（对照 104、136、248）
  - 证据:
    ```python
    # compute_input_roi:
    resize_mode = self._normalise_resize_mode(params.get("resize_mode", "no_resize"))
    # _compute_scale_factor / compute_output_bbox / _do_evaluate:
    ... params.get("resize_mode", "fit") ...
    ```
  - 影响: 当参数字典缺 `resize_mode` 键（如快照/测试构造的精简 params），ROI 逆映射走 no_resize 直通、而 bbox 前向与实际渲染按 fit 缩放，前后向假设分叉。当前 `self.parameters` 恒有该键故平时无感，属隐性一致性风险。
  - 建议: 三处统一默认值（建议 "fit"），或抽成类常量。
  - 置信度: 高

- [AUD-7207] [Suggestion] 未知 preset 名静默回落到输入尺寸，无告警
  - 位置: src/features/composite/nodes/reformat_node.py:92-96, 238-243
  - 证据:
    ```python
    preset = FORMAT_PRESETS.get(preset_name)
    if preset:
        return preset.width, preset.height
    return input_width, input_height
    ```
  - 影响: 工程携带已更名/删除的 preset 键（或拼写损坏）时，节点静默以"输入尺寸"为目标，行为从"改画布"退化为"不变"，用户与日志均无感知，排障困难。
  - 建议: 回落到默认 preset（"HD 1080"）并 `logger.warning` 一次，与 `_dispatch_effect` 对未知 effect_type 的告警策略对齐。
  - 置信度: 高

### 通过项
- 线程安全：marker/分析路径无共享可变状态。
- GPU 资源生命周期：非直通路径 placeholder `texture_id=-1` 交 `_apply_reformat` 分配池纹理；`_apply_reformat` 保留 alpha_premultiplied/color_space。
- 信号防递归：不适用。
- 状态提交一致性：`_LEGACY_RESIZE_ALIASES` 兼容旧工程序列化值；`filter/resize_mode` 在 marker 前完成 `index()` 且先验成员检查（281-282 行），无 ValueError 路径。
- 异常与取消路径：无效输入返回 `{}`；除零有 `input_width > 0` 守卫。
- 性能模式：同尺寸直通短路避免无谓 GPU pass（方向正确）。

---

## src/features/composite/nodes/relight_node.py (行数: 189)

### 发现
- [AUD-7208] [Major] color 参数未经验证即按下标取值/float()，损坏的持久化数据可令求值抛异常、节点静默输出空
  - 位置: src/features/composite/nodes/relight_node.py:148-149
  - 证据:
    ```python
    color = self.parameters.get("color", (1.0, 1.0, 1.0, 1.0))
    color_rgb = (float(color[0]), float(color[1]), float(color[2]))
    ```
  - 影响: `parameters` 来自 `from_dict` 原样覆盖（base_node.py:1243-1245），旧版本/损坏工程若存了 `"color": null`、二元组或非数值，`float(color[2])` 抛 TypeError/IndexError；求值器按节点捕获（base_evaluator.py:1034-1038 `except Exception ... cache[node_id] = {}`），下游全黑且仅一条 warning 日志。违反 .clinerules"外部数据须验证"。对照同文件 `light_type`（143-145 行）与 `intensity/ambient`（146-147 行，`max(0.0, float(...))`）均有防护，唯独 color 裸取。
  - 建议: 包一层防护：`c = color if isinstance(color, (tuple, list)) and len(color) >= 3 else (1.0, 1.0, 1.0)`，再逐分量 try/float 回落默认。
  - 置信度: 中（异常链路确凿；触发需要畸形持久化值）

### 通过项
- 线程安全：marker 生成无共享可变状态。
- GPU 资源生命周期：placeholder 交 `_apply_relight`（filter_evaluator.py:1295-1331）分配池纹理，渲染后 `finally: _unbind_texture_unit(1/2)` 释放绑定。
- GLSL·HDR 正确性：`intensity` 上限 10、`ambient` 上限 4，无 0-1 clamp，符合 HDR 管线；输出保留 `alpha_premultiplied/color_space`（光照乘子在 RGB 域，预乘语义不变）；`light_type/normal_decode` 白名单校验后下传，分发器再做枚举映射。
- 信号防递归：不适用。
- 状态提交一致性：`cache_to_disk`（81 行）由 base_evaluator.py:860-861/1022-1024 的磁盘缓存读写消费，参数闭环；隐藏的 `specular/roughness` 为 phase-2 预留槽，marker 不下传、shader 置 no-op，一致。
- 异常与取消路径：image/normal 双输入缺失各自返回 `{}`（缺 normal 不静默出未打光图像，注释明确）；position 可选路径正确。
- 输入验证：除 AUD-7208 外，`light_type`、数值钳制、`normal_flip_y` bool 化均到位。
- 性能模式：纯逐像素，`requires_roi_expansion=0` 与注释声明一致。
- 类型注解：完整（含 `_do_evaluate` 全参数注解）。

---

## src/features/composite/nodes/remove_channels_node.py (行数: 106)

### 发现
（无）

### 通过项
- 线程安全：无共享可变状态。
- GPU 资源生命周期：placeholder 交 `_apply_remove_channels`（channel_evaluator.py:102-150）分配池纹理。
- GLSL·HDR 正确性：`fill_value` 钳制 [0,1]（对"替换通道"语义合理，非 HDR 数据 clamp）；移除 alpha 时输出标记 straight（channel_evaluator.py:137-140），预乘语义处理正确。
- 信号防递归：不适用。
- 状态提交一致性：`BYPASS_MAPPING = {"result": "image"}` 与端口拓扑一致，禁用时正确直通；无移除项时直通返回原对象，元数据完整。
- 异常与取消路径：无效输入返回 `{}`；参数经 `_get_param_bool/_get_param_clamped` 类型安全读取。
- 输入验证：到位（本批次中最规范的一个）。
- 性能模式：全不勾选时零成本直通。
- 类型注解：完整。

---

## src/features/composite/nodes/retime_node.py (行数: 124)

### 发现
- [AUD-7209] [Suggestion] 模块 docstring 宣称光流插值/帧混合，实现实为直通 + 元数据（文档过度承诺）
  - 位置: src/features/composite/nodes/retime_node.py:9-10, 78-82（对照 base_evaluator.py:2163-2166）
  - 证据:
    ```python
    Uses optical flow for smooth motion interpolation when available,
    otherwise falls back to frame blending for simple transitions.
    ```
    ```python
    Short-term implementation: passthrough + timing metadata.
    ```
    分发器（base_evaluator.py:2163-2166）：
    ```python
    elif effect_type == "retime":
        # use_optical_flow is metadata for a future resampling backend;
        # the speed remap itself already applies at the source.
        return {"result": effect_input}
    ```
  - 影响: 顶部 docstring 与 `_do_evaluate` docstring、分发器实现三方不一致；用户按 docstring 预期运动插值（`use_optical_flow=True` 默认开），实际只做源端抽帧重映射，快速/慢速回放表现为跳帧。属文档性缺陷而非渲染错误（速度重映射经 core/time_remap.py:182-197 正确生效，`reverse/loop` 参数闭环）。
  - 建议: 重写模块 docstring 为"源端时间重映射 + 直通；光流插值为预留"；或明确 UI 上标注 use_optical_flow 暂不生效。
  - 置信度: 高

### 通过项
- 线程安全：无共享可变状态。
- GPU 资源生命周期：直通型（capability `owns_color_texture=False`），分发器返回原输入，无新纹理。
- GLSL·HDR 正确性：不触及像素。
- 信号防递归：不适用。
- 状态提交一致性：`speed/reverse/loop_mode/loop_in/loop_out` 参数名与 `core/time_remap.py` 的 `remap_for_parameters("retime", ...)`（182-197 行）及 `_parse_loop_range`（213-234 行）完全匹配；time_remap 侧已有 try/except 数值防护，故 `_effect_params` 原样传字符串/数值无风险。
- 异常与取消路径：无效输入返回 `{}`；marker 走标准 `_effect_input` 字段，`_dispatch_effect` 单输入闸（base_evaluator.py:2067）先于 retime 分支兜底。
- 输入验证：端口名 "input" 与 bbox/ROI 覆盖方法键一致（63-72 行）。
- 性能模式：零 GPU 成本直通，方向正确。

---

## src/features/composite/nodes/roto_shape_node.py (行数: 292)

### 发现
- [AUD-7210] [Major] set_parameter 对 roto_point_* 先提交后验证 + 静默 except，失败时 JSON 与数值通道发散，后续求值可再抛异常
  - 位置: src/features/composite/nodes/roto_shape_node.py:179-216（关键 181-183, 193-207, 212-216）
  - 证据:
    ```python
    def set_parameter(self, key: str, value: Any):
        super().set_parameter(key, value)          # 先无条件提交（含非法值）
        if key == "points":
            self._sync_point_parameters(self.parse_points(value))
        elif key.startswith("roto_point_"):
            try:
                ...
                if channel == "x":
                    changed = point["pos"][0] != float(value)
                    point["pos"][0] = float(value)
                ...
                if changed:
                    self.parameters["points"] = json.dumps(points, separators=(",", ":"))
            except (TypeError, ValueError, IndexError, KeyError):
                pass                                # 静默吞掉同步失败
    ```
  - 影响: `super().set_parameter` 已把非法值（如 None、非数值字符串）写入 `parameters` 并 bump 修订号；随后 `float(value)` 抛异常被静默吞掉，`points` JSON 不更新——两套表示发散。更糟的是发散值会在求值时二次引爆：`_do_evaluate` → `apply_point_parameter_overrides`（165-173 行）执行 `float(parameters[names[axis]])` 无任何防护，TypeError 直接在 `_do_evaluate` 内抛出，被求值器捕获后节点输出 `{}`（下游全黑、仅 warning）。同时违反 .clinerules"外部数据须验证/不得以静默 except 掩盖领域错误"。UI 正常路径传 float 不触发，但程序化写入（脚本/插件/未来表达式绑定）无防护。
  - 建议: 先验证后提交：对 roto_point_* 值先做 `float()` 与范围检查，验证失败直接拒绝（return，不调用 super），至少要把 `pass` 改为 `logger.warning` 并回滚 `parameters[key]`；`apply_point_parameter_overrides` 内的 `float()` 同样加 try 防护。
  - 置信度: 中（代码链路确凿；触发依赖非法值写入路径）

- [AUD-7211] [Suggestion] node_capabilities 的 effect_type="roto_shape" 与节点 marker "_effect_type": "roto" 不一致
  - 位置: src/features/composite/nodes/roto_shape_node.py:275（对照 node_capabilities.py:835-843）
  - 证据:
    ```python
    "_effect_type": "roto",
    ```
    ```python
    "roto_shape": NodeCapability(
        node_type="roto_shape",
        action_type="effect",
        effect_type="roto_shape",
    ```
  - 影响: 实际渲染走 marker 的 "roto"（`_dispatch_effect` base_evaluator.py:1989-1990 正确路由），但能力表登记的 `effect_type="roto_shape"` 在 `_dispatch_effect` 中是未知键（只会落入 "Unknown effect type" 告警分支）。当前 `get_effect_type()` 无调用方，属潜伏地雷：未来任何按 capability.effect_type 驱动分发/统计的代码都会拿错键。
  - 建议: 将 node_capabilities.py:838 改为 `effect_type="roto"`，与 marker/分发器三方对齐（paint 等其它条目已是这种对齐模式）。
  - 置信度: 高（不一致本身确凿；无现行运行时影响，故 Suggestion）

- [AUD-7212] [Suggestion] 动态生成的点位 ParamDef 硬钳 [0,1]，与 JSON 点位可越界的事实不一致，动画化越界点会被钳断
  - 位置: src/features/composite/nodes/roto_shape_node.py:126-137（对照 11-16 默认路径、218-252 parse_points 无范围限制）
  - 证据:
    ```python
    for name, default in values.items():
        definitions[name] = ParamDef(
            name, "float", float(default),
            min_val=0.0,
            max_val=1.0,
            ...
    ```
    而 `parse_points` 接受任意 float 坐标（243-245 行仅做 float 转换，无 clamp），`_flatten_roto_points_with_feather`（filter_evaluator.py:600-615）同样直接使用越界坐标绘制。
  - 影响: roto 形状允许（且实际需要）控制点在画布外（如旋转/出血形状）。静态渲染读 JSON 不受影响；但一旦对该点通道打关键帧，`set_keyframe_track` 会把 ParamDef 的 min/max 写入轨道（base_node.py:565-570），插值/UI 将坐标钳在 [0,1]，形状边缘跳变，与未动画状态不一致。ParamDef default 本身也可超出 max_val，定义自相矛盾。
  - 建议: min_val/max_val 放宽到与渲染一致的合法范围（如 -1.0~2.0 或 -2.0~3.0，与 transform 类参数对齐），或在 parse/sync 层统一声明越界约定。
  - 置信度: 中

### 通过项
- 线程安全：除 set_parameter 的既述问题外，无跨线程共享；求值路径为只读参数 + 局部对象。
- GPU 资源生命周期：placeholder 双端口（result/matte）交 `_apply_roto`（filter_evaluator.py:711-785）分配池纹理；`_effect_input` 为 None 时分发器正确走纯形状渲染分支（767-785 行），marker 契约（双输出、None 输入）被完整消费。
- GLSL·HDR 正确性：mode 2 着色器输出 `vec4(source.rgb * alpha, source.a * alpha)`（shaders.py:5456-5458），RGB 乘 alpha 即转预乘，节点 placeholder 的 `alpha_premultiplied=has_image` 与分发器结果（filter_evaluator.py:774 `alpha_premultiplied=True`）语义一致；无 0-1 数据 clamp（feather/opacity 的 [0,0.5]/[0,1] 钳制属参数语义边界，合理）。shader 循环上限 `MAX_POINTS` 编译期常量、带 `if (i >= u_point_count) break`，符合 .clinerules。
- 信号防递归：不适用（无 UI 信号）。
- 状态提交一致性：`set_parameter("points")` 同步镜像数值通道、roto_point 编辑仅在 `changed` 时重编码 JSON（保留旧工程序列化字节），undo/redo 走同一入口；`apply_point_parameter_overrides` 保证插值参数覆盖 JSON，静态/动画一致（除 AUD-7212 的范围问题）。
- 异常与取消路径：`parse_points` 对畸形 JSON/点结构逐点容错跳过（222-247 行），旧 list-of-pairs 数据兼容。
- 输入验证：无输入端口依赖的尺寸回退（output_width/height）+ `max(1, ...)` 下限到位。
- 性能模式：flatten 在分发器侧缓存 uniform，节点侧每帧 parse JSON 有少量开销（<16 点，可忽略）。

---

## 附注（跨文件、非本批次编号）

- 分发器侧（base_evaluator/color/transform/filter/channel_evaluator）在重建 marker 结果时普遍不搬运 `revision/bit_depth/frame_metadata/aov_*`（如 color_evaluator.py:324-332、filter_evaluator.py:1333-1341、transform_evaluator.py:350-358），与本批次节点无直接责任，但与 AUD-7202 同根；建议在分发器层统一改用 `input_tex.with_updates(...)`。
