# 合成器：节点预览管线设计大纲

> 日期：2026-06-29
> 状态：设计草案（待评审）
> 关联：3D 预览网格遮挡问题的根治方案；补齐 Nuke/Fusion 级「选中任意节点直接预览」能力

---

## 1. 背景与动机

### 1.1 现状的根本缺陷

当前合成器的预览链路**硬绑定 Output 节点**：

- 渲染器 `_evaluate_snapshot`（`evaluators/base_evaluator.py:656`）永远返回 **Output 节点 `display` 端口**的 `TextureResult`。
- `_render_to_output`（`render/composite_renderer.py:912-970`）把它 blit 出去，经 `frame_ready` 信号链送给 Viewer。
- **后果**：Viewer 永远只显示整图合成结果（Output）。Scene3D 不连到 Output 就看不见；选中任意节点也不会改变画面。

### 1.2 这导致的连锁问题

| 问题 | 根因 |
|------|------|
| Scene3D 必须连 Output 才能预览 | 没有「节点级预览」管线 |
| 3D 预览和合成输出共用一条管线 | 网格（3D 专属辅助元素）被迫和合成输出纠缠 |
| 2D/3D 网格开关只能靠 snapshot 注入 hack | 同上——没有独立上下文 |
| 无法快速查看中间节点结果（如 Merge 前/后对比） | 没有「选中即预览」 |

### 1.3 Nuke / Fusion 的做法

- **Nuke**：选中任意节点按数字键（1/2/3...）→ 该节点输出直接送 Viewer。Output 只管最终交付。
- **Fusion**：节点右上角「View On」按钮，任意节点进预览。
- **共同点**：预览管线与输出管线**分离**。预览是节点级的，输出是整图级的。

### 1.4 关键事实：基础设施已具备

调研确认以下现成机制可直接复用：

1. **每个节点的输出纹理都已被求值并缓存**（`CacheEntry.outputs: Dict[port, TextureResult]`）。
2. **缩略图路径已实现「按 node_id 取任意节点输出」**（`composite_renderer.py:_handle_readback_node_thumbnail:744-819`）——只是它缩到 64×64 走 CPU readback，而非全分辨率 GL 纹理。
3. **`_node_cache_keys: Dict[node_id, cache_key]`** 提供了 node_id → 缓存的反查桥梁。
4. **`set_texture` / `frame_ready` 链路只消费 `TextureResult`**，不关心来自哪个节点——Viewer 完全不用改。

**所以本功能的核心是：在「永远取 Output」的链路上，加一条「可取任意节点」的分叉。**

---

## 2. 目标与非目标

### 2.1 目标

| # | 目标 | 验收标准 |
|---|------|---------|
| G1 | 选中任意节点能直接在 Viewer 预览其输出 | 选中 Scene3D（不连 Output）→ Viewer 显示它的渲染结果 |
| G2 | 预览可一键切换回 Output（整图） | 快捷键/按钮在「预览选中节点」与「预览 Output」间切换 |
| G3 | 3D 预览天然分离 | 3D 模式默认预览选中 Scene3D，网格随上下文开关，**无需 snapshot 注入 hack** |
| G4 | 中间节点可预览 | 选中 Merge/Transform 等中间节点 → Viewer 显示该节点输出 |
| G5 | 最小侵入 | 复用现有求值/缓存/stale/dirty/frame_ready 链路，不新建渲染线程 |

### 2.2 非目标（本期不做）

- ❌ 多 Viewer 同时显示不同节点（Nuke 的多 Viewer）。
- ❌ 节点对比视图（A/B split）。
- ❌ 预览时的独立参数覆盖（如预览时临时改某节点参数）。
- ❌ Edit/Color 页面的节点预览（仅限 Composite 页）。

---

## 3. 核心设计

### 3.1 双管线模型

