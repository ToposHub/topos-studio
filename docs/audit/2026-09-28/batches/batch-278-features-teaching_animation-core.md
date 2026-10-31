# 批次审查报告：batch-278

- 主题：src/features/teaching_animation/core//
- 编号区间：AUD-27800–AUD-27899
- 日期：2026-09-28
- 文件：text_layout.py (339)、validator.py (596)、vector_shapes.py (158)，共 1,093 行
- 结论摘要：Critical 0 / Major 0 / Suggestion 9。三个文件均为纯 Python 无头核心模块（无 Qt 控件、无 ModernGL/GLSL、无信号），九维中 GPU、GLSL·HDR、信号防递归、线程安全维度全部通过；发现集中在排版边界行为与校验契约一致性。

## src/features/teaching_animation/core/text_layout.py (行数: 339)

### 发现

- [AUD-27801] [Suggestion] 省略号宽度按截断前行最大字号测量，与截断后重算的字号不一致
  - 位置: src/features/teaching_animation/core/text_layout.py:247-252
  - 证据:
    ```python
    ellipsis_w = measure.measure(run.ellipsis, _line_font_size(kept[-1]))
    kept[-1] = _truncate_line_for_ellipsis(kept[-1], run.max_width, ellipsis_w)
    # append the ellipsis as a synthetic unit on the last line
    fs = _line_font_size(kept[-1]) or 24.0
    kept[-1].append(_Unit(run.ellipsis, ellipsis_w, fs, kept[-1][-1].color if kept[-1] else "#ffffff",
                          "regular", breakable_before=False, is_space=False))
    ```
  - 影响: `_truncate_line_for_ellipsis` 可能删掉携带最大字号的单元，使截断后 `fs` 变小，但 `ellipsis_w` 仍按截断前的最大字号计算。合成的省略号单元 `width` 与其声明的 `font_size` 不匹配（渲染端按 font_size 重排时省略号位置/预留空间偏差），截断预留也偏保守。
  - 建议: 在 `_truncate_line_for_ellipsis` 之后按最终行字号重测 `ellipsis_w`，或将省略号宽度统一延迟到渲染时按其单元 font_size 计算。
  - 置信度: 高

- [AUD-27802] [Suggestion] 超过 max_width 的不可断行单元（长单词/长 token）静默溢出行盒
  - 位置: src/features/teaching_animation/core/text_layout.py:225
  - 证据:
    ```python
    for unit in units:
        would_wrap = current and current_w + unit.width > run.max_width + 1e-6 and unit.breakable_before
        if would_wrap:
    ```
  - 影响: 当 `current` 为空（行首）时短路求值使换行判定恒为 False，单个宽度大于 `max_width` 的连续拉丁词元（URL、公式片段）被整单元放入一行，`LineBox.width` 超出 `max_width`，CENTER/RIGHT 的 slack 被钳为 0，JUSTIFY 不拉伸；内容溢出文本框且无任何标记或截断（除非同时触发 max_lines）。
  - 建议: 对行首仍超宽的词元增加字符级强制断行或显式溢出标记（如在 LineBox 上加 `overflow` 字段），供上层选择缩字或裁剪策略。
  - 置信度: 高

- [AUD-27803] [Suggestion] JUSTIFY 对无空格的 CJK 行不生效，中文两端对齐静默退化为左对齐
  - 位置: src/features/teaching_animation/core/text_layout.py:281-284
  - 证据:
    ```python
    if run.align == TextAlign.JUSTIFY and slack > 0.0 and line_units is not raw_lines[-1]:
        gaps = sum(1 for u in line_units if u.is_space)
        if gaps:
    ```
  - 影响: 纯中文（或 CJK+少量标点）行的 `gaps == 0`，justify 分支被跳过，非末行保持左对齐。教学动画验收目标明确包含 CJK 排版，两端对齐对中文正文是常见需求；目前无任何诊断提示。注：`layout_text` 当前在库内尚无调用方（仅 core/__init__.py 再导出），故暂无用户可见影响。
  - 建议: 当 `gaps == 0` 且行内存在多个 `breakable_before` 的 CJK 单元时，把 slack 分摊到 CJK 字符间；至少在文档中声明该限制。
  - 置信度: 中（不排除是阶段性设计取舍，但模块与注释均未声明）

