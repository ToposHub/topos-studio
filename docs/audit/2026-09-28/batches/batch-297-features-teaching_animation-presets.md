# 批次报告：batch-297 — src/features/teaching_animation/presets/

- 审查日期：2026-09-28
- 编号区间：AUD-29700–AUD-29799
- 文件数：4（411 行），全部通读
- 依赖核查（只读）：`core/validator.py`、`core/model.py`、`presets/library.py`、`presets/i18n_fields.py`、`ui/asset_library_panel.py`、`ui/motion_graphic_editor_window.py`

---

## src/features/teaching_animation/presets/loader.py (行数: 102)

### 发现

- [AUD-29701] [Major] 全部悬空的 scene 级动画未被丢弃，与文档语义矛盾并使预设加载失败
  - 位置: src/features/teaching_animation/presets/loader.py:76
  - 证据:
    ```python
    changed = False
    for element in scene.elements:
        extras = by_element.get(element.element_id)
        if extras:
            merged = tuple(element.local_animation_tracks) + tuple(extras)
            element = replace(element, local_animation_tracks=merged)
            changed = True
        elements.append(element)
    if not changed and scene.html_layer is None:
        return scene
    return replace(scene, elements=tuple(elements), animations=(), html_layer=None)
    ```
  - 影响: 函数 docstring（62–66 行）明确宣称 "Animations pointing at missing elements are dropped here rather than failing validation later"。但当 `scene.animations` 非空且每一条都指向不存在的 element、且 `html_layer is None` 时（例如预设更新后 element_id 改名），`changed` 保持 False，第 76–77 行提前返回原 scene，`animations` 未被清空。随后 `validate_scene` 走 `core/validator.py:394-395` 的 "animation element does not exist" error，整个预设以 PresetError 拒载——本应按设计宽容丢弃动画后加载成功。
  - 建议: 提前返回条件收紧为 `if not changed and not scene.animations and scene.html_layer is None:`，或只要 `scene.animations` 非空就统一走末行 `replace(..., animations=())` 的归一化分支；并为该场景补一条单元测试。
  - 置信度: 高

- [AUD-29702] [Major] `TeachingSceneDocument.from_dict` 抛出的 ValueError/TypeError 未包装为 PresetError，逃逸出只捕获 PresetError 的调用方
  - 位置: src/features/teaching_animation/presets/loader.py:93
  - 证据:
    ```python
    scene = TeachingSceneDocument.from_dict(data)
    scene = _normalize_preset_scene(scene)
    result = validate_scene(scene)
    if not result.is_valid:
        details = "; ".join(issue.message for issue in result.errors[:4])
        raise PresetError(f"preset scene failed validation: {details}")
    ```
  - 影响: `data` 来自 `json.loads`（90 行），合法 JSON 但顶层为数组时 `core/model.py:586` 的 `dict(data)` 抛 TypeError；`width`/`fps_num` 等字段为非数字字符串时 `model.py:593-599` 的 `int()` 抛普通 ValueError，`__post_init__`（model.py:416-424）也 raise 普通 ValueError——均不是 `PresetError`。调用方 `ui/motion_graphic_editor_window.py:2540-2545` 只 `except PresetError` 做状态栏提示，畸形 scene.json 会让未处理异常直接冲进 Qt 事件循环（拖放实例化操作中断、无用户可见错误）。对比 89–92 行对 JSON 读取已做包装，唯独漏了模型构造这一步。
  - 建议: 将 `TeachingSceneDocument.from_dict(data)` 包进 `try/except (TypeError, ValueError)` 并 `raise PresetError(...) from exc`，与 `_load_code_scene` 43–44 行的模块边界处理保持一致。
  - 置信度: 高

- [AUD-29703] [Suggestion] `by_element` 使用裸 `list` 泛型注解
  - 位置: src/features/teaching_animation/presets/loader.py:64
  - 证据:
    ```python
    by_element: dict[str, list] = {}
    for animation in scene.animations:
        by_element.setdefault(animation.element_id, []).append(animation.track)
    ```
  - 影响: 收集的是 keyframe track 对象，裸 `list` 丢失元素类型信息，与库内其余完备注解风格不一致，IDE/类型检查收益归零。
  - 建议: 改为 `dict[str, list[KeyframeTrack]]`（从 `..core.model` 导入对应轨道类型）。
  - 置信度: 高

