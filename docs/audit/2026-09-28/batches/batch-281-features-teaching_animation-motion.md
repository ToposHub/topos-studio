# 批次报告：batch-281 — src/features/teaching_animation/motion//

- 审查日期：2026-09-28
- 编号区间：AUD-28100–AUD-28199
- 文件数：8（1,640 行），全部通读
- 统计：Critical 0 · Major 9 · Suggestion 15

---

## src/features/teaching_animation/motion/__init__.py (行数: 108)

### 发现
- [AUD-28123] [Suggestion] 包级 `__all__` 未导出 `motion_semantics` / `read_motion_plans`
  - 位置: src/features/teaching_animation/motion/__init__.py:65-72
  - 证据:
    ```python
    from .motion_commands import (
        ApplyMotionPresetCommand,
        SetStaggerIntervalCommand,
        StaggerElementsCommand,
        StaggerPlan,
        compile_preset_to_track,
        scene_diff,
    )
    ```
  - 影响: `motion_commands.__all__` 含 `motion_semantics`、`read_motion_plans`，但包入口未再导出；模块 docstring 宣称的公共面与包级 API 不一致。当前调用方（`ui/scene_inspector.py:409`、`tests/render/test_animation_property_coverage.py`）直接从子模块导入得以绕过，新调用方经包入口访问会失败。
  - 建议: 在包级 import 与 `__all__` 中补齐这两个名字，或在 `motion_commands.__all__` 中移除以统一公共面。
  - 置信度: 高（事实确定，影响小）

### 通过项
- 纯 re-export 层，无逻辑；导入名与 `__all__` 其余条目逐一核对一致
- 不涉及线程安全、GPU 资源、GLSL/HDR、Qt 信号维度

---

## src/features/teaching_animation/motion/beat.py (行数: 114)

### 发现
- [AUD-28111] [Major] `format_timecode` 将有理帧率取整，分数帧率（如 30000/1001）时间码系统性漂移
  - 位置: src/features/teaching_animation/motion/beat.py:87-90
  - 证据:
    ```python
    fps_float = fps_num / fps_den
    fps_int = max(1, int(round(fps_float)))
    total_seconds_floor = frame // fps_int
    fields = frame % fps_int
    ```
  - 影响: API 显式提供 `fps_num/fps_den` 以支持有理帧率，但实现立刻 `round()` 取整。29.97fps（30000/1001）下按 30 计：每真实 1 分钟（1798 帧）累计约 3.6 帧偏差，显示 `00:00:59:28` 而真实位置约 `00:01:00:00`；时间码、beat 对齐提示随时长线性漂移。整数帧率（24/30/60）不受影响。
  - 建议: 用整数运算直接按 `fps_num/fps_den` 换算（`total_seconds_num = frame * fps_den`，秒/帧字段由整数除法导出），或显式拒绝 `fps_den != 1` 的输入并说明。
  - 置信度: 高（行为可复算；实际影响取决于工程是否使用分数帧率）