- [AUD-27804] [Suggestion] TextSpan 的 font_size/letter_spacing 未做数值校验，NaN 会静默破坏换行
  - 位置: src/features/teaching_animation/core/text_layout.py:41-46（对照 59-65）
  - 证据:
    ```python
    text: str
    font_size: float = 24.0
    color: str = "#ffffff"
    weight: str = "regular"
    letter_spacing: float = 0.0      # em, added per character
    ```
    （`TextRun.__post_init__` 只校验 `max_width <= 0.0` / `line_height <= 0.0` / `max_lines < 0`，且 `NaN <= 0.0` 为 False，NaN 也能通过；span 级数值完全未校验）
  - 影响: `font_size=NaN` 时 `measure` 返回 NaN，`current_w + unit.width > run.max_width + 1e-6` 恒为 False，永不换行，所有 fragment 坐标/宽度为 NaN 并静默输出；负 `font_size` 产生负宽度。模块定位为可注入外部 measurer 的公共核心，缺乏对入口数值的防线。
  - 建议: 在 `TextSpan.__post_init__` 校验 `font_size` 为有限正数、`letter_spacing` 为有限数；`TextRun.__post_init__` 对 `max_width`/`line_height` 改用 `not (x > 0.0)` 形式以排除 NaN。
  - 置信度: 高（逻辑追踪确定；影响取决于未来调用方）

- [AUD-27805] [Suggestion] 未使用的导入 `field`
  - 位置: src/features/teaching_animation/core/text_layout.py:21
  - 证据:
    ```python
    from dataclasses import dataclass, field
    ```
  - 影响: 全文件无 `field(...)` 使用（各 dataclass 字段均为标量/元组字面量默认值），留下 lint 噪音。
  - 建议: 改为 `from dataclasses import dataclass`。
  - 置信度: 高

### 通过项
- 线程安全：纯函数 + frozen dataclass，无共享可变状态，可任意线程调用
- GPU 资源生命周期：不涉及 ModernGL/纹理/缓冲
- GLSL·HDR 正确性：不涉及着色器与像素数据
- 信号防递归：无 Qt 控件与信号
- 状态提交一致性：无 undo/redo/revision/持久化职责
- 异常与取消路径：`TextRun.__post_init__` 显式 `ValueError`，无宽泛 except
- 性能模式：贪心换行 O(units)；`_truncate_line_for_ellipsis` 的循环内 `sum()` 为 O(n²) 但 n 受行宽上界约束，可接受
- 类型注解：公开 API 与内部函数全部标注

## src/features/teaching_animation/core/validator.py (行数: 596)

### 发现

- [AUD-27810] [Suggestion] 运行时校验与导出的 JSON Schema 契约发散（background / animation_id / seed·version / payload clips·nodes）
  - 位置: src/features/teaching_animation/core/validator.py:31-35（对照 379-396、504-532、543-590）
  - 证据:
    ```python
    "required": [
        "scene_id", "schema_version", "name", "width", "height",
        "fps_num", "fps_den", "duration_frames", "background",
        "elements", "animations", "markers", "asset_refs",
    ],
    ```
    ```python
    animation_id = animation.get("animation_id")
    if animation_id in animation_ids:
        issues.append(ValidationIssue(f"{path}.animation_id", "duplicate", "animation_id must be unique"))
    if isinstance(animation_id, str):
        animation_ids.add(animation_id)
    ```
  - 影响: 文件头注释声明 SCENE_JSON_SCHEMA 是"tooling and AI dry-runs"的契约、运行时是其超集，但实际存在双向缺口：(1) `background` 在 schema 中 required，`validate_scene_json` 全程未读取该键，缺失不报错；(2) `animations[].animation_id` 在 schema 中 required + minLength 1，运行时对缺失/非字符串不产生任何 issue（缺失时 model 层 `from_dict` 会自动生成新 id，见 model.py:231，但直接对原始 JSON 调 `validate_scene_json`/AI dry-run 场景下两套契约判定不一致）；(3) `expressions[].seed/version` 仅 schema 约束，运行时不校验；(4) `PAYLOAD_JSON_SCHEMA` required 的 `clips`/`nodes` 缺失时运行时按 `[]` 处理且不报错。工具链与运行时对同一文件的接受/拒绝结论可能相反。
  - 建议: 以运行时为准补齐或收敛：为 `animation_id` 增加 required/非空字符串检查（与 marker/cue 一致）、决定 `background` 是否真为必填、对 seed/version 补运行时类型检查，或从 schema 的 required 中移除实际可选的键。
  - 置信度: 高（发散事实由代码对照确定；影响等级为 Suggestion 是因为 facade 路径先经 `from_dict` 再校验，已缓解）

