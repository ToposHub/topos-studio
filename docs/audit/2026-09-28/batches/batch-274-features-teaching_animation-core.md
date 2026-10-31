# 批次报告：batch-274 [P2] src/features/teaching_animation/core//

区间：AUD-27400–AUD-27499。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 /
状态提交一致性（undo·redo·revision·持久化）/ 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

---

## src/features/teaching_animation/core/commands.py (行数: 1,242)

### 发现

- [AUD-27400] [Critical] RemoveElementCommand 的 undo 不恢复随元素一起被删除的动画轨，撤销后数据永久丢失
  - 位置: src/features/teaching_animation/core/commands.py:46
  - 证据:
    ```python
    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        return scene.with_element_added(self.removed_element, self.removed_index)
    ```
    交叉验证 src/features/teaching_animation/core/model.py:667-672：
    ```python
    def with_element_removed(self, element_id: str) -> "TeachingSceneDocument":
        if self.element_by_id(element_id) is None:
            raise ValueError(f"Element not found: {element_id}")
        elements = tuple(element for element in self.elements if element.element_id != element_id)
        animations = tuple(animation for animation in scene.animations if animation.element_id != element_id)
    ```
  - 影响: `with_element_removed` 会同时删除该元素的所有 `SceneAnimation`（按 element_id 过滤），而
    undo 只调用 `with_element_added` 重新插回元素。用户删除元素后撤销，元素回来了但其全部关键帧
    动画轨永久丢失且不可恢复（apply 前的不可变场景已无人引用）。调用方均为单独执行该命令、无补偿
    逻辑（ui/editor_controller.py、ui/motion_graphic_editor_window.py:1763、ai/command_catalog.py:279）。
    项目还专门为动画可撤销写了 RemoveAnimationCommand（捕获 removed），此处遗漏与之矛盾。
  - 建议: apply 时同时捕获 `tuple(a for a in scene.animations if a.element_id == self.element_id)`，
    undo 在 `with_element_added` 后把捕获的动画逐条 `with_animation_added` 恢复。
  - 置信度: 高

- [AUD-27401] [Major] DetachComponentInstanceCommand 把来自持久化 metadata 的 override 键未经白名单校验直接传给 dataclasses.replace
  - 位置: src/features/teaching_animation/core/commands.py:813
  - 证据:
    ```python
    update = overrides.get(element.element_id, {})
    if isinstance(update, Mapping):
        from copy import deepcopy
        patch = {key: deepcopy(value) for key, value in update.items()}
        if "style" in patch and isinstance(patch["style"], Mapping):
            patch["style"] = {**dict(element.style), **dict(patch["style"])}
        element = replace(element, **patch)
    ```
  - 影响: overrides 源自 `read_component_instances(scene.metadata)`，即磁盘上的场景文件/外部数据，
    键内容不受本模块控制。patch 键未对照 `_ELEMENT_FIELDS` 白名单（UpdateElementCommand 第 106-108
    行有此校验）：键为 `element_id` 时会静默改写元素身份、破坏与 hierarchy/component 的关联；未知
    键则抛出难以定位的 `TypeError: __init__() got an unexpected keyword argument`，detach 中途失败。
    违反"外部数据须验证"铁律。
  - 建议: 在 replace 前执行与 UpdateElementCommand 相同的 `set(patch) - _ELEMENT_FIELDS` 检查，
    并显式拒绝 `element_id` 键。
  - 置信度: 高

- [AUD-27402] [Major] UpdateComponentMasterCommand 与 SetComponentOverrideCommand 的更新键缺少字段白名单，与 UpdateElementCommand 校验纪律不一致
  - 位置: src/features/teaching_animation/core/commands.py:1061
  - 证据:
    ```python
    by_id[source_id] = replace(by_id[source_id], **{
        key: _normalize_element_update(key, value) for key, value in update.items()
    })
    ```
    以及 SetComponentOverrideCommand（commands.py:1136）：
    ```python
    overrides[self.element_id] = {**overrides.get(self.element_id, {}), **dict(self.updates)}
    ```
  - 影响: 两处 updates 均直接进入 `replace()` 或写入持久化 overrides，键未做 `_ELEMENT_FIELDS`
    白名单校验（UpdateElementCommand:106-108 有）。错误键名导致 TypeError 崩溃；`element_id`/
    `page_id` 等合法字段名则静默改写元素身份；写入 overrides 的脏键随后经 AUD-27401 的 detach
    路径放大。传播分支（commands.py:1078-1081）同样未校验。
  - 建议: 抽取共享的 `validate_element_update_keys(updates)` 帮助函数，在三个写路径统一执行白名单校验。
  - 置信度: 高

