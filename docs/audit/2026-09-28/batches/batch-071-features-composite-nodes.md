# batch-071 审查报告 — src/features/composite/nodes/（noise/noop/normalize/output/paint/particle/placeholder/pointcloud3d）

编号区间：AUD-7100–AUD-7199。审查日期：2026-09-28。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
交叉验证文件（只读）：`src/features/composite/nodes/base_node.py`、`src/features/composite/evaluators/base_evaluator.py`、`src/features/composite/evaluators/filter_evaluator.py`、`src/features/composite/evaluators/shader_library.py`、`src/features/composite/core/shaders.py`、`src/features/composite/core/node_capabilities.py`、`src/features/composite/render/gpu_resource_pool.py`。

---

## src/features/composite/nodes/noise_node.py (行数: 127)

### 发现

- [AUD-7101] [Major] `animate`/`animation_speed` 参数完全无效：`_effect_params` 从不提供 `time`，下游 `u_time` 恒为 0.0
  - 位置: src/features/composite/nodes/noise_node.py:112-126（佐证：src/features/composite/evaluators/filter_evaluator.py:392、src/features/composite/core/shaders.py:4659）
  - 证据:
    ```python
    "_effect_type": "noise",
    "_effect_params": {
        "noise_type": noise_type,
        ...
        "animate": animate,
        "animation_speed": animation_speed,
        "width": width,
    },
    ```
    消费端 `filter_evaluator.py:392`：`prog['u_time'].value = params.get("time", 0.0)`；着色器 `shaders.py:4659`：`coord += vec2(u_time * u_animation_speed * 0.1, u_time * u_animation_speed * 0.05);`。全库 grep 确认无任何位置向 noise 的 effect_params 注入 `"time"`。
  - 影响: 用户勾选 animate 后噪声纹理永远静止在第 0 帧——功能静默失效。次级问题：本节点未覆写 `params_require_frame_cache`，且 `node_capabilities.py` 的 "noise" 条目未设 `requires_frame_number=True`，因此即使补上 time，缓存键也不含 frame_number（`base_evaluator._node_needs_frame_cache` 返回 False），播放期间会复用首帧的缓存结果。
  - 建议: `_do_evaluate` 中读取 `ctx.frame_number` 写入 `_effect_params["time"]`；同时覆写 `params_require_frame_cache`（`params.get("animate")` 为 True 时返回 True）或在 capability 表为 noise 设置 `requires_frame_number`（按 animate 参数动态判断更精确）。
  - 置信度: 高

- [AUD-7102] [Suggestion] 未使用的导入 `Any`；`noise_type`/`output_channels` 在本层不验证合法性
  - 位置: src/features/composite/nodes/noise_node.py:17
  - 证据:
    ```python
    from typing import Dict, Optional, Any
    ```
    （`Any` 在全文件无使用；`noise_type`/`output_channels` 依赖 `_apply_noise` 内的归一化兜底。）
  - 影响: 死导入与轻微重复校验；手改工程文件中的非法取值依赖下游静默回落，无早期提示。
  - 建议: 删除 `Any`；可选地在节点层校验 `NOISE_TYPES`/`OUTPUT_CHANNELS` 成员资格。
  - 置信度: 高（事实），影响低

### 通过项
- GPU 资源生命周期：节点本身不持有 GL 对象，仅返回 effect marker（texture_id=-1），实际分配由 `_apply_noise` 经 resource_pool 走 TEMP 纹理（capability owns_color_texture=True，缓存条目接管释放）。
- GLSL·HDR 正确性：参数经 `max/min` 钳制（scale≥1、octaves 1–8、w/h≥1），预览模式降八度数且缓存键含 render_mode，不越界。
- 信号防递归：文件不涉及 Qt 控件/信号。
- 状态提交一致性：只读 parameters，无状态突变；不破坏 undo/revision。
- 异常与取消路径：无吞异常的宽泛 except。
- 性能模式：marker 协议本身零 GPU 工作，合理。
- 类型注解：签名与基类一致。

