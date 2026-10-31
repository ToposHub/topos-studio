# batch-264 审查报告 — src/features/shared/effects/

编号区间：AUD-26400–AUD-26499。审查日期：2026-09-28。
文件数：5（共 2,855 行），全部 Read 通读；关键疑点已用只读 grep 对照调用方
（`edit_gpu_preview_renderer.py`、`effect_catalog.py`、`base_node.py`）验证。

汇总：Critical 2 / Major 2 / Suggestion 7。

---

## src/features/shared/effects/effect_pass_runner.py (行数: 1602)

### 发现

- [AUD-26401] [Critical] `_run_separable` 的 H/V pass 写入目标固定为 scratch_b/scratch_a 且 V pass 将输入绑到 unit 1，链式调用时读写同一纹理形成 GL 反馈回路
  - 位置: src/features/shared/effects/effect_pass_runner.py:1049-1058（V pass）；同缺陷亦见 H pass 1037-1040
  - 证据:
    ```python
            self._scratch_a_fbo.use()
            self._ctx.viewport = (0, 0, width, height)
            self._ctx.clear(0.0, 0.0, 0.0, 0.0)
            self._scratch_b.use(0)
            self._bind_external_texture(input_glo, unit=1)
            self._set_uniform(prog_v, "u_texture", 0)
            self._set_uniform(prog_v, "u_original", 1)
    ```
  - 影响: 调用方（`edit_gpu_preview_renderer.py:4290` 附近）以
    `current_glo = runner.run(spec, current_glo, ...)` 链式传递前一效果输出。
    前一效果为 `_run_single_pass` 且输入非 scratch_a 时输出恰为 scratch_a
    （如常见链 [sharpen → blur]），此时 V pass 写 scratch_a 的同时又通过
    u_original（unit 1）采样 scratch_a；输入为 scratch_b 时（如
    [sharpen → denoise → blur]）H pass 写 scratch_b 的同时采样 scratch_b。
    渲染到同时被采样的附件属未定义行为——本项目自身在 `_run_single_pass`
    注释中记载该类反馈回路表现为黑帧/花屏（"two sharpens → black screen"），
    mask/mix<1 时 u_original 读到 clear 后的 0 值，画面被拉黑。对比之下，
    `_run_separable_downsampled`（fast tier）与 `_run_single_pass` 均做了
    输入/输出错开防护，唯独全分辨率 separable 路径没有。
  - 建议: 仿照 `_run_single_pass` 的 ping-pong 选择——当
    `input_glo == self._scratch_a.glo` 时让 V pass 写 scratch_c（或交换
    H/V 目标为 scratch_a→scratch_b 并将 u_original 改绑内部拷贝）；当
    `input_glo == self._scratch_b.glo` 时 H pass 目标改用 scratch_c。
    同时为该不变量补 golden-frame 回归（两个效果链 + blur）。
  - 置信度: 高

- [AUD-26402] [Critical] `_run_glow` pass 1 固定写 scratch_b，输入为 scratch_b 时读写同一纹理形成反馈回路（FX-M02 只修了 pass 2）
  - 位置: src/features/shared/effects/effect_pass_runner.py:1232-1237（对照防护代码 1227-1230）
  - 证据:
    ```python
            if input_glo == self._scratch_a.glo:
                out_tex, out_fbo = self._scratch_c, self._scratch_c_fbo
            else:
                out_tex, out_fbo = self._scratch_a, self._scratch_a_fbo

            # ── Pass 1: threshold + horizontal blur → scratch_b ──
            self._scratch_b_fbo.use()
    ```
    pass 1 随后 `self._bind_external_texture(input_glo)`（unit 0）并渲染。
  - 影响: FX-M02 的 scratch_c 防护只覆盖「输入==scratch_a 时 pass 2 的输出
    别名」。链 [效果1 → 效果2 → glow]（效果2 为 `_run_single_pass`，输入
    scratch_a，输出 scratch_b）时，glow 的输入即 scratch_b：pass 1 写
    scratch_b 同时采样 scratch_b —— 同一反馈回路类缺陷，辉光通道读到
    clear 后的 0，输出黑帧/损坏。
  - 建议: pass 1 目标做同样的别名检查：输入为 scratch_b 时 pass 1 改写
    scratch_a 或 scratch_c（并相应调整 pass 2 的 bloom 绑定与输出选择，
    保持「输出 ∉ {输入, bloom}」三元互异不变量）。
  - 置信度: 高

