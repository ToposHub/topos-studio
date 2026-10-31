# 批次报告：batch-267 — src/features/teaching_animation/ai//

编号区间：AUD-26700–AUD-26799。审查日期：2026-09-28。
本批次为纯 Python AI 编排层（Qt-free）：线程/GPU/GLSL/信号四个维度整体不适用，审查重心为
状态提交一致性（undo/redo/hash 守卫）、异常契约、输入验证与性能模式。所有跨文件结论均已对照
core/model.py、core/commands.py、core/design_tokens.py、core/executable_template.py、
core/builtin_templates.py、motion/presets.py、src/shared/keyframe.py 核实。

---

## src/features/teaching_animation/ai/brief.py (行数: 499)

### 发现

- [AUD-26700] [Suggestion] from_dict / apply_patch 对数值字段不做类型归一，畸形外部数据以 TypeError 崩溃而非校验错误
  - 位置: src/features/teaching_animation/ai/brief.py:338
  - 证据:
    ```python
    duration_seconds=data.get("duration_seconds"),
    canvas_aspect=str(data.get("canvas_aspect", CANVAS_16_9)),
    width=data.get("width"),
    height=data.get("height"),
    ```
    配合 __post_init__ (216-219 行)：
    ```python
    if self.fps <= 0:
        raise ValueError("MotionGraphicBrief.fps must be positive")
    if self.duration_seconds is not None and self.duration_seconds <= 0:
        raise ValueError("MotionGraphicBrief.duration_seconds must be positive")
    ```
  - 影响: fps 有 float() 归一，但 duration_seconds/width/height/budget_units 直接透传。持久化 JSON 中 `"duration_seconds": "30"` 会在 `self.duration_seconds <= 0` 处抛 `'<' not supported between 'str' and 'int'` 的 TypeError——是晦涩崩溃而非可读的校验错误。`call_to_action`/`source_user_text_hash` 也未归一为 str。同缺口存在于 apply_patch：_normalize_patch_values (471-472 行) 对标量原样放行，patch `{"fps": "24"}` 同样以 TypeError 失败而非干净的补丁拒绝。
  - 建议: from_dict 中对四个数值字段做 `_to_float_or_none`/`_to_int_or_none` 归一并拒绝非法类型；apply_patch 在 _normalize_patch_values 里按字段类型校验后再生效。
  - 置信度: 高

- [AUD-26701] [Suggestion] 模块级死常量 `_ALLOWED_TYPES` 无任何引用
  - 位置: src/features/teaching_animation/ai/brief.py:435
  - 证据:
    ```python
    _ALLOWED_TYPES = (str, int, float, bool, list, tuple, dict, type(None))
    ```
  - 影响: 全模块（含 `_copy_json`/`_normalize_patch_values`）无一处使用，是残留的死代码，误导读者以为有类型白名单过滤。
  - 建议: 删除；若原意是校验 patch 值类型，则应在 _normalize_patch_values 中真正落地。
  - 置信度: 高

### 通过项
- 线程安全：frozen dataclass + 每次编辑生成新对象，无共享可变状态（通过）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（Qt-free、GL-free 纯数据模型）（通过）
- 输入验证（枚举部分）：canvas_aspect/platform/language 闭集校验、fps/duration/width/height/budget 正负校验在 __post_init__ 全量执行（通过）
- 状态提交一致性：apply_patch 键白名单（376-384 行）阻止经 patch 走私 brief_id/revision；patches append-only，from_revision→to_revision 可回溯（通过）
- brief_hash 使用 sort_keys + separators 的 canonical JSON，确定性可复现，且有意排除 patches/来源哈希（通过）
- from_user_text 只留 sha256 摘要不存原文，符合“不持久化原始 prompt”约束（通过）
- 类型注解完整（通过）

---

## src/features/teaching_animation/ai/capability_context.py (行数: 342)

### 发现