---

## src/features/composite/nodes/noop_node.py (行数: 67)

### 发现
（无）

### 通过项
- 通过（无逻辑）：纯透传节点。`_do_evaluate` 仅在输入有效时原样返回 `{"image": image_input}`，否则空 dict——符合 base_node 的无 marker 透传契约；`compute_output_bbox`/`compute_input_roi` 直通正确；参数仅 `label` 字符串，无状态、无 GL、无信号。`if __name__ != "__main__"` 的 BBox 条件导入仅影响直接脚本运行，注解为字符串形式不会触发 NameError。

---

## src/features/composite/nodes/normalize_node.py (行数: 160)

### 发现

- [AUD-7103] [Suggestion] `channels` 字符串未验证，任意非法字符串生成全零 channel_mask，归一化静默变 no-op
  - 位置: src/features/composite/nodes/normalize_node.py:131-136
  - 证据:
    ```python
    channel_mask = [
        1.0 if "r" in channels else 0.0,
        1.0 if "g" in channels else 0.0,
        1.0 if "b" in channels else 0.0,
        1.0 if "a" in channels else 0.0,
    ]
    ```
  - 影响: 手改工程文件（外部数据）将 channels 写成 "xyz"/"1" 之类时掩码全 0，percentile 直方图与逐通道重映射静默失效，无任何错误提示。
  - 建议: 解析后过滤出 rgba 合法字符子集，结果为空时回落 "rgb"（或记录 warning）。
  - 置信度: 高（行为确定），影响低

### 通过项
- 线程安全：纯参数读取 + marker 返回，无可变共享状态。
- GPU 资源生命周期：marker 协议（texture_id=-1 + `_effect_input`），实际 GPU 工作由 evaluator 完成；恒等变换早退直接透传上游纹理，不产生新纹理。
- GLSL·HDR 正确性：identity 判定（range 模式 + 区间相等 + 不 clamp）逻辑正确；percentile 参数交给 HistogramManager，钳制由 ParamDef 与下游负责。
- 信号防递归：不涉及 Qt。
- 状态提交一致性：无状态突变。
- 异常与取消路径：无 except；无效输入早退返回 {}。
- 性能模式：identity 早退避免无谓 GPU pass，好模式。
- 类型注解：完整。

---

## src/features/composite/nodes/output_node.py (行数: 797)

### 发现

- [AUD-7104] [Suggestion] `_parse_bg_color` 接受非有限浮点（"nan" 等），NaN 经 min/max 钳制后变成 1.0，背景静默变为不透明白色
  - 位置: src/features/composite/nodes/output_node.py:106-117
  - 证据:
    ```python
    try:
        parts = [float(v) for v in str(raw).split(",")]
        ...
        return tuple(max(0.0, min(1.0, v)) for v in parts[:4])
    except (ValueError, TypeError):
        return (0.0, 0.0, 0.0, 0.0)
    ```
    实测（项目 venv，Python 3.12）：`max(0.0, min(1.0, float('nan')))` == 1.0，即 `"nan,nan,nan,1"` 解析为 (1.0, 1.0, 1.0, 1.0)。
  - 影响: 用户在 background_color 输入非法但可解析的字符串时，全画面被不透明白色背景替换，与直觉（回落透明）相反。
  - 建议: 解析后用 `math.isfinite(v)` 过滤，非有限值整体回落 (0,0,0,0)。
  - 置信度: 高（行为已实测）