```
                     ┌─────────────────┐
   用户选中节点 X     │   预览管线       │ → Viewer
   (preview_mode on) ─▶│  渲染到节点 X    │   (显示节点 X 输出)
                     └─────────────────┘

                     ┌─────────────────┐
   preview_mode off  │   输出管线       │ → Viewer
   (或选中 Output)  ─▶│  渲染整图        │   (显示 Output 结果)
                     └─────────────────┘
```

- **预览管线**：`_evaluate_snapshot` 返回**选中节点**的首个 image 输出端口结果。
- **输出管线**：`_evaluate_snapshot` 返回 **Output 节点**的 `display` 结果（现状）。
- 两条管线**共用同一套求值/缓存/调度**，区别仅在「最终取哪个节点的结果」。

### 3.2 关键数据流（预览选中节点）

```
[UI 线程]
  选中节点 X + 进入预览模式
    → page 设 _preview_node_id = "X"
    → page._evaluate_graph()
    → snapshot = graph.snapshot(...)
    → RenderRequest(snapshot, viewer_node_id="X", ...)
    → renderer.submit(EVALUATE_GRAPH, {request})

[渲染线程]
  _evaluate_snapshot(snapshot, viewer_node_id="X")
    → 遍历 eval_order 求值（所有节点照常求值+缓存）
    → ★ 若 viewer_node_id 指定：output_result = 节点 X 的首个 image 输出端口结果
      （取数逻辑同缩略图：_node_cache_keys[X] → cache.get(key).outputs[port]）
    → return output_result（节点 X 的 TextureResult）
  _render_to_output(result)   ← 不变，只消费 TextureResult
    → frame_ready.emit(req, tex_id, w, h)

[UI 线程]
  frame_ready → set_texture(tex_id, w, h)   ← 不变
    → Viewer 显示节点 X 输出
```

### 3.3 三处核心改动点（最小侵入）

调研确认，改动集中在 3 处，其余链路（`_render_to_output` / `frame_ready` / `set_texture`）零改动：

#### 改动 1：扩展 RenderRequest（`render_scheduler.py:109`）

```python
@dataclass
class RenderRequest:
    ...
    snapshot: GraphSnapshot
    request_id: int
    render_mode: str = RenderMode.PREVIEW
    # ★ 新增：预览指定节点（None = 预览 Output，现状行为）
    viewer_node_id: Optional[str] = None
    viewer_node_port: Optional[str] = None  # 显式指定端口；None = 自动取首个 image 输出
    ...
```

#### 改动 2：`_evaluate_snapshot` 增加 viewer_node 分支（`base_evaluator.py`）

在求值循环结束后、`return output_result`（656 行）之前，插入：

```python
# ★ 预览指定节点：优先返回 viewer_node_id 的输出，而非 Output 节点
if viewer_node_id:
    vn_outputs = cache.get(viewer_node_id)  # 本次求值已缓存该节点
    if vn_outputs:
        port = viewer_node_port or _first_image_port(snapshot.nodes[viewer_node_id])
        vn_result = vn_outputs.get(port)
        if vn_result and vn_result.is_valid:
            return vn_result
    # 兜底：若节点未求值（bypass/异常），退回 output_result
return output_result
```

**取数逻辑完全复用缩略图路径**（`composite_renderer.py:780-794`），无需新机制。

`_first_image_port` 是新增的小工具函数（参考 `composite_renderer.py:770-776`）：
```python
def _first_image_port(ns: NodeSnapshot) -> str:
    if ns.node_type == "output":
        return "display"
    if ns.output_ports:
        # 优先 image 类型
        for name, dtype in ns.output_ports:
            if dtype == "image":
                return name
        return ns.output_ports[0][0]
    return "result"
```

#### 改动 3：page 侧预览模式状态 + 触发（`composite_page.py`）

新增状态字段：
```python
self._preview_node_id: Optional[str] = None   # None = 预览 Output；否则预览该节点
```