- [AUD-28112] [Suggestion] 未使用导入：`field`、`Mapping`
  - 位置: src/features/teaching_animation/motion/beat.py:13-14
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Mapping
    ```
  - 影响: `BeatGrid`/`Beat` 均未用 `field`，全文件无 `Mapping` 引用；死导入增加噪音。
  - 建议: 删除两个未使用导入。
  - 置信度: 高

### 通过项
- 线程安全：全 frozen dataclass + 纯函数，无可变共享状态
- 输入验证：`__post_init__` 对 bpm/时号/fps/anchor/frame 非负均有校验；`format_timecode` 拒绝负 frame
- 状态提交一致性：`to_dict` 与字段一一对应，round-trip 无损
- 不涉及 GPU 资源、GLSL/HDR、Qt 信号、取消路径

---

## src/features/teaching_animation/motion/easing.py (行数: 233)

### 发现
- [AUD-28100] [Major] `elastic_in` 曲线数学错误：起点后立即跌至 ≈-1、终点前≈0.0005 后跳变到 1
  - 位置: src/features/teaching_animation/motion/easing.py:133-138
  - 证据:
    ```python
    def elastic_in(t: float) -> float:
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        return math.sin((t - 0.075) * (2.0 * math.pi / 0.3)) * (2.0 ** (-10.0 * t)) + 1.0 * 0  # amplitude builds
    ```
  - 影响: 该式即 `elastic_out` 去掉 `+1.0`（`+ 1.0 * 0` 是明显改坏的痕迹），并非 Penner elastic-in（`-(2**(10*(t-1)) * sin((t-10.075)*2π/0.3))`）。数值复算：`elastic_in(0)=0` 但 `elastic_in(0.001)≈-0.993`（起点处 0→-1 跳变）；包络 `2**(-10t)` 是衰减而非注释所称 "amplitude builds"；`t→1⁻` 值 ≈0.0005，`t=1` 由 guard 直接返回 1.0（第二次跳变）。经 `NAMED_EASINGS`/`resolve_easing` 选中该缓动时，中间值会大幅越界（如用于 scale/opacity 插值产生负值）且视觉上是"开始猛跳、结尾瞬移"。仓库内暂无内部调用方，属公共 API 潜在缺陷。
  - 建议: 改为标准 Penner `easeInElastic`：`-(2.0 ** (10.0 * (t - 1.0))) * math.sin((t - 10.075) * (2.0 * math.pi / 0.3))`，并补 0/1 端点连续性单测。
  - 置信度: 高（数值可复算）

- [AUD-28101] [Major] `elastic_in_out` 中点跳变 0.5、终点前≈0.75 后跳到 1
  - 位置: src/features/teaching_animation/motion/easing.py:149-156
  - 证据:
    ```python
    if t < 0.5:
        return 0.5 * math.sin((2.0 * t - 0.075) * (2.0 * math.pi / 0.3)) * (2.0 ** (-20.0 * t))
    return 0.5 * (math.sin((2.0 * t - 0.075) * (2.0 * math.pi / 0.3)) * (2.0 ** (-20.0 * (1.0 - t)))) + 0.5
    ```
  - 影响: 两支包络均衰减：`t→0.5⁻` 值 ≈0.0002，`t=0.5⁺` ≈0.5002（中点跳变 0.5）；`t→1⁻` ≈0.75，`t=1` guard 返回 1.0（再跳 0.25）。正确 Penner easeInOutElastic 应为 `0.5 * 2**(20t-10) * sin((20t-11.125)*2π/0.3)`（后支 +0.5 对称）。实际效果是前半段贴 0、中点瞬移 0.5、后半段贴 0.5、终点瞬移 1，非弹性 S 曲线。
  - 建议: 按 Penner in-out 公式重写，并加入 t=0/0.5/1 连续性断言。
  - 置信度: 高（数值可复算）

### 通过项
- 线程安全：全部纯函数 + 模块级不可变常量（frozen `CubicBezier`），无共享可变状态
- `elastic_out`/`back_*`/`bounce_*`/`ease_in_out` 端点与中点连续性复算通过（`elastic_out` 与 Penner 公式一致）
- `CubicBezier`：x1/x2 ∈[0,1] 校验、Newton 导数式正确、bisection 兜底确定；`steps` 与 CSS jump-start/end 语义核对一致
- `resolve_easing` 对未知名字显式 ValueError；不涉及 GPU/GLSL/HDR/信号/持久化

---

## src/features/teaching_animation/motion/expression.py (行数: 177)

### 发现
- [AUD-28107] [Major] 整数 `**`/`pow` 可绕过复杂度预算，造成求值挂起/巨量内存（沙箱 DoS）
  - 位置: src/features/teaching_animation/motion/expression.py:37,65,163-170
  - 证据:
    ```python
    _ALLOWED_BINOPS = {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}
    ...
    "sqrt": math.sqrt, "pow": pow, ...
    ...
    result = eval(compiled, sandbox_globals, {})  # noqa: S307 - sandboxed allowlist eval
    if isinstance(result, (int, float)):
        numeric = float(result)
        ...
    ```
  - 影响: `_MAX_NUMERIC_MAGNITUDE` 只在求值完成后检查，且 `float(结果)` 在结果已是天文数字 int 时已经太迟：`9 ** 9 ** 9`（合法语法、≤200 节点）计算 3.69 亿位整数，分钟级耗时 + GB 级内存；`pow(999999999, 999999999)` 同理。浮点溢出会抛 OverflowError 被 (ArithmeticError) 捕获，但整数幂不会。本模块自我定位为"安全边界"，表达式按设计来自 AI/外部场景数据并在预览中按帧求值，一条恶意/失误表达式即可挂死渲染/预览线程。
  - 建议: 验证期对 `ast.Pow` 限制操作数（如禁止 int 底数+大指数，或限定 `**` 右操作数为小非负 int）；或在 `visit_Constant` 时限制数值字面量量级，并在 eval 处用 `signal.setrlimit`/子进程超时兜底。
  - 置信度: 高（行为确定；触达前提是外部输入表达式）

- [AUD-28108] [Major] `markers[...]` 缺键时原生 KeyError 逃逸，违反"一切非法输入抛 ExpressionError"契约
  - 位置: src/features/teaching_animation/motion/expression.py:160-174
  - 证据:
    ```python
    sandbox_globals["markers"] = dict(context.markers or {})
    ...
    try:
        result = eval(compiled, sandbox_globals, {})  # noqa: S307 - sandboxed allowlist eval
        ...
    except ZeroDivisionError as exc:
        raise ExpressionError("division by zero in expression") from exc
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ExpressionError(f"evaluation error: {exc}") from exc
    ```
  - 影响: `markers["missing"]` 抛 KeyError（LookupError，非 ValueError/ArithmeticError/TypeError），直接逃出 `evaluate_expression`；只捕获 `ExpressionError` 的调用方会把用户级错误当内部异常处理。`markers` 是来自场景数据的外部字典，键缺失是正常可发生路径。
  - 建议: except 元组加入 `KeyError`/`LookupError`（或 `Exception` 白名单化转译为 `ExpressionError`，注意不得吞掉编程错误）。
  - 置信度: 高

- [AUD-28109] [Suggestion] 求值结果类型未限定：字符串字面量或裸 `markers` 原样返回
  - 位置: src/features/teaching_animation/motion/expression.py:127-131,163-170
  - 证据:
    ```python
    def visit_Constant(self, node: ast.Constant) -> None:
        self._tick()
        if not isinstance(node.value, (int, float, str, bool)):
            raise ExpressionError(f"disallowed literal type: {type(node.value).__name__}")
    ...
    if isinstance(result, (int, float)):
        numeric = float(result)
        ...  # 有限性/量级检查只覆盖数值
    return result
    ```
  - 影响: 表达式 `"abc"` 或裸名 `markers`（返回 dict）通过验证并原样返回；下游按 motion 数值消费时类型不符，错误延后到使用点。
  - 建议: 若契约是"表达式产出 motion 数值"，在返回前限制 `isinstance(result, (int, float, bool))`，否则在 docstring 标明可返回字符串。
  - 置信度: 高（行为确定，影响取决于调用方）

- [AUD-28110] [Suggestion] 深层嵌套表达式可在节点预算生效前触发 RecursionError/MemoryError，未转译为 ExpressionError
  - 位置: src/features/teaching_animation/motion/expression.py:133-139
  - 证据:
    ```python
    def validate_expression(source: str) -> None:
        """Parse + allowlist-check an expression without evaluating it."""
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"syntax error: {exc.msg}") from exc
        _Validator().visit(tree)
    ```
  - 影响: 上万层嵌套括号/一元运算可使 `ast.parse`（CPython 自身递归）抛 RecursionError/MemoryError，只有 SyntaxError 被转译；`_MAX_NODES` 在 visit 阶段才生效，拦不住解析期爆栈。
  - 建议: 解析前限制 `len(source)`，并将 `except (SyntaxError, RecursionError, MemoryError)` 统一转译为 ExpressionError。
  - 置信度: 中

### 通过项
- 沙箱白名单机制本身健全：`ast.Attribute`/`ast.Import`/关键字参数/推导式均在 `_ALLOWED_NODE_TYPES` 之外被拒；`__builtins__` 置空；Call 仅限白名单函数名
- 复杂度预算计数虽在 Name/Call/Constant 处双计（保守偏严），不构成漏计
- ZeroDivisionError/TypeError/ValueError/浮点 OverflowError 均转译为 ExpressionError
- 类型注解完整；纯函数无状态，线程安全；不涉及 GPU/GLSL/HDR/信号/持久化

---

## src/features/teaching_animation/motion/interpolate.py (行数: 159)

### 发现
- [AUD-28102] [Major] `_mix_colors` 通道值未钳制，越界 eased/外推生成非法十六进制色串（如 `#-ff...`）
  - 位置: src/features/teaching_animation/motion/interpolate.py:140-149
  - 证据:
    ```python
    def _mix_colors(a: str, b: str, t: float) -> str:
        ar, ag, ab, aa = _parse_color(a)
        br, bg, bb, ba = _parse_color(b)
        r = round(ar + (br - ar) * t)
        ...
        if aa == 255 and ba == 255:
            return f"#{r:02x}{g:02x}{bl:02x}"
    ```
  - 影响: `t` 来自 eased progress，overshoot 类缓动（back/elastic）或 "extend" 外推会使 t 越出 [0,1]，`r/g/b` 可为负或 >255；`format(-255, "02x")` 产出 `-ff`，返回 `#-ff...` 非法颜色串。下游再经 `_parse_color`（fullmatch `^#[0-9a-fA-F...]$`）即抛 ValueError，或被渲染层静默解析失败。模块 docstring 承诺"不支持的输入显式失败"，这里却是"生成坏值"。
  - 建议: 混合前 `t` 不动、对通道 `r = min(255, max(0, r))`（或对 eased 值按 CSS 语义 clamp 后再混合），并加 round-trip 测试：`_parse_color(_mix_colors(a, b, 2.0))` 必须成功。
  - 置信度: 高（行为确定；仓库内 preset 目前仅用数值端点，颜色路径为公共 API 能力）

