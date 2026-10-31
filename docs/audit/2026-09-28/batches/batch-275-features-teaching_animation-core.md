# 批次报告：batch-275 — src/features/teaching_animation/core/

- 任务书: docs/audit/2026-09-28/assignments/batch-275.md
- 编号区间: AUD-27500–AUD-27599
- 文件数: 8（2,240 行）
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 汇总: Critical 0 · Major 2 · Suggestion 15

本批全部为纯数据/纯函数模块（无 Qt、无 OpenGL、无 GLSL、无信号），维度 1–4 在所有文件均不适用且未发现问题；发现集中在状态一致性、输入验证与代码质量。

---

## src/features/teaching_animation/core/devtools_property_mapping.py (行数: 274)

### 发现

- [AUD-27500] [Suggestion] 元素 id 提取存在永假死分支，`_values_equal` 集合字面量含重复项
  - 位置: src/features/teaching_animation/core/devtools_property_mapping.py:91-92, 147
  - 证据:
    ```python
    markers = {key: value for key, value in attrs if key == "data-topos-native-element"}
    if markers and self._current is None:
        self._current = _NodeSnapshot(tag=tag.lower())
        self.nodes[markers[""] if "" in markers else str(list(markers.values())[0])] = \
            self._current
    ```
    ```python
    if key in {"color", "background-color", "background", "fill", "background"}:
    ```
  - 影响: `markers` 的键只能是属性名 `data-topos-native-element`，`"" in markers` 永假，前半分支为死代码；可读性差且暗示了不存在的语义。第 147 行集合中 `"background"` 重复一次（无行为影响）。另外属性无值时 HTMLParser 给 `None`，`str(None)` 会把元素 id 变成 `"None"`。
  - 建议: 直接取 `dict(attrs)` 过滤后的首个值并对 `None` 显式报 unmappable；去重集合字面量。
  - 置信度: 高

- [AUD-27501] [Suggestion] void/未闭合原生元素会使解析器停滞，后续兄弟原生节点全部丢失
  - 位置: src/features/teaching_animation/core/devtools_property_mapping.py:87-108
  - 证据:
    ```python
    def handle_starttag(self, tag, attrs):
        markers = ...
        if markers and self._current is None:
            ...
        elif self._current is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._current is not None:
            if self._depth == 0:
                self._current = None
    ```
  - 影响: 若原生元素是没有闭合标签的 void 标签（或 DevTools 会话留下未闭合标记），`_current` 永不复位，后续所有兄弟原生元素落入 `elif` 分支不被收集，diff 结果错误且不会进入 unmappable 回退路径（静默丢失而非降级为整层替换）。
  - 建议: 对常见 void 标签集合在 `handle_starttag` 中直接完成快照不入栈；或在解析结束后检测 `_current is not None` 并将整份标记报告为 unmappable。
  - 置信度: 低（Chromium 序列化输出通常良构、原生元素当前为 div，触发条件罕见）

- [AUD-27502] [Suggestion] 文本编辑会把字符串型 `content` 静默改写为 `{"text": ...}` 字典，改变字段类型形状
  - 位置: src/features/teaching_animation/core/devtools_property_mapping.py:259-264
  - 证据:
    ```python
    else:  # content
        content = bucket.get("content")
        if content is None:
            content = dict(element.content) if isinstance(element.content, dict) else {}
            bucket["content"] = content
        content[edit.property] = edit.value
    ```
  - 影响: `SceneElement.content` 类型为 `Any`，默认 `""`（model.py:141）。字符串 content 的元素被编辑后 content 变成只含 `text` 键的字典——渲染层（html_native_overlay.py:272、render/advanced.py:24）恰好对 dict 有兜底所以多数路径仍能渲染，但类型形状与作者数据不一致，`UpdateElementCommand` 的整体替换语义使其不可逆（undo 可恢复，但后续保存的工程数据形状漂移）。
  - 建议: 非 dict content 时构造 `{"text": value}` 前记录原值，或直接保持字符串赋值语义（`content = edit.value`），与渲染层两种读取方式对齐。
  - 置信度: 中

