# batch-159 审查报告 — src/features/edit/core/captions/

批次：batch-159 [P1] 主题 captions
编号区间：AUD-15900–AUD-15999
审查日期：2026-09-28
文件数：7（2,542 行）

跨文件验证说明：本批为纯数据模型/解析/CPU 光栅化委托层，无 UI 控件与 OpenGL 调用。
关键结论均对照过外部依赖实现（`subtitle_renderer.py` 的 `_warp_rgba_to_canvas` /
`get_subtitle_texture_cached`、`timeline_v2.py` 的 `SubtitleClip`、preview/export 两个调用点），
mux 结论在本机 ffmpeg（Lavf 63.1.102）上实测复现。

---

## src/features/edit/core/captions/graphics.py (行数: 860)

### 发现

- [AUD-15900] [Major] `cue_animation_envelope` 把 X/Y 两个轴向的位移合并成单一标量返回，违反自身文档契约
  - 位置: src/features/edit/core/captions/graphics.py:836-860
  - 证据:
    ```python
    elif in_animation == "slide_up":
        opacity = t
        translate_y = -(1.0 - t)
    elif in_animation == "slide_left":
        opacity = t
        translate_x = -(1.0 - t)
    ...
    if out_animation == "slide_up":
        opacity = min(opacity, t)
        translate_y = -(1.0 - t)
    ...
    return (opacity, translate_y + translate_x, scale)
    ```
  - 影响: docstring 声明返回 `(opacity, translate_y_ratio, scale)`，但入场与出场动画可各自独立指定：`in="slide_left"` + `out="slide_up"` 时 translate_x 与 translate_y 被相加成一个数，消费端无法区分轴向，字幕会被错误地单轴偏移。另外 slide_up 的进入方向（负位移=向上）与同文件规范实现 `caption_graphic_animation_state` 的 Y-down 惯例（slide_up 从下方进入，正位移）相反。当前生产代码无调用方（仅 tests/edit/test_title_templates.py 使用，测试只测单轴），属带病 API，一旦接线即错。
  - 建议: 返回 `(opacity, translate_x, translate_y, scale)` 四元组（或直接复用 `CaptionGraphicAnimationState`），并对齐 slide 方向与 `caption_graphic_animation_state` 的 Y-down 惯例；在接线前补充 in/out 异轴组合的测试。
  - 置信度: 高（轴向合并与方向分歧均为代码直接可见；生产影响目前为零）

- [AUD-15901] [Suggestion] `template_parameters` 不按 `published_parameters` 过滤 overrides，任意键静默接受
  - 位置: src/features/edit/core/captions/graphics.py:402-405
  - 证据:
    ```python
    def template_parameters(template: CaptionGraphicTemplate, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = dict(template.defaults)
        values.update(_copy_dict(overrides))
        return values
    ```
  - 影响: 拼写错误或已被模板废弃的参数键（如 `"in_frame"`）会被静默写入 `binding.parameters` 并随工程序列化，动画代码不读取它们，用户看不到任何提示，参数面板与实际渲染悄然脱节。
  - 建议: 对 overrides 键做白名单过滤（`template.published_parameters`），或至少对未知键记日志/返回诊断。
  - 置信度: 高

- [AUD-15902] [Suggestion] `render_caption_graphic_rgba` 跨模块导入 `subtitle_renderer` 的私有下划线函数
  - 位置: src/features/edit/core/captions/graphics.py:693-697
  - 证据:
    ```python
    from src.features.edit.core.subtitle_renderer import (
        _resolved_value,
        _warp_rgba_to_canvas,
        get_subtitle_texture_cached,
    )
    ```
  - 影响: `_resolved_value` / `_warp_rgba_to_canvas` 是渲染器内部契约，重命名或签名调整会在字幕图形预览/导出路径静默炸开（ImportError 在函数首次调用时才触发）。
  - 建议: 在 `subtitle_renderer` 中将这两个函数提升为公开 API（去下划线 + `__all__`），或提供专门的图形字幕渲染入口。
  - 置信度: 高

