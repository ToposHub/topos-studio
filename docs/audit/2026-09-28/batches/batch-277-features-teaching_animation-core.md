# 批次报告：batch-277 [P2] src/features/teaching_animation/core//

编号区间：AUD-27700–AUD-27799。文件 8 个，共 1,483 行，全部 Read 通读。
本批次为纯模型/注册表/存储层，无 GLSL、无 OpenGL 调用、无 Qt 信号，九维审查中 GPU/GLSL·HDR/信号防递归三项均标记为不适用（n/a）。

---

## src/features/teaching_animation/core/motion_graphic_generator_registry.py (行数: 149)

### 发现

- [AUD-27701] [Suggestion] 监听器回调异常被静默吞掉且无任何日志（register 与 clear 两处）
  - 位置: src/features/teaching_animation/core/motion_graphic_generator_registry.py:59-63（同型代码 115-119）
  - 证据:
    ```python
        listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                # A palette refresh must never break template registration.
                pass
    ```
  - 影响: "注册不被面板刷新破坏"的意图合理，但 `except Exception` 连编程错误（如 AttributeError）也一并吞掉且无 logging，Effects 面板刷新失败时无任何可诊断痕迹，违反"不得宽泛 except 掩盖领域错误"铁律的精神。
  - 建议: 保留兜底但至少 `logger.exception(...)` 记录回调名；或仅捕获已知 UI 异常类型。
  - 置信度: 高

### 通过项
- 线程安全：全部读写均在 RLock 内，监听器以锁内快照 `tuple(self._listeners)` 在锁外调用，无死锁/竞争路径
- 输入验证：register 对非 HtmlTemplate 显式 TypeError
- 状态一致性：`_latest` 用 max 合并版本，clear 全量清空并通知，行为对称
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（纯数据注册表）
- 类型注解：完整

---

## src/features/teaching_animation/core/motion_graphic_template_library.py (行数: 198)

### 发现

- [AUD-27702] [Major] 第三方 manifest 的数值字段（viewport/fps/duration）未验证为正数，0/负值可注册进面板
  - 位置: src/features/teaching_animation/core/motion_graphic_template_library.py:146-151
  - 证据:
    ```python
        viewport_width=int(raw.get("viewport_width", 1920)),
        viewport_height=int(raw.get("viewport_height", 1080)),
        version=int(raw.get("version", 1)),
        fps_num=int(raw.get("fps_num", 24)),
        fps_den=int(raw.get("fps_den", 1)),
        duration_frames=int(raw.get("duration_frames", 240)),
    ```
  - 影响: `HtmlTemplate.__post_init__`（html_template.py:76-85）只校验 template_id/display_name 非空、version>0 与参数重名，viewport/fps/duration 完全不校验。`fps_den=0` 的包能通过加载并注册，拖到时间轴实例化时才在下游（如 Fraction(num, den)）以难懂的 ZeroDivisionError 失败；`duration_frames<=0` 会破坏页/帧跨度数学；`viewport=0` 仅靠下游 `or` 回退部分容忍。对明确面向第三方作者的包格式（模块 docstring 自称防御 "hostile or broken template package"），这是外部数据验证缺口。
  - 建议: 在 `load_motion_graphic_template_package` 中要求 viewport_width/height、fps_num/fps_den、duration_frames 均为正整数（`fps_den > 0`），否则抛 ValueError 让 discover 丢弃并告警。
  - 置信度: 中（缺校验为确定事实；具体崩溃点取决于实例化路径）