在 `_do_evaluate_graph` 构造 RenderRequest 时填入：
```python
request = RenderRequest(
    snapshot=snapshot,
    ...
    viewer_node_id=self._preview_node_id,   # ★ 新增
)
```

预览模式的进入/退出（见 §4 交互设计）。

### 3.4 边界情况处理

| 情况 | 处理 |
|------|------|
| 目标节点不在 eval_order 里 | eval_order 是整图拓扑序，图中所有节点都在；除非节点被删除（snapshot 已不含）→ viewer_node_id 失效 → 兜底回 Output |
| 目标节点是 dynamic bypass（timeline/video 源） | 其 `_node_cache_keys` 被 pop，cache 查不到 → 该节点仍被求值（结果在临时 dict 里）→ **改动 2 用临时 `cache` dict 取**（已覆盖，因为求值循环里 `cache[node_id] = outputs` 对所有节点都执行） |
| 目标节点输出无效（texture_id<=0） | `vn_result.is_valid` 为假 → 兜底回 output_result |
| 目标节点无 image 输出端口 | `_first_image_port` 兜底返回首个端口或 "result" → 取不到 → 兜底回 output_result |
| 预览 Output 节点本身 | `viewer_node_id == Output 节点 id` → 等价于现状（取 display） |

> **重要**：改动 2 优先从**本次求值的临时 `cache` dict** 取（`base_evaluator.py:338` 声明的 `cache: Dict[node_id, Dict[port, TextureResult]]`），它对所有求值过的节点都有结果（含 bypass 节点）。这比查持久 CacheManager 更可靠（bypass 节点不进持久缓存）。

---

## 4. 交互设计

### 4.1 触发方式（待定，列候选）

| 方式 | 说明 | 类比 |
|------|------|------|
| **A. 选中节点自动预览** | 单击节点 → 自动进入预览模式显示该节点；点空白 → 回 Output | Fusion 默认行为 |
| **B. 选中 + 快捷键预览** | 选中节点后按 `P`（或数字键）→ 预览该节点；再按回 Output | Nuke 数字键 |
| **C. 节点右键菜单** | 右键节点 → "Preview in Viewer" | 通用 |

**推荐 A+B 组合**：选中自动预览（流畅），`Esc` 或点空白回 Output；高级用户可用快捷键。

> ⚠️ 注意：当前 `_on_node_selected`（`composite_page.py:1379`）**不触发渲染**。方案 A 需要在这里加预览触发（设 `_preview_node_id` + `_evaluate_graph`）。这会改变选中节点的行为，需评估对性能的影响（每次选中都重渲染）——可加防抖。

### 4.2 视觉反馈

- Viewer 角标/状态栏显示当前预览的节点名（如 "Previewing: Scene3D"），区分预览 vs 输出。
- 节点图上，正在预览的节点高亮（边框/角标）。

### 4.3 3D 模式与预览的关系

| 模式 | 显示来源 | 网格 |
|------|---------|------|
| 2D + 预览 Output | Output 纹理 | 无（合成输出干净） |
| 2D + 预览 Scene3D | Scene3D 纹理 | 无（用户可手动开 show_grid） |
| 3D + 预览 Scene3D | Scene3D 纹理 | **有**（show_grid 默认开，硬件深度遮挡） |
| 3D + 预览 Output | Output 纹理 | 无 |

**关键**：3D 模式 + 选中 Scene3D 时，自动切到「预览该 Scene3D」，网格随预览上下文开启——**彻底消除 snapshot 注入 hack**。预览管线和输出管线天然分离，网格开关是上下文属性，不再需要跨视图态注入。

---

## 5. 实施阶段（建议分 4 期，每期可独立验证）

### 阶段 1：渲染层支持 viewer_node（纯后端，无 UI）

**目标**：`_evaluate_snapshot` 能返回任意节点输出，但暂不改交互。