- [AUD-15903] [Suggestion] ease-out 在同文件重复实现两次，且残留一个"注释字符串"伪常量
  - 位置: src/features/edit/core/captions/graphics.py:430-432, 792-797
  - 证据:
    ```python
    _EASE_OUT = "(1-(1-t)^3) 的闭式在下方内联实现"


    def _ease_out(t: float) -> float:
        t = max(0.0, min(1.0, float(t)))
        return 1.0 - (1.0 - t) ** 3
    ```
    （与第 430 行 `_ease_out_cubic` 逐字等价）
  - 影响: 两份相同缓动实现 + 一个内容是注释的模块级字符串，增加维护漂移风险与阅读困惑。
  - 建议: 删除 `_EASE_OUT` 字符串常量，`cue_animation_envelope` 改用 `_ease_out_cubic`。
  - 置信度: 高

- [AUD-15904] [Suggestion] `renderable_graphic_for_cue` / `animation_opacity` 无生产调用方，且 track-style 解析逻辑与导出路径内联实现重复
  - 位置: src/features/edit/core/captions/graphics.py:543-572
  - 证据:
    ```python
    track_style = next((item for item in timeline.caption_track_graphic_styles
                        if item.track_id == cue.track_id and item.enabled), None)
    if track_style is None:
        return None
    template = timeline.get_caption_graphic_template_registry().get(
        track_style.template_id, track_style.template_version
    )
    if template is None:
        return None
    ```
    而 timeline_export.py:6445-6462 自行实现同一段解析并额外处理 `fallback_to_native`。
  - 影响: 全库 grep 仅 tests/edit/test_caption_graphics.py 引用本函数；规范解析器与导出内联版并存，`fallback_to_native` 语义已在两处分叉，后续修改容易只改一边。
  - 建议: 让导出/预览统一走 `renderable_graphic_for_cue`（并把 fallback 语义收进来），或删除该兼容入口。
  - 置信度: 高（grep 证据）

### 通过项
- 线程安全：本模块为纯函数/纯数据，无共享可变状态；GPU/CPU 光栅化经 `render_caption_graphic_rgba` 委托且 docstring 明确要求调用方保持在渲染线程（已核对 preview/export 调用点均在渲染 worker 内）
- GPU 资源生命周期：无 ModernGL/GL 对象；reveal<1.0 时先 `texture.rgba.copy()` 再清零列，不会污染全局字形缓存（已核对 `_warp_rgba_to_canvas` 返回新分配 canvas，不修改输入数组）
- GLSL·HDR 正确性：动画仅乘 alpha（straight-alpha 已在 subtitle_renderer.py:330 注释确认），RGB/HDR 值全程无 clamp，符合铁律
- 输入验证：`CaptionGraphicBinding`/`GraphicClip` 的 `__post_init__` 对 id、display_mode、materialization、时间范围 fail-fast；未知 effect id 安全回退为静态字幕（docstring 声明的前向兼容策略）
- 性能模式：复用紧凑字形纹理缓存，动画成本与字幕尺寸而非画布面积成正比（注释与实现一致）；`out_progress` 对未出场帧被 clamp 后不参与计算
- 状态提交一致性：绑定用 frozen dataclass + `_replace` 不可变更新；`clone_caption_graphic_binding` 生成新 id 并落 source_snapshot，符合 detached 复制语义
- `build_composite_input` 依赖的 `caption_metadata`/`style_override` 字段在 `SubtitleClip` 上有默认值（timeline_v2.py:1354-1355），不会 AttributeError

---

## src/features/edit/core/captions/importer.py (行数: 342)

### 发现

