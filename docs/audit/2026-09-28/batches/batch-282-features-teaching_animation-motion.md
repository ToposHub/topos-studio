# batch-282 审查报告：src/features/teaching_animation/motion/（sequence / spring / time_range）

批次：batch-282 [P2]，编号区间 AUD-28200–AUD-28299，日期 2026-09-28。
三个文件均为纯 Python 值对象/纯函数模块（frozen dataclass），无 Qt 信号、无 OpenGL 资源、无持久化副作用；九维度中 GPU 生命周期、信号防递归、GLSL/HDR 对本批整体不适用（已在各文件通过项注明）。发现计数：Critical 1、Major 3、Suggestion 5。

## src/features/teaching_animation/motion/sequence.py (行数: 206)

### 发现

- [AUD-28200] [Major] `with_step_gap` 接受无界负 gap，`layout()` 在深层抛出延迟 ValueError
  - 位置: src/features/teaching_animation/motion/sequence.py:107-112（编辑入口无校验）、57-70（layout 内构造 TimeRange）
  - 证据:
    ```python
    def with_step_gap(self, step_id: str, gap_after: int) -> "SeriesGroup":
        new_steps = tuple(
            replace(step, gap_after=gap_after) if step.step_id == step_id else step
            for step in self.steps
        )
        return replace(self, steps=new_steps)
    ```
    ```python
    def layout(self) -> tuple[ChildLayout, ...]:
        result: list[ChildLayout] = []
        cursor = self.start
        for step in self.steps:
            time_range = TimeRange(
                start=cursor,
                ...
            cursor += step.duration + step.gap_after
    ```
  - 影响: `gap_after` 允许任意负值（重叠），但当 `duration + gap_after` 使累积 cursor 为负时，异常由 `TimeRange.__post_init__`（time_range.py:35-36）在 layout 期才抛出。实测 `SeriesGroup((SeriesStep("a", 5, gap_after=-100), SeriesStep("b", 5))).layout()` 与 `with_step_gap("a", -100).layout()` 均抛 `ValueError: TimeRange start must be non-negative`。错误从编辑期延迟到布局/渲染期，且报错文案不含 step 上下文，排障困难；若 layout 在时间线绘制路径中被调用则中断该帧渲染。当前仓库内无生产调用方（仅 `__init__` re-export 与测试），属导出 API 的潜在缺陷。
  - 建议: 在 `with_step_gap`/`SeriesGroup.__post_init__` 中校验累积 cursor 恒非负（或在 layout 抛错时携带 step_id 上下文），尽早拒绝或钳制越界重叠。
  - 置信度: 高（行为已实测复现；无生产调用方故标注为潜在）

- [AUD-28201] [Suggestion] `ChildLayout = tuple` 为无参数化的弱类型别名
  - 位置: src/features/teaching_animation/motion/sequence.py:21
  - 证据:
    ```python
    ChildLayout = tuple  # (child_id: str, range: TimeRange)
    ```
  - 影响: 裸 `tuple` 丢失元素结构信息，类型检查器与 IDE 无法校验 `(str, TimeRange)` 二元组，注释中的约定无静态保障。
  - 建议: 改为 `ChildLayout = tuple[str, TimeRange]`（文件已 `from __future__ import annotations`，运行时别名赋值亦合法）。
  - 置信度: 高

### 通过项
- 线程安全：全部 frozen dataclass + 纯函数，无可变共享状态
- GPU 资源生命周期：本模块不涉及 GPU 资源（N/A）
- GLSL/HDR 正确性：纯 Python 时间图，无 GLSL/HDR 数据面（N/A）
- 信号防递归：无 Qt 控件/信号（N/A）
- 状态提交一致性：`with_step_duration`/`with_step_gap` 均返回新实例（`replace`），适配 undo/redo 快照模型
- 异常路径（其余）：领域错误统一 `ValueError` 且文案具体，无宽泛 except
- 输入验证（其余）：`SeriesStep`/`SeriesGroup`/`SequenceChild`/`SequenceGroup` 的 id 非空、id 唯一、duration>0、start≥0、trim_in≥0 均在构造期校验；`stagger_starts` 校验 interval/duration 并借 `SequenceGroup` 拒绝重复 id
- 性能：`layout()` O(n) 重算，n 为组内步骤数，规模小，可接受

