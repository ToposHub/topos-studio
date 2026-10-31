# batch-269 — src/features/teaching_animation/ai//

- 批次: batch-269 [P2]
- 编号区间: AUD-26900–AUD-26999
- 文件数: 8（2,760 行）
- 汇总: Critical 0 · Major 3 · Suggestion 12
- 说明: 本批全部为纯函数/纯数据模块（regex/stdlib，无 Qt 信号、无 OpenGL、无 GLSL、无直接持久化）。九个维度中的线程安全、GPU 资源生命周期、GLSL/HDR 正确性、信号防递归对全部文件均为"不适用且通过"；审查重点落在输入验证、异常路径与 gate 逻辑正确性。涉及 `detect_page_count`（恒 ≥1，无除零风险）与 `HtmlSanitizer._allow_*`（属性名核实存在）两处跨模块依赖均已到源验证。

## src/features/teaching_animation/ai/html_design_quality.py (行数: 411)

### 发现

- [AUD-26900] [Suggestion] `_page_structure_issues` 的宽泛 except 会把解析器自身缺陷伪装成"模型 HTML 结构问题"
  - 位置: src/features/teaching_animation/ai/html_design_quality.py:142-147
  - 证据:
    ```python
    parser = _PageStructureParser()
    try:
        parser.feed(str(document or ""))
        parser.close()
    except Exception:  # noqa: BLE001 - malformed model HTML is a diagnostic
        return ("页面 HTML 结构无法完成静态解析，需重新生成独立页面容器",)
    ```
  - 影响: `html.parser` 对畸形 HTML 极少抛异常，此 except 实际捕获的多半是宿主 bug（AttributeError/RecursionError 等）。这些错误会被归因为"模型 HTML 无法解析"并触发重生成循环，违反"不得宽泛 except 掩盖领域错误"的铁律（错误归因错误，且无日志）。
  - 建议: 至少限定为 `html.parser.HTMLParser` 相关异常，或对非预期异常记录日志后再降级；不要将任意异常映射为用户可见的"需重新生成"文案。
  - 置信度: 低

### 通过项
- 线程安全: 纯函数 + 模块级只读编译正则 + frozen dataclass，无可变共享状态
- GPU 资源生命周期: 不涉及（无 ModernGL 对象）
- GLSL/HDR 正确性: 不涉及（纯 HTML/CSS 静态信号）
- 信号防递归: 不涉及（无 Qt 控件/信号）
- 状态提交一致性: 只读评估，不写工程；`needs_repair`/`repair_feedback` 与各分项分数一致
- 异常与取消路径: `evaluate_html_design` 各评分函数对空文档/None 输入有兜底（`str(document or "")`、`if pages else 0.0`、`len(fragments) <= 1`）
- 输入验证: `_responsive_layout_signal` 用 `max(1.0, float(extent))` 防零视口；分数统一 clamp 到 [0,1]
- 性能模式: 正则均为线性（`[^>]*`/惰性组受 `>`/引号约束），`_word_set` 仅做 bigram 集合，规模受生成 HTML 体积约束
- 类型注解: 完整（含 `tuple[str, ...]`、`frozenset` 等精确类型）

## src/features/teaching_animation/ai/html_edit_audit.py (行数: 116)

### 发现

- [AUD-26901] [Suggestion] `changed` 与 `summary()` 语义不一致：仅有 `html_size_delta` 变化时判定"已改变"但摘要说"无变化"
  - 位置: src/features/teaching_animation/ai/html_edit_audit.py:54-78
  - 证据:
    ```python
    @property
    def changed(self) -> bool:
        return bool(
            self.before_page_count != self.after_page_count
            or self.changed_pages
            or self.added_colors
            or self.removed_colors
            or self.html_size_delta
        )

    def summary(self) -> str:
        ...
        if not parts:
            return "未检测到可见内容或颜色变化"
    ```
  - 影响: 仅空白/注释变化（size delta ≠ 0、页数/文案/颜色均不变）时，`changed` 为 True 而用户读到的摘要是"未检测到可见内容或颜色变化"。若调用方以 `changed` 决定是否提交一步编辑记录，用户会在审计记录与实际行为之间产生矛盾。
  - 建议: 要么在 `summary()` 中补充"仅源码体积变化 N 字符"分支，要么将 `html_size_delta` 从 `changed` 的判定条件中移除。
  - 置信度: 高

