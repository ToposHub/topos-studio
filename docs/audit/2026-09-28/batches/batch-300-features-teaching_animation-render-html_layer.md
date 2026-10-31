# 批次报告：batch-300 — src/features/teaching_animation/render/html_layer/

编号区间：AUD-30000–AUD-30099。审查范围：7 个文件，2,133 行（全部通读）。
本包为 W2-prep 纯逻辑层（无 Qt/GL/信号），九个维度中的线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归
对所有文件均为"不适用/通过"，逐文件不再重复列出，仅列与各文件相关的维度。

---

## src/features/teaching_animation/render/html_layer/__init__.py (行数: 58)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 门面。导出 html_patch 的 6 个模型符号 + validate_patch、
  html_patch_apply 的 3 个函数、node_identity 的 3 个提取函数、sanitizer 的 4 个符号、
  budget 的 5 个符号、template_promote 的 2 个函数；逐一核对 12 个子模块导入源，
  `__all__` 24 个名称全部真实存在，无悬空导出。

---

## src/features/teaching_animation/render/html_layer/budget.py (行数: 150)

### 发现

- [AUD-30001] [Major] measure_dom 宽泛 `except Exception: pass` 吞掉解析错误后仍以部分计数出具预算报告
  - 位置: src/features/teaching_animation/render/html_layer/budget.py:62-69
  - 证据:
    ```python
    def measure_dom(sanitized_html: str) -> DomStats:
        """Count DOM nodes/depth, stylesheet size, keyframes and data-URI bytes."""
        parser = _DomCounter()
        try:
            parser.feed(sanitized_html)
            parser.close()
        except Exception:
            pass
    ```
  - 影响: FH-07 预算门的存在意义就是"runaway 页面在冻结渲染器之前被上报"。若 feed 中途抛错，
    node_count/max_depth 停在出错前的部分值，超大页面可被静默判为达标（fail-open），
    违反 .clinerules"不得宽泛 except 掩盖领域错误"铁律。
  - 建议: 捕获后记录 note/向上抛结构化错误，或至少把"计数不完整"写入 BudgetReport 而非静默通过。
  - 置信度: 中（HTMLParser 抛错罕见，但后果与铁律违反客观存在）

- [AUD-30002] [Major] data_uri_bytes 把全部 data: URI 求和后与"单条上限" max_data_uri_bytes 比较
  - 位置: src/features/teaching_animation/render/html_layer/budget.py:79,102-103
  - 证据:
    ```python
    data_uri_bytes = sum(len(m) for m in re.findall(r"data:[^\"')]+", sanitized_html))
    ...
    if stats.data_uri_bytes > budget.max_data_uri_bytes:
        violations.append(BudgetViolation("max_data_uri_bytes", stats.data_uri_bytes, budget.max_data_uri_bytes))
    ```
    而依赖方定义（html_renderer_spike/security.py:335）为 `max_data_uri_bytes: int = 32_000  # individual inline data: payload`。
  - 影响: 语义错位。40 个各 1KB 的合法小图标（合计 40KB）会触发本应只针对"单条 32KB 载荷"的违例，
    合法页面被结构化拒绝；反之若未来按"总量"理解，32,000 的量级又明显偏小。两种读法都不自洽。
  - 建议: 与 ResourceBudget 语义对齐——按单条最大值比较，或为"总量"另设独立字段/上限。
  - 置信度: 中（求和与单条上限的矛盾可从代码+依赖注释确证；哪种语义是本意需作者确认）

- [AUD-30003] [Suggestion] enforce_budget 文档串与实现不符；data URI 按"字符"计数却命名 bytes
  - 位置: src/features/teaching_animation/render/html_layer/budget.py:63,79,90
  - 证据:
    ```python
    def enforce_budget(sanitized_html: str, budget: ResourceBudget | None = None) -> BudgetReport:
        """Return the first budget violation (if any) plus full DOM stats."""
    ```
    实际实现收集全部 violations（93-103 行），非"first"。
  - 影响: 误导调用方与审查者；data URI 的 len() 是 base64 字符数而非字节数，阈值口径偏差约 1.33 倍。
  - 建议: 修正 docstring 为"all violations"；计量口径统一为字节或改名 data_uri_chars。
  - 置信度: 高