- [AUD-27703] [Suggestion] `_REQUIRED_METADATA` 只是默认值而非强制契约，manifest 可覆盖 render_profile / allow_inline_scripts / allow_freeform_css
  - 位置: src/features/teaching_animation/core/motion_graphic_template_library.py:31-35, 128-132
  - 证据:
    ```python
    _REQUIRED_METADATA = {
        "render_profile": "motion_graphic_freeform",
        "allow_inline_scripts": True,
        "allow_freeform_css": True,
    }
    ...
        metadata = dict(_REQUIRED_METADATA)
        raw_metadata = raw.get("metadata", {})
        if not isinstance(raw_metadata, Mapping):
            raise ValueError("metadata must be an object")
        metadata.update(dict(raw_metadata))
    ```
  - 影响: 命名为 "REQUIRED" 却随后被 `metadata.update` 整体覆盖，安全相关标志（allow_inline_scripts 等，export_service.py:149-151、html_layer_commands.py:152-154 会读取）可被包作者改成任意值；目前默认已是最宽松，覆盖只能收紧，故现实风险低，但契约语义与实现不符，未来读者若信任该键做沙箱决策会被误导。
  - 建议: 加载后显式校验这三个键与要求值一致（不一致即拒绝），或去掉 "REQUIRED" 命名改为 `_DEFAULT_METADATA`。
  - 置信度: 高（代码事实确定，影响为前瞻性）

### 通过项
- 输入验证（路径部分）：`_safe_package_file` 用 resolve 后 `resolved_root not in resolved.parents` 防目录逃逸，拒绝绝对路径/空值，且带 256KiB/4MiB/32MiB 尺寸上限
- 输入验证（结构部分）：schema_version 必须 ==1、manifest 必须为对象、preview 必须为 .gif、params_schema 逐项校验 name/type/enum
- 异常路径：discover 对 (OSError, TypeError, ValueError) 逐包告警并跳过，不中断整体扫描
- 线程安全：本模块无共享可变状态（写入由 registry 的锁保护）
- GPU / GLSL·HDR / 信号：不适用

---

## src/features/teaching_animation/core/motion_graphic_template_store.py (行数: 266)

### 发现

- [AUD-27704] [Critical] template_id 未限制字符集，`root / template_id` 可越出库根写入；overwrite=True 时更会先 rmtree 任意已存在目录
  - 位置: src/features/teaching_animation/core/motion_graphic_template_store.py:165-174
  - 证据:
    ```python
    package = load_motion_graphic_template_package(source)
    template_id = package.template.template_id or source.name
    destination = root / template_id
    if destination.exists():
        if not overwrite:
            raise TemplateStoreError(
                f"a template named {template_id!r} already exists in {root}; "
                "pass overwrite=True to replace it")
        shutil.rmtree(destination)
    _atomic_copytree(source, destination)
    ```
  - 影响: template_id 来自第三方 manifest（motion_graphic_template_library.py:141 仅 `str(...).strip()`，HtmlTemplate 只校验非空），含 `/` 或 `..` 时（如 `"../../../../.topos-color/evil"`）`destination` 逃出 user/project 库根；`_atomic_copytree` 的 `destination.parent.mkdir(parents=True)` 会创建逃逸路径，`os.rename` 把包写到库外任意新位置；配合 `overwrite=True` 还会先 `shutil.rmtree` 删除库外任意同名已存在目录（数据丢失）。`import_template_zip` 直通同一逻辑，即模块 docstring 自称防御的 "path escapes"（第 16 行 TemplateStoreError 注释）在写路径上并未设防。当前 UI 仅调用 `discover_stored_templates`（motion_graphic_editor_window.py:2469），import_* 尚无 UI 接线，现实暴露面为公共 API/测试，但该模块正是为第三方包导入设计的入口。
  - 建议: 在 `load_motion_graphic_template_package`（解析边界）校验 template_id 为安全单段名（如 `^[A-Za-z0-9._-]+$` 且非 `.`/`..`），并在 `import_template_folder` 内断言 `destination.resolve()` 位于 `root.resolve()` 之下。
  - 置信度: 高

- [AUD-27705] [Major] overwrite 导入非原子：旧包先 rmtree 后复制，复制失败即丢失原模板
  - 位置: src/features/teaching_animation/core/motion_graphic_template_store.py:168-174（与 132-145 的 `_atomic_copytree`）
  - 证据:
    ```python
        if destination.exists():
            if not overwrite:
                raise TemplateStoreError(...)
            shutil.rmtree(destination)
        _atomic_copytree(source, destination)
    ```
  - 影响: 模块 docstring（13-15 行）承诺 "written atomically (temp dir + rename) so a failed write never leaves a half package behind"，但 overwrite 路径在复制开始前就已 `rmtree` 原包；随后 copytree 因磁盘满/IO 错误失败时只回滚 tmp，旧模板已不可恢复——用户模板（可能含手绘 HTML 作品）数据丢失。
  - 建议: 先复制到 tmp、校验新包可加载后，再 `os.replace`/交换目录（或先把旧包 rename 为备份，成功后删除、失败时回滚）。
  - 置信度: 高

