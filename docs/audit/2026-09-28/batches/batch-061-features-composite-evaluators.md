# batch-061 审查报告：src/features/composite/evaluators/

- 任务书：docs/audit/2026-09-28/assignments/batch-061.md
- 编号区间：AUD-6100–AUD-6199
- 审查日期：2026-09-28
- 文件数：6（1,831 行）
- 汇总：Critical 2 / Major 3 / Suggestion 14

交叉验证说明：uniform 名称已逐一与 `src/features/composite/core/shaders.py` 中的
`_KEYLIGHT_FRAGMENT`、`_LUMA_KEYER_FRAGMENT`、`_CHROMA_KEYER_FRAGMENT`、
`_DIFFERENCE_KEYER_FRAGMENT`、`_MATTE_FRAGMENT`、`_MATTE_CLEANUP_FRAGMENT`、
`_SET_ALPHA_FRAGMENT`、`_DISSOLVE_FRAGMENT`、`_DEEP_MERGE_FRAGMENT`、
`_LIGHT_WRAP_FRAGMENT` 比对；`utility_shaders` 唯一填充点为
`base_evaluator.py:3435-3438`（遍历 `ShaderLibrary.STANDARD_NAMES`）；
moderngl 行为以项目 venv 内 moderngl 5.12.0 源码为准。

---

## src/features/composite/evaluators/keyer_evaluator.py (行数: 544)

### 发现

- [AUD-6100] [Major] 模块未定义 `logger`，未知 matte cleanup 操作触发 NameError 崩溃
  - 位置: src/features/composite/evaluators/keyer_evaluator.py:505
  - 证据:
    ```python
    # 模块头部仅有（第 14-19 行）：
    from typing import Dict, Optional, TYPE_CHECKING
    from ..nodes.base_node import TextureResult
    ...
    else:
        logger.warning("Unknown matte cleanup operation %r; passing through", operation)
        result = {"result": input_tex}
    ```
  - 影响: `_apply_matte_cleanup`（经 base_evaluator.py:2114 分发，matte_cleanup 节点可达）在
    `operation` 不属于 erode/dilate/blur/clamp/invert/levels 时引用未定义名称，抛 NameError
    中断求值。节点 UI 的 ParamDef 约束了 6 个合法值，但序列化/迁移/损坏的图参数属于外部
    数据，可携带越界值，恰是 else 分支的兜底目标场景——兜底路径本身崩溃。
  - 建议: 模块顶部补 `import logging` + `logger = logging.getLogger(__name__)`；对越界
    operation 一并做白名单校验后再分发。
  - 置信度: 高

- [AUD-6101] [Major] keylight 降级路径丢失用户 screen_color，恒用默认绿
  - 位置: src/features/composite/evaluators/keyer_evaluator.py:200-208（发送方）与 314-317（接收方）
  - 证据:
    ```python
    legacy = {
        "screen_color": params.get("screen_color", (0.0, 1.0, 0.0)),
        "tolerance": params.get("clip_black", 0.0) * 0.5 + 0.2,
        ...
    }
    return self._apply_chroma_keyer(input_tex, legacy, w, h)
    ...
    prog['u_screen_color'].value = (
        float(params.get("screen_color_r", 0.0)),
        float(params.get("screen_color_g", 1.0)),
        float(params.get("screen_color_b", 0.0)))
    ```
  - 影响: `_apply_chroma_keyer` 只读取 `screen_color_r/g/b` 键（ChromaKeyerNode 的参数模型，
    已核实节点确实使用这三个键），不识别元组键 `screen_color`。当 keylight shader 不可用走
    降级路径时，用户设置的屏幕色被静默替换为默认 (0,1,0) 绿幕，抠除结果错误。
  - 建议: 降级字典把元组拆为 `screen_color_r/g/b` 三个键，或在 `_apply_chroma_keyer` 中
    兼容读取元组形式的 `screen_color`。
  - 置信度: 高

- [AUD-6102] [Suggestion] `_apply_difference_keyer` 纹理单元 1 的解绑不在 finally 中
  - 位置: src/features/composite/evaluators/keyer_evaluator.py:359-389
  - 证据:
    ```python
    self._bind_external_texture(input_a.texture_id)
    prog['u_texture_a'].value = 0
    from ..core.gl_bind import bind_texture_unit
    bind_texture_unit(input_b.texture_id, 1)
    ...
    self._render_quad(prog)
    bind_texture_unit(0, 1)
    ```
  - 影响: `_render_quad` 或 uniform 赋值抛异常时单元 1 保持绑定；同文件
    `_apply_light_wrap`（第 53-80 行）已示范 try/finally 模式，此处不一致。残留绑定可能
    指向后续被回收的纹理。
  - 建议: 与 `_apply_light_wrap` 对齐，用 try/finally 包裹渲染并复位单元 1。
  - 置信度: 中