- [AUD-15905] [Suggestion] 编码回退链终止于 gb18030，非白名单编码的字幕文件可能"成功"解码为乱码而非报错
  - 位置: src/features/edit/core/captions/importer.py:277-287
  - 证据:
    ```python
    def _read_subtitle_content(source: Path) -> str:
        content = None
        for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
            try:
                content = source.read_text(encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        if content is None:
            raise UnicodeError(f"Unable to decode subtitle file: {source}")
        return content
    ```
  - 影响: gb18030 对大量字节序列兼容，Big5/Shift-JIS 等文件常能无异常解码为乱码汉字；无 BOM 的 utf-16 分支对偶数长度纯 ASCII 文件也可能产出乱码。结果是整轨乱码字幕被静默导入，用户只看到"成功但内容坏了"。
  - 建议: 对解码结果做启发式质检（可打印字符比例/替换字符计数），可疑时提示用户选择编码，而不是直接采用第一个"不抛异常"的结果。
  - 置信度: 中（机制确定，触发面取决于真实文件编码分布）

- [AUD-15906] [Suggestion] `_parse_ttml`/`_ttml_text` 递归遍历无深度上限，病态嵌套 XML 触发 RecursionError
  - 位置: src/features/edit/core/captions/importer.py:197-243
  - 证据:
    ```python
    def _ttml_text(element: ET.Element) -> str:
        parts: list[str] = []
        if element.text:
            parts.append(element.text)
        for child in element:
            if _xml_local_name(child.tag) == "br":
                parts.append("\n")
            else:
                parts.append(_ttml_text(child))
    ...
        for child in element:
            visit(child, begin_ms)
    ```
  - 影响: 外部字幕文件（不可信输入）嵌套深度超过解释器递归上限（默认约 1000）时抛 RecursionError 而非带上下文的 ValueError，异常类型逃逸出解析器契约。
  - 建议: 给 `visit`/`_ttml_text` 加 depth 参数并在超限（如 200）时抛 `ValueError("TTML nesting too deep")`；或改为显式栈迭代。
  - 置信度: 中

### 通过项
- 输入验证：扩展名白名单（`SUPPORTED_SUBTITLE_EXTENSIONS`）；SRT/VTT/ASS 时间戳严格正则 + 值域校验（分钟<60、秒<60）；非法 TTML/XML 抛 ValueError 且带原因链；不支持扩展抛 ValueError；`_fps_fraction` 对非法/零 fps 兜底 24
- 异常与取消路径：解析为纯函数无资源持有；坏 cue 单条跳过不中断整批（`_cue_from_timestamps` 返回 None 过滤零长/空文本）
- 性能模式：单遍行扫描 + 预编译正则；`_milliseconds_to_frame` 用整数运算避免浮点累积极误差
- `partition_subtitle_cues` 正确性：按开始时间排序后 first-fit 区间划分经推导为最优（新开组当且仅当所有组最后一条 cue 在新 cue 起点仍活跃，此时冲突深度≥组数+1），docstring 的"fewest"声明成立
- 状态提交一致性：解析结果为 frozen `ParsedSubtitleCue`，排序键 (start_frame, text) 确定性稳定

---

## src/features/edit/core/captions/model.py (行数: 711)

### 发现

- [AUD-15907] [Suggestion] `style_to_full_legacy_fields` 不传递 `base`，按其宣称的 dual-write 用途接线会破坏 legacy 扁平字段的真值
  - 位置: src/features/edit/core/captions/model.py:689-699
  - 证据:
    ```python
    def style_to_full_legacy_fields(
        track_default: CaptionStyle,
        cue_override: Optional[CaptionStyle] = None,
    ) -> dict[str, Any]:
        """Resolve to a *complete* flat dict ...
        This is what the dual-write serializer uses when emitting the legacy flat
        block: ...
        """
        return resolve_style(track_default, cue_override)
    ```
  - 影响: `resolve_style` 的解析顺序是 cue_override → track_default → `base[f]` → `_LEGACY_DEFAULTS`；本函数不接收 `base`，对样式存在扁平字段、`style_override=None` 的 legacy cue（resolve_style docstring 明确警告的场景），结果会用硬编码默认值（如 fill_r=1.0）覆盖 cue 的真实扁平值。当前 src 内无调用方（仅 tests/edit/test_caption_data_model.py），是带隐患的死代码。
  - 建议: 增加 `base: Optional[dict] = None` 参数透传给 `resolve_style`，或删除该入口并让序列化方直接调用 `resolve_style(track, override, base=clip_style_base(clip))`。
  - 置信度: 高（机制确定；现无生产影响）