- [AUD-7105] [Suggestion] 全部显示 pass 固定使用默认 RGBA16F 临时纹理（不随 render_mode 分层）；`_apply_color_space` 使用已弃用的 `acquire_texture` 别名且在着色器编译之前获取纹理
  - 位置: src/features/composite/nodes/output_node.py:554-566（acquire 在 555，program 编译在 562-566）；其余 pass 的默认 f2 获取：367-370、433-436、489-492、613-616
  - 证据:
    ```python
    w, h = src.width, src.height
    out_tex = resource_pool.acquire_texture(w, h) \
        if resource_pool is not None else fb_manager.get_temp_texture(w, h)
    out_fbo = fb_manager.get_fbo(out_tex)
    ...
    if self._cs_prog is None:
        self._cs_prog = ctx.program(
    ```
    `gpu_resource_pool.py:832-838` 标注 `acquire_texture` 为 "Deprecated: Use acquire_temp_texture()"；`acquire_temp_texture` 默认 `dtype='f2'`（RGBA16F），存在 `acquire_tiered_texture`（preview=f2 / export=f4）但未被使用。
  - 影响: (1) 高精度/export 路径经 Output 的 resize/bg/alpha/cs bake 一律降为半精度中转；(2) `_apply_color_space` 若着色器编译抛异常，已获取的临时纹理停留在 `_active_textures` 且永不归还池（其余 pass 都是先编译后获取，无此问题）。
  - 建议: 统一先编译/校验 program 再取纹理；按 render_mode 走 `acquire_tiered_texture`；替换弃用别名。
  - 置信度: 中（分层影响取决于导出路径是否经过 Output 显示纹理；泄漏仅在编译失败时触发）

- [AUD-7106] [Suggestion] `_resize` 仅双线性 passthrough 采样，无 mipmap/预过滤，大幅缩小（>2×）时混叠
  - 位置: src/features/composite/nodes/output_node.py:687-696
  - 证据:
    ```python
    void main() {
        frag_color = texture(u_texture, v_uv);
    }
    ```
    （注释自述 "Minimal resize shaders (passthrough with UV scaling)"。）
  - 影响: 自定义输出分辨率远小于源（如 4K→SD）时输出闪烁/摩尔纹，属质量缺陷而非错误。
  - 建议: 缩小比 >2 时生成 mipmap 或多次两分步降采样。
  - 置信度: 高（着色器可见），严重度低（注释已自认 minimal）

### 通过项
- 线程安全：无 Qt 交互；缓存的 program 对象仅渲染线程访问（与 filter_evaluator 的 utility shader 模式一致）。
- GPU 资源生命周期：`_release_intermediate_texture` 对中间纹理按 pool/legacy 两路正确归还（含输入直通守卫）；每个 pass 的 vao/vbo 均 release；`bind_texture_unit(0,0)` 复位纹理单元；`cleanup()` 释放全部缓存 program，且 `composite_renderer.py:1275`/`base_evaluator.py:336` 确认在渲染线程 teardown 调用。
- GLSL·HDR 正确性：linear→sRGB OETF 分段公式正确且对负值以 `max(c, vec3(0.0))` 防护；premultiply 先除后乘保持一致性；Porter-Duff over 正确；alpha discard 保留 HDR 直通 RGB 不钳制；所有字面量均带 `.0`，版本声明 410 core 一致。
- 信号防递归：不涉及。
- 状态提交一致性：`_result_like` 在尺寸变化时正确失效 depth/AOV/诊断元数据；color_space/alpha_premultiplied 元数据在 Step 5 收敛正确（含 legacy "display" 归一化）。
- 异常与取消路径：`_parse_bg_color` 具名捕获 (ValueError, TypeError)；各 pass 早退条件完整。
- 输入验证：format_mode 分支默认回落 "input"；宽高 `max(1, ...)` 钳制。
- 性能模式：program 全部缓存复用；bg/alpha/cs pass 有无需执行的早退判定。

---

## src/features/composite/nodes/paint_node.py (行数: 250)

### 发现
（无）