- [ ] `RenderRequest` 加 `viewer_node_id` / `viewer_node_port` 字段（render_scheduler.py）。
- [ ] `_evaluate_snapshot` 加 viewer_node 分支（base_evaluator.py），从临时 `cache` dict 取目标节点输出。
- [ ] 新增 `_first_image_port` 工具函数。
- [ ] 单元测试：mock snapshot，验证 viewer_node_id 指定时返回正确节点输出；None 时返回 Output。

**验证**：临时在 page 里硬编码 `_preview_node_id = 某 scene3d`，确认 Viewer 显示该节点。

### 阶段 2：page 侧预览状态 + 手动触发

**目标**：能用代码/快捷键切预览节点，Viewer 跟着变。

- [ ] page 加 `_preview_node_id` 状态。
- [ ] `_do_evaluate_graph` 填 `viewer_node_id`（5 个 snapshot 路径统一处理，参考 `_apply_view_mode_overrides` 模式）。
- [ ] 加快捷键 `P`（选中节点 → 预览；再按 → 回 Output）。
- [ ] Viewer 状态栏显示预览节点名。
- [ ] 处理选中节点变更/删除时的 `_preview_node_id` 同步。

**验证**：选中 Scene3D（不连 Output）按 P → Viewer 显示它；按 P 回 Output。

### 阶段 3：3D 模式自动预览 Scene3D + 网格根治

**目标**：3D 模式天然显示选中 Scene3D，网格随上下文开关，移除所有 hack。

- [ ] Tab 进 3D + 有选中 Scene3D → 自动 `_preview_node_id = 该 scene3d`。
- [ ] 移除 `composite_page._apply_view_mode_overrides` 里的 show_grid 注入 hack。
- [ ] Scene3D 烘焙网格（show_grid 默认 True）→ 3D 预览自带网格（硬件遮挡）；输出管线 show_grid 默认 False（合成干净）。
- [ ] 确认网格遮挡正常（硬件深度测试，不再跨 context）。

**验证**：Tab 进 3D → 直接看 Scene3D 渲染（带网格，模型遮挡网格）；切 2D → 回 Output（无网格）。

### 阶段 4：交互打磨 + 边界完善

**目标**：生产可用。

- [ ] 选中自动预览（方案 A）+ 防抖，评估性能。
- [ ] 预览节点删除/失效的优雅降级。
- [ ] 预览非 image 输出节点（如纯数值节点）的提示。
- [ ] 节点图预览节点高亮。
- [ ] 预览模式下参数调整的实时更新（改选中节点参数 → 预览刷新）。
- [ ] 缓存策略：预览节点切换时避免不必要的整图重算（目标节点多半已缓存）。

---

## 6. 涉及文件清单

| 文件 | 改动 | 阶段 |
|------|------|------|
| `render/render_scheduler.py` | RenderRequest 加 viewer_node_id/port | 1 |
| `evaluators/base_evaluator.py` | _evaluate_snapshot 加 viewer_node 分支 + _first_image_port | 1 |
| `ui/composite_page.py` | _preview_node_id 状态 + 触发 + snapshot 路径 | 2,3 |
| `ui/composite_viewport.py` | 状态栏显示预览节点名（可选） | 2 |
| `ui/node_editor_widget.py` | 预览节点高亮（可选） | 4 |
| 测试 | `tests/composite/test_node_preview.py`（新增） | 1-2 |

**不改**：`composite_renderer._render_to_output`、`frame_ready` 链路、`main_window_adapter`、`composite_controller`、`CompositeViewer.set_texture`、`paintGL`。

---

## 7. 风险与权衡

### 7.1 性能

- **选中即重渲染**：方案 A（自动预览）每次选中节点触发渲染。缓解：防抖（已有 `_eval_debounce_timer`）；目标节点多半已缓存（命中即跳过求值）。
- **预览节点切换不重算整图**：eval_order 不变，只是返回值换了节点——缓存全部复用，切换预览节点**接近零成本**（仅 blit 不同纹理）。

### 7.2 与现有 view_mode 的关系