- [AUD-27403] [Suggestion] UpdateAnimationTrackCommand.undo 在目标动画不存在时静默无操作
  - 位置: src/features/teaching_animation/core/commands.py:682
  - 证据:
    ```python
    animations = tuple(
        replace(item, track=self._previous) if item.animation_id == self.animation_id else item
        for item in scene.animations
    )
    return replace(scene, animations=animations)
    ```
  - 影响: apply 在动画缺失时抛 `ValueError`（commands.py:676），undo 却在不匹配任何动画时返回
    原场景、不报错——调用方认为撤销成功但什么都没恢复。异常路径不对称，掩盖状态不一致。
  - 建议: undo 中若没有 `animation_id == self.animation_id` 的项，抛出与 apply 对称的 ValueError。
  - 置信度: 中

- [AUD-27404] [Suggestion] RenameMarkerCommand 对空白 label 静默沿用旧名
  - 位置: src/features/teaching_animation/core/commands.py:768
  - 证据:
    ```python
    renamed = replace(marker, label=str(self.label_text).strip() or marker.label)
    ```
  - 影响: `label_text` 为空白时命令"成功"执行并入栈，但 marker 名字未变；撤销栈出现一个空操作条目，
    调用方无法区分成功与忽略。
  - 建议: 空白时抛 ValueError（或至少在 label 上注明 no-op 语义），由 UI 层提前拦截空输入。
  - 置信度: 高

- [AUD-27405] [Suggestion] DetachComponentInstanceCommand 硬编码在 frame 0 求值烘焙，与"保持用户所见"的语义有偏差
  - 位置: src/features/teaching_animation/core/commands.py:809
  - 证据:
    ```python
    from ..render.evaluator import evaluate  # local import avoids a cycle at module load
    evaluated = {item.source.element_id: item for item in evaluate(scene, 0).elements}
    ```
  - 影响: 求值固定在 frame 0；若用户当前编辑帧不是 0（动画/布局在中途帧状态不同），detach 烘焙出的
    transform/style 与用户当前所见不一致。对 frame 0 处于入场动画起始偏移的元素尤其明显。
  - 建议: 让命令接收当前编辑帧（或从控制器传入），在 detach 时以当前帧求值。
  - 置信度: 中

- [AUD-27406] [Suggestion] 多个命令持有调用方 Mapping 的引用或仅做浅拷贝，redo 语义可被外部突变破坏
  - 位置: src/features/teaching_animation/core/commands.py:855
  - 证据:
    ```python
    object.__setattr__(self, "_previous", scene.background)
    return replace(scene, background=dict(self.background))
    ```
    另见 InsertSceneFragmentCommand.parents（commands.py:339 直接持有 `field(default_factory=dict)`
    传入的映射）、SetSceneMetadataCommand（commands.py:881 `dict(self.metadata)` 浅拷贝）。
  - 影响: 命令对象是 frozen 但其 Mapping 字段内容可变；调用方在 execute 后修改传入 dict，redo 时
    行为与首次 apply 不同，破坏 undo/redo 决定性。
  - 建议: 构造或 apply 时对 mapping 字段做 deepcopy（文件内已有 `_normalize_element_update` 的
    deepcopy 先例），统一不可变约定。
  - 置信度: 高

- [AUD-27407] [Suggestion] MoveElementsToPageCommand.visit 递归遍历层级，深嵌套可触发 RecursionError
  - 位置: src/features/teaching_animation/core/commands.py:550
  - 证据:
    ```python
    def visit(element_id: str) -> None:
        if element_id in seen:
            return
        seen.add(element_id)
        ordered.append(element_id)
        for child_id in hierarchy.children_of(element_id):
            visit(child_id)
    ```
  - 影响: 层级可经 SetParentCommand 任意堆叠（同页约束不限制深度），接近默认递归上限（约 1000 层）
    时 apply 抛 RecursionError，移动操作不可用。
  - 建议: 改为显式栈的迭代遍历。
  - 置信度: 中

