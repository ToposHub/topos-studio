# 批次报告：batch-243 [P2] src/features/color/ai//

编号区间：AUD-24300–AUD-24399。审查日期：2026-09-28。

---

## src/features/color/ai/__init__.py (行数: 0)

### 发现

（无——空文件）

### 通过项

- 通过（无逻辑）：0 字节空文件，仅作为 `features.color.ai` 包标记存在，未导出任何符号（`color_context` 需显式导入）。

---

## src/features/color/ai/color_context.py (行数: 638)

Qt-free 契约层：色彩空间词汇表、ColorContextSnapshot、ColorMeasurement、GradeOperation、ColorGradePlan、ShotMatch 匹配、HDRGuard 校验、LookRecipe/BrandColorProfile、GradeDiff 对比。全库无其他引用（当前为孤立契约层）。

### 发现

- [AUD-24301] [Major] frozen dataclass 的 Mapping 字段持有可变 dict，破坏不可变与哈希绑定契约
  - 位置: src/features/color/ai/color_context.py:205（另见 85-86、150-151、368、479、514-515）
  - 证据:
    ```python
    @dataclass(frozen=True)
    class GradeOperation:
        ...
        parameters: Mapping[str, Any] = field(default_factory=dict)
    ```
    `__post_init__` 未做防御性拷贝或 `MappingProxyType` 封装（`to_dict` 里的 `dict(self.parameters)` 只保护序列化结果，不保护对象本身）。
  - 影响: `ColorGradePlan` 自称 "dry-run, hash-bound, approval-gated"（L237），但调用方（LLM 侧或宿主）在审批后原地修改 `op.parameters` / `context.metadata` 等字典即可改变 `plan_hash()` 的结果，且共享同一 dict 的多个计划对象参数互相耦合——审批绑定可被绕过或污染，冻结声明形同虚设。
  - 建议: 在各 `__post_init__` 中用 `object.__setattr__` 将 Mapping 字段替换为 `types.MappingProxyType(dict(...))`（或深拷贝后的只读视图），使 frozen 语义完整。
  - 置信度: 高

- [AUD-24302] [Major] validate_hdr 未校验 LUT transfer_function 取值合法性，未知字符串静默绕过 HDR 防线
  - 位置: src/features/color/ai/color_context.py:411-427
  - 证据:
    ```python
    target_tf = str(op.parameters.get("transfer_function", context.timeline_transfer_function))
    if context.is_hdr and target_tf == SDR:
        violations.append(HDRViolation(code="HDR_SDR_LUT_ON_HDR_TIMELINE", ...))
    if not context.is_hdr and target_tf in {HDR_PQ, HDR_HLG}:
        violations.append(HDRViolation(code="HDR_LUT_ON_SDR_TIMELINE", ...))
    ```
    只与 `SDR`/`HDR_PQ`/`HDR_HLG` 三个字面量比较，`"hdr10"`、`"pq"`、`"None"`（显式传 None 时 `str(None)`）等任何未知值两个分支都不命中，直接放行。
  - 影响: CG-06 的职责是"refuses incompatible transforms"、铁律要求外部数据须验证；LLM 生成的参数拼写错误或非标准值会使不兼容的传递函数变换完全绕过 HDR 守卫，可能导致错误渲染输出而不报任何违规。
  - 建议: `target_tf` 不在 `TRANSFER_FUNCTIONS` 内时追加 `severity="error"` 的违规（unknown transfer function），再做兼容性判断。
  - 置信度: 高

- [AUD-24303] [Suggestion] HDRViolation.severity 无枚举校验，非 "error" 值一律视为非阻断
  - 位置: src/features/color/ai/color_context.py:365, 379-381
  - 证据:
    ```python
    @dataclass(frozen=True)
    class HDRViolation:
        code: str
        severity: str            # error / warning
    ...
    @property
    def is_blocking(self) -> bool:
        return any(v.severity == "error" for v in self.violations)
    ```
  - 影响: 宿主侧构造违规时把严重度写成 `"fatal"`/`"critical"`/`"Error"` 等拼法，`is_blocking` 恒为 False，HDR 硬违规被当警告放行。
  - 建议: 改为 `Literal["error", "warning"]` 或在 `__post_init__` 校验取值。
  - 置信度: 高

- [AUD-24304] [Suggestion] ColorMeasurement 无范围/NaN 校验，NaN 峰值可静默通过阈值判断
  - 位置: src/features/color/ai/color_context.py:139-151, 443
  - 证据:
    ```python
    peak_luma: float | None = None          # 0..1 for SDR; nits for HDR
    clipping_ratio: float | None = None       # fraction of clipped pixels
    ...
    if m.peak_luma > peak_nit_limit:
    ```
    数据类无 `__post_init__`；NaN 与任何上限比较均为 False。
  - 影响: GPU scope/外部测量产生 NaN、inf 或负值（如 0/0、未初始化通道）时，`validate_hdr` 的峰值与 clipping 判定失真：超限被漏报，报告的 `peak_nits_observed` 也可能是 NaN。违反"外部数据须验证"铁律。
  - 建议: 增加 `__post_init__` 用 `math.isfinite` 校验数值字段，并按注释语义校验范围（如 clipping_ratio ∈ [0,1]）。
  - 置信度: 中

