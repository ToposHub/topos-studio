# batch-283 审查报告 — src/features/teaching_animation/presets/

批次：batch-283 [P2] | 编号区间：AUD-28300–AUD-28399 | 日期：2026-09-28
文件：__init__.py (42)、builders.py (209)、fragment.py (226)、i18n_fields.py (19)、library.py (372)

汇总：Critical 0 · Major 3 · Suggestion 7

---

## src/features/teaching_animation/presets/__init__.py (行数: 42)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 包门面，导出 builders、fragment 五函数、MgPresetLibrary/PresetError/PresetRecord、load_preset_scene、manifest 常量与工具、三个根路径函数、PREVIEW_WIDTH/render_preset_gif；`__all__` 与 import 一致，外加 MG_PRESET_MIME 常量。

---

## src/features/teaching_animation/presets/builders.py (行数: 209)

### 发现

- [AUD-28310] [Suggestion] 渐变工厂不校验 opacities 与 stops 数量一致，也不校验 stop position
  - 位置: src/features/teaching_animation/presets/builders.py:86-103
  - 证据:
    ```python
    def linear_gradient(*stops: tuple[float, str], angle: float = 90.0,
                        opacities: Sequence[float] | None = None) -> dict[str, Any]:
        parsed = [
            {"position": float(position), "color": color,
             "opacity": 1.0 if opacities is None else float(opacities[index])}
            for index, (position, color) in enumerate(stops)
        ]
        return {"kind": "linear", "angle": float(angle), "stops": parsed}
    ```
  - 影响: `opacities` 比 stops 短时抛裸 IndexError（深层、难定位）；position 未要求单调且在 [0,1]；stops 可为空列表。core/validator.py 不校验 gradient（grep 无 gradient/stops 检查项），坏渐变会静默进入场景并交给渲染器。
  - 建议: 入口处校验 `len(opacities or ()) == len(stops)`、stops 非空、position 单调且在 [0,1]，不满足时抛带上下文的 ValueError。
  - 置信度: 高

- [AUD-28311] [Suggestion] track() 对显式传入的倒置 min/max 边界静默改写而非报错
  - 位置: src/features/teaching_animation/presets/builders.py:60-65
  - 证据:
    ```python
    lo = min(values) if min_val is None else float(min_val)
    hi = max(values) if max_val is None else float(max_val)
    if hi <= lo:
        hi = lo + 1.0
    return KeyframeTrack(param_name=param, keyframes=list(keyframes),
                         min_val=min(lo, 0.0), max_val=max(hi, 1.0))
    ```
  - 影响: 作者误写 `min_val=1.0, max_val=0.0` 时被静默改为 [1.0, 2.0]（再被 max(hi,1.0) 扩为 [1.0,2.0]），钳制范围与意图不符且无任何提示，动画行为难排查。
  - 建议: 显式边界倒置（min_val > max_val）时抛 ValueError；仅对"由 keyframes 推导"的退化区间做 +1.0 修正。
  - 置信度: 高

### 通过项
- 线程安全：全部为无共享状态的纯工厂函数。
- GPU 资源生命周期：无 ModernGL/GL 对象。
- 输入验证：`key()` 拒绝未知插值；`track()` 校验可动画属性、keyframe 非空/有序/唯一，并自动扩展 clamp 覆盖关键帧值；`_as_tracks()` 防单轨道缺逗号陷阱。
- GLSL/HDR：不适用（无着色器代码，无 float 数据 clamp 到 [0,1] 的路径）。
- 状态提交一致性：产物经 loader 的 validate_scene 才入场景；ANIMATABLE_PROPERTIES 与 evaluator 可解析的 transform 属性集（x/y/width/height/scale_x/scale_y/rotation + opacity）一致。
- 异常路径：`scene_document` 的 duration 自适应（max_frame+1）保证关键帧落在 [0, duration) 内，符合 validator 约束（有注释说明）。

---

## src/features/teaching_animation/presets/fragment.py (行数: 226)

