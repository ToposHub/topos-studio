# 批次报告：batch-059 — src/features/composite/evaluators/

- 审查日期：2026-09-28
- 编号区间：AUD-5900–AUD-5999
- 文件数：5（1,424 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 背景核验：mixin 由 `GraphEvaluatorMixin`（graph_evaluator_mixin.py:61-78）组合，仅在渲染线程执行；shader 源核对自 `src/features/composite/core/shaders.py`；节点参数契约核对自 `src/features/composite/nodes/`。

## src/features/composite/evaluators/blur_evaluator.py (行数: 420)

### 发现

- [AUD-5910] [Major] 池纹理获取后无 try/finally 保护，求值中途抛错即永久泄漏整幅纹理
  - 位置: src/features/composite/evaluators/blur_evaluator.py:175-207
  - 证据:
    ```python
    temp_tex = self._resource_pool.acquire_texture(w, h)   # 175
    ...
    self._render_quad(prog_h)                              # 187
    ...
    out_tex = self._acquire_fbo(w, h)                      # 190 (内部 acquire_texture)
    ...
    self._render_quad(prog_v)                              # 204
    self._resource_pool.release_texture(temp_tex)          # 206
    ```
    两处 acquire（temp、out）与 release/返回之间无异常保护；`_apply_defocus`:347、`_apply_sharpen`:396、`_blur_single_pass`:237 同模式。
  - 影响: uniform 设置或 `_render_quad` 抛错时，纹理滞留在 `GPUResourcePool._active_textures`（gpu_resource_pool.py:887/909，仅 `release_texture` 能移出，无错误路径清扫）。持续性错误（如参数类型不符）每帧泄漏一张全幅 RGBA16F（4K 约 66MB），交互帧率下数十秒内耗尽 VRAM。
  - 建议: 每个效果方法用 try/except 包裹渲染段并在异常时 `release_texture` 归还已获取纹理；或在求值器层面登记本节点 acquire 的 glo 列表、异常时统一回收。
  - 置信度: 高（机制已核实），中（触发依赖上游错误）

- [AUD-5911] [Suggestion] 旧版 blur_h/blur_v 回退路径静默丢弃 kernel_mode/通道掩码/mix 语义
  - 位置: src/features/composite/evaluators/blur_evaluator.py:167-170
  - 证据:
    ```python
    # Fallback to legacy single-type shaders if new ones unavailable.
    prog_h = self.utility_shaders.get('blur_h')
    prog_v = self.utility_shaders.get('blur_v')
    ```
    旧版 shader 仅声明 `u_texture/u_resolution/u_radius`（shaders.py:398-400、432-434），随后经 `_set_uniform_if_present` 设置的 `u_kernel_mode`/`u_channel_mask`/`u_mix`/`u_original` 全部被静默跳过。
  - 影响: 回退触发时 gaussian 退化为 box、按通道处理与 mix 静默失效，无任何日志提示语义降级。
  - 建议: 回退到旧版 shader 时至少 logger.warning 一次，或直接删除该死回退（新 shader 在库中常驻）。
  - 置信度: 高

- [AUD-5912] [Suggestion] separable 双 pass 渲染后残留纹理单元绑定，与 P1-6 约定不一致
  - 位置: src/features/composite/evaluators/blur_evaluator.py:181-204
  - 证据:
    ```python
    self._bind_external_texture(input_tex.texture_id)   # 181 → unit 0
    ...
    temp_tex.use(0)                                     # 191
    self._bind_texture_unit(input_tex.texture_id, 1)    # 195 → unit 1
    self._render_quad(prog_v)                           # 204 之后不解绑
    ```
    对比同 mixin `_blur_single_pass`:281-285（finally 解绑 unit 1）与 base_evaluator `_copy_texture`/`_apply_grade` 的 P1-6 注释（"drop the bind before returning"）。
  - 影响: unit 0/1 长期持有对池化输入纹理的引用；输入纹理随后被释放时依赖 GL 驱动自动解绑行为，与项目已确立的 P1-6 修复模式相悖，埋下采样到已释放名字的隐患。
  - 建议: 渲染完成后统一 `self._unbind_texture_unit(0)` / `(1)`。
  - 置信度: 中

