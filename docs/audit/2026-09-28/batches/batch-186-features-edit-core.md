# 批次报告：batch-186 — src/features/edit/core（timeline Metal 导出渲染器）

任务书：docs/audit/2026-09-28/assignments/batch-186.md（编号区间 AUD-18600–AUD-18699）
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

交叉核实的外部依据（只读）：
- `src/features/edit/core/timeline_export.py:1604-1725`（`_cpu_defocus` 参考实现）
- `src/features/edit/core/timeline_export.py:7289-7297, 7700-7774`（后端路由与 GpuWipeElement 构造）
- `src/features/edit/core/timeline_gpu_export_renderer.py:909`（GL u16 管线注释）、`:806-820`（GpuYuvSource 字段）
- `src/shared/gpu/metal/context.py:200-261, 334-449`（dispatch 只编码、finish 才提交；write_buffer 为 CPU memcpy）

## src/features/edit/core/timeline_metal_export_renderer.py (行数: 5,412)

### 发现

- [AUD-18600] [Critical] 同帧多个转场复用同名上传缓冲，CPU 复写覆盖已编码 dispatch 的参数/掩码
  - 位置: src/features/edit/core/timeline_metal_export_renderer.py:4292
  - 证据:
    ```python
    mask_params_buffer = self._upload_array(
        "stack_wipe_mask_params", mask_params)      # :4292 每个 wipe 都写同名缓冲
    ...
    mask_buffer = self._upload_array(
        "stack_wipe_mask", np.ascontiguousarray(mask))  # :4327 CPU 掩码路径同样同名
    ...
    5: self._upload_array("stack_wipe_params", params),  # :4337 每个 wipe 再次复写
    ```
    `render_layer_stack` 把整帧所有 dispatch 编码进同一个 command buffer，仅在循环结束后 `ctx.finish()`（:4824）一次提交；而 `MetalComputeContext.dispatch` 只编码不提交（context.py:343、:366-385），`write_buffer` 是对 Shared 存储的直接 CPU memcpy（context.py:244-259）。调用方在遍历 `sorted_layers` 时可对一帧内多条转场各追加一个 `GpuWipeElement`（timeline_export.py:7750 在循环内 append）。
  - 影响: 一帧含 ≥2 个转场时，第 2+ 个转场对 `"stack_wipe_params"` / `"stack_wipe_mask_params"` / `"stack_wipe_mask"` 的 `write_buffer` 发生在整帧提交之前；GPU 执行时这些缓冲只剩最后一个转场的内容——前面的转场使用错误的 opacity/progress/transition_mode/掩码，输出画面错误（转场参数串台）。GPU 侧同名缓冲（stack_wipe_left/right 等）因单一 command buffer 内 encoder 顺序执行而安全，仅 CPU 上传路径存在该竞争。
  - 建议: 为每个 wipe 使用带索引的唯一键（如 f"stack_wipe_{wipe_index}_params"），或先把全部 wipe 参数一次性打包上传，或每个 wipe 后立即 `ctx.finish()` 前先独立提交。
  - 置信度: 高

- [AUD-18601] [Major] Metal u16 / YUV422P10 交付链路在 `to_u8` 处降为 8-bit 精度，与 GL 的 RGB16UI 全链及本文件声明不符
  - 位置: src/features/edit/core/timeline_metal_export_renderer.py:293
  - 证据:
    ```metal
    return float3(to_u8(clamp(color.r, 0.0f, 1.0f) * 255.0f),   // :293-295 每源像素量化到 8-bit 码值
                  to_u8(clamp(color.g, 0.0f, 1.0f) * 255.0f),
                  to_u8(clamp(color.b, 0.0f, 1.0f) * 255.0f));
    ```
    ```metal
    // High-depth packed delivery ... The compositor keeps its float working set on the
    // GPU ... without first reducing the signal to 8-bit.   // :1618-1622 声明
    output[pixel] = to_u16(color.b * (65535.0f / 255.0f));      // :1635 实为 8-bit 码 ×257
    ```
    `timeline_fused_export_u16`（:1623-1638）、`timeline_fused_export_texture_planar_yuv422p10`（经 `prepare_planar_texture_source_pixel` :1508 同样 `to_u8`）、`timeline_finalize_stack_u16`（:3184-3198，累加器前景已 8-bit）全部经由 8-bit 码值。GL 侧对照：timeline_gpu_export_renderer.py:909 注释 ">8-bit 源走 RGB16UI 全链，8-bit 源仅出口 u16"。
  - 影响: 高精度导出（`_use_high_precision_export` → `output_u16`，timeline_export.py:7289 对 metal 后端同样传参）在 Metal 后端静默退化为 8-bit 有效精度；10-bit 源（P010/Topos 422P10 转 RGB）同样在 RGB 域先降到 8-bit。与 GL/CPU 参考出现后端间精度分歧，且与 :1618-1622 的 float working-set 注释矛盾。
  - 建议: 在 u16/422P10 交付路径保留 float 或 u16 中间表示（至少在 `prepare_*_pixel_unfiltered` 出口按交付位深量化），或修正注释并让调用方对 >8-bit 源的高精度导出回落 GL/CPU。
  - 置信度: 高（机制）；对用户可见影响取决于路由，为中。