### 通过项
- 状态提交一致性：全模块 frozen dataclass + `_dc_replace`/`with_*` 不可变更新，符合项目不可变数据惯例；`bumped_revision`/`with_revision` 提供审校版本递增原语
- 输入验证：`_opt` 对枚举字段 fail-fast（weight/halign/valign/pivot_mode）；`CaptionWordTiming` 校验时间与 confidence 区间；`source_span` 结构/数值双校验；`CaptionTrackMetadata` 校验 role/safe_anchor/default_style 类型——外部数据在模型边界即报错，符合铁律
- 未知键保留（SUB-P2-10）：`CaptionCueMetadata.extra` round-trip 逐字保留；`to_dict` 不回写与已知键冲突的 extra 项
- GLSL·HDR 正确性：`resolve_style` 对 fill_* 等 HDR 浮点全程透传，无 clamp
- `STYLE_FIELDS`/`_LEGACY_DEFAULTS` 带 34 字段数与键集合断言，防止漂移
- 模块底部对 graphics 的再导出（model.py:705-711）与 graphics 内的函数级延迟导入构成受控延迟循环，导入顺序两个方向均已验证无环

---

## src/features/edit/core/captions/mux.py (行数: 42)

### 发现

- [AUD-15908] [Major] container="mkv" 生成 `-f mkv`，ffmpeg 没有名为 mkv 的 muxer，命令必然失败
  - 位置: src/features/edit/core/captions/mux.py:24-39
  - 证据:
    ```python
    normalized = str(container).lower().lstrip(".")
    if normalized not in {"mov", "mp4", "mxf", "mkv"}:
        raise ValueError(f"Unsupported caption container: {container!r}")
    ...
    codec = "mov_text" if normalized in {"mov", "mp4"} else "srt"
    return [
        str(ffmpeg_bin), "-y", "-i", str(video), "-i", str(subtitles),
        ...
        "-f", normalized, str(output),
    ]
    ```
  - 影响: 实测（本机 ffmpeg Lavf 63.1.102）：`ffmpeg ... -f mkv -` 报 `Requested output format 'mkv' is not known.` 并以退出码 234 失败。白名单放行 mkv 但产出的命令 100% 无法执行，MKV 交付路径形同虚设；现有测试只覆盖 mov 分支（tests/edit/test_caption_delivery_extensions.py:83）故未暴露。
  - 建议: 将 mkv 映射到 muxer 名 `matroska`（`-f matroska`，已实测可正常封装 srt 字幕）。
  - 置信度: 高（已实测复现）

- [AUD-15909] [Major] container="mxf" 搭配 `-c:s srt`，ffmpeg 的 mxf muxer 不支持 srt 字幕流，命令在写头阶段失败
  - 位置: src/features/edit/core/captions/mux.py:32-38
  - 证据:
    ```python
    codec = "mov_text" if normalized in {"mov", "mp4"} else "srt"
    return [
        ...
        "-c:v", "copy", "-c:a", "copy", "-c:s", codec,
        "-metadata:s:s:0", f"language={str(language).strip()}",
        "-f", normalized, str(output),
    ]
    ```
  - 影响: 实测：`ffmpeg ... -c:s srt -f mxf -` 报 `track 2: could not find essence container ul, codec not currently supported in container`，退出码 255。mxf muxer 的字幕支持是 eia_608 隐闭 captions（`-h muxer=mxf` 显示 Default subtitle codec: eia_608），srt 文本字幕无法封装；docstring 自己也含糊承认（"when the installed ffmpeg build supports it"），但函数仍对 mxf 放行并产出必败命令，MXF 交付在运行期才失败。
  - 建议: 对 mxf 要么显式 raise ValueError（不支持文本字幕 sidecar），要么改走 `-c:s eia_608`/商业插件路径，并在注释中写明限制。
  - 置信度: 高（已实测复现）