- [AUD-27503] [Suggestion] `background`/`color` 新值未验证即写入 `style.fill`，任意文本可成为填充色
  - 位置: src/features/teaching_animation/core/devtools_property_mapping.py:209-210, 135-143
  - 证据:
    ```python
    elif key in {"color", "background-color", "background"}:
        style_edits[mapping[1]] = _color_key(new_value)
    ```
    ```python
    def _color_key(value: str) -> str:
        ...
        return text   # 非 #hex 且非 rgb() 整数形式时原样返回
    ```
  - 影响: `background` 简写可含 `url(...)`、渐变、`rgba(…)` 小数分量等；`_color_key` 对这些返回原文，未经校验就写入 `style.fill`，下游按颜色值消费 `fill` 的代码可能得到非颜色字符串。
  - 建议: 对写入 `style.fill` 的值做颜色白名单校验（#hex / rgb() 含小数 / 命名色），不合法则报 unmappable 走整层替换回退。
  - 置信度: 中

### 通过项
- 线程安全：纯 stdlib、无共享可变状态（解析器实例局部）。
- GPU 资源生命周期：不涉及 GPU/ModernGL。
- GLSL·HDR 正确性：不涉及着色器。
- 信号防递归：不涉及 Qt 信号。
- 状态提交一致性：edits 折叠为每元素一条 `UpdateElementCommand`（commands.py:102-114 对 updates 做字段白名单与 deepcopy），未知元素 id 报 unmappable 而非抛异常，回退路径完整。
- 异常路径：`_as_float` 捕获 ValueError 返回 None 转为 unmappable，无宽泛 except。
- 性能：diff 为 O(节点数 × 样式键)，sorted 遍历保证确定性输出。
- 类型注解：公共 API 注解完整。

---

## src/features/teaching_animation/core/executable_template.py (行数: 266)

### 发现

- [AUD-27504] [Suggestion] 未知参数被当作致命错误，与注释“preserved (round-trip) but reported”直接矛盾
  - 位置: src/features/teaching_animation/core/executable_template.py:104-108, 129-132
  - 证据:
    ```python
    for key, value in params.items():
        if key not in known:
            # Unknown params are preserved (round-trip) but reported.
            errors.append(f"unknown parameter: {key}")
            continue
    ```
    ```python
    errors = self.validate_params(merged)
    if errors:
        raise ValueError(f"invalid params for template {self.template_id}: {'; '.join(errors)}")
    ```
  - 影响: 注释声称未知参数被保留并仅“报告”，实际 `instantiate` 对任何非空 errors 直接 raise——传入多余参数（如 AI 生成参数带冗余键）会硬失败而非保留。`from_dict` 路径不经过 validate 所以工程往返不受影响，但注释描述的行为不存在，误导维护者。
  - 建议: 二选一对齐——要么把未知参数从 errors 改为单独的 warnings 列表返回，要么修正注释为“未知参数导致实例化失败”。
  - 置信度: 高（矛盾本身确定；哪边是意图不确定）

### 通过项
- 线程安全：注册表为普通 dict，本模块未声明线程安全，实例不可变（frozen dataclass）。
- GPU/GLSL/信号：不涉及。
- 状态提交一致性：`compile` 优先实例版本、回退 latest 并经 migrate 适配（232-238 行），`__post_init__` 校验 id/版本/参数名唯一。
- 异常路径：KeyError/ValueError 语义明确，无宽泛 except。
- 输入验证：required/enum/integer/number/boolean/string 类型检查完整（114-121 行），bool 与 int 正确区分。
- 性能：`validate_params` 内 `next(p for p in ...)` 为 O(n²) 于参数数，模板参数规模小，可接受。
- 持久化：`to_dict`/`from_dict` 对 context 字段带默认值往返完整。

