# batch-075 审查报告 — src/features/composite/nodes/（tracker / transform / unknown_placeholder / unpremult）

- 日期：2026-09-28
- 编号区间：AUD-7500–AUD-7599（本报告使用 AUD-7501–AUD-7509）
- 审查文件：4 个，共 721 行，全部完整 Read
- 交叉验证：`nodes/base_node.py`（effect marker 协议、参数/端口 revision）、`evaluators/base_evaluator.py`（_resolve_output/_dispatch_effect/_create_ephemeral_node、B1 viewer-ROI 裁剪）、`evaluators/transform_evaluator.py`（_apply_transform）、`evaluators/color_evaluator.py` + `core/shaders.py`（unpremult shader）、`render/render_scheduler.py`（_propagate_roi、params 哈希、脏检测）、`render/tracker_analyze_worker.py`（track 数据生产者）、`core/format.py`（BBox Y-up 约定）、`ui/composite_page.py`（track 写回/导入/烘焙调用点）
- 汇总：Critical 1 / Major 3 / Suggestion 5

---

## src/features/composite/nodes/tracker_node.py (行数: 151)

### 发现

- [AUD-7501] [Major] `decode_track_data` 键规范化对非数值键抛未捕获异常，违背"容忍损坏"契约
  - 位置: src/features/composite/nodes/tracker_node.py:70
  - 证据:
    ```python
    transforms = payload.get("transforms")
    if isinstance(transforms, dict):
        payload["transforms"] = {
            str(int(float(k))): v for k, v in transforms.items()
            if isinstance(v, dict)}
    ```
  - 影响: docstring 声明 "Parse stored track_data; tolerates corruption (returns None)"，且注释明确预期外部/手工编辑的 JSON。但键为 `"abc"` 时 `float(k)` 抛 `ValueError`，`"nan"` 抛 `ValueError`，`"inf"` 抛 `OverflowError`——`except` 只包住 `json.loads`（58-61 行）。`track_data` 随项目文件持久化（外部数据），重载后在 `node_properties_panel.py:6862`、`composite_page.py:3770/3827/3953`（烘焙/导入/面板刷新）直接调用，一个坏键即让 UI 槽函数异常中断。
  - 建议: 对每个键做 `try: int(float(k)) except (TypeError, ValueError, OverflowError): continue` 的安全规范化，坏键跳过而非中断；补一条针对非数值键的单测。
  - 置信度: 高

- [AUD-7502] [Major] `track_data` 以整段 JSON 字符串作为参数存储，调度器每次生成计划都全量序列化+MD5，长跟踪下播放性能退化
  - 位置: src/features/composite/nodes/tracker_node.py:101（存储设计）；触发点 render_scheduler.py:576-577
  - 证据:
    ```python
    # Serialized analysis result (see module docstring) — not animated.
    ParamDef("track_data", "str", "", hidden=True),
    ```
    （消费端 render_scheduler.py:576-577：`params_str = json.dumps(ns.parameters, sort_keys=True, default=str)` / `params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]`，对全部节点、每次计划生成都执行。）
  - 影响: 分析范围上限为"参考帧+100000 帧"（composite_page.py:3690-3691），每帧约 150 字节 JSON；十万帧量级的跟踪数据达数 MB。计划生成在回放期间按帧反复执行，`json.dumps`（转义+排序键）与 `md5` 每次都对整段字符串线性扫描，且 CacheManager 的 cache key 同样基于序列化参数——CPU 开销随跟踪长度线性放大，违背 4K 实时基准的性能要求。
  - 建议: 将 `track_data` 改为旁挂的节点级 blob（不进 `parameters`，用内容哈希/revision 进 cache key），或对超长参数做一次性内容摘要缓存（仅在 param_revision 变化时重算）。
  - 置信度: 中（机制已核实；实际幅度取决于跟踪长度与回放路径）

- [AUD-7503] [Suggestion] `decode_track_data` 在入参为 dict 时原地修改调用方对象
  - 位置: src/features/composite/nodes/tracker_node.py:55-56, 69-71
  - 证据:
    ```python
    if isinstance(raw, dict):
        payload = raw
    ...
    payload["transforms"] = {
        str(int(float(k))): v for k, v in transforms.items()
    ```
  - 影响: `payload` 与入参 `raw` 同一对象，规范化写入会反向修改调用方持有的 dict（当前调用方都传 str，暂无实际触发面，属隐患）。
  - 建议: `payload = dict(raw)` 浅拷贝后再改写，与 `encode_track_data` 的 `dict(data)` 风格一致。
  - 置信度: 高

