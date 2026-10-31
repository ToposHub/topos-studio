# T76 / T79 评估文档 — Composite AE-style Timeline

本文件记录 Composite AE-style Timeline 实现计划（
`composite_ae_like_timeline_implementation_plan_2026-08-06.md`）中两项
**评估类**任务的结论。这两项的完成标准本身就是一份评估文档，而不是代码
变更。

---

## T76 — 主题 Token 迁移审计

**完成标准**：新增颜色全部使用语义 token，深色主题下无硬编码散色。

### 审计范围

Graph Editor 全套新代码（T100–T129）：
- `src/features/composite/ui/graph_editor_view.py`
- `src/features/composite/ui/graph_model.py`
- `src/features/composite/ui/graph_expression_editor.py`
- `src/features/composite/ui/graph_view.py`
- `src/features/composite/ui/timeline_icons.py`
- `src/features/composite/ui/timeline_time_display.py`
- `src/features/composite/ui/keyframes_panel.py`（新增工具栏/页脚部分）
- `src/features/composite/ui/keyframes_tree_view.py`（AE 列重写）

### 结论：**通过（无遗留硬编码主题色）**

UI 主题色全部走语义 token：

| UI 元素 | 解析方式 |
|---|---|
| 背景 / 网格 / 边框 / 文本 / 零线 | `timeline_layout.resolve_token(...)`（优先项目 `C.*` 调色板） |
| 播放头 / 关键帧选中色 / 强调色 | `resolve_token("keyframe_selected" / "accent_orange" / "accent_blue" / ...)` |
| 表达式错误标记 | `resolve_token("error")` |
| 表达式编辑器输入框 / 标签 | `C.BG_INPUT / C.TEXT_PRIMARY / C.BORDER / #ff6666`（沿用既有 `ERROR_LABEL_STYLE` 的红，与项目既有错误色一致） |

### 保留的“硬编码”色（**有意为之，非主题色**）

下列颜色是**数据默认值**或**跨主题固定的语义编码**，不应迁移到主题 token：

1. **通道轴色**（`graph_model._CHANNEL_COLORS`）：X `#ff8a3d`（橙红）、
   Y `#3dd97a`（绿）、Z `#3d9cff`（蓝）、Alpha `#c8c8c8`（灰白）。
   计划 §19.6 明确要求“X 红/橙、Y 绿、Z 蓝”作为**固定语义编码**，
   跨主题一致（色盲安全形状 + 固定轴色是 AE 行为）。
2. **Marker 默认色** `#ff3d3d`：仅当 marker 数据本身没有颜色时的回退；
   实际颜色来自 marker 数据（`TimelineMarker.color`），不是 UI 主题。
3. **序列默认色** `#bdbdbd`：仅当调用方未指定节点色时的回退；
   实际颜色来自 `build_graph_series(node_color=...)`，不是主题。

这些均通过 `resolve_token` 或数据传入，不会在深色主题下产生散色。

### 后续建议（非阻塞）

- 若未来引入浅色主题，通道轴色可考虑放到一个独立的
  `channel_axis_palette` token 组，让浅色主题微调明度（当前固定色在浅色
  背景上仍可读，但不是最优）。

---

## T79 — AE 数据导入/导出评估

**完成标准**：只在确认需求后实现映射；不把内部 schema 伪装为 AE 文件。
独立 format mapping 文档和测试。

### 结论：**当前不实现 AE 文件 I/O（按计划要求推迟）**

计划 T79 原文：“只在确认需求后实现映射；不把内部 schema 伪装为 AE 文件”。

#### 1. 内部 schema 与 AE schema 的差异（为何不能直接映射）

| 维度 | Topos 内部 | After Effects |
|---|---|---|
| 节点模型 | 节点图（NodeGraph，有向无环图，任意节点连接） | 图层（Layer）列表 + 每图层属性树 |
| 动画存储 | `node.keyframe_tracks[param]`（标量轨道，vec2 拆成 `_x/_y`） | `AVLayer.property("Transform").property("Position")`（vec2/vec3 单轨） |
| 表达式 | `node._expressions[param]`（字符串，AST 求值器子集） | `Property.expression`（完整 AE 表达式语言，含 `thisComp.layer(...)`、`transform.position` 等） |
| 时间 | 帧整数（`frame`）+ `fps` | 秒（`time`）+ 帧率，comp/layer 双时间基 |
| 插值 | `linear/hold/bezier/ease_*`（字符串） | `KeyframeInterpolationType` 枚举 + spatial/temporal 分离 |
| 空间属性 | vec2 拆成 `_x/_y` 标量（无空间路径） | Position 是空间路径（有 spatial bezier） |

Topos 是**节点图合成器**，AE 是**图层合成器**——两者数据模型本质不同。
强行双向映射会导致：
- 节点图无法无损表达为图层栈（多输入 merge、ramp、noise 无对应图层类型）；
- AE 的空间路径/spatial handle 在 Topos 的标量拆分里丢失；
- AE 表达式语言的 `thisComp.layer(...)` / `transform.*` 引用在 Topos 的
  AST 求值器里没有对应（Topos 用 `this.xxx` / `NodeName.param`）。

#### 2. 评估结论

- **导入（AE → Topos）**：技术上可行但价值有限——只能导入 AE 的图层
  属性动画到对应节点参数，且会丢失空间路径、图层效果、表达式引用。在
  没有明确用户需求前**不实现**。
- **导出（Topos → AE）**：节点图无法映射为 AE 图层栈（拓扑不兼容），
  强行导出会产生误导性的“假 AE 文件”（计划明确禁止：'不把内部 schema
  伪装为 AE 文件'）。**不实现**。
- **替代方案**：若未来需要与 AE 互通，应走**中间格式**（如修改版
  `.aep` 的 xmp/JSON 侧车，或独立的 `.topos-anim.json`），明确标注是
  Topos 格式而非原生 AE 文件，并提供单向的“动画曲线导出”（帧/值表，
  不含节点拓扑），由用户在 AE 里手工挂载。

#### 3. 当前状态

- **未实现**任何 AE 文件读写代码。
- **未创建**任何 `.aep` 相关的导出/导入入口。
- 关键帧数据本身的序列化走 Topos 项目文件（既有 `NodeSnapshot` +
  `keyframe_tracks` dict），与 AE 无关。

#### 4. 触发实现的条件（未来）

当且仅当满足以下**全部**条件时再评估实现：
1. 有明确用户需求（用户调研/反馈表明 AE 互通是高优先级）；
2. 选定了清晰的中间格式（不是直接读写 `.aep`）；
3. 设计了无损映射范围文档（哪些保留、哪些丢失、如何告知用户）；
4. 通过了格式一致性测试（往返不丢已支持的属性）。

在此之前，T79 的“完成标准”就是**本评估文档**——明确记录了不实现的
理由和触发条件，避免未来误把内部 schema 当 AE 文件导出。

---

## 总结

| 任务 | 状态 | 交付物 |
|---|---|---|
| T76 主题 token 迁移 | ✅ 通过 | UI 主题色全部走语义 token；保留的硬编码色均为数据默认值或固定语义编码（计划允许） |
| T78 跨平台输入 | ✅ 通过 | `_additive_modifiers` 接受 Shift/Ctrl/Cmd；表达式编辑器 Ctrl/Cmd+Return 均提交；测试覆盖（`tests/ui/test_graph_cross_platform.py`） |
| T79 AE 导入/导出 | ✅ 评估完成（不实现） | 本文档：节点图 vs 图层栈模型不兼容，按计划要求推迟到有明确需求 |
