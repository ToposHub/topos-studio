# Batch-054 静态审查报告：features/composite/core 分块 / 时间重映射 / 撤销栈

- 审查日期：2026-09-28
- 审查范围：src/features/composite/core/tile_padding_computer.py、tile_scheduler.py、time_remap.py、undo_stack.py
- 问题编号区间：AUD-5400 – AUD-5499
- 方法：全文 Read 四个目标文件（undo_stack.py 分两段读完）；对跨文件断言逐一取证：node_capabilities.py（action_type 分类、_GENERATOR/_SOURCE_MEDIA 集合）、nodes/video_frame_node.py、nodes/denoise_node.py、nodes/godrays_node.py 等 16 个声明 padding 的节点、core/format.py（BBox.expanded/clamped）、render/composite_export_renderer.py 与 render/render_scheduler.py（padding 消费方）、ui/composite_page.py（UndoStack 唯一生产使用方、begin/end_merge_coalescing 配对）。

---

## src/features/composite/core/tile_padding_computer.py (行数: 494)

### 发现

- [AUD-5400] [Critical] 源类型硬编码集合缺失 video_frame/text/ramp/noise 等源与生成器节点，这些图源的 total_padding 恒为 0，分块渲染出现接缝
  - 位置: src/features/composite/core/tile_padding_computer.py:140
  - 证据:
    ```python
    # 阶段七：camera3d/light3d/geo3d/model3d 同为 source 节点（scene 数据源）
    _SOURCE_TYPES = frozenset({"color_input", "constant", "image_input", "timeline_clip_input", "scene3d", "camera3d", "light3d", "geo3d", "model3d"})
    ...
    source_paddings = [
        accumulated.get(nid, 0)
        for nid, ns in snapshot.nodes.items()
        if ns.node_type in TilePaddingComputer._SOURCE_TYPES
    ]
    total = max(source_paddings) if source_paddings else 0
    ```
  - 影响: 代码库自身在 node_capabilities.py:1197-1204 将 `video_frame/image_input/timeline_clip_input` 归类为 `_SOURCE_MEDIA_NODE_TYPES`、`constant/color_input/text/ramp/noise/motion_graphic` 为 `_GENERATOR_NODE_TYPES`，与本集合不一致。图源全部为 video_frame（VideoFrameNode，TAGS=["input","source","video"]）、text、ramp、noise、particle、roto_shape、mask 等的图，source_paddings 为空 → total_padding=0，下游 blur/defocus 的 padding 需求被完全丢弃。消费方 composite_export_renderer.py:1184 与 render_scheduler.py:825 直接用该值生成 tile → 4K 分块导出/预览在 tile 边界出现可见接缝（渲染错误）。
  - 建议: 放弃硬编码集合，改用能力系统判定（get_node_capability().action_type=="source" 或 get_node_contract_class），或直接取 `max(accumulated.values())`（padding 沿链向上累加，源节点处最大，无源时全图最大值仍能覆盖最深链）。
  - 置信度: 高

- [AUD-5401] [Critical] _get_node_padding 硬编码 if/elif 类型链遗漏 denoise/face_smooth/light_wrap/edge_detect 等已声明非零 padding 的 effect 节点，一律返回 0
  - 位置: src/features/composite/core/tile_padding_computer.py:272
  - 证据:
    ```python
            elif node_type == "chromatic_aberration":
                from ..nodes.chromatic_aberration_node import \
                    ChromaticAberrationNode
                cls = ChromaticAberrationNode
            elif node_type == "output":
                from ..nodes.output_node import OutputNode
                cls = OutputNode
            else:
                return 0
    ```
  - 影响: `_needs_tile_padding_check()`（能力系统）对全部 effect 节点返回 True，但导入链只覆盖 30 个类型；grep 证实 16 个定义了 `_tile_padding`/`requires_roi_expansion` 的类型不在链中，其中 denoise（`_tile_padding` 返回 `int(radius)`，默认 3）、face_smooth（`int(radius)`，默认 4）、light_wrap（`max(0, int(width))`，默认 12px）、edge_detect（委托 `_tile_padding`）为非零值。这些节点即使挂在 image_input（在 _SOURCE_TYPES 内）之下，padding 也被算作 0 → tile 边缘出现去噪/光晕/边缘检测伪影。新增 effect 节点同样会静默丢失 padding。
  - 建议: 通过节点注册表按 node_type 反查类，或让节点类向本模块注册 padding 工厂；至少补齐已声明非零 padding 的四个类型，并为"能力判定需要检查但链外"的情况打告警日志。
  - 置信度: 高