- [AUD-30004] [Suggestion] _DomCounter.handle_starttag 的 attrs 参数为裸 `list` 注解
  - 位置: src/features/teaching_animation/render/html_layer/budget.py:32
  - 证据:
    ```python
    def handle_starttag(self, tag: str, attrs: list) -> None:
    ```
  - 影响: 同包 node_identity.py:37 已用 `list[tuple[str, str | None]]`，风格不一致且丢失类型信息。
  - 建议: 统一为 `list[tuple[str, str | None]]`。
  - 置信度: 高

### 通过项
- 线程安全：无共享可变状态，_DomCounter 每次调用新建。
- 输入验证：budget 参数缺省回退 ResourceBudget()；DomStats/BudgetReport 为 frozen dataclass。
- 性能模式：正则+单遍 HTMLParser，对典型页面规模线性，无明显热点。
- 状态提交一致性：纯函数、无状态、不触碰 undo/revision。

---

## src/features/teaching_animation/render/html_layer/html_patch.py (行数: 200)

### 发现

- [AUD-30005] [Major] REPLACE_TEXT 不校验 edit_mode，且 text_slot 全库恒为 True——token_only 节点可被改写文本
  - 位置: src/features/teaching_animation/render/html_layer/html_patch.py:135-140；src/features/teaching_animation/render/html_layer/node_identity.py:69-72
  - 证据:
    ```python
    if op == HtmlPatchOperation.REPLACE_TEXT:
        node = _require_node(patch.target, state)
        if not node.text_slot:
            raise HtmlPatchError(HtmlPatchErrorCode.EDIT_MODE_FORBIDS, f"{patch.target} has no text slot")
        if not isinstance(patch.value, str):
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE, "replace_text value must be a string")
    ```
    而 node_identity.py 构造时从不传 text_slot（默认 True）：
    ```python
    return {
        node_id: DeclaredNode(node_id=node_id, edit_mode=data["edit_mode"], asset_slot=data["asset_slot"])
        for node_id, data in scanner.nodes.items()
    }
    ```
    全库 grep "text_slot" 仅命中定义（html_patch.py:58）与该死分支（:137），无任何处置 False 的代码。
  - 影响: FH-13 契约声明 token_only = "only CSS-token edits, no content/transform"，但一个
    `data-topos-edit-mode="token_only"` 的节点可原样通过 REPLACE_TEXT 校验并被改写内容；
    `if not node.text_slot` 为永假死代码，编辑能力契约（防响应式布局被破坏的核心设计）失效。
  - 建议: REPLACE_TEXT 分支增加 `node.edit_mode == "token_only"` 拒绝；或让提取器按 edit_mode/tag
    推导 text_slot，使该字段真实生效。
  - 置信度: 高

- [AUD-30006] [Major] SET_ANIMATION_TIMING 的 duration_ms 未做类型/非负校验，int() 的裸异常逃出 HtmlPatchError 契约
  - 位置: src/features/teaching_animation/render/html_layer/html_patch.py:165-169；src/features/teaching_animation/render/html_layer/html_patch_apply.py:254
  - 证据: 校验端只查键存在：
    ```python
    elif op == HtmlPatchOperation.SET_ANIMATION_TIMING:
        _require_node(patch.target, state)
        if not isinstance(patch.value, Mapping) or "duration_ms" not in patch.value:
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE,
                                 "set_animation_timing value needs duration_ms")
    ```
    应用端却假定更多（注释与事实不符）：
    ```python
    duration_ms = int(patch.value.get("duration_ms"))  # validate_patch guarantees presence + int-able
    ```
  - 影响: `{"duration_ms": "abc"}` 抛裸 ValueError、`{"duration_ms": [1]}` 抛 TypeError——
    模块契约"非法值产生结构化 HtmlPatchError"被击穿（HtmlPatchError 是 ValueError 子类，
    只捕 HtmlPatchError 的调用方会漏接）；负数/0 会生成非法 CSS `animation: -0.5s` 静默下发。
    patch value 属外部/AI 输入，违反"外部数据须验证"铁律。
  - 建议: 在 validate_patch 中校验 duration_ms 为非负 int（拒绝 bool），失败抛 ILLEGAL_VALUE；
    修正 apply 端不实注释。
  - 置信度: 高