- [AUD-27706] [Major] zip 导入解压无总量/条目数上限，zip 炸弹可撑满磁盘
  - 位置: src/features/teaching_animation/core/motion_graphic_template_store.py:193-211
  - 证据:
    ```python
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                target = (staging / info.filename).resolve()
                if staging.resolve() not in target.parents:
                    raise TemplateStoreError(f"unsafe zip entry: {info.filename}")
            zf.extractall(staging)
    ```
  - 影响: library 中的 `_MAX_MANIFEST_BYTES/_MAX_TEMPLATE_BYTES/_MAX_PREVIEW_BYTES` 只在解压完成后的 load 阶段生效；`extractall` 本身对解压后总字节数与条目数无限制，一个压缩比极高的恶意/损坏 zip（共享模板是明示场景）可先把磁盘写满再报错。
  - 建议: 预检 `sum(info.file_size for info in zf.infolist())` 与条目数（如 ≤512 个文件、总量 ≤64MiB），超限直接 TemplateStoreError。
  - 置信度: 中（事实确定；触发需恶意或损坏的 zip）

- [AUD-27707] [Suggestion] Qt 相关调用用宽 `except Exception` 静默回退且无日志，AppData 定位失败时库位置静默分裂
  - 位置: src/features/teaching_animation/core/motion_graphic_template_store.py:51-59（另见 244-249 `open_template_directory`）
  - 证据:
    ```python
    try:
        from PyQt6.QtCore import QStandardPaths
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation)
        if base:
            return Path(base) / "motion_graphic_templates"
    except Exception:  # noqa: BLE001 — store must work without Qt (tests/CLI)
        pass
    return Path.home() / ".topos-color" / "motion_graphic_templates"
    ```
  - 影响: headless 回退有 noqa 说明、意图正当；但 GUI 进程内 QStandardPaths 异常（初始化时序问题等）也会被吞，用户模板被静默写到 `~/.topos-color`，与正常安装行为不一致且无任何日志可查。
  - 建议: except 分支加 `logger.debug/warning` 记录回退原因；`open_template_directory` 失败返回 False 前同样记录。
  - 置信度: 高（事实）/影响低

- [AUD-27708] [Suggestion] `export_template_zip` 写失败时 `.tmp` 残留，无 finally 清理
  - 位置: src/features/teaching_animation/core/motion_graphic_template_store.py:222-227
  - 证据:
    ```python
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root))
    os.replace(tmp, destination)
    ```
  - 影响: ZipFile 写入中途抛异常（磁盘满等）时 tmp 文件留在目标目录，无清理；成功路径的 `os.replace` 原子性本身是好的。
  - 建议: 包 try/except 或 finally：失败时 `tmp.unlink(missing_ok=True)` 后 re-raise。
  - 置信度: 高

### 通过项
- 输入验证：bundled 根只读保护（import 双重判断 158-161）、remove 拒绝非可写 origin（233-236）、zip 条目逐个 resolve 防绝对路径/穿越（197-199）
- 异常路径：所有失败均抛带可操作信息的 TemplateStoreError（ValueError 子类），无静默 no-op
- discover 的同 id 三级优先级（project > user > bundled）实现与 docstring 一致
- 线程安全：模块为无共享状态的自由函数；GPU/GLSL·HDR/信号不适用

---

## src/features/teaching_animation/core/pages.py (行数: 221)

### 发现