### 通过项
- 异常与取消路径（build.py 执行、JSON 读取、校验失败均有 PresetError 包装；`sys.modules` 在 finally 中清理，`except Exception` 带 BLE001 理由注释）
- 输入验证（`entry.is_file()` 存在性检查、代码大小上限 `_MAX_CODE_BYTES`、`build()` 返回类型三重校验、validate_scene 终检）
- 线程安全（无共享可变状态，纯函数式加载）
- GPU 资源生命周期（无 OpenGL/ModernGL 资源）
- GLSL/HDR 正确性（不涉及）
- 信号防递归（无 Qt 信号）
- 状态提交一致性（`_normalize_preset_scene` 将动画折叠进元素本地轨道，支撑"插入即单条 undo 命令"的设计意图，除 AUD-29701 边界外逻辑正确）
- 性能模式（一次性遍历 O(n)，无重复计算）
- 代码执行信任模型与 `.tglsl` 插件一致且有大小上限（文档已声明，非缺陷）

---

## src/features/teaching_animation/presets/manifest.py (行数: 116)

### 发现

- [AUD-29704] [Major] `tags`/`schema_version` 类型错误时抛出的 ValueError/TypeError 逃逸 PresetError，预设扫描崩溃
  - 位置: src/features/teaching_animation/presets/manifest.py:86
  - 证据:
    ```python
    tags=tuple(data.get("tags", ())),
    entry=str(data.get("entry", PRESET_ENTRY_SCENE)),
    preview=str(data.get("preview", PRESET_PREVIEW_NAME)),
    schema_version=int(data.get("schema_version", PRESET_MANIFEST_SCHEMA_VERSION)),
    ```
  - 影响: `PresetManifest.from_dict` 对外部 JSON 做了 `str()` 宽容转换，但 `tuple()` 和 `int()` 是硬转换：`"tags": 5` → `tuple(5)` TypeError；`"schema_version": "v2"` 或 `null` → ValueError/TypeError。这些都不是 `PresetError`，而唯一消费方 `presets/library.py:186-187` 的扫描循环只 `except PresetError` 后 `errors.append(...)` 跳过坏预设——一个畸形 preset.json 会让 `rescan()` 及 `MgPresetLibrary.__init__`（library.py:138）整体失败，整个资产面板无法打开，违背扫描"容错跳过、收集 errors"的设计意图（铁律：外部数据须验证；不得让领域错误以意外类型逃逸）。
  - 建议: `from_dict` 内对 `tags` 先做 `isinstance(x, (list, tuple))` 检查、对 `schema_version` 用 try/except 包裹，非法值抛 `PresetError`；或在 `__post_init__` 中统一捕获并转 PresetError。
  - 置信度: 高

- [AUD-29705] [Major] `preview` 字段无任何校验，路径遍历/绝对路径可将 GIF 写到预设库外并覆盖任意文件
  - 位置: src/features/teaching_animation/presets/manifest.py:45
  - 证据:
    ```python
    entry: str = PRESET_ENTRY_SCENE
    preview: str = PRESET_PREVIEW_NAME
    ...
    # __post_init__ 仅校验 preset_id/name/entry/category/tags，preview 不在校验之列
    if self.entry not in _PRESET_ENTRIES:
        raise PresetError(f"unsupported preset entry: {self.entry!r}")
    ```
  - 影响: `entry` 有 `_PRESET_ENTRIES` 白名单（57–58 行），`preview` 却接受任意字符串。消费链：`ui/asset_library_panel.py:734` `render_preset_gif(scene, record.folder / record.manifest.preview)` → `presets/preview.py:48` `out_path.parent.mkdir(parents=True, exist_ok=True)` → `preview.py:86` `frames[0].save(out_path, ...)` 覆盖写。当手工编辑或外部导入的 preset.json 含 `"preview": "../../something.gif"` 或绝对路径时，`Path(folder) / "/abs"` 即为该绝对路径，会在库目录外创建目录树并覆盖已存在文件（数据破坏）。`library.py:60-62` 的 `preview_path()` 读取同样受影响。
  - 建议: 在 `__post_init__` 中将 `preview` 校验为纯文件名（如复用 `_SLUG_RE` 加 `.gif` 后缀的白名单，拒绝路径分隔符与 `..`），非法值抛 PresetError，与 `entry` 同等对待。
  - 置信度: 中（代码路径确定；实际触发需畸形/恶意 preset.json 进入任一扫描根，本地信任模型降低概率）