### 发现

- [AUD-28301] [Major] extract_fragment 只做空间原点归一化，不做时间原点归一化，保存的预设关键帧仍停留在宿主全局帧
  - 位置: src/features/teaching_animation/presets/fragment.py:195-202
  - 证据:
    ```python
    for element in kept_elements:
        transform = replace(element.transform, x=element.transform.x - min_x,
                            y=element.transform.y - min_y)
        extras = by_element.get(element.element_id)
        tracks = tuple(element.local_animation_tracks) + tuple(extras) if extras else element.local_animation_tracks
        elements.append(replace(element, transform=transform,
                                local_animation_tracks=tracks,
                                z_index=element.z_index))
    ```
    而插入路径明确假定 0-based（本文件 123-131 行 docstring："Preset tracks live in a bare 0-based frame space … keyframes must start at that page's global start frame"；`retime_fragment_to_page` 仅做 `keyframe.frame + start` 平移）。
  - 影响: 宿主中动画轨道按全局帧存储（core/pages.py:27-29 "tracks remain stored in the scene's global frame space"）。从非第 0 帧/非第 1 页的动画选区"Save as Preset"后：再次拖入时 `prepare_insert` 不移帧、`retime_fragment_to_page` 又叠加目标页起点，动画在页中段才播放或被钳到页首；同时 `fragment_max_keyframe_frame` 返回全局大帧，`_instantiate_preset_at`（motion_graphic_editor_window.py:2565-2568）据此把目标页撑长数十/数百帧。编辑器保存流程 `_save_preset_triggered` → `extract_fragment` → `save_preset` 之间无任何帧归一化（已核实）。仅当动画恰从全局帧 0 开始时碰巧正确；现有测试也只覆盖 0 起始用例（tests/ui/test_mg_asset_library.py:186-219）。
  - 建议: 在 `extract_fragment` 中把所有轨道关键帧减去片段内最小关键帧帧（按元素或按片段统一），使预设时间原点为 0，与 `retime_fragment_to_page`/`fragment_max_keyframe_frame` 的契约一致；并补一个"选区动画起始于非 0 帧"的回归测试。
  - 置信度: 高

- [AUD-28302] [Suggestion] fragment_bounds 忽略 rotation/scale，旋转/缩放元素的包围盒与实际渲染不符
  - 位置: src/features/teaching_animation/presets/fragment.py:21-33
  - 证据:
    ```python
    for element in scene.elements:
        transform = element.transform
        min_x = min(min_x, transform.x)
        min_y = min(min_y, transform.y)
        max_x = max(max_x, transform.x + transform.width)
        max_y = max(max_y, transform.y + transform.height)
    ```
  - 影响: 该 bbox 用于拖放居中（prepare_insert 的 dx/dy）。旋转 45° 或大 scale 的元素实际轴对齐包围盒大于未旋转矩形，落点中心系统性偏移，视觉上"没落在鼠标处"。
  - 建议: 对 rotation/scale 非默认值的元素用四角变换后的极值计算 bbox（可复用 core/hierarchy.py 的 Affine2d）。
  - 置信度: 高（机制确定，影响小）

- [AUD-28303] [Suggestion] 局部容器注解缺元素类型
  - 位置: src/features/teaching_animation/presets/fragment.py:54、190
  - 证据:
    ```python
    def _rebuild_track(track: KeyframeTrack, remapped_frames: list) -> KeyframeTrack:
    ```
    ```python
    by_element: dict[str, list] = {}
    ```
  - 影响: 应为 `list[Keyframe]` / `dict[str, list[KeyframeTrack]]`；弱注解削弱 IDE/类型检查对轨道数据流的校验。
  - 建议: 补齐元素类型注解（.clinerules 强类型要求）。
  - 置信度: 高