- [AUD-27709] [Suggestion] `retime_page_tracks` 中动画→元素查找为 O(animations×elements) 的线性 next 扫描
  - 位置: src/features/teaching_animation/core/pages.py:92-99
  - 证据:
    ```python
    for animation in after.animations:
        old_element = old_elements.get(animation.element_id)
        new_element = next(
            (element for element in after.elements if element.element_id == animation.element_id),
            None,
        )
    ```
  - 影响: `old_elements` 已建 dict，但 `after.elements` 每次动画都全量线性扫描；页命令是交互路径，元素/动画上千的课件场景下每次改页时长都会产生百万次级比较，违背项目 4K 实时性能基准的成本意识（同函数上一循环已有 dict 可复用/仿照）。
  - 建议: 循环前 `new_elements = {e.element_id: e for e in after.elements}`，查找改 dict 取值。
  - 置信度: 高（事实确定，量级影响随场景规模）

- [AUD-27710] [Suggestion] HTML 层计时总长超过场景时长时，末页被钳为 1 帧、页总时长超出 scene.duration_frames，页视图与时间轴不一致
  - 位置: src/features/teaching_animation/core/pages.py:189-203
  - 证据:
    ```python
    for index in range(count):
        start = starts[index]
        end = starts[index + 1] if index + 1 < count else total
        ...
        pages.append(ScenePage(
            page_id=page_id,
            name=name,
            duration_frames=max(1, end - start),
    ```
  - 影响: `starts` 来自层元数据的自定义页时长（page_timing_for 允许任意正秒数），若其总和换算的帧数 > `total`（如外部编辑把场景时长改短），末页 `total - start` 为负、被 `max(1, ...)` 钳为 1 帧，页总时长之和大于场景时长，且末页元素动画被 retime 进 1 帧范围；docstring 只声明吸收"rounding difference"，未覆盖这种越界。数据可经 undo 恢复，非丢失。
  - 建议: 检测 `starts[-1] >= total` 时按比例缩放 timing 或显式上报不一致，而非静默钳 1 帧。
  - 置信度: 中

### 通过项
- 状态提交一致性（结构）：`_retime_track` 经 from_dict 深拷贝重建、`add_keyframes_bulk` 一次排序+去重（src/shared/keyframe.py:157-176，同帧保留后写入者），重定时确定且不污染原 track
- 页 id 复用逻辑正确：新页 id 生成时全部旧页 id 已入 `used` 集合，无碰撞；缩页时孤儿元素迁移到末页并随 retime 保持局部时间
- `page_frame_ranges` 用 max(start+1, ...) 保证半开区间至少 1 帧，单一权威实现
- `resize_scene_pages` 从末页缩减且不低于 1 帧，返回场景时长与页总和保持一致
- 线程安全：纯函数式文档变换，无共享可变状态；GPU/GLSL·HDR/信号不适用；`from src.shared.keyframe import` 与 contracts.py/model.py 同款约定

---

## src/features/teaching_animation/core/professional.py (行数: 161)

### 发现

- [AUD-27711] [Suggestion] `SceneColorPipeline.from_dict` 静默把损坏的管线数据重置为 SDR 默认值，违背自身"降级须上报"的契约
  - 位置: src/features/teaching_animation/core/professional.py:44-52
  - 证据:
    ```python
        try:
            return cls(
                working_space=str(values.get("working_space", "srgb")),
                bit_depth=int(values.get("bit_depth", 8)),
                tone_map=str(values.get("tone_map", "none")),
                alpha_mode=str(values.get("alpha_mode", "straight")),
            )
        except (TypeError, ValueError):
            return cls()
    ```
  - 影响: 工程/外部数据中 bit_depth 写坏（如 `"16x"`）时无日志地回到 sRGB/8bit/none——正是类 docstring 第 12 行 "renderers must report any downgrade" 要求上报的 HDR→SDR 降级；用户察觉的只有画面变灰，无诊断线索。
  - 建议: 回退分支记录 warning（含原始值），或返回降级标记供上层提示。
  - 置信度: 高（事实确定）

- [AUD-27712] [Suggestion] `read_particle_emitters` 静默丢弃非法发射器条目，工程数据部分消失无痕迹
  - 位置: src/features/teaching_animation/core/professional.py:141-147
  - 证据:
    ```python
        for item in raw if isinstance(raw, (list, tuple)) else ():
            if not isinstance(item, Mapping):
                continue
            try:
                emitters.append(ParticleEmitter.from_dict(item))
            except (TypeError, ValueError, IndexError):
                continue
    ```
  - 影响: 反序列化旧版/手改工程时坏条目直接消失，无日志；与 AUD-27711 同属"外部数据须验证"的宽进严出问题——此处连验证结果都不可见。
  - 建议: 跳过时 logger.warning 记录 emitter_id/索引与原因。
  - 置信度: 高