- [AUD-24305] [Suggestion] validate_hdr 的 max_code_value 永远为 None（未完成功能/死变量）
  - 位置: src/features/color/ai/color_context.py:407, 463（字段声明 377）
  - 证据:
    ```python
    peak_nits: float | None = None
    max_code: float | None = None
    ...（函数体内无任何对 max_code 的赋值）
    return HDRGuardReport(
        violations=tuple(violations),
        peak_nits_observed=peak_nits,
        max_code_value=max_code,
    )
    ```
  - 影响: `HDRGuardReport.max_code_value` 是公开报告字段却恒为 None，依赖它做码值判断的调用方会误判"无数据"为"无问题"。
  - 建议: 从测量数据计算 max_code（如 histogram_quantiles 上界），或删除该字段与局部变量。
  - 置信度: 高

- [AUD-24306] [Suggestion] ColorGradePlan.is_empty 恒为 False，与 __post_init__ 校验矛盾
  - 位置: src/features/color/ai/color_context.py:259-260, 264-266
  - 证据:
    ```python
    if not self.operations:
        raise ValueError("ColorGradePlan.operations cannot be empty")
    ...
    @property
    def is_empty(self) -> bool:
        return not self.operations
    ```
  - 影响: 空 operations 在构造时即抛异常，`is_empty` 永远返回 False，属死代码；语义上误导调用方以为存在合法空计划路径。
  - 建议: 删除 `is_empty`，或在注释中明确其仅为防御性谓词。
  - 置信度: 高

- [AUD-24307] [Suggestion] compare_grade 用单一回归阈值套用不同量纲指标，自动回滚判定失真
  - 位置: src/features/color/ai/color_context.py:574, 588-604
  - 证据:
    ```python
    regression_threshold: float = 0.1,
    ...
    for metric, worse_when_higher in (
        ("clipping_ratio", True),
        ("skin_tone_deviation", True),
        ("noise_floor", True),
        ...
    if worse_when_higher is True and delta > regression_threshold:
        regressed = True
    ```
  - 影响: clipping_ratio 是 0–1 的比例（阈值 0.1 = 绝对 10 个百分点），clipping 从 0.1% 恶化到 10% 仍不判回归；而 skin_tone_deviation（度数）、noise_floor 量纲完全不同，同一 0.1 含义迥异。`should_auto_rollback` 任意指标回归即回滚，易误报/漏报触发自动回滚。
  - 建议: 每个指标独立阈值（如 `Mapping[str, float]`），或按指标基线归一化。
  - 置信度: 中

- [AUD-24308] [Suggestion] plan_hash 用 default=str 序列化任意参数，set 等无序 repr 导致跨进程哈希不稳定
  - 位置: src/features/color/ai/color_context.py:287
  - 证据:
    ```python
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ```
  - 影响: `parameters` 中含 set（`str(set)` 顺序受 PYTHONHASHSEED 影响）或含内存地址的默认 repr 对象时，同一逻辑内容在不同进程得到不同 `plan_hash`，审批-执行绑定间歇性失败且难以复现；NaN/inf 也会产出非标准 JSON。
  - 建议: 哈希前显式规范化参数（类型白名单、集合排序转列表），并校验可 JSON 序列化，失败即抛错而非静默 `str()`。
  - 置信度: 中

- [AUD-24309] [Suggestion] ColorGradePlan 未校验 project_id/timeline_id 非空与 base_revision 非负；快照 detected_input_spaces 值未校验
  - 位置: src/features/color/ai/color_context.py:243-247, 256-262, 85
  - 证据:
    ```python
    project_id: str
    timeline_id: str
    ...
    base_revision: int
    ...
    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("ColorGradePlan.plan_id is required")
    ```
    校验只覆盖 plan_id/operations/context_snapshot_hash；`detected_input_spaces: Mapping[str, str]` 的值也未对照 `KNOWN_COLOR_SPACES` 验证（快照自身的三个 space 字段倒是验证了）。
  - 影响: 空 project_id/timeline_id 或负 base_revision 的计划可合法构造并绑定哈希，下游按 revision 做一致性检查时行为未定义；非法输入色彩空间进入快照后经 to_dict 持久化扩散。
  - 建议: 补齐 `__post_init__` 非空/非负校验，`detected_input_spaces` 值纳入 `KNOWN_COLOR_SPACES` 校验（或明确允许未知检测值并标注）。
  - 置信度: 高

### 通过项

- 线程安全：纯 Qt-free 不可变契约对象，无共享可变状态、无锁需求（可变性缺口见 AUD-24301）。
- GPU 资源生命周期：不涉及 ModernGL/纹理/缓冲，无 deferred-release 义务。
- GLSL·HDR 正确性：HDR/SDR LUT 互斥方向判断、SDR 0..1 与 HDR nits 双语义注释一致；gain>1 仅告警不硬拦符合 HDR 不无理由 clamp 原则（缺口见 AUD-24302/24305）。
- 信号防递归：无 Qt 控件与信号连接。
- 状态提交一致性：plan_hash 正确排除 plan_id（内容哈希）、绑定 context_snapshot_hash + base_revision，to_dict 完整覆盖持久化字段。
- 异常与取消路径：校验统一抛带字段名的 ValueError，无宽泛 except 掩盖领域错误。
- 输入验证：色彩空间/传递函数词汇表、op kind、confidence ∈ [0,1]、LookRecipe.version ≥ 1 均有校验（其余缺口见各发现）。
- 性能模式：哈希/序列化/去重均为小对象 O(n)，无 4K 帧路径热点。
- 类型注解：全量类型注解，`__all__` 与导出一致，无缺失。
