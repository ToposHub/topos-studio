# Batch-005 静态审查报告：color/core 调色剪贴板 / 关键帧 / 历史

- 审查日期：2026-09-28
- 审查范围：src/features/color/core/grade_clipboard.py、grade_keyframes.py、history.py
- 问题编号区间：AUD-0500 – AUD-0599
- 方法：全文 Read 三个目标文件；对跨文件断言（Layer.to_dict 引用语义、apply_grade_patch 就地变更、undo 消费路径、canvas 就地编辑）逐一 Read/grep 了 layer.py、image_engine.py、edit_controller.py、layer_controller.py、canvas.py 的对应行号取证。

---

## src/features/color/core/grade_clipboard.py (行数: 217)

### 发现

- [AUD-0510] [Major] 粘贴时同一 patch dict 对象复用到多个目标图层，目标间共享可变嵌套结构
  - 位置: src/features/color/core/grade_clipboard.py:197
  - 证据:
    ```python
    broadcast = len(self.layer_patches) == 1 and len(target_layers) > 1
    applied = 0
    for i, target in enumerate(target_layers):
        patch = (self.layer_patches[0] if broadcast
                 else self.layer_patches[i % len(self.layer_patches)])
        target.apply_grade_patch(patch, cats)
    ```
  - 影响: 广播模式（单源多层）与取模回绕、以及同一剪贴板的第二次粘贴，都把同一个 patch dict 传给 `Layer.apply_grade_patch`。该函数对 mask_shapes 走 `MaskShape.from_dict(d)`（layer.py:215-221 `shape.points = data.get("points", [])` 等直接存引用），不做 deepcopy。于是多个目标图层的 `points`/`grad_points`/`brush_strokes`/`center`/`size` 列表是同一对象，且与剪贴板内的 patch 共享。canvas 编辑是就地变更（canvas.py:1822 `self.bezier_points[idx]['pos'] = pos`、canvas.py:1839 `self.size[0] = ...`），拖动一个目标上的曲线点会同时改掉其他目标图层和剪贴板快照里的数据——跨目标状态污染、再次粘贴得到被污染的数据。
  - 建议: 在 `paste_onto_layers` 循环内对每次应用做 `copy.deepcopy(patch)`，或在 `apply_grade_patch` 的 mask_shapes 分支统一 deepcopy。
  - 置信度: 高

- [AUD-0511] [Major] copy_grade 走 to_dict(None)，BITMAP 蒙版的 bitmap_data 被静默丢弃
  - 位置: src/features/color/core/grade_clipboard.py:142
  - 证据:
    ```python
    for layer in layers:
        layer_dict = layer.to_dict(None)
        patch = _filter_category_keys(layer_dict, layer_cats)
    ```
    （layer.py:172-183，`to_dict` 仅在 `assets_dir` 有效时落盘位图，且注释明示 `# If no assets_dir, bitmap_data is not persisted (will be lost)`）
  - 影响: 复制含 BITMAP 类型蒙版的 windows 类别后粘贴到其他 clip，得到的 MaskShape `bitmap_data=None`（from_dict 无 bitmap_file 可读），位图蒙版在目标上失效/渲染为空；`copied_categories` 仍包含 "windows"，用户无任何提示。与 ai_matte 二进制"不跨 clip 携带"有明确注释不同（layer.py:741-742），位图蒙版丢失没有任何文档说明。
  - 建议: 复制时改用带临时 assets_dir 的序列化，或在剪贴板快照中内联位图数据；至少在 `describe()`/复制反馈中对含 BITMAP 蒙版的情况给出告警。
  - 置信度: 中（数据丢失由 layer.py:183 注释直接证实；BITMAP 蒙版为 None 时的具体渲染表现未逐行验证）

- [AUD-0512] [Suggestion] lut_intensity 恒被保留导致无 LUT 源也把 "lut" 计入 copied_categories，粘贴预览误报
  - 位置: src/features/color/core/grade_clipboard.py:145
  - 证据:
    ```python
    patch = {
        k: v for k, v in patch.items()
        if v not in (None, "", [], {}, ()) or k == "lut_intensity"
    }
    ...
    for cat, keys in GRADE_CATEGORY_KEYS.items():
        if any(k in present_keys for k in keys):
            self.copied_categories.add(cat)
    ```
  - 影响: `to_dict` 无条件序列化 `lut_intensity`（layer.py:942），即使源层从未启用 LUT（`active_lut_path=None` 被剔除），"lut" 也总出现在 `copied_categories` 与 `describe()` 清单中，违背 dataclass 文档"源为空数据的类别剔除，使粘贴预览只列出真正会变化的项"的约定。对目标层无实际副作用（apply_grade_patch 仅在 patch 含 `active_lut_path` 时应用，layer.py:707-709），属预览信息失真。另注：`or k == "lut_intensity"` 分支实际是死代码——`lut_intensity` 是 float，永不等价于 `(None, "", [], {}, ())` 中任何元素。
  - 建议: "lut" 类别的存在性只依据 `active_lut_path` 非空判定；删除无效的特判分支。
  - 置信度: 高