- [AUD-5913] [Suggestion] `_apply_defocus`/`_apply_sharpen` 直接索引 uniform，未沿用 `_set_uniform_if_present` 容错模式
  - 位置: src/features/composite/evaluators/blur_evaluator.py:354-357, 403-408
  - 证据:
    ```python
    for name, value in uniforms.items():
        prog[name].value = value          # 356-357
    ...
    prog['u_amount'].value = amount       # 404
    prog['u_radius'].value = radius       # 405
    ```
    已核对 `_DEFOCUS_FRAGMENT`/`_SHARPEN_FRAGMENT`（shaders.py:1263-1273、2388-2395）当前均声明这些 uniform，暂不触发；但同 mixin 其它路径全部使用容错 setter，一旦 shader 侧删改 uniform 即 KeyError → 节点空输出（同批 AUD-5901 即该模式的现实案例）。
  - 影响: 防御性不一致；未来 shader 调整会直接炸掉 defocus/sharpen 节点。
  - 建议: 统一改用 `self._set_uniform_if_present`。
  - 置信度: 高（不一致性），低（当前触发可能）

- [AUD-5914] [Suggestion] `_apply_defocus` 结果沿用原始 bbox，而 blur/sharpen 传 `effective_bbox()`，元数据传播不一致
  - 位置: src/features/composite/evaluators/blur_evaluator.py:365 vs 416 与 93
  - 证据:
    ```python
    # _apply_defocus
    bbox=input_tex.bbox,                 # 365
    # _apply_sharpen / _make_blur_result
    bbox=input_tex.effective_bbox(),     # 416 / 93
    ```
  - 影响: 两者语义等价（None 即隐式全帧，见 base_node.py effective_bbox），但下游若做 `is None` 分支或比较 bbox 对象，同一输入经不同效果会得到不同形态的元数据。
  - 建议: 统一走 `effective_bbox()`。
  - 置信度: 高（事实），低（实际影响）

### 通过项
- 线程安全：仅渲染线程调用（GraphEvaluatorMixin 契约），无跨线程状态。
- GPU 生命周期：成功路径上 temp/out 纹理均归还或经 TextureResult 交给缓存 owned_ids 管理。
- GLSL·HDR 正确性：channel mask 4 分量与 vec4 声明一致；mix/edge_policy 语义经 u_original 引用原始帧，注释与 shader 实现吻合；无无理由 clamp。
- 异常路径：compute shader 失败回退 fragment 均有日志且降级安全（161-162 宽 except 属带日志的显式回退，可接受）。
- 输入验证：blur_type 未知值回退 box 并告警；aperture_blades 字符串/数值双形态均处理；shutter 夹取 [0,2]；sample_count 夹取 [4,32]。
- 信号防递归 / 状态提交一致性：本文件无 Qt 信号与 undo/redo 交互，不适用。
- 类型注解：主要方法参数有注解（`_acquire_fbo`/`_make_blur_result` 的纹理参数未标注类型，轻微）。

## src/features/composite/evaluators/cache_evaluator.py (行数: 192)

### 发现

- [AUD-5920] [Major] 磁盘缓存命中路径丢失 depth/AOV/render_diagnostics 元数据，与新鲜求值结果不一致
  - 位置: src/features/composite/evaluators/cache_evaluator.py:101-129
  - 证据:
    ```python
    return TextureResult(
        texture_id=tex.glo,
        width=w,
        height=h,
        format=reference.format if reference is not None else None,
        alpha_premultiplied=(reference.alpha_premultiplied ...),
        color_space=(reference.color_space ...),
    )   # 120-128：仅恢复 format/premult/color_space
    ```
    磁盘命中分支（base_evaluator.py:862-889）直接 `continue`，绕过新鲜求值必经的 `_propagate_auxiliary_metadata`（base_evaluator.py:951）；而 opt-in 的 `cache_to_disk` 节点恰含 zblur/fog/depth_matte/cryptomatte_select/relight 等 depth/AOV 类节点（depth_matte_node.py:46、relight_node.py:81 等）。
  - 影响: 同一节点磁盘命中帧与新鲜求值帧输出契约不同：depth/AOV 通道在命中帧静默消失，下游 Output/FileOutput 按其文档"silently fall back to beauty-only"——时间线上表现为 aux 通道闪烁/导出缺失。
  - 建议: `_load_disk_cache_outputs` 返回前对恢复结果执行与 951 行相同的 aux 传播（reference 即 working_inputs 中的输入，可直接引用其 depth/AOV glo）。
  - 置信度: 高（代码路径确定；实际影响取决于节点是否携带 aux）