## src/features/teaching_animation/motion/spring.py (行数: 138)

### 发现

- [AUD-28202] [Critical] 近临界阻尼浮点舍入使 `omega_d` 为 0，`spring()`/`_step_response` 抛 ZeroDivisionError
  - 位置: src/features/teaching_animation/motion/spring.py:46-52
  - 证据:
    ```python
    discriminant = c * c - 4.0 * m * k
    if discriminant < 0.0:
        # Under-damped: oscillates, decays.
        omega0 = math.sqrt(k / m)
        zeta = c / (2.0 * math.sqrt(k * m))
        omega_d = omega0 * math.sqrt(1.0 - zeta * zeta)
        envelope = math.exp(-zeta * omega0 * t)
        return 1.0 - envelope * (math.cos(omega_d * t) + (zeta * omega0 / omega_d) * math.sin(omega_d * t))
    ```
  - 影响: 判别走 `discriminant` 与走 `1.0 - zeta*zeta` 是两条独立舍入路径。实测 `SpringConfig(mass=1.0, damping=3.4641016151377544, stiffness=3.0)`（即 c=2√(mk) 的临界阻尼选择）通过全部参数校验，但 `discriminant=-1.776e-15` 进入欠阻尼分支而 `zeta` 舍入为精确 1.0，`omega_d=0.0`，`(zeta*omega0/omega_d)` 抛 `ZeroDivisionError: division by zero`。临界阻尼是 UI 弹簧最常用配置之一；且 `SpringConfig` 会从持久化/外部 JSON dict 构造（motion_commands.py:224、presets.py:173 的 `_coerce_spring_config`），`spring()` 又被 presets 的每帧 MotionFn（`_pop_preset`/`_overshoot_preset`）在预览与导出烘帧循环中逐帧调用，未捕获异常将中断渲染/导出任务。
  - 建议: 对 `|1.0 - zeta*zeta|` 设数值容差（如 < 1e-12 时走临界阻尼闭式解），或在欠阻尼分支对 `omega_d` 下限保护；补近临界阻尼的回归测试。
  - 置信度: 高（已实测复现崩溃输入）

- [AUD-28203] [Major] `duration` 不构成有限端点：`measure_spring` 哨兵值混淆可致单帧跳变，且与 docstring 承诺不符
  - 位置: src/features/teaching_animation/motion/spring.py:98-104（对照 79-81 docstring、118-120 measure_spring docstring）
  - 证据:
    ```python
    if duration is not None and local_frame >= duration:
        settle = measure_spring(fps, cfg, rest_threshold=duration_rest_threshold)
        if local_frame >= settle:
            return to
    ```
    docstring（79-81 行）："After the settle frame (or after ``duration`` frames if given) it is ``to``."；measure_spring docstring（118-120 行）："if it never settles, ``max_frames`` is returned."
  - 影响: ① 传 `duration=30` 而实际 settle=56 时，帧 30–55 仍返回活动值，违背 docstring"after duration frames it is to"的契约，依赖有限端点的 beat 调度在 duration 之后仍看到运动；② `measure_spring` 返回的 600 无法区分"恰在 600 收敛"与"600 帧内从未收敛"：慢弹簧（如 mass=1000, stiffness=1, damping=1，真实 settle 远超 600 帧）配合 `duration` 参数时，`local_frame>=600` 即 `return to`，残差 |value−to| 可远超 0.005 阈值，产生单帧不连续跳变（渲染可见的突跳）。当前 presets 调用未传 `duration`，属导出 API 的潜在缺陷路径。
  - 建议: 让 `measure_spring` 显式区分"未收敛"（返回 None 或抛错），或在 snap 前校验 |value−to| ≤ 阈值；修正 docstring 使契约与实现一致。
  - 置信度: 高（矛盾与跳变路径可证；触发需传 `duration` 且配置收敛慢）