### 通过项
- 线程安全：命令栈为单线程封闭设计（ui/editor_controller.py 仅 UI 线程持有），场景文档不可变、
  apply/undo 全程返回新对象，无跨线程共享可变状态。
- GPU 资源生命周期：纯数据命令层，不触碰 ModernGL/OpenGL，无 GPU 资源创建或释放路径。
- GLSL·HDR 正确性：无着色器代码；float 字面量均为显式 .0 形式（constraints/model 侧）。
- 信号防递归：无 Qt 信号，控制器解耦在上层完成。
- undo/redo 一致性（除上述发现外）：绝大多数命令在 apply 捕获前态、undo 精确还原；
  SceneCommandStack 的 redo 清空（execute 时 `_redo_stack.clear()`）、max_size 截断行为正确；
  CompositeCommand 应用顺序/逆序撤销正确，子命令中途抛错时因文档不可变而不会留下半应用状态。
- 异常与取消路径：未应用即 undo 均抛 RuntimeError；元素/页面/组件/变体缺失时抛带 id 的 ValueError，
  无宽泛 except。
- 性能模式：局部导入避免循环依赖；命令操作均为 O(n) 线性扫描，规模可控。

---

## src/features/teaching_animation/core/components.py (行数: 257)

### 发现

- [AUD-27408] [Suggestion] ComponentMaster.variants 的嵌套结构在 from_dict/解析路径均未验证，畸形数据在 resolve_component_updates 抛 AttributeError
  - 位置: src/features/teaching_animation/core/components.py:226
  - 证据:
    ```python
    variant = master.variants.get(variant_id, {})
    update = variant.get(element_id)
    ...
    update = master.variants.get(variant_id, {}).get(master.element_ids[index])
    ```
    from_dict 仅校验顶层（components.py:51-52）：`variants = raw_variants if isinstance(raw_variants, Mapping) else {}`。
  - 影响: variants 的第二层（变体 id → 值）与第三层（element_id → 更新）未验证是否为 Mapping。
    持久化文件中 `variants: {"v1": "broken"}` 能正常加载，渲染/解析时 `"broken".get(...)` 抛
    AttributeError，整帧求值中断——外部数据未验证即消费。
  - 建议: from_dict 时规范化嵌套结构（丢弃非 Mapping 层），或 resolve 处加 isinstance 判断。
  - 置信度: 中