- [AUD-5921] [Suggestion] `_readback_result_array` 切换 FBO 与 viewport 后不恢复全局 GL 状态
  - 位置: src/features/composite/evaluators/cache_evaluator.py:88-95
  - 证据:
    ```python
    fbo = self.fb_manager.get_fbo(tex)
    fbo.use()
    self.ctx.viewport = (0, 0, int(tex_result.width), int(tex_result.height))
    raw = fbo.read(components=4, dtype="f4")
    ```
  - 影响: write-through 完成后节点输出纹理的 FBO 仍处绑定状态；后续效果各自 `fbo.use()` 重建状态故通常无碍，但任何假定先前绑定的后续操作（展示、连续 readback）会读到意外目标。
  - 建议: readback 后恢复原绑定或至少在注释中固化"调用方必须重绑"的契约。
  - 置信度: 中

- [AUD-5922] [Suggestion] `_invalidate_cache_with_downstream` 用裸 `except Exception: pass` 吞掉遍历错误且无日志
  - 位置: src/features/composite/evaluators/cache_evaluator.py:179-184
  - 证据:
    ```python
    try:
        frontier.extend(snapshot.get_downstream_nodes(nid))
    except Exception:
        # Snapshot API missing/changed — best-effort ...
        pass
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"；若 snapshot API 变更，下游陈旧缓存条目（其注释自述会"bind the dead name and segfault"）被静默留下，把致命路径降级为不可诊断的偶发崩溃。
  - 建议: 至少 `logger.warning` 记录异常与 node_id。
  - 置信度: 高

- [AUD-5923] [Suggestion] 磁盘缓存同步全帧 f4 readback + npz 压缩阻塞渲染线程（MVP 已注明）
  - 位置: src/features/composite/evaluators/cache_evaluator.py:74-99
  - 证据:
    ```python
    rgba = self._readback_result_array(tex)     # fbo.read(components=4, dtype="f4")
    ...
    get_disk_cache_store().store(...)           # 同步 npz float16 压缩
    ```
    docstring 自述 "Synchronous MVP readback"。
  - 影响: 4K 下单次 readback 约 132MB float32 + 翻转拷贝 + 压缩，落在渲染线程；`cache_to_disk` 节点参数/时间变化导致的每次 cache miss 都产生明显帧时间尖峰，与 4K 实时基准冲突。属已声明的 MVP 取舍，故仅列 Suggestion。
  - 建议: 移至后台 PBO/Fence 异步 readback 或至少降采样存储。
  - 置信度: 高

### 通过项
- 线程安全：渲染线程专用；disk store 读写均以失败降级（返回 None / 告警）不破坏求值。
- GPU 生命周期：上传路径 float32→float16 收窄与池 f2 纹理字节宽匹配（注释解释了 tex.write 尺寸校验）；上传异常时纹理不外泄（失败返回 None，acquire 后异常路径属 AUD-5930 同款模式但 try 内立即捕获并返回 None，纹理仍滞留 active——严格说同泄漏模式，因已捕获且路径罕见并入本条提示：`_upload_rgba_array_result`:112-119 异常分支未 release）。
- GLSL·HDR 正确性：flipud 上下翻转两次对称处理（读回顶朝上/上传底朝上），行列序正确。
- 输入验证：`_upload_rgba_array_result` 校验 ndim/shape/尺寸；缓存键 "n" 前缀命名空间隔离。
- 状态提交一致性：下游 BFS 失效带 visited 防环，正确覆盖 Constant→Merge→Output 链；`_clear_eval_cache` 走正规清理并文档化禁止裸 clear。
- 类型注解：完整。

## src/features/composite/evaluators/channel_evaluator.py (行数: 252)

### 发现

- [AUD-5930] [Major] 四个效果方法池纹理获取后无异常保护（同 AUD-5910 泄漏模式）
  - 位置: src/features/composite/evaluators/channel_evaluator.py:30, 68, 114, 179
  - 证据:
    ```python
    out_tex = self._resource_pool.acquire_texture(w, h)   # 30 (_apply_shuffle)
    out_fbo = self.fb_manager.get_fbo(out_tex)
    ...
    prog['u_r_from'].value = int(params.get("r_from", 0))  # 38：直索引，可抛 KeyError
    self._render_quad(prog)                                # 42
    ```
    `_apply_copy_channel`:68、`_apply_remove_channels`:114、`_apply_channel_merge`:179 同构；uniform 直索引（38-41、78-81、121-133、197-231）任一 KeyError 或渲染异常即泄漏已获取纹理。
  - 影响: 与 AUD-5910 相同——出错帧泄漏全幅纹理，持续错误下 VRAM 快速耗尽。
  - 建议: 同 AUD-5910（try/finally 归还或求值器级回收）。
  - 置信度: 高（机制）

- [AUD-5931] [Suggestion] `_apply_copy_channel` 只验证 input_b 不验证 input_a（不对称输入验证）
  - 位置: src/features/composite/evaluators/channel_evaluator.py:64-74
  - 证据:
    ```python
    input_b = params.get("input_b")
    if not input_b or not input_b.is_valid:
        return {"image": input_a}
    ...
    self._bind_external_texture(input_a.texture_id)   # 74：input_a 未校验 is_valid
    ```
  - 影响: input_a 无效（texture_id<=0）时绑定非法名字，GL 报错或输出黑帧，错误难定位。
  - 建议: 对 input_a 同样做 is_valid 前置检查并安全返回。
  - 置信度: 中

- [AUD-5932] [Suggestion] shuffle/copy_channel/remove_channels 渲染后不解绑 unit 0，与 channel_merge 的完整清理不一致
  - 位置: src/features/composite/evaluators/channel_evaluator.py:42, 84, 135（对照 235-236）
  - 证据:
    ```python
    self._render_quad(prog)          # 42 (_apply_shuffle，无解绑)
    bind_texture_unit(0, 1)          # 84 (_apply_copy_channel 仅清 unit 1)
    ...
    for i in range(4):
        bind_texture_unit(0, i)      # 235-236 (_apply_channel_merge 正确清理)
    ```
  - 影响: 同 AUD-5912——残留绑定引用池化输入纹理，违背 P1-6 约定；同文件内三种清理策略并存易引入回归。
  - 建议: 统一渲染后解绑所有已用单元。
  - 置信度: 中

### 通过项
- GLSL 正确性：四个 shader 的 uniform 名与类型（int 索引、bool、vec4、float）逐一对 shaders.py 声明核对一致；shuffle/channel_merge 的越界索引在 shader 内以 if 链 + 默认返回兜底，不会 OOB。
- 输入验证：channel_merge 对四个输入逐一 is_valid 检查，缺失通道绑 1×1 白掩码并以 u_has_* 门控（避免采样未初始化池纹理，注释明确）；节点侧已把字符串选项转为 int 索引（channel_merge_node.py:201-211、shuffle_node.py:92），求值器 int() 再防御一层。
- 信号防递归 / 状态提交一致性 / 异常取消：无信号交互；求值无中途取消点（单 quad 渲染粒度合适）。
- 类型注解：完整。

## src/features/composite/evaluators/color_evaluator.py (行数: 531)

### 发现

- [AUD-5901] [Critical] `_apply_color_correct` 设置 shader 不存在的 `u_brightness` uniform，每次求值必抛 KeyError，ColorCorrect 节点输出为空
  - 位置: src/features/composite/evaluators/color_evaluator.py:151-156
  - 证据:
    ```python
    prog['u_texture'].value = 0
    prog['u_saturation'].value = params.get("saturation", 1.0)
    prog['u_contrast'].value = params.get("contrast", 1.0)
    prog['u_brightness'].value = params.get("brightness", 0.0)   # 155
    prog['u_mix'].value = params.get("mix", 1.0)
    ```
    `_COLOR_CORRECT_FRAGMENT`（shaders.py:1812-1825）声明的 uniform 为 u_exposure/u_contrast/u_saturation/u_hue_shift/u_temperature/u_tint/u_shadows_lifting/u_midtones_gamma/u_highlights_gain/u_channel_selector/u_mix——没有 `u_brightness`。moderngl `prog['u_brightness']` 对不存在的 uniform 抛 KeyError；上游 base_evaluator.py:1074 `except Exception` 捕获后 `cache[node_id] = {}`。已确认调度路径可达：ColorCorrectNode 发 `_effect_type: "color_correct"`（color_correct_node.py:137）→ base_evaluator.py:2116 → 本方法；节点参数默认时走 passthrough（node `_do_evaluate` has_changes 检查），用户改动任一参数即进入此路径。
  - 影响: ColorCorrect 节点任意参数一旦非默认，该节点 100% 求值失败输出空，下游画面缺失/黑帧（渲染错误）。
  - 建议: 删除 u_brightness 行并按 AUD-5902 重写参数映射。
  - 置信度: 高

- [AUD-5902] [Major] `_apply_color_correct` 未映射 ColorCorrectNode 的 13 个真实参数，且 8 个 shader 必需 uniform 缺省为 0 将校正完全还原甚至压碎中间调
  - 位置: src/features/composite/evaluators/color_evaluator.py:139-165
  - 证据:
    ```python
    prog['u_saturation'].value = params.get("saturation", 1.0)
    prog['u_contrast'].value = params.get("contrast", 1.0)
    ...
    # 此处之外再无任何 uniform 设置
    ```
    节点实际提供 exposure/hue_shift/temperature/tint/shadows_lifting_r|g|b/midtones_gamma_r|g|b/highlights_gain_r|g|b/channel_r|g|b|a/mix（color_correct_node.py:36-63），求值器一个都没读；未设置的 active uniform 缺省 0.0：shader 第 7 步 `mix(color.r, result.r, u_channel_selector.r)`（selector=0 → 输出原始色，校正整体作废）、第 6 步 `pow(max(mid_val,0.0001), 1.0/max(gamma,0.001))`（gamma=0 → pow(v,1000)≈0，中间调被压碎）。
  - 影响: 即便修复 AUD-5901，节点 UI 全部控件仍然无效且画面错误（channel_selector=0 恒为原图；若 selector 被置 1 则 gamma/gain=0 会毁掉中间调与高光）。
  - 建议: 完整映射：exposure/hue_shift/temperature/tint 直传；shadows_lifting_r/g|b、midtones_gamma_*、highlights_gain_* 组 vec3；channel_r/g/b/a 组 u_channel_selector vec4。
  - 置信度: 高

- [AUD-5903] [Major] 九个效果方法池纹理获取后无异常保护（同 AUD-5910 泄漏模式）
  - 位置: src/features/composite/evaluators/color_evaluator.py:92, 145, 189, 252, 286, 314, 340, 366, 434
  - 证据:
    ```python
    out_tex = self._resource_pool.acquire_texture(w, h)   # 92 (_apply_grade)
    ...
    prog['u_lift'].value = tuple(params.get("lift", [0, 0, 0]))   # 100：标量参数时 tuple() 抛 TypeError
    ```
    AUD-5901 的 KeyError 即本模式的现成例子：92 行已 acquire，155 行抛错，out_tex 永久滞留 active。
  - 影响: 同 AUD-5910——ColorCorrect 这类持续失败节点每帧泄漏一张全幅纹理，叠加 VRAM 耗尽风险。
  - 建议: 同 AUD-5910。
  - 置信度: 高（机制），且本文件已存在必然触发的实例（AUD-5901）

- [AUD-5904] [Suggestion] `_apply_topos_color` 每次求值都实例化 `ColorStackRenderService` 并解析 stack
  - 位置: src/features/composite/evaluators/color_evaluator.py:415-425
  - 证据:
    ```python
    from src.features.color.core.color_stack_render_service import ColorStackRenderService
    service = ColorStackRenderService()
    resolved = service.resolve_grading_params(...)
    ```
  - 影响: 交互预览下每个含 ToposColor 节点的帧都重复 import 查找 + 构造 + 解析；stack 未变化时纯重复开销，60fps 基准下不必要。
  - 建议: 按 (stack_source, color_stack_id, preset_id, revision) 缓存 resolved 结果，或缓存 service 实例。
  - 置信度: 中（未测 resolve 实际开销，方向确定）

- [AUD-5905] [Suggestion] `_apply_topos_color` 自建第二份 1×1 白掩码纹理，重复 `_get_white_mask_texture()` 且永不释放
  - 位置: src/features/composite/evaluators/color_evaluator.py:476-480
  - 证据:
    ```python
    white_tex = getattr(self, '_topos_color_white_mask_tex', None)
    if white_tex is None:
        white_tex = self.ctx.texture((1, 1), 4, data=bytes([255, 255, 255, 255]), dtype='f1')
        self._topos_color_white_mask_tex = white_tex
    ```
    base_evaluator.py:2994-3007 已有完全相同语义的 `_get_white_mask_texture()` 缓存（`_white_mask_tex`）。
  - 影响: 求值器持有两份等价白纹理、两套缓存属性；上下文销毁路径通常只清理共享那份，第二份靠上下文整体回收，资源管理面冗余。
  - 建议: 直接调用 `self._get_white_mask_texture()`。
  - 置信度: 高

- [AUD-5906] [Suggestion] 多个效果渲染后不解绑纹理单元（仅 `_apply_grade` 遵循 P1-6），且 `_apply_grade` 存在死变量 `white_tex`
  - 位置: src/features/composite/evaluators/color_evaluator.py:115, 167-177, 214, 483（对照 126-127 的正确解绑）
  - 证据:
    ```python
    white_tex = None            # 115：赋值后从未使用（死代码）
    ...
    self._render_quad(prog)     # 167 (_apply_color_correct) / 214 (_apply_huecorrect) / 483 (_apply_topos_color) 后无 _unbind_texture_unit
    # 对照 _apply_grade:126-127
    self._unbind_texture_unit(0)
    self._unbind_texture_unit(1)
    ```
  - 影响: 与 AUD-5912/5932 同族——mask(unit 1)/input(unit 0) 绑定残留、三套清理策略并存；死变量徒增阅读负担。
  - 建议: 统一 draw 后解绑；删除 115 行死赋值。
  - 置信度: 高（事实），中（危害）

### 通过项
- 线程安全：全部方法渲染线程执行；`_histogram_manager`、`_white_mask_tex` 惰性初始化无并发面。
- GLSL·HDR 正确性（除 color_correct 外）：grade/huecorrect/normalize/invert/clamp/premult/unpremult 的 uniform 名与类型逐一对 shaders.py 核对一致（grade u_channel_mask 为 vec3 与 3 元组匹配；normalize/invert/clamp 为 vec4 与 4 元组匹配）；grade 节点侧已把 lift_r/g/b 打包为向量（grade_node.py:160-163）；`_grading_hsb_to_rgb` 中性输入正确映射为 (0,0,0)/(1,1,1)，无 HDR clamp。
- 异常与取消：`_compute_percentile_range` 各失败分支均降级 (0,1) 并留日志/告警；defocus 半径、shutter、sample_count 均有夹取。
- 输入验证：mask 缺失时统一绑白掩码（`_get_white_mask_texture`，规避池纹理未初始化陷阱）；normalize 上传通道掩码取自参数且节点默认与求值器回退值一致。
- 信号防递归 / 状态提交一致性：不适用（无信号、无 undo 交互）。
- 类型注解：`_set_uniform_if_present(prog, name, value)` 的 prog/value 未标注，其余完整。

## src/features/composite/evaluators/expression_evaluator.py (行数: 29)

### 发现

（无）

### 通过项
- 通过（无逻辑）：空占位 mixin，仅类声明与文档字符串；表达式解析委托给 ExpressionParser 模块（histogram_evaluator.py 注释亦确认该分工），无导出符号、无可执行路径。

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-5901 |
| Major | 5 | AUD-5902, 5903, 5910, 5920, 5930 |
| Suggestion | 12 | AUD-5904–5906, 5911–5914, 5921–5923, 5931–5932 |

最优先修复：AUD-5901+5902（ColorCorrect 节点完全失效）与 AUD-5903/5910/5930 泄漏模式（其触发实例恰由 5901 提供）。