### 通过项
- 线程安全：分析结果经 Qt 队列信号在 UI 线程写回（composite_page._on_tracker_analyze_finished），worker 只发 dict；节点求值为纯直通，无 OpenGL 调用
- GPU 资源生命周期：无节点自有纹理，直通返回上游 TextureResult 引用，符合 base 协议（"Preserve TextureResult metadata when passing textures through"）
- GLSL/HDR 正确性：节点本身不触及 shader；无效输入（`image is None or not image.is_valid`）正确返回 `{}`
- 信号防递归：无 UI 控件、无信号
- 状态提交一致性：`set_track_data` 走 `set_parameter`（bump param_revision + 端口 revision），且本类在端口赋值后显式调用 `_init_port_revisions()`（125 行，同类中做得正确）
- undo/redo：composite_page 的 tracker_import 走 UndoAction 老/新值成对记录（3792-3800 行已核实）
- compute_output_bbox / compute_input_roi 直通语义正确（tracker 是数据节点，不改像素窗口）

---

## src/features/composite/nodes/transform_node.py (行数: 392)

### 发现

- [AUD-7504] [Critical] `_build_inverse_matrix` 不是前向矩阵的逆（m01/m10 符号错误），rotation ≠ 0 时 `compute_input_roi` 算出错误 ROI → ROI 渲染取错区域
  - 位置: src/features/composite/nodes/transform_node.py:180-187
  - 证据:
    ```python
    rad = math.radians(-rot)  # Inverse rotation
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    ...
    m00 = inv_sx * cos_r
    m01 = inv_sy * sin_r
    m10 = -inv_sx * sin_r
    m11 = inv_sy * cos_r
    ```
  - 影响: 前向矩阵线性部分 A = [[sx·c, −sy·s],[sx·s, sy·c]]（c=cos(rot), s=sin(rot)），其真逆为 [[c/sx, s/sy],[−s/sx, c/sy]]；而代码在 sin_r = sin(−rot) = −s 下写成 m01 = −s/sy、m10 = +s/sx，两项符号与真逆相反——线性部分恰好等于前向矩阵本身（旋转被应用两次而非撤销）。数值验证（按文件公式复算）：rot=90°、pivot(2,3)、tx=10 时点 (5,7) → 前向 (8,6) → 代码"逆" (−1,−1)（应为 (5,7)）；rot=30°、scale(1.5,0.8) 同样全点失配；rot=0 的纯平移/缩放则往返正确。`compute_input_roi`（279-280 行）将该矩阵用于 ROI 反向传播，运行时被 `render_scheduler.py:1017` 调用，其结果作为 `np.output_roi` 在 `base_evaluator.py`（B1 viewer-ROI 路径）用于裁剪上游源输出——任何带旋转的 Transform 在 ROI 启用的预览（局部区域/缩放视图）下会取错输入区域，产生内容缺失/错位的渲染错误。
  - 建议: 修正逆矩阵：m01 应为 `-inv_sy * sin_r`、m10 应为 `+inv_sx * sin_r`（连同 m02/m12 平移项按 A⁻¹ 重推导），并补充"前向∘逆 = 恒等"的逐点往返单测（至少覆盖 rot≠0、非零 pivot、非零 translate 组合）。
  - 置信度: 高（数学推导 + 数值复算双重复核；调用链已 Read 核实）

- [AUD-7505] [Major] 调度器侧与求值器侧使用不同的 pivot/坐标系参考：`compute_output_bbox`/`compute_input_roi` 用输入 bbox 尺寸，`_do_evaluate`/GPU 路径用纹理尺寸，与自身 docstring 的"matches the forward matrix"声明矛盾
  - 位置: src/features/composite/nodes/transform_node.py:247（调度器路径用 bbox 尺寸）；337-338、350-352（求值器路径用 image 尺寸）；263-265（docstring 声称一致）；204-206（flip 常量假设 y 底部锚定）
  - 证据:
    ```python
    matrix = self._build_transform_matrix(params, input_bbox.width, input_bbox.height)   # L247
    ...
    Pivot reference: use the input bbox dimensions when known (matches the
    forward matrix in ``_build_transform_matrix``), otherwise fall back to
    the output ROI dimensions.                                                    # L263-265
    ...
    piv_x = image.width / 2.0                                                       # L337
    ...
    fwd_matrix = self._build_transform_matrix(
        self.parameters, image.width, image.height
    )                                                                               # L350-352
    ```
  - 影响: `_do_evaluate`（供 `_apply_transform` 消费的 out_w/out_h/out_bbox 都由它派生）以 `image.width/height` 解析 center pivot 与 flip 常量；而调度器正向传播 `compute_output_bbox` 用 `input_bbox.width/height`（247 行），反向 `compute_input_roi` 的 ref_w/ref_h 同样取 bbox 尺寸（269-273 行）。当数据窗口 ≠ 纹理全幅或带偏移（上流 Merge/Transform 输出常见的 offset bbox，`_apply_transform` 返回的就是 bbox 偏移的打包纹理）时，两条路径的旋转中心与 `_transform_bbox` 的 Y 翻转常量（`ys_down = [image_height - bbox.y, ...]` 仅在 bbox.y=0 且高度等于画布时正确）不一致——CPU 估计 bbox（驱动 ROI 传播与画面摆位）与 GPU 实际标注 bbox 分叉，旋转/缩放下内容摆位或取样区域偏移。
  - 建议: 统一参考系：`compute_output_bbox`/`compute_input_roi` 与 `_do_evaluate` 使用同一尺寸来源（纹理尺寸或统一传 canvas/format），`_transform_bbox` 的 flip 常量改用画布高度而非 bbox 高度；修完后用"上游 bbox 偏移 + 旋转"用例对比 CPU 估计 bbox 与求值器返回的 out_bbox。
  - 置信度: 中（不一致本身在代码与 docstring 层面确凿；实际触发幅度取决于偏移 bbox 场景的普遍性）