- [AUD-26702] [Major] 能力清单（模型契约）与实际编译器参数名大面积漂移，清单宣称的多条命令无法被编译
  - 位置: src/features/teaching_animation/ai/capability_context.py:99
  - 证据: 清单中 apply_motion_preset 的参数声明（99-106 行）：
    ```python
    CommandDescriptor(
        "apply_motion_preset", "motion",
        (CommandParam("target", "string", required=True, description="Element id to animate."),
         CommandParam("preset_id", "string", required=True, description="A preset id from the manifest."),
         CommandParam("property", "enum", required=True, enum=("opacity", "x", "y", "scale", "rotation")),
         CommandParam("params", "string", required=False, description="Preset parameters (start, duration, ...).")),
    ```
    而实际编译器 command_catalog.py:573-581 读取的是另一组键名：
    ```python
    element_id = str(args.get("element_id", ""))
    if scene.element_by_id(element_id) is None:
        raise TeachingAIProposalError(f"Element not found: {element_id}")
    preset_id = str(args.get("preset_id", "") or "")
    ...
    param_name = str(args.get("param_name", "") or "")
    if not param_name:
        raise TeachingAIProposalError("apply_motion_preset.param_name is required")
    ```
  - 影响: 同类漂移共五处——(1) apply_motion_preset：清单 `target`/`property` vs 编译器 `element_id`/`param_name`（按清单生成的参数会以 element_id="" 命中 "Element not found: "）；(2) stagger_elements：清单 `targets`（107-113 行）vs 编译器 `args.get("element_ids")`（command_catalog.py:599）；(3) set_brand_theme：清单顶层 `theme_id`/`palette`（93-98 行）vs 编译器 `args.get("theme")` 对象（command_catalog.py:527-529）；(4) replace_asset_slot：清单 `asset_id`（119-124 行）vs 编译器 `args.get("asset")` 对象（command_catalog.py:555-557）；(5) set_beat_timing / set_transition（125-137 行）在全仓库（src/ 与 tests/）无任何编译器，EXTENDED_COMMAND_NAMES 加基础 4 命令即全部词汇表。另外 context_provider.py:34-35 广告的又是第三种清单（基础 4 + EXTENDED_COMMAND_NAMES，无 set_beat_timing/set_transition、无参数 schema），两处 prompt 面互不一致。83-86 行注释声称测试会交叉核对，但 tests/ai/test_capability_context.py:52 只校验名字唯一性与类别，参数级漂移不被捕获。
  - 建议: 以编译器实际参数名为唯一事实源生成 CommandDescriptor（或表驱动共享一份定义）；为 set_beat_timing/set_transition 补编译器或从清单删除；新增测试断言每条广告命令的参数名与编译器读取键一一对应。
  - 置信度: 高

### 通过项
- 线程安全 / GPU / GLSL·HDR / 信号防递归：不适用（纯函数清单构建）（通过）
- to_prompt_text 索引键与 executable_template.schema_for_ai 输出核对一致（executable_template.py:240-256 含 template_id/version/description/params，preset 侧由本模块 302-307 行自建键）（通过）
- 输入验证：schema_version 固定常量；capabilities 去重保序（dict.fromkeys）（通过）
- default_capability_manifest 局部导入规避循环依赖，注释说明明确（通过）
- to_dict/to_prompt_text 均为注册表纯函数、同构建可复现，可参与缓存键（通过）
- 类型注解完整；`assert preset is not None`（301 行）在当前单线程 ids()/get() 同字典实现下成立（通过，附注：python -O 下断言被剥离，若注册表未来改为动态删除需改为显式判空）

---

## src/features/teaching_animation/ai/command_catalog.py (行数: 960)

### 发现

- [AUD-26703] [Major] stagger_elements 的 `or` 默认值写法把显式 0 值静默替换为默认值（interval=0→12，duration=0→18 并绕过正值校验）
  - 位置: src/features/teaching_animation/ai/command_catalog.py:612
  - 证据:
    ```python
    interval = int(args.get("interval", 12) or 12)
    duration = int(args.get("duration", 18) or 18)
    if interval < 0:
        raise TeachingAIProposalError("stagger_elements.interval must be non-negative")
    if duration <= 0:
        raise TeachingAIProposalError("stagger_elements.duration must be positive")
    ```
  - 影响: `interval < 0` 才拒绝说明 0 是合法值（= 所有元素同时动画），但 `0 or 12` 会把模型显式给出的 interval=0 静默改成 12 帧错峰；显式 duration=0 本应命中 "must be positive" 报错，却被 `0 or 18` 洗成 18。编译结果与模型请求不符且无任何报错，渲染出的节奏错误。
  - 建议: 改为显式判 None：`raw = args.get("interval", 12); interval = int(raw) if raw is not None else 12`（duration 同理），让 0 值走后续区间校验。
  - 置信度: 高