- [AUD-6103] [Suggestion] matte_cleanup 的 size/iterations 只做下限钳制，无上限校验
  - 位置: src/features/composite/evaluators/keyer_evaluator.py:466-473
  - 证据:
    ```python
    result = self._apply_erode_dilate(input_tex, {
        "operation": operation,
        "size": max(1, int(params.get("size", 3))),
        "iterations": max(1, int(params.get("iterations", 1))),
        ...
    ```
  - 影响: 节点 ParamDef 上限 50，但求值器信任 `_effect_params` 原值；外部（损坏图/脚本）
    传入超大 size×iterations 时形态学按迭代循环执行，4K 下可造成长时间 GPU 占用。
    违反"外部数据须验证"约束的防线应落在求值边界。
  - 建议: 与节点约束一致地加上限（如 size≤50、iterations≤8），越界回落默认并告警。
  - 置信度: 中

### 通过项
- 线程安全：全部 GL 调用（acquire/fbo.use/clear/bind/render）经由渲染线程路径，无 UI 线程 OpenGL 操作
- GPU 资源生命周期：输出纹理统一走 `_resource_pool.acquire_texture` 池化；无直接 release 泄漏点
- GLSL/HDR 正确性：keylight/luma_keyer/chroma_keyer/difference_keyer/matte/matte_cleanup/light_wrap 的 uniform 名与类型已与 shaders.py 逐一比对一致；无多余 clamp(0,1) 作用于 HDR 颜色数据（仅 alpha/matte 语义处 clamp，符合规范）
- `u_output_mode` 语义：luma/chroma/difference 用 `1 if matte else 0`，`_apply_matte` 用反向映射 `0 if matte else 1`，均已对照各自 shader 注释（_MATTE_FRAGMENT: 0=matte,1=result；其余 0=result,1=matte）确认正确
- dissolve/差分键控的 premultiplied 元数据标记与 shader 输出（result=预乘、matte=灰度）一致
- 信号防递归：纯求值 mixin，不接触 Qt 控件，不适用且无违规
- 状态提交一致性：输出新建 TextureResult（revision 默认 0），与兄弟求值器约定一致，不触碰 undo/持久化
- `_apply_keyer` 三种模式输入端口（_effect_input / _effect_input_a/_b）与 KeyerNode 端口（image/reference）及 base_evaluator 分发键一致（已核实）

---

## src/features/composite/evaluators/keyframe_evaluator.py (行数: 31)

### 发现

- [AUD-6104] [Suggestion] 占位 mixin 残留死导入与空 TYPE_CHECKING 块
  - 位置: src/features/composite/evaluators/keyframe_evaluator.py:13-18
  - 证据:
    ```python
    from typing import Dict, TYPE_CHECKING
    from ..core.graph_snapshot import NodeSnapshot
    if TYPE_CHECKING:
        pass
    ```
  - 影响: `Dict`、`NodeSnapshot` 均未使用，徒增导入与循环依赖面（graph_snapshot 为运行时
    导入而非 TYPE_CHECKING 保护）。
  - 建议: 删除未用导入与空块，或将 NodeSnapshot 移入 TYPE_CHECKING 内。
  - 置信度: 高

### 通过项
- 通过（无逻辑）：类体为 `pass` 占位，插值逻辑按文档位于 BaseEvaluatorMixin._interpolate_parameters，不构成执行风险

---

## src/features/composite/evaluators/merge_evaluator.py (行数: 311)

### 发现