- [AUD-27409] [Suggestion] ComponentInstance 未校验 element_ids 非空/唯一，与 ComponentMaster 校验纪律不一致
  - 位置: src/features/teaching_animation/core/components.py:69
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if not self.instance_id or not self.component_id:
            raise ValueError("instance_id and component_id cannot be empty")
    ```
    对照 ComponentMaster（components.py:36-38）同时校验 `element_ids` 非空且唯一。
  - 影响: 空或含重复 id 的实例可通过构造进入 metadata；`resolve_component_updates` 中
    `instance.element_ids.index(element_id)` 在重复 id 时取首个，覆盖语义悄悄错位。
  - 建议: 与 Master 相同，校验 element_ids 非空且无重复。
  - 置信度: 高

### 通过项
- 线程安全：纯函数 + frozen dataclass，无共享可变状态。
- GPU 资源生命周期：不涉及。
- GLSL·HDR 正确性：不涉及。
- 信号防递归：不涉及（无 Qt）。
- 状态提交一致性：读/写 helpers 均返回新 dict，不修改入参 metadata；`write_component_state_into`
  未显式传 active_variants 时保留现有值，往返保存语义稳定。
- 异常与取消路径：`read_brand_themes` 式容错在此文件为跳过非 Mapping 项（components.py:148/154），
  无宽泛 except。
- 性能模式：`_json` deepcopy 用于序列化边界，量级小；`resolve_responsive_updates` 排序键确定性
  （含 variant_id 兜底）设计正确。
- 类型注解：完整。

---

## src/features/teaching_animation/core/composition_schema.py (行数: 186)

### 发现

- [AUD-27410] [Suggestion] Mask/Matte from_dict 将未知 kind/mode 静默降级为 alpha，掩盖领域错误并改变渲染语义
  - 位置: src/features/teaching_animation/core/composition_schema.py:106
  - 证据:
    ```python
    try:
        kind = MaskKind(data.get("kind", "alpha"))
    except ValueError:
        kind = MaskKind.ALPHA
    ```
    TrackMatte.from_dict 同型（composition_schema.py:136-139，降级为 MatteMode.ALPHA）。
  - 影响: 旧版本读取新版本文件（或手工编辑出错）时，一个本应为 vector/luma 的遮罩被静默当作
    alpha 渲染——输出画面错误但无任何诊断；正是"宽泛 except 掩盖领域错误"要避免的静默语义漂移。
  - 建议: 至少记录诊断（返回值或日志），或保留原始字符串待渲染端按能力协商降级。
  - 置信度: 高

- [AUD-27411] [Suggestion] read_masks/read_mattes 对单条畸形数据 fail-fast，与同目录容错读取模式不一致
  - 位置: src/features/teaching_animation/core/composition_schema.py:158
  - 证据:
    ```python
    def read_masks(metadata: Mapping[str, Any]) -> tuple[Mask, ...]:
        raw = metadata.get(_MASKS_KEY, []) if isinstance(metadata, Mapping) else []
        return tuple(Mask.from_dict(item) for item in raw if isinstance(item, Mapping))
    ```
    而 Mask.__post_init__（composition_schema.py:92-95）对 feather<0 / opacity 越界抛 ValueError；
    对照 constraints.read_breakpoints（constraints.py:193-196）逐条 try/except 跳过。
  - 影响: 持久化 metadata 中一条 feather=-1 的 mask 会让整个 read_masks 抛 ValueError，整场景
    合成配置不可读（预览/导出全挂），而同库其他读取器对同类问题选择跳过单条——行为不一致，
    一条脏数据放大为全场景失败。
  - 建议: 统一策略：要么逐条捕获跳过（记录诊断），要么全库 fail-fast；不要两种混用。
  - 置信度: 高

### 通过项
- 线程安全：纯数据类型 + 纯函数。
- GPU 资源生命周期：不涉及（path_ref 由渲染端解析）。
- GLSL·HDR 正确性：不涉及；feather/opacity 数值均为显式 .0 字面量。
- 信号防递归：不涉及。
- 状态提交一致性：write_composition_into 返回新 dict、三键独立写回，往返稳定；命令层
  （SetCompositionCommand）已做 target/source 存在性校验。
- 异常与取消路径：无宽泛 except（除 AUD-27410 所述定向降级）。
- 输入验证（除上述发现外）：SceneInstance/Mask/TrackMatte 的 __post_init__ 校验非空、非负、
  opacity∈[0,1] 完整。
- 类型注解：完整。

---

## src/features/teaching_animation/core/constraints.py (行数: 239)

### 发现

- [AUD-27412] [Major] keep_aspect 在仅一轴为 scale 时另一轴不随统一系数缩放，元素仍被拉伸，与模块自述"never stretch"矛盾
  - 位置: src/features/teaching_animation/core/constraints.py:116
  - 证据:
    ```python
    if constraints.keep_aspect and ("scale" in (constraints.horizontal, constraints.vertical)):
        uniform = min(sx, sy)
        sx = uniform
        sy = uniform
    ```
    随后垂直轴非 scale 分支（constraints.py:134-139）：
    ```python
    y, height = transform.y, transform.height
    if constraints.vertical == "scale":
        y, height = transform.y * sy, transform.height * sy
    else:
        delta = target_height - base_height
    ```
  - 影响: keep_aspect 仅统一了 sx/sy 两个系数，但只有 "scale" 模式的轴真正乘系数。例如
    horizontal="scale"、vertical="top"、keep_aspect=True，base 1920x1080 → target 1080x1920：
    uniform=0.5625，宽×0.5625 而高不变，400x400 变 225x400——纵横比被破坏。模块 docstring 明言
    "logos and portraits never stretch"，UI（ui/scene_inspector.py:192-199）又允许任意组合
    keep_aspect 与各轴模式，用户勾选"Keep Aspect"后仍会得到拉伸结果，预览/导出一致地错。
  - 建议: keep_aspect 生效时，若任一轴为 scale 则两轴的几何（x/width 与 y/height）都按 uniform
    缩放后再套用非 scale 轴的 pin 规则；或明确限制 keep_aspect 仅在双轴 scale 下可用并在 UI 联动。
  - 置信度: 中

### 通过项
- 线程安全：纯函数。
- GPU 资源生命周期：不涉及。
- GLSL·HDR 正确性：不涉及；浮点字面量全部显式 .0。
- 信号防递归：不涉及。
- 状态提交一致性：读/写 helpers 返回新 dict；SetCanvasSizeCommand 首次变更记录 base canvas 的
  逻辑与本模块 BASE_CANVAS_KEY 约定一致。
- 异常与取消路径：read_base_canvas 捕获 (KeyError, TypeError, ValueError) 为定向收窄，可接受。
- 输入验证（除上述发现外）：CanvasConstraints/Breakpoint 的 __post_init__ 校验完整；
  from_dict 对非法约束值回退默认值（遗留元素语义）有注释说明。
- 性能模式：O(1) 几何计算，evaluator 仅在 canvas 尺寸偏离 base 时逐元素调用，有早退条件。
- 类型注解：完整。

---

## src/features/teaching_animation/core/content_localization.py (行数: 67)

### 发现

（无）

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：纯字符串解析 helper，均不涉及。
- 状态提交一致性：无状态函数。
- 异常与取消路径：无 except；缺失翻译按 locale → en_US → base 的顺序降级，单条缺失只影响该条
  （与模块 docstring 声明一致）。
- 输入验证：sanitized_i18n_dict 对键值类型、空白均过滤；localized_text 对非 str 值跳过。
- 性能模式：O(1)；`locale == "en_US"` 时候选重复检查一次，无实际开销。
- 类型注解：完整（参数为 `Any` 的 template 访问器用 getattr 防御，属合理鸭子类型）。

---

## src/features/teaching_animation/core/contracts.py (行数: 526)

### 发现

- [AUD-27413] [Suggestion] RenderedFrame 不校验 data 字节长度与 width/height/pixel_format 的一致性
  - 位置: src/features/teaching_animation/core/contracts.py:133
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if self.frame < 0:
            raise ValueError("frame must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("rendered frame dimensions must be positive")
        if not self.pixel_format:
            raise ValueError("pixel_format cannot be empty")
        if not isinstance(self.data, bytes):
            raise TypeError("rendered frame data must be bytes")
    ```
  - 影响: 尺寸错误/截断的字节缓冲可以通过构造校验，错误延迟到导出/预览适配器解码时才爆，定位困难；
    契约层本应把"帧数据自洽"作为不变量。
  - 建议: 对已知格式（如 "rgba8"）按 width*height*channels 校验 len(data)，未知格式至少留出
    显式注册表。
  - 置信度: 高