- [AUD-0513] [Suggestion] describe() 绕过 i18n 辅助函数直用硬编码标签
  - 位置: src/features/color/core/grade_clipboard.py:213
  - 证据:
    ```python
    ordered = list(GRADE_CATEGORY_KEYS) + ["tracking", "raw"]
    return [GRADE_CATEGORY_LABELS[c] for c in ordered if c in cats]
    ```
    （同文件 74-77 行已定义 `grade_category_label(cat)` 走 `I18N.tr`）
  - 影响: 粘贴预览清单永远是中文缺省标签，`grade_category_label` 的 i18n 路径在预览对话中失效，两处标签来源不一致。
  - 建议: `describe()` 改用 `grade_category_label(c)`。
  - 置信度: 高

### 通过项
- 线程安全：纯内存数据结构，本文件无 OpenGL 调用、无跨线程信号传递。
- GPU 资源生命周期：不创建/持有任何 GPU 资源，不适用。
- GLSL/HDR 正确性：无内嵌 GLSL，无 clamp/采样问题。
- 复制隔离：`_filter_category_keys` 对选定键 deepcopy（96 行），trackings/raw_params 复制时 deepcopy（160/166 行），源层数据与剪贴板隔离正确。
- 栈级类别（tracking/raw）由调用方（edit_controller.py:7390-7398）deepcopy 后经命令提交，本文件仅存储，边界清晰。
- 输入验证：`v not in (None, "", [], {}, ())` 对 ndarray 会抛 ValueError，但核实 `to_dict` 产物（warper 经 tolist、curves 为 dict/list、位图不内联）不含 ndarray，当前安全。

---

## src/features/color/core/grade_keyframes.py (行数: 337)

### 发现

- [AUD-0520] [Major] from_dict 不校验 value 的类型/分量数，畸形工程数据延迟到渲染/保存期才崩溃
  - 位置: src/features/color/core/grade_keyframes.py:84
  - 证据:
    ```python
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GradeKeyframe":
        return cls(
            frame=int(data.get("frame", 0)),
            value=data.get("value", 0.0),
            interpolation=str(data.get("interpolation", "linear")),
        )
    ```
    （消费端 value_at:152 `return tuple(value) if is_vec else float(value)`、165-168 `tuple(_lerp(float(a), float(b), t) for a, b in zip(left.value, right.value))`）
  - 影响: 工程文件（外部输入）中的 `value` 可以是字符串、dict、None 或分量数错误的数组。加载期 `frame=int(...)` 的错误会被 Track.from_dict 的 try 捕获，但 `value` 的畸形完全不被检查：求值时 `float("abc")`/`float({...})` 抛 ValueError/TypeError 且发生在 `value_at`（预览渲染与 timeline_export 共用入口，image_engine.py:1463、timeline_export.py:8667）；分量不匹配时 `zip` 静默截断，产出 2 元 tuple 冒充 vec3 下传 GLSL uniform。违反"外部数据安全读取"维度，错误从加载期推迟到渲染热路径。
  - 建议: 在 `GradeKeyframe.from_dict` 或 `__post_init__` 中按 `GRADE_KEYFRAME_PARAMS`/`MASK_KEYFRAME_FIELDS` 的值形态校验 value（可 float 化、分量数为 3/2），非法即丢弃或降级为默认值。
  - 置信度: 中（缺失校验为代码事实；实际是否崩溃取决于上层渲染循环的异常兜底，未在本批次文件内验证）

- [AUD-0521] [Suggestion] Track.from_dict 捕获 TypeError/ValueError 后静默 continue，无日志吞掉畸形关键帧
  - 位置: src/features/color/core/grade_keyframes.py:189
  - 证据:
    ```python
    for kf_data in data.get("keyframes", []):
        try:
            track.keyframes.append(GradeKeyframe.from_dict(kf_data))
        except (TypeError, ValueError):
            continue
    ```
  - 影响: 工程文件局部损坏时关键帧被无提示丢弃，用户动画"少了几帧"却无从排查；异常类型也偏宽（与 int() 相关的领域错误被泛化吞掉）。
  - 建议: 至少 logger.warning 记录被丢弃的帧号与原因。
  - 置信度: 高