- [AUD-30007] [Suggestion] _is_transform_value 宣称 "finite" 但未做有限性检查
  - 位置: src/features/teaching_animation/render/html_layer/html_patch.py:163-164,181-189
  - 证据:
    ```python
    raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE, "transform_node value must be a finite transform mapping")
    ...
    for key in ("x", "y", "scale", "rotation"):
        if key in value:
            v = value[key]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return False
    ```
  - 影响: float('nan')/float('inf') 通过校验，`transform: translate(nan, nan)` 被写入并原样通过
    再净化（transform 属允许属性、无 url()），产出静默失效的变换。
  - 建议: 增加 `math.isfinite(v)` 判断。
  - 置信度: 高

### 通过项
- 线程安全：纯不可变数据模型 + 无副作用校验函数。
- 状态提交一致性：validate 不变异状态（fail-before-write 边界成立）；HtmlLayerState frozen。
- 输入验证：STALE_REVISION / PRECONDITION_MISMATCH 双前置条件检查完整；SET_ASSET 校验绑定资产集合。

---

## src/features/teaching_animation/render/html_layer/html_patch_apply.py (行数: 352)

### 发现

- [AUD-30008] [Critical] _locate_element 不识别自闭合写法，回退把"文档剩余部分"当作 inner——replace_text 会清空目标之后全部 HTML
  - 位置: src/features/teaching_animation/render/html_layer/html_patch_apply.py:39-43,107-124,180-189
  - 证据: 自闭合标签（尾随 `/`）被当成普通开标签，且 SVG 几何标签不在 void 集合里：
    ```python
    _VOID_ELEMENTS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })
    ...
    is_void = target_tag.lower() in _VOID_ELEMENTS
    ...
    # No matching close tag found (malformed); treat inner as the rest of the doc.
    return _ElementSpan(target_tag, open_start, open_end, open_end, len(html), len(html), False)
    ```
    而 sanitizer 本身就会输出这种形态（sanitizer.py:303-314 把 `<rect/>` 等原样重发自闭合、
    把非 SVG 的 `<div/>` 重发为不带闭合的 `<div>`）。随后：
    ```python
    return html[:span.inner_start] + str(patch.value) + html[span.inner_end:]
    ```
  - 影响: 对 `<rect data-topos-node-id="shape1"/>`（extract_declared_nodes 默认给一切节点
    text_slot=True，可合法通过 REPLACE_TEXT 校验）应用 replace_text 时，inner_end=len(html)，
    目标开标签之后的整段文档——包括其余全部节点与闭合标签——被一段文本取代，revision+1 落库，
    静默数据丢失。`<div/>` 形态同样触发。
  - 建议: _TAG_RE 捕获尾随 `/` 并在深度行走中按"立即闭合"处理；将 sanitizer 会自闭合重发的
    SVG 几何标签并入 void 处理；对未找到匹配闭合标签的情况抛 HtmlPatchError 而非吞成"剩余全文档"。
  - 置信度: 中（机制从代码完全确证；触发需自闭合目标节点，此类形态 sanitizer 日常产出）

- [AUD-30009] [Major] set_animation_timing 对 `animation:` 简写的整个值做整体替换，动画名/迭代/缓动被抹掉
  - 位置: src/features/teaching_animation/render/html_layer/html_patch_apply.py:261-266,270-272
  - 证据:
    ```python
    rule_re = re.compile(
        rf'(\[data-topos-node-id\s*=\s*"{re.escape(patch.target)}"[^{{]*\{{[^}}]*?animation(?:-duration)?\s*:\s*)([^;}}]+)([;}}])',
        re.DOTALL,
    )
    new, n = rule_re.subn(
        lambda m: f"{m.group(1)}{seconds:g}s{m.group(3)}", html, count=1)
    ```
  - 影响: `animation: slide 2s ease-in infinite;` 经补丁变为 `animation: 0.5s;`——动画名丢失，
    整条动画在渲染中失效（页面静止或初始态消失）。这正是该操作存在要保护的目标（时序编辑）。
    行内回退分支同病。
  - 建议: 仅改写 `animation-duration` 长属性；遇 `animation:` 简写时解析各分量只替换时间分量，
    或拒绝并要求改写为长属性形式（ILLEGAL_VALUE 可诊断）。
  - 置信度: 高