- [AUD-28204] [Suggestion] `measure_spring` 的 fps 校验缺 `isfinite`，NaN/inf 静默通过并返回伪 settle 帧
  - 位置: src/features/teaching_animation/motion/spring.py:122-123（对照 spring() 的 83-84 行）
  - 证据:
    ```python
    if fps <= 0.0:
        raise ValueError("measure_spring fps must be positive")
    ```
    而 `spring()` 为：`if fps <= 0.0 or not math.isfinite(fps):`
  - 影响: `NaN <= 0.0` 为 False，NaN fps 通过校验；t=NaN 使响应恒 NaN，永不满足收敛条件，实测 `measure_spring(float("nan"))` 与 `measure_spring(float("inf"))` 均静默返回 600，调用方（如 motion_commands.py:241 将其写入 `settle_frame` 元数据）拿到看似合法的伪值。两入口校验不一致。
  - 建议: 与 `spring()` 对齐，加 `not math.isfinite(fps)` 检查。
  - 置信度: 高（已实测）

- [AUD-28205] [Suggestion] `spring()` 每次求值内嵌完整 `measure_spring` 601 帧扫描且无缓存（预览/导出热路径）
  - 位置: src/features/teaching_animation/motion/spring.py:99、103
  - 证据:
    ```python
    if duration is not None and local_frame >= duration:
        settle = measure_spring(fps, cfg, rest_threshold=duration_rest_threshold)
    ...
    if abs(value - to) < duration_rest_threshold and local_frame >= measure_spring(fps, cfg, rest_threshold=duration_rest_threshold):
    ```
  - 影响: settle 之后每帧求值都触发 99/103 行的整段 O(max_frames) 闭式解扫描（601 次 exp/sqrt 序列），实测 settled 后单次 `spring()` 约 39.2µs；presets 的 `_pop_preset`/`_overshoot_preset` 将 `spring()` 包成每帧 MotionFn，`compile_preset_to_track` 导出烘帧时逐帧逐属性调用，成本随元素数×帧数线性放大，与实时基准目标相悖。同一求值内 99/103 行还可能各扫一次。
  - 建议: settle 帧仅是 `(fps, config, rest_threshold)` 的纯函数且 `SpringConfig` 为 frozen 可哈希——用 `functools.lru_cache` 缓存 `measure_spring`，或在 `spring()` 调用侧预计算一次传入。
  - 置信度: 高（耗时实测；量级随调用方规模放大）

### 通过项
- 线程安全：纯函数 + frozen dataclass，无共享可变状态
- GPU 资源生命周期：不涉及 GPU 资源（N/A）
- 信号防递归：无 Qt 信号（N/A）
- 浮点字面量：全部显式 `.0`（4.0、2.0、1.0、0.0），符合 .clinerules
- 输入验证（其余）：`SpringConfig` 校验 mass/damping/stiffness 有限且为正；`spring()` 校验 fps 有限且为正
- 阻尼三分支公式：欠阻尼/临界/过阻尼闭式解在 t=0（响应 0）与 t→∞（响应 1）边界数学正确（临界与过阻尼分支未发现数值缺陷）
- `measure_spring` 循环有 `max_frames` 上界，无死循环风险
- 状态提交一致性：无状态、无持久化（N/A）

## src/features/teaching_animation/motion/time_range.py (行数: 110)

### 发现

