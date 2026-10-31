# batch-276 审查报告 — src/features/teaching_animation/core//

编号区间：AUD-27600–AUD-27699（本报告使用 AUD-27601–AUD-27644）。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解。
本批次为纯数据模型 + HTML 字符串生成层，无 GPU、GLSL、Qt 信号面；重点落在输入验证、异常路径与状态一致性。
标注"已实证"的发现均通过只读 Python 片段在本仓库源码上实际执行验证。

## src/features/teaching_animation/core/html_page_deck.py (行数: 909)

### 发现

- [AUD-27601] [Major] page_timing_for 对 default_seconds/durations_seconds 的 float 转换无防御，损坏的持久化 metadata 会在加载/导出链路抛未捕获 ValueError（已实证）
  - 位置: src/features/teaching_animation/core/html_page_deck.py:511-517
  - 证据:
    ```python
    count = max(1, int(page_count or raw.get("page_count", 1)))
    default = float(raw.get("default_seconds", fallback_default_seconds) or fallback_default_seconds)
    if default <= 0:
        default = fallback_default_seconds
    mode = str(raw.get("mode", "default"))
    durations_raw = raw.get("durations_seconds", ())
    durations = tuple(float(value) for value in durations_raw) if isinstance(durations_raw, (list, tuple)) else ()
    ```
    同函数内 transition_seconds 却有完整防御（525-530 行 `try: ... except (TypeError, ValueError): ...`）。
  - 影响: docstring 承诺 "Read metadata defensively, repairing old/missing layer values"，但 `{"default_seconds": "abc"}` 或 `durations_seconds: ["x"]` 直接抛 `ValueError: could not convert string to float`（已在本机实证）。该函数被 pages.py:179（sync_pages_with_html_layer → model.TeachingSceneDocument.__post_init__ 构造链）、export_service.py:256/372、html_layer_commands.py:166（undo 元数据）等调用，即一个手工编辑/损坏的项目文件会让整个场景加载、页面同步、导出与对应 undo 全部失败，且与相邻字段的防御策略不一致。
  - 建议: 对 default/durations 的转换套用与 transition_seconds 相同的 `try/except (TypeError, ValueError)` + 回退；或抽一个 `_safe_float(value, fallback)` 工具统一三条路径。
  - 置信度: 高

- [AUD-27602] [Major] PageDeckTiming 数值校验用 `<= 0` 拦不住 NaN，NaN 时长经 page_start_frame 的 round(NaN) 抛 ValueError（已实证）
  - 位置: src/features/teaching_animation/core/html_page_deck.py:331-339, 563
  - 证据:
    ```python
    if self.default_seconds <= 0:
        raise ValueError("default_seconds must be positive")
    ...
    if any(value <= 0 for value in self.durations_seconds):
        raise ValueError("page durations must be positive")
    ```
    ```python
    return max(0, round(sum(timing.durations[:page]) * max(0.001, float(fps))))
    ```
  - 影响: 实证：`PageDeckTiming(2, "default", float("nan"))` 构造成功；`page_timing_for({"page_timing": {"default_seconds": nan}}, page_count=2)` 也原样返回 NaN timing（`nan` 为真值绕过 `or fallback`，`nan <= 0` 为 False 绕过校验），随后 `page_start_frame` 抛 `ValueError: cannot convert float NaN to integer`。Python json 默认接受/写出 NaN 字面量，因此含 NaN 的 page_timing 持久化元数据会让 sync_pages_with_html_layer（pages.py:181 逐页调用 page_start_frame）在场景构造期崩溃，整份项目无法加载。
  - 建议: 在 `__post_init__` 与 `page_timing_for` 中用 `math.isfinite` 校验 default_seconds/durations_seconds/transition_seconds；`page_timing_for` 的回退逻辑改为显式 `if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0)`。
  - 置信度: 高