- [AUD-18602] [Major] `render_layer_stack` 的 GPU trail 未记录 CPU 上传字节，full-GPU 证据系统性低估 CPU 搬运
  - 位置: src/features/edit/core/timeline_metal_export_renderer.py:4825
  - 证据:
    ```python
    self._record_render_completion(          # :4825-4829 未传 cpu_upload_bytes
        ctx,
        output_size,
        yuv_output=bool(output_yuv420p or output_yuv422p10),
    )
    ```
    同文件 `_render` 路径显式记录了上传字节（:4995 `cpu_upload_bytes=(packed.nbytes + y.nbytes + u.nbytes + v.nbytes)`），而 `render_layer_stack` 每帧对 `stack_source_*` 源平面、`stack_wipe_params`、逐层 `source_params/blend_params/effect_weights` 做了大量 `_upload_array` 上传。
  - 影响: 导出证据（ExportGpuTrail）把 stack 路径记为 0 字节 CPU 上传；依赖该证据判定"全 GPU/零拷贝"的门禁或报告会得出偏乐观的错误结论，与 `_record_render_completion` 文档中"keeps the full-GPU gate fail-closed"的意图相悖。
  - 建议: 在 `render_layer_stack` 累计本帧 `_upload_array` 的 nbytes 并传入 `cpu_upload_bytes`（surface 路径 readback=False 时尤其需要）。
  - 置信度: 高

- [AUD-18603] [Suggestion] `_make_transition_mask_params` 未验证 progress/angle/softness/vertices 有限性
  - 位置: src/features/edit/core/timeline_metal_export_renderer.py:5084
  - 证据:
    ```python
    params["control"] = (
        float(getattr(spec, "progress", 0.0)),   # :5084-5088 无 isfinite 校验
        float(getattr(spec, "angle", 0.0)),
        float(getattr(spec, "softness", 0.02)),
        ...
    for index, vertex in enumerate(vertices):
        params["vertices"][0, index] = (
            float(vertex[0]), float(vertex[1]))   # :5116-5118 同样未校验
    ```
  - 影响: NaN/Inf 进入 `timeline_transition_mask` 后 `clamp(NaN,0,1)` 在 Metal 中结果未定义，掩码与合成可能出现 NaN 像素；当前 spec 由内部 `build_gpu_transition_mask_spec` 构造通常有限，属防御性缺口。
  - 建议: 对 control/vertices 统一 `np.isfinite` 校验，非法时返回 None 走 `_fallback("invalid_transition_mask")`。
  - 置信度: 中

- [AUD-18604] [Suggestion] `GpuYuvSource` 色度平面与 luma/抽样因子的一致性未校验
  - 位置: src/features/edit/core/timeline_metal_export_renderer.py:4484
  - 证据:
    ```python
    source_height, source_width = y.shape
    source_chroma_width, source_chroma_height = u.shape[1], u.shape[0]   # :4484-4485
    ...
    bit_depth = int(source.luma_bits)
    chroma_x = int(source.chroma_x_div)      # :4492-4493 未验证 u/v 与之匹配
    ```
    `_render`（:4954-4956 直接把 u.shape/v.shape 填入 chroma_dims）同样只查 ndim/dtype，不查 u.shape==v.shape 及 (w+chroma_x-1)//chroma_x 关系；纹理路径（:4451-4456）倒是做了完整校验。
  - 影响: 若上游产出不一致平面，kernel 用声明的 chroma_dims 越界钳制不会崩溃，但色度采样网格错位导致图像畸变且难以归因；不构成 OOB（读被钳制在实际数组尺寸内）。
  - 建议: 对齐纹理路径的校验：`u.shape == v.shape` 且与 `source_width/chroma_x`、`source_height/chroma_y` 一致，否则 `_fallback("invalid_planar_chroma_dimensions")`。
  - 置信度: 中

