# batch-268 审查报告 — src/features/teaching_animation/ai/（7 文件，2,912 行）

编号区间：AUD-26800–AUD-26899。严重度：Critical 0 / Major 5 / Suggestion 16。
通用上下文：本批全部为 Qt-free 纯数据/纯逻辑模块（无 OpenGL、无 Qt 信号）；`creative_templates.py` 与 `eval_rubric.py` 带 DORMANT 标记；`export_job.py` 的 API 在 src/ 内无任何调用方（与 grep 验证一致）。跨模块引用（proposal.py 的 SOURCE_* 常量、brief.py 的 CANVAS_*、core.model）均已验证存在。

---

## src/features/teaching_animation/ai/creative_templates.py (行数: 409)

### 发现

- [AUD-26800] [Major] `supports_4k` 能力声明永远无法被触发——兼容性检查根本没有分辨率参数
  - 位置: src/features/teaching_animation/ai/creative_templates.py:132, 278-303, 384
  - 证据:
    ```python
    supports_4k: bool = True          # L132（social_card_9x16 显式置 False，L384）
    ...
    def compatibility_issues(
        self, template: CreativeTemplate, *,
        duration_seconds: float, canvas_aspect: str, transparent: bool,
    ) -> tuple[str, ...]:
        ...
        if canvas_aspect == "9:16" and not caps.supports_vertical:
    ```
  - 影响: 模块文档承诺"planner 可提前拒绝不支持的组合"，但 `compatibility_issues` 只接收时长/画幅/透明三个参数，4K 请求（如 3840×2160 profile）不会让 `supports_4k=False` 的模板（social_card_9x16）被拒；全库 grep 确认 `supports_4k` 无其他读取点。约束失效会在导出/渲染阶段才暴露。
  - 建议: 给 `compatibility_issues` 增加 `width/height`（或 `DeliveryProfile`）参数，并在尺寸超过 1920 宽/高时检查 `caps.supports_4k`。
  - 置信度: 高

- [AUD-26801] [Suggestion] `Iterable` 导入未使用
  - 位置: src/features/teaching_animation/ai/creative_templates.py:25
  - 证据: `from typing import Any, Iterable, Mapping`（文件内无 `Iterable` 使用点）
  - 影响: 死导入，轻微噪音。
  - 建议: 删除 `Iterable`。
  - 置信度: 高

### 通过项
- 线程安全：纯数据注册表，无共享可变状态跨线程写入路径（追加语义下 GIL 足够）。
- GPU 资源生命周期 / GLSL·HDR 正确性：模块不触及 OpenGL 与着色器。
- 信号防递归：无 Qt 信号。
- 状态提交一致性：frozen dataclass + `__post_init__` 校验（template_id/version/category）完整。
- 异常与取消路径：注册冲突以 `False` 返回而非异常，语义清晰。
- 输入验证：category 闭集校验到位。
- 类型注解：完整。

---

## src/features/teaching_animation/ai/delivery_profile.py (行数: 355)

### 发现

- [AUD-26802] [Suggestion] Registry 文档承诺 `version_of` 保留旧版本，方法不存在且 register 实为整体替换
  - 位置: src/features/teaching_animation/ai/delivery_profile.py:264-280
  - 证据:
    ```python
    class DeliveryProfileRegistry:
        """Append-only registry of delivery profiles.
        ... re-registering the same id replaces (for upgrades) but
        keeps the old version available through :meth:`version_of`.
        """
    ```
    （类内仅有 register/get/require/all_profiles/profiles_for_aspect，无 `version_of`）
  - 影响: 依赖文档的调用方会调用不存在的方法（AttributeError）；"append-only" 描述与替换行为矛盾。
  - 建议: 实现 `version_of`（保留 id→版本列表），或修正 docstring 删除该承诺。
  - 置信度: 高

- [AUD-26803] [Suggestion] `width/height` 与 `canvas_aspect` 标签无一致性校验
  - 位置: src/features/teaching_animation/ai/delivery_profile.py:167-170
  - 证据:
    ```python
    if self.canvas_aspect not in SUPPORTED_CANVAS_ASPECTS:
        raise ValueError(...)
    if self.width <= 0 or self.height <= 0:
        raise ValueError("DeliveryProfile.width/height must be positive")
    ```
  - 影响: 自定义 profile 可声明 `canvas_aspect="16:9"` 却配 1080×1920 尺寸通过构造；planner 按标签布局、renderer 按尺寸渲染，二者错位导致构图/安全区计算错误。
  - 建议: 构造时按 `DEFAULT_CANVAS_SIZES` 的比例容差校验 `width/height ≈ aspect_ratio`。
  - 置信度: 中