- [AUD-0522] [Suggestion] UI 编辑与导出线程共享 tracks 无同步，append→sort 间隙可读到中间态
  - 位置: src/features/color/core/grade_keyframes.py:115
  - 证据:
    ```python
    self.keyframes.append(kf)
    self.keyframes.sort()
    ```
    （消费端 157-160：`for index in range(len(self.keyframes) - 1): left = self.keyframes[index]; right = self.keyframes[index + 1]`；timeline_export.py:8667 在导出路径调用 evaluate_grade_keyframes，该模块含 threading）
  - 影响: 模块文档自述"engine 预览（GPU）与 timeline_export（CPU 导出）共用"。导出线程求值期间 UI 线程 `set_keyframe` 先 append 后 sort：两步之间 `keyframes[-1]` 是未排序的新帧，`value_at` 的端点外推（150-155 行）可能返回错误值写进导出结果。CPython GIL 下单次 append/sort 原子，不会崩溃，属瞬态错值竞态。
  - 建议: tracks 加轻量锁，或 UI 编辑与求值统一到单一线程队列。
  - 置信度: 低（导出求值与 UI 编辑是否真实并发运行未在导出线程模型中逐行确认）

- [AUD-0523] [Suggestion] 关键公共入口类型注解过宽
  - 位置: src/features/color/core/grade_keyframes.py:64
  - 证据:
    ```python
    frame: int
    value: Any  # float 或 3 元素 tuple/list
    interpolation: str = "linear"  # 到下一帧的插值模式
    ```
    （另 275 行 `layer: Any`、310 行 `layer: Any`）
  - 影响: `value: Any` 与 `layer: Any` 使 mypy 无法捕获 AUD-0520 一类的形态错误；建议引入 `Union[float, Tuple[float, ...]]` 与 Layer 协议类型。
  - 置信度: 高

### 通过项
- 线程安全：本文件自身无 OpenGL/ModernGL 调用；evaluate_mask_keyframes 遍历用 `list(tracks.items())`（321 行）做了防御性拷贝。
- GPU 资源生命周期：不适用。
- GLSL/HDR 正确性：无内嵌 GLSL；插值数学无 clamp(0,1)，端点外推语义符合调色需求；`_interp_factor` 的 t 由区间约束保证在 [0,1]。
- 求值一致性：`evaluate_grade_keyframes` 返回浅拷贝并只覆盖白名单内轨道（209 行双条件），mask.* 轨道与 grade 轨道互不干扰，注释与实现一致。
- 插值边界：`span <= 0` 时 t=0（162 行），重复帧不会除零。
- 序列化对称性：to_dict/from_dict 的 frame 强制 int、interpolation 非法值回退 linear（69-70 行）。

---

## src/features/color/core/history.py (行数: 155)

### 发现

- [AUD-0501] [Critical] 历史快照与活动图层共享可变对象，undo/redo 静默失效
  - 位置: src/features/color/core/history.py:136
  - 证据:
    ```python
    serialized = []
    for layer in layers:
        serialized.append(layer.to_dict(assets_dir=None))
    return serialized
    ```
    （Layer.to_dict 返回活动引用——layer.py:938 `"grading_params": self.grading_params`、967 `data["plugins"] = self.plugins`、945 mask_shapes 经 MaskShape.to_dict:155-159 `"center": self.center, "points": self.points`；均不拷贝）
  - 影响: push_state 存进 undo_stack 的"快照"持有的是活动图层当前的 dict/list 本体。此后任何就地变更都会改写历史：`apply_grade_patch` 就地写 `self.grading_params[k] = v`（layer.py:699）、`qualifier_params.update(...)`（layer.py:712）、`soft_clip_params.update(...)`（layer.py:715）、edit_controller.py:7144 `layer.grading_params[k] = ...`、`set_plugin_enabled` 就地改 plugin dict（layer.py:910）、canvas 曲线点拖动 `bezier_points[idx]['pos'] = pos`（canvas.py:1822）。undo 走 `image_engine.py:4178 → _restore_state → deserialize_layers`（image_engine.py:4211）用 from_dict 重建，读到的已是污染后的同一 dict——撤销后调色参数/插件开关/蒙版点保持现值，撤销静默失效，用户编辑历史丢失。反向同样成立：恢复出的 Layer 的 `mixer_params = data.get("mixer_params", {})`（layer.py:1029）继续与栈内其他条目共享。
  - 建议: `_serialize_layers` 对 `to_dict` 产物做 `copy.deepcopy`（mask_shapes 逐项已新建外层，但内层 points 等仍是引用，需整体深拷）；或在 Layer.to_dict 内部保证导出快照为深拷贝，另设浅导出路径给保存场景。
  - 置信度: 高