---

## src/features/teaching_animation/core/font_registry.py (行数: 178)

### 发现

- [AUD-27505] [Major] 交付许可检查只看链中第一个可解析字体，实际供字的回退字体许可未被检查
  - 位置: src/features/teaching_animation/core/font_registry.py:157-173
  - 证据:
    ```python
    for family in families:
        entry = self.resolve_family(family)
        if entry is None:
            continue
        if chosen is None:
            chosen = entry
        covers = {cp for cp in text_codepoints if entry.covers_codepoint(cp)}
        missing_by_font[family] = tuple(sorted(text_codepoints - covers))
        unresolved -= covers
    ...
    blocks = bool(chosen is not None and chosen.license in (FontLicense.OPEN, FontLicense.EMBEDDED))
    return FontResolution(..., blocks_delivery=not blocks)
    ```
  - 影响: `chosen` 固定为链中第一个能解析的 family。若首选字体为 OPEN 但只覆盖拉丁，而实际覆盖 CJK/emoji 的后继回退字体是 `PREVIEW_ONLY`/`MISSING`，这些字形仍会进入渲染输出，`blocks_delivery` 却为 False——模块的核心目标（“No-licence or missing-glyph fonts are reported so they can block/warn before delivery”）被绕过，许可受限字形可流入交付物。
  - 建议: 对链中每个实际贡献了字形的字体（`covers` 非空）检查许可，任一非 OPEN/EMBEDDED 即置 `blocks_delivery=True`，并在 `missing_by_font`/结果中带上触发许可问题的 family 名。
  - 置信度: 高

- [AUD-27506] [Suggestion] 回退链中未注册的 family 被静默跳过，与“字体替换可诊断”的模块目标相悖
  - 位置: src/features/teaching_animation/core/font_registry.py:158-160
  - 证据:
    ```python
    entry = self.resolve_family(family)
    if entry is None:
        continue
    ```
  - 影响: 链里写错/未注册的 family 名直接消失，`missing_by_font` 与结果中均无痕迹；调用方无法得知链配置引用了不存在的字体。
  - 建议: 为未解析 family 记录一条显式条目（如 `missing_by_font[family] = ()` 配独立 `unknown_families` 字段），保持替换决策可诊断。
  - 置信度: 中

- [AUD-27507] [Suggestion] `covers_text` 返回未排序的码点元组，输出顺序不确定
  - 位置: src/features/teaching_animation/core/font_registry.py:96-99
  - 证据:
    ```python
    def covers_text(self, text: str) -> tuple[bool, tuple[int, ...]]:
        missing = tuple({ord(ch) for ch in text if not self.covers_codepoint(ord(ch))})
        return (not missing, missing)
    ```
  - 影响: 集合推导的迭代顺序依赖哈希，同一文本两次运行/两个进程可能得到不同顺序的 missing 元组；对比 `resolve_chain`（164 行 `tuple(sorted(...))`）已排序，风格不一致且影响确定性输出（码点列表进日志/哈希时不稳定）。
  - 建议: `tuple(sorted({...}))`。
  - 置信度: 高（行为确定；影响小）

### 通过项
- 线程安全：注册表 dict 未做并发保护，但本模块在文档定位中为构建期/单线程使用，无渲染线程接触点。
- GPU/GLSL/信号：不涉及。
- 输入验证：`FontEntry.__post_init__` 校验 family 非空、file_uri 相对路径、coverage_blocks 属于已知块；`from_dict` 对非法 license 枚举降级为 MISSING 而非崩溃。
- 异常路径：无宽泛 except。
- 性能：`resolve_chain` 对每个 family 全量扫描码点集合，O(families × codepoints)，字体链短，可接受。

---

## src/features/teaching_animation/core/graphics.py (行数: 332)

### 发现