### 通过项
- 线程安全：纯参数/JSON 解析，无可变共享状态。
- GPU 资源生命周期：marker 协议（result/mask 均 texture_id=-1），CPU raster 与纹理分配由 `_apply_paint`（filter_evaluator）负责，该 handler 对读回失败有防黑底保护（已核对其 857-905 行契约注释）。
- GLSL·HDR 正确性：brush_color RGB 上限 65535 允许 HDR 直色；mask 输出按约定写入 RGB+alpha。
- 信号防递归：不涉及 Qt。
- 状态提交一致性：笔划列表单参数（JSON）序列化，`set_strokes` 走 `set_parameter` → 单条 undo、param_revision/port revision 正确递增；`make_stroke` 生成稳定 uuid 供 undo 引用；`frame_range` 解析/序列化对称（MVP 不生效已在 docstring 声明边界）。
- 异常与取消路径：`parse_strokes` 全字段具名异常捕获 + 逐字段钳制（点钳 [0,1]、size [1e-4,0.5]、feather/opacity [0,1]、rgba 下限 0），坏条目丢弃不抛——恶意/手改工程文件无法破坏求值。
- 输入验证：上面对异常项的覆盖即验证项。
- 性能模式：无输入且无笔划时 marker 仍保持两端口契约（mask 全零也存在），避免下游静默丢边。
- 类型注解：完整。

---

## src/features/composite/nodes/particle_node.py (行数: 760)

### 发现

- [AUD-7107] [Major] 湍流力场为逐粒子 Python 循环，复杂度 O((frame−t0)×N×2)，与 docstring "fully vectorized in numpy" 不符；可达参数下单个求值数十秒
  - 位置: src/features/composite/nodes/particle_node.py:433-452（列表推导在 441-446）
  - 证据:
    ```python
    for f in range(t0, frame):
        newly = (birth_vals <= f) & ~activated
        ...
        if turb > 0.0:
            turb_seed = seed * 7919 + f
            nx = np.array([_noise01(turb_seed, pos[k, 0] * turb_scale,
                                    pos[k, 1] * turb_scale) - 0.5
                           for k in range(len(idx))])
            ny = np.array([_noise01(turb_seed + 101, pos[k, 0] * turb_scale,
                                    pos[k, 1] * turb_scale) - 0.5
                           for k in range(len(idx))])
    ```
    循环迭代数 ≈ `life_max = lifetime×(1+var)`（UI 上限 lifetime=2000、variance=1 → 最多 4000 帧），每帧对全部存活粒子（cap 上限 5000）各调 2 次 `_noise01`（每次 4 次 `_hash01` 纯 Python 整数运算）。极端参数下单次求值 ≈ 2×10⁷–4×10⁷ 次 Python 函数调用。
  - 影响: lifetime/rate 调大或长序列 scrub 时单帧评估秒级到数十秒，UI 冻结，直接违背 4K 实时基准；P2-11 GPU splat（亚毫秒）的收益被 CPU 模拟抵消。默认参数（rate=4、lifetime=40，窗口约 50 帧 ×160 粒子）尚可接受。
  - 建议: 将 `_noise01` 批量化（对整批粒子坐标一次 numpy 网格插值），或将湍流场预采样为固定网格后双线性查表；保持闭式/确定性不变。
  - 置信度: 高（代码可见、量级可推算；实际卡顿程度依赖参数，标中-高影响）

- [AUD-7108] [Suggestion] CPU 回退路径 `_rasterize` 每次分配全画布 `np.mgrid`（int64×2）+ float32 画布，4K 下峰值约 400MB
  - 位置: src/features/composite/nodes/particle_node.py:460、473
  - 证据:
    ```python
    canvas = np.zeros((h, w, 4), dtype=np.float32)
    ...
    ys, xs = np.mgrid[0:h, 0:w]
    ```
    3840×2160 时 canvas ≈132MB，mgrid 两个 int64 数组合计 ≈132MB，且每次调用重新分配。
  - 影响: GPU splat 失败回退到 CPU 时内存尖峰与 GC 压力；hidden 参数上限 16384² 时直接 MemoryError。
  - 建议: mgrid 改 float32 或按粒子包围盒局部生成；canvas 按尺寸缓存复用（sig 未变时已跳过重算，主要影响回退场景）。
  - 置信度: 高