- [AUD-29706] [Suggestion] `schema_version` 无上界检查，未来版本的 manifest 被静默按 v1 解析
  - 位置: src/features/teaching_animation/presets/manifest.py:89
  - 证据:
    ```python
    schema_version=int(data.get("schema_version", PRESET_MANIFEST_SCHEMA_VERSION)),
    ```
  - 影响: 场景 schema 有对应防线（`core/validator.py:252-253` 对更新版本报 "scene schema is newer than supported"），manifest 却对 `schema_version: 99` 照单全收，新版本字段语义被 v1 逻辑静默丢弃，问题被推迟到运行期。
  - 建议: `__post_init__` 中加 `if self.schema_version > PRESET_MANIFEST_SCHEMA_VERSION: raise PresetError(...)`（或至少记入 errors 供 UI 提示）。
  - 置信度: 高

- [AUD-29707] [Suggestion] `tags` 为字符串时被 `tuple()` 拆成单字符标签（静默垃圾数据）
  - 位置: src/features/teaching_animation/presets/manifest.py:86
  - 证据:
    ```python
    tags=tuple(data.get("tags", ())),
    ...
    object.__setattr__(self, "tags", tuple(str(tag).strip() for tag in self.tags if str(tag).strip()))
    ```
  - 影响: `"tags": "intro"` 不抛异常，被拆为 `('i','n','t','r','o')` 五个单字符标签，进入搜索索引（`library.py:68-70` 的 `matches`）和 UI 显示，产出不可理解的垃圾数据而非报错。与 AUD-29704 的崩溃路径互补，同属输入验证缺口。
  - 建议: `from_dict` 中检测 `isinstance(data.get("tags"), str)` 时按空格切分或直接抛 PresetError，禁止整串字符串流入 `tuple()`。
  - 置信度: 高

### 通过项
- 线程安全（frozen dataclass、无共享可变状态；`localized_*` 每次重建 dict 的开销可忽略）
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归（不涉及）
- 状态提交一致性（纯值对象，`to_dict`/`from_dict` 往返字段完备）
- `preset_id`/`category` 的 slug 白名单校验（`_SLUG_RE`）与 `entry` 白名单严格有效
- i18n 字段经 `sanitized_i18n_mapping`（presets/i18n_fields.py）容错归一化，非 Mapping 输入返回空 dict，无逃逸
- 类型注解完备（除 Suggestion 所列外）
- 循环导入规避（`_i18n_pairs`/`localized_*` 的函数内延迟导入合理）

---

## src/features/teaching_animation/presets/paths.py (行数: 101)

### 发现

（无）

### 通过项
- 线程安全：`_ACTIVE_PROJECT_DIR` 全局可变状态由 `_STATE_LOCK` 互斥保护，读（`active_project_dir`）写（`set_active_project_dir`）两侧均持锁，返回路径对象不可变
- 输入验证：`set_active_project_dir` 对 None/空白字符串统一归一为清空语义，`expanduser().resolve()` 处理得当
- 根合并逻辑（`default_preset_roots`）去重正确，bundled→user→project 的优先级顺序与 docstring 一致
- `fps`/除法等数值陷阱不涉及；`user_preset_root` 的 `TOPOS_MG_PRESET_ROOT` 覆盖与三平台分支正确（darwin/win/其他）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性（不涉及，纯路径解析无副作用、不创建目录）
- 异常路径：所有函数对不存在路径均返回描述性 Path/None 而非抛异常（创建延迟到保存时，符合 docstring 契约）
- 类型注解完备