- [AUD-27603] [Suggestion] 页面探测正则不支持属性值内含 ">"，class 位于其后时 detect_page_count 少计、ensure_page_markers 漏标（已实证）
  - 位置: src/features/teaching_animation/core/html_page_deck.py:299-310
  - 证据:
    ```python
    _PAGE_START_RE = re.compile(
        r"<(?P<tag>section|article|div|main|aside|header|footer)\b"
        r"(?P<attrs>[^>]*?)>", re.IGNORECASE | re.DOTALL,
    )
    _CLASS_RE = re.compile(r"\bclass\s*=\s*(['\"])(?P<value>.*?)\1", re.IGNORECASE | re.DOTALL)
    ```
  - 影响: 实证：`<div title="a>b" class="slide">x</div><section class="slide">y</section>` 的 detect_page_count 返回 1（应为 2），ensure_page_markers 只给 section 加了标记。含 `>` 的属性值（如 `aria-label=">"`）出现在 class 之前时，该页被漏计，导致页面时序/导航页数与实际 DOM 不一致。
  - 建议: attrs 段改用引号感知模式（如 `(?:"[^"]*"|'[^']*'|[^>])*`），或在文档级说明该已知限制并接受偏差。
  - 置信度: 高（行为已实证；实际触发依赖少见的属性内容）

### 通过项
- 注入安全：背景值经 `_SAFE_BACKGROUND_RE` 白名单（无法终止声明/注入标记）；背景脚本 payload `json.dumps(...).replace("</", "<\\/")` 防 `</script>`；`timing.transition` 由 `PAGE_TRANSITIONS` 枚举校验后才注入 JS；capture 路径的属性值经 `_html.escape(..., quote=True)`
- `re.sub` 替换串风险已排除：`_background_script` 产生的 `<\/` 反斜杠序列在作为 re.sub 替换串时原样保留（本机实证，Python 3.14），不会触发 bad escape 或被吞
- capture 注入保持 `<!doctype html>` 首位（quirks-mode 注释正确处理了无 `</head>` 文档）
- parse_model_page_timing 对非法 durations/transition/transition-seconds 均有 try/except 与回退；NaN/inf 声明被 `> 0` 与比例缩放检查自然拒绝
- ensure_page_markers 右到左应用偏移、已有 data-topos-page 不重复标注、无候选时字节级原样返回
- page_index_at_frame / page_start_frame 边界（负帧、越界帧、fps<=0 防护）正确
- 单页/未定制背景时不注入 host-owned scaffolding 的门控（_has_background_customization）在 runtime/editor/capture 三条路径一致
- 无 GPU 资源、无 Qt 信号、无共享可变状态（纯函数 + frozen dataclass），线程安全与信号防递归维度不适用且无风险

## src/features/teaching_animation/core/html_template.py (行数: 248)

### 发现