- [AUD-26704] [Major] insert_keyframe 在已占用帧上的 undo 不恢复原关键帧，反而把该帧关键帧整个删除（undo 路径数据丢失）
  - 位置: src/features/teaching_animation/ai/command_catalog.py:770
  - 证据: apply 记录了原有 keyframe（752-753 行）：
    ```python
    existing = next((kf for kf in animation.track.keyframes if kf.frame == self.frame), None)
    object.__setattr__(self, "_existed", existing)
    ```
    undo（775-780 行）只跳过插入的那个、从不回填 `existed`：
    ```python
    existed = getattr(self, "_existed", None)
    new_track = KeyframeTrack(animation.track.param_name, min_val=animation.track.min_val, max_val=animation.track.max_val)
    for kf in animation.track.keyframes:
        if kf.frame == self.frame and kf is not existed:
            continue
        new_track.add_keyframe(kf)
    ```
  - 影响: KeyframeTrack.add_keyframe 对同帧 keyframe 是“原地替换”（src/shared/keyframe.py:143-157），因此目标帧已有 keyframe 时 apply 的效果是替换其值；undo 随后跳过新 keyframe 却不把 `existed` 加回——该帧的关键帧被彻底删除。AI 应用 insert_keyframe 再撤销后，场景比提案前基线少一个关键帧（有界数据丢失；redo 也只会重放新值，原值不可恢复）。
  - 建议: undo 中当 `_existed` 非 None 时，在遍历当前 keyframe 的同时于该帧回填 `_existed`（或直接按记录重建整条 track）。
  - 置信度: 高

- [AUD-26705] [Major] set_color_token 匹配 `var(token)`、set_typo_token 匹配 `font_token`——两种约定在全仓库无任何生产者，两条已广告命令永远失败
  - 位置: src/features/teaching_animation/ai/command_catalog.py:449
  - 证据:
    ```python
    matches = [
        element for element in scene.elements
        if isinstance(element.style, Mapping)
        and (
            element.style.get("color") == f"var({token})"
            or element.style.get("fill") == f"var({token})"
        )
    ]
    if not matches:
        raise TeachingAIProposalError(f"No elements reference color token {token!r}")
    ```
    以及 470-473 行：
    ```python
    matches = [
        element for element in scene.elements
        if isinstance(element.style, Mapping) and element.style.get("font_token") == token
    ]
    ```
  - 影响: 全 feature 范围 grep 确认 `var(` 与 `font_token` 仅出现在本文件这两处；设计令牌约定是 `$category.key`（core/design_tokens.py:21 `_TOKEN_REF = "$"`），且模板产出的 style 经 resolve_style 已解析为具体 hex/字号（core/builtin_templates.py:46-49）。两条命令均在 EXTENDED_COMMAND_NAMES 中并经 context_provider 广告给模型，但任何场景下都命中 0 个元素、必抛 "No elements reference ... token"——死词汇表只会产生失败的 AI 编辑。
  - 建议: 改为匹配真实约定（解析前 `$token` 引用、或具体 hex 值反查 token），或暂时从 EXTENDED_COMMAND_NAMES 与 prompt 广告中移除这两条命令。
  - 置信度: 高

- [AUD-26706] [Suggestion] MAX_DURATION_FRAMES 注释与实际值不符：注释称“60fps 下 10 分钟”，实值 216000 帧 = 60fps 下 60 分钟
  - 位置: src/features/teaching_animation/ai/command_catalog.py:135
  - 证据:
    ```python
    #: Maximum duration in frames (10 minutes at 60 fps is plenty for an MG clip).
    MAX_DURATION_FRAMES = 60 * 60 * 60
    ```
  - 影响: 60*60*60 = 216000 帧，60fps 下为 3600 秒（24fps 下 150 分钟），是注释声称上限的 6 倍——防失控模型的复杂度上限比设计意图宽松 6 倍。
  - 建议: 若意图是 10 分钟@60fps 应为 36000；若 216000 是新意图则修正注释（建议写成 `60 * 600` 之类可读表达式）。
  - 置信度: 高