### 通过项
- 线程安全: 纯函数 + frozen dataclass
- GPU 资源生命周期 / GLSL·HDR / 信号防递归: 不涉及
- 状态提交一致性: 只做差异描述，不提交状态；`to_dict` 字段完整
- 异常与取消路径: 对 None/空输入统一 `str(x or "")` 兜底，无裸异常路径
- 输入验证: 页数对不齐时用空串补位比较（`old_pages[index] if index < len(old_pages) else ""`），无越界
- 性能模式: 线性正则，无重复全文扫描浪费
- 类型注解: 完整

## src/features/teaching_animation/ai/html_edit_autonomy.py (行数: 630)

### 发现

- [AUD-26902] [Major] 增/删页后置条件正则对常见中文措辞双向误判：把"添加页码/删除页面底部…"当成增删页，而真正的"删除第2页"反而漏判
  - 位置: src/features/teaching_animation/ai/html_edit_autonomy.py:222-223, 410-419
  - 证据:
    ```python
    _ADD_PAGE_RE = re.compile(r"(增加|新增|插入|再加|添加|加)(一|两|三|四|五|几|\d+)?\s*页")
    _REMOVE_PAGE_RE = re.compile(r"(删除|删掉|去掉|移除|减少|减掉)(一|两|三|四|五|几|\d+)?\s*页")
    ...
    if _ADD_PAGE_RE.search(text) and revised_pages <= original_pages:
        issues.append(
            f"要求增加页面，但页数仍为 {revised_pages}（原 {original_pages}）"
        )
    ```
  - 影响: 已实测验证（python3 复跑正则）：`"为每一页添加页码"` 命中 `_ADD_PAGE_RE`、`"删除页面底部的版权信息"`/`"减少页面之间的留白"` 命中 `_REMOVE_PAGE_RE`。这些与页数无关的正常修改会被后置条件判为"页数未变→未满足需求"，正确修订被永久拒绝、陷入修复循环。反向：`"删除第2页"`、`"增加第 4 页"`（"第 N 页"序数形）两个正则均不命中，真增删页反而不设防。gate 失败方向虽是安全侧（不写坏数据），但会持续否决合法编辑，属功能缺陷。
  - 建议: 增/删页判定要求动词后紧跟数量词（去掉 `?` 可选性）或"第 N 页"序数形；对"页码/页面之间/页面底部"等复合词加负向前瞻排除；与 `_EXPLICIT_PAGE_ORDINAL_RE` 复用以覆盖序数表达。
  - 置信度: 高