- [AUD-26403] [Suggestion] 非 blur 的 SEPARABLE_PASSES spec 直接进 `_run_separable` 会在 `packed.pop("u_radius_x")` 处 KeyError，被顶层 catch-all 吞成静默 no-op
  - 位置: src/features/shared/effects/effect_pass_runner.py:1013-1014（触发入口 493-494）
  - 证据:
    ```python
            if spec.shader_kind == ShaderKind.SEPARABLE_PASSES:
                return self._run_separable(spec, input_glo, params, width, height, dtype=dtype)
    ```
    ```python
        radius_x = packed.pop("u_radius_x")
        radius_y = packed.pop("u_radius_y")
    ```
  - 影响: `_pack_params` 对未注册 dispatch_key 走 identity packing（1519-1522），
    不会产生 `u_radius_x/y`。当前 catalog 中 separable 只有 blur（有专用
    dispatch 分支）与 glow（有专用分支），故为潜伏缺陷；新增 separable 效果
    忘记注册 packer 时效果直接静默失效，仅一条 warning 日志。
  - 建议: `packed.pop("u_radius_x", None)` 带默认值，或在 `_run_separable`
    入口断言 packer 已注册 / 对 identity packing 显式报错。
  - 置信度: 高（代码路径确定；当前无可触发的 spec）

- [AUD-26404] [Suggestion] `_bind_external_texture` 每次调用都新建 `QOpenGLFunctions_4_1_Core` 并重跑 `initializeOpenGLFunctions`
  - 位置: src/features/shared/effects/effect_pass_runner.py:785-792
  - 证据:
    ```python
            from PyQt6.QtGui import QOpenGLContext
            if QOpenGLContext.currentContext() is not None:
                from PyQt6.QtOpenGL import QOpenGLFunctions_4_1_Core
                gl = QOpenGLFunctions_4_1_Core()
                if gl.initializeOpenGLFunctions():
                    gl.glActiveTexture(0x84C0 + unit)  # GL_TEXTURE0 + unit
                    gl.glBindTexture(0x0DE1, texture_glo)  # GL_TEXTURE_2D
    ```
  - 影响: 该函数在每帧每个效果每 pass 都被调用（4K 实时基准下每帧多次），
    每次构造 Qt 函数对象并重新解析函数表，属热路径上的重复开销。
  - 建议: 按 context 缓存 `QOpenGLFunctions_4_1_Core` 实例（首次成功后
    复用；context 切换时失效重建），或改用 moderngl 原生
    `ctx.raw_gl`/预取的 PyOpenGL 函数指针。
  - 置信度: 高（代码事实）；性能影响量为中

- [AUD-26405] [Suggestion] `_ensure_blit_program` / `_ensure_combine_program` 中途失败时已创建的 program/vbo 泄漏
  - 位置: src/features/shared/effects/effect_pass_runner.py:1359-1367（combine 同型 1387-1395）
  - 证据:
    ```python
        try:
            prog = self._ctx.program(
                vertex_shader=_PASSTHROUGH_VERTEX, fragment_shader=_BLIT_FRAGMENT)
            vbo = self._ctx.buffer(_QUAD_VERTICES.tobytes())
            vao = self._ctx.vertex_array(
                prog, [(vbo, "2f 2f", "in_vert", "in_uv")])
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash
            logger.warning("EffectPassRunner: blit program failed: %s", exc)
            return False
    ```
  - 影响: 若 `ctx.buffer` 或 `ctx.vertex_array` 抛异常，已创建的 prog/vbo
    既未释放也未存入任何字段，成为该 context 生命周期内的孤儿 GPU 对象。
    对比 `_compile_spec` 的 separable 分支在失败时显式 `prog_h.release()`。
  - 建议: 失败分支内对已创建对象逐个 try/release。
  - 置信度: 高