- [AUD-6105] [Critical] `ctx.clear(color=1.0)` 传标量导致 TypeError，DeepMerge 深度合成主路径必崩
  - 位置: src/features/composite/evaluators/merge_evaluator.py:211
  - 证据:
    ```python
    out_depth_tex = self._resource_pool.acquire_depth_texture(w, h)
    depth_fbo = self.fb_manager.get_fbo(out_depth_tex)
    depth_fbo.use()
    self.ctx.viewport = (0, 0, w, h)
    self.ctx.clear(color=1.0)
    ```
  - 影响: 项目 venv 为 moderngl 5.12.0，其 `Context.clear` 实现为
    `if color is not None: red, green, blue, alpha, *_ = tuple(color) + (0.0,)*4`，
    `tuple(1.0)` 抛 `TypeError: 'float' object is not iterable`。该行在
    `has_depth_a or has_depth_b` 为真时执行，而 DeepMergeNode 只有在任一输入带深度时才
    发出 `_effect_type: "deep_merge"` 标记（deep_merge_node.py:150-198）——即深度合成
    功能的每一次求值都在 RGBA 渲染完成后于深度输出分配处抛异常，节点渲染失败。
  - 建议: 改为 `self.ctx.clear(depth=1.0)`（或仅 `self.ctx.clear()`，depth 默认即 1.0）；
    补一条深度路径回归测试。
  - 置信度: 高

- [AUD-6106] [Major] deep_merge shader 缺失时的兜底返回错误尺寸元数据
  - 位置: src/features/composite/evaluators/merge_evaluator.py:125-138
  - 证据:
    ```python
    w, h = self._resolve_merge_dims(input_a, input_b, format_policy)
    prog = self.utility_shaders.get('deep_merge')
    if prog is None:
        return {
            "result": TextureResult(
                texture_id=input_a.texture_id,
                width=w, height=h, ...
    ```
  - 影响: 默认 `format_policy="from_b"` 时 w/h 取自 input_b 画布，而返回的 `texture_id`
    是 input_a 的纹理。两输入尺寸不同时，TextureResult 声明的 width/height 与实际 GL
    纹理不符，下游 merge/upsample 按 UV 缩放采样几何错误（拉伸/错位）。
  - 建议: 兜底路径直接沿用 input_a 自身的 width/height/bbox 元数据，或干脆返回
    `{"result": input_a}` 保持原样。
  - 置信度: 高（缺陷确定；触发条件为 deep_merge 程序编译失败）

- [AUD-6107] [Suggestion] `alpha_clamp` 参数读取后从未下发，死参数
  - 位置: src/features/composite/evaluators/merge_evaluator.py:176-177
  - 证据:
    ```python
    alpha_policy = effect_params.get("alpha_policy", "auto")
    alpha_clamp = effect_params.get("alpha_clamp", True)
    if alpha_policy == "premult_force":
    ```
  - 影响: 已核对 `_DEEP_MERGE_FRAGMENT` 未声明 `u_alpha_clamp`（该 uniform 属于 blend
    shader），故无陈旧 uniform 风险，但读取即弃会让后续维护者误以为深度合成支持该参数。
  - 建议: 删除该读取，或注释说明深度合成不支持 alpha_clamp。
  - 置信度: 高

- [AUD-6108] [Suggestion] deep_merge 输入仅判 None 未判 is_valid，与 dissolve 校验不一致
  - 位置: src/features/composite/evaluators/merge_evaluator.py:102-104（对比 41-44）
  - 证据:
    ```python
    if not input_a or not input_b:
        result = outputs.get("result")
        return {"result": result} if result and isinstance(result, TextureResult) else {}
    # 对比 _apply_dissolve:
    if input_a is None or not input_a.is_valid:
        return {"result": input_b} if input_b else {}
    ```
  - 影响: TextureResult 未定义 `__bool__`（已核实 base_node.py:76-135），
    `not input_a` 仅识别 None。texture_id=0 的无效结果会进入渲染，绑定 GL 名 0 后
    采样得到未定义内容，静默输出黑图而非走兜底。
  - 建议: 与 dissolve 对齐，补充 `.is_valid` 判定。
  - 置信度: 中

- [AUD-6109] [Suggestion] 多输入纹理单元（1-4）的复位不在 try/finally 中
  - 位置: src/features/composite/evaluators/merge_evaluator.py:67-79、146-202
  - 证据:
    ```python
    self._bind_external_texture(input_a.texture_id)
    from ..core.gl_bind import bind_texture_unit
    bind_texture_unit(input_b.texture_id, 1)
    prog['u_texture_a'].value = 0
    ...
    self._render_quad(prog)
    bind_texture_unit(0, 1)
    ```
  - 影响: 渲染或 uniform 赋值异常时，deep_merge 最多遗留 4 个单元（1-4）脏绑定；
    dissolve 遗留单元 1。渲染线程恢复后这些单元可能指向已回收纹理。
  - 建议: 统一 try/finally 复位所有显式绑定的单元（参照 keyer_evaluator `_apply_light_wrap`）。
  - 置信度: 中