- [AUD-15910] [Suggestion] 无条件 `-y` 且不校验输出路径与输入视频相同，存在覆盖源视频的数据丢失路径
  - 位置: src/features/edit/core/captions/mux.py:33-39
  - 证据:
    ```python
    return [
        str(ffmpeg_bin), "-y", "-i", str(video), "-i", str(subtitles),
        ...
        "-f", normalized, str(output),
    ]
    ```
  - 影响: 若调用方误传 `output_path == video_path`，`-y` 会在读取中途截断源视频文件，造成不可恢复的源素材损失；函数对路径关系不做任何防御。
  - 建议: 解析三路径的绝对路径并 assert/raise `output != video` 且 `output != subtitles`；`-y` 可保留但应由该 guard 兜底。
  - 置信度: 中（需要调用方传错路径才触发，但防御成本极低）

### 通过项
- 安全：argv-list 构造、无 shell 拼接，含空格/特殊字符路径与字幕文本不会注入 shell（有测试覆盖）
- 输入验证：容器白名单、language 非空校验
- 类型注解完整；模块职责单一（构造与执行分离，进度/进程归调用方）

---

## src/features/edit/core/captions/quality.py (行数: 177)

### 发现

- [AUD-15911] [Suggestion] `_reading_units` 的 CJK 范围漏掉假名、谚文、CJK 扩展 B 与全角字符，日/韩字幕阅读速度被显著低估
  - 位置: src/features/edit/core/captions/quality.py:65-69
  - 证据:
    ```python
    def _reading_units(text: str) -> int:
        """Count CJK glyphs and Latin/number words as readable units."""
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        non_cjk = re.sub(r"[\u3400-\u9fff]", " ", text)
        return cjk + len(re.findall(r"[^\W_]+", non_cjk, flags=re.UNICODE))
    ```
  - 影响: 平假名/片假名（\u3040-\u30ff）与谚文（\uAC00-\uD7AF）不在 CJK 类，一整串 kana 会落入 `[^\W_]+` 被计为 1 个"单词"；日/韩字幕的 units_per_second 远低于真实值，"reading_speed" 警告基本失效（漏报）。
  - 建议: 扩展范围为 `\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af` 等（或按码位区间分段统计），全角字符同样按 1 unit 计。
  - 置信度: 高（Unicode 区间事实）

### 通过项
- 正确性验证：overlap 检测的"仅与 running-max-end cue 比较"算法经推导正确（start ≥ 当前最大 end 则与所有前条都不重叠；start < 最大 end 则必与该条重叠，归因的 cue 也确为真实重叠对象），半开区间端点相接不算重叠的语义与时间线模型一致
- 输入验证：track_type 校验、fps 有限正数校验（含 Fraction→float）、行数/每秒字数参数正数校验
- 异常与取消路径：纯函数无状态；invalid_timing/out_of_range/empty_text 报 error 并跳过后续派生检查，逻辑短路合理
- 性能模式：单次排序 + 线性扫描；warning 计数惰性属性
- 类型注解完整；frozen dataclass 输出可序列化（to_dict）

---

## src/features/edit/core/captions/render_plan.py (行数: 147)

### 发现

- [AUD-15912] [Suggestion] `from_clip` 与 `pivot_texture_local` 各自重复实现相同的 `_value` 解析闭包
  - 位置: src/features/edit/core/captions/render_plan.py:55-58, 120-123
  - 证据:
    ```python
    def _value(name: str, default: Any) -> Any:
        if style is not None and style.get(name) is not None:
            return style[name]
        return getattr(clip, name, default)
    ```
    （在 pivot_texture_local 与 from_clip 中逐字重复）
  - 影响: 本模块宗旨即"transform 解析只写一次"，但值解析仍在文件内重复两份；后续若调整 style→clip 回退规则容易只改一处。
  - 建议: 提取为模块级 `_resolve(clip, style, name, default)` 供两处共用。
  - 置信度: 高