- [AUD-28206] [Major] `shifted()` 负向越界时静默 clamp 到 0，破坏兄弟相对布局且违背"平移"契约
  - 位置: src/features/teaching_animation/motion/time_range.py:68-70
  - 证据:
    ```python
    def shifted(self, delta: int) -> "TimeRange":
        """Return a copy translated by ``delta`` parent frames (rippling)."""
        return replace(self, start=max(0, self.start + delta))
    ```
  - 影响: 实测 `TimeRange(start=5, duration=10)` 与 `TimeRange(start=10, duration=10)` 同时 `shifted(-8)` 后位于 0 和 2，兄弟重叠间距由 -5 变为 -8——返回结果不再是平移，组内相对几何被静默破坏（undo/redo 重放平移时同样失真），且无任何告警或异常。当前仓库内无生产调用方，属导出 API 的潜在缺陷。
  - 建议: 越界时 raise（由调用方决定整组统一钳制 delta），或明确文档化 clamp 语义并供组级 API 先对 delta 取公共下界。
  - 置信度: 中（行为实测确定；是否故意以 clamp 维持 start≥0 不变式不确定）

- [AUD-28207] [Suggestion] `local_frame` 的 `local < 0` 分支不可达（死代码）
  - 位置: src/features/teaching_animation/motion/time_range.py:61-63
  - 证据:
    ```python
    if not self.contains(parent_frame):
        return None
    local = (parent_frame - self.start) + self.trim_in
    if local < 0:
        return None
    ```
  - 影响: `contains()` 保证 `parent_frame - self.start >= 0`，`__post_init__`（39-40 行）保证 `trim_in >= 0`，故 `local < 0` 永假（实测 `local_frame(10)=0`）。死分支暗示"负 trim/预卷"语义存在，误导后续维护者。
  - 建议: 删除该分支，或如需预卷语义则显式建模（如 `preroll` 字段）。
  - 置信度: 高

- [AUD-28208] [Suggestion] `transform_to_parent` 丢弃父窗口 duration，组合结果可越过父范围末尾（可见性误报陷阱）
  - 位置: src/features/teaching_animation/motion/time_range.py:78-93
  - 证据:
    ```python
    return TimeRange(
        start=parent.start + child.start,
        duration=child.duration,
        trim_in=child.trim_in + parent.trim_in,
        freeze=child.freeze,
        enabled=True,
    )
    ```
  - 影响: 父的 `duration` 未参与组合：当 `child.start + child.duration > parent.duration`（子局部范围超出父窗口）时，组合后的 TimeRange 终点越过父的 end；消费方若直接用组合结果的 `contains()` 判可见性，会在父窗口结束之后的帧误报可见。当前仅被 `__init__.py` re-export、无仓库内调用方（`compose_local_frame` 逐层经 `parent.local_frame` 是正确路径），属潜在 API 陷阱。另注意禁用分支（85-86 行）返回的 range 仍保留父本地坐标未平移，语义混合。
  - 建议: 文档化"结果不裁剪到父窗口"的约定并提示配合 `compose_local_frame` 使用，或组合时与父 end 取 min（并定义 duration≤0 时的行为）。
  - 置信度: 中（语义可证；是否为有意设计不确定）

### 通过项
- 线程安全：frozen dataclass + 纯函数，无共享可变状态
- GPU 资源生命周期：不涉及 GPU 资源（N/A）
- GLSL/HDR 正确性：无 GLSL/HDR 数据面（N/A）
- 信号防递归：无 Qt 信号（N/A）
- 半开区间语义：`contains`/`end`/`local_frame` 一致采用 `[start, start+duration)`，首末帧无 off-by-one（测试覆盖）
- 输入验证（其余）：`__post_init__` 校验 start≥0、duration>0、trim_in≥0；`with_duration` 拒绝非正时长；`local_frame` 的 freeze 钳到 `content_duration-1` 逻辑正确
- `compose_local_frame`：先经父 `local_frame`（含父窗口与 freeze 语义）再解析子，层级可见性正确
- 异常路径：领域错误统一 `ValueError`，无宽泛 except