- [AUD-6110] [Suggestion] depth_output shader 缺失时返回"仅清空"的深度纹理且标记 has_deep_data
  - 位置: src/features/composite/evaluators/merge_evaluator.py:204-214、254-255
  - 证据:
    ```python
    if has_depth_a or has_depth_b:
        out_depth_tex = self._resource_pool.acquire_depth_texture(w, h)
        ...
        self.ctx.clear(color=1.0)
        depth_prog = self.utility_shaders.get('depth_output')
        if depth_prog:
            ...
    # 返回时无条件：
    depth_texture_id=out_depth_tex.glo if out_depth_tex else None,
    has_deep_data=(has_depth_a or has_depth_b),
    ```
  - 影响: depth_prog 为 None 时深度纹理只被清空、从未渲染，却作为有效深度输出传递，
    下游 ZBlur/Fog 等深度工具会拿到全 1.0（最远）的假深度。
  - 建议: depth_prog 缺失时置 `depth_texture_id=None`、`has_deep_data=False` 并告警。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：RGBA/深度输出均从资源池获取，深度 FBO 用毕恢复主输出 FBO（第 239-243 行），视口恢复正确
- GLSL/HDR 正确性：dissolve 的 mix 方向（mix=0→B，mix=1→A）与 `_DISSOLVE_FRAGMENT` 注释及 `mix(b, a, u_mix)` 一致；deep_merge 的 u_a_straight/u_b_straight 语义与 shader 一致；无 HDR clamp 违规
- 输入验证：dissolve 对 None 与 is_valid 双重校验；`_resolve_merge_dims/format/bbox` 全部 max(1,...) 下限保护
- `_apply_set_alpha` 的 mask 通路成立：base_evaluator.py:2073-2074 会把 `_effect_mask` 注入 `effect_params["_mask_tex"]`（已核实分发器与 set_alpha_node.py:100）
- set_alpha 输出标记 straight（alpha_premultiplied=False）符合"新 alpha 与 RGB 不再对应"的语义并在 docstring 说明
- 线程安全：全部操作位于渲染线程；无 Qt 信号/控件访问
- 状态提交一致性：新建 TextureResult 元数据（format/bbox/color_space/depth）字段来源明确，无 undo/持久化耦合

---

## src/features/composite/evaluators/shader_library.py (行数: 345)

### 发现

- [AUD-6111] [Critical] STANDARD_NAMES 漏登 4 个已注册 shader，Roto/MaskMerge/运动模糊累积恒走降级路径
  - 位置: src/features/composite/evaluators/shader_library.py:91-121（STANDARD_NAMES）与 181、183、186、206（_SHADER_SOURCES）
  - 证据:
    ```python
    # _SHADER_SOURCES 中存在：
    "motion_blur_accum": (_PASSTHROUGH_VERTEX, _MOTION_BLUR_ACCUM_FRAGMENT),
    "roto":              (_PASSTHROUGH_VERTEX, _ROTO_FRAGMENT),
    "mask_merge":        (_PASSTHROUGH_VERTEX, _MASK_MERGE_FRAGMENT),
    "mesh_pbr":          (_MESH_VERTEX, _MESH_PBR_FRAGMENT),
    # 但 STANDARD_NAMES 列表（91-121 行）不包含上述任何名字。
    ```
    且 `utility_shaders` 的唯一写入点是 base_evaluator.py:3435-3438：
    ```python
    for name in ShaderLibrary.STANDARD_NAMES:
        prog = self._shader_lib.get_program(name)
        if prog:
            self.utility_shaders[name] = prog
    ```
    （全仓 grep 确认无第二个填充点；CompositeRenderer/CompositeExportRenderer 均只调 `_load_shaders()`。）
  - 影响: filter_evaluator.py:715 `utility_shaders.get("roto")`、:1169 `get("mask_merge")`、
    base_evaluator.py:1863 `get('motion_blur_accum')` 永远取到 None——Roto 节点恒返回输入
    直通（蒙版不生效）、MaskMerge 恒返回 A 输入（忽略 B 与布尔运算）、运动模糊多采样累积
    恒被跳过。属静默渲染错误，且"mesh_pbr 注释自称当前默认 PBR 程序"同样不可达（该名字
    目前无消费方，暂无运行时影响）。
  - 建议: 把 `roto`、`mask_merge`、`motion_blur_accum`（以及如需启用 PBR 的 `mesh_pbr`）
    加入 STANDARD_NAMES；并加一个单元测试断言 `{k[0] for k in _SHADER_SOURCES} ⊇/== set(STANDARD_NAMES)`
    防止再次漂移。
  - 置信度: 高