- [AUD-30010] [Major] 补丁后 source_hash 被写成"已净化 HTML 的哈希"，违反 FH-01 authored(预净化)语义且重复计算同一哈希
  - 位置: src/features/teaching_animation/render/html_layer/html_patch_apply.py:342-349
  - 证据:
    ```python
    new_source_hash = hashlib.sha256(sanitized.html.encode("utf-8")).hexdigest()
    return replace(
        layer,
        sanitized_html=sanitized.html,
        source_hash=new_source_hash,
        sanitizer_hash=hashlib.sha256(sanitized.html.encode("utf-8")).hexdigest(),
        revision=layer.revision + 1,
    )
    ```
    契约定义（core/html_layer.py:95-96）：`source_hash: sha256 of the authored (pre-sanitization) source.
    sanitizer_hash: sha256 of sanitized_html (post-sanitization)`。
  - 影响: 首次补丁后 source_hash 与 sanitizer_hash 永久相同——FH-01 承诺的"reviewer 能看到
    sanitizer 到底改了什么"的审计链断裂；缓存键对 authored 侧变化的反应能力同样失真。
    另外同一 sha256 计算了两遍（微性能 + 混淆）。
  - 建议: `source_hash=sha256(new_html)`（handler 输出、进 sanitizer 之前的文本），
    `sanitizer_hash=sha256(sanitized.html)`，一次计算复用。
  - 置信度: 高

- [AUD-30011] [Major] _op_set_slot_attr 用全文字符串 find 定位槽位，<style> 中的 `[data-topos-editable="x"]` 选择器会先被命中
  - 位置: src/features/teaching_animation/render/html_layer/html_patch_apply.py:230-242
  - 证据:
    ```python
    needle = f'data-topos-editable="{patch.target}"'
    pos = html.find(needle)
    if pos < 0:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_SLOT, f"slot {patch.target} not in source")
    open_start = html.rfind("<", 0, pos)
    open_end = html.find(">", pos)
    ```
  - 影响: sanitizer 的 _sanitize_rule_blocks 逐字保留选择器前导（含属性选择器），
    且 <style> 通常位于文档前部；`[data-topos-editable="title"] { color: red }` 会被 find 先命中，
    rfind("<") 落到 `<style>` 上，`_set_attr_on_open_tag` 把属性插进样式区（如生成 `</style attr="v">`
    一类的坏输出），真实元素未被修改——补丁"成功"但写错目标并破坏样式。同名槽位出现在多个元素时
    也永远只改第一个。
  - 建议: 复用 _TAG_RE 标签遍历在"标签属性内"定位 `data-topos-editable`，跳过 <style> 原文区段；
    多命中时全部更新或报歧义错误。
  - 置信度: 高（样式选择器先命中为常见文档布局下的确定性顺序）

- [AUD-30012] [Suggestion] _op_set_asset 以子串 `"src" in open_tag` 选择属性，可被 data-src/class 文本误判
  - 位置: src/features/teaching_animation/render/html_layer/html_patch_apply.py:196-198
  - 证据:
    ```python
    open_tag = html[span.open_start:span.open_end]
    attr = "src" if "src" in open_tag else "href"
    ```
  - 影响: `<a data-topos-src-x="1" ... href="topos-asset://old">` 会判定"有 src"并在 <a> 上写入
    无效的 src 属性，原 href 残留旧资产引用（read_node_asset_ref 读 src 优先，引用与展示错位）。
  - 建议: 用 _parse_attrs 解析出的属性名集合判断，而非裸子串。
  - 置信度: 中