- [AUD-27611] [Suggestion] HtmlTemplate.__post_init__ 缺少 viewport/fps/duration 正值校验，from_dict 的 bound_asset_ids/editable_tokens 元素未字符串化
  - 位置: src/features/teaching_animation/core/html_template.py:76-85, 179, 181
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if not self.template_id:
            raise ValueError("template_id cannot be empty")
        if not self.display_name:
            raise ValueError("display_name cannot be empty")
        if self.version <= 0:
            raise ValueError("template version must be positive")
        names = [p.name for p in self.params_schema]
        if len(names) != len(set(names)):
            raise ValueError("param names must be unique")
    ```
    ```python
    bound_asset_ids=tuple(data.get("bound_asset_ids", ())),
    ...
    editable_tokens=tuple(data.get("editable_tokens", ())),
    ```
  - 影响: 磁盘模板包（discover_stored_templates）属外部数据，`viewport_width: 0`、`fps_den: 0`、`duration_frames: -1` 的损坏包能通过 from_dict 构造并进入 catalog/AI 能力清单，直到 apply 时才被 FreeformHtmlMotionLayer.__post_init__（html_layer.py:129-134）拒绝——失败被推迟到用户操作时刻。`bound_asset_ids`/`editable_tokens` 为数字列表时元素保持 int，与 `tuple[str, ...]` 注解不符（同类其余字段均有 str() 强转）。
  - 建议: __post_init__ 增加 `viewport_width/height > 0`、`fps_num > 0`、`fps_den > 0`、`duration_frames > 0` 校验（与下游一致、提前到加载时刻）；from_dict 对两个 tuple 补 `str(...)` 强转。
  - 置信度: 高

### 通过项
- frozen 数据类 + 参数名唯一性校验；validate_params 的 required/enum/integer/number/boolean/string 分支与 ExecutableTemplate 契约一致，bool 不会被当作 int/number 接受
- to_dict/from_dict 往返完整（含 kind 判别字段）；metadata/slots 均重建新对象不共享调用方引用
- HtmlTemplateRegistry 仅会话内使用（editor window 与 AI service 各自实例化，无跨线程共享，无持久化声明与实际一致）
- 无 GPU/信号面；文档明确 registry 不做磁盘持久化的现状

## src/features/teaching_animation/core/layout.py (行数: 433)

### 发现

- [AUD-27621] [Suggestion] SceneLayout 的 grid_padding/grid_safe_area 长度未校验，AutoLayout 有校验而 grid 路径绕过，resolve() 时解包抛错、布局被静默丢弃
  - 位置: src/features/teaching_animation/core/layout.py:309-315, 372-373, 207-208
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if self.mode not in {"stack", "grid"}:
            raise ValueError(f"unknown SceneLayout mode: {self.mode}")
        if self.mode == "grid" and self.columns <= 0:
            raise ValueError("grid columns must be positive")
        if not self.child_ids:
            raise ValueError("SceneLayout child_ids cannot be empty")
    ```
    ```python
    grid_padding=tuple(float(v) for v in (data.get("grid_padding") or (0.0, 0.0, 0.0, 0.0))),
    grid_safe_area=tuple(float(v) for v in (data.get("grid_safe_area") or (0.0, 0.0, 0.0, 0.0))),
    ```
    而 resolve_grid 内 `pl, pt, pr, pb = padding`（207 行）要求 4 元组。
  - 影响: 持久化 metadata 中 `grid_padding: [0, 0]` 能通过 from_dict 与 read_scene_layout 的宽泛捕获（from_dict 不抛错），直到每帧 resolve() 才在解包处抛 ValueError；evaluator.py:225-228 捕获后 `layout_rects = {}`，用户配置的 Auto Layout 静默失效回退绝对定位，无任何诊断。stack 路径的 AutoLayout.__post_init__ 有同款校验，行为不对称。
  - 建议: SceneLayout.__post_init__ 对 mode=="grid" 校验 grid_padding/grid_safe_area 长度为 4（与非负），使坏数据在 read_scene_layout 处即被降级为 None。
  - 置信度: 高

- [AUD-27622] [Suggestion] resolve_grid 静默丢弃超出 rows×columns 容量的 children，无任何诊断
  - 位置: src/features/teaching_animation/core/layout.py:217-227
  - 证据:
    ```python
    for index, child in enumerate(children):
        col = index % columns
        row = index // columns
        if rows and row >= rows:
            break
        result[child.child_id] = Rect(...)
    ```
  - 影响: child_ids 里排在容量之外的元素不出现在结果中，evaluator 视其为"未参与布局"而保留旧绝对坐标——第 N+1 个元素在画布上停留原地，与 Figma 式网格预期（换页或压缩）不符，且无日志/诊断，排查困难。
  - 建议: 至少记录一条诊断（或返回值附带 dropped_ids），或在 rows 固定时自动扩展 actual_rows 的策略文档化。
  - 置信度: 高

### 通过项
- 纯函数确定性：resolve_stack/resolve_grid/distribute_spacing 均为输入的纯函数，无共享状态、无浮点随机性；STRETCH/CENTER/END/FILL/min-size 语义实现正确
- AutoLayout.__post_init__ 对 padding/safe_area 长度与负值、gap 负值校验完整；Rect 负尺寸拒绝
- read_scene_layout 宽泛捕获返回 None（"layout must never break rendering"），evaluator 侧 resolve 再兜底一层（evaluator.py:225-228 已核实）
- from_dict 对非法枚举（direction/align/sizing）回退默认值；非数值经 float() 抛错被 read_scene_layout 捕获
- write_scene_layout_into 纯拷贝写、不改入参

## src/features/teaching_animation/core/mg_package_registry.py (行数: 208)

### 发现

（无——未发现达到报告门槛的问题）

### 通过项
- 只读目录契约遵守：refresh 只读 presets.records()/discover_stored_templates()，不写两套内容库
- 三级发现（project > user > bundled）与 session 生成器合并的 origin 标记正确；同 id 去重以磁盘目录为准
- 懒导入（refresh 内 import content_localization/motion_graphic_template_store/MgPresetLibrary）避免构造期依赖环与启动开销
- instantiate_html_template_package 的非模板包守卫（ValueError）与调用方（motion_graphic_editor_window.py:2506-2512 捕获 RuntimeError/TypeError/ValueError 并转状态栏消息，已核实）构成完整异常路径
- 排序/检索（casefold、categories 去重保序）确定且稳定
- 注解维度：presets/template_packages/generator_registry 用 Any 是有意的解耦（docstring 说明），不作为缺陷