### 通过项
- 输入验证（构造侧）：SceneColorPipeline 与 ParticleEmitter 的 `__post_init__` 完整校验枚举、bit_depth 集合、count 0–2000、opacity [0,1]、lifetime>0；from_dict 的 origin 长度/类型有防护
- 性能模式：`particle_positions` 确定性有界（无时钟/可变 RNG，整数 LCG 派生角度），count 上限 2000，逐帧 O(count)
- HDR 语义：`is_hdr` 判定（bit_depth>8 或 rec2020/acescg）与元数据往返 to_dict/from_dict 对称
- 线程安全：不可变 dataclass + 纯函数；GPU/GLSL·HDR 管线数值/信号不适用

---

## src/features/teaching_animation/core/style_schema.py (行数: 233)

### 发现

- [AUD-27713] [Suggestion] `resolved()` 不解析渐变 stop 颜色的 token 引用，与其余字段的解析契约不一致
  - 位置: src/features/teaching_animation/core/style_schema.py:209-211, 222-223（GradientStop 契约见 50-52）
  - 证据:
    ```python
        if self.fill is not None:
            flat["fill"] = tokens.resolve_value(self.fill)
        ...
        if self.gradient is not None:
            flat["gradient"] = self.gradient.to_dict()
    ```
    而 GradientStop 声明 `color: str  # hex or token`（51 行）。
  - 影响: fill/stroke/shadow 都经 `tokens.resolve_value`，唯独渐变 stop 颜色原样输出 `"$palette.primary"` 之类 token 串；下游 svg_renderer 的 `_color()` 会把非 hex 回退为 fallback 色，品牌主题下的渐变将呈现错误颜色（SG-06"同一 style 跨后端无惊喜"的目标落空）。当前 `resolved()` 尚无生产调用方（仅 tests/core/test_batch_a_model.py:110），属潜伏契约缺陷。
  - 建议: resolved() 中对每个 stop `replace(stop, color=tokens.resolve_value(stop.color))` 后再 to_dict，或提供 Gradient.resolved(tokens)。
  - 置信度: 高（代码事实确定；因无生产调用方，现实影响为 0，故定 Suggestion）

- [AUD-27714] [Suggestion] from_dict 家族对坏数据的策略不一致：枚举回退、gradient/stops/extras 直接抛异常
  - 位置: src/features/teaching_animation/core/style_schema.py:74-78, 155-165, 184
  - 证据:
    ```python
        def _blend(v: Any) -> BlendMode:
            try:
                return BlendMode(v)
            except ValueError:
                return BlendMode.NORMAL
    ...
        stops = tuple(GradientStop(position=float(s.get("position", 0.0)), color=str(s.get("color", "#000")))
                      for s in data.get("stops", []) if isinstance(s, Mapping))
    ```
  - 影响: 同一个 from_dict 里未知 blend_mode 静默回退 NORMAL，但 stops 少于 2 个 Mapping 时 Gradient 抛 "gradient needs at least 2 stops"（68 行）、`extras=dict(非字典)` 抛 TypeError——工程加载路径遇到坏渐变会直接失败而非降级，两种策略混用使容错行为不可预测。
  - 建议: 统一策略（推荐：结构性损坏回退 None/默认并记日志，或全部 fail-fast）。
  - 置信度: 高（事实）/影响取决于未来接入加载路径的调用方