- [AUD-7506] [Suggestion] 端口在 `super().__init__()` 之后才赋值，`_port_revisions` 为空，`bump_output_revision()` 变 no-op
  - 位置: src/features/composite/nodes/transform_node.py:63-70
  - 证据:
    ```python
    def __init__(self, node_id: Optional[str] = None):
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
    ```
  - 影响: 基类 `__init__` 在 outputs 为空时调用 `_init_port_revisions()`（base_node.py:486），本类未像 TrackerNode（125 行）那样在端口赋值后重新初始化，因此 `_port_revisions == {}`：`set_parameter` 触发的 `bump_output_revision()`（base_node.py:649）遍历空字典不生效。求值器侧对快照有 `.get(port,0)+1` 自举（base_evaluator.py:1052-1053）、脏检测另有 param_revision/params_hash 兜底，故实际后果有限，但违背了基类"Mutate parameters through set_parameter() so … port revisions stay correct"的约定，且 UI 侧直接调 `bump_output_revision()`（composite_page.py:3887/4069）时同样静默失效。
  - 建议: 在 `__init__` 端口赋值后补 `self._init_port_revisions()`（同本目录 TrackerNode/Paint/Roto 等 10 个节点的做法）。
  - 置信度: 高（机制确定，影响低）

### 通过项
- effect marker 协议合规：`_effect_type`/`_effect_input`/`_effect_params` 齐备，占位 `TextureResult(texture_id=-1)` 符合 base_node 协议；已核实 `_dispatch_effect`/`_apply_transform`（base_evaluator.py:2085、transform_evaluator.py:108）消费这些键
- identity 直通分支（328-329 行）返回真实 TextureResult、无 `_effect_*` 泄漏，符合协议"No-op paths should return without marker keys"
- 前向矩阵与 GPU 端 `_build_transform_pixel_matrix`（transform_evaluator.py:45-51）逐项一致，已人工核对 m00/m01/m10/m11/m02/m12 公式
- A5 motion blur 门控参数（motion_blur/shutter/samples/shutter_offset，387-390 行）已按 `_try_transform_motion_blur` 的读取契约注入 `_effect_params`
- 异常/取消路径：无效输入返回 `{}`；`filter` 参数取值非法时回退 index 0（332 行）不抛异常
- 输入验证：`_build_inverse_matrix` 对 sx/sy 为 0 有除零保护（162-163 行）；bbox 退化时 `_transform_bbox` 返回空 BBox（218-219 行），out_w/out_h 有 `max(1, …)` 下限（354-355 行）
- 类型注解完整（矩阵构建返回 `Tuple[float, ...]`，各方法参数有注解）

---

## src/features/composite/nodes/unknown_placeholder_node.py (行数: 120)

### 发现

- [AUD-7507] [Suggestion] `from_dict` 的 known_fields 缺 `expression_enabled` 与 `user_label`，占位节点的禁用表达式状态与重命名在重载后丢失
  - 位置: src/features/composite/nodes/unknown_placeholder_node.py:95-99
  - 证据:
    ```python
    known_fields = {
        "id", "type", "position", "parameters", "disabled",
        "expressions", "keyframe_tracks", "parameter_bindings",
        "parent_id", "time_range", "node_color",
    }
    ```
  - 影响: 基类 `Node.from_dict` 的 known_fields（base_node.py:1310-1315）包含 `"expression_enabled"` 与 `"user_label"` 并专门恢复它们（1253-1257、1306-1308 行）；本类重写的 `from_dict` 不读取这两个字段——禁用的表达式重载后恢复为启用（`_expressions` 直拷于 84 行，`_expression_enabled` 为空），用户重命名的 `user_label` 丢失。二者落进 `_unknown_fields` 仅在文件层面盲目往返，语义状态不恢复。
  - 建议: 在本类 `from_dict` 中补读 `expression_enabled`（调 `set_expression_enabled`）与 `user_label`（strip 后赋值），并把两字段加进 known_fields。
  - 置信度: 高（与基类实现直接对比可证）