### 通过项
- 线程安全：纯函数，无共享可变状态。
- GPU 资源生命周期：无 GL 资源。
- 信号防递归：无 Qt 信号。
- 状态提交一致性：`prepare_insert` 的 id 重映射防宿主冲突（uuid 再生成循环）；层级边仅在片段内部重映射并交由 `InsertSceneFragmentCommand`（commands.py:329-370）单命令合并进 metadata，undo 一次回滚，不留半插入状态；fps 换算（scene-frame 语义）有测试覆盖。
- 异常与取消路径：`prepare_insert` 空片段抛 ValueError；`retime_fragment_to_page` 的 span 钳制有 docstring 说明且调用方先扩页；`_rebuild_track` 依赖 `add_keyframes_bulk` 的排序/同帧去重（keyframe.py:157-180，保留最后一个），塌帧安全。
- 输入验证：`extract_fragment` 过滤不存在的选中 id、空集抛 ValueError；场景级动画折叠顺序（local 在前、scene extras 在后）与 evaluator `_tracks_for` 的"场景级覆盖局部"语义一致（evaluator.py:62-67、字典推导保留最后一个）。
- GLSL/HDR：不适用。

---

## src/features/teaching_animation/presets/i18n_fields.py (行数: 19)

### 发现

（无）

### 通过项
- 通过：单一纯函数 `sanitized_i18n_mapping`，正确拒绝非 Mapping 输入、过滤空 locale/空文本并 strip；类型注解完整；无状态、无 I/O、无异常路径。
- 线程安全 / GPU 生命周期 / 信号 / 状态提交：不适用（纯工具函数）。

---

## src/features/teaching_animation/presets/library.py (行数: 372)

### 发现

- [AUD-28320] [Major] _import_folder 在 preset_id 冲突时只改文件夹名、不改写 preset.json 内的 preset_id，导致导入假失败并遮蔽同 id 记录
  - 位置: src/features/teaching_animation/presets/library.py:337-351
  - 证据:
    ```python
    preset_id = manifest.preset_id
    category = normalize_category(manifest.category)
    target = target_root / category / preset_id
    while target.exists():
        preset_id = f"{manifest.preset_id}_{uuid.uuid4().hex[:4]}"
        target = target_root / category / preset_id
    manifest = replace(manifest, preset_id=preset_id, category=category)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(folder, target)
    self.rescan()
    record = self.get(preset_id)
    if record is None:
        raise PresetError(f"imported preset failed to rescan: {preset_id}")
    ```
  - 影响: `rescan`/`_read_manifest` 以 preset.json 内的 preset_id 作为记录键（library.py:189、104-122），copytree 原样复制旧 preset.json。冲突时：① `self.get(新id)` 必为 None → 对用户报"imported preset failed to rescan"假失败，但文件已落盘；② 新文件夹内的 preset_id 与既有预设相同，rescan 后两条记录争一个键，后扫描者（同目录字典序，带后缀名更长）覆盖原记录——库面板显示的原预设被指向新导入文件夹，`delete` 该 id 会删错文件夹。代码预设（build.py 无 preset.json，id 取自文件夹名）不受影响。
  - 建议: copytree 后将 `manifest.to_dict()` 以新 preset_id 重写 target/preset.json；或统一改为"以文件夹名为准生成 preset_id"。
  - 置信度: 高