- [AUD-26804] [Suggestion] `duration_frames` 对非正时长静默映射为 1 帧
  - 位置: src/features/teaching_animation/ai/delivery_profile.py:184-185
  - 证据: `def duration_frames(self, seconds: float) -> int: return max(1, int(round(seconds * self.fps)))`
  - 影响: `seconds=-5` 或 `0` 均返回 1 帧，负时长错误被掩盖为"极短片段"，下游差异/时长一致性检查失真。
  - 建议: 对 `seconds <= 0` 抛 ValueError 或返回 0 由调用方处理。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：纯数据模块，不适用，无风险。
- 异常与取消路径：`builtin_profile`/`require` 对未知 id 抛 KeyError 语义明确。
- 输入验证：SafeArea/SubtitleRegion/codec/container/fps/max_duration 闭集与范围校验完整。
- 性能模式：`ResponsiveLayout.pixel_rect` O(1)，钳位逻辑正确（含越界钳回安全区）。
- 类型注解：完整。

---

## src/features/teaching_animation/ai/design_validator.py (行数: 777)

### 发现

- [AUD-26805] [Major] 素材校验复用 `MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS` 而非专用的 `MOTION_GRAPHIC_ASSET_UNRESOLVED`
  - 位置: src/features/teaching_animation/ai/design_validator.py:633-651（L635、L645 两处）
  - 证据:
    ```python
    findings.append(DesignFinding(
        code=MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS,
        severity="error",
        element_id=element_id,
        message=f"元素 {element_id} 引用了未声明的素材 {ref}。",
    ```
  - 影响: error 级 finding 会阻断预览；quality_gate.py:268-279 将 `issue.code` 原样透传，UI 按 error_codes 前缀路由并显示 `suggested_next_step`——用户看到的恢复建议是"将元素移回安全区后重新预览"，与素材缺失完全无关（正确词汇 `MOTION_GRAPHIC_ASSET_UNRESOLVED` 已在 error_codes.py 定义且 quality_gate 自身 L199 在用）。
  - 建议: 两处改用 `MOTION_GRAPHIC_ASSET_UNRESOLVED`。
  - 置信度: 高

- [AUD-26806] [Major] 对比度检查实现与文档承诺不符：只对比场景背景色，而非"文字背后的元素"
  - 位置: src/features/teaching_animation/ai/design_validator.py:20-21, 371, 424-427
  - 证据:
    ```python
    # 模块文档（L20-21）：
    # * contrast between text fill and the element behind it (accessibility level)
    bg_color = background.get("color") if isinstance(background, Mapping) else None   # L371
    ...
    fg_color = style.get("color") or style.get("fill")                                 # L425
    ratio = contrast_ratio(fg_color, bg_color)                                         # L426
    ```
  - 影响: 文字位于卡片/形状之上（Motion Graphic 常见排版）时，对比度按整幅背景计算：浅色卡片上的白字可通过校验（假阴性，产出不可读且是 error 级阻断规则的漏放行），深色卡片上的合规文字可能被误判 error 阻断预览。
  - 建议: 短期在文档注明"仅对场景背景近似"；正确修复为对与文本 bbox 相交的最上层不透明元素取色再算对比度（可复用 `_bbox_overlap`）。
  - 置信度: 高（文档/实现错位确凿；实际误判程度取决于场景排版）

- [AUD-26807] [Suggestion] 时长不一致与文本重叠 finding 复用 `MOTION_GRAPHIC_TEXT_OVERFLOW` 错误码
  - 位置: src/features/teaching_animation/ai/design_validator.py:684-686, 757-758
  - 证据:
    ```python
    findings.append(DesignFinding(
        code=MOTION_GRAPHIC_TEXT_OVERFLOW,
        severity="warning",
        message=f"场景时长 {duration_frames} 帧与需求 {expected_duration_frames} 帧不一致。",
    ```
  - 影响: warning 级不阻断，但按 code 查 `_SUGGESTED_NEXT_STEPS` 得到"缩短文案、调小字号或加大文本框后重试"，对时长问题与重叠问题均为误导性提示；破坏 MG-23"code 分组驱动 UI 行为"的设计规则。
  - 建议: 为时长一致性新增专用 code（如 `MOTION_GRAPHIC_DURATION_MISMATCH`），重叠可归入 `MOTION_GRAPHIC_VISUAL_OVERLAP` 类新码或复用最贴近的现有码。
  - 置信度: 高