- [AUD-28103] [Major] extrapolate 模式字符串未验证，非法值静默按 "extend" 处理，违反模块自身"非法输入显式 ValueError"契约
  - 位置: src/features/teaching_animation/motion/interpolate.py:104-118
  - 证据:
    ```python
    if value < input_range[0]:
        if extrapolate_left == "identity":
            return value
        if extrapolate_left == "clamp":
            return output_range[0]
        value_proj = value
        return _interpolate_segment(value_proj, input_range[0], input_range[1],
                                     output_range[0], output_range[1], ease)
    ```
  - 影响: `extrapolate_left="cmap"`（AI/调用方 typo）不报错，静默走 extend → 值越界（如 opacity 外推到 >1）。docstring 明言 "Unsupported input ... raises ValueError rather than silently producing a wrong value"，此路径直接违反。右侧 112-118 同样。
  - 建议: 在 `_validate_ranges` 或入口处校验 `extrapolate_left/right ∈ {"extend","clamp","identity"}`，非法即 ValueError。
  - 置信度: 高

- [AUD-28104] [Suggestion] `value=inf` 未被拦截：`value != value` 只查 NaN，非有限值契约不完整
  - 位置: src/features/teaching_animation/motion/interpolate.py:95-99
  - 证据:
    ```python
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("interpolate value must be a finite number")
    value = float(value)
    if value != value:  # NaN
        raise ValueError("interpolate value must be finite")
    ```
  - 影响: `math.inf` 通过检查后走外推分支，`(y1-y0)*inf` 在 y1==y0 时得 NaN 被静默返回。docstring 承诺 non-finite 输入抛 ValueError。实践中 frame 为 int，影响低。
  - 建议: 改用 `not math.isfinite(value)`（对 int/float 均适用）。
  - 置信度: 高（行为确定，影响低）