- [AUD-28321] [Major] save_preset 把未校验的 scene_data 直接落盘，解析/渲染异常被 `except Exception: pass` 吞掉；且 preset.json 先于 scene.json 写入，失败会留下"幽灵预设"
  - 位置: src/features/teaching_animation/presets/library.py:230-244
  - 证据:
    ```python
    (folder / "preset.json").write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    scene_payload = json.dumps(dict(scene_data), ensure_ascii=False, indent=2)
    if len(scene_payload.encode("utf-8")) > _MAX_SCENE_BYTES:
        shutil.rmtree(folder, ignore_errors=True)
        raise PresetError("scene.json exceeds 8 MiB")
    (folder / PRESET_ENTRY_SCENE).write_text(scene_payload, encoding="utf-8")
    try:
        scene = TeachingSceneDocument.from_dict(dict(scene_data))
        render_preset_gif(scene, folder / manifest.preview)
    except Exception:  # noqa: BLE001 — a failed preview never blocks saving
        pass
    ```
  - 影响: ① `TeachingSceneDocument.from_dict` 与场景校验只在预览 try 块里"顺带"执行，任何解析失败都被宽泛 except 吞掉（违反 .clinerules "不得宽泛 except 掩盖领域错误"），无效 scene_data 被持久化且 UI 报"保存成功"；加载时才在 loader.py:93-98 报 "preset scene failed validation"，用户看到的是保存好的预设无法插入。② 写序为 manifest→entry：scene.json 写入抛 OSError（磁盘满等）时异常外传（调用方捕获 OSError），但只剩 preset.json 的文件夹残留；`_read_manifest`（104-122）不校验 entry 文件存在，rescan 会把它收进库面板，插入时报 "missing preset entry file"。③ 除 8MiB 超限路径外无失败清理。
  - 建议: 先 `from_dict` + `validate_scene` 通过后再写盘；调整写序为 scene.json→preset.json（manifest 作为提交标记），任一步失败清理文件夹；预览失败单独 `except (PresetError, OSError, ValueError)` 并记录日志，不放行解析/校验异常。
  - 置信度: 高

- [AUD-28322] [Suggestion] 保存名称 slug 超长时触发晦涩的 preset_id 校验失败
  - 位置: src/features/teaching_animation/presets/library.py:222（配合 manifest.py:15 `_SLUG_RE = ^[a-z0-9][a-z0-9_-]{0,39}$`、manifest.py:53）
  - 证据:
    ```python
    preset_id = f"{slugify(name)}_{uuid.uuid4().hex[:6]}"
    ```
  - 影响: `slugify` 不截断长度；preset_id = slug+7，slug 超过 33 字符（即名称约 >33 个有效字符）时 `PresetManifest.__post_init__` 抛 "invalid preset_id slug: …"，用户面对与输入名称无关的报错，正常长标题（默认取 host.name）可能无法保存。
  - 建议: `slugify` 后截断到 33 字符再拼接 uuid，或在保存对话框限制/提示名称长度。
  - 置信度: 高

- [AUD-28323] [Suggestion] save_preset 在调用线程（UI 线程）同步渲染全分辨率 GIF 预览
  - 位置: src/features/teaching_animation/presets/library.py:237-241
  - 证据:
    ```python
    try:
        scene = TeachingSceneDocument.from_dict(dict(scene_data))
        render_preset_gif(scene, folder / manifest.preview)
    except Exception:  # noqa: BLE001 — a failed preview never blocks saving
        pass
    ```
  - 影响: `render_preset_gif`（preview.py）按场景原生分辨率（如 1920×1080）光栅化最多 24 帧再 LANCZOS 缩到 320px，复杂场景可阻塞 UI 线程数秒（保存入口 `_save_preset_triggered` 在 UI 槽内同步调用）。纯 CPU 路径（无 OpenGL，符合"UI 线程不得操作 GL"铁律），但违背交互性能模式。
  - 建议: 将预览渲染移到后台任务（沿用项目渲染任务队列模式），完成后异步刷新缩略图；或让 preview 直接以目标宽度渲染。
  - 置信度: 高

- [AUD-28324] [Suggestion] rescan/_read_code_info 的异常覆盖不完整，单个坏包可中断整库扫描
  - 位置: src/features/teaching_animation/presets/library.py:94-97、174-181、186-188
  - 证据:
    ```python
    try:
        value = ast.literal_eval(node.value)
    except ValueError as exc:
        raise PresetError(f"PRESET metadata must be a literal dict: {exc}") from exc
    ```
    ```python
    for category_dir in sorted(root.iterdir()):
    ...
    except PresetError as exc:
        errors.append(f"{folder}: {exc}")
    ```
  - 影响: `ast.literal_eval` 文档化可抛 TypeError（未识别节点），当前只捕 ValueError；rescan 只捕 PresetError，`root.iterdir()`/`stat()` 的 OSError（权限变化、目录竞态）同样外逃。任一异常都会从 rescan 冒出，中断本轮全部预设发现（面板刷新路径无兜底 try）。
  - 建议: `_read_code_info` 捕 `(ValueError, TypeError)`；rescan 的目录遍历与 `_read_manifest` 外层兜底 `except (PresetError, OSError)` 记入 errors。
  - 置信度: 中（TypeError 路径文档化但实际触发少见；iterdir OSError 为竞态场景）