- [AUD-26707] [Suggestion] 自定义命令类在 apply 路径抛裸 ValueError，绕开 TeachingAIProposalError 错误契约
  - 位置: src/features/teaching_animation/ai/command_catalog.py:804
  - 证据:
    ```python
    removed = next((kf for kf in animation.track.keyframes if kf.frame == self.frame), None)
    if removed is None:
        raise ValueError(f"Keyframe not found at frame {self.frame}")
    ```
    以及 882-883 行：
    ```python
    if target is None:
        raise ValueError(f"Audio reference not found: {self.audio_id}")
    ```
  - 影响: 本模块所有编译期校验统一抛 TeachingAIProposalError（宿主按提案失败统一呈现），但 remove_keyframe/remove_audio_ref/remove_marker 等在 dry_run 的 apply 阶段以裸 ValueError 逃逸；且 _compile_remove_keyframe（369-380 行）不像 _compile_remove_element（277-278 行）那样做编译期存在性预检，错误类型与时机都不一致。
  - 建议: 在各 _compile_* 中预检目标存在性并抛 TeachingAIProposalError；命令类内部异常保留为防御层即可。
  - 置信度: 高

- [AUD-26708] [Suggestion] set_background / add_audio_ref 接受任意 Mapping 直通场景文档，set_brand_theme 对非法类别静默降级为空 dict
  - 位置: src/features/teaching_animation/ai/command_catalog.py:313
  - 证据:
    ```python
    def _compile_set_background(
        self, args: Mapping[str, Any], scene: TeachingSceneDocument,
    ) -> SceneCommand:
        background = args.get("background")
        if not isinstance(background, Mapping):
            raise TeachingAIProposalError("set_background.background must be an object")
        return _SetBackgroundCommand(dict(background))
    ```
    以及 533-534 行：
    ```python
    def _cast_map(value: Any) -> Mapping[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}
    ```
  - 影响: LLM 提供的 background/audio dict 不做任何键/结构校验即写入 scene.background/metadata（错误键名静默持久化并流向渲染器）；set_brand_theme 中 palette/type_scale 非法（非 Mapping）时被静默替换为 {}，模型请求的主题被无声丢弃而非报错。违反“外部数据须验证”铁律（影响限于 AI 编辑路径，故记 Suggestion）。
  - 建议: 为 background 校验必需键（如 color）、audio 校验 audio_id；_cast_map 对非 Mapping 抛 TeachingAIProposalError。
  - 置信度: 高

### 通过项
- 状态提交一致性（守卫）：dry_run/apply 的 base_hash 校验（246-247 行）与 undo_applied 的 proposed_hash 校验（261-262 行）成对存在，拒绝过期提案与串扰撤销（通过）
- 状态提交一致性（展开平衡）：compiled_counts 记录 list 型编译结果（replace_element→remove+add、group_elements→N 次 update），undo_applied 按 sum(counts) 撤销；`proposal.compiled_counts or (1,) * len(...)`（265 行）运算优先级正确，基线提案退化为全 1（通过）
- 输入验证（结构）：所有 _compile_* 均做 isinstance 结构校验、未知命令名经 EXTENDED_COMMAND_NAMES 闭集拒绝并回落基线注册表（169-174 行）（通过）
- 性能：MAX_ELEMENTS_AFTER_EDIT=200 在 dry_run 与 dry_run_extended 双路径执行（221-225、925-929 行）；KeyframeTrack 逐 kf 重建仅在单动画内，规模有界（通过）
- _SetEasingCommand 的 undo 按记录的 previous 逐 kf 恢复且长度与 apply 时一致（706-730 行）（通过）
- _RemoveMarkerCommand/_SetBackgroundCommand 的 object.__setattr__ 记录撤销状态模式与 core/commands.py 既有命令（如 ReorderElementCommand.old_index）一致，且 dry_run 与 apply 各自重新 compile，命令实例单用途（通过）
- 冻结命令的 command_id 默认 uuid 工厂保证可哈希/可区分（通过）
- GPU / GLSL·HDR / 信号防递归 / 线程安全：不适用（无 Qt、无 GL、宿主单线程调用面；metadata["audio_refs"] 经 grep 确认无其他消费者，_AddAudioRefCommand.undo 的“删最后匹配项”无顺序副作用）（通过）
- 类型注解完整（通过）

---

## src/features/teaching_animation/ai/commands.py (行数: 123)