- [AUD-28105] [Suggestion] `_apply_extrapolation` 为死代码，从未被调用
  - 位置: src/features/teaching_animation/motion/interpolate.py:42-54
  - 证据:
    ```python
    def _apply_extrapolation(
        value: float,
        input_range: Sequence[float],
        extrapolate: Extrapolate,
        edge_index: int,
        fallback: float,
    ) -> float | None:
        """Return the clamped input value, or None to signal identity/extension."""
    ```
  - 影响: `interpolate` 内联处理了外推（104-118），该函数全文件无调用点，语义（返回 None 表示 identity）还与现行实现不同，易误导维护者。
  - 建议: 删除，或将其真正接入 `interpolate` 统一外推逻辑。
  - 置信度: 高

- [AUD-28106] [Suggestion] `interpolate` 每次调用执行函数内 `from .easing import resolve_easing`
  - 位置: src/features/teaching_animation/motion/interpolate.py:101
  - 证据:
    ```python
    _validate_ranges(input_range, output_range)
    from .easing import resolve_easing
    ease = resolve_easing(easing)
    ```
  - 影响: 顶部 21 行已从 `.easing` 导入，不存在循环导入理由；`compile_preset_to_track` 逐帧采样时每帧重复 import 查找（sys.modules 命中，~µs 级，累计无谓开销）。
  - 建议: 移至模块顶部导入。
  - 置信度: 高