- [AUD-7508] [Suggestion] `position` 反序列化无类型验证，损坏的外部数据会产生错误元组
  - 位置: src/features/composite/nodes/unknown_placeholder_node.py:66
  - 证据:
    ```python
    node.position = tuple(data.get("position", [0, 0]))
    ```
  - 影响: 本类的存在意义就是承载"未知类型/新版宿主"的外部图数据（docstring 1-9 行），铁律要求外部数据须验证。`position` 若为字符串 `"12"` 会得到 `('1','2')`，若为标量会抛 `TypeError`，后续 UI 以 `position[0]/[1]` 取坐标时崩溃或错位。（基类 from_dict 同样写法，属共性问题，但本类面向外部数据的概率最高。）
  - 建议: 校验为 2 元数值序列后再 `tuple(map(float, …))`，否则回落 `(0.0, 0.0)`。
  - 置信度: 高（机制确定，触发需损坏数据）

- [AUD-7509] [Suggestion] `_do_evaluate` docstring 声称记录警告日志，实际无任何日志调用
  - 位置: src/features/composite/nodes/unknown_placeholder_node.py:50-56
  - 证据:
    ```python
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                      resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
        """Placeholder evaluation — returns empty dict (no output).

        Logs a warning about the unknown node type for debugging.
        """
        return {}
    ```
  - 影响: 文档与行为不符——占位节点静默无输出，排障时缺少"未知类型被跳过"的线索（虽然模块未定义 logger，导入 `logging` 也没有）。
  - 建议: 要么补 `logger.warning`（模块级建 logger，内容含 `self._original_type`），要么删掉 docstring 中 "Logs a warning" 一句。
  - 置信度: 高

### 通过项
- 往返序列化：`to_dict` 恢复原始 `type`（112 行）并过滤 `_` 前缀内部参数（含 `_original_type`），外部参数/表达式/keyframe_tracks/绑定/time_range/node_color 均能保留（70-104 行逐一核实）
- `_unknown_fields` 前向兼容负载正确保留（100-104 行），基类 `to_dict` 会写回
- 异常路径：`_do_evaluate` 返回 `{}`，不崩溃；bindings/expressions/keyframe_tracks/time_range 加载均有 `isinstance` 防御
- GPU 资源生命周期 / GLSL·HDR：无纹理、无 shader 参与
- 信号防递归：无信号
- 状态：`NodeState.PLACEHOLDER` 正确设置（36 行），与基类 set_parameter/set_keyframe_track 的占位保护分支匹配
- 端口 revision 初始化小问题与 AUD-7506 同源（本类同样在基类 `__init__` 后才赋值端口），不另行立项
- 类型注解基本完整；`from_dict` 返回类型有注解

---

## src/features/composite/nodes/unpremult_node.py (行数: 58)

### 发现

（无 Critical/Major 发现。）

备注：端口在基类 `__init__` 后才赋值导致 `_port_revisions` 为空的共性问题与 AUD-7506（transform_node.py）同源，不重复立项。

### 通过项
- effect marker 协议合规：`_effect_type: "unpremult"` + `_effect_input` + 空 `_effect_params` + 占位 `TextureResult(texture_id=-1)`，已核实由 `base_evaluator.py:2091` 分发到 `_apply_unpremult`
- 直通语义正确：输入已标注直 alpha（`alpha_premultiplied is False`，45-46 行）时原样返回真实 TextureResult、无 marker 泄漏
- GPU 侧契约（交叉核实 color_evaluator.py:334-360 与 core/shaders.py `_UNPREMULT_FRAGMENT`）：输出正确标注 `alpha_premultiplied=False`，保留 format/bbox/color_space；shader 有除零保护（`max(color.a, 0.00001)`）、零 alpha 像素置黑，且无 `clamp(0.0,1.0)`——HDR 数值安全
- 线程安全：纯标记返回，无 OpenGL 调用、无可变状态
- 异常路径：无效输入返回 `{}`
- 输入验证：`not image_input or not image_input.is_valid` 有防御
- 无参数节点：无状态提交/undo/信号递归面
- 类型注解完整