- [AUD-26808] [Suggestion] 顶层 `int()` 直接转换外部场景字段，验证器自身可抛未捕获异常
  - 位置: src/features/teaching_animation/ai/design_validator.py:723-725
  - 证据:
    ```python
    width = int(data.get("width", 0) or 0)
    height = int(data.get("height", 0) or 0)
    duration_frames = int(data.get("duration_frames", 0) or 0)
    ```
  - 影响: 输入是 LLM 产出的 dict（外部数据）；`"1920px"`、`None` 显式存在（`or 0` 只挡 falsy 字符串不挡非数字串）等情况会抛 ValueError，验证器崩溃而非产出 finding。module.py:4375-4381 与 quality_gate 均有 `except Exception` 兜底，结果是"设计校验静默不运行"，削弱 MG-10 阻断保证。
  - 建议: 用与 `_number()` 一致的防御式解析，解析失败产出场景级 error finding。
  - 置信度: 中

- [AUD-26809] [Suggestion] `_check_overlap` O(n²) 且不因元素超限提前截断
  - 位置: src/features/teaching_animation/ai/design_validator.py:680-684, 478-495, 743-749
  - 证据:
    ```python
    for i in range(len(text_boxes)):
        for j in range(i + 1, len(text_boxes)):
    ```
    （`max_elements_error` 超限时仅追加 finding，L743-749 仍继续执行全部检查）
  - 影响: 对抗性/病态 LLM 场景（数万 text 元素）验证耗时平方级膨胀；该路径在 LLM 构建回调线程上执行，会拖慢生成确认流程。
  - 建议: 复杂度超硬上限时按 2D 扫描线或网格分桶做重叠检测，或直接跳过重叠检查（已有 error 阻断）。
  - 置信度: 中

- [AUD-26810] [Suggestion] 场景无元素（或元素全无 id）时悬空动画引用不被报告
  - 位置: src/features/teaching_animation/ai/design_validator.py:602-603
  - 证据: `if target and element_id_set and target not in element_id_set:`（`element_id_set` 为空时短路）
  - 影响: `elements=[]` + 引用任意目标的动画可通过重复项检查，渲染期才失败。
  - 建议: 去掉 `element_id_set and` 守卫；目标集合为空时任何非空 target 均应报错。
  - 置信度: 高

- [AUD-26811] [Suggestion] 未使用导入 `SUPPORTED_ELEMENT_TYPES`、`TeachingSceneDocument`、`Iterable`
  - 位置: src/features/teaching_animation/ai/design_validator.py:37, 39
  - 证据: `from typing import Any, Iterable, Mapping` / `from ..core.model import SUPPORTED_ELEMENT_TYPES, TeachingSceneDocument`（三者仅在 docstring 文本中被提及，代码无引用）
  - 影响: 死导入；`..core.model` 依赖被无谓引入。
  - 建议: 删除未用名称。
  - 置信度: 高

### 通过项
- 线程安全：无状态纯函数，无共享可变状态。
- GPU 生命周期 / GLSL·HDR / 信号防递归：不涉及。
- 状态提交一致性：finding 携带 element_id/path，供阻断路径解释性呈现，符合 MG-10。
- WCAG 对比度数学（`_relative_luminance`/`contrast_ratio`）与颜色解析（#rgb/#rrggbb/rgb()）实现正确，解析失败返回 None 跳过检查的设计合理。
- 复杂度/重复/闪烁启发式逻辑自洽（opacity 翻转计数阈值）。
- 类型注解：完整。

---

## src/features/teaching_animation/ai/error_codes.py (行数: 422)

### 发现