- [AUD-7109] [Suggestion] GPU 失败仅记录一次告警，`_gpu_fail_logged` 永久置位后所有后续异常静默回退 CPU
  - 位置: src/features/composite/nodes/particle_node.py:734-741
  - 证据:
    ```python
    except Exception as exc:
        rendered = False
        if not self._gpu_fail_logged:
            self._gpu_fail_logged = True
            logger.warning(
                "Particle GPU splat failed (%s); "
                "falling back to CPU raster", exc)
    ```
  - 影响: 回退本身正确（确定性保持），但之后出现的不同失败原因（上下文丢失、驱动错误）永久无日志，排障困难；宽泛 `except Exception` 属于有意回退，可接受但日志被抑制过度。
  - 建议: 按异常文本/上下文指纹去重，或周期性（如每 N 次失败）重记日志；在 `cleanup`/上下文重建时重置标志。
  - 置信度: 高（事实），影响低

### 通过项
- 线程安全：GL 对象仅在渲染线程触达；`_update_current_frame` 在解包 CompositeEvalContext 之前锁存帧号（避免 bare ctx 无 frame_number 冻结为 0，注释解释了 TextNode 同源缺陷）。
- GPU 资源生命周期：自管纹理模式实现完整——`_ensure_gpu_targets` 按上下文身份/尺寸重建，上下文更换时 `release=False` 只丢引用（避免对死上下文调用 release）；`_release_gpu_objects` 释放后置 `_cache_invalidated_needed`，`base_evaluator.py:989-1002` 确认会连带失效下游缓存（防 stale glo segfault）；`cleanup()` 释放全部对象（composite_renderer.py:1275 调用链已核实）。R16F 累积目标规避 EXT_float_blend 缺失的注释与实现一致。
- GLSL·HDR 正确性：GLSL 3.30 全部字面量带 `.0`；软点 σ=r/2、支撑域 ceil(r·3)+1 与 CPU 路径一致；半像素偏移对齐 CPU 栅格；alpha 钳 [0,1] 属覆盖度语义，颜色不钳（HDR 保留至 65535）；norm pass 结束解除 BLEND 并复位纹理单元。
- 信号防递归：不涉及 Qt。
- 状态提交一致性：`params_require_frame_cache` 恒 True 正确（粒子场是播放头纯函数）；`_content_hash` 覆盖全部参数 + 帧号；`_revision` 随重栅格化递增并写入 TextureResult。
- 异常与取消路径：GPU→CPU 回退链完整（见 AUD-7109 的日志粒度问题）；`_cpu_raster_upload` 先释放 FBO 引用的旧纹理再重建，顺序正确。
- 输入验证：rate/lifetime/life_var/cap 等均 max/min 钳制；region/shape 字符串回落默认分支。
- 性能模式：主路径 GPU instanced splat + 签名缓存跳过重算，设计良好（性能问题仅 AUD-7107 的湍流模拟与 CPU 回退）。
- 类型注解：完整。

---

## src/features/composite/nodes/placeholder_node.py (行数: 28)

### 发现
（无）

### 通过项
- 通过（无逻辑）：占位基类仅设 `IS_PLACEHOLDER = True`、`_state = NodeState.PLACEHOLDER`，`_do_evaluate` 返回空 dict（符合文档契约"不产出"）；参数定义/序列化由基类处理。（可顺带清理未使用的 `Port/TextureResult/ParamDef` 导入，不构成缺陷。）

---

## src/features/composite/nodes/pointcloud3d_node.py (行数: 181)

### 发现