- [AUD-5402] [Major] _get_node_padding 用宽泛 `except Exception: pass` 吞掉全部异常并静默返回 0 padding
  - 位置: src/features/composite/core/tile_padding_computer.py:383
  - 证据:
    ```python
            node = cls.__new__(cls)
            node.__init__(node_id=ns.node_id)
            # Sync parameters
            for key, val in ns.parameters.items():
                node.parameters[key] = val
    ...
        except Exception:
            pass

        return 0
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"。`cls.__new__(cls)` + 手动 `__init__(node_id=...)` 本身脆弱：任何节点构造签名变化、parameters 非字典、_tile_padding 内部断言失败都会被吞掉，padding 静默归 0 → 接缝，且无任何日志可排查。
  - 建议: 缩窄捕获范围（TypeError/AttributeError）并 logger.warning 记录 node_type 与异常；或改为不实例化节点、由类方法/注册表直接提供 padding 计算。
  - 置信度: 高

- [AUD-5403] [Suggestion] `memory_budget_mb or 默认值` 把显式传入的 0 预算当作未提供
  - 位置: src/features/composite/core/tile_padding_computer.py:163
  - 证据:
    ```python
    budget = memory_budget_mb or TilePaddingComputer._DEFAULT_MEMORY_BUDGET_MB
    ```
  - 影响: 调用方传 0（希望在极小预算下强制最小 tile）时被替换为 1024MB，行为与调用意图相反。
  - 建议: `budget = memory_budget_mb if memory_budget_mb is not None else ...`。
  - 置信度: 高

- [AUD-5404] [Suggestion] _compute_tile_size 的 output_w/output_h 形参从未使用，属误导性死参数
  - 位置: src/features/composite/core/tile_padding_computer.py:389
  - 证据:
    ```python
    def _compute_tile_size(
        output_w: int,
        output_h: int,
        padding: int,
        node_count: int,
        memory_budget_mb: float
    ) -> TileSizeRecommendation:
    ...
        for tile_size in tile_sizes:
            effective_size = tile_size + 2 * padding
            pixels = effective_size * effective_size
    ```
  - 影响: 函数体只使用 padding/node_count/预算，output_w/output_h 恒被忽略（调用方 compute() 还专门为其做了 `or 1920/1080` 回退），维护者易误以为 tile 尺寸会按输出分辨率自适应。
  - 建议: 删除两个形参及 compute() 中对应的回退逻辑，或在估算中加入输出面积因子。
  - 置信度: 高

### 通过项
- 反向拓扑累计算法验证正确：downstream_map 构建、own + max(downstream) 累加、padding_chain 追踪与模块 docstring 中的示例链（Blur10→Grade→Blur5）推演一致
- PaddingInfo/TileSizeRecommendation 为 frozen dataclass，无可变共享陷阱（默认工厂字段正确）
- estimate_tile_count/get_effective_tile_size 纯函数、ceil 除法与 max(1,...) 边界正确
- 本模块无 GPU/OpenGL 调用、无信号、无跨线程共享状态（线程安全/GPU 生命周期/信号防递归维度 N/A）
- 时间复杂度为 O(N+E)，无性能问题

---

## src/features/composite/core/tile_scheduler.py (行数: 116)

### 发现

- [AUD-5405] [Suggestion] generate_tiles 输入验证不足：负 padding 导致评估区小于输出区、float tile_size 直接抛 TypeError
  - 位置: src/features/composite/core/tile_scheduler.py:83
  - 证据:
    ```python
        if tile_size < 1:
            tile_size = 1024
    ...
        for y in range(0, output_format.height, tile_size):
            for x in range(0, output_format.width, tile_size):
    ...
                padded_roi = output_roi.expanded(padding).clamped(output_format)
    ```
  - 影响: BBox.expanded(margin)（format.py:83-90）对负 margin 会收缩，padded_roi 将小于 output_roi，GPU 实际评估区域盖不住待拼接区域 → 边缘漏渲染；tile_size 传入 float（如 1024.0）时 range() 抛 TypeError；tile_size<1 被静默改成 1024 而非报错。当前库内调用方（TilePaddingComputer 已 clamp 到 ≥0、常量 tile_size）不受影响，但这是公开静态 API，外部数据须经校验（铁律）。
  - 建议: 入口校验 `padding = max(0, int(padding))`、`tile_size = int(tile_size)`，非法值 raise ValueError 而非静默替换。
  - 置信度: 高

### 通过项
- tile 覆盖完整性正确：min(tile_size, width-x) 保证最后一个不完整 tile 精确覆盖到格式边界、无重叠
- padded_roi 经 expanded+clamped 处理，边界 tile 的 padding 截断符合 docstring 语义
- Tile 为 frozen dataclass；模块无状态、无 GPU/线程/信号代码（N/A）
- 底部向后兼容 re-export（TilePaddingComputer/PaddingInfo/TileSizeRecommendation/TilePaddingInfo）与源模块一致，无逻辑

---

## src/features/composite/core/time_remap.py (行数: 385)

### 发现

- [AUD-5406] [Suggestion] 纯仿射折叠引入取整漂移，链式 retime 实际取帧与逐层合成可差 1-2 帧
  - 位置: src/features/composite/core/time_remap.py:147
  - 证据:
    ```python
        if self._is_pure_affine(self) and self._is_pure_affine(downstream_g):
            new_speed = float(self.speed) * float(downstream_g.speed)
            new_offset = (int(round(int(self.offset) * float(downstream_g.speed)))
                          + int(downstream_g.offset))
            return TimeRemap(speed=new_speed, offset=new_offset)
    ```
  - 影响: apply() 计算 `round(F*ts)+to`，折叠后为 `round(F*ts*gs)+round(to*gs)+go`，而真实合成是 `round((round(F*ts)+to)*gs)+go`。反例：F=5, ts=0.5, gs=3.0（Python round 银行家舍入）→ 折叠得 8、逐层得 6，差 2 帧。两个 retime 串联的视频源会解码到与数学合成不同的帧；注释虽以 "≈" 承认近似，但同文件已为非仿射提供了 exact 的 inner 链接机制，仿射却未复用。signature() 会把折叠结果当作新维度缓存，错误帧可能被缓存固化。
  - 建议: 仿射折叠也走 inner 链接（exact），或至少在折叠会造成 round 语义差异（speed 非整数）时退回链接。
  - 置信度: 高（数学事实成立；实际影响为个位数帧偏差）

- [AUD-5407] [Suggestion] precomp 的 hold_frame=None 时 int(None) 抛 TypeError，整个节点退化为 pass-through，speed/offset 一并失效
  - 位置: src/features/composite/core/time_remap.py:203
  - 证据:
    ```python
            offset = int(parameters.get("frame_offset", 0) or 0)
            hold_raw = parameters.get("hold_frame", -1)
            hold = None
            if hold_raw is not None and int(hold_raw) >= 0:
                hold = int(hold_raw)
    ```
  - 影响: 同函数内 time_offset/retime 分支用 `or 0` 防御 None，precomp 分支没有——若持久化数据中 hold_frame 为显式 null，`int(None)` 抛 TypeError 被外层捕获后 return None，节点连 speed/frame_offset 的重映射也一起丢失（与 docstring"不可用参数值仅该映射退化为 pass-through"的粒度不符，属整节点级失效）。
  - 建议: `hold_raw is not None` 分支内再捕获/防御 None（如 `hold = int(hold_raw) if hold_raw is not None else None`），使坏字段只禁用 hold 语义。
  - 置信度: 中

### 通过项
- pushed_upstream 组合语义验证正确：hold 优先级处理（hold∘任意=常量）、非仿射走 inner 链接保 exact（apply 先 inner 后自身）、构造上 hold 与 inner 不会共存，apply 的 hold 短路安全
- apply() 的异常降级仅捕获 TypeError/ValueError/OverflowError 且有确定回退值，无宽泛 except
- signature() 递归生成稳定缓存维度，inner 链以 "|>" 前缀区分嵌套
- compute_frame_overrides 的分支冲突 first-wins 为文档化限制且提供 conflict_logger；bypassed/时间窗未激活节点的 pass-through 语义与 evaluator 一致
- _parse_loop_range 对 hi<lo、非数值的退化处理正确（返回 None 而非抛错）
- gate_active/_intersect_gates 的 None 无界与交集语义正确（相离区间自然产生永不可见）
- compute_layer_frame_gates 对非法帧值跳过该层（int 失败 continue），外部数据有验证
- 类型注解完整，模块纯函数无状态、无 GPU/线程/信号代码（N/A）

---

## src/features/composite/core/undo_stack.py (行数: 1189)

### 发现

- [AUD-5408] [Critical] UndoTree.compress 合并删除 next_node 时不重接其子节点，当前历史链断裂：undo 游标会走进已删除节点，redo 永久失效，被合并动作不可达
  - 位置: src/features/composite/core/undo_stack.py:629
  - 证据:
    ```python
            if (node.action and next_node.action and
                node.action.can_merge_with(next_node.action)):
                # Merge next_node into node
                merged = node.action.merge_with(next_node.action)
                node.action = merged
                ...
                # Remove next_node from tree
                del self._nodes[next_node_id]

                # Update current_node_id if needed
                if self._current_node_id == next_node_id:
                    self._current_node_id = node_id
    ```
  - 影响: 只处理了 current==next_node 的特例；当 current 在更深处（线性历史常态）时，next_node 的子节点（含 current）parent_id 悬挂指向已删除 id。此后 `_get_path_to_root` 在洞口截断（get_history_list 丢失根段）；undo 逐级走到洞口后 `get_current_node()` 为 None → can_undo/can_redo 同时为 False、redo 永久失效；此后每次 push 的节点父链均悬挂，undo 一步即回 ghost，redo 再也无法回到任何旧分支；被合并动作（保存着跨越两步的 old/new 值）本身不可达，该段历史事实上不可撤销。触发条件普通：历史超过 50 节点即每次 push 触发 compress，且当前路径上存在同 merge_key、时间窗（500ms）内相邻的两个动作（如两次快速拖同一滑杆）。
  - 建议: 删除 next_node 前把其 children 全部重挂到 node（child.parent_id=node_id、node.children.extend），并同步修正 current 与路径；或压缩仅针对"叶子侧"节点（next_node 无子节点时才允许合并删除）。
  - 置信度: 高

- [AUD-5409] [Major] compress 的按最旧淘汰剪枝同样不重接子链：可删除 current 的祖先，keep_types 节点沦为不可达孤儿，undo 到底后游标落入 ghost 触发与 AUD-5408 相同的级联
  - 位置: src/features/composite/core/undo_stack.py:658
  - 证据:
    ```python
        while self.get_nodes_count() > max_nodes:
            ...
            for nid, node in self._nodes.items():
                if node.is_root():
                    continue
                if nid == self._current_node_id:
                    continue  # 永不淘汰当前节点（undo/redo 游标）
                if node.action and node.action.action_type in keep_types:
                    continue
            ...
            node = self._nodes.get(oldest_id)
            if node and node.parent_id:
                parent = self._nodes.get(node.parent_id)
                if parent and oldest_id in parent.children:
                    parent.children.remove(oldest_id)

            del self._nodes[oldest_id]
    ```
  - 影响: 两轮扫描都只保护 root 与 current，current 的祖先可被删除且子链不重接：线性+无 keep 节点时尚属"从最旧端截断"的近似预期；一旦路径上穿插 keep_types 节点（add_node/connect 被跳过而其更新的非 keep 邻居被删），存活 keep 节点的后代被删后 keep 节点成为不可达孤儿——"永久保留"承诺失效，get_all_branches 会向 UI 列出永远无法 switch/redp 到的分支；退化兜底轮（注释自述为修死循环）还会直接淘汰 keep_types 节点。用户 undo 到洞底后游标落入 ghost，后续 push 悬挂、redo 永久失效（同 AUD-5408 级联）。
  - 建议: 淘汰候选限定为"无后代的分支叶"（children 为空且不在 current 路径上）；删除时清理被删节点后代的悬挂引用；_get_path_to_root 遇洞时打告警。
  - 置信度: 高（机制确定；严重后果需分支/keep 混合或 undo 到底才显现）

- [AUD-5410] [Major] load_from_file 的动作恢复是死代码：UndoNode.from_dict 从不写入 action 字段，恢复循环永不执行，载入后 undo 移游标不执行动作、redo 拒绝前进
  - 位置: src/features/composite/core/undo_stack.py:201
  - 证据:
    ```python
        node = cls(
            id=data["id"],
            parent_id=data.get("parent_id"),
            children=data.get("children", []),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            ...
        )
        # Note: action needs to be restored separately with callbacks
        return node
    ...
            for node_id, node in self._tree._nodes.items():
                if node.action:                      # 恒为 None
                    action_data = node.action  # This is actually dict at this point
    ```
  - 影响: from_dict 丢弃 "action" 键，node.action 恒为 None，`if node.action:` 分支（含 UndoAction.from_dict 调用）无任何执行路径。结果：load_from_file 返回 True 并 emit changed，看似成功；实际所有动作丢失——tree.undo() 仍把游标移到父节点但不执行任何动作（游标与图状态错位），tree.redo() 因 child.action 为 None 返回 None 且不移动游标（can_redo 却为 True，UI 显示可重做但点击无反应）。注：已 grep 确认 src/ 内无生产调用方接线持久化（composite_page 以 `UndoStack(self)` 构造、未传 persistence_path），当前属潜在缺陷。
  - 建议: UndoNode.from_dict 将原始 dict 暂存（如 node.action = data.get("action")）以兑现注释约定；load_from_file 对 action_type 不在 _callback_registry 的节点显式剔除并告警，而非留下 action=None 的节点。
  - 置信度: 高

- [AUD-5411] [Major] save_to_file 对含 group 动作的历史必然失败：data 内嵌 UndoAction 对象列表，json.dump 抛 TypeError 被宽泛 except 吞掉
  - 位置: src/features/composite/core/undo_stack.py:819
  - 证据:
    ```python
        group_undo = UndoAction(
            "group",
            {"actions": actions},          # List[UndoAction] 对象
            lambda d: self._undo_group(d["actions"]),
            lambda d: self._redo_group(d["actions"]),
            desc,
        )
    ...
        def to_dict(self) -> dict:
            return {
                "action_type": self.action_type,
                "data": self.data,          # 原样嵌入，未递归序列化
    ...
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
    ```
  - 影响: 一旦历史中存在任何 group 动作（composite_page 的 Add media/Paste/Duplicate/Delete/Modify backdrop 均走 begin_group），save_to_file 必抛 `TypeError: Object of type UndoAction is not JSON serializable`，被 `except Exception` 吞掉后仅 logger.warning 并返回 False——撤销历史持久化静默全量失败（不是丢一条，是整个文件写不出）。参数值含 numpy 标量等非 JSON 类型时同样失败，且无 default= 序列化钩子。与 AUD-5410 同注：当前无生产调用方接线，属潜在缺陷。
  - 建议: to_dict 对 "group" 类型递归序列化子动作；json.dump 提供 default= 处理常见数值类型；失败日志升级为 error 并区分 IO 错误与序列化错误。
  - 置信度: 高

- [AUD-5412] [Major] undo/redo 执行回调无异常保护：回调半途抛异常时游标不移动、无回滚，重试会把已部分应用的子动作再应用一遍
  - 位置: src/features/composite/core/undo_stack.py:286
  - 证据:
    ```python
        current = self.get_current_node()
        if not current or current.is_root():
            return None

        # Execute undo
        if current.action:
            current.action.undo()

        # Move to parent
        self._current_node_id = current.parent_id
    ```
  - 影响: action.undo() 抛异常时 `_current_node_id` 未移动、UndoStack.undo 的 changed/graph_changed 不发射（UI 菜单状态停滞），异常直接上抛到 Qt 槽。对 group 动作（`_undo_group` 反序逐个执行）而言，第 3/5 个子动作失败后前两个已被撤销；用户再按 Ctrl+Z 会从列表头重放全部子动作，已撤销的部分被二次撤销（如参数被设成更早的旧值、节点被重复删除）→ 图状态错乱且无日志。
  - 建议: 包裹 action.undo()/redo()，异常时记录已执行前缀并告警（至少保证游标推进到与已应用前缀一致），禁止静默重放；或在文档中明确回调必须幂等。
  - 置信度: 中（取决于回调是否可能抛异常；结构性缺口确定）

- [AUD-5413] [Suggestion] begin_merge_coalescing 无条件丢弃未提交的 _pending_merge_action，嵌套/重复 begin 会静默丢失撤销记录
  - 位置: src/features/composite/core/undo_stack.py:777
  - 证据:
    ```python
    def begin_merge_coalescing(self):
        self._merge_coalescing = True
        self._pending_merge_action = None  # Reset pending action
    ```
  - 影响: 若上一段 coalescing 尚未 end（或调用方漏配对）就再次 begin，已累积的合并动作被直接丢弃，该段参数编辑失去撤销记录。已核实当前唯一生产调用方 composite_page 以 `_pending_param_change is None` 做了互斥保护，暂不触发；属公开 API 的防御缺失。
  - 建议: begin 时若 pending 非空先 push 再重置，或断言/告警未配对调用。
  - 置信度: 中

- [AUD-5414] [Suggestion] undo/redo 执行期间无重入保护，回调触发的参数变更路径可重入 push() 污染正在撤销的历史
  - 位置: src/features/composite/core/undo_stack.py:840
  - 证据:
    ```python
    def undo(self) -> Optional[UndoAction]:
        action = self._tree.undo()
        if action:
            self.changed.emit()
            self.graph_changed.emit()
        return action
    ```
  - 影响: tree.undo() 内联执行任意 undo_fn/redo_fn（composite_page 的 _undo_param 会 set_node_parameter + _refresh_properties_panel + _evaluate_graph）；若回调间接触发属性面板信号 → _on_parameter_changed → push()，会在 undo 过程中把"撤销本身"记录为新动作，历史被污染。目前依赖调用方的 `_page_node_is_locked` 状态锁兜底，栈自身无 `_applying` 重入门闩。
  - 建议: 增加 `_applying` 标志，执行回调期间 push() 直接拒绝或入队缓冲。
  - 置信度: 中

- [AUD-5415] [Suggestion] 类型注解不严谨：可变默认值语义的参数未标 Optional
  - 位置: src/features/composite/core/undo_stack.py:593
  - 证据:
    ```python
    def compress(self, keep_types: List[str] = None, max_nodes: int = 50):
    ...
    def __init__(self, parent=None, persistence_path: str = None):
    ...
    def register_callbacks(self, action_type: str, undo_fn: Callable, redo_fn: Callable):
    ```
  - 影响: keep_types/persistence_path 实际接受 None 但注解为 List[str]/str，register_callbacks 缺 -> None；与项目强类型要求不符，静态检查会漏报。
  - 建议: 改为 Optional[List[str]]/Optional[str]，补全返回注解。
  - 置信度: 高

### 通过项
- UndoTree 基本结构操作正确：push 分支命名继承、undo/redo 游标移动、select_branch 一次性选择、redo_to_branch、switch_to_node 的公共祖先算法（结构完好时）
- UndoAction 合并语义正确：merge_with 保持最早 old + 最新 new、timestamp 取较早者、can_merge_with 校验 merge_key/时间窗/other.is_compressed
- 压缩的两级淘汰 + 兜底退出（注释所述死循环场景）已正确退出，无 100% CPU 空转
- UndoStack 为 QObject，生产使用方 composite_page 在 UI 线程构造与调用，未发现跨线程访问（线程安全按单线程使用成立）
- changed/graph_changed 仅在动作实际执行后发射（正常路径无递归信号发射源）
- 命令工厂函数（create_param_change_action 等）字段完整、merge_key 设计合理（param 维度/节点集合维度）
- 模块无 GPU/OpenGL/GLSL 代码，HDR 维度 N/A
- load/save 的路径缺省回退（persistence_path/不存在返回 False）处理正确

---

## 统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 3 | AUD-5400、AUD-5401、AUD-5408 |
| Major | 5 | AUD-5402、AUD-5409、AUD-5410、AUD-5411、AUD-5412 |
| Suggestion | 8 | AUD-5403、AUD-5404、AUD-5405、AUD-5406、AUD-5407、AUD-5413、AUD-5414、AUD-5415 |

合计 16 项发现。