- [AUD-26812] [Suggestion] `MotionGraphicAuditEvent.from_dict` 防御不一致：timestamp 为 None 会崩溃，brief_revision 不做类型收敛
  - 位置: src/features/teaching_animation/ai/error_codes.py:314-315, 324
  - 证据:
    ```python
    timestamp=float(data.get("timestamp", 0.0)),      # 键存在且为 None → TypeError
    brief_revision=data.get("brief_revision"),        # 声明 int | None，str 原样通过
    ...
    cost_units=float(data.get("cost_units", 0.0) or 0.0),  # 有 `or` 防护
    ```
  - 影响: 加载被污染/手改的 .topos 诊断数据时 `float(None)` 抛 TypeError；brief_revision 类型注解被破坏，后续按 int 比较的代码可能出错。
  - 建议: `timestamp` 采用与 cost_units 相同的 `or 0.0` 防护；`brief_revision` 显式 `int(...) if ... is not None else None`。
  - 置信度: 中

### 通过项
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：不适用，无风险。
- 状态提交一致性：`sanitized()` 经 `replace()` 生成新事件，`_copy_json` 深拷贝避免共享可变 details。
- 输入验证：`redact_for_storage` 敏感键闭集 + 正则掩码 + 截断哈希，符合 .topos 持久化红线（API key/绝对路径/超长 prompt）。
- `canonical_json_hash`（sort_keys + 分隔符 + sha256）实现稳定。
- `_SUGGESTED_NEXT_STEPS` 对每个 code 均有条目，未知 code 有兜底文案。
- 类型注解：完整。

---

## src/features/teaching_animation/ai/eval_rubric.py (行数: 337)

### 发现

- [AUD-26813] [Suggestion] `weight=NaN` 通过校验，`weighted_score` 变为 NaN
  - 位置: src/features/teaching_animation/ai/eval_rubric.py:76-77, 97-101
  - 证据:
    ```python
    if self.weight < 0:      # NaN < 0 为 False，放行
        raise ValueError("weight cannot be negative")
    ...
    total_weight = sum(score.weight for score in self.scores)   # NaN 污染
    ```
  - 影响: 人工评审填充的主观项若带 NaN 权重，回归检测（`regression_detected` 比较 weighted_score）恒为 False，质量回归静默漏报。
  - 建议: 校验改为 `if not math.isfinite(self.weight) or self.weight < 0`（score 的 `[0,1]` 比较已天然挡住 NaN，可复用思路）。
  - 置信度: 中

### 通过项
- 线程安全：`CreativeEvalTraceStore` 为进程内 append-only 列表，CPython 下 add/读取无撕裂风险（文档已注明宿主负责持久化）。
- GPU 生命周期 / GLSL·HDR / 信号防递归：不涉及。
- 状态提交一致性：`CreativeEvalTrace.hash()` 用 canonical json + sha256，回归比对依据稳定。
- 输入验证：criterion 闭集（ALL_CRITERIA）、score ∈ [0,1]、除零防护（expected<=0、total<=0、ratios 为空）均到位。
- 客观打分函数（duration/min-max 比、覆盖率、对比度通过率、重叠、字幕对齐）边界处理正确。
- 类型注解：完整。

---

## src/features/teaching_animation/ai/export_job.py (行数: 329)

### 发现

- [AUD-26814] [Major] 取消/质量阻断/覆盖被拒路径均不清理临时文件，也无任何清理 API——.tmp 大文件泄漏
  - 位置: src/features/teaching_animation/ai/export_job.py:200-216, 218-223
  - 证据:
    ```python
    if self.cancel_requested:
        self.status = EXPORT_CANCELLED
        return                                  # output_temp_path 留在磁盘
    if postflight.is_blocking:
        self.status = EXPORT_FAILED
        return                                  # 同上
    ...
    def fail(self, *, code, message, now=None):
        ...                                     # 同样不清理
    ```
    （全模块无 os.remove/os.unlink/unlink 调用）
  - 影响: 4K ProRes 导出取消一次即可遗留数 GB `.tmp`；与模块自己声明的"cancelled / crashed export never leaves a half-written file"目标只保护了最终路径，未覆盖临时文件。本模块 API 目前在 src/ 内无调用方，接线后将暴露此缺口。
  - 建议: `finish`/`fail` 的非成功路径对 `output_temp_path` 做 best-effort `os.unlink`（吞掉 FileNotFoundError），或提供显式 `cleanup_temp()` 由宿主在取消时调用。
  - 置信度: 高（模块内缺失确凿；宿主适配器是否兜底无法从本批验证）