- [AUD-27414] [Suggestion] RenderPlan.fail_policy 为自由字符串，注释声明的合法值未在构造时校验
  - 位置: src/features/teaching_animation/core/contracts.py:476
  - 证据:
    ```python
    fail_policy: str = "preview_placeholder"     # preview_placeholder | export_block
    ```
  - 影响: 拼写错误（如 "export_block " 或 "block"）静默落入默认分支或被消费端当成未知策略处理；
    同文件 ProxyTier/AlphaMode/ColorSpace 均用 Enum+校验，此处纪律不一致。
  - 建议: 定义 FailPolicy(str, Enum) 或在 __post_init__ 中校验取值集合。
  - 置信度: 高

### 通过项
- 线程安全：纯 frozen 契约类型；SceneRendererV2 的 docstring 明确 GPU/浏览器资源在 prepare/render/
  close 契约内创建与释放、"never on the UI thread"，符合渲染线程铁律。
- GPU 资源生命周期：RendererSession.close 由 owning renderer 恰好关闭一次的契约已写明（本文件为
  协议层，无实现可查）。
- GLSL·HDR 正确性：bit_depth 允许 8/10/12/16/32、working_color_space 枚举校验、无 HDR clamp，
  契约层面正确。
- 信号防递归：不涉及。
- 状态提交一致性：revision/render_profile_hash 进入 FrameContext.to_dict，缓存键确定性有支撑；
  fps 用 fps_num/fps_den 有理数表示避免浮点帧身份漂移，设计正确。