- [AUD-27715] [Suggestion] `from_style` 的 shadow 触发键与读取键不匹配，legacy 阴影颜色恒为默认黑
  - 位置: src/features/teaching_animation/core/style_schema.py:194-198
  - 证据:
    ```python
        if "shadow" in style or "box_shadow" in style:
            shadow = Shadow(color=str(style.get("shadow_color", "#000000")),
                            blur=float(style.get("shadow_blur", 8.0)),
                            opacity=float(style.get("shadow_opacity", 0.5)))
    ```
  - 影响: 触发条件看 `"shadow"`/`"box_shadow"`，取值却读 `shadow_color/shadow_blur/shadow_opacity`；若 legacy style 形如 `{"shadow": "#333333"}`，会构造出默认 8px/50% 的纯黑阴影，用户设置的色值被静默丢弃。stroke 分支（触发与读取同用 "stroke"）无此问题。测试（test_batch_a_model.py:99-105）未覆盖 shadow 路径。
  - 建议: 补齐映射：`"shadow"` 为颜色时读入 color，`box_shadow` CSS 串至少解析颜色；或明确文档化支持的 legacy 键名。
  - 置信度: 中（legacy 键名约定未知，可能本就约定为 shadow_color 系列）

### 通过项
- 输入验证：GradientStop position [0,1]、Gradient kind 白名单+最少 2 stop、Shadow/Stroke 非负与 [0,1] 区间、StyleSchema opacity/fill_opacity 均在 `__post_init__` 强制
- to_dict/from_dict 主字段往返对称（tests 已覆盖含 blend/alpha 的往返）
- `resolved()` 对 extras 也经 resolve_style 做 token 解析且尊重 `__locked__`
- 线程安全：全部不可变 frozen dataclass；GPU/GLSL·HDR 数值/信号不适用（颜色以字符串承载，无 clamp 需求）

---

## src/features/teaching_animation/core/template_commands.py (行数: 156)

### 发现

- [AUD-27716] [Suggestion] `from .commands import SceneCommand` 为死导入，三个命令类均未继承或引用该 Protocol
  - 位置: src/features/teaching_animation/core/template_commands.py:24
  - 证据:
    ```python
    from .commands import SceneCommand
    from .design_tokens import BrandTheme, DesignTokenSet, resolve_style, validate_theme
    from .executable_template import ExecutableTemplateRegistry, TemplateBuildContext, TemplateInstance
    ```
    （文件内此后再无 `SceneCommand` 出现；commands.py:21 定义其为 Protocol，命令类仅结构性实现 command_id/label/apply/undo。）
  - 影响: 误导读者以为存在继承关系；静态检查噪音。三类命令实际满足 SceneCommand 协议（apply/undo/command_id/label 齐备），功能不受影响。
  - 建议: 删除该导入，或保留并在类上标注 `# satisfies SceneCommand`；亦可让 dataclass 显式继承以获得协议检查。
  - 置信度: 高

- [AUD-27717] [Major] `SetBrandThemeCommand.apply` 对模板场景整档重编译，静默丢弃模板应用之后的场景级编辑
  - 位置: src/features/teaching_animation/core/template_commands.py:87-96
  - 证据:
    ```python
        if isinstance(instance_data, Mapping):
            registry = self.registry or create_builtin_template_registry()
            instance = TemplateInstance.from_dict(instance_data)
            # Re-compile the same instance against the themed token set.
            themed_context = replace(instance.context, tokens=themed)
            recompiled = registry.compile(instance, themed_context)
            new_metadata = {**dict(recompiled.metadata), _TEMPLATE_INSTANCE_KEY: instance.to_dict(),
                            "brand_theme": self.theme.theme_id}
            return replace(recompiled, metadata=new_metadata)
    ```
  - 影响: `registry.compile` 返回全新 TeachingSceneDocument，返回值只回填 template_instance/brand_theme 元数据——应用模板之后用户/AI 对该场景追加的页、cues、附加 elements、color_pipeline/particle_emitters 等元数据全部被静默丢弃（AI facade 明确支持"应用模板后再加元素"的混合工作流）。undo 可恢复，但 redo/后续命令基于被裁剪的状态，属状态提交一致性缺陷。ApplyTemplateCommand 的整场景替换是"编译进场景"的预期语义，不在此列。
  - 建议: 重编译后把非模板拥有的增量（scene.pages 与 compiled.pages 的差集元素、cues、其余 metadata 键）合并回 recompiled，或文档化"模板场景不接受模板外编辑"的限制并在编辑入口阻止。
  - 置信度: 中（重编译替换文档为确定事实；是否可接受属设计判断）