- [AUD-26903] [Major] `_REMOVE_QUOTED_RE` 把"不要删除「X」"及"删掉A，改成「B」"中的 X/B 误判为待删文案
  - 位置: src/features/teaching_animation/ai/html_edit_autonomy.py:224-226, 394-404
  - 证据:
    ```python
    _REMOVE_QUOTED_RE = re.compile(
        r"(删除|删掉|去掉|移除|不要)[^「”\"]{0,12}[「”\"]([^」”\"]{1,80})[」”\"]"
    )
    ...
    if quote in removed_quotes:
        if quote in revised_plain or quote in revised:
            issues.append(f"要求删除的「{quote}」仍然出现在修改后的网页中")
    ```
  - 影响: 已实测验证：`"不要删除「公司简介」这一段"` 捕获「公司简介」，`"删掉旧数据，把文案改成「最新数据」"` 捕获「最新数据」。前者导致"用户明确要保留的文案被 gate 要求必须消失"，后者导致"用户要求写入的新文案同时出现在"要求包含"与"要求删除"两个集合，任何修订必被否决。中间跨度 `[^「”"]{0,12}` 允许跨越逗号和普通文字，混合型请求（改 A 删 B）是常态输入。
  - 建议: 去掉"不要"这一否定动词分支，或要求动词与引号之间不允许出现逗号/句号/其他动词；对同一引号同时命中"要求包含"与"要求删除"时优先按"包含"处理并记为歧义而非直接否决。
  - 置信度: 高

- [AUD-26904] [Suggestion] `page_texts` 截断 100 字符导致 `explicit_noop_reason` 对长文案漏判 no-op
  - 位置: src/features/teaching_animation/ai/html_edit_autonomy.py:108, 615-620
  - 证据:
    ```python
    page_texts.append(_visible_text(text[match.end():end])[:100])
    ...
    page_text = dossier.page_texts[index]
    if all(value in page_text for value in quoted):
        return f"第 {index + 1} 页已包含指定文案「{quoted[0]}」，无需修改"
    ```
  - 影响: 引号文案最长 80 字符，但快照只保留每页前 100 个可见字符。指定文案存在于第 100 字符之后时 no-op 判定失效，退化为一次不必要的模型往返。方向安全（不会误报 no-op），仅损失效率与确定性。
  - 建议: 在 `EditDossier` 中为 no-op 判定保留更长（或未截断）的页文本，快照摘要另用截断值。
  - 置信度: 中

### 通过项
- 线程安全: 纯函数 + frozen dataclass；LLM 调用由调用方经既有通道传输，本模块无线程面
- GPU 资源生命周期 / GLSL·HDR / 信号防递归: 不涉及
- 状态提交一致性: `deterministic_minimal_html_edit` 仅做单一文本节点替换且保留其余源码字节，postcondition/preservation 检查与之配套；undo/redo 由上层承载
- 异常与取消路径: `parse_edit_plan` 容错解析失败返回 None 而非抛出；`_is_palette_color` 数值解析受正则保证
- 输入验证: 所有入口统一 `str(x or "")`；`deterministic_minimal_html_edit` 对嵌套标记/多 H1/含实体标题一律返回 None 退回模型路径
- 性能模式: 惰性 `evaluate_html_design` 导入避免循环依赖；所有扫描单遍完成
- 类型注解: 完整

## src/features/teaching_animation/ai/html_generation_protocol.py (行数: 582)

### 发现

- [AUD-26905] [Suggestion] STATIC_ANALYSIS 阶段对 `evaluate_html_design` 的 `except Exception` 静默置空结构诊断
  - 位置: src/features/teaching_animation/ai/html_generation_protocol.py:253-263
  - 证据:
    ```python
    page_structure_issues: tuple[str, ...] = ()
    try:
        from .html_design_quality import evaluate_html_design

        page_structure_issues = evaluate_html_design(
            sanitized.html,
            viewport_width=float(cons.viewport_width),
            viewport_height=float(cons.viewport_height),
        ).page_structure_issues
    except Exception:  # noqa: BLE001 - browser/static gates remain authoritative
        page_structure_issues = ()
    ```
  - 影响: 注释上方刚声明"页面边界是安全/正确性契约"，但评估器自身的 bug（或惰性导入失败）会被完全吞掉并当作"无结构问题"。嵌套页 deck 若恰逢评估器缺陷将静默通过 STATIC_ANALYSIS，违背"能力缺失须报告不得隐藏"的本模块自述原则，也违反宽泛 except 铁律。
  - 建议: 捕获后将"结构检查不可用"记为一条诊断或日志，而不是置空元组；至少限定异常类型。
  - 置信度: 中

- [AUD-26906] [Suggestion] `_run_preview` 捕获循环（2 + N 页次，每次 timeout 20s）无取消点
  - 位置: src/features/teaching_animation/ai/html_generation_protocol.py:431-494
  - 证据:
    ```python
    def _capture(frame: int) -> CapturedFrame | CaptureFailure:
        nonlocal duration_ms
        result = preview_backend.capture(
            capture_html, surface,
            frame=int(frame), fps_num=fps_num, fps_den=1,
            csp=csp, url_policy=policy, timeout_ms=20000,
        )
    ...
    for _ in range(2):
        result = _capture(int(preview_frame))
    ...
    for index, seconds in enumerate(preview_timing.durations):
    ```
  - 影响: 每次预览最少 2 次确定性捕获 + 每页 1 次探测，单次超时 20s。多页 deck 最坏情况可阻塞数分钟且无任何取消/中止检查点；用户取消生成后工作线程仍持续占用浏览器后端。
  - 建议: 为 `_run_preview`/`run_html_generation_pipeline` 增加可选 `should_cancel: Callable[[], bool]`，在每页探测前检查。
  - 置信度: 中

- [AUD-26907] [Suggestion] `page_count == 1` 时跳过 near-blank 检查，全白单页 deck 可确定性通过预览
  - 位置: src/features/teaching_animation/ai/html_generation_protocol.py:486-490
  - 证据:
    ```python
        near_blank, stats = is_effectively_blank(
            page_result.data, page_result.surface.width, page_result.surface.height,
        )
        if page_count > 1 and near_blank:
            page_failures.append({
                "page": index + 1, "frame": frame,
                "failure": f"near-uniform frame: {stats}",
            })
    ```
  - 影响: 前两次确定性捕获只比对哈希是否一致，不判空白；唯一的空白检测被 `page_count > 1` 条件短路。单页 deck 渲染为纯白/纯色（如文字与背景同色）时 PREVIEW 判 passed，writable 仅剩用户批准一道闸，用户确认后即写入时间线才发现空白。
  - 建议: 单页 deck 同样执行 near-blank 检查（若担心极简开场页误伤，可将判定阈值放宽而非完全跳过）。
  - 置信度: 中

- [AUD-26908] [Suggestion] render-only 模式（`post_generation_quality_gate=False`）连 `check_unsafe_content` 固定点复扫也一并跳过
  - 位置: src/features/teaching_animation/ai/html_generation_protocol.py:265-303
  - 证据:
    ```python
    if cons.post_generation_quality_gate:
        budget_report = enforce_budget(sanitized.html)
        quality = run_html_quality_checks(...)
    else:
        quality = QualityResult()
        stages.append(StageResult(
            HtmlPipelineStage.STATIC_ANALYSIS,
            not page_structure_issues,
            detail=("；".join(page_structure_issues[:3]) if page_structure_issues else
                    "skipped: browser/render-only mode leaves design decisions to the model and user")))
    ```
  - 影响: 注释自述"sanitizer 和捕获沙箱是渲染/安全要求而非审美闸门"，但自由度最高的 freeform browser profile 恰恰跳过了 sanitizer 固定点复扫与残留危险 token 扫描（`check_unsafe_content`），安全纵深在最宽松的配置下最薄。
  - 建议: 将 `check_unsafe_content`（含 profile 感知参数）从"设计 gate"中拆出，render-only 模式下仍然执行。
  - 置信度: 中

- [AUD-26909] [Suggestion] `_run_preview` 返回类型注解中 `list` 未参数化
  - 位置: src/features/teaching_animation/ai/html_generation_protocol.py:357-365
  - 证据:
    ```python
    def _run_preview(
        sanitized_html: str,
        *,
        cons: HtmlGenerationConstraints,
        ...
    ) -> tuple[Mapping[str, object] | None, list, bool]:
    ```
  - 影响: 第二个返回值实为 `list[QualityIssue]`，裸 `list` 使调用侧（`determinism_issues` 合入 `quality.issues`）失去类型检查保护。
  - 建议: 改为 `list[QualityIssue]`（需将 `QualityIssue` 提前到模块顶部导入或使用字符串注解）。
  - 置信度: 高

### 通过项
- 线程安全: 纯编排函数，无共享可变状态；`quality` 重绑定是局部变量
- GPU 资源生命周期: 不直接持有 GPU 资源；浏览器捕获由后端自管
- GLSL·HDR: 不涉及
- 信号防递归: 不涉及
- 状态提交一致性: `writable` 不变量（安全阶段全过 + 无 ERROR + 显式批准 + 预览后端失败阻断）在 `_finalize` 中集中且与各阶段结果一致；sanitize 早退路径同样产出不可写结局
- 异常与取消路径: LLM 失败在 html_llm_generator 侧 fail-closed；本模块对 `is_available()` 异常给出"blocked"理由而非静默通过；`detect_page_count` 恒 ≥1（已核源码 html_page_deck.py:379/385），`cons.max_duration_seconds / page_count` 无除零
- 输入验证: `HtmlGenerationConstraints.__post_init__` 校验 duration/fps/viewport 正值；BIND_ASSETS 对未解析资产在 gate 模式下阻断
- 性能模式: `budget_report` 复用避免二次解析；惰性导入解耦渲染后端

## src/features/teaching_animation/ai/html_llm_generator.py (行数: 316)

### 发现

- [AUD-26910] [Major] `strip_markdown_fence` 用 `rfind("```")` 无条件截断，正文含代码围栏的教学页面被静默腰斩
  - 位置: src/features/teaching_animation/ai/html_llm_generator.py:225-230
  - 证据:
    ```python
    trailing_fence = value.rfind("```")
    if trailing_fence >= 0:
        value = value[:trailing_fence].rstrip()
    # Keep the historical regex as a final compatibility pass for unusual
    # whitespace/newline combinations.
    return _FENCE_RE.sub("", value).strip()
    ```
  - 影响: 已实测验证：对合法文档 `<!DOCTYPE html>...<pre>代码块这样写：\n```python\nprint(1)\n```</pre><p>总结</p>...`，函数从最后一个 ``` 处截断，`</pre>` 之后的全部内容（含 `</section></body></html>`）丢失。teaching_animation 场景下"讲解 Markdown/代码"的页面出现正文围栏是合理输入；截断后的残缺文档经 sanitizer/preview（单页时无空白检查，见 AUD-26907）可能仍被写入，造成静默内容丢失。
  - 建议: 仅当 ``` 位于文档末尾（其后仅剩空白）才剥离；或剥离前校验截断点之后不含 `<` 标签；同时校验截断后文档标签配平。
  - 置信度: 中

- [AUD-26911] [Suggestion] 插件 prompt 加载失败被 `except (ImportError, AttributeError)` 静默吞掉
  - 位置: src/features/teaching_animation/ai/html_llm_generator.py:170-176
  - 证据:
    ```python
    try:
        from src.features.edit.ai_editing.assistant_plugins import assistant_plugin_prompt

        plugin_prompt = assistant_plugin_prompt("motion_graphic", "html_build")
    except (ImportError, AttributeError):
        plugin_prompt = ""
    return f"{plugin_prompt}\n\n{runtime_contract}" if plugin_prompt else runtime_contract
    ```
  - 影响: `ImportError` 同样覆盖 assistant_plugins 模块内部依赖链的导入失败（而非仅"插件不存在"）。用户自定义的设计方向 prompt 会静默失效，生成结果回归默认风格且无任何日志/诊断，难以排查。
  - 建议: 区分"插件未注册"（可静默回退）与"模块导入失败"（应记录日志）；至少在回退时输出一条 debug 日志。
  - 置信度: 中

### 通过项
- 线程安全: 无状态纯函数 + 注入式 client
- GPU 资源生命周期 / GLSL·HDR / 信号防递归: 不涉及
- 状态提交一致性: 生成结果一律经 `run_html_generation_pipeline`，调用方只见 `HtmlGenerationOutcome`，writable 单一决策点
- 异常与取消路径: `generate_html_with_llm` 对 LLM 异常 fail-closed（转为不可写 outcome，reason 含异常类型）；空内容单独处理
- 输入验证: 约束校验委托 `HtmlGenerationConstraints.__post_init__`；prompt 构建对 `page_count` 做 `> 0` 判断
- 性能模式: prompt 每次生成构建一次，无重复 IO
- 类型注解: 完整（client 为有意 `Any` 注入点）

## src/features/teaching_animation/ai/html_quality_gate.py (行数: 251)

### 发现

- [AUD-26912] [Suggestion] `_FIXED_WIDTH_RE` 无左边界，`min-width:`/`max-width:` 会被当作固定宽度误报
  - 位置: src/features/teaching_animation/ai/html_quality_gate.py:59, 144-152
  - 证据:
    ```python
    _FIXED_WIDTH_RE = re.compile(r"width:\s*([0-9.]+)\s*(px|vw)", re.IGNORECASE)
    ...
    for match in _FIXED_WIDTH_RE.finditer(sanitized_html):
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit == "px" and value > viewport_width + 1.0:
            issues.append(QualityIssue(
                QualityCode.HTML_VIEWPORT_OVERFLOW, QualitySeverity.WARNING,
                f"fixed width {value}px exceeds viewport {viewport_width}px",
    ```
  - 影响: 响应式页常见的 `min-width: 2000px`（媒体查询/容器约束）中子串 `width: 2000px` 命中，产生"fixed width exceeds viewport"假警告；与模块自身鼓励的 fluid/min-width 布局建议相抵触。WARNING 级不阻断导出，但污染用户可见诊断。
  - 建议: 在 `width` 前加 `(?<![\w-])` 负向后行断言（排除 `min-`/`max-`/`border-`/`stroke-` 等前缀）。
  - 置信度: 高

- [AUD-26913] [Suggestion] `check_flicker` 漏掉 ms 单位动画时长，且跨元素混合全部 opacity 声明计算 swing
  - 位置: src/features/teaching_animation/ai/html_quality_gate.py:62-66, 165-176
  - 证据:
    ```python
    _RAPID_DURATION_RE = re.compile(
        r"animation(?:-duration)?\s*:\s*[^;{}]*?([0-9]*\.?[0-9]+)\s*s",
        re.IGNORECASE)
    ...
    durations = [float(d) for d in _RAPID_DURATION_RE.findall(sanitized_html)]
    opacities = [float(o) for o in _OPACITY_KEYFRAME_RE.findall(sanitized_html)]
    rapid = any(d < 0.2 for d in durations)
    swings = max(opacities) - min(opacities) > 0.6 if len(opacities) >= 2 else False
    ```
  - 影响: (1) `100ms`（最典型的闪烁时长）不匹配 `([0-9.]+)\s*s`（"m" 挡住 "s"），最危险输入恰好漏检；(2) 不同元素无关的 `opacity: 0.1` 与 `opacity: 0.9` 合并算出 0.8 假摆动，叠加任一快速动画即误报。漏报+误报双向失真，但均为 WARNING 级。
  - 建议: 时长正则支持 `ms` 并统一换算为秒；opacity 摆动按元素/动画块分组（如按 `@keyframes` 块切分）后再判定。
  - 置信度: 中

- [AUD-26914] [Suggestion] 残留危险 token 扫描对"可见文本中字面出现"误报（教学/代码示例内容会被判 ERROR 阻断）
  - 位置: src/features/teaching_animation/ai/html_quality_gate.py:96-110
  - 证据:
    ```python
    lowered = sanitized_html.lower()
    allow_scripts = bool(...)
    needles = ["javascript:", "<iframe"]
    if not allow_scripts:
        needles.append("<script")
    if not allow_handlers:
        needles.extend((" onerror", " onload"))
    for needle in needles:
        if needle in lowered:
            return (QualityIssue(QualityCode.HTML_UNSAFE_CONTENT, QualitySeverity.ERROR,
                                 f"residual unsafe token: {needle!r}"),)
    ```
  - 影响: needle 是对整个 sanitized HTML 的裸子串搜索，不区分属性与可见文本。讲解 Web 安全/URL 方案的教学页在正文或 `<pre>` 中字面写出 `javascript:`、`<iframe` 时被误判 ERROR（`HTML_UNSAFE_CONTENT`），deck 永久不可写，用户无路可走（同类内容再生成仍失败）。默认离线 profile 下确有可能触发。
  - 建议: 先剥离 `<pre>`/`<code>`/文本节点后再做 needle 扫描，或要求 `javascript:` 出现在 `href=`/`url(` 等属性上下文中。
  - 置信度: 中

- [AUD-26915] [Suggestion] 通过 `getattr` 探测 `HtmlSanitizer` 私有属性 `_allow_*`，脆弱的跨模块私有耦合
  - 位置: src/features/teaching_animation/ai/html_quality_gate.py:97-101
  - 证据:
    ```python
    allow_scripts = bool(
        getattr(san, "_allow_network_resources", False)
        or getattr(san, "_allow_inline_scripts", False)
    )
    allow_handlers = bool(getattr(san, "_allow_event_handlers", False))
    ```
  - 影响: 属性名已核实现存于 sanitizer.py:214-218，当前可用；但依赖下划线私有名，sanitizer 重构（改名/改构造）时此处会静默退化为 False，使自由 profile 的合法脚本被误判 unsafe。getattr 默认值掩盖了契约破坏。
  - 建议: 在 `HtmlSanitizer` 上暴露公开只读属性（property 或 `profile` 数据类），quality gate 只读公开接口。
  - 置信度: 高

### 通过项
- 线程安全: 纯函数检查器
- GPU 资源生命周期 / GLSL·HDR / 信号防递归: 不涉及
- 状态提交一致性: 检查结果以 `QualityResult` 只读返回，ERROR/WARNING 分级与 pipeline 的 writable 判定一致
- 异常与取消路径: `check_frame_determinism` 对无证据返回 INFO 而非静默通过（符合"能力缺失须报告"）
- 输入验证: 空 payload 显式 ERROR；`budget_report` 可由调用方复用避免重复解析
- 性能模式: 各检查单遍扫描；固定点复扫为有意的纵深防御
- 类型注解: 完整

## src/features/teaching_animation/ai/motion_graphic_eval.py (行数: 201)

### 发现

（无）

### 通过项
- 通过（无逻辑缺陷发现）: 纯评测编排模块；`MotionGraphicEvalCase.__post_init__` 校验非空 case_id/user_turns；Mapping/Iterable 双入口均确定性（缺失 artifact 以空串参与评测并落 issues）；`dict.fromkeys` 去重保序；类型注解完整
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性: 不涉及或无问题
- 输入验证: `pass_rate` 对空 results 有零除保护；`artifacts.get(case.case_id, "")` 兜底缺失样本

## src/features/teaching_animation/ai/motion_quality_gate.py (行数: 253)

### 发现

（无）

### 通过项
- 通过（无逻辑缺陷发现）: 全部为纯函数检查器；`MotionQualityInput` 用 `field(default_factory=dict)` 规避可变默认值陷阱；越界判定带 `1e-6` 容差；`check_strobing` 对长度 <2 的序列跳过；`check_transition_overlap` 对 limit==0 显式放过；类型注解完整
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径: 不涉及或无问题
- 输入验证: `check_font_resolution` 样本仅取前 4 个码点，消息有界

---

## 批次总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-26902, AUD-26903, AUD-26910 |
| Suggestion | 12 | AUD-26900, AUD-26901, AUD-26904–26909, AUD-26911–26915 |

3 个 Major 集中在"确定性 gate 的误判"与"输入规范化截断"：增删页/删文案后置条件对常见中文否定义与混合请求的误判会让正确的自主编辑被反复否决（fail-safe 方向，但功能不可用）；`strip_markdown_fence` 会静默腰斩含代码围栏的教学页面。均已在真实代码上实测复现（见各条证据）。批次内无 Critical、无 UI/GLSL/线程类问题。
