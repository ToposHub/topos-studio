# batch-069 审查报告 — src/features/composite/nodes/（8 文件，1,978 行）

审查人：batch-069 子智能体（2026-09-28）。编号区间 AUD-6900–AUD-6999。
交叉核对过的外部模块：`nodes/base_node.py`、`evaluators/base_evaluator.py`（`_resolve_output`/`_dispatch_effect`/`_sync_render_nodes`/求值异常捕获）、`evaluators/keyer_evaluator.py`、`evaluators/color_evaluator.py`、`evaluators/filter_evaluator.py`、`render/composite_renderer.py`、`core/frame_sequence.py`、`core/shaders.py`、`render3d/hdri.py`、`render3d/math_utils.py`、`nodes/constant_node.py`。

汇总：Critical 0 / Major 4 / Suggestion 11。

---

## src/features/composite/nodes/huecorrect_node.py (行数: 129)

### 发现
- [AUD-6908] [Suggestion] 默认值在 PARAM_DEFS / __init__ / has_changes 三处重复，存在漂移风险
  - 位置: src/features/composite/nodes/huecorrect_node.py:66-74
  - 证据:
    ```python
    self.parameters = {
        "hue_shift": 0.0,
        "saturation_mult": 1.0,
        "brightness_mult": 1.0,
        "range_low": 0.0,
        "range_high": 360.0,
        "range_softness": 10.0,
        "mix": 1.0,
    }
    ```
    （:85-93 `_do_evaluate` 内又定义了一份逐键相同的 `defaults` 字典用于 `has_changes` 判断；PARAM_DEFS :41-55 为第三份）
  - 影响: 任何一处默认值修改后漂移，`has_changes` 会误判"无变化"直接直通（或反之），调色结果静默错误。
  - 建议: 提取类级 `_DEFAULTS` 常量（或由 PARAM_DEFS 生成），三处共用。
  - 置信度: 高

### 通过项
- 线程安全：纯渲染线程求值，无跨线程共享可变状态
- GPU 资源生命周期：走 effect-marker 协议（`texture_id=-1` 占位），节点不自持 GPU 资源
- GLSL·HDR 正确性：参数全部经 `_get_param_clamped` 钳制；已核对 shaders.py `hue_range_weight`（:2110-2149）正确处理 low>high 环绕与全范围直通；shader 端无 clamp，HDR 保留
- 信号防递归：无 Qt 信号
- 状态提交一致性：走基类 `set_parameter`/revision 机制
- 输入验证：`is_valid` 检查 + 可选 mask（已核对 `_dispatch_effect` :2073-2074 对 `_effect_mask=None` 安全降级为白mask）
- 性能模式：默认参数直通短路（:99-100）
- 异常与取消路径：无易抛路径

---

## src/features/composite/nodes/image_input_node.py (行数: 617)

### 发现
- [AUD-6900] [Major] 任意参数编辑都会触发渲染线程同步磁盘重解码（纹理内容未变也重读）
  - 位置: src/features/composite/nodes/image_input_node.py:512-515
  - 证据:
    ```python
    if self._dirty and self._load_state != ImageLoadState.READY_CPU:
        # Legacy sync path: only if no async loader is driving us
        if self._load_state not in (ImageLoadState.LOADING, ImageLoadState.READY_CPU):
            self.upload_to_gpu(ctx, frame=frame_number)
    ```
    `base_evaluator._sync_render_nodes`（:373-385）把任意快照参数（含 color_space/premultiplied/media_role 等纯元数据）经 `existing.set_parameter(key, val)` 打到渲染实例 → `_dirty=True`；此时 `_load_state==UPLOADED_GPU`、`needs_file_mtime_check` 对未变文件返回 False（异步 loader 不会接管），于是 `upload_to_gpu`（:449-478）无条件重新 `decode_image_file`。
  - 影响: 用户每次修改非内容参数（如切换色彩空间标签）都在渲染线程做完整磁盘 I/O + 解码 + 纹理重建（4K EXR 可达数百毫秒），交互卡顿，违背实时基准。
  - 建议: 同步路径进入前先用 `needs_file_mtime_check`/exr 选择比对判断内容是否真的变化，未变化则仅刷新元数据；或区分"影响内容的参数"与"仅元数据参数"两类脏标记。
  - 置信度: 高