- [AUD-27811] [Suggestion] html_layer.sanitized_html 的非空检查对非字符串值做 str() 强转后放行
  - 位置: src/features/teaching_animation/core/validator.py:509-511
  - 证据:
    ```python
    if not str(html_layer.get("sanitized_html", "")).strip():
        issues.append(ValidationIssue("html_layer.sanitized_html", "required",
                                      "html_layer.sanitized_html cannot be empty"))
    ```
  - 影响: `sanitized_html` 为 dict/list/int 时 `str()` 结果非空，类型错误不报 issue；下游 `html_layer.py:172` 的 `from_dict` 同样 `str()` 强转，垃圾字符串会作为"已消毒 HTML"进入文档与渲染（FH-01 是安全敏感层，类型应从严）。
  - 建议: 改为 `if not isinstance(html_layer.get("sanitized_html"), str) or not html_layer["sanitized_html"].strip():`，类型与非空分别报 issue。
  - 置信度: 高

- [AUD-27812] [Suggestion] z_index 类型检查接受 bool，与同文件其他整数字段的 bool 排除惯例不一致
  - 位置: src/features/teaching_animation/core/validator.py:341-342
  - 证据:
    ```python
    if not isinstance(element.get("z_index", 0), int):
        issues.append(ValidationIssue(f"{path}.z_index", "type", "z_index must be an integer"))
    ```
    （对照本文件 frame/seed 等处的 `isinstance(value, int) or isinstance(value, bool)` 双重排除写法）
  - 影响: `z_index: true/false`（bool 是 int 子类）通过校验进入文档，层叠顺序被当作 1/0，且与文件内既定的 bool 排除规范不一致。
  - 建议: 补 `or isinstance(..., bool)` 排除，与本文件 `_validate_track`、pages 等处写法统一。
  - 置信度: 高

- [AUD-27813] [Suggestion] pages 结构非法时额外产生误导性的"时长总和不符"二次报错
  - 位置: src/features/teaching_animation/core/validator.py:269-272（叠加 299-302）
  - 证据:
    ```python
    if not isinstance(pages, list) or not pages:
        issues.append(ValidationIssue("pages", "type", "pages must be a non-empty array"))
        pages = []
    ```
    ```python
    if total_page_frames != duration_frames:
        issues.append(ValidationIssue(
            "pages", "duration", "page durations must add up to scene duration_frames",
        ))
    ```
  - 影响: pages 为非 list/空数组时置 `pages = []`、`total_page_frames = 0`，随后必然触发第二条 "page durations must add up to scene duration_frames" 错误，把结构性错误误报为时长对账错误，干扰用户/AI 对根因的判断。单个 page 的 duration 非法时同理（该页计 0 后又报总和不符）。
  - 建议: 结构非法（非 list/空）时跳过总和对账，或将 `pages = []` 改为标志位，仅在 pages 本身合法时才做 total vs duration_frames 检查。
  - 置信度: 高（代码路径确定；单条错误消息噪音，影响低）

### 通过项
- 线程安全：无共享可变状态，`ValidationResult`/`ValidationIssue` 为 frozen + tuple
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：模块不涉及（纯 JSON 校验）
- 异常与取消路径：`except (TypeError, ValueError)`、`except ExpressionError` 均窄化；`SceneValidationError` 携带结构化 result，无宽泛 except
- 输入验证（主体）：root 类型、必填字符串、数值有限性（`_number` 显式排 bool + isfinite）、element/animation/asset/marker/cue/expression 的 id 唯一性与交叉引用、关键帧严格有序与帧区间、插值枚举、表达式安全（validate_expression）、asset 路径相对性、cue 不越界、html_layer 绑定资产存在性，覆盖全面
- 状态提交一致性：校验不修改输入（无 mutation），facade 边界（facade.py:135/173）在序列化前调用，符合契约
- 性能模式：单次线性遍历 + set 成员检查，无嵌套扫描
- 类型注解：公开函数全部标注（`template_registry: Any` 属鸭子类型约定，可后续提炼 Protocol）

## src/features/teaching_animation/core/vector_shapes.py (行数: 158)

### 发现

（无）

### 通过项
- 线程安全：纯函数、无状态、无 IO
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：模块不涉及（刻意与 Qt 解耦，见模块 docstring）
- 状态提交一致性：几何以 JSON 原生值表示，`path_geometry_from_scene_points` 输出可直接持久化的 payload（含 anchors 本地化与 min 2.0 尺寸下限）
- 异常与取消路径：`path_geometry_from_scene_points` 对 <2 锚点显式 `ValueError`；`clamp_int`/`clamp_float` 的 `except (TypeError, ValueError)` 仅用于回退默认值，语义明确非掩盖领域错误
- 输入验证：`clamp_int`/`clamp_float` 显式拒绝 bool 并回退非有限值（NaN/inf → default）；`sanitize_path_data` 白名单正则 + 32,768 长度上限，字符集不含任何可构成标记/属性的字符，防注入充分；`path_points_in_scene` 对 anchors 形状逐一验证并整体拒绝
- 性能模式：均为 O(n) 一次性构造，无热点
- 类型注解：全部函数完整标注，`__all__` 与实现一致