- [AUD-26815] [Major] `finish()` 内 `os.makedirs`/`os.replace` 的 OSError 逃逸，任务停留在不一致状态
  - 位置: src/features/teaching_animation/ai/export_job.py:196-216
  - 证据:
    ```python
    with self._lock:
        timestamp = now if now is not None else time.time()
        self.finished_at = timestamp            # 先写入
        self.postflight = postflight            # 先写入
        ...
        os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)   # L214 可抛
        os.replace(output_temp_path, final_path)                        # L215 可抛（跨盘/权限/临时文件缺失）
        self.status = EXPORT_SUCCEEDED
    ```
  - 影响: 抛出时 `finished_at`/`postflight` 已设置而 `status` 仍为 `EXPORT_RUNNING`——`to_snapshot()` 呈现"运行中但已结束"的矛盾状态；若宿主未捕获并补调 `fail()`，任务永久卡在 running。
  - 建议: 将文件系统操作包在 try/except OSError 中，失败即走 `EXPORT_FAILED` + `diagnostic_code="EXPORT_FINALIZE_FAILED"`；或先完成 replace 再提交状态字段。
  - 置信度: 中

- [AUD-26816] [Suggestion] 覆盖审批检查与 `os.replace` 之间存在 TOCTOU 竞态
  - 位置: src/features/teaching_animation/ai/export_job.py:209-215
  - 证据: `if os.path.exists(final_path) and not self.request.overwrite_existing: ... return` 随后 `os.replace(...)`
  - 影响: 检查与替换之间目标文件被其他进程/任务创建时，会在未获 `overwrite_existing` 批准的情况下被覆盖，削弱"显式覆盖审批"设计规则（单用户桌面场景窗口小，故仅 Suggestion）。
  - 建议: Windows 外 `os.replace` 本身不可原子探测；可用 `O_EXCL` 硬链接/临时探测，或至少在 replace 前后 stat 对比并回滚。
  - 置信度: 中

- [AUD-26817] [Suggestion] `ExportRequest` 无输入验证，绕过 MG-11 的 codec/container 闭集词汇
  - 位置: src/features/teaching_animation/ai/export_job.py:58-74
  - 证据:
    ```python
    @dataclass(frozen=True)
    class ExportRequest:
        scene_id: str
        output_path: str
        profile_id: str
        container: str = "mp4"
        codec: str = "h264"
        fps: float = 24.0
        ...
    ```
    （无 `__post_init__`；不校验 `SUPPORTED_CODECS`/`SUPPORTED_CONTAINERS`、fps/width/height 正性、output_path 非空）
  - 影响: 闭集规则"LLM 不能发明 codec 标识"在这一入口失效，非法值延迟到 ffmpeg 调用才失败，诊断信息劣化。
  - 建议: 增加 `__post_init__` 复用 delivery_profile 的 `SUPPORTED_CODECS`/`SUPPORTED_CONTAINERS` 与正性校验。
  - 置信度: 中

- [AUD-26818] [Suggestion] `start()` 失败借用语义无关的 `MOTION_GRAPHIC_CLIP_INSERT_FAILED`；`EXPORT_*` 码为散落字符串未入词汇表
  - 位置: src/features/teaching_animation/ai/export_job.py:167-172, 258, 288, 296
  - 证据:
    ```python
    raise MotionGraphicFailure(
        MOTION_GRAPHIC_CLIP_INSERT_FAILED,      # "时间线/轨道"域的码用于导出任务状态机
        f"导出任务当前状态 {self.status}，无法开始。",
    ```
    （`"EXPORT_OVERWRITE_REQUIRED"`、`"EXPORT_FILE_MISSING"`、`"EXPORT_EMPTY_FILE"`、`"EXPORT_INSUFFICIENT_DISK"` 均为局部字符串常量）
  - 影响: 违反本模块"stable codes"声明与 error_codes.py 的集中词汇规则：UI 按前缀路由会把导出启动失败当成时间线插入失败提示；EXPORT_* 码无 `suggested_next_step` 条目。
  - 建议: 在 error_codes.py 增加 `MOTION_GRAPHIC_EXPORT_*` 组并统一引用。
  - 置信度: 高

- [AUD-26819] [Suggestion] 未使用导入 `Callable`/`Iterable`；`is_terminal` 属性读取未持锁
  - 位置: src/features/teaching_animation/ai/export_job.py:29, 161-163
  - 证据: `from typing import Any, Callable, Iterable, Mapping`（后两者未用）；`def is_terminal(self) -> bool: return self.status in TERMINAL_EXPORT_STATES`（其余所有访问均在 `self._lock` 内）
  - 影响: 死导入；`is_terminal` 与锁协议不一致——CPython 下 str 读原子、实际风险低，但破坏了"worker 在锁下变更"的既定契约。
  - 建议: 删除未用导入；`is_terminal` 内取锁或注释说明依赖 GIL 的单字段原子读。
  - 置信度: 高