## src/features/teaching_animation/core/migration.py (行数: 187)

### 发现

- [AUD-27631] [Suggestion] migrate_payload_json 静默丢弃非对象 scenes 条目，损坏场景在加载-保存往返后被无痕抹除
  - 位置: src/features/teaching_animation/core/migration.py:173-182
  - 证据:
    ```python
    scenes = data.get("scenes", [])
    if isinstance(scenes, Mapping):
        scenes = list(scenes.values())
    if not isinstance(scenes, list):
        raise SceneMigrationError("payload scenes must be an array")
    data["scenes"] = [
        migrate_scene_json(scene)
        for scene in scenes
        if isinstance(scene, Mapping)
    ]
    ```
  - 影响: scenes 整体不是 list 会显式报错，但列表内的非对象条目（如 `"scenes": ["corrupt", {...}]`）被静默过滤；用户打开项目（不报错）→ 保存 → 该条目永久消失。与模块 docstring "without dropping unknown fields" 的保数据方针不一致。
  - 建议: 收集被跳过条目的索引，迁移结果附带 warning 列表（或对非对象条目直接 raise SceneMigrationError，保持与 "scenes must be an array" 同等的严格度）。
  - 置信度: 高

### 通过项
- 深拷贝后迁移（deepcopy(dict(raw))），绝不改动调用方输入；未知字段全程保留
- 版本治理正确：拒绝比目标更新的 schema、拒绝非当前 target_version、链尾统一盖章
- _fps_rational 对 fps 数值有有限性/正性校验并转有理数；fps_num/fps_den 缺省回退 24/1；v0 duration 数值校验完整（有限、正、向上取整至少 1 帧）
- v0→v1 的字段改名（layers→elements、animation_tracks→local_animation_tracks）逐条 setdefault/pop，不丢数据
- SceneMigrationError 继承 ValueError，调用方按 ValueError 处理即得一致语义

## src/features/teaching_animation/core/model.py (行数: 934)

### 发现

- [AUD-27641] [Major] _from_current_dict 对非列表/None 的 elements 静默清空或抛 TypeError，损坏文件经"打开-保存"即造成场景内容丢失，且与 v0 迁移路径的显式校验不一致
  - 位置: src/features/teaching_animation/core/model.py:607-611
  - 证据:
    ```python
    elements=tuple(
        SceneElement.from_dict(element)
        for element in data.get("elements", [])
        if isinstance(element, Mapping)
    ),
    ```
    对比 migration.py:55-57（仅 v0 路径）：
    ```python
    elements = data.get("elements", [])
    if not isinstance(elements, list):
        raise SceneMigrationError("elements must be an array")
    ```
  - 影响: 对 schema_version>=1 的场景 JSON：`"elements": {}`（dict 迭代产出键，全部被 isinstance 过滤）→ 场景静默加载为空，用户一保存内容永久丢失；`"elements": null` → `for element in None` 直接 TypeError，报错信息与领域无关。animations/markers/asset_refs/cues/expressions 使用同款过滤，同样静默丢弃列表内的非对象条目。
  - 建议: 在 `_from_current_dict`（或 migrate_scene_json 对全部版本）校验 elements（及各集合字段）为 list，非 list 抛 ValueError；列表内非对象条目至少聚合报错或警告，而非静默过滤。
  - 置信度: 高

- [AUD-27642] [Suggestion] from_dict 多个外部字段未做 str 强转，JSON 中为数字时打破类型注解并可能造成 id 匹配失配
  - 位置: src/features/teaching_animation/core/model.py:192-193, 299, 842-843, 882-883
  - 证据:
    ```python
    asset_ref=data.get("asset_ref"),
    template_id=data.get("template_id"),
    ```
    ```python
    composite_id=data.get("composite_id"),
    render_cache_id=data.get("render_cache_id"),
    ```
  - 影响: 同函数内其余字符串字段均有 str() 强转，这些字段没有：`"asset_ref": 3` 会让 SceneElement.asset_ref 为 int，后续按字符串 asset_id 的查找（asset_refs 表关联、audio plan 校验）静默失配；composite_id/render_cache_id 同理（typed `str | None`）。
  - 建议: 统一 `str(...) if value is not None else None` 式强转，与相邻字段一致。
  - 置信度: 高