- [AUD-6112] [Suggestion] `reload`/`reload_all` 立即 release 可能在用程序，违背 deferred-release 模型
  - 位置: src/features/composite/evaluators/shader_library.py:270-314
  - 证据:
    ```python
    old_prog = self._cache.pop(key, None)
    if old_prog is not None:
        try:
            old_prog.release()
        except Exception:
            pass
    ```
  - 影响: 持锁 release 期间，另一线程（文档自称渲染+导出双线程并发）可能正用该 program
    走 `_render_quad` → 对已删除 GL 对象调用 draw。项目铁律是 deferred-release；当前
    全仓未发现外部调用方（仅 reload_all→reload 自引用），风险处于休眠状态。
  - 建议: 接入统一的延迟释放队列；或至少在 release 前与渲染帧同步（如等待渲染空闲回调）。
  - 置信度: 中

- [AUD-6113] [Suggestion] `_ensure_fused_source` 就地变更类级共享 `_SHADER_SOURCES`
  - 位置: src/features/composite/evaluators/shader_library.py:217-227
  - 证据:
    ```python
    self._fused_loaded = True
    try:
        from ..core.shaders import _FUSED_FRAGMENT, _FUSED_COLOR_FRAGMENT
        self._SHADER_SOURCES["fused"] = (_PASSTHROUGH_VERTEX, _FUSED_FRAGMENT)
        self._SHADER_SOURCES["fused_color"] = (_PASSTHROUGH_VERTEX, _FUSED_COLOR_FRAGMENT)
    ```
  - 影响: `_SHADER_SOURCES` 是类属性，viewport 与 export 两个实例（各自 ctx）共享同一
    dict 并在锁外写入；GIL 下赋值原子、结果幂等，暂无实际竞争，但属跨实例可变共享状态，
    未来任何读-改-写都会引入竞争。
  - 建议: 在模块导入或类定义时一次性完成 fused 源注册；或把源表改为实例级/在锁内写入。
  - 置信度: 低

- [AUD-6114] [Suggestion] 类型注解缺失
  - 位置: src/features/composite/evaluators/shader_library.py:209、316-320
  - 证据:
    ```python
    def __init__(self, ctx):
        ...
    @property
    def cached_programs:
    ```
  - 影响: `ctx` 无注解（应为 `moderngl.Context`），`cached_programs` 缺返回注解
    （`List[str]`）；与项目"所有函数有类型注解"约束不符。
  - 建议: 补齐参数/返回注解。
  - 置信度: 高

### 通过项
- 线程安全：缓存读写全程 RLock 保护；reload 内嵌套调用 get_program 依赖 RLock 可重入性，正确
- `_compile` 的宽泛 `except Exception` 附带 warning 日志与 None 返回值，符合"不掩盖领域错误"的豁免形态（编译守卫）
- `_inject_defines` 正确插入在 `#version` 之后，未命中时插到第 0 行（源码均带 #version，安全）
- get_program 键含 (name, defines, version)，避免同名单不同 defines 的缓存冲突
- GPU 资源生命周期：`release()` 清空全部缓存并逐个释放，关闭路径完整

---

## src/features/composite/evaluators/tile_evaluator.py (行数: 160)

### 发现

- [AUD-6115] [Suggestion] bbox 缩放用 int() 截断且 upsample 假定等比缩放
  - 位置: src/features/composite/evaluators/tile_evaluator.py:57-64、99-107
  - 证据:
    ```python
    scaled_bbox = BBox(
        x=int(tex.bbox.x * scale),
        y=int(tex.bbox.y * scale),
        width=int(tex.bbox.width * scale),
        height=int(tex.bbox.height * scale),
    )
    ...
    scale = float(target_w) / float(src_w)
    upscaled_bbox = BBox(
        x=int(tex.bbox.x * scale), ...
    ```
  - 影响: 反复 down→up 会因截断累积 1px 级漂移（bbox 与实际内容错位）；upsample 的
    纵向 scale 直接借用宽度比，若 target 与 src 宽高比不一致，y/height 缩放错误。
    LOD 场景通常等比，故实际影响有限。
  - 建议: 用 round 并分别计算 x/y 两个 scale（或断言等比）。
  - 置信度: 中