- view_mode（2D/3D）是**交互态**（Alt 导航/gizmo），preview_node_id 是**显示来源**。两者正交：
  - 2D + 预览 Output = 现状
  - 3D + 预览 Scene3D = 新的 3D 工作流
  - 2D + 预览中间节点 = 调试工作流
- Tab 切 3D 时，**若选中 Scene3D 则自动预览它**（阶段 3），让 3D 模式真正显示 3D 内容。

### 7.3 dynamic bypass 节点

- timeline/video 源节点及其下游不进持久缓存。但改动 2 从**临时 `cache` dict** 取（求值循环内所有节点都写入），所以预览这类节点仍可用（只是每帧重算，符合预期——它们本来就是动态的）。

### 7.4 向后兼容

- `viewer_node_id=None` 时行为与现状完全一致（预览 Output）。
- 旧 `.tos` / 旧项目不受影响（preview_node_id 是运行时状态，不持久化）。

---

## 8. 开放问题（待讨论）

1. **触发方式**：自动预览（A）vs 快捷键（B）vs 两者？自动预览改变现有选中行为，需确认。
2. **3D 模式是否强制预览 Scene3D**：还是允许 3D 模式下仍预览 Output（看 3D 内容合成进整图的效果）？
3. **预览状态的持久化**：preview_node_id 是否随项目保存？还是每次启动默认 Output？（建议：不持久化，默认 Output）
4. **多端口节点**：少数节点可能有多个 image 输出（目前没有，但设计上 viewer_node_port 已预留）。
5. **与缩略图的关系**：预览功能上线后，缩略图逻辑可统一复用 `_first_image_port`，减少重复代码。

---

## 附录 A：关键代码位置速查

| 机制 | 文件:行 |
|------|---------|
| 求值主循环 | `evaluators/base_evaluator.py:358-577` |
| 临时结果 dict | `evaluators/base_evaluator.py:338` (`cache`) |
| Output 结果选取 | `evaluators/base_evaluator.py:516-518, 656` |
| 持久缓存写入 | `evaluators/base_evaluator.py:559-563` |
| node_id→cache_key 反查 | `evaluators/base_evaluator.py:326` (`_node_cache_keys`) |
| 缩略图取任意节点输出（样板） | `render/composite_renderer.py:744-819` |
| cache key 计算 | `render/cache_manager.py:178-280` |
| RenderRequest 定义 | `render/render_scheduler.py:109-151` |
| CompositeCommand 枚举 | `render/composite_renderer.py:59-71` |
| frame_ready 发射 | `render/composite_renderer.py:951` |
| frame_ready→viewer 链路 | `adapters/main_window_adapter.py:259-311` |
| Viewer.set_texture | `ui/composite_viewport.py:800-849` |
| 节点选中处理 | `ui/composite_page.py:1379-1431` |
| view_mode 切换 | `ui/composite_viewport.py:2668-2691, 3378-3392` |
| snapshot 构建（5 处） | `ui/composite_page.py:680, 743, 878, 1912, 2610` |

---

## 附录 B：为什么这能根治网格问题

之前的网格遮挡方案反复失败，根因是**3D 预览和合成输出共用一条管线**，导致：
- 网格（3D 专属辅助）和合成输出纠缠 → 需要 snapshot 注入 show_grid hack（脆弱，5 个路径都要改）。
- 视口叠加层方案要跨 context 采样深度纹理（UV 对齐/时序/生命周期/段崩溃）。

节点预览管线**分离了这两条管线**：
- 3D 预览管线：显示 Scene3D 节点输出，show_grid 默认开 → 网格和模型同 FBO，**硬件深度测试自动遮挡**，零跨 context。
- 输出管线：显示 Output，Scene3D 渲染时 show_grid 默认关 → 合成干净。

网格开关成为**上下文属性**（预览=开，输出=关），不再是跨视图态的 hack。这是架构层面的根治。