- [AUD-6901] [Major] 图像序列（####/%04d）播放期间每帧在渲染线程同步解码，无法实时预览
  - 位置: src/features/composite/nodes/image_input_node.py:503-515
  - 证据:
    ```python
    if "#" in path or "%0" in path:
        frame_number = int(frame_number)
        if getattr(self, "_last_sequence_frame", None) != frame_number:
            self._last_sequence_frame = frame_number
            self.reload()
            self._load_state = ImageLoadState.MISSING
    ...
            self.upload_to_gpu(ctx, frame=frame_number)
    ```
    `composite_renderer._sync_image_input_nodes`（:1164）对 pattern 路径调用 `needs_file_mtime_check` 时因 `os.path.isfile(pattern)==False` 返回 False 直接 continue——异步 loader 永远不接管序列源，每帧变更都落到 `upload_to_gpu` 同步"读盘+解码"。
  - 影响: 序列播放时每帧阻塞渲染线程（4K EXR 数百 ms/帧），帧率崩坏；`upload_to_gpu` 自身 docstring 明言 "Blocks the calling thread… Prefer upload_from_cpu() for interactive use"。
  - 建议: 序列源接入 ImageLoader 异步预取（当前帧 ±1 队列），预览模式下未就绪时沿用上一帧纹理。
  - 置信度: 高
- [AUD-6902] [Major] 同步上传路径释放/重建自持 GPU 纹理时未设置 `_cache_invalidated_needed`，下游缓存可能绑定已释放/复用的 glo
  - 位置: src/features/composite/nodes/image_input_node.py:280-285
  - 证据:
    ```python
    # Release old textures
    if self._gpu_texture is not None:
        self._gpu_texture.release()
        self._gpu_texture = None
    if self._gpu_depth_texture is not None:
        self._gpu_depth_texture.release()
        self._gpu_depth_texture = None
    ```
    constant_node.py:619-627 在同样"释放旧 glo + 新建纹理"场景显式置 `self._cache_invalidated_needed = True`，base_evaluator.py:988-1005 依据该标志清除陈旧缓存并注释"cache hit … would bind the dead name and segfault"；composite_renderer.py:1203-1214 仅在异步上传路径强清全部缓存，序列播放/导出/无 loader 环境的同步路径（本文件 `upload_to_gpu`→`upload_from_cpu`）没有等价防护。
  - 影响: 同步换帧释放旧 glo 后，在 glo 被 GL 复用且下游缓存键未捕获上游 texture_id 的路径上可能命中陈旧条目 → 渲染错误（张冠李戴的纹理）甚至原生崩溃。
  - 建议: `upload_from_cpu` 成功上传后置 `self._cache_invalidated_needed = True`，与 constant/particle/text/scene3d 四个自管源节点保持一致。
  - 置信度: 中（机制与缺失事实确定；实际触发依赖缓存键未覆盖的路径）
- [AUD-6903] [Suggestion] 上传路径整幅图像双重拷贝，峰值内存翻倍
  - 位置: src/features/composite/nodes/image_input_node.py:274-291
  - 证据:
    ```python
    data = np.ascontiguousarray(np.flipud(self._cpu_data))
    ...
    self._gpu_texture = ctx.texture(
        (w, h), components,
        data=data.tobytes(),
        dtype='f4',
    )
    ```
  - 影响: `ascontiguousarray` 已做一次整幅拷贝（flipud 视图落地），`tobytes()` 再整幅拷贝一次；4K RGBA f32 每次上传瞬时多占约 134 MB 并多一次大 memcpy（moderngl 支持直接接受连续 ndarray 的缓冲协议）。
  - 建议: `data=data` 直传连续数组；深度纹理上传（:300-305）同理。
  - 置信度: 高