### 通过项
- 数学正确性验证：`build_affine_ydown` 的锚点项 m02/m12 代入 (lx,ly)=(px,py) 得 (px+tx, py+ty)，pivot 在 scale+rotate 下保持不动点，公式正确；与 `_warp_rgba_to_canvas` 的 forward/inverse 变换推导一致（含 det=sx·sy 的逆变换与 safe_sx/safe_sy 防零除衔接）
- 线程安全：纯几何函数、无状态
- GLSL·HDR：模块不触碰颜色，premultiplied/straight 契约显式声明为调用方责任
- 性能模式：仅计算 2×3 矩阵，无逐像素工作
- 类型注解完整

---

## src/features/edit/core/captions/schema.py (行数: 263)

### 发现

- [AUD-15913] [Suggestion] `migrate_subtitle_clip_dict` 不拒绝负数版本号，负索引会静默命中迁移表错误条目；迁移表类型标注含 None 占位
  - 位置: src/features/edit/core/captions/schema.py:88-94, 118-135
  - 证据:
    ```python
    _CLIP_MIGRATIONS: list[tuple[int, Any]] = [
        (2, _clip_v0_to_v2),   # index 0: v0 → v2
        None,                  # index 1: v1 → v2 placeholder (filled below)
        (2, _clip_v1_to_v2),   # index 2: v2 is current (no-op marker)
    ]
    ...
    if from_version is None:
        from_version = int(data.get(CAPTION_SCHEMA_VERSION_KEY, 0) or 0)
    ...
        entry = (_CLIP_MIGRATIONS[version]
                 if version < len(_CLIP_MIGRATIONS) else None)
    ```
  - 影响: 工程文件中 `caption_schema_version` 为负数（外部数据、无下界校验）时，`from_version > CAPTION_SCHEMA_VERSION` 不触发，`_CLIP_MIGRATIONS[-1]` 经 Python 负索引取到表尾条目并"迁移成功"。当前各条目只盖章版本号故无实害，但表一旦增长（出现真正改写数据的 v3 迁移），负版本会走错迁移链。另：类型标注声明 `list[tuple[int, Any]]` 却含 None 占位；index 2 条目在 `while version < 2` 循环中不可达，注释与用途不符。
  - 建议: 在读取 from_version 后校验 `0 <= from_version`（否则 raise ValueError 指明损坏数据）；迁移表用 dict[int, tuple] 显式注册，消除 None 占位与不可达标记。
  - 置信度: 高（负索引机制确定；当前影响为零）

### 通过项
- 状态提交一致性：v0/v1→v2 迁移仅盖章不改数据，幂等且无损；`migrate_track_caption_dict` 对每条 clip 先 `dict(c)` 拷贝再迁移，避免共享字典别名问题；`upgrade_captions_in_project` 同时兼容 timeline_sequences/timelines 两种布局且双跑安全（幂等）
- 异常与取消路径：超前版本（>2）显式 raise ValueError 提示"由更新版本保存"，符合不静默降级原则；迁移链缺项时盖章止损防死循环
- 输入验证：非 dict 轨道/序列条目逐层 isinstance 过滤，损坏结构不崩溃
- 未知键保留：`preserve_unknown_keys` 不覆盖 target 已有键，方向正确
- 纯 dict 操作、无 IO/线程/GPU 维度问题

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-15900, AUD-15908, AUD-15909 |
| Suggestion | 10 | AUD-15901–15907, AUD-15910–15913 |

无 Critical 发现。本批为纯数据/解析/CPU 委托层，不涉及 UI 线程、OpenGL 资源与信号递归维度；三个 Major 中两个为 mux 命令构造缺陷（已在本机 ffmpeg 实测复现），一个为动画包络 API 的轴向合并缺陷（暂无生产调用方）。