### 发现

- [AUD-26709] [Suggestion] set_animation 关键帧解析抛裸 KeyError/TypeError/ValueError，且帧号不做场景范围校验
  - 位置: src/features/teaching_animation/ai/commands.py:91
  - 证据:
    ```python
    track = KeyframeTrack(property_name, min_val=float(args.get("min_val", 0.0)), max_val=float(args.get("max_val", 1.0)))
    for item in keyframes:
        if not isinstance(item, Mapping):
            raise TeachingAIProposalError("keyframe must be an object")
        track.add_keyframe(Keyframe(int(item["frame"]), float(item["value"])))
    ```
  - 影响: 缺 "frame"/"value" 键的畸形 keyframe 以裸 KeyError 逃逸（未包裹为 TeachingAIProposalError）；帧号不校验 [0, scene.duration_frames)（对比扩展注册表 command_catalog.py:365-366 有校验），负帧/超界帧直接进入 track；`property` 为空串也被接受。
  - 建议: 逐项 try/except 包裹并转 TeachingAIProposalError；增加帧范围与非空 property 校验。
  - 置信度: 高

- [AUD-26710] [Suggestion] reorder_element 编译期不预检元素存在与索引范围，错误以裸 ValueError/IndexError 在 apply 阶段逃逸
  - 位置: src/features/teaching_animation/ai/commands.py:79
  - 证据:
    ```python
    if command.name == "reorder_element":
        return ReorderElementCommand(str(args.get("element_id", "")), int(args.get("index", 0)))
    ```
  - 影响: update_element 在编译期做了存在性预检（76-77 行），reorder_element 没有；元素缺失或索引越界要到 apply 时才由 TeachingSceneDocument.with_elements_reordered 抛裸 ValueError/IndexIndexError（core/model.py:691-694），错误类型与时机不符合本注册表的 TeachingAIProposalError 契约。
  - 建议: 与 update_element 对齐——编译期检查 scene.element_by_id 与 0 <= index < len(scene.elements)。
  - 置信度: 高

### 通过项
- 状态提交一致性：apply 需 confirmed=True 且校验 base_hash（108-112 行），undo_applied 校验 proposed_hash（117-119 行）；undo 历史不完整时显式报错而非静默返回（通过）
- 输入验证：未知命令名显式拒绝（closed vocabulary，93 行）；add_element/update_element/set_animation 均做 Mapping/Sequence 结构校验（通过）
- 异常路径：TeachingAIProposalError 继承 ValueError，语义清晰；dry_run 纯函数、不触碰 controller（通过）
- _hash_scene 使用 canonical JSON（ensure_ascii=False + sort_keys + separators），与扩展注册表共享同一实现（通过）
- compiled_counts 字段文档化了基线“空元组视作全 1”的约定，与扩展注册表 265 行实现一致（通过）
- 线程安全 / GPU / GLSL·HDR / 信号：不适用（纯编译层）（通过）
- 类型注解完整（通过）

---

## src/features/teaching_animation/ai/context_collector.py (行数: 319)

### 发现

- [AUD-26711] [Suggestion] collect 对宿主传入条目直接 dict() 无 Mapping 预检，数值字段透传不校验
  - 位置: src/features/teaching_animation/ai/context_collector.py:253
  - 证据:
    ```python
    subtitle_tracks=tuple(dict(item) for item in subtitle_tracks),
    audio_tracks=tuple(dict(item) for item in audio_tracks),
    ```
    以及 269 行 `available_tracks=tuple(dict(track) for track in available),` 与 278-280 行：
    ```python
    duration_seconds=data.get("duration_seconds"),
    width=data.get("width"),
    height=data.get("height"),
    ```
  - 影响: 非 Mapping 条目会让 collect 以裸 TypeError 崩溃（254/269 行）；asset 的 duration/width/height 未做数值归一，字符串值会原样进入快照并注入 LLM prompt（278-280 行）。快照仅作证据用途，影响是采集崩溃与提示词污染而非数据损坏，故记 Suggestion。
  - 建议: 循环内加 isinstance(item, Mapping) 守卫（或跳过并计数）；数值字段做 int/float 归一、失败置 None。
  - 置信度: 高