- [AUD-6904] [Suggestion] missing_policy=error 且解码失败后无失败退避，每次 evaluate 重复同步解码尝试
  - 位置: src/features/composite/nodes/image_input_node.py:468-475
  - 证据:
    ```python
    decoded = decode_image_file(path, exr_part=exr_part,
                                extra_channels=extra_list)
    if decoded is None:
        # Try fallback
        if not self._upload_fallback(ctx):
            self._load_state = ImageLoadState.FAILED
            self.error_message = f"Cannot decode: {path}"
        return
    ```
    失败分支不清 `_dirty`，`_load_state=FAILED` 不在跳过集合（:514）内，下一帧求值再次进入 `upload_to_gpu` 重复磁盘尝试。
  - 影响: 缺文件/坏文件场景每帧在渲染线程重复 stat+decode 调用（配合 renderer 侧 `_sync_image_input_nodes` 的 is_ready 门还会反复发起异步请求），浪费 CPU 与日志。
  - 建议: 失败后记录已尝试的 (path, mtime, exr 选择) 组合，未变化时短路返回。
  - 置信度: 中
- [AUD-6905] [Suggestion] 解码失败/回退占位时 `_cpu_color_space_hint` 不清理，占位纹理沿用旧文件色彩空间标记
  - 位置: src/features/composite/nodes/image_input_node.py:583-585
  - 证据:
    ```python
    # Use hint from decoded image if available
    if self._cpu_color_space_hint:
        return self._cpu_color_space_hint
    ```
    `set_load_failed`（:238-242）与 `_upload_fallback`（:364-401）均不重置该 hint。
  - 影响: 切换到新文件后解码失败、以 black/checker 占位时，占位纹理被标成上一个文件的颜色空间，下游颜色管理判定错误。
  - 建议: `set_load_failed`/`reload` 中将 `_cpu_color_space_hint` 置空。
  - 置信度: 中（代码路径确定，影响面小）
- [AUD-6906] [Suggestion] 多个公开方法缺少类型注解
  - 位置: src/features/composite/nodes/image_input_node.py:217, 238, 244, 449, 554
  - 证据:
    ```python
    def set_decoded_image(self, decoded: DecodedImage):
    def set_load_failed(self, error_message: str):
    def set_load_loading(self):
    def upload_to_gpu(self, ctx, frame=None):
    def cleanup(self):
    ```
  - 影响: 与项目强类型规范不一致（其余方法均已注解）。
  - 建议: 补 `-> None` / `-> bool` 等返回注解，`ctx: moderngl.Context`、`frame: Optional[int]`。
  - 置信度: 高
- [AUD-6907] [Suggestion] 内联 `"#" in path or "%0" in path` 与 `has_frame_pattern` 重复实现
  - 位置: src/features/composite/nodes/image_input_node.py:505
  - 证据:
    ```python
    path = str(self.parameters.get("file_path", "") or "")
    if "#" in path or "%0" in path:
    ```
    `frame_sequence.has_frame_pattern`（frame_sequence.py:20-22）为同一逻辑的正式实现，本文件 :420-423 已 import 过该模块。
  - 影响: 若 frame_sequence 将来扩展 pattern 形式（如 `$F4`），此处序列换帧判断不跟随，序列刷新静默失效。
  - 建议: 复用 `has_frame_pattern(path)`。
  - 置信度: 高

### 通过项
- GPU 资源生命周期（除 AUD-6902 外）：`cleanup` 释放主/深度纹理并清 CPU 数据；`_upload_fallback` 先释放旧纹理再新建；`cleanup` 仅由渲染线程调用（composite_renderer.py:1275、base_evaluator.py:336 均在渲染循环内）
- 线程安全：`set_decoded_image`/`set_load_failed` 经渲染线程命令队列调用（composite_renderer.py:1185-1224），符合设计
- 输入验证：`needs_upload`/`isfile` 门控；exr 选择漂移检测（:339-360）请求对请求比较，避免重解码死循环
- 状态提交一致性：`_revision` 每次上传自增并随 TextureResult 输出；to_dict/from_dict `_meta` 元数据往返完整
- 异常与取消路径：`upload_from_cpu` 捕获异常置 FAILED 并记录 error_message；节点级求值异常由 evaluator :1037 兜底
- GLSL·HDR 正确性：float32 'f4' 上传，无 0-1 clamp；CPU→GL 翻转一次性在边界完成

---

## src/features/composite/nodes/invert_node.py (行数: 86)