---

## src/features/teaching_animation/presets/preview.py (行数: 92)

### 发现

- [AUD-29708] [Major] 全尺寸 CPU 渲染 + GIF 编码为同步 API，被 UI 线程直接调用，4K 场景可冻结界面数秒以上
  - 位置: src/features/teaching_animation/presets/preview.py:36
  - 证据:
    ```python
    def render_preset_gif(scene: TeachingSceneDocument, out_path: Path | str, *,
                          width: int = PREVIEW_WIDTH,
                          max_frames: int = PREVIEW_MAX_FRAMES) -> Path:
        """Sample the scene across its duration and write a looping GIF."""
        ...
        for index in _frame_indices(scene.duration_frames, max_frames):
            rendered = renderer.render_frame(AnimationFrame(index, fps))
            svg_renderer = QSvgRenderer(QByteArray(rendered.data))
            image = QImage(scene.width, scene.height, QImage.Format.Format_RGBA8888)
            ...
            pil = pil.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)
    ```
  - 影响: 两个调用方都在 UI 线程同步执行：`ui/asset_library_panel.py:732-734`（`_regenerate_preview` 是 QWidget 槽函数）和 `presets/library.py:237-240`（`save_preset` 保存流程）。每帧要完成 SVG 生成 → QSvgRenderer 全场景尺寸光栅化（4K RGBA 单帧约 33 MB）→ stride 拷贝 → LANCZOS 缩放，共最多 24 帧，再 `optimize=True` 编码 GIF。4K/长时长场景下 UI 无响应可达数秒到数十秒，无进度反馈、无取消途径，违背项目实时性能基准与"渲染走独立渲染线程/任务队列"的架构约定（虽为纯 CPU 路径，但阻塞主线程的后果等同）。
  - 建议: 将预览生成移到后台线程/任务队列（QThread 或现有 renderer 任务设施），完成后经信号回 UI 刷新缩略图；`save_preset` 场景可先落盘 manifest/scene 再异步补预览。至少应在文档标注"可能长耗时，勿在 UI 线程调用"并增加进度/取消参数。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：纯 CPU 路径（QSvgRenderer/QImage，无 OpenGL），`renderer.cleanup()` 置于 finally（80–81 行）保证释放
- 线程安全（函数无共享状态；QImage 绘制允许在非主线程，本问题仅是阻塞，见 AUD-29708）
- GLSL·HDR 正确性（不涉及着色器；RGBA 直通无 clamp，背景合成走 alpha 蒙版正确）
- 信号防递归（无 Qt 信号）
- 状态提交一致性（无 undo/持久化状态；输出为磁盘文件，失败路径 `load_preset_scene` 校验前置）
- 异常路径：无帧时抛 ValueError（82–83 行）；调用方均以 `except Exception` 呈现/吞掉预览失败（asset_library_panel.py:735、library.py:240），预览失败不阻塞主流程
- 输入验证：`fps_den or 1` 防除零（49 行）；`width/height/fps/duration` 的正数约束由上游 `load_preset_scene`→`validate_scene`（width/height/fps_num/fps_den/duration_frames 均要求正整数）保证，当前两条调用链不可达零值；`_frame_indices` 的 `max(1, ...)` 与去重排序正确
- 性能模式（除 AUD-29708 外）：帧数上限 24、缩放目标 320 宽、行对齐 stride 防御处理（69–72 行）、sip voidptr→asstring 转换惯用法与 FrameAdapter 一致
- 类型注解完备（返回 `Path`、关键字参数均有注解）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-29701, AUD-29702, AUD-29704, AUD-29705, AUD-29708 |
| Suggestion | 3 | AUD-29703, AUD-29706, AUD-29707 |

paths.py 无发现，整体质量最好；loader.py 与 manifest.py 的共性问题是对外部 JSON 的类型宽容转换存在漏洞，使普通 ValueError/TypeError 绕过 PresetError 契约逃逸到只捕获 PresetError 的 UI 调用方。