### 通过项
- 线程安全：纯函数无状态
- `_validate_ranges`：等长/至少两点/有限性/bool 排除/严格递增校验完备
- 向量混合等长校验、类型不匹配显式 ValueError；`clamp` 校验 low<=high
- 颜色 hex 解析正则严格（3/6/8 位）；不涉及 GPU/GLSL/信号/持久化；8bit hex 混色属矢量动画层语义，非 HDR 浮点数据路径，不构成 clamp 违规

---

## src/features/teaching_animation/motion/motion_binding.py (行数: 77)

### 发现
- [AUD-28122] [Suggestion] 未使用导入 `field`；`from_dict` 静默丢弃非法 target 条目且不验证 scale/offset 有限性
  - 位置: src/features/teaching_animation/motion/motion_binding.py:15,69-74
  - 证据:
    ```python
    from dataclasses import dataclass, field
    ...
    targets = tuple(
        BindingTarget(element_id=str(t.get("element_id", "")), property=str(t.get("property", "")),
                      scale=float(t.get("scale", 1.0)), offset=float(t.get("offset", 0.0)))
        for t in data.get("targets", []) if isinstance(t, Mapping)
    )
    ```
  - 影响: `field` 未使用；外部持久化数据中非 Mapping 的 target 被无声丢弃（绑定悄悄少目标，无诊断），`float("nan")`/`float("inf")` 字符串可成为 scale/offset，`evaluate` 输出 NaN 污染下游。
  - 建议: 删除死导入；对非法条目抛 ValueError 或至少记录告警；对 scale/offset 加 `math.isfinite` 校验。
  - 置信度: 高（行为确定，触发依赖外部坏数据）

### 通过项
- 线程安全：frozen dataclass，`evaluate` 纯函数
- 输入验证：binding_id/driver/property 非空、target key 唯一性在 `__post_init__` 校验
- 状态一致性：`to_dict`/`from_dict` 字段对应；不涉及 GPU/GLSL/HDR/信号/undo

---

## src/features/teaching_animation/motion/motion_commands.py (行数: 461)