### 发现
- [AUD-6909] [Suggestion] HDR 反相按 1.0-c 定义产生负值，HDR 语义未文档化/无 pivot
  - 位置: src/features/composite/nodes/invert_node.py:60-85
  - 证据:
    ```python
    return {
        "result": TextureResult(
            texture_id=-1,
            ...
        "_effect_params": {
            "channels": [
                1.0 if self.parameters.get("invert_r", True) else 0.0,
    ```
    （shaders.py:1556-1559 `mix(color.r, 1.0 - color.r, u_channels.r)`，无 clamp、pivot 固定 1.0）
  - 影响: HDR（float16/32）像素 c>1.0 反相后为负值；下游 screen/add 混合、显示转换对负 RGB 行为未定义，可能产生非预期结果。
  - 建议: 在节点/帮助文档明确 HDR 反相语义，或提供 pivot 参数（pivot - c）；确认下游对负值的处理策略。
  - 置信度: 中（行为确定存在；是否算缺陷取决于设计意图）

### 通过项
- 线程安全：无共享可变状态，仅渲染线程求值
- GPU 资源生命周期：marker 协议，无自持资源
- 信号防递归：无 Qt 信号
- 状态提交一致性：默认参数直通（:67-68）返回真实 TextureResult、无 marker 键，符合协议
- 输入验证：`is_valid` 检查；全关直通短路
- 性能模式：直通短路正确
- 异常与取消路径：无易抛路径

---

## src/features/composite/nodes/keyer_node.py (行数: 278)

### 发现
- [AUD-6910] [Suggestion] keyer_mode/output_mode 未做白名单校验、gain 无上限钳制
  - 位置: src/features/composite/nodes/keyer_node.py:208-221
  - 证据:
    ```python
    keyer_mode = self.parameters.get("keyer_mode", "auto")
    ...
    gain = max(0.0, float(self.parameters.get("gain", 1.0)))
    ...
    output_mode = self.parameters.get("output_mode", "result")
    ```
    keyer_mode 为非法字符串时 :246-274 三个分支均不命中，`effect_params` 缺 `keyer_mode` 键；keyer_evaluator.py:116 `keyer_mode = effect_params.get("keyer_mode", "chroma")` 静默按 chroma 渲染。gain 的 ParamDef max_val=4.0（:84-85）但求值只钳下限。对照 despill_method 有白名单校验（:253-255）。
  - 影响: 手改/损坏工程文件下静默按错误模式渲染而非报错；gain 越界（如 100）导致 matte 过曝。
  - 建议: `keyer_mode not in KEYER_MODES → 回落 auto/auto-detect`，`output_mode not in OUTPUT_MODES → "result"`；gain 改用 `_get_param_clamped("gain", 0.0, 4.0)`。
  - 置信度: 高

### 通过项
- 线程安全：无共享可变状态
- GPU 资源生命周期：marker 协议；difference 模式正确改用 `_effect_input_a/_b` 并删除 `_effect_input`（:272-276），与 `_apply_keyer` 契约一致（已核对 keyer_evaluator.py:96-140）
- GLSL·HDR 正确性：matte 参数 0-1 域合理（matte 本身为 0-1）；despill_method 索引映射与 shader u_despill_method 注释一致（:38-39）
- 信号防递归：无 Qt 信号
- 状态提交一致性：BYPASS_MAPPING={"result": "image"} 正确；screen_color_hex 与 RGB 分量双轨（同 ConstantNode 模式）
- 输入验证（除 AUD-6910 外）：chroma 分量全部 `_get_param_clamped`；difference 无参考时安全直通
- 性能模式：无参考输入的 difference 直通短路
- 异常与取消路径：无易抛路径

---

## src/features/composite/nodes/lens_distortion_node.py (行数: 201)