- [AUD-26406] [Suggestion] `run()` 顶层 `except Exception` 将参数打包等领域错误与着色器错误一并降级为 no-op
  - 位置: src/features/shared/effects/effect_pass_runner.py:496-501
  - 证据:
    ```python
        except Exception as exc:  # noqa: BLE001 — render thread must survive
            logger.warning(
                "EffectPassRunner: effect '%s' failed (%s); returning input",
                spec.effect_type, exc,
            )
            return input_glo
    ```
  - 影响: 渲染线程存活策略本身合理，但捕获范围把 KeyError/TypeError 等
    程序缺陷（如 AUD-26403）与真实的 GL 失败混为同一条 warning，违反
    「不得宽泛 except 掩盖领域错误」铁律的精神——缺陷被静默降级而非暴露。
  - 建议: 在进入渲染前先完成 `_pack_params`（打包错误单独上报
    record_compile_failure/诊断码）；渲染段再捕获 GL 异常，或对
    KeyError/TypeError 以更高日志级别（error + 诊断计数）区分。
  - 置信度: 高

### 通过项
- 线程安全：单线程-per-context 契约在模块 docstring 明确，`_lock` 使用与该契约一致，`_last_output_tex` 生命周期文档化（同帧同线程立即读取）
- GPU 资源生命周期：`release()` 覆盖 program/VAO/VBO/scratch a-c/ds a-b/blit/combine 全部对象且逐个 try 包裹；LRU 逐出同步清理 fallback 表与 VAO
- GLSL 正确性：内置 vertex/blit/combine 源码字面量均带 `.0`，无循环非常量边界
- HDR 正确性：dtype f1/f2 缓存键分离（FX-M01），HDR 链路 dtype="f2" 由调用方传入，clear 无额外 clamp
- 输入验证：`run()` 入口校验 width/height/glo；`_label_to_index` 对损坏保存值容错降级；`u_shutter` clamp 到 [0,2]；blur_type 未知值回退 box
- 程序缓存：cache key 含 source_hash（插件热更新触发重编译）、fallback-to-last-known-good、compile failure 诊断上报不含路径/源码
- erode 迭代 ping-pong 与 fast-tier downsampled 路径的输入/输出别名防护正确
- 信号防递归 / 状态提交一致性：不适用（无 Qt 信号、无 undo/持久化职责）

---

## src/features/shared/effects/effect_spec.py (行数: 294)

### 发现

- [AUD-26407] [Suggestion] `EffectSpec` 为 frozen dataclass 但持有三个可变 dict 字段，且 `hash(spec)` 会因 dict 字段抛 TypeError
  - 位置: src/features/shared/effects/effect_spec.py:121-123（类装饰器 65）
  - 证据:
    ```python
    shader_variants: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    variant_shader_kinds: Dict[str, str] = field(default_factory=dict)
    param_to_uniform: Dict[str, str] = field(default_factory=dict)
    ```
  - 影响: frozen+eq 使 dataclass 自动生成基于全部字段的 `__hash__`，dict
    不可哈希 → 任何 `hash(spec)` 调用抛 TypeError；同时外部代码可就地修改
    这三个 dict，破坏"不可变共享描述"契约（三个渲染器共享同一 spec 实例）。
    当前仓库只 `hash(sources)`（runner:604），未哈希 spec，故为设计性风险
    而非现行崩溃。
  - 建议: 三个映射字段改为 `Tuple[Tuple[str, ...], ...]` 或在文档中明示
    "构造后不得修改"；如需可哈希可加 `eq=True, unsafe_hash=False` 显式化。
  - 置信度: 高（行为确定）；影响为中低