### 发现
- [AUD-28113] [Major] 部分 dict 形式 `spring_config` 时，持久化 motion 语义与实际编译轨道使用不同弹簧参数（ripple/重开后动画漂移）
  - 位置: src/features/teaching_animation/motion/motion_commands.py:222-228（对照 src/features/teaching_animation/motion/presets.py:168-178）
  - 证据:
    ```python
    # motion_commands.motion_semantics —— 缺省取 preset 基准（pop: damping=12, stiffness=180）
    raw_config = params.get("spring_config", base_config)
    if isinstance(raw_config, Mapping):
        config = SpringConfig(
            mass=float(raw_config.get("mass", base_config.mass)),
            damping=float(raw_config.get("damping", base_config.damping)),
            stiffness=float(raw_config.get("stiffness", base_config.stiffness)),
        )
    ```
    ```python
    # presets._coerce_spring_config —— 缺省 damping=10.0, stiffness=100.0
    if isinstance(value, dict):
        return SpringConfig(
            mass=float(value.get("mass", 1.0)),
            damping=float(value.get("damping", 10.0)),
            stiffness=float(value.get("stiffness", 100.0)),
        )
    ```
  - 影响: 当 params 传入部分 dict（如 `{"spring_config": {"mass": 2.0}}`，AI 填参的常见形态）时，`_build_plan_animations` 经 `registry.build` 实际用 damping=10/stiffness=100 编译轨道，而 `motion_semantics` 记入 scene.metadata 的是 damping=12/stiffness=180 且 `settle_frame` 也按错误 config 计算。之后改 interval 触发 ripple 重编译、或保存/重开后按 motion 语义重推导时，动画与首渲不一致——正是该字段设计要保证的性质被破坏。
  - 建议: 两处共用同一个 coercion（如把 `_coerce_spring_config` 提为公共函数并接收 preset 基准 config 作为缺省），并对 pop/overshoot 基准统一。
  - 置信度: 高

- [AUD-28114] [Suggestion] 未使用导入 `SceneCommand`
  - 位置: src/features/teaching_animation/motion/motion_commands.py:27
  - 证据:
    ```python
    from ..core.commands import SceneCommand
    ```
  - 影响: 全文件仅此一处出现（grep 确认）；`SceneCommand` 是 Protocol，本文件三个命令以 duck-typing 满足 apply/undo，无需导入。死导入暗示"想继承而未继承"的意图漂移。
  - 建议: 删除导入；或显式让命令声明实现该 Protocol 以获得静态检查。
  - 置信度: 高

- [AUD-28115] [Suggestion] `from src.shared.keyframe import ...` 使用仓库根绝对路径，与同文件相对导入风格混用
  - 位置: src/features/teaching_animation/motion/motion_commands.py:31
  - 证据:
    ```python
    from ..core.commands import SceneCommand
    from ..core.model import SceneAnimation, TeachingSceneDocument
    ...
    from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType
    ```
  - 影响: 依赖 `src` 顶层包可导入（仓库根运行）；包重命名/安装为站点包/测试以不同根启动时该行先断，而相对导入不受影响。
  - 建议: 改为对 shared 的稳定相对/配置化路径（与仓库 shared→features 引用约定一致），至少统一风格。
  - 置信度: 中（当前可运行；风险为可移植性）

- [AUD-28116] [Suggestion] 冻结命令用 `object.__setattr__` 写私有 undo 令牌，重复 apply 会孤儿化先前动画
  - 位置: src/features/teaching_animation/motion/motion_commands.py:312,369,411
  - 证据:
    ```python
    animation_id = _new_id(f"motion_{self.preset_id}")
    object.__setattr__(self, "_animation_id", animation_id)
    ```
    ```python
    animations, _ids, final_plan = _build_plan_animations(plan, scene, registry)
    object.__setattr__(self, "_plan_id", final_plan.plan_id)
    ```
  - 影响: 每次 `apply` 生成新 id 并覆盖 `_animation_id`/`_plan_id`：同一命令对象被连续 apply 两次（无中间 undo）时，第一次生成的动画残留场景而 undo 只移除最新 id（孤儿动画 + 孤儿 plan 数据）。命令对象若经序列化往返（undo 栈持久化），下划线私有字段也可能不被保留，导致 undo 抛 RuntimeError。正常 undo→redo 序列当前可用（apply 前已 undo 移除旧动画）。
  - 建议: 在 apply 入口检测已应用状态（或生成确定性 id），把 undo 令牌作为显式返回值/独立 Memento 传递，而非改写冻结字段。
  - 置信度: 中（当前主流程正确，风险在复用/序列化场景）