- [AUD-0502] [Major] flip_h/flip_v 只在 push_state 记录，undo()/redo() 的对侧栈状态不含翻转位，恢复路径也不回读
  - 位置: src/features/color/core/history.py:77
  - 证据:
    ```python
    redo_state = {
        'image': current_image.copy(),
        'layers': self._serialize_layers(current_layers) if current_layers else [],
        'active_layer_index': current_active_index
    }
    self.redo_stack.append(redo_state)
    ```
    （push_state 的 state 含 `'flip_h': flip_h, 'flip_v': flip_v`，44-50 行；redo() 105-109 行同样缺失）
  - 影响: 两条失效链。(1) undo→redo 往返后，redo 栈弹出的状态没有 flip 键，消费端 `state.get("flip_h", False)` 恒为 False，翻转状态丢失；(2) `flip_image` 压栈时带翻转位（image_engine.py:4252-4253），但引擎的 `_restore_state`（image_engine.py:4194-4225）只恢复 image/layers/active_layer_index，从不回读 flip 键——撤销翻转后 `_flip_h` 仍为 True 而像素已回到翻转前，后续任一 `_apply_flip_state()`（再次翻转/裁剪时）会用脏标志从 `img_source_full` 重新翻出错误图像。
  - 建议: undo()/redo() 构造对侧状态时补记 flip_h/flip_v（可从引擎查询），并让 `_restore_state` 调用 `set_flip_state` 恢复。
  - 置信度: 高

- [AUD-0503] [Suggestion] copy_image=False 的引用共享依赖"源图像永不就地变更"的隐式约定
  - 位置: src/features/color/core/history.py:45
  - 证据:
    ```python
    state = {
        'image': image.copy() if copy_image else image,
        'layers': self._serialize_layers(layers) if layers else [],
    ```
  - 影响: 多处调用方传 `copy_image=False`（color_controller.py:120、layer_controller.py:1550、menu_toolbar_controller.py:1621 等）。当前未发现对 `img_full_original` 的就地写入（翻转/旋转/缩放均为重绑定），约定成立；但该不变量只写在 docstring 里，任何未来引入的就地像素操作（in-place filter、ROI 涂抹）都会无声污染全部历史快照，且无法从本模块发现。
  - 建议: 在模块级加注释断言该契约，或改为保存只读视图（`image.setflags(write=False)` 的副本检测）以防退化。
  - 置信度: 中（当前安全、未来脆弱，属防御性建议）

- [AUD-0504] [Suggestion] current_image 为 None 时 undo/redo 仍弹出条目但不写对侧栈，该编辑层历史不可达
  - 位置: src/features/color/core/history.py:76
  - 证据:
    ```python
    # 保存当前状态到重做栈
    if current_image is not None:
        redo_state = {...}
        self.redo_stack.append(redo_state)

    return self.undo_stack.pop()
    ```
  - 影响: 无图像状态下（清空画布后）执行 undo，当前图层状态被弹出却不进 redo 栈，redo 无法回到该状态，撤销/重做不再互逆。同理 push_state 在 `image is None` 时直接 return（37-38 行），调用方无感知压栈失败。
  - 建议: layers 非空时即使 image 为 None 也应保存对侧状态；push 失败可返回 bool 供调用方判断。
  - 置信度: 高

- [AUD-0505] [Suggestion] 类型注解缺失与隐式 Optional
  - 位置: src/features/color/core/history.py:16
  - 证据:
    ```python
    def __init__(self, max_steps=100):
        self.max_steps = max_steps
    ...
    def push_state(self, image: np.ndarray, layers: List = None, active_layer_index: int = 0,
    ```
  - 影响: `max_steps` 无注解；`layers: List = None`、`current_layers: List = None` 应为 `Optional[List]`；`clear()` 无返回注解。
  - 建议: 补齐注解，便于 mypy 捕获 None 误用。
  - 置信度: 高

### 通过项
- 线程安全：HistoryManager 仅由引擎/控制器在 UI 线程使用（调用点均在 controller/engine 主路径），本文件无跨线程共享。
- GPU 资源生命周期：不持有 GPU 资源，不适用。
- GLSL/HDR：无 GLSL 内容，不适用。
- 栈容量：`redo()` 向 undo_stack 回填虽无 max_steps 检查，但两栈总量在 undo/redo 间守恒且 push 时清空 redo 并裁剪，上限不会被突破（已推演）。
- `pop(0)` 的 O(n) 移位在 max_steps=100 量级下无性能影响。
- deserialize_layers 用局部 import 规避循环依赖，正确。
- undo()/redo() 对 `image` 均做 `.copy()`（78、106 行），True 路径的图像快照隔离正确。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-0501 |
| Major | 4 | AUD-0502, AUD-0510, AUD-0511, AUD-0520 |
| Suggestion | 8 | AUD-0503, AUD-0504, AUD-0505, AUD-0512, AUD-0513, AUD-0521, AUD-0522, AUD-0523 |

最高优先修复：AUD-0501（历史快照浅共享可变对象，undo/redo 静默失效，涉及 history.py:136 与 layer.py to_dict 的引用语义，修复点建议放在 `_serialize_layers` deepcopy）。