- [AUD-27508] [Suggestion] `_fmt` 对 NaN/Inf 坐标产出 `"nan"`/`"inf"` 字符串污染 SVG path 数据
  - 位置: src/features/teaching_animation/core/graphics.py:46-49
  - 证据:
    ```python
    def _fmt(value: float) -> str:
        """Format a coordinate without trailing zeros (3,0 -> "3"; 3.5 -> "3.5")."""
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text if text else "0"
    ```
  - 影响: 上游几何计算产生 NaN/Inf（如除零、退化反三角输入）时，`to_svg_d()` 会生成 `M nan inf` 之类的非法 path，浏览器静默丢弃整个路径，缺陷难定位；本模块作为“跨 SVG/GPU/HTML 同一定义”的权威序列化点应自卫。
  - 建议: `_fmt` 用 `math.isfinite` 校验，非有限值抛 ValueError（fail-fast）或夹取到 0。
  - 置信度: 低（正常调用链传有限值；属防御缺口）

### 通过项
- 线程安全：全部纯数据 + 纯函数，frozen dataclass。
- GPU/GLSL/信号：不涉及（SVG 序列化路径独立于着色器）。
- 状态提交一致性：`Connector.path_for` 从节点矩形即时重算，无陈旧存储几何（SG-07 契约）；`recompute_connectors` 对缺失节点跳过而非抛错，删除节点自然断链。
- 输入验证：`rounded_rect_path` 半径夹取到半短边、负宽高归零；`Connector.__post_init__` 拒绝空 id 与自环。
- 性能：无可变全局；`arrow_path` 内 `import math` 为局部导入（微小开销，风格问题）。
- 浮点字面量：全部显式 `.0`（0.5522847498 等），符合规范。
- `combine_paths` 明确文档化为子路径拼接而非布尔运算，未伪装能力。

---

## src/features/teaching_animation/core/hierarchy.py (行数: 234)

### 发现

- [AUD-27509] [Suggestion] `duplicate_parent` 检查为死代码——dict 键唯一使该分支永不可触发
  - 位置: src/features/teaching_animation/core/hierarchy.py:129-133
  - 证据:
    ```python
    seen_children: set[str] = set()
    for child, parent in hierarchy.parents.items():
        if child in seen_children:
            issues.append(HierarchyIssue(child, "duplicate_parent", "element has multiple parents"))
        seen_children.add(child)
    ```
  - 影响: `hierarchy.parents` 是 `Mapping[str, str]`（dict），同一 child 不可能作为键出现两次，“元素多父”问题在数据结构层面已不可能，检查永远不触发，给读者虚假的安全感。
  - 建议: 删除该分支，或把不变式注释说明为“由 dict 键唯一性保证”。
  - 置信度: 高

- [AUD-27510] [Suggestion] `Hierarchy.from_dict` 完全忽略载荷中的 `schema_version`，未来 v2 数据会被静默按 v1 解析
  - 位置: src/features/teaching_animation/core/hierarchy.py:107-115
  - 证据:
    ```python
    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> "Hierarchy":
        data = data or {}
        return cls(
            parents={str(k): str(v) for k, v in (data.get("parents") or {}).items()},
            ...
        )
    ```
  - 影响: `to_dict` 写出 `schema_version`，`from_dict` 读时不比对 `_SCHEMA_VERSION`；一旦层级结构升级（如增加字段/语义变更），旧读取器会把 v2 载荷当 v1 静默解释，违背“无 schema 版本升级也要显式迁移”的设计意图。
  - 建议: `from_dict` 读取 `schema_version`，高于 `_SCHEMA_VERSION` 时抛出明确错误或走迁移钩子。
  - 置信度: 中