- [AUD-28117] [Suggestion] `StaggerPlan.from_dict` 不做范围校验，坏数据延后到 apply 深处才报错
  - 位置: src/features/teaching_animation/motion/motion_commands.py:138-152
  - 证据:
    ```python
    interval=int(data.get("interval", 12)),
    delay=int(data.get("delay", 0)),
    duration=int(data.get("duration", 18)),
    ```
  - 影响: 从 scene.metadata（工程文件，可手改/损坏）载入 `duration=0` 或 `interval=-1` 不会在载入期失败，而是在 `SetStaggerIntervalCommand.apply` → `_build_plan_animations` → `compile_preset_to_track` 深处以 `ValueError("end_frame must exceed start_frame")` 爆出，错误远离根因；"外部数据须验证"要求载入期校验。
  - 建议: `from_dict` 中对 interval>=0、duration>0、anchor_frame>=0 校验（与 `StaggerElementsCommand.__post_init__` 同一规则）。
  - 置信度: 高

- [AUD-28118] [Suggestion] `StaggerPlan(**plan.to_dict())` 重建路径使 tuple 字段在运行时持有 list
  - 位置: src/features/teaching_animation/motion/motion_commands.py:267,431-434
  - 证据:
    ```python
    updated = StaggerPlan(**{**plan.to_dict(), "animation_ids": ids, "motion": motion})
    ```
    ```python
    animations, ids, _ = _build_plan_animations(
        StaggerPlan(**{**previous.to_dict(), "animation_ids": ()}), updated, registry
    )
    restored_plan = type(previous)(**{**previous.to_dict(), "animation_ids": ids})
    ```
  - 影响: `to_dict` 将 `element_ids` 等转为 `list(...)`，重建后 `element_ids` 为 list 而注解为 `tuple[str, ...]`；frozen 语义下不可变仍成立，功能无碍，但类型不健全、`==` 比较时 `("a",) != ["a"]` 可造成计划对比误判。
  - 建议: 重建前将序列字段显式 `tuple(...)`，或提供 `with_updates(**kw)` 类方法封装。
  - 置信度: 高

### 通过项
- 线程安全：场景变换全部纯函数式（frozen doc + replace），命令内无共享可变状态；registry 每次新建或外部注入
- 状态提交一致性（主体）：apply/undo 对称——undo 重编译确定性一致；`_strip_animations` 先于 `_write_plan`，元素缺失时在写入前抛错，无半提交（调用方原 scene 不被破坏）
- 异常路径：元素不存在、plan 不存在、end<=start、sample_step<1、fps<=0 均显式 ValueError；无宽泛 except
- `compile_preset_to_track`：至少两关键帧保证、共线压缩逻辑核对正确（kept/next 参照系正确）、宽轨道界 + 求值端语义 clamp 的设计与注释一致
- `_json_safe_params` 保证 SpringConfig 可持久化；`scene_diff` 深比较正确；不涉及 GPU/GLSL/HDR/Qt 信号

---

## src/features/teaching_animation/motion/presets.py (行数: 311)