- 异常与取消路径：SceneRendererV2.render 带 cancel_token 参数，取消契约在位；RenderError 结构化
  错误码完整（cancelled/invalid_scene/missing_asset/...）。
- 输入验证（除上述发现外）：RenderContext/AnimationFrame/CanvasSpace/RenderSurface/FrameContext
  的 __post_init__ 校验覆盖 fps/尺寸/dpr/枚举/safe-area 可见性。
- 性能模式：_copy_json 仅在序列化边界；from_dict 无热路径。
- 类型注解：完整（个别 # type: ignore[arg-type] 有注释原因）。

---

## src/features/teaching_animation/core/design_tokens.py (行数: 293)

### 发现

- [AUD-27415] [Major] DesignTokenSet.resolve_value 递归解析无环检测且类别经 getattr 任意取属性，畸形 token 引用可致 RecursionError/TypeError
  - 位置: src/features/teaching_animation/core/design_tokens.py:39
  - 证据:
    ```python
    def category(self, name: str) -> Mapping[str, Any]:
        return getattr(self, name) if hasattr(self, name) else {}

    def resolve_value(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith(_TOKEN_REF):
            ref = value[1:]
            category, _, key = ref.partition(".")
            cat = self.category(category) if category else {}
            if key in cat:
                return self.resolve_value(cat[key])
    ```
  - 影响: token 数据来自场景 metadata 与品牌主题包（外部数据）。(1) `palette={"a": "$palette.b",
    "b": "$palette.a"}` 构成引用环，resolve_value 无限递归直接 RecursionError——resolve_style 在
    求值/渲染路径被调用（render/evaluator.py:276、render/scene_renderer_router.py:259 经
    read_scene_tokens 读取含环数据），一条脏 token 使渲染/主题切换整体崩溃。(2) `category()`
    用 getattr 暴露任意实例属性：`"$to_dict.x"` 使 cat 成为方法对象，`key in cat` 抛
    TypeError。违反"外部数据须验证"。
  - 建议: 解析时维护 visited 集合检测环（或限制递归深度）；类别查找改为显式白名单
    {"palette","type_scale","spacing","radius","shadow","motion"}。
  - 置信度: 高

- [AUD-27416] [Suggestion] DesignTokenSet.from_dict 对 safe_area 非序列值未防御，一条畸形数据使 read_scene_tokens 抛 TypeError
  - 位置: src/features/teaching_animation/core/design_tokens.py:99
  - 证据:
    ```python
    safe = data.get("safe_area") or (0.0, 0.0, 0.0, 0.0)
    return cls(
        ...
        safe_area=tuple(float(v) for v in safe[:4]),
    )
    ```
  - 影响: `safe_area: 40`（标量）时 `40[:4]` 抛 TypeError；read_scene_tokens（design_tokens.py:192）
    调用 from_dict 无 try/except，UI（motion_graphic_editor_window.py:885 等）与渲染路由
    （scene_renderer_router.py:259）整条路径失败。同文件 read_brand_themes 对每主题 try/except
    (TypeError, ValueError)（design_tokens.py:239-240），此处不一致。
  - 建议: from_dict 中对 safe_area 做 isinstance(sequence) 检查后回退默认，或统一逐条容错策略。
  - 置信度: 高

### 通过项
- 线程安全：纯函数 + frozen dataclass；_json/_copy_json 在边界隔离可变状态。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不涉及。
- 状态提交一致性：读/写 helpers 均返回新 dict；write_scene_tokens_into 附带 schema_version，
  未知 metadata 键保留；set_active_brand_theme_into 的 None 清除分支正确。
- 异常与取消路径：read_brand_themes 的 (TypeError, ValueError) 捕获为定向且逐主题跳过，可接受。
- 输入验证（除上述发现外）：BrandTheme/overlay 的 priority 非负校验、ResponsiveVariant 的区间
  校验完整（components.py 侧）；validate_theme 的未解析引用诊断设计良好。
- 性能模式：css_variables/resolve_style 均为一次性 O(n) 编译，无热路径递归以外的风险。
- 类型注解：完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-27400 |
| Major | 4 | AUD-27401, AUD-27402, AUD-27412, AUD-27415 |
| Suggestion | 11 | AUD-27403–AUD-27411, AUD-27413, AUD-27414, AUD-27416 |

（合计 16 项；content_localization.py 无发现。）