### 发现
- [AUD-6911] [Suggestion] `_effect_params` 原样透传 `self.parameters`，畸变系数未按 ParamDef 范围钳制
  - 位置: src/features/composite/nodes/lens_distortion_node.py:192-201
  - 证据:
    ```python
    return {
        "result": TextureResult(
            texture_id=-1,
            width=image.width,
            height=image.height,
        ),
        "_effect_type": "lens_distortion",
        "_effect_input": image,
        "_effect_params": dict(self.parameters),
    }
    ```
    filter_evaluator.py:258-264 直接 `float(params.get("k1", 0.0))` 透传 uniform，无钳制。
  - 影响: 表达式/手改工程写入越界 k1..p2/center（如 k1=50）时直接进 shader；"distort" 模式的逆迭代对大系数可能不收敛 → 采样坐标发散、黑块/条纹。
  - 建议: 与批次内其他节点一致，用 `_get_param_clamped` 按 ParamDef min/max 归一后再放入 `_effect_params`。
  - 置信度: 中

### 通过项
- 线程安全：无共享可变状态
- GPU 资源生命周期：marker 协议，无自持资源
- GLSL·HDR 正确性：纯几何重采样，不触碰颜色；CPU 参考模型 `map_point` 与 GPU 同一高度归一化约定（:64-69 注释明确）
- 信号防递归：无 Qt 信号
- 状态提交一致性：BYPASS_MAPPING 正确；`filter_enabled=False` 直通返回真实 TextureResult
- 输入验证（除 AUD-6911 外）：`_tile_padding` 对非法数值做了 `or 0.0` 防御，零系数短路 padding=0
- 性能模式：tile padding 依据输入帧高度缩放（:155-158），未知时保守 2160
- 异常与取消路径：无易抛路径
- ROI 契约：compute_output_bbox/compute_input_roi/requires_roi_expansion 三者一致

---

## src/features/composite/nodes/lens_flare_node.py (行数: 169)

### 发现
- [AUD-6912] [Suggestion] `color` 参数未验证/归一化，非数值形态导致求值抛异常
  - 位置: src/features/composite/nodes/lens_flare_node.py:124
  - 证据:
    ```python
    color = self.parameters.get("color", (1.0, 0.9, 0.75, 1.0))
    ...
    "light_color": (float(color[0]), float(color[1]),
                    float(color[2])),
    ```
  - 影响: 旧工程/手改数据把 color 存成 hex 字符串（`float("#")`）或 None（`color[0]`）时抛异常，节点求值失败（evaluator :1037 捕获后该节点输出为空，flare 整体消失）。
  - 建议: 校验 `len(color)>=3` 且逐分量 `float()`+范围钳制，失败回落默认色。
  - 置信度: 中

### 通过项
- 线程安全：无共享可变状态
- GPU 资源生命周期：marker 协议，无自持资源
- GLSL·HDR 正确性：叠加型效果不 clamp，intensity 上限 3.0 与 ParamDef 一致；light_pos 允许 -0.5..1.5 画外光源合理
- 信号防递归：无 Qt 信号
- 状态提交一致性：输出 TextureResult 完整透传 format/bbox/premultiplied/color_space（:131-134），符合协议对元数据保留的要求
- 输入验证（除 AUD-6912 外）：全部数值参数 min/max 双向钳制，ghost_count 钳到 [0, MAX_GHOSTS]
- 性能模式：preview 下 ghost_count 减半（:120-122），符合 draft 降级策略；纯 screen-space 无 ROI 膨胀
- 异常与取消路径：无其他易抛路径

---

## src/features/composite/nodes/light3d_node.py (行数: 372)

### 发现
- [AUD-6913] [Major] `from_dict` 旧参数迁移分支不可达，v6 场景灯光静默回落默认值
  - 位置: src/features/composite/nodes/light3d_node.py:183
  - 证据:
    ```python
    has_legacy = any(p in params for p in _LEGACY_PARAMS) or "light_intensity" in params
    if has_legacy and "light_type" not in params and not _has_new_light_params(params):
    ```
    `params = node.parameters` 来自 `super().from_dict(data)`（:179），而 base_node.py:1240-1245 先 `cls(node_id=...)` 构造（本类 `__init__` :166 `self.parameters = _make_default_parameters()` 已含 `light_type`/`light_pos_x`），再把 loaded params 合并覆盖——所以 `"light_type" not in params` 恒为 False，:184-211 整个迁移块永不执行。
  - 影响: v6 旧场景（light0_*~light3_* 4 槽位 / legacy 主灯）加载后灯光静默变成默认白平行光（intensity 2.0、dir -0.6/0.7/0.4），旧外观丢失；且 legacy 键残留在 parameters 字典中随再次保存持久化。
  - 建议: 迁移检测改为基于 `data.get("parameters", {})` 的原始 loaded 参数（构造默认值混入之前），而非合并后的 `node.parameters`。
  - 置信度: 高