### 发现
- [AUD-28119] [Major] `_slide_preset` 非法 direction 静默回退为 "up"，方向参数无验证
  - 位置: src/features/teaching_animation/motion/presets.py:158
  - 证据:
    ```python
    def _slide_preset(start: int = 0, duration: int = 12, *, distance: float = 80.0,
                      direction: str = "up", easing: EasingFn | str | None = "ease_out") -> MotionFn:
        """Returns the *offset* (px) to apply along an axis; caller maps to x/y."""
        ease = resolve_easing(easing)
        sign = {"up": 1.0, "down": -1.0, "left": 1.0, "right": -1.0}.get(direction, 1.0)
    ```
  - 影响: direction 是 AI/编辑器填充的 preset 参数（本模块定位即"AI 填参数"）；传入 "top"/"Up"/拼写错误时不报错，静默按 up（sign=1.0）处理，元素朝错误方向运动且无任何诊断——违反"外部数据须验证"。另注：sign 对 up=+1/down=-1 的正负号约定依赖调用方把 offset 映射到 y 轴时的取反约定，docstring 已声明由 caller 映射，尚可。
  - 建议: `direction` 不在字典内时抛 ValueError（与 `resolve_easing` 对未知缓动名的处理一致）。
  - 置信度: 中（行为确定；`.get` 缺省可能是有意容错，但对参数化配方 API 构成明确验证缺口）

- [AUD-28120] [Suggestion] 未使用导入 `ease_out`
  - 位置: src/features/teaching_animation/motion/presets.py:25
  - 证据:
    ```python
    from .easing import EasingFn, ease_out, resolve_easing
    ```
  - 影响: 文件内仅以字符串 `"ease_out"` 出现（作为默认参数值传给 `resolve_easing`），函数对象 `ease_out` 无引用。
  - 建议: 从导入中移除。
  - 置信度: 高

- [AUD-28121] [Suggestion] `register` 对重复 preset_id 静默返回 False，调用方不检查
  - 位置: src/features/teaching_animation/motion/presets.py:274-282
  - 证据:
    ```python
    def register(self, preset: MotionPreset, *, replace: bool = False) -> bool:
        existing = self._presets.get(preset.preset_id)
        if existing is not None and not replace:
            return False
        ...
        return True
    ```
  - 影响: `create_default_preset_registry` 连续 register 8 个 preset 均不检查返回值；若内置集出现重复 id（未来维护引入），后者被静默丢弃，直到 `build` 时才以 KeyError 爆出，错误远离根因。
  - 建议: 默认注册路径对 False 返回值断言/抛错（`replace=True` 显式表达覆盖意图）。
  - 置信度: 高

### 通过项
- 线程安全：helpers 全纯函数；`wiggle` 用 sha256 派生确定性噪声（无 random 模块），preview/export 一致性成立
- 输入验证（helpers 部分）：loop/ping_pong period>0、wiggle frequency/amplitude、count_up 区间、typewriter chars_per_frame、reveal duration、stagger_index interval>=0 均显式校验
- `MotionPreset.__post_init__` 校验 preset_id/version；`build` 合并 default_params 后传参，未知 preset 抛 KeyError
- 持久化契约：preset+params 序列化而非采样值，符合"参数化配方"设计；不涉及 GPU/GLSL/HDR/信号/undo（命令层负责）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 9 | AUD-28100, 28101, 28102, 28103, 28107, 28108, 28111, 28113, 28119 |
| Suggestion | 15 | AUD-28104, 28105, 28106, 28109, 28110, 28112, 28114, 28115, 28116, 28117, 28118, 28120, 28121, 28122, 28123 |

重点主题：
1. easing 库两条 elastic 曲线数学错误（AUD-28100/28101，数值可复算的端点/中点跳变）；
2. interpolate 对越界 eased 的颜色通道与 extrapolate 模式缺少验证（AUD-28102/28103），违反模块自身"非法输入显式失败"契约；
3. expression 沙箱存在整数幂 DoS 与 KeyError 逃逸两个边界缺口（AUD-28107/28108）；
4. motion_commands 与 presets 对部分 dict `spring_config` 的缺省参数分叉，导致持久化语义与实际动画不一致（AUD-28113，ripple/重开漂移）；
5. 若干未使用导入与静默回退验证缺口（Suggestion 级）。