### 通过项
- undo/redo 路径：三个命令的 undo 均校验 `_previous` 未设置即 RuntimeError；ReplaceAssetSlotCommand 正确记录并回滚 `_previous_asset_ref/_previous_refs/_added`
- 输入验证：ReplaceAssetSlotCommand 对缺失元素抛 ValueError 带元素 id；SetBrandThemeCommand 经 validate_theme 计算未解析 token 并通过 `unresolved_tokens` 暴露
- 命令不可变（frozen dataclass + object.__setattr__ 仅用于内部前态，单线程命令栈下安全）；无 GPU/GLSL/信号面
- 类型注解完整

---

## src/features/teaching_animation/core/template_registry.py (行数: 99)

### 发现

- [AUD-27718] [Suggestion] `SceneTemplateRegistry` 无锁保护，与同包 MotionGraphicGeneratorRegistry 的线程安全基线不一致
  - 位置: src/features/teaching_animation/core/template_registry.py:55-62
  - 证据:
    ```python
    def __init__(self) -> None:
        self._templates: dict[str, SceneTemplate] = {}

    def register(self, template: SceneTemplate, *, replace: bool = False) -> bool:
        if template.template_id in self._templates and not replace:
            return False
        self._templates[template.template_id] = template
        return True
    ```
  - 影响: 同项目的 motion_graphic_generator_registry.py:32 为同类结构配了 RLock 并说明"shared by the editor and Effects panel"；本注册表 register/get/list 并发读写 dict 在 CPython 下虽不致崩溃，但 list_templates 迭代期间 register 可能触发 RuntimeError(dict changed size)。当前生产代码无调用方（仅 tests/feature_availability 与 __init__ 导出），风险为前瞻性。
  - 建议: 若计划跨线程共享，加同款 RLock；若确定主线程专用，加注释声明单线程契约。
  - 置信度: 中

- [AUD-27719] [Suggestion] `SceneTemplate.to_dict` 无对应 `from_dict`，序列化往返不对称
  - 位置: src/features/teaching_animation/core/template_registry.py:36-49
  - 证据:
    ```python
    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "display_name": self.display_name,
            "default_parameters": dict(self.default_parameters),
            "capabilities": { ... },
        }
    ```
    （文件内无 from_dict；对比 professional.py/style_schema.py 的 dataclass 均成对提供。）
  - 影响: to_dict 输出（含嵌套 capabilities）没有反序列化入口，持久化/工程内嵌模板时只能手工重建，且 default_parameters 是可变 Mapping 字段、frozen dataclass 亦不可哈希，二次构造易踩共享引用。
  - 建议: 补 `SceneTemplate.from_dict`（含 TemplateCapabilities 重建）并在加载侧校验 version>0 等不变量。
  - 置信度: 高（事实确定，属质量改进）

### 通过项
- 输入验证：`__post_init__` 强制 template_id/display_name 非空、version>0；register 的重复 id 显式返回 False 而非静默覆盖
- 注册表语义确定性好：list_templates 按 id 排序输出；TemplateRegistry 别名与 `__all__` 导出一致
- 纯内存数据结构，无 GPU/GLSL·HDR/信号/文件 IO 面；类型注解完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-27704 |
| Major | 4 | AUD-27702, AUD-27705, AUD-27706, AUD-27717 |
| Suggestion | 14 | AUD-27701, AUD-27703, AUD-27707–27716, AUD-27718, AUD-27719 |

交叉验证说明：HtmlTemplate 校验范围（html_template.py:76-85）、KeyframeTrack.add_keyframes_bulk 排序去重（src/shared/keyframe.py:157-176）、SceneCommand Protocol（core/commands.py:21-29）、TeachingSceneDocument fps 校验（core/model.py:421-422）、svg_renderer 渐变消费（render/svg_renderer.py:258-296）与 design_tokens 解析行为均已 Read 核实；StyleSchema.resolved/from_style 与 create_default_template_registry 目前仅测试引用、无生产调用方，相应发现均已按此标注现实影响。