- [AUD-6914] [Suggestion] cleanup 与 IBL 降级路径宽泛 `except Exception: pass` 静默吞异常
  - 位置: src/features/composite/nodes/light3d_node.py:323-326
  - 证据:
    ```python
                try:
                    irradiance_cube, prefilter_cube, brdf_lut = self._hdri_baker.bake(gl_ctx, "")
                except Exception:
                    pass
    ```
    （cleanup 同样：:363-366 `except Exception: pass`）
  - 影响: 资源释放失败/降级 bake 失败无任何日志，违背"宽泛 except 不得掩盖领域错误"；GL 上下文异常被掩盖后状态不可诊断。
  - 建议: 至少 `logger.warning(...)` 记录异常再降级。
  - 置信度: 高

### 通过项
- 线程安全：求值在渲染线程；HDRIBaker 注释明确要求渲染线程调用；`_hdri_baker`/`_ies_cache_key` 仅渲染线程触碰
- GPU 资源生命周期（除 AUD-6914 外）：cleanup 释放 IBL 烘焙器 cube 资源；cleanup 仅在渲染线程调用（base_evaluator.py:336、composite_renderer.py:1275）
- GLSL·HDR 正确性：方向归一化对零向量有防御（math_utils.py:334-338）；outer_cone ≥ inner_cone 修正（:265-266）；颜色/强度仅下限钳制，HDR 高强度保留（max 50）
- 输入验证：light_type/decay_mode/shadow_quality/ies 均有白名单回落；ies 文件缺失按非致命诊断降级且异常类型收窄（OSError/TypeError/ValueError）
- 信号防递归：无 Qt 信号
- 状态提交一致性（除 AUD-6913 外）：参数默认值 PARAM_DEFS 与 `_make_default_parameters` 对齐；IBL 缓存按 path+mtime（内部）
- 性能模式：IBL 按 (path, mtime, size, samples) 缓存，重复求值不重烘焙（hdri.py:387-389）；IES 结果缓存
- scene 旁路协议：正确解包 CompositeEvalContext 取 gl，同时用原 ctx emit payload（:232-235, :354）；非 CompositeEvalContext 环境静默跳过为文档化行为

---

## src/features/composite/nodes/light_wrap_node.py (行数: 126)

### 发现
（无）

### 通过项
- 线程安全：无共享可变状态，仅渲染线程求值
- GPU 资源生命周期：marker 协议（`_effect_input_a`=background / `_effect_input_b`=foreground），与 `_apply_light_wrap` 契约一致（已核对 keyer_evaluator.py:25-94）
- GLSL·HDR 正确性：亮度类参数仅下限钳制不设 0-1 上限，HDR 叠加保留；alpha 语义显式声明（alpha_premultiplied=True，:113）
- 信号防递归：无 Qt 信号
- 状态提交一致性：输出元数据（format/bbox/color_space 取 fg 优先、bg 回落）与 GPU applier 输出一致
- 输入验证：双输入均要求 `is_valid`；falloff 下限 0.1 与 ParamDef 一致
- 性能模式：preview 下 width 钳到 48（:100-101）符合 draft 降级；ROI 双输入按 width 膨胀且与 requires_roi_expansion 一致
- 异常与取消路径：无易抛路径
- 类型注解：完整

---

## 附注（跨文件共性，供后续批次参考）

1. effect-marker 协议在本批次 8 个节点中的使用均符合 base_node.py 文档约定（占位 `texture_id=-1`、直通路径无 `_` 键、多输入用 `_effect_input_a/_b`、mask 用 `_effect_mask`）。
2. base_evaluator.py:2042-2044 的注释称"the unified keyer never sets `_effect_input`"，与 keyer_node.py:243 实际实现（chroma/luma 模式设置 `_effect_input`）不符——注释过时，不影响功能（`_apply_keyer` 自行读取两种键），建议另行修正注释（归属 base_evaluator 批次）。