### 通过项
- `__post_init__` 在构造期校验源数/kind 一致性（含 variant 与 dangling kind 检查），防御好
- `default_parameters` 纯函数不修改 spec；hidden 参数按 ptype 给出类型正确回退值
- `uniform_manifest` 对无 GLSL 投影的 ptype 显式 ValueError（G5 fail-explicit，符合不静默降级要求）；`GLSL_UNIFORM_TYPES` 与 DCTL ABI 的 checkbox/select→int 约定一致
- `PluginBinding` 使用 default_factory 而非可变默认值；类型注解完整
- 线程安全 / GPU 资源 / 信号防递归 / 异常路径 / 性能：纯数据模块，不适用

---

## src/features/shared/effects/effect_tiers.py (行数: 44)

### 发现
（无）

### 通过项
- 通过（无逻辑）：纯常量（FAST_TIER_MIN_RADIUS/FAST_TIER_SCALE/上限）+ 纯函数 `effective_denoise_radius`，含 `max(1, int(radius))` 输入钳制，未知 quality 字符串按 normal 处理；导出供 GPU packer 与 CPU fallback 双侧共用，契约单一来源

---

## src/features/shared/effects/plugin_identity.py (行数: 524)

### 发现

- [AUD-26408] [Major] `PluginIdentity.from_dict` 不验证反序列化数据类型，损坏的项目文件可使 `resolve_plugin_status` 抛异常，违反其 "never raises" 契约
  - 位置: src/features/shared/effects/plugin_identity.py:138-147（调用点 340）
  - 证据:
    ```python
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "PluginIdentity":
        """Reconstruct from a serialized dict."""
        return PluginIdentity(
            plugin_id=data.get("plugin_id", ""),
            version=data.get("version", ""),
            content_hash=data.get("content_hash", ""),
    ```
    以及 146 行 `capabilities=tuple(data.get("capabilities", ()) or ())`。
  - 影响: `stored_identity` 来自项目文件（外部数据，铁律要求验证）。若被
    存成字符串/列表（手改或旧版本写入异常），`data.get` 抛
    AttributeError；`capabilities` 为非可迭代标量时 `tuple()` 抛
    TypeError。`resolve_plugin_status` docstring 承诺 "It never raises —
    all failure modes produce a report"（275-276），实际逃逸异常会沿项目
    加载 / 导出 preflight 路径上抛，可能中断整个加载流程而非给出
    MISSING/LEGACY 报告。
  - 建议: `from_dict` 入口 `isinstance(data, dict)` 检查（否则返回空
    identity 或交由调用方按 LEGACY 处理）；capabilities 逐项过滤为 str。
  - 置信度: 高（代码事实）；实际触发需损坏数据，概率中低

- [AUD-26409] [Suggestion] `validate_effect_instances` 的 `catalog_lookup: Any` 类型过宽，且用重建 report 的方式回填 effect_id
  - 位置: src/features/shared/effects/plugin_identity.py:415-420（重建 451-465）
  - 证据:
    ```python
    def validate_effect_instances(
        instances: Sequence[Any],
        *,
        catalog_lookup: Any,
        compile_failures: Optional[set] = None,
    ) -> List[PluginStatusReport]:
    ```
  - 影响: `catalog_lookup` 实际契约是 `Callable[[str], Optional[EffectSpec]]`，
    Any 掩盖传错签名的调用方；`resolve_plugin_status` 内 `effect_id=""` 占位
    后由本函数再整份重建 PluginStatusReport，重复构造且两处字段需人工保持
    同步。
  - 建议: 类型收紧为
    `Callable[[str], Optional["EffectSpec"]]`；`resolve_plugin_status` 增加
    可选 `effect_id` 参数避免重建。
  - 置信度: 高