### 通过项
- 线程安全：纯渲染线程 GL 操作，无共享可变状态
- GPU 资源生命周期：dst 纹理走资源池；提前返回路径（dst>=src、prog None）不产生孤儿分配
- LOD 防护：lod_level<=0 直通；scale 对 lod 1/2/3 分别为 0.5/0.25/0.125，max(1,...) 防零尺寸
- 元数据保真：format/alpha_premultiplied/color_space 逐字段透传，无 HDR clamp
- 输入验证：`isinstance(tex, TextureResult) and tex.is_valid` 后才降采样，非纹理项原样保留

---

## src/features/composite/evaluators/transform_evaluator.py (行数: 440)

### 发现

- [AUD-6116] [Suggestion] 函数体内 `import logging` 且每次调用重建 logger
  - 位置: src/features/composite/evaluators/transform_evaluator.py:126-127、203-204
  - 证据:
    ```python
    if prog is None:
        import logging
        logging.getLogger(__name__).warning("Transform shader not available, returning input")
    ...
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"Transform: uv_matrix=({m00:.3f},...")
    ```
  - 影响: 拖拽 Transform 时每帧执行模块查找与 getLogger；且每帧 debug 格式化一个长
    f-string（logger 未按级别短路前 f-string 已求值）。轻微性能与风格问题。
  - 建议: 模块顶部 `logger = logging.getLogger(__name__)`；debug 行改用 `%` 惰性格式化。
  - 置信度: 高

- [AUD-6117] [Suggestion] translate/scale/pivot 解包无形状校验
  - 位置: src/features/composite/evaluators/transform_evaluator.py:156-159
  - 证据:
    ```python
    tx_px, ty_px = params.get("translate", [0, 0])
    sx, sy = params.get("scale", [1, 1])
    rotation = params.get("rotation", 0.0)
    piv_px, piv_py = params.get("pivot", [0, 0])
    ```
  - 影响: TransformNode 正常提供二元列表（已核实 transform_node.py:366-376），但求值器
    面对的 `_effect_params` 属外部可序列化数据；键帧插值或损坏图给出错误形状时抛
    ValueError 中断求值而非回落默认。
  - 建议: 对长度异常的列表 try/except 或显式校验后回落默认值。
  - 置信度: 中

- [AUD-6118] [Suggestion] crop 矩形无边界与负值校验
  - 位置: src/features/composite/evaluators/transform_evaluator.py:366-380
  - 证据:
    ```python
    crop_x = params.get("crop_x", 0)
    crop_y = params.get("crop_y", 0)
    crop_w = max(1, params.get("crop_w", 512))
    crop_h = max(1, params.get("crop_h", 512))
    ...
    prog['u_crop_rect'].value = (float(crop_x), float(crop_y), float(crop_w), float(crop_h))
    ```
  - 影响: crop_x/y 可为负或超出源纹理范围，输出尺寸只下限保护；越界采样依赖纹理
    CLAMP 行为产生边缘拉伸，且 `outside` 模式下元数据（full_frame bbox）与内容可能不符。
  - 建议: 将矩形钳制到 [0, input 尺寸] 区间并告警越界。
  - 置信度: 中

### 通过项
- GLSL/HDR 正确性：矩阵数学经手工推导验证——`_build_transform_pixel_matrix`（Y-down 像素空间）、`_pixel_matrix_to_uv_matrix`（含 Y 翻转与纵横比交叉项）、packed-FBO rebase（x 用 `in_bbox.x - result_bbox.x`，y 用 `result_bbox.top - in_bbox.top`，与 GL Y-up bbox 的 top=y+height 语义自洽）均正确；无 clamp 引入
- GPU 资源生命周期：输出纹理池化；`_render_quad` 的 VAO 按 program 缓存（base_evaluator.py:3038-3056）
- tile/preview 双模式画布策略实现与 docstring 一致（tile 固定 canvas、preview 扩展 canvas + out_bbox rebase）；tile_ctx 坐标平移（163-167 行）正确
- `_apply_cs_transform` 对可选 uniform 的 `except (KeyError, AttributeError)` 属窄口径且逐处注释说明（u_primaries 固定 0 防陈旧值），不构成"宽泛 except 掩盖领域错误"
- `_apply_reformat` 三类 resize_mode 的 bbox 缩放/居中/clamped 逻辑自洽，preserve_bbox 分支齐全
- 返回端口键（"result"/"image"）与对应节点端口名核实一致（transform/cornerpin→result，cs_transform→image）
- 状态提交一致性：`result_format` 回退构造 ImageFormat 保证下游有稳定画布，无 undo/revision 耦合
- Python 版本：`int | None` 注解语法要求 3.10+，pyproject `requires-python = ">=3.10"` 满足