- [AUD-26712] [Suggestion] time_fn 注解为 Any，丢失可调用签名检查
  - 位置: src/features/teaching_animation/ai/context_collector.py:226
  - 证据:
    ```python
    def __init__(self, *, time_fn: Any = None) -> None:
        self._time_fn = time_fn or _default_time
    ```
  - 影响: 注入的时间函数无静态约束，传错签名只在 collect 时晚爆；与文件其余部分的严格注解风格不一致。
  - 建议: `time_fn: Callable[[], float] | None = None`（typing.Callable）。
  - 置信度: 高

### 通过项
- 状态提交一致性：fingerprint 排除 collected_at/notes（184-208 行），重新采集不产生假漂移；包含 timeline revision/asset 指纹/brand hash 等语义键，stale 检测可靠（通过）
- 输入验证（文本字段）：asset_id/label/kind/license_status 等均经 str() 归一（272-283 行）（通过）
- 性能/安全：快照不嵌入路径、原始媒体字节或 prompt 原文，仅 id/指纹/短标签，符合设计规则（通过）
- 所有 dataclass frozen，to_dict 与 fingerprint 字段一一对应（通过）
- 线程安全 / GPU / GLSL·HDR / 信号：不适用（纯数据适配层）（通过）

---

## src/features/teaching_animation/ai/context_provider.py (行数: 131)

### 发现

- [AUD-26713] [Major] 两处 `except Exception: pass` 无日志吞掉第一方注册表构建的全部异常，掩盖领域错误
  - 位置: src/features/teaching_animation/ai/context_provider.py:43
  - 证据:
    ```python
        from ..core.builtin_templates import create_builtin_template_registry
        registry = create_builtin_template_registry()
        for template in registry.list_templates():
            templates.append(registry.schema_for_ai(template.template_id))
    except Exception:  # noqa: BLE001 - registry is best-effort context, never blocks AI
        pass
    # Motion preset ids.
    motion_presets: tuple[str, ...] = ()
    try:
        from ..motion.presets import create_default_preset_registry
        motion_presets = create_default_preset_registry().ids()
    except Exception:  # noqa: BLE001
        pass
    ```
  - 影响: create_builtin_template_registry / create_default_preset_registry 均为项目内第一方代码，其中的真实 bug（而非可选依赖缺失）会被静默吞掉且零日志——AI 上下文无声丢失全部模板 schema/preset id，此后每次生成的 prompt 质量退化而无任何诊断痕迹。直接违反 .clinerules“不得宽泛 except 掩盖领域错误”铁律。
  - 建议: 仅对 ImportError/ModuleNotFoundError 静默（可选能力），其余异常必须 logger.warning/error 记录，并在返回结构中带 degraded 标记供宿主观测。
  - 置信度: 高

- [AUD-26714] [Suggestion] `_ai_capability_catalog` 的 lru_cache 使能力面在进程内永久冻结，与“注册表变化直达 prompt”的文档承诺不符
  - 位置: src/features/teaching_animation/ai/context_provider.py:22
  - 证据:
    ```python
    @lru_cache(maxsize=1)
    def _ai_capability_catalog() -> dict[str, Any]:
    ```
  - 影响: 22-52 行 docstring 及 capability_context.py 均声称“注册表变化（新模板/preset/命令）无需改码即可进入 prompt”，但 maxsize=1 的缓存首次构建后进程内永不刷新；若未来支持运行期动态注册（插件模板），prompt 永远是首帧快照。当前内置注册表为静态构建，影响为潜伏性。
  - 建议: 保留缓存但提供显式 invalidate 入口，或以注册表版本号作为缓存键。
  - 置信度: 中

### 通过项
- 输入验证：select_element 校验元素存在（105-108 行），非法选择抛 ValueError（通过）
- get_context 输出为纯 dict 快照（scene.to_dict + 引用 to_dict），不含可变引用逃逸（114-131 行）（通过）
- TeachingSceneContextProvider 的可变 setter 面（set_scene/set_references）仅在宿主线程调用，无跨线程共享证据（通过）
- GPU / GLSL·HDR / 信号防递归：不适用（通过）
- capabilities 字段与 commands 字段的“高级动词 vs 编辑命令”分层有注释说明（通过）

---

## src/features/teaching_animation/ai/creative_rubric.py (行数: 355)

（文件头已标注 DORMANT(2026-08-19)：有意休眠、无生产入口。以下发现按休眠代码定级。）