- [AUD-27511] [Suggestion] `sort_for_paint`/`world_affine` 关键参数与返回值缺类型注解（`elements: Any`、`id_of: Any`）
  - 位置: src/features/teaching_animation/core/hierarchy.py:196-214, 156-165
  - 证据:
    ```python
    def sort_for_paint(
        elements: Any,
        parents: Mapping[str, str],
        *,
        id_of: Any = None,
        z_of: Any = None,
    ) -> list:
    ```
  - 影响: 适配 `EvaluatedElement` 等异构输入的灵活性以丢失全部静态检查为代价；`world_affine` 中 `transforms[element_id]`（159 行）对缺失元素直接 KeyError，未在签名/文档中声明前置条件。
  - 建议: 用 `Callable[[Any], str]`/`Callable[[Any], int]` 与 `Iterable` 注解，`world_affine` 对缺失 id 抛带元素 id 的 KeyError 或显式校验。
  - 置信度: 高（注解缺失确定；`z_index` 已在 model.py:154-155 强制 int，无截断风险）

### 通过项
- 线程安全：纯函数 + frozen dataclass，无共享可变状态。
- GPU/GLSL/信号：不涉及。
- 状态提交一致性：层级经 `write_hierarchy_into` 生成新 metadata dict（不改原对象），`read_hierarchy` 对非 Mapping 安全降级为空层级。
- 逻辑正确性：`Affine2d.compose` 矩阵乘法与 `world_affine` 的 parent-first 顺序核对无误；`effective_visibility` 的 hidden 祖先传播与 solo 子树隔离语义与文档一致；`ancestors`/`sort_for_paint` 均有 `seen` 防环。
- `sort_for_paint` 的 root-first z 元组排序正确实现“祖先严格先于后代绘制”。

---

## src/features/teaching_animation/core/html_fonts.py (行数: 183)

### 发现

- [AUD-27512] [Suggestion] `default_font_set_hash` 依赖 `_PACK` 字典字面量顺序与 `DEFAULT_FONT_ASSET_IDS` 声明顺序一致的隐式 zip
  - 位置: src/features/teaching_animation/core/html_fonts.py:103-108
  - 证据:
    ```python
    entries = load_default_html_font_pack(assets_dir)
    by_id = dict(zip(DEFAULT_FONT_ASSET_IDS, entries))
    parts = sorted(f"{aid}:{by_id[aid].file_hash}" for aid in DEFAULT_FONT_ASSET_IDS)
    ```
  - 影响: `load_default_html_font_pack` 按 `_PACK` 迭代序返回 entries，正确性依赖两个常量的插入顺序永远保持一致；未来任何人调整 `_PACK` 字面量的键顺序（语义上无变化）会使 `by_id` 错位——TTF 哈希挂到错误 asset_id 上，且排序后哈希仍合法、无崩溃信号，属于静默的缓存键错配。
  - 建议: 让 `load_default_html_font_pack` 返回 `dict[asset_id, FontEntry]`，或 zip 前断言长度一致并直接以 `_PACK` 键为序。
  - 置信度: 中（当前顺序一致，属脆弱耦合而非现行 bug）

### 通过项
- 线程安全：`lru_cache` 为 CPython 线程安全实现，CSS 构建幂等。
- GPU/GLSL/信号：不涉及。
- 确定性设计：字体字节内联为 data: URI、`*{font-family:...!important}` 强制别名、TTF 哈希（而非 woff2）作为 font_set_hash——与模块文档的跨机确定性目标自洽。
- 资源存在性：`src/assets/fonts/` 下 6 个 TTF/woff2 文件与 `_PACK` 声明一一对应（已核实），`parents[3]` 路径解析在仓库布局下正确。
- 输入验证：`_resolve_dir` 对 None 走默认路径；文件缺失时 `read_bytes` 抛 FileNotFoundError（响亮失败，非静默）。
- 性能：~850KB base64 CSS 经 `lru_cache(maxsize=8)` 每 assets-dir 只构建一次，`wrap_with_font_lock` 支持外部预计算 CSS 复用。

---

## src/features/teaching_animation/core/html_layer.py (行数: 247)

### 发现