- [AUD-7110] [Critical] 每次求值直接 `gl.texture(...)` 新建纹理，节点不持有引用、不经 resource_pool、缓存不接管释放 → GPU 纹理泄漏
  - 位置: src/features/composite/nodes/pointcloud3d_node.py:169-178
  - 证据:
    ```python
    try:
        gl = getattr(ctx, "gl", ctx)
        data = np.ascontiguousarray(np.flipud(frame))
        tex = gl.texture((width, height), 4, data=data.tobytes(),
                         dtype="f4")
        return {"result": TextureResult(
            texture_id=tex.glo, width=width, height=height,
    ```
    节点实例无任何纹理字段（对照 particle_node 的 `self._gpu_texture` / text_node 同模式），无 `cleanup()` 覆写；`node_capabilities.py:906-913` 中 `"pointcloud3d"` 为 `owns_color_texture=False`（自管预期），`base_evaluator.py:1279` 据此使 `_collect_owned_texture_ids` 返回空集 → CacheEntry 驱逐时不释放；纹理也不在 `resource_pool._active_textures`（未走 acquire），`release_texture_by_id` 的 legacy 回退查 `fb_manager._active_textures` 同样找不到。局部变量 `tex` 在求值返回后即失引用。
  - 影响: 任何触发重求值的变化（参数修改；`point_size`/`point_color`/`opacity` 均为 `animated=True`，播放动画时每帧一次）都分配一张新的 w×h RGBA32F 纹理且无人释放——默认 640×480 约 4.9MB/次，参数上限 8192×8192 约 1GB/次；keyframed 播放以 60fps 泄漏约 300MB/s（默认分辨率），VRAM 耗尽导致渲染失败/崩溃。次级缺陷：返回的 TextureResult `revision` 恒为默认 0，也不带 format/bbox，与同类源节点（particle）的元数据契约不一致。
  - 建议: 采用 particle/text 的自管模式——实例持有 `self._gpu_texture`，内容/尺寸签名变化时先 `release()` 再重建，并置 `_cache_invalidated_needed`；或改走 `resource_pool.acquire_*` + capability `owns_color_texture=True`。返回结果写入递增 revision 与 format/bbox。
  - 置信度: 高（持有/释放/缓存接管三条路径均已核实为空；注：同 pattern 也存在于本批次之外的 volume3d_node/deflicker_node，建议一并排查）

- [AUD-7111] [Suggestion] 上传失败仅 `logger.warning`，未设置 `error_message`（其前一行刚被清空），UI 无错误状态
  - 位置: src/features/composite/nodes/pointcloud3d_node.py:179-181
  - 证据:
    ```python
    except Exception as exc:
        logger.warning("Point cloud upload failed: %s", exc)
        return {}
    ```
    （147 行 `self.error_message = ""` 在此之前已执行。）
  - 影响: 数据加载成功但纹理上传失败时，节点静默无输出且 UI 不显示任何错误（与 143-146 行加载失败设置 `error_message` 的处理不一致）。
  - 建议: 上传失败分支同样设置 `self.error_message = f"Point Cloud upload: {exc}"`。
  - 置信度: 高

### 通过项
- 线程安全：无可变共享状态（除 error_message 单字符串）。
- GLSL·HDR 正确性：CPU raster + f4 上传，flipud 与合成管线一致；opacity 只乘点 alpha 一次（避免空像素平方），注释正确。
- 信号防递归：不涉及 Qt。
- 状态提交一致性：`set_track_data` 走 `set_parameter`（revision 正确）；`runtime_file_signature` 提供 mtime/size 缓存失效签名。
- 异常与取消路径：加载失败设置 `error_message` + `load_error` 并返回 {}，错误可见（上传分支见 AUD-7111）。
- 输入验证：宽高 `max(1,...)`；color_mode 白名单校验；opacity 钳 [0,1]；track_data JSON 具名异常捕获；`points_from_track_data` 空结果显式报错。
- 性能模式：非帧依赖（params_require_frame_cache=False）避免逐帧缓存失效，合理。
- 类型注解：完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-7110 |
| Major | 2 | AUD-7101、AUD-7107 |
| Suggestion | 8 | AUD-7102、AUD-7103、AUD-7104、AUD-7105、AUD-7106、AUD-7108、AUD-7109、AUD-7111 |

Critical：pointcloud3d 每次求值泄漏一张 GPU 纹理（无持有/无释放/缓存不接管，动画播放可致 VRAM 耗尽崩溃）。
Major：noise 节点 animate 参数完全失效（time 永不注入 + 未声明帧缓存）；particle 湍流模拟为纯 Python O(帧数×粒子数) 循环，可达参数下单帧数十秒。
noop/paint/placeholder 三文件无发现。