### 通过项
- 线程安全：核心状态机（start/report_progress/request_cancel/finish/fail/to_snapshot）全部持 RLock，进度值钳位 [0,1]。
- GPU 生命周期：模块不拥有 GL 资源（渲染在宿主适配器）。
- GLSL·HDR / 信号防递归：不涉及。
- 状态提交一致性：原子 `os.replace`（temp→final）方向正确；终态闭集定义清晰；取消在 start/finish 两处均有处理。
- 输入验证：`preflight_disk` 覆盖已存在文件与磁盘不足两项并产出 error finding。
- 性能模式：锁内无长循环；postflight_from_stat 仅做 stat 级检查。
- 类型注解：完整。

---

## src/features/teaching_animation/ai/fallback.py (行数: 283)

### 发现

- [AUD-26820] [Suggestion] 用户取消被归类为 `FALLBACK_PROVIDER_UNAVAILABLE` 且 `allows_template=True`；错误消息文本嗅探脆弱
  - 位置: src/features/teaching_animation/ai/fallback.py:173-188
  - 证据:
    ```python
    if code == MOTION_GRAPHIC_LLM_CANCELLED or "cancel" in message.lower():
        return MotionGraphicFallbackDecision(
            state=FALLBACK_PROVIDER_UNAVAILABLE,   # 取消 ≠ 提供方不可用
            error_code=MOTION_GRAPHIC_LLM_CANCELLED,
            allows_template=True,
    ```
  - 影响: (1) 取消后 `is_failure=True`，`FallbackPolicy.may_use_template` 放行"改用内置模板"入口——用户刚取消却又收到模板推销，语义错位；(2) `"cancel"/"timeout" in message.lower()` 会把含这些词的无关异常（如 "operation could not be cancelled"）误分类。
  - 建议: 取消单独建模（如 FALLBACK_CANCELLED 或保持 NONE 并标注 cancelled），消息嗅探仅在无 code 时兜底并收紧匹配短语。
  - 置信度: 中（可能是有意的产品决策，但状态命名与语义明显不符）

- [AUD-26821] [Suggestion] `FallbackState` str 子类为死代码
  - 位置: src/features/teaching_animation/ai/fallback.py:54-55
  - 证据:
    ```python
    class FallbackState(str):
        """The explicit outcome of a generation attempt."""
    ```
    （全库 grep 无 `FallbackState` 实例化/isinstance 使用，常量均为普通 str 并入 `__all__` 导出）
  - 影响: 导出未被使用的类型，误导调用方以为应使用该类型标注。
  - 建议: 删除该类及 `__all__` 条目，或让常量改为 `FallbackState` 实例以名副其实。
  - 置信度: 高

### 通过项
- 线程安全：纯数据 + 纯函数，无可变共享状态。
- GPU 生命周期 / GLSL·HDR / 信号防递归：不涉及。
- 状态提交一致性：MG-05 核心不变量成立——`may_use_template` 要求 `decision.is_failure` 且 `template_allowed`，`template_chosen` 在禁用时抛 `MotionGraphicFailure`，模板只能显式 opt-in，无静默替换路径。
- 异常与取消路径：未识别异常落入"provider 不可用"而非冒充成功，符合"绝不静默替换"红线。
- 输入验证：`MotionGraphicFallbackDecision.__post_init__` 校验 state 闭集。
- 类型注解：完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-26800, AUD-26805, AUD-26806, AUD-26814, AUD-26815 |
| Suggestion | 16 | AUD-26801–26804, AUD-26807–26813, AUD-26816–26821 |

主题性观察：本批最集中的缺陷模式是 (1) 稳定错误码被跨域复用（design_validator 三处、export_job 两处），直接破坏 error_codes.py 自己声明的"code 前缀驱动 UI 行为"契约；(2) 声明的能力/文档承诺未实现（supports_4k、version_of、element-behind 对比度）；(3) export_job 的文件收尾路径（临时文件清理、finalize 异常）是接线上生产前需补齐的主要风险点。