### 通过项
- 状态提交一致性：dataclasses.replace 生成新 frozen 层、revision+1，无原地变异；
  应用前经 validate_patch（html_layer_commands.py:274 为 undo 命令真实调用点）。
- 异常与取消路径：未知节点/槽/令牌/空净化结果均抛结构化 HtmlPatchError，无部分写入。
- 输入验证：净化器 profile 从 metadata 显式 `is True` 读取（缺失时保守回退方向正确）。
- 线程安全：无共享状态，函数式。

---

## src/features/teaching_animation/render/html_layer/node_identity.py (行数: 89)

### 发现

- [AUD-30013] [Suggestion] extract_declared_nodes 宽泛 `except Exception: return {}` 静默丢弃全部可寻址节点
  - 位置: src/features/teaching_animation/render/html_layer/node_identity.py:61-69
  - 证据:
    ```python
    scanner = _NodeScanner()
    try:
        scanner.feed(sanitized_html)
        scanner.close()
    except Exception:
        return {}
    ```
  - 影响: fail-closed（后续补丁报 UNKNOWN_NODE、模板提升静默跳过全部槽位），但诊断完全失真——
    使用者看到的是"节点未声明"而非"解析失败"；违反 .clinerules 宽泛 except 铁律。
  - 建议: 至少记录/上抛解析错误，或返回带诊断的空结果（与 sanitizer 的 parse_error note 对齐）。
  - 置信度: 中（触发罕见，方向安全，主要是可诊断性与规范问题）

### 通过项
- 线程安全：每次调用新建 scanner，无共享状态。
- 输入验证：非法 edit_mode 回退 "content"；重复 node_id 合并取更严格模式（_stricter_mode 次序正确）。
- 性能模式：单遍 HTMLParser + 轻量正则。

---

## src/features/teaching_animation/render/html_layer/sanitizer.py (行数: 980)

### 发现