### 发现

- [AUD-26715] [Suggestion] readability/hierarchy 直接 `el.style.get(...)` 未守卫 None style，与同文件 brand 维度的守卫不一致
  - 位置: src/features/teaching_animation/ai/creative_rubric.py:174
  - 证据:
    ```python
    for el in texts:
        raw = el.style.get("font_size")
    ```
    220 行同样裸访问：`sizes.add(round(float(el.style.get("font_size", 16.0)), 1))`；而 238 行有守卫：
    ```python
    for el in scene.elements:
        style = el.style or {}
    ```
  - 影响: SceneElement.from_dict 对 `"style": null` 原样透传（core/model.py:189，_copy_json(None)→None），持久化场景含 null style 时 score_scene 在这两个维度以 AttributeError 崩溃；command_catalog.py:447-450 的 `isinstance(element.style, Mapping)` 守卫也说明该字段不可信赖。
  - 建议: 三处统一 `style = el.style or {}` 守卫（或在 SceneElement.from_dict 边界归一 `data.get("style") or {}`）。
  - 置信度: 高

- [AUD-26716] [Suggestion] brand 维度以 `$` 前缀统计 token 引用，但模板 style 在构建期已解析为具体值，该分支近乎恒不命中
  - 位置: src/features/teaching_animation/ai/creative_rubric.py:240
  - 证据:
    ```python
    for value in style.values():
        if isinstance(value, str) and value.startswith("$"):
            token_refs += 1
    ```
  - 影响: core/design_tokens.py resolve_style（141-156 行）在模板构建时把 `$palette.x`/`$type_scale.x` 解析为具体 hex/字号，仅 locked 字面量可能保留 `$` 引用；典型场景 token_refs=0，token_score 恒为 0.4，brand 维度区分度退化。休眠模块，影响潜伏。
  - 建议: 改从模板实例/token set 元数据统计 token 使用，或在注释中明确“仅统计 locked 残留引用”的语义。
  - 置信度: 中

- [AUD-26717] [Suggestion] rhythm 维度硬编码 24fps 折算事件密度，非 24fps 场景误判
  - 位置: src/features/teaching_animation/ai/creative_rubric.py:195
  - 证据:
    ```python
    duration = max(1, scene.duration_frames)
    n_animations = len(scene.animations)
    n_markers = len(scene.markers)
    # Events per second at 24fps; healthy explanatory clips sit ~0.5–3/s.
    per_second = (n_animations + n_markers) / duration * 24.0
    ```
  - 影响: brief.fps 默认 24 但用户可改（brief.py:182），60fps 场景的事件密度被低估 2.5 倍——健康的密集节奏被打成 "very dense timing"，或稀疏场景被误入健康带，阈值门（score_scene 的 passed 判定）失真。
  - 建议: 由场景/brief 取实际 fps 参与折算，24 仅作缺省。
  - 置信度: 高（硬编码事实确定；休眠模块影响潜伏）

### 通过项
- 纯函数性：六个打分器均无渲染/时钟/IO 依赖，可缓存可重放（模块 docstring 声明与实现一致）（通过）
- 权重一致性：四类权重经验算均恰好合计 1.0（105-138 行），score_scene 仍按 weight_sum 归一化防御舍入（336 行）（通过）
- 输出一致性：所有分数经 _clamp01 限制在 [0,1]，round(…,4) 序列化稳定；score_for 缺维度时抛 KeyError 而非静默（84-88 行）（通过）
- 状态提交一致性 / 异常路径：无状态、无撤销面；类别/维度为闭集枚举（通过）
- DORMANT 标注合规（1-2 行注明休眠原因与处置建议）（通过）
- 类型注解完整（通过）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-26702, AUD-26703, AUD-26704, AUD-26705, AUD-26713 |
| Suggestion | 13 | AUD-26700, AUD-26701, AUD-26706–AUD-26712, AUD-26714–AUD-26717 |

跨文件主题：AI 能力契约（manifest ↔ 编译器 ↔ prompt 广告）三方漂移是本批次最大风险源（AUD-26702/AUD-26705/AUD-26713 相关）；undo 平衡与 hash 守卫机制本身经核查健全，唯一缺口在 insert_keyframe 同帧替换语义（AUD-26704）。