### 通过项
- 身份构造：`build_plugin_identity` 的 plugin_id 取值与 catalog 写入一致（`plugin_id=f"plugin:{id}"` 等于 effect_type，已对照 effect_catalog.py:524/541 核实，342 行的 id 匹配检查不会误报）
- 哈希确定性：content hash 用 NUL 分隔 + UTF-8；schema hash 用 sort_keys + default=str，跨平台稳定
- 状态矩阵：MISSING/UNSUPPORTED_BACKEND/COMPILE_ERROR/SCHEMA_MISMATCH 阻断导出，VERSION_MISMATCH/CONTENT_CHANGED/LEGACY 不阻断，语义自洽且与注释一致
- `validate_effect_instances` / `migrate_legacy_identity` 不 mutate EffectInstance（迁移经调用方 command 保持可 undo）；纯数据无 GL/Qt 依赖，线程安全不适用
- 性能：单次 SHA-256，无热点

---

## src/features/shared/effects/preview_policy.py (行数: 391)

### 发现

- [AUD-26410] [Major] `_pick_degradation` 的 PARAMETER_CAP 分支在参数已达标时提前 return FULL，永远不会尝试该效果声明的 LOWER_RESOLUTION / BYPASS
  - 位置: src/features/shared/effects/preview_policy.py:343-351
  - 证据:
    ```python
            # Params already within caps → no degradation needed.
            return (
                DegradationMode.FULL,
                None,
                "params already within caps",
            )

        # Next: LOWER_RESOLUTION.
        if DegradationMode.LOWER_RESOLUTION in decl.supported_modes:
    ```
  - 影响: glow 声明了 `{FULL, PARAMETER_CAP, LOWER_RESOLUTION}`（161-170 行）。
    播放中 glow 半径 ≤8（无需 cap）但超出帧预算时（效果堆叠、4K
    resolution_factor 放大成本），本应降级半分辨率却返回 FULL，预算策略失效、
    掉帧。denoise（{FULL, BYPASS}）不受影响，但凡同时声明 cap 与其他降级
    模式的效果均受影响。
  - 建议: 参数已达标时不提前 return，改为继续向下探测
    LOWER_RESOLUTION / BYPASS；仅当无任何可用降级时才返回 FULL。
  - 置信度: 高

- [AUD-26411] [Suggestion] `register_degradation_decl` 无保护地改写模块级 `_BUILTIN_DECLS`，可覆盖内置声明且无并发防护
  - 位置: src/features/shared/effects/preview_policy.py:214-216
  - 证据:
    ```python
    def register_degradation_decl(effect_type: str, decl: DegradationDecl) -> None:
        """Register or override a degradation declaration (for plugins)."""
        _BUILTIN_DECLS[effect_type] = decl
    ```
  - 影响: 插件注册若与渲染线程的 `get_degradation_decl` 读取并发，dict
    写入在 CPython 下原子但缺乏发布语义；同时用插件键意外命中内置键名
    （"blur" 等）会静默改变内置效果行为。
  - 建议: 命中 `_BUILTIN_DECLS` 保留键时 log warning；或维护独立的
    `_PLUGIN_DECLS` 字典分层查找。
  - 置信度: 中

### 通过项
- 决策不可变：`PreviewPolicyDecision` frozen，`resolve_params_for_decision` / `_pick_degradation` 均不 mutate 原始 params（capped 为 dict(params) 拷贝），符合"决策永不回写 EffectInstance.params"的设计目标
- 暂停/导出/单帧恢复 FULL（force_full or not is_playing），预算扣减逻辑自洽（仅在 cost≤remaining 时扣减全额，不会出现负预算）
- `DegradationDecl.__post_init__` 强制补入 FULL 模式（fail-safe）
- 引擎无状态、纯算术，无 GPU/Qt 依赖，线程安全（除 AUD-26411 的注册路径）
- 信号防递归 / 状态提交一致性：不适用（frame-local 决策对象，不触 undo/持久化）