- [AUD-30014] [Major] CSS 自定义属性在 url() 白名单检查之前无条件放行，`--x: url(https://…)` 可绕过 URL 策略
  - 位置: src/features/teaching_animation/render/html_layer/sanitizer.py:747-757,764-774
  - 证据:
    ```python
    # Drop any declaration carrying a dangerous construct entirely.
    if any(danger.search(declaration) for danger in _CSS_DANGER_TOKENS):
        removed.append(f"css_danger:{prop}")
        continue
    # CSS custom properties ... keep them even though they are not in the fixed property allowlist.
    if prop.startswith("--"):
        kept.append(declaration)
        continue
    ...
    # url() must be topos-asset only.
    if "url(" in value.lower():
    ```
    danger 令牌只有 expression/@import/-moz-binding/behavior（172-177 行），不含 url(；
    自定义属性在 url 检查前 continue。
  - 影响: 模块契约"任何 url(...) 指向非 topos-asset 的一律剥除"被自定义属性旁路：
    `--brand-bg: url(https://evil/x)` 原样保留，页面再用 `background: var(--brand-bg)`（该声明
    字面值无 url(，同样通过）即可在 network profile 下远程加载。SET_TOKEN 补丁值与模板参数值
    都能到达该入口（外部输入）。
  - 建议: 自定义属性声明也执行 url() 白名单（或至少对包含 url( 的自定义属性值走 _safe_css_url）。
  - 置信度: 中（旁路机制确证；实际外联是否成功还取决于渲染端 CSP）

- [AUD-30015] [Suggestion] `allowed_tags or ALLOWED_TAGS`——显式传入空 frozenset 会静默回退为"全部允许"
  - 位置: src/features/teaching_animation/render/html_layer/sanitizer.py:951
  - 证据:
    ```python
    def __init__(
        self,
        *,
        allowed_tags: frozenset[str] | None = None,
        ...
    ) -> None:
        self._allowed_tags = allowed_tags or ALLOWED_TAGS
    ```
  - 影响: 调用方想收紧为"零标签"（纯文本化）时传入 frozenset()，因 falsy 被替换为完整白名单，
    安全意图被静默反转（fail-open）。
  - 建议: `allowed_tags if allowed_tags is not None else ALLOWED_TAGS`。
  - 置信度: 高

- [AUD-30016] [Suggestion] 注释/模块契约与实现漂移：animate/set 声称"被排除"实则在白名单；"单个 <style>"实际保留任意多个
  - 位置: src/features/teaching_animation/render/html_layer/sanitizer.py:12-13,56-59,66-67,269-273
  - 证据:
    ```
    # Executable/time-based SVG tags such as animate and set remain excluded; ...
    ```
    而 ALLOWED_TAGS 实际包含：
    ```python
    "animate", "animatetransform", "set",
    ```
    模块 docstring 称 "a single ``<style>`` block"，实现保留所有 <style> 块。
  - 影响: 审计者与二次开发者据注释理解安全边界会得出错误结论（SMIL 动画实际可用）；
    预算/复杂度假设（单样式块）与实现不符。
  - 建议: 更新注释与 docstring 使其与白名单/实现一致，或按注释真正移除时间型标签。
  - 置信度: 高

### 通过项
- 线程安全：HtmlSanitizer 每次 sanitize 新建 parser，无共享可变状态。
- 输入验证：data:/blob: 仅限可视化 MIME 前缀且有 4MB 上限（明确排除 svg+xml/html/script）；
  网络资源强制 HTTPS+netloc+无控制字符（_is_network_url）；use 仅同文档 #fragment；
  属性值经 _escape_attr 双引号输出，文本经 _escape_text。
- 异常与取消路径：sanitize 顶层 except 记录 parse_error note 并返回空结果（有诊断、不崩溃，
  调用方 apply_html_patch 对空结果显式报错，无部分写入）；深嵌套 @media 递归错误同样被该层接住。
- 性能模式：CSS 划分采用括号/引号感知的 _split_css_declarations 与平衡大括号扫描，线性量级；
  @keyframes/@font-face 单独抽取避免重复扫描。
- 状态提交一致性：无状态纯变换，SanitizerReport 记录全部移除原因（可诊断契约成立）。
- 标签/token 化安全：<style> 走 CDATA 模式，`</style>` 注入被解析器边界天然阻断。

---

## src/features/teaching_animation/render/html_layer/template_promote.py (行数: 304)

### 发现

- [AUD-30017] [Critical] 参数值仅剥 `<script>` 块；经自由 profile 重建后 `on*` 事件处理器等活动内容注入存活，违背"参数值绝不能注入"承诺
  - 位置: src/features/teaching_animation/render/html_layer/template_promote.py:38-41,237-248；src/features/teaching_animation/render/html_layer/sanitizer.py:372-374
  - 证据: 参数值的防御只针对 script 标签：
    ```python
    _SCRIPT_BLOCK_RE = re.compile(
        r"<script\b[^>]*>.*?</script\s*>|</?script\b[^>]*>?",
        re.IGNORECASE | re.DOTALL,
    )
    ...
    values = {name: _SCRIPT_BLOCK_RE.sub("", value) for name, value in values.items()}
    ...
    substituted = _MARKER_RE.sub(_sub, template.html_template)
    ```
    而重建走 build_html_layer_from_source（默认 allow_event_handlers=True 等自由 profile），
    sanitizer 对应放行：
    ```python
    if self._allow_event_handlers and name_l.startswith("on"):
        cleaned.append((name_l, value))
        continue
    ```
  - 影响: 文本槽参数值 `<svg onload=alert(1)>` 或 `<img src="https://tracker/x">`（network profile
    下 https src 保留）被原样代入并存活净化——在捕获浏览器内执行脚本/外联。模块自述
    "a parameter value must never inject one [script]" 与 "an attacker-controlled parameter value
    can never bypass the allowlist" 被击穿。ai/service.py:270 与 timeline_view.py:14106 均以
    用户/AI 提供的 params 走到此路径（外部输入）。
  - 建议: 参数值按用途归类约束：文本槽参数做 HTML 转义或强制纯文本；token 参数限定为单一 CSS 值
    （拒绝 `;`、`}`、`<`、`url(` 等序列）；或对参数值一律走保守 profile 二次净化。
  - 置信度: 中（注入链各环节均经代码确证；实际执行取决于渲染端 CSP 与 profile 配置）

- [AUD-30018] [Major] `if layer_metadata:` 恒为真——schema 键被无条件注入，保守净化回退路径成为死代码，所有模板实例化都获得自由 profile
  - 位置: src/features/teaching_animation/render/html_layer/template_promote.py:255-281
  - 证据:
    ```python
    layer_metadata = dict(template.metadata)
    layer_metadata["motion_graphic_parameter_schema"] = [
        spec.to_dict() for spec in template.params_schema
    ]
    if layer_metadata:
        from ...core.html_layer_commands import build_html_layer_from_source
        return build_html_layer_from_source(...)
    # Legacy templates without metadata retain the conservative sanitizer path below.
    san = sanitizer or HtmlSanitizer()
    ```
    且 build_html_layer_from_source（html_layer_commands.py:430-441）默认
    `network_access/allow_inline_scripts/allow_event_handlers/allow_freeform_css = True`。
  - 影响: 注释承诺"无 metadata 的遗留模板走下方保守 sanitizer"，但 layer_metadata 因无条件注入
    schema 键永不为空——保守分支（281-298 行）不可达。任何保守模板实例化后都被加上
    网络访问+内联脚本+事件处理器的自由浏览器 profile，安全属性静默升级，也是 AUD-30017 的放大器。
  - 建议: 以 `if template.metadata:`（原始模板 metadata）作为分支条件，schema 键注入移到分支内；
    或显式比较自由/保守 profile 的判定标志。
  - 置信度: 高

- [AUD-30019] [Suggestion] token/参数值未限定为单一 CSS 值，`;`/`}` 可重组样式规则（受属性白名单约束的有限注入）
  - 位置: src/features/teaching_animation/render/html_layer/template_promote.py:85-91,237-248；src/features/teaching_animation/render/html_layer/sanitizer.py:755-757
  - 证据:
    ```python
    new, n = pattern.subn(lambda m: f"{m.group(1)}{new_value}", html, count=1)
    ...
    values = {name: str(val) for name, val in merged.items() if val is not None}
    ```
    值直接拼入 `--token:` 声明位置；_filter_declarations/_sanitize_rule_blocks 以 `;`/`}` 分界。
  - 影响: 值含 `red} *{position:fixed;inset:0` 时可提前闭合规则并注入任意选择器的新规则——
    属性仍受白名单约束、url 受策略约束，危害限于视觉/布局篡改与规则逃逸，但违背"单一值"的
    参数语义并为审计埋雷。
  - 建议: 参数/补丁值在拼入前校验不含 `;`、`}`、`<`、换行等元字符（颜色/数值类可用类型化校验）。
  - 置信度: 中

### 通过项
- 状态提交一致性：promote 不改源层（FH-09 fallback 保留）；apply 生成新层并经
  build_html_layer/build_html_layer_from_source 重算哈希与 revision。
- 异常与取消路径：参数校验失败（validate_params + 必填缺失）在替换前抛 ValueError，无部分写入。
- 线程安全：纯函数，无共享状态。
- 性能模式：_MARKER_RE 单遍替换（值不重扫），避免二次注入；_locate_element 复用共享实现。
- 输入验证：promote 对缺失节点/令牌跳过并保持忠实于画布；required 参数无默认值时显式失败。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 2 | AUD-30008, AUD-30017 |
| Major | 9 | AUD-30001, AUD-30002, AUD-30005, AUD-30006, AUD-30009, AUD-30010, AUD-30011, AUD-30014, AUD-30018 |
| Suggestion | 8 | AUD-30003, AUD-30004, AUD-30007, AUD-30012, AUD-30013, AUD-30015, AUD-30016, AUD-30019 |

跨维度说明：本包不含 GLSL/HDR、GPU 资源、Qt 信号路径（浏览器无关纯逻辑层），相关维度全部不适用；
状态提交一致性整体健康（frozen 数据模型 + replace 生成新层），唯一偏差为 AUD-30010 的哈希语义。
两个 Critical 均在真实生产调用链上（html_layer_commands.py:274 的 undo 命令、ai/service.py:270 与
timeline_view.py:14106 的模板实例化）。