- [AUD-27513] [Major] `from_dict` 信任外部载荷的 `sanitizer_hash`，不校验其与 `sanitized_html` 的一致性
  - 位置: src/features/teaching_animation/core/html_layer.py:137-139, 188
  - 证据:
    ```python
    # Keep sanitizer_hash consistent with the payload when not overridden.
    if not self.sanitizer_hash:
        object.__setattr__(self, "sanitizer_hash", _sha256(self.sanitized_html))
    ```
    ```python
    sanitizer_hash=str(data.get("sanitizer_hash", "")),
    ```
  - 影响: `__post_init__` 只在哈希为空时推导；`from_dict` 原样接受工程文件中的任意值，而下游校验器（core/validator.py:517-521）也只检查“非空字符串”。被手改/损坏/旧版本写入的工程文件可携带与 `sanitized_html` 不一致的溯源哈希，直接进入 `RenderEvidence`/缓存键（export_service.py:485 消费）——哈希是本模块声明的缓存失效与审计锚点，不一致即缓存可能命中错误帧且审计结论失真。这违反“外部数据须验证”铁律。
  - 建议: `from_dict` 重算 `_sha256(sanitized_html)` 与载荷比对，不一致时报校验 issue（或覆盖并记录告警），至少在 core/validator.py 的 html_layer 校验段增加一致性检查。
  - 置信度: 中（仓库内写路径——ReplaceHtmlSourceCommand、html_patch_apply.py:347——均正确重算哈希，风险面是外部工程文件加载）

### 通过项
- 线程安全：frozen dataclass、无共享可变状态（metadata 用 default_factory 每实例新建）。
- GPU/GLSL/信号：不涉及。
- 输入验证：`__post_init__` 对 layer_id/HTML 非空、viewport/fps/duration/revision 正数全量校验；`HtmlEditSlot` 对 edit_mode/kind 白名单校验；`from_dict` 对 slot 逐项 `isinstance(slot, Mapping)` 过滤。
- 状态提交一致性：revision 语义清晰（补丁修订号），`build_html_layer` 同时推导 source_hash/sanitizer_hash 双锚点。
- 异常路径：非法构造抛 ValueError 语义明确，无宽泛 except。

---

## src/features/teaching_animation/core/html_layer_commands.py (行数: 526)

### 发现

- [AUD-27514] [Suggestion] `RemoveHtmlLayerCommand` 移除层后不同步 `scene.pages`，残留由已删层生成的页组
  - 位置: src/features/teaching_animation/core/html_layer_commands.py:233-241
  - 证据:
    ```python
    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        object.__setattr__(self, "_previous_layer", scene.html_layer)
        object.__setattr__(self, "_applied", True)
        return replace(scene, html_layer=None)
    ```
  - 影响: 其余四个 HTML 命令（Set/Replace/Patch/Timing）apply 后都调用 `sync_pages_with_html_layer`，唯独 Remove 不调用（且 `sync_pages_with_html_layer` 对 `layer is None` 直接返回原 scene，pages.py:175-176，想同步也做不到）。移除层后，由该层 HTML 检测出的页数/时长仍留在 `scene.pages` 与页面轨道中，页组引用一个已不存在的层；undo 对称（apply 未动 pages），但已应用状态下页组与层不一致。
  - 建议: 为 `sync_pages_with_html_layer` 增加“层移除时收敛为单页/保留用户命名页”的策略，或在 Remove 的 apply 中显式将 pages 归一到结构化场景默认，并同步记录 `_previous_pages`。
  - 置信度: 中（可能是“保留页面骨架供结构化元素继续使用”的有意选择，但与同文件其余命令的同步约定不一致）