- [AUD-27643] [Suggestion] TeachingAnimationProjectPayload.from_dict 对每个场景做两次完整迁移+深拷贝，大项目加载成本翻倍
  - 位置: src/features/teaching_animation/core/model.py:583-587, 912-919；migration.py:132, 178-182
  - 证据:
    ```python
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TeachingSceneDocument":
        from .migration import migrate_scene_json
        migrated = migrate_scene_json(dict(data))
        return cls._from_current_dict(migrated)
    ```
    migrate_payload_json 已先对每个 scene 执行 `migrate_scene_json(scene)`（内部 `data = deepcopy(dict(raw))`），随后 TeachingSceneDocument.from_dict 再次 migrate + deepcopy。
  - 影响: 载入一份多场景、大 HTML 层的项目时，每个场景被 deepcopy/migrate 两轮（含 sanitized_html 大字符串），加载耗时与内存峰值约两倍于必要值；迁移本身幂等，无正确性问题，纯属性能损耗。
  - 建议: _from_current_dict 提供私有入口供 payload 路径复用已迁移结果（跳过二次 migrate/deepcopy），或在 migrate 结果上打标记避免重复。
  - 置信度: 高

- [AUD-27644] [Suggestion] frozen 文档携带可变 Mapping 字段，replace() 生成的新快照与旧快照共享同一 dict 引用，undo/revision 快照隔离依赖外部调用者自律
  - 位置: src/features/teaching_animation/core/model.py:139-141, 410-412, 674-683
  - 证据:
    ```python
    style: Mapping[str, Any] = field(default_factory=dict)
    layout: Mapping[str, Any] = field(default_factory=dict)
    ```
    ```python
    canvas: Mapping[str, Any] = field(default_factory=dict)
    html_layer: FreeformHtmlMotionLayer | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    ```
    with_element_updated 经 `replace(self, elements=...)` 传递原 dict 引用。
  - 影响: 本批次文件内所有构造/序列化路径均 deepcopy（_copy_json），自身无违规；但 frozen 只冻结字段绑定，style/layout/metadata/background/canvas 的 dict 本体可变。任何调用方若原地 `scene.metadata["k"].update(...)` 或改 element.style，会同时改变 undo 栈中共享该引用的所有历史快照，破坏 revision/undo 语义（此类缺陷发生时无异常、极难定位）。
  - 建议: 文档级约定"Mapping 字段只读，改动必须走 replace + 新 dict"并加注释；或对 metadata/style 的热门改写点统一提供 `with_metadata(...)` 式纯函数。
  - 置信度: 中（风险真实存在，但本批次内未发现实际原地改写调用者）

### 通过项
- SceneTransform/ScenePage/SceneCue/SceneExpression/MotionGraphicClip 数值校验完整（有限性、正性、范围），z_index 显式 int 类型检查
- 页面归一化（至少一页、去重、元素挂靠回退）在 __post_init__ 内完成且幂等，replace 路径重跑结果稳定；page_index_for_frame 末页吸收尾段、page_start_frames 累计正确
- with_* 命令族均为纯 replace（无原地修改），重复添加/未找到均显式 ValueError，索引边界校验完整——undo/redo 状态一致性在本层成立
- SceneAssetRef 路径穿越风险已由渲染边界收敛：render/asset_resolver.py:61-64（`candidate != root and root not in candidate.parents` 拒绝逃逸，已核实），模型层仅拒绝绝对路径属分层合理
- model↔pages 无导入环：pages.py 顶层 import model，model 在 __post_init__ 内延迟 import pages（已核实）
- SceneAnimation 的 track param_name 与 property 一致性校验、to_dict 合并往返正确
- from_json 根类型检查（TypeError）；from_dict 经 migrate_scene_json 统一版本治理

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-27601, AUD-27602, AUD-27641 |
| Suggestion | 8 | AUD-27603, AUD-27611, AUD-27621, AUD-27622, AUD-27631, AUD-27642, AUD-27643, AUD-27644 |

mg_package_registry.py 无发现（通过）。