- [AUD-28325] [Suggestion] delete/_import_folder/export_to 可能抛出 PresetError 之外的 OSError，UI 调用方只捕 PresetError
  - 位置: src/features/teaching_animation/presets/library.py:268、345-346、361-368
  - 证据:
    ```python
    shutil.rmtree(record.folder)
    self._records.pop(preset_id, None)
    ```
    ```python
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(folder, target)
    ```
  - 影响: 磁盘满/权限不足时 rmtree、copytree、mkdir、ZipFile.write 抛 OSError；asset_library_panel.py:714-719（删除）与 669-674（导入）的槽只捕 `PresetError`，OSError 成为 UI 线程未处理异常（且 delete 场景文件夹可能已部分删除而记录未弹出，状态不一致直到下次 rescan）。
  - 建议: 在库层把这些 I/O 失败包装为 PresetError（携带底层原因），或在面板槽补捕 OSError。
  - 置信度: 高（异常传播路径确定，触发依赖低磁盘/权限场景）

### 通过项
- GPU 资源生命周期：无 ModernGL/GL 对象；预览走纯 CPU 路径（preview.py docstring 与实现核实，无 WebEngine/OpenGL）。
- GLSL/HDR：不适用。
- 信号防递归：无 Qt 信号。
- 线程安全：库对象按 UI 线程单线程使用（asset_library_panel 持有）；跨线程的 project root 状态由 paths.py 内部锁保护（set_active_project_dir/active_project_dir 均 with _STATE_LOCK），refresh_roots 无条件调用安全。
- 输入验证（磁盘侧）：preset.json 64KiB 上限、build.py 仅 AST 解析不执行（_read_code_info）、场景条目 8MiB / 代码 256KiB / 预览 32MiB 上限；zip 拒绝绝对路径与 `..` 条目、总解压量 128MiB 上限（防穿越/炸弹）；`_writable_root` 用写探测确认可写并把 bundled 根在多根场景下设为只读；`delete` 拒绝删除 bundled 根内预设；`normalize_category` 统一类别 slug。
- 状态提交一致性：rescan 全量重建 records/errors（无半更新状态）；保存/删除后 `_records` 与磁盘同步维护；`import_from` 落盘后 rescan 再取记录；`export_to` 先校验 preset_id 存在再打包。
- 性能模式：`add_keyframes_bulk` 热路径优化在 _rebuild_track 中正确复用；rescan 的排序扫描规模为文件夹数，可接受。

---

## 审查方法附注

- 通读批次 5 个文件全文，并 Read 核实依赖方行为：core/model.py（fps/element_by_id/pages 归一化/with_element_removed）、core/pages.py（轨道全局帧存储与 retime）、core/validator.py（gradient 不校验、hierarchy cross_page 仅双侧可解析时触发）、core/commands.py（InsertSceneFragmentCommand 单步 undo）、render/evaluator.py（_tracks_for 的场景级覆盖局部语义）、presets/{manifest,paths,preview,loader}.py、ui/motion_graphic_editor_window.py（保存/插入流程）、ui/asset_library_panel.py（异常捕获范围）、tests/ui/test_mg_asset_library.py（既有断言仅覆盖 0 起始帧用例）。
- 未发现 Critical（崩溃/数据丢失/渲染错误）级问题；Major 三项均已在代码中核实完整因果链。