- [AUD-27515] [Suggestion] 未使用的 `SceneCommand` 导入
  - 位置: src/features/teaching_animation/core/html_layer_commands.py:29
  - 证据:
    ```python
    from .commands import SceneCommand
    ```
  - 影响: 全文件仅此一处出现 `SceneCommand`（commands.py:21 中它是 `Protocol`，本文件命令以结构化类型鸭子实现协议，无需导入）；死导入徒增阅读负担与循环导入风险面。
  - 建议: 删除该导入；如需声明协议一致性，可加 `if TYPE_CHECKING` 并在各命令类上注释说明。
  - 置信度: 高

- [AUD-27516] [Suggestion] `SetHtmlPageTimingCommand.timing: Any` 等私有状态字段缺具体类型注解
  - 位置: src/features/teaching_animation/core/html_layer_commands.py:81, 57-59, 262-264
  - 证据:
    ```python
    @dataclass(frozen=True)
    class SetHtmlPageTimingCommand:
        """Change page-deck timing without touching the authored HTML source."""

        timing: Any
    ```
    ```python
    _previous_layer: FreeformHtmlMotionLayer | None = None
    _previous_pages: tuple[ScenePage, ...] | None = None
    _previous_elements: Any = None
    ```
  - 影响: `timing` 实际契约是 `PageDeckTiming`（apply 内访问 `total_seconds` 并传给 `timing_metadata`），标 `Any` 使错误类型的 timing 推迟到 apply 中段抛 AttributeError；`_previous_elements: Any` 同样弱化（实为 `tuple[SceneElement, ...] | None`）。
  - 建议: `timing: "PageDeckTiming"`（TYPE_CHECKING 导入），`_previous_elements: tuple[SceneElement, ...] | None`。
  - 置信度: 高

### 通过项
- 线程安全：命令为 frozen dataclass + `object.__setattr__` 记录前值，场景以 `replace()` 生成新文档，命令栈（commands.py:1194-1226）不突变 scene；本模块无 Qt/渲染线程接触点。
- GPU/GLSL/信号：不涉及。
- undo/redo 一致性：五个命令均记录 `_previous_layer/_previous_pages/_previous_elements` 并在 undo 精确还原；apply 前置校验（无层抛 ValueError）先于任何状态记录（ReplaceHtmlSourceCommand 的 sanitize 在记录前执行，失败不污染命令状态）；`SceneCommandStack.execute` 中 apply 抛异常则命令不入栈，半记录状态随对象丢弃，无栈内不一致。
- 异常路径：错误信息具体（“scene has no html_layer”、清洗失败附 report.notes），无宽泛 except。
- 输入验证：sanitize 结果空 HTML 显式拒绝；`detect_page_count` 内部 `max(1, ...)`（html_page_deck.py:379, 386）保证除法无零除；`build_html_layer_from_source` 对 viewport/duration/fps 全部 `max(1, int(...))` 夹取。
- 许可/CSP：font-lock 启用时 CSP 资产白名单并入 `DEFAULT_FONT_ASSET_IDS`，与 html_fonts 的字体锁配套；freeform profile 的宽松 sanitize 标志为文档化的产品决策且仍经 CSP 约束。
- 性能：重导入（sanitizer/page_deck/node_identity/CspPolicy）均延迟到函数体内，避免模块级循环导入与启动开销。

---

## 总体结论

本批 8 个文件均为 teaching_animation 的纯数据核心（模型、模板、字体、图形、层级、HTML 层及其命令），无 UI 线程/GPU/信号面，铁律中与渲染线程相关各项全部通过。两个 Major 均为“声明的不变量在边界处未强制”：

1. AUD-27505（font_registry）：交付许可门禁只检查首选字体，回退字体的许可被绕过——直接影响模块存在的理由。
2. AUD-27513（html_layer）：溯源哈希在外部载荷加载时不验证一致性，缓存键/审计锚点可被不一致数据穿透。

其余 15 条 Suggestion 集中在死代码/注释矛盾（AUD-27500、27504、27509）、静默类型变形（AUD-27502）、输入白名单缺口（AUD-27503、27506）与确定性/脆弱耦合（AUD-27507、27512）及注解缺失（AUD-27511、27516）。