- [AUD-18605] [Suggestion] 单个小半径效果在 planar/packed 源上走逐 tap 全解码+调色的融合路径，4K 性能风险
  - 位置: src/features/edit/core/timeline_metal_export_renderer.py:4533
  - 证据:
    ```python
    materialize_effect_chain = (
        len(effect_chain) > 1
        or (len(effect_chain) == 1
            and int(round(float(effect_chain[0].get("kind", 0.0))))
            in (6, 7, 8, 9, 10, 11))
        or (len(effect_chain) == 1 and is_metal_texture)
    )
    ```
    未物化时 `timeline_stack_layer` → `sample_layer_packed_rgb` 的 4 次双线性 tap × `prepare_source_pixel`（:1132-1142）对每个源 tap 重复完整 YUV 双线性解码 + `apply_primary_grade`（half≤5 即 121 tap）。
  - 影响: 4K 帧上每输出像素约 4×121 次全解码+调色，远超实时预算；仅影响 Metal 后端下经 `_gpu_planar_source_from_decoded` 回落的 planar 源（视频层通常已是纹理源会被物化），属可达但低频的热点。
  - 建议: 将"单效果 + planar 源 + half>1"也纳入 materialize 条件，或在 fused 路径对效果 tap 先做一次性解码缓存。
  - 置信度: 中

- [AUD-18606] [Suggestion] GPU 句柄/来源大量使用 `Any` 注解；个别 TypeError 被归因为 `metal_render_error`
  - 位置: src/features/edit/core/timeline_metal_export_renderer.py:4374
  - 证据:
    ```python
    opacity = float(opacity)        # :4374 None/非法类型 → TypeError
    if not np.isfinite(opacity):    # 不会执行到
    ```
    该 TypeError 落入 :4859 的 `except Exception` 记为 "metal_render_error"，而真实原因是层元数据非法；另外 `render_*` 系列的 `source: Any`、`_MetalBufferImage.buffer: Any` 等可收敛为 Union/协议类型。
  - 影响: 归因错误的 fallback 原因误导排查；类型注解宽泛降低静态检查价值（Suggestion 级）。
  - 建议: `opacity` 转换包 try/except 归为 `invalid_layer_opacity`；对 source/buffer 引入类型别名或 Protocol。
  - 置信度: 高

### 通过项
- 线程安全：全部公开入口（render_planar / render_metal_texture_frame / render_packed_bgr / render_layer_stack / release / set_gpu_trail）经 `_serialize_renderer_call` 的 RLock 串行化，`_buffers`/计数器/trail 只在锁内变更。
- GPU 资源生命周期：缓冲按名复用且 `stack_layer_*`/`stack_source_*` 每帧剪枝防增长；`release` 先 finish 再清空且 `_released` 防止重建；编码异常时 context 丢弃半编码 command buffer，其余方法的 `finally: finish()` 显式提交部分编码工作避免共享缓冲复用竞争（已核实 dispatch 只编码、finish 才提交）。
- GLSL·HDR 正确性（内核对拍维度）：数字字面量均带 `.0/.0f`；循环边界为常量或经 host 侧闸门限制的有限半径（>32 会因 `_effect_axis_weights` 切片不匹配抛错 fail-closed）；defocus 两种模式与 `_cpu_defocus` 逐位对齐（水平 ±15 变 sigma 高斯与 mip LOD 混合均镜像核实）；双边滤波复刻 OpenCV LUT 语义；tile kernel 先 `threadgroup_barrier` 再越界提前 return；`write_buffer` 显式上传而非伪装读回。
- 信号防递归：无 Qt 信号/控件，不适用。
- 状态提交一致性：纯无状态渲染器，无 undo/redo/revision/持久化状态，不适用。
- 异常与取消路径：所有渲染入口 fail-closed 返回 None 交由 CPU 合成器回落，fallback 原因去重记录并计入 `metal_errors`（仅首次告警）；Topos 路径用 `command_submitted` 标志保证异常时仍 finish。
- 输入验证：输出尺寸/奇偶、dtype/ndim、位深、矩阵系数、blend mode 0-15、opacity 有限性、wipe 掩码形状与有限性、crop 参数、纹理平面维度等均有显式校验与具名 fallback。
- 性能模式：可分离高斯（host 预归一化权重 + half4 中间面）、defocus mipmap、denoise/bilateral 分块 threadgroup、`_effect_axis_weights` 对 OpenCV 小核逐位复刻、bokeh 偏移预计算等优化到位；`_serialize_renderer_call` 无嵌套重入风险。
