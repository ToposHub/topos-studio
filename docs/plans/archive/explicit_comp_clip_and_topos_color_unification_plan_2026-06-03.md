# 显式 Comp Clip 与 Topos Color 统一调色合成实施计划

创建日期：2026-06-03
最后更新：2026-06-03（Phase 4 修正 + R-1~R-8 + Section 16 全部完成）

## 实施进度总览

| 阶段 | 状态 | Git Commit | 完成日期 |
|------|------|------------|----------|
| Phase 1 | ✅ 完成 | `0125c46` | 2026-06-03 |
| Phase 2 | ✅ 完成 | `a20fccd` | 2026-06-03 |
| Phase 3 | ✅ 完成 | `74a79c3` | 2026-06-03 |
| Phase 4 | ✅ 完成 | `d4991d5` | 2026-06-03 |
| Phase 5 | ✅ 完成 | `53b9876` | 2026-06-03 |
| Phase 6 | ✅ 完成 | `58849b4` | 2026-06-03 |
| Phase 7-9 | ✅ 完成 | `45f55c5` | 2026-06-03 |

**测试状态**: 3655 个测试全部通过 (0 失败) ✅

### 剩余功能完成状态 (R-1 ~ R-10 + Section 16)

| 编号 | 项目 | 状态 | 说明 |
|------|------|------|------|
| R-1 | Bypass Composite | ✅ 完成 | ClipInstance.composite_bypassed 字段、菜单切换、预览旁路逻辑 |
| R-2 | Render Cache This Clip | ✅ 完成 | RenderCacheSettings + RenderCacheManifest + 信号接线 |
| R-3 | Reveal Comp Clip in Media Pool | ✅ 完成 | reveal_asset() + 信号接线 + 状态栏提示 |
| R-4 | 旧 promote API 清理 | ✅ 完成 | 重命名为 _legacy_promote_composition_to_comp_clip，禁止生产调用 |
| R-5 | _pending_comp_clip_context 清理 | ✅ 完成 | 移除所有赋值引用，禁用 lazy promote 路径 |
| R-6 | ColorOutput legacy 路径清理 | ✅ 完成 | 自动迁移 color_input → topos_color，弃用注释 |
| R-7 | 页面切换性能优化 | ✅ 完成 | 消除 UI 线程同步 readback、缩略图延迟 300ms、切换计时日志 |
| R-8 | Comp Clip 失败策略 | ✅ 完成 | CompositeFailurePolicy + 错误 overlay + 预览渲染异常处理 |
| R-9 | Source Viewer GPU provider | ⏭ 跳过 | 需要深度架构重构，标记为后续独立任务 |
| R-10 | Program/Source Viewer zoom/pan | ✅ 完成 | PreviewViewTransform 共享类 + 锚点缩放 + 中键平移 |
| Sec 16 | Viewer 预览交互统一 | ✅ 完成 | resize 保持手动缩放、滚轮直接缩放、Source 新增 zoom/pan |

---

## 实施总结

### Phase 1: 停止自动创建 Comp Clip ✅
- 添加4个验证测试文件确认切页不修改项目
- `_ensure_temporary_composition_for_clip()` 已废弃
- `_pending_comp_clip_context` 总是 None

### Phase 2: 显式 Create Comp Clip ✅
- 修复 `remove_composite_binding` 信号链断裂
- 菜单文案已使用 "Create Comp Clip"
- `create_comp_clip_from_timeline_clip()` 正确使用真实 metadata

### Phase 3: Composite preview-only/editable 模式分离 ✅
- 删除残留的 `_ensure_comp_clip_for_current_composite()` 方法
- `_emit_user_edit_once()` 正确阻止 preview-only 编辑

### Phase 4: Topos Color graph 规范 ✅
- 更新测试预期：3节点 (Input → ToposColor → Output)
- timeline-backed graph 始终有 Topos Color 节点

### Phase 5: Color 页面同步 Topos Color 与缓存失效 ✅
- `_notify_composite_inputs_stale` 真正触发失效
- 调用 `composite_page._on_color_dirty()`

### Phase 6: Topos Color 复制/链接规则 ✅
- `paste_nodes` 增强获取 runtime_context
- 创建新的 color_stack_id 用于复制节点

### Phase 7-8: Edit 播放/导出/缓存打通 + Schema清理 ✅
- TimelineClipFrameProvider 已实现
- Schema 无旧语义字段

### Phase 9: 全量测试与验收 ✅
- 66个核心Comp Clip测试全部通过

---

目标读者：后续执行开发的 AI / 工程 Agent

适用范围：

- Edit 时间线片段进入 Composite 的交互规则
- Comp Clip 的创建、打开、重置、媒体池显示
- Color 页面与 Composite `Topos Color` 节点的统一数据模型
- `Topos Color` 节点复制到其它镜头时的调色预设 / 链接调色规则
- Edit / Color / Composite 播放、预览、导出、项目保存验证

技术栈：PyQt6 + ModernGL + GLSL

---

## 0. 核心结论

当前“从 Edit 切到 Composite 自动创建 Comp Clip”的方案应改为：

```text
普通时间线 clip:
  允许进入 Composite 只读预览
  不自动修改工程结构
  不自动创建 composition / comp clip asset

用户明确创建 Comp Clip 后:
  该 clip 才绑定 composite_id
  时间线显示 Comp 标签 / 颜色
  媒体池显示对应 Comp Clip 引用
  Composite 页面允许真实编辑并持久化 graph
```

原因：

1. 页面切换不应该产生工程结构修改。
2. 自动创建会让“预览、编辑、保存、撤销、媒体池、导出、渲染缓存”边界混乱。
3. 显式创建 Comp Clip 后，Edit / Color / Composite 都能以同一个 `clip_id` 为中心稳定工作。
4. Color 页面调色应等价于修改 Composite 图中的 `Topos Color` 节点所引用的 `ColorStackData`。

最终推荐渲染顺序：

```text
Timeline Clip Input(source_raw)
  -> Topos Color(color_stack_id = clip.color_stack_id, pass-through if no grade)
  -> Composite Effects
  -> Output
```

### 0.1 合并后代码审计结论（2026-06-03）

本节基于当前合并后的代码状态补充。后续 AI 执行时必须先按本节核对，不要按旧计划从零实现。

| 模块 | 当前已有实现 | 当前问题 | 计划调整 |
|---|---|---|---|
| Edit -> Composite 直接入口 | `src/app/controllers/page_controller.py::_auto_bind_composite_to_selected_clip()` 会调用 `_ensure_temporary_composition_for_clip()` | 仍然在“切页”时创建 `EditProject.compositions` 并写入 `clip.composite_id` | Phase 1 改为下线这条自动持久化路径，改成 preview-only 内存 graph |
| 右键 Open in Composite | `src/app/controllers/edit_controller.py::_on_open_in_composite_requested()` 会为普通 clip 创建 temporary composite | 右键“打开”仍然等价于修改项目，和显式 Create Comp Clip 冲突 | Phase 1 同步下线 temporary composite；普通 clip 打开只预览，编辑前提示创建 |
| `switch_from_edit_to_composite()` | `src/app/controllers/page_controller.py` 中仍会在无 `composite_id` 时调用 `_ensure_temporary_composition_for_clip()` | 菜单/快捷入口同样会自动创建 graph | Phase 1 必须覆盖这个入口 |
| 首次编辑自动 promote | `src/app/controllers/composite_controller.py::_on_composite_user_edit_started()` 和 `_flush_composite_to_edit_before_leave()` 会把 temporary graph promote 为 Comp Clip | 这是自动创建模型的尾巴，会保留“页面切换已写项目”的副作用 | Phase 3 改成“编辑前请求显式创建”，不再依赖 pending temporary context |
| Timeline 右键菜单 | `TimelineItem/TimelineView` 已有 `convert_to_comp_clip_requested/reset/bypass/reveal/render_cache` 信号 | 信号没有完整上接到 controller，菜单文案仍是 `Convert to Comp Clip`，不是最终 `Create Comp Clip` | Phase 2 不是新增菜单，而是补齐信号接线、命令实现、文案和禁用状态 |
| 媒体池 Comp Clip | `MediaPoolPanel` 已有 `open_comp_clip_in_composite_requested`、`create_empty_comp_clip_requested` | 空白 Comp Clip 是可保留基础；需确认打开时 runtime context 正确 | Phase 2 保留并纳入验收 |
| Comp Clip 数据模型 | `EditProject.create_empty_comp_clip()`、`promote_composition_to_comp_clip()`、`remove_comp_clip_from_timeline()`、`reset_comp_clip_graph()` 已存在 | `create_comp_clip_from_timeline_clip()` 当前用空的 `original_asset_id/path` 和默认时长，不能直接作为最终显式创建实现 | Phase 2 优先修正现有方法，使用真实 timeline/clip/asset/duration/color_stack |
| Timeline clip 的 `asset_id` | promote 路径会把 clip 的 `asset_id` 替换为 comp asset | 这会增加 TimelineClipInput 解回原始素材的复杂度，当前计划推荐 clip 保留原始 asset_id + composite_id | Phase 2 先做架构决策；若保留替换策略，必须补全 source_refs 和所有解包测试 |
| Topos Color 节点 | `topos_color_node.py`、`CompositeGraphData.create_default_for_clip(color_stack_id=...)`、`CompositePage.ensure_topos_color_node()` 已存在 | 只有有 `color_stack_id` 时才插入 Topos Color；计划要求 timeline-backed graph 始终有主 Topos Color，没调色时 pass-through | Phase 4 改为“强化规范化和自动修复”，不是从零新增节点 |
| Color -> Composite 同步 | `edit_controller.py` 有 ColorStack 变化后 dirty/同步相关代码，Composite evaluator 已有 `_apply_topos_color` | 需要确认 revision/cache invalidation 是否覆盖 Edit preview、Composite viewer、导出 | Phase 5 改为补齐失效链路和测试 |

当前最关键的结论：

```text
合并后的代码不是缺少 Comp Clip 功能，而是同时存在：
1. 自动 temporary composition / lazy promote 旧路径
2. 显式 Comp Clip / 媒体池 / Topos Color 新基础

下一步不是继续叠功能，而是收敛为唯一模型。
```

---

## 1. 产品规则

### 1.1 普通 clip 进入 Composite

用户在 Edit 时间线选中普通视频 / 图片 clip 后切换到 Composite。

预期行为：

- Composite 显示当前 clip 的只读预览图。
- 默认图可以临时显示为：

```text
Timeline Clip Input -> Topos Color -> Output
```

- 如果该 clip 还没有 `composite_id`，此图是临时预览图，不写入项目。
- 用户不能因为“切换页面”就生成新的 composition。
- 如果用户添加节点、删除节点、修改连接、修改参数、粘贴节点，应先触发明确创建动作。

推荐 UI：

```text
当前片段尚未创建 Comp Clip
[Create Comp Clip]
```

首次编辑时也可以弹出确认：

```text
This clip must be converted to a Comp Clip before editing.
[Create Comp Clip and Continue] [Cancel]
```

### 1.2 显式创建 Comp Clip

入口：

- Edit 时间线 clip 右键：`Create Comp Clip`
- Edit 时间线 clip 右键：`Open in Composite`，若未创建则先询问创建
- 媒体池：`New Comp Clip`
- 媒体池已有 Comp Clip 双击：打开 Composite

创建后：

- `ClipInstance.composite_id` 指向 Composite graph。
- `ClipInstance.color_stack_id` 保持原样，不重新创建无关调色栈。
- 时间线 clip 视觉上显示 `Comp` 标签和专属颜色。
- 媒体池新增 / 显示对应 Comp Clip 引用。
- Composite 页面进入真实编辑模式。

### 1.3 不推荐自动创建

禁止以下行为：

- 用户只是切换到 Composite 页面，系统就修改项目。
- 用户只是预览，系统就创建 `comp_clip` asset。
- 用户进入 Composite 后还没有任何编辑，保存项目却多出无用 composition。

### 1.4 Color 与 Composite 的关系

Color 页面不是独立视频入口，而是编辑 timeline clip 的 `ColorStackData`。

```text
Edit Clip.color_stack_id
        |
        v
Color Page UI edits ColorStackData
        |
        v
Topos Color node reads same color_stack_id
        |
        v
Composite output changes automatically
```

结论：

- Color 页面调色 = 修改当前 clip 的 `ColorStackData`。
- Composite `Topos Color` 节点只保存引用，不复制一份独立参数。
- Edit 播放和导出最终使用：

```text
source frame -> Topos Color -> composite nodes -> output
```

### 1.5 Topos Color 节点复制规则

普通复制 / 粘贴 `Topos Color` 节点到其它 clip 的 graph 时，默认行为必须是“复制调色预设”，不是共享原来的 `color_stack_id`。

默认复制：

```text
Shot A: Topos Color -> color_stack_A
Shot B: Topos Color -> color_stack_B
color_stack_B 参数从 color_stack_A 复制而来
```

高级链接复制：

```text
Shot A: Topos Color -> color_stack_A
Shot B: Topos Color -> color_stack_A
```

此模式必须显式触发，UI 必须显示 `Linked Color` 标签，并提供：

- `Unlink and Make Local`
- `Reveal Source Clip`
- `Replace with Current Clip Grade`

---

## 2. 最终数据模型

### 2.1 Clip 是三页共同身份

必须统一以 `timeline_id + clip_id` 作为视频调色和合成入口。

核心字段：

```python
ClipInstance.clip_id
ClipInstance.asset_id
ClipInstance.color_stack_id
ClipInstance.composite_id
ClipInstance.timeline_start
ClipInstance.timeline_duration
ClipInstance.source_start
ClipInstance.source_duration
ClipInstance.speed
```

### 2.2 Comp Clip 推荐存储策略

为了避免 Timeline Clip Input 找不到原始素材，推荐第一阶段采用以下稳定策略：

```text
时间线 clip:
  asset_id 保持原始 source asset
  composite_id 指向 graph

媒体池 Comp Clip:
  作为 graph 的引用 / alias
  metadata.graph_id = clip.composite_id
  metadata.source_clip_id = clip.clip_id
  metadata.source_timeline_id = timeline_id
  metadata.original_asset_id = clip.asset_id
```

不要在“从普通 clip 创建 Comp Clip”时立刻把该时间线 clip 的 `asset_id` 替换为 `comp_clip` asset，除非已经完整修好以下链路：

- `TimelineClipFrameProvider` 能稳定从 `source_refs.original_asset_id` 解回原始视频路径。
- Edit 预览 / 导出能识别 `comp_clip` asset 并跳转到 graph。
- Color 页面能在 `comp_clip` asset 上仍然定位到 timeline clip 的 `color_stack_id`。
- 媒体池拖拽 Comp Clip 到其它时间线时能正确生成新的 clip context。

若当前代码已经存在 `asset_id` 替换逻辑，本计划要求先做一次模型统一审查，并选择一种规则。推荐改成“timeline clip 保留原始 asset_id + composite_id”。

### 2.3 CompositeGraphData 必填字段

每个 timeline-backed graph 必须包含：

```json
{
  "graph_id": "...",
  "source_kind": "timeline_clip",
  "timeline_id": "...",
  "source_clip_id": "...",
  "source_asset_id": "...",
  "color_stack_id": "...",
  "width": 1920,
  "height": 1080,
  "fps": 24.0,
  "duration_frames": 120,
  "nodes": [],
  "connections": [],
  "metadata": {
    "created_from": "edit_context_menu",
    "is_persistent_comp_clip": true
  }
}
```

### 2.4 Topos Color 节点参数

节点类型：

```python
node_type = "topos_color"
node_title = "Topos Color"
```

输入输出：

```text
input:
  image

output:
  result
```

参数建议：

```json
{
  "stack_mode": "clip_stack",
  "color_stack_id": "",
  "source_timeline_id": "",
  "source_clip_id": "",
  "linked": false,
  "bypass": false,
  "intensity": 1.0
}
```

参数含义：

- `clip_stack`：默认，读取当前 timeline clip 的 `color_stack_id`。
- `preset_copy`：由复制 / 粘贴创建，参数已经复制进目标 clip 的本地 stack。
- `linked_stack`：高级模式，多个 clip 共享同一个 `color_stack_id`。
- `embedded`：可选，节点内部保存调色快照，第一阶段不推荐默认使用。

### 2.5 ColorStackData 仍然归 clip

`ColorStackData` 必须包含：

```python
timeline_id
clip_id
scope = "clip_local" | "linked_group" | "embedded"
source_kind = "timeline_clip"
revision
```

要求：

- 普通 clip 默认一个独立 `ColorStackData`。
- 创建 Comp Clip 不改变 `ColorStackData` 归属。
- 删除 `Topos Color` 节点不删除 `ColorStackData`。
- 删除 / 重置调色必须通过明确命令完成。

---

## 3. 实施阶段总览

| 阶段 | 目标 | 主要文件 | 验证重点 |
|---|---|---|---|
| Phase 1 | 下线合并后仍存在的 temporary composition 自动创建路径 | `page_controller.py`, `edit_controller.py`, `composite_controller.py` | 切页 / Open in Composite 不修改项目 |
| Phase 2 | 收敛现有右键与媒体池 Comp Clip 基础为显式 Create Comp Clip | `timeline_items.py`, `timeline_view.py`, `timeline_panel.py`, `edit_page.py`, `edit_controller.py`, `project.py` | 右键创建、真实元数据、媒体池显示、撤销/重置 |
| Phase 3 | Composite preview-only / editable 模式分离，移除 lazy promote 依赖 | `composite_page.py`, `node_editor_widget.py`, `composite_controller.py` | 未创建时不能持久编辑，首次编辑前明确创建 |
| Phase 4 | 强化 Topos Color graph 规范与自动修复 | `composite_graph_data.py`, `composite_page.py`, `topos_color_node.py` | timeline-backed graph 始终有主 Topos Color |
| Phase 5 | 补齐 Color 页面同步 Topos Color 与缓存失效 | `page_controller.py`, `edit_controller.py`, `color_controller.py`, `topos_color_node.py` | Color 调色后 Composite / Edit / Export 全部更新 |
| Phase 6 | Topos Color 复制 / 链接规则 | `node_editor_widget.py`, `topos_color_node.py`, `color_stack.py` | 默认复制为本地预设 |
| Phase 7 | Edit 播放 / 导出 / 缓存打通 | `timeline_preview_renderer.py`, `timeline_export.py`, `composite_frame_renderer.py` | 回 Edit 能看到合成效果 |
| Phase 8 | 项目 schema 清理 | `manifest.py`, `topos_project_io.py`, `save_router.py` | 保存文件无旧语义 |
| Phase 9 | 全量测试与验收 | `tests/*` | 自动测试 + 手工矩阵 |

---

## 4. Phase 1：停止自动创建 Comp Clip

### 4.1 目标

从 Edit 直接切到 Composite 或右键 `Open in Composite` 只能绑定当前 clip 预览，不创建持久 composition，不写 `clip.composite_id`，不创建 media pool Comp Clip。

合并后当前代码里仍有三条自动创建路径，本 Phase 必须全部处理：

| 路径 | 当前函数 | 必须改成 |
|---|---|---|
| 页面 tab 直接进入 Composite | `PageManagementMixin._auto_bind_composite_to_selected_clip()` | 普通 clip 只创建 preview-only runtime context |
| 菜单/快捷入口进入 Composite | `PageManagementMixin.switch_from_edit_to_composite()` | 普通 clip 不调用 `_ensure_temporary_composition_for_clip()` |
| 时间线右键 Open in Composite | `EditControllerMixin._on_open_in_composite_requested()` | 普通 clip 不创建 `EditProject.create_composition()` |

### 4.2 重点检查文件

- `src/app/controllers/page_controller.py`
- `src/app/controllers/composite_controller.py`
- `src/app/controllers/edit_controller.py`
- `src/features/composite/ui/composite_page.py`
- `src/features/edit/core/composite_graph_data.py`

### 4.3 实施步骤

1. 搜索所有自动创建入口：

```bash
rg -n "ensure_temporary|temporary composition|create_composition|with_composite_id|composite_user_edit_started|promote_composition_to_comp_clip|_pending_comp_clip_context" src/app src/features
```

2. 修改 `src/app/controllers/page_controller.py::_auto_bind_composite_to_selected_clip()`：

当前若存在 `_ensure_temporary_composition_for_clip()`，必须拆分为：

```python
if clip.composite_id:
    open_persistent_composite(clip)
else:
    open_readonly_composite_preview(clip)
```

禁止继续执行：

```python
composite_id = self._ensure_temporary_composition_for_clip(...)
new_project = self._current_edit_project.create_composition(...)
new_clip = clip.with_composite_id(composite_id)
timeline_view.load_timeline(new_timeline)
```

3. 修改 `src/app/controllers/page_controller.py::switch_from_edit_to_composite()`：

当前代码在 `not composite_id` 时仍会调用：

```python
composite_id = self._ensure_temporary_composition_for_clip(...)
```

必须改成：

```python
if not composite_id:
    composite_id = None
    open_mode = "preview_timeline_clip"
```

然后传入 context，但不写项目。

4. 修改 `src/app/controllers/edit_controller.py::_on_open_in_composite_requested()`：

当前普通 clip 会执行：

```python
composite_id = str(uuid.uuid4())
new_project = self._current_edit_project.create_composition(...)
new_clip = clip.with_composite_id(composite_id)
self._pending_comp_clip_context = {...}
```

这整段必须下线。普通 clip 的 `Open in Composite` 应只调用 page context：

```python
open_timeline_clip_in_composite_preview(timeline_id, clip_id, timeline_frame)
```

或通过现有 `switch_from_edit_to_composite(clip_id, create_new=False)`，但确保它不会创建 composition。

5. `_ensure_temporary_composition_for_clip()` 的处理：

推荐直接删除该函数。若为了过渡保留，必须改名为调试专用并禁止产品路径调用：

```python
def _create_debug_temporary_composition_for_tests_only(...):
    raise RuntimeError("Temporary composition creation is disabled")
```

6. `_pending_comp_clip_context` 的处理：

此字段是 lazy promote 旧模型的状态。Phase 1 后普通预览不能再写：

```python
self._pending_comp_clip_context = {...}
```

如果已有测试依赖它，应重写为 explicit create 流程。

7. 新增只读预览 context：

```python
CompositeRuntimeContext.for_timeline_clip(
    graph_id="",
    timeline_id=timeline_id,
    clip_id=clip.clip_id,
    source_frame=clip.timeline_to_source_frame(timeline_frame),
    input_stage="source_raw",
)
```

8. CompositePage 增加模式字段：

```python
self._timeline_preview_only = True
self._current_composite_id = ""
```

9. 未创建 Comp Clip 时，Composite 页面使用临时内存 graph：

```text
Timeline Clip Input -> Topos Color -> Output
```

要求：

- 不写入 `EditProject.compositions`
- 不写入 `.topos`
- 不设置 `clip.composite_id`
- 不创建 media pool comp asset

10. Composite 页面所有真实编辑入口调用：

```python
ensure_editable_comp_clip_before_mutation()
```

如果当前是 preview-only，则弹窗或状态栏提示创建。

### 4.4 验证方法

自动测试：

- 新增 `tests/edit/test_composite_direct_entry_does_not_mutate_project.py`
- 新增 `tests/edit/test_open_in_composite_does_not_create_temporary_composition.py`
- 新增 `tests/edit/test_switch_from_edit_to_composite_does_not_bind_composite_id.py`

测试点：

1. 创建一个 project、timeline、video clip。
2. 分别调用：
   - 直接切到 Composite tab
   - 时间线右键 `Open in Composite`
   - `switch_from_edit_to_composite()`
3. 断言：

```python
clip.composite_id is None
len(project.compositions) == 0
no comp_clip asset added
composite_page.is_preview_only is True
getattr(main_window, "_pending_comp_clip_context", None) is None
```

手工验证：

1. 导入视频到 Edit。
2. 选中 clip，切 Composite。
3. 关闭项目前检查媒体池没有新增 Comp Clip。
4. 保存 `.topos`，检查 JSON 中没有新增无用 composition。

---

## 5. Phase 2：显式 Create Comp Clip

### 5.1 目标

用户明确创建后，clip 才进入真实合成状态。

### 5.2 UI 入口

合并后当前代码已有以下菜单/信号基础，不要重复创建：

- `src/features/edit/ui/timeline_items.py` 已有 Composite 子菜单。
- `src/features/edit/ui/timeline_view.py` 已有：
  - `convert_to_comp_clip_requested`
  - `discard_composite_draft_requested`
  - `bypass_composite_requested`
  - `reset_composite_requested`
  - `reveal_comp_clip_in_media_pool_requested`
  - `render_cache_clip_requested`
- `src/features/edit/ui/media_pool_panel.py` 已有：
  - `open_comp_clip_in_composite_requested`
  - `create_empty_comp_clip_requested`

需要调整：

- 菜单文案从 `Convert to Comp Clip` 改为 `Create Comp Clip`。
- 未创建 Comp Clip 的普通 clip 不显示 `Discard Composite Draft`，因为 Phase 1 后不再存在 draft temporary composition。
- `Open in Composite` 对普通 clip 打开 preview-only；若用户要编辑，再提示创建。

普通 clip：

```text
Create Comp Clip
Open in Composite...
```

已创建 comp clip：

```text
Open in Composite
Reset Composite
Remove Composite Binding
Reveal Comp Clip in Media Pool
Render Cache This Clip
```

### 5.3 涉及文件

- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/ui/timeline_view.py`
- `src/features/edit/ui/timeline_panel.py`
- `src/features/edit/ui/edit_page.py`
- `src/app/controllers/edit_controller.py`
- `src/app/controllers/page_controller.py`
- `src/features/edit/core/commands.py`
- `src/features/edit/core/composite_graph_data.py`
- `src/features/edit/core/comp_clip_data.py`
- `src/features/edit/ui/media_pool_panel.py`

### 5.4 实施步骤

1. 补齐信号接线。

当前 `TimelineView` 已经有 Comp Clip 相关信号，但 `TimelinePanel` 只转发了 `open_in_color_requested`，未看到完整转发这些 Comp Clip 信号。必须：

- 在 `TimelinePanel` 定义同名 signal。
- 在 `TimelinePanel.__init__()` 中把 `timeline_view.convert_to_comp_clip_requested` 等信号转发出去。
- 在 `EditPage` 或 MainWindow signal hub 中继续转发给 `EditControllerMixin`。
- 在 `EditControllerMixin` 实现对应 handler。

推荐信号命名最终统一为：

```python
create_comp_clip_requested = pyqtSignal(str, str)          # clip_id, track_id
open_in_composite_requested = pyqtSignal(str, str)         # clip_id, track_id
reset_composite_requested = pyqtSignal(str, str)
remove_composite_binding_requested = pyqtSignal(str, str)
reveal_comp_clip_in_media_pool_requested = pyqtSignal(str, str)
render_cache_clip_requested = pyqtSignal(str, str)
```

如果为了兼容暂时保留 `convert_to_comp_clip_requested`，handler 内应转到 `create_comp_clip_for_timeline_clip()`。

2. 修正 `EditProject.create_comp_clip_from_timeline_clip()`。

当前此方法存在严重占位数据：

```python
original_asset_id = ""
original_asset_path = ""
clip_duration = 240
width = 1920
height = 1080
fps = 24.0
```

必须改为真实读取：

- 从 `self.timelines` 或调用方传入的 `TimelineSequence` 获取 clip。
- 从 `clip.asset_id` 获取原始 MediaAsset。
- `duration_frames = clip.timeline_duration`。
- `width/height/fps` 优先 timeline，其次 asset metadata，其次项目默认。
- `color_stack_id = clip.color_stack_id`，若为空则调用 `_ensure_clip_has_color_stack()` 或由 controller 先创建。
- `source_refs.original_asset_id = clip.asset_id`。
- `source_refs.original_clip_id = clip.clip_id`。
- `source_refs.original_timeline_id = timeline_id`。

建议函数签名升级为：

```python
create_comp_clip_from_timeline_clip(
    timeline_id: str,
    clip_id: str,
    timeline: TimelineSequence,
    name: str | None = None,
    keep_original_asset_id_on_timeline: bool = True,
) -> tuple[EditProject, TimelineSequence, str, str, str]
```

返回值中必须包含更新后的 `TimelineSequence`，不要让 controller 手动重复 patch clip。

3. 在 `EditControllerMixin` 实现显式创建：

```python
create_comp_clip_for_timeline_clip(timeline_id, clip_id) -> str
```

具体步骤：

- 获取 timeline / track / clip。
- 确保 clip 有 `color_stack_id`。
- 调用修正后的 `EditProject.create_comp_clip_from_timeline_clip(...)`。
- graph 必须包含 `Timeline Clip Input -> Topos Color -> Output`。
- 写入 `project.compositions[graph_id]`、`project.comp_clips[comp_clip_id]`、`project.assets[comp_asset_id]`。
- 更新 clip：`clip.composite_id = graph_id`。
- 创建或更新媒体池 Comp Clip 引用。
- 推入 undo command。
- 刷新 timeline view 和 media pool。

关于 `clip.asset_id`：

推荐第一阶段保留原始素材 `asset_id`，只设置 `clip.composite_id`。这样 Color 页面、TimelineClipInput、原素材解码都更稳定。

```python
new_clip = clip.with_composite_id(graph_id)
```

如果团队坚持把 `clip.asset_id` 改成 `comp_asset_id`，必须同时补齐所有解包路径测试：

- `TimelineClipFrameProvider._resolve_media_asset_path()`
- `TimelinePreviewRenderer`
- `TimelineExport`
- Color 页面打开 timeline clip
- 保存重开后 source_refs 还原

4. 媒体池 Comp Clip 引用 metadata：

```json
{
  "media_type": "comp_clip",
  "path": "topos://comp_clip/<comp_asset_id>",
  "metadata": {
    "comp_clip_id": "<comp_clip_id>",
    "graph_id": "<graph_id>",
    "source_kind": "timeline_clip",
    "source_timeline_id": "<timeline_id>",
    "source_clip_id": "<clip_id>",
    "original_asset_id": "<asset_id>"
  }
}
```

5. `Open in Composite`：

- 如果 clip 没有 `composite_id`：打开 preview-only，不创建项目数据。
- 如果用户在 preview-only 中尝试编辑：弹出创建确认。
- 如果有：加载 graph 并进入 Composite。

6. `Reset Composite`：

- 保留 graph 和 Comp Clip 身份。
- 重置节点为：

```text
Timeline Clip Input -> Topos Color -> Output
```

- 不删除 `color_stack_id`。
- 使用已存在的 `EditProject.reset_comp_clip_graph()` 前必须修正它：timeline 来源 graph 重置时也要插入主 `Topos Color`，不能只重置为 `TimelineClipInput -> Output`。

7. `Remove Composite Binding`：

- 从 clip 移除 `composite_id`。
- 可选择保留 graph/media pool item 到“Unused Comps”，或删除。第一阶段建议弹窗询问：

```text
Remove binding only
Delete Comp Clip graph
Cancel
```

8. 删除旧 lazy promote 入口：

显式创建完成后，以下路径不应再负责创建 Comp Clip：

- `CompositeControllerMixin._on_composite_user_edit_started()`
- `CompositeControllerMixin._flush_composite_to_edit_before_leave()` 中的 promote 分支
- `EditControllerMixin._ensure_comp_clip_for_current_composite()`

这些函数可以暂时保留为兼容日志，但不能在普通预览编辑时静默执行项目修改。首次编辑应走 Phase 3 的创建确认。

### 5.5 验证方法

自动测试：

- `tests/edit/test_create_comp_clip_explicit.py`
- `tests/edit/test_create_comp_clip_uses_real_clip_metadata.py`
- `tests/edit/test_timeline_panel_forwards_comp_clip_menu_signals.py`
- `tests/edit/test_remove_comp_clip_binding.py`
- `tests/edit/test_reset_comp_clip_keeps_color_stack.py`
- `tests/edit/test_reset_comp_clip_graph_keeps_topos_color.py`

断言：

```python
before: clip.composite_id is None
create -> clip.composite_id == graph_id
graph.source_asset_id == original_asset.asset_id
graph.duration_frames == clip.timeline_duration
graph has timeline_clip_input
graph has topos_color
graph has output
project has comp_clip media asset
clip.color_stack_id unchanged
ToposColor.color_stack_id == clip.color_stack_id
undo -> clip.composite_id is None
redo -> clip.composite_id == graph_id
```

手工验证：

1. 右键普通 clip，创建 Comp Clip。
2. 时间线片段出现 `Comp` 标签。
3. 媒体池出现 Comp Clip。
4. 双击媒体池 Comp Clip 能进入 Composite。
5. Reset 后画面仍然显示原素材 + 调色。

---

## 6. Phase 3：Composite 只读预览与编辑模式分离

### 6.1 目标

Composite 页面必须知道当前 graph 是否可编辑。

合并后当前代码已经会监听 graph change 并通过：

- `CompositeControllerMixin._on_composite_user_edit_started()`
- `CompositeControllerMixin._flush_composite_to_edit_before_leave()`
- `_pending_comp_clip_context`

把临时 graph 自动 promote 成 Comp Clip。Phase 3 的目标是用明确的 preview-only / editable 模式替代这套 lazy promote。

### 6.2 状态设计

```python
class CompositeOpenMode:
    PREVIEW_TIMELINE_CLIP = "preview_timeline_clip"
    EDIT_COMP_CLIP = "edit_comp_clip"
    EDIT_EMPTY_COMP = "edit_empty_comp"
```

CompositePage 字段：

```python
self._open_mode
self._timeline_preview_only
self._current_composite_id
self._runtime_context
```

### 6.3 实施步骤

1. `CompositePage.load_preview_for_timeline_clip(context)`：

- 创建临时 graph。
- 设置 preview-only。
- 禁用保存 graph changed。
- `_current_composite_id = ""`。
- 不能向 `EditProject.compositions` 写入 graph。

2. `CompositePage.load_composite_data(graph)`：

- 设置 editable。
- 加载持久 graph。
- `_current_composite_id = graph.composite_id`。
- 允许 `composite_graph_changed` 同步项目。

3. 所有会改变 graph 的入口统一调用：

```python
def _ensure_can_mutate_graph(self) -> bool:
    if not self._timeline_preview_only:
        return True
    self.request_create_comp_clip.emit(current_clip_id)
    return False
```

需要覆盖：

- 添加节点
- 删除节点
- 连接 / 断开连接
- 修改节点参数
- 粘贴节点
- 自动 layout 修改
- group / ungroup
- 添加 roto / mask

4. 替换当前 lazy promote。

当前代码中以下行为应被禁用：

```python
_on_composite_user_edit_started(composite_id)  # 不再自动 promote 普通 preview
_flush_composite_to_edit_before_leave()        # 不再把 preview graph promote
_pending_comp_clip_context                     # 不再作为创建依据
```

新的逻辑：

```text
preview-only graph mutation requested
  -> emit request_create_comp_clip(clip_id)
  -> controller 调用显式 create_comp_clip_for_timeline_clip()
  -> CompositePage reload persistent graph
  -> 用户继续编辑
```

5. 如果用户确认创建 Comp Clip：

- controller 创建 graph。
- CompositePage 从 preview graph 切换为 persistent graph。
- 把用户刚才想执行的动作重新执行一次。

第一阶段可简化：确认创建后不重放动作，只提示用户再点一次。

### 6.4 验证方法

自动测试：

- `tests/composite/test_preview_mode_does_not_emit_graph_changed.py`
- `tests/composite/test_preview_mode_mutation_requests_comp_clip_creation.py`
- `tests/composite/test_preview_mode_leave_does_not_promote_comp_clip.py`
- `tests/composite/test_editable_comp_clip_graph_changed_persists.py`

手工验证：

1. 普通 clip 进 Composite。
2. 添加 Blur。
3. 应提示创建 Comp Clip。
4. 取消后项目无变化。
5. 确认后项目创建 Comp Clip，并允许添加 Blur。

---

## 7. Phase 4：Topos Color graph 规范与自动修复

### 7.1 目标

所有 timeline-backed Composite graph 都必须有一个主 `Topos Color` 节点。

合并后当前实现状态：

- `src/features/composite/nodes/topos_color_node.py` 已存在。
- `src/features/composite/ui/composite_page.py::ensure_topos_color_node()` 已存在。
- `src/features/edit/core/composite_graph_data.py::create_default_for_clip()` 只有在 `color_stack_id` 有值时才插入 Topos Color。

需要调整为：

```text
timeline-backed graph 始终有主 Topos Color。
没有 color_stack_id 时也创建 Topos Color，但它必须 pass-through。
进入 Color 页面创建 color_stack_id 后，更新同一个主 Topos Color 的引用。
```

标准图：

```text
Timeline Clip Input -> Topos Color -> Output
```

有特效时：

```text
Timeline Clip Input -> Topos Color -> Blur -> Glow -> Output
```

### 7.2 涉及文件

- `src/features/edit/core/composite_graph_data.py`
- `src/features/composite/nodes/topos_color_node.py`
- `src/features/composite/ui/composite_page.py`
- `src/features/composite/core/node_engine.py`
- `src/features/composite/evaluators/color_evaluator.py`
- `src/features/composite/evaluators/base_evaluator.py`

### 7.3 实施步骤

1. 实现 graph 规范化函数：

```python
ensure_timeline_graph_has_topos_color(graph, timeline_id, clip_id, color_stack_id)
```

规则：

- 如果没有 `timeline_clip_input`，创建一个。
- 如果没有 `output`，创建一个。
- 如果没有主 `topos_color`，插入到 `timeline_clip_input` 后面。
- 如果 `color_stack_id` 为空，仍然插入主 `topos_color`，参数为：

```json
{
  "stack_source": "clip_stack",
  "color_stack_id": "",
  "bypass": false,
  "intensity": 1.0,
  "role": "primary_clip_grade"
}
```

Evaluator 在找不到 stack 时必须 pass-through，而不是失败或返回黑画面。
- 如果已有 `timeline_clip_input -> Output`，改为：

```text
timeline_clip_input -> topos_color -> output
```

- 如果已有：

```text
timeline_clip_input -> FirstEffect -> ...
```

改为：

```text
timeline_clip_input -> topos_color -> FirstEffect -> ...
```

2. `Topos Color` 主节点识别：

```json
{
  "role": "primary_clip_grade",
  "source_clip_id": "<clip_id>",
  "color_stack_id": "<clip.color_stack_id>"
}
```

3. 创建 Comp Clip 时必须调用规范化函数。

4. `CompositeGraphData.create_default_for_clip()` 必须改为始终创建 Topos Color。

当前注释和实现是：

```text
无调色：TimelineClipInput -> Output
有调色：TimelineClipInput -> Topos Color -> Output
```

应改为：

```text
始终：TimelineClipInput -> Topos Color -> Output
```

5. 进入 Color 页面时，如果 clip 有 `composite_id`，也调用一次规范化函数，确保 Color 调色能影响 Composite。

6. 加载旧 / 半成品 graph 时调用修复：

```python
graph = repair_timeline_backed_graph(graph, context)
```

7. 禁止 `Timeline Clip Input.input_stage="after_color"` 作为主链路默认值。第一阶段统一：

```json
input_stage = "source_raw"
```

调色只能通过 `Topos Color` 表达。

### 7.4 验证方法

自动测试：

- `tests/composite/test_timeline_graph_topos_color_repair.py`
- `tests/composite/test_default_clip_graph_always_has_topos_color.py`
- `tests/composite/test_topos_color_missing_stack_passes_through.py`

用例：

1. 空 graph -> 创建三节点。
2. `Timeline Clip Input -> Output` -> 插入 Topos Color。
3. `Timeline Clip Input -> Blur -> Output` -> 插入 Topos Color 到 Blur 前。
4. 已有主 Topos Color -> 不重复创建。
5. 多个 Topos Color -> 保留，只有 role=`primary_clip_grade` 的负责 Color 页面同步。

手工验证：

1. 创建 Comp Clip。
2. 打开 Composite。
3. 节点图必定看到 `Timeline Clip Input -> Topos Color -> Output`。
4. 删除 Topos Color 后重新进入 Color，应自动修复或提示恢复主调色节点。

---

## 8. Phase 5：Color 页面同步 Topos Color

### 8.1 目标

用户进入 Color 页面调色时，实际上修改的是当前 clip 的 `ColorStackData`，Composite `Topos Color` 节点自动读取同一份数据。

### 8.2 涉及文件

- `src/app/controllers/page_controller.py`
- `src/app/controllers/color_controller.py`
- `src/features/color/adapters/color_stack_hydrator.py`
- `src/features/color/core/color_stack_render_service.py`
- `src/features/composite/nodes/topos_color_node.py`
- `src/features/composite/evaluators/color_evaluator.py`
- `src/shared/project/color_stack.py`

### 8.3 实施步骤

1. `open_timeline_clip_in_color()` 中确保：

```python
clip.color_stack_id exists
ColorStackData.timeline_id == timeline_id
ColorStackData.clip_id == clip_id
```

2. 如果 clip 有 `composite_id`：

- 加载 graph。
- 调用 `ensure_timeline_graph_has_topos_color(...)`。
- 主 `Topos Color.color_stack_id = clip.color_stack_id`。

3. Color 页面任何参数变化后：

- 更新 `ColorStackData.revision`。
- 发出事件：

```python
color_stack_changed(color_stack_id, revision)
```

4. CompositePage 监听 ColorStack 变化：

- 如果当前 graph 使用该 stack，标记 graph dirty。
- 清除 Topos Color 节点缓存。
- 触发 `_evaluate_graph()`。

5. Edit 预览 / 导出监听同一 revision：

- 如果 clip 有 `composite_id`，使 composite render cache 失效。

6. `ToposColorNode` 不保存完整调色参数副本。它只保存引用：

```python
color_stack_id
source_clip_id
source_timeline_id
```

### 8.4 验证方法

自动测试：

- `tests/color/test_color_updates_topos_color_node.py`
- `tests/composite/test_topos_color_reads_color_stack_revision.py`
- `tests/edit/test_comp_clip_color_change_invalidates_preview_cache.py`

手工验证：

1. 创建 Comp Clip，打开 Composite，确认有 Topos Color。
2. 回 Color 调整曝光 / 对比度。
3. 回 Composite，画面应带调色结果。
4. 回 Edit，时间线预览应显示调色 + 合成。
5. 导出一帧截图，对比 Color + Composite 结果一致。

---

## 9. Phase 6：Topos Color 复制、粘贴、链接调色

### 9.1 默认复制为本地调色预设

用户复制 Shot A 的 `Topos Color` 节点，粘贴到 Shot B 的 Composite graph。

默认行为：

- 读取 Shot A 的 `ColorStackData`。
- 确保 Shot B 有自己的 `ColorStackData`。
- 将 A 的调色参数复制到 B 的 stack。
- 粘贴出的 `Topos Color` 指向 B 的 stack。
- 不共享 `color_stack_id`。

### 9.2 显式链接调色

新增右键菜单：

- `Paste Topos Color as Local Grade`
- `Paste Topos Color as Linked Grade`
- `Link to Existing Color Stack...`
- `Unlink and Make Local`

链接模式要求：

- Node UI 显示 `Linked Color`。
- Inspector 显示来源 clip 名称。
- 修改该 stack 前提示：

```text
This grade is linked to multiple clips.
Changes will affect all linked clips.
```

### 9.3 涉及文件

- `src/features/composite/ui/node_editor_widget.py`
- `src/features/composite/ui/node_item.py`
- `src/features/composite/ui/node_properties_panel.py`
- `src/features/composite/nodes/topos_color_node.py`
- `src/shared/project/color_stack.py`
- `src/app/controllers/color_controller.py`

### 9.4 实施步骤

1. 扩展 copy payload：

```json
{
  "node_type": "topos_color",
  "copy_kind": "grade_reference",
  "source_color_stack_id": "...",
  "source_clip_id": "...",
  "source_timeline_id": "..."
}
```

2. Paste 时根据当前 Composite runtime context 获取目标：

```python
target_timeline_id
target_clip_id
target_color_stack_id
```

3. 默认 Paste：

```python
source_stack = project.color_stacks[source_color_stack_id]
target_stack = ensure_color_stack(target_clip_id)
target_stack.copy_grade_params_from(source_stack)
new_node.color_stack_id = target_stack.stack_id
new_node.stack_mode = "preset_copy"
new_node.linked = False
```

4. Linked Paste：

```python
new_node.color_stack_id = source_color_stack_id
new_node.stack_mode = "linked_stack"
new_node.linked = True
```

5. `Unlink and Make Local`：

- 创建当前 clip 独立 stack。
- 从 linked stack 复制参数。
- node 指向新 stack。
- `linked=False`。

6. UI 标签：

- 主 clip grade: `Topos Color`
- preset copy: `Topos Color Preset`
- linked: `Linked Color`

### 9.5 验证方法

自动测试：

- `tests/composite/test_topos_color_copy_as_local_preset.py`
- `tests/composite/test_topos_color_copy_as_linked_grade.py`
- `tests/color/test_linked_grade_unlink_make_local.py`

断言：

```python
local paste: shot_b.color_stack_id != shot_a.color_stack_id
local paste: params equal after paste
local paste: changing B does not change A

linked paste: node_b.color_stack_id == shot_a.color_stack_id
linked paste: changing linked stack affects both
unlink: node_b.color_stack_id != shot_a.color_stack_id
```

手工验证：

1. Shot A 调色。
2. 复制 A 的 Topos Color 到 Shot B。
3. 调整 Shot B，Shot A 不变。
4. 使用 linked paste，再调整，两个镜头一起变化。
5. Unlink 后再次调整，两个镜头分离。

---

## 10. Phase 7：Edit 播放、导出、渲染缓存

### 10.1 目标

回到 Edit 后，Comp Clip 必须显示最终合成结果。

最终渲染：

```text
Edit timeline frame
  -> resolve active clip
  -> source frame
  -> if clip.composite_id:
       CompositeFrameRenderer(graph, runtime_context)
     else:
       normal timeline renderer
```

### 10.2 涉及文件

- `src/features/edit/core/timeline_preview_renderer.py`
- `src/features/edit/core/timeline_export.py`
- `src/features/composite/render/composite_frame_renderer.py`
- `src/features/edit/core/timeline_clip_frame_provider.py`
- `src/features/edit/core/timeline_render_plan.py`

### 10.3 实施步骤

1. Timeline render plan 增加 clip 标记：

```python
layer.has_composite = bool(clip.composite_id)
layer.composite_id = clip.composite_id
layer.color_stack_id = clip.color_stack_id
```

2. PreviewRenderer 遇到 composite clip：

```python
image = composite_frame_renderer.render_frame(
    graph_id=clip.composite_id,
    timeline_id=timeline_id,
    clip_id=clip.clip_id,
    timeline_frame=frame,
    source_frame=clip.timeline_to_source_frame(frame),
    color_stack_id=clip.color_stack_id,
)
```

3. 不允许在 persistent comp clip 渲染失败时静默显示原素材。推荐：

- 预览中显示 error overlay，并记录日志。
- 导出中失败则中止或标记错误。

开发阶段可以加 config：

```python
allow_composite_fallback_to_source = False
```

4. Render cache key 必须包含：

```text
timeline_id
clip_id
composite_id
graph_revision
color_stack_id
color_stack_revision
source_frame
render_quality
timeline_fps
output_resolution
```

5. Color 参数变化后，清理相关 composite cache。

6. Composite graph 变化后，清理相关 edit preview cache。

### 10.4 验证方法

自动测试：

- `tests/edit/test_comp_clip_timeline_preview_uses_composite_output.py`
- `tests/edit/test_comp_clip_export_uses_composite_output.py`
- `tests/edit/test_comp_clip_cache_invalidates_on_color_change.py`
- `tests/edit/test_comp_clip_cache_invalidates_on_graph_change.py`

手工验证：

1. 创建 Comp Clip。
2. Composite 添加明显效果，例如 Invert / Blur / Transform。
3. 回 Edit，预览显示效果。
4. Color 调色后回 Edit，预览显示调色 + 合成。
5. 导出短片，逐帧检查结果一致。

---

## 11. Phase 8：项目工程文件 schema 清理

### 11.1 目标

`.topos` 工程中只保留新的清晰模型。

### 11.2 要删除 / 禁止的旧语义

- video standalone project
- Composite 页面单独打开 video 并创建自己的 timeline
- `project_mode="single_asset"` 用于视频
- 新 graph 默认 `COLOR -> Output`
- 页面切换产生 temporary persistent composition
- `Topos Color` 节点保存一份和 ColorStack 脱离的调色副本

### 11.3 推荐 schema

```json
{
  "schema_version": 3,
  "project_mode": "edit_project",
  "active_timeline_id": "...",
  "assets": {},
  "timelines": {},
  "color_stacks": {},
  "compositions": {},
  "comp_clip_assets": {}
}
```

### 11.4 涉及文件

- `src/shared/project/manifest.py`
- `src/shared/project/topos_project_io.py`
- `src/shared/project/save_router.py`
- `src/features/edit/core/project.py`
- `src/features/edit/core/project_serializer.py`
- `src/features/edit/core/composite_graph_data.py`
- `tests/project/*`

### 11.5 实施步骤

1. 升级 manifest schema。

2. 保存时校验：

```python
if asset.is_video and project_mode != "edit_project":
    reject
```

3. 保存前校验 timeline-backed graph：

```python
assert graph.source_kind == "timeline_clip"
assert graph.source_clip_id
assert graph has timeline_clip_input
assert graph has primary topos_color
assert graph has output
```

4. 移除或拒绝旧 `single_asset video`。

5. 若不考虑旧项目兼容，可以直接 fail fast：

```python
raise ValueError("Legacy standalone video project is unsupported")
```

6. 提供开发期检查脚本：

```bash
python tools/validate_topos_project_schema.py path/to/project.topos
```

### 11.6 验证方法

自动测试：

- `tests/project/test_schema_v3_rejects_video_single_asset.py`
- `tests/project/test_schema_v3_saves_comp_clip_graph.py`
- `tests/project/test_schema_v3_requires_topos_color_for_timeline_graph.py`

手工验证：

1. 创建包含普通 clip、调色 clip、comp clip 的项目。
2. 保存。
3. 重新打开。
4. Edit / Color / Composite 三页都能定位同一个 clip。
5. JSON 中没有旧 `COLOR -> Output` 默认图。

---

## 12. Phase 9：验收矩阵

### 12.1 自动化测试清单

至少新增 / 更新以下测试：

```text
tests/edit/test_composite_direct_entry_does_not_mutate_project.py
tests/edit/test_create_comp_clip_explicit.py
tests/edit/test_remove_comp_clip_binding.py
tests/edit/test_reset_comp_clip_keeps_color_stack.py
tests/composite/test_preview_mode_does_not_emit_graph_changed.py
tests/composite/test_preview_mode_mutation_requests_comp_clip_creation.py
tests/composite/test_timeline_graph_topos_color_repair.py
tests/color/test_color_updates_topos_color_node.py
tests/composite/test_topos_color_reads_color_stack_revision.py
tests/edit/test_comp_clip_color_change_invalidates_preview_cache.py
tests/composite/test_topos_color_copy_as_local_preset.py
tests/composite/test_topos_color_copy_as_linked_grade.py
tests/color/test_linked_grade_unlink_make_local.py
tests/edit/test_comp_clip_timeline_preview_uses_composite_output.py
tests/edit/test_comp_clip_export_uses_composite_output.py
tests/project/test_schema_v3_rejects_video_single_asset.py
tests/project/test_schema_v3_saves_comp_clip_graph.py
```

每阶段完成后运行：

```bash
.venv/bin/python -m pytest tests/edit tests/composite tests/color tests/project -q
```

### 12.2 手工验收用例

#### 用例 A：普通 clip 只读预览

步骤：

1. 导入视频。
2. 放入 Edit 时间线。
3. 不创建 Comp Clip，直接切 Composite。
4. 查看画面。
5. 保存项目。

通过标准：

- Composite 有画面。
- 项目没有新增 composition。
- 时间线 clip 没有 `composite_id`。
- 媒体池没有新增 Comp Clip。

#### 用例 B：显式创建 Comp Clip

步骤：

1. 右键 clip -> `Create Comp Clip`。
2. 打开 Composite。
3. 添加 Blur。
4. 回 Edit 播放。

通过标准：

- 时间线 clip 显示 `Comp` 标签。
- 媒体池显示 Comp Clip。
- Composite graph 有 `Timeline Clip Input -> Topos Color -> Blur -> Output`。
- Edit 预览显示 Blur 效果。

#### 用例 C：Color 修改 Topos Color

步骤：

1. 对已创建 Comp Clip 的 clip 进入 Color。
2. 调整曝光 / 饱和度。
3. 回 Composite。
4. 回 Edit。

通过标准：

- Composite 的 Topos Color 节点引用同一个 `color_stack_id`。
- Composite 预览显示新的调色结果。
- Edit 显示调色 + 合成。
- 没有创建第二套 ColorStack。

#### 用例 D：复制 Topos Color 为本地预设

步骤：

1. Shot A 调色。
2. 复制 A 的 Topos Color。
3. Shot B 创建 Comp Clip。
4. 粘贴到 B。
5. 修改 B 的调色。

通过标准：

- B 获得和 A 一样的初始调色。
- B 的 `color_stack_id` 不等于 A。
- 修改 B 不影响 A。

#### 用例 E：链接调色

步骤：

1. Shot A 调色。
2. Shot B 使用 `Paste as Linked Grade`。
3. 修改 linked grade。
4. 对 B 执行 `Unlink and Make Local`。

通过标准：

- 链接阶段 A/B 一起变化。
- UI 显示 `Linked Color`。
- Unlink 后 A/B 分离。

#### 用例 F：保存重开

步骤：

1. 项目包含：
   - 普通视频 clip
   - 调色 clip
   - Comp Clip
   - linked grade clip
2. 保存 `.topos`。
3. 重新打开。

通过标准：

- 每个 clip 的 Color / Composite 页面打开正确。
- Comp Clip 媒体池项存在。
- linked grade 关系仍然清晰。
- Edit 播放和导出结果一致。

---

## 13. 执行顺序建议

不要一次性大改全部。建议严格按顺序：

1. 先完成 Phase 1，把三条自动 temporary composition 路径全部下线：
   - `_auto_bind_composite_to_selected_clip()`
   - `switch_from_edit_to_composite()`
   - `_on_open_in_composite_requested()`
2. 再完成 Phase 2，接通已存在的右键信号，并修正 `create_comp_clip_from_timeline_clip()` 的真实元数据。
3. 再完成 Phase 3，用 preview-only / editable 模式替代 `_pending_comp_clip_context` 和 lazy promote。
4. 再完成 Phase 4，把 Topos Color 固定为 timeline-backed graph 的强制主节点。
5. 再完成 Phase 5，打通 ColorStack revision -> Topos Color -> Composite/Edit cache invalidation。
6. 再完成 Phase 7，保证 Edit 预览 / 导出显示最终结果。
7. 最后做 Phase 6 的复制 / 链接高级功能。
8. schema 清理应在核心行为稳定后做，避免迁移中反复改格式。

合并后当前代码里已有较多半成品功能，后续 AI 每做一个 Phase 都要先跑：

```bash
rg -n "ensure_temporary|temporary composition|_pending_comp_clip_context|promote_composition_to_comp_clip|with_asset_id\\(comp_asset_id\\)" src/app src/features
```

Phase 1-3 完成后，普通预览路径中不应再出现这些自动创建 / 自动 promote 调用。

每完成一个 Phase，必须：

- 跑对应自动测试。
- 手工执行最小验收。
- 保存一个小 `.topos` 样例并重开验证。
- 不允许靠 debug placeholder 代替真实画面。

---

## 14. 给后续 AI 的关键提醒

1. 不要把页面切换写成项目修改。
2. 不要让 Color 页面创建视频独立时间线。
3. 不要让 Composite graph 依赖 Color 页面当前 `ColorOutput texture`。
4. 不要让 `Topos Color` 默认共享其它 clip 的 `color_stack_id`。
5. 不要删除 `Topos Color` 节点时顺手删除 `ColorStackData`。
6. 不要在 Comp Clip 失败时静默回退原素材，否则用户会以为效果没有应用。
7. 所有视频处理必须从 `timeline_id + clip_id + frame` 出发。
8. `clip_id` 是 Edit / Color / Composite 三页共同身份，`color_stack_id` 和 `composite_id` 都挂在这个身份上。

---

## 15. 验收后剩余未完成内容补充（2026-06-03）

本节记录本计划被 AI 执行后的二次验收结果。结论：

- 显式 Comp Clip 的主干方向已经落地。
- `Timeline Clip Input -> Topos Color -> Output` 的默认图契约已经基本落地。
- Edit 预览已经具备通过 `CompositeFrameRenderer` 调用 Composite graph 的桥接能力。
- 但仍存在若干半成品、旧 API、交互缺口和性能风险，不能认为 Edit / Color / Composite 已经达到产品级闭环。

### 15.1 当前已确认仍未完成的功能

| 编号 | 未完成项 | 当前表现 | 风险 | 优先级 |
|---|---|---|---|---|
| R-1 | `Bypass Composite` 未实现 | 右键菜单触发后只显示 `coming soon` | 用户无法临时关闭合成效果对比原素材 | P1 |
| R-2 | `Render Cache This Clip` 未实现 | 右键菜单触发后只显示 `coming soon` | Comp Clip 无法缓存，复杂合成无法流畅回放 | P0 |
| R-3 | `Reveal Comp Clip in Media Pool` 未完整实现 | 只在状态栏显示 asset id，未真正定位媒体池条目 | 用户无法从时间线反查对应 Comp Clip | P2 |
| R-4 | 旧 `promote_composition_to_comp_clip()` API 仍保留 | 测试仍覆盖 temporary composition promotion | 后续 AI 可能继续使用旧懒创建模型 | P1 |
| R-5 | `_pending_comp_clip_context` 仍有遗留引用 | 部分方法保留 lazy promote 时代状态字段 | 增加维护混乱和错误路径概率 | P1 |
| R-6 | `ColorOutput` 兼容路径仍存在 | legacy `COLOR/color_input` 节点仍会请求 ColorOutput | 可能和 Topos Color 的新链路混用，造成卡顿/旧帧 | P1 |
| R-7 | Edit/Composite 页面切换仍可能卡顿 | 需要实测同步 readback、renderer 生命周期和 dock viewport swap | 页面切换体验不稳定，容易误判为解码问题 | P0 |
| R-8 | Comp Clip 失败回退策略未统一 | 有些路径会 fallback 原素材，有些路径返回 None | 用户可能看不到效果或看到黑屏，行为不一致 | P0 |
| R-9 | Source Viewer 仍是独立 CPU/QImage 预览思路 | 未接入统一 GPU frame provider / cache 策略 | 源监看播放性能和时间线播放性能不一致 | P2 |
| R-10 | Program / Source Viewer 视图交互未统一 | Program 有半成品 zoom/pan，Source 无 zoom/pan | 面板尺寸改变会改变画面显示比例，体验不专业 | P1 |

### 15.2 已经临时修复但需要继续固化的内容

以下问题已经做了小范围修复，但后续 AI 仍需要补自动化测试或更完整工程化：

1. Composite 本地播放现在必须以 `clip_local_frame` 驱动，再通过 `ClipInstance.timeline_to_source_frame()` 计算真实 `source_frame`。
2. Edit 时间线预览调用 `CompositeFrameRenderer` 时必须同时传入：
   - `timeline_frame`
   - `clip_local_frame`
   - `source_frame`
3. `create_comp_clip_from_timeline_clip()` 找不到真实 clip/source asset 时必须 fail fast，不允许从项目里随便拿第一个素材。
4. macOS/PyOpenGL 下不能直接调用不存在的 `glGetCurrentContext()`，PBO 初始化必须兼容该函数缺失。
5. 默认 timeline-backed graph 无论有没有 `color_stack_id`，都应该保留 `Topos Color` 节点。没有调色栈时它是 pass-through 节点。

后续 AI 必须把这些行为补成正式测试，而不是只依赖当前手工验收。

### 15.3 R-1：实现 Bypass Composite

目标：

- 时间线 clip 已绑定 `composite_id` 时，用户可以右键临时 bypass 合成。
- Bypass 后 Edit 播放显示原素材 + Color，Composite graph 数据不丢。
- 再次取消 bypass 后 Edit 播放显示 Composite 结果。

建议数据模型：

1. 在 `ClipInstance` 增加字段：

```python
composite_bypassed: bool = False
```

2. 序列化中保存该字段：

- `ClipInstance.to_dict()`
- `ClipInstance.from_dict()`
- project schema 对应 clip 字段

3. UI 菜单：

- 如果 clip 有 `composite_id` 且未 bypass，显示 `Bypass Composite`。
- 如果 clip 有 `composite_id` 且已 bypass，显示 `Enable Composite`。
- 菜单状态要可勾选或文字切换，避免用户不知道当前状态。

4. Edit 预览逻辑：

在 `TimelinePreviewRenderer._get_layer_frame()` 中：

```python
if layer.composite_id and not layer.clip.composite_bypassed:
    render composite
else:
    render original source + color/effects path
```

5. Color 页面：

- Bypass Composite 不应影响 `color_stack_id`。
- 进入 Color 仍然编辑该 clip 的 Topos Color/ColorStack。

6. Composite 页面：

- Bypass 只影响 Edit 回放/导出。
- 打开 Composite 页面仍然能编辑 graph。
- Viewer 上可显示一个小型 `Bypassed in Edit` 状态提示，但不能阻止编辑。

验证方法：

1. 创建 Comp Clip，添加明显 Blur。
2. 回 Edit，确认 Blur 可见。
3. 右键 Bypass，确认 Blur 消失，原素材显示。
4. 回 Composite，graph 仍存在。
5. 取消 Bypass，Edit 中 Blur 恢复。
6. 保存重开，bypass 状态保持。

自动测试：

```text
tests/edit/test_comp_clip_bypass.py
tests/edit/test_comp_clip_bypass_serialization.py
tests/edit/test_comp_clip_bypass_export.py
```

### 15.4 R-2：实现 Render Cache This Clip

目标：

- Comp Clip 不要求实时播放。
- 用户可右键生成渲染缓存。
- Edit 时间线优先播放缓存，不命中缓存时再走 live composite render。

缓存数据模型：

1. 新增 `RenderCacheSettings`：

```python
@dataclass(frozen=True)
class RenderCacheSettings:
    codec: str            # prores_422_proxy, prores_422_hq, h264, hevc
    pixel_format: str     # yuv422p10, yuv420p, rgba16f
    resolution_mode: str  # timeline, half, quarter, custom
    custom_width: int | None
    custom_height: int | None
    quality: str          # preview, high, final
    cache_root: str       # project-local cache directory
```

2. 在 Preferences 面板增加 `Render Cache` 页面：

- Format：ProRes Proxy / ProRes HQ / H.264 / HEVC / Image Sequence
- Resolution：Timeline / Half / Quarter / Custom
- Bit depth：8-bit / 10-bit / 16f
- Auto render cache：Off / Smart / User
- Cache location：Project cache / Custom path
- Maximum cache size

3. 在 project 中记录：

```python
project.render_cache_settings
clip.render_cache_id
clip.render_cache_status
clip.render_cache_revision
```

4. 新增 `RenderCacheManifest`：

```json
{
  "cache_id": "...",
  "timeline_id": "...",
  "clip_id": "...",
  "composite_id": "...",
  "color_stack_revision": 12,
  "composite_graph_revision": 34,
  "source_asset_revision": "...",
  "timeline_fps": "24000/1001",
  "start_frame": 100,
  "duration_frames": 240,
  "path": "Cache/clip_xxx.mov",
  "codec": "prores_422_proxy",
  "width": 1920,
  "height": 1080
}
```

渲染流程：

1. 用户右键 `Render Cache This Clip`。
2. 根据 `timeline_id + clip_id` 找到 clip。
3. 检查是否有 `composite_id`，无则提示无需渲染合成缓存。
4. 使用 `CompositeFrameRenderer` 或更高级的 batch renderer 渲染该 clip 的全部帧。
5. 每帧必须使用：
   - timeline frame = clip.timeline_start + local frame
   - source frame = clip.timeline_to_source_frame(timeline frame)
   - color stack = clip.color_stack_id
   - composite graph = clip.composite_id
6. 编码输出到项目缓存目录。
7. 写入 cache manifest。
8. 更新 clip cache 状态。
9. Timeline strip 上显示缓存状态线：
   - 蓝色：cache valid
   - 红色：cache missing/stale
   - 橙色：cache rendering

Edit 播放优先级：

```text
if clip has valid render cache:
    decode cache media
else if clip has composite_id:
    live render composite
else:
    normal source preview
```

缓存失效条件：

- `ColorStackData.revision` 变化。
- Composite graph nodes/connections/params 变化。
- Clip trim/speed/transform/crop/opacity 变化。
- Source media relink 或 proxy 变化。
- Timeline resolution/fps 变化。
- Render cache settings 变化。

验证方法：

1. 创建 Comp Clip，添加 Blur。
2. 右键 Render Cache。
3. 渲染完成后 Edit 播放不再实时调用 CompositeFrameRenderer。
4. 修改 Composite 节点参数，缓存状态变红。
5. 重新 Render Cache，状态变蓝。
6. 修改 Color 页面调色，缓存状态变红。
7. 保存重开，缓存仍可识别。

自动测试：

```text
tests/edit/test_comp_clip_render_cache_manifest.py
tests/edit/test_comp_clip_render_cache_invalidates_on_color_change.py
tests/edit/test_comp_clip_render_cache_invalidates_on_graph_change.py
tests/edit/test_timeline_preview_uses_valid_render_cache.py
tests/project/test_render_cache_settings_serialization.py
```

### 15.5 R-3：实现 Reveal Comp Clip in Media Pool

目标：

- 时间线 clip 上右键 `Reveal Comp Clip in Media Pool` 后，媒体池滚动并选中对应 Comp Clip 资产。

实施步骤：

1. 在 `MediaPoolPanel` 增加公共方法：

```python
def reveal_asset(self, asset_id: str) -> bool:
    ...
```

2. 方法职责：

- 找到 `asset_id -> tree item`。
- 展开父 bin。
- 设置当前 item。
- 滚动到可见区域。
- 高亮闪烁 1 次或显示临时 selection。

3. 在 `EditController._on_reveal_comp_clip_in_media_pool()` 中：

- 通过 clip.composite_id 找 graph。
- 读取 `graph.comp_asset_id`。
- 调用 `media_pool_panel.reveal_asset(comp_asset_id)`。

4. 如果找不到：

- 状态栏显示明确错误。
- 不能只打印日志。

验证方法：

1. 创建 Comp Clip。
2. 把媒体池滚动到其它 bin。
3. 右键时间线 clip -> Reveal。
4. 媒体池自动展开并选中对应 Comp Clip。

自动测试：

```text
tests/edit/test_media_pool_reveal_comp_clip.py
```

### 15.6 R-4/R-5：清理旧 temporary promotion 状态

目标：

- 页面切换不创建 composition。
- 用户编辑不自动 promote。
- `_pending_comp_clip_context` 不再作为生产逻辑。

实施步骤：

1. 保留 `promote_composition_to_comp_clip()` 仅作为旧测试或 migration helper 时，必须改名或加注释：

```python
def promote_legacy_composition_to_comp_clip(...)
```

2. 生产代码不允许调用：

```bash
rg -n "_pending_comp_clip_context|promote_composition_to_comp_clip|with_asset_id\\(comp_asset_id\\)" src/app src/features
```

3. 如果仍需测试 legacy helper，把测试文件改名：

```text
tests/edit/test_legacy_comp_clip_promotion.py
```

4. 从 `EditController._ensure_comp_clip_for_current_composite()` 移除生产调用，或删除该方法。

5. 删除所有“temporary composition”用户可见文案。

验证方法：

1. 普通 clip 切 Composite。
2. 保存项目。
3. 项目中没有新增 `composition`。
4. clip 没有 `composite_id`。
5. 添加节点时被提示先创建 Comp Clip。

自动测试：

```text
tests/edit/test_open_composite_preview_does_not_create_composition.py
tests/composite/test_preview_mode_cannot_mutate_graph.py
tests/app/test_no_pending_comp_clip_context_in_page_switch.py
```

### 15.7 R-6/R-7：页面切换卡顿专项优化

当前怀疑点：

1. `CompositeController._on_composite_frame_ready()` 在无 adapter 路径中仍可能调用 `request_output_readback()`，这是同步阻塞点。
2. `CompositeRenderer.readback_output()` 会 `done_event.wait(timeout)`，若被 UI 线程调用会造成卡顿。
3. `CompositePage.on_enter()` 会初始化 renderer、提交 evaluate、可能触发 readback 和 thumbnails。
4. `ColorOutput` legacy 事件仍可能在进入 Composite 时触发额外 evaluation。
5. Dock viewport swap 后可能触发重复 GL 初始化或重复 refresh。

实施步骤：

1. 全项目搜索同步 readback：

```bash
rg -n "readback_output\\(|request_output_readback\\(|done_event.wait|glFinish|ctx.finish|fbo.read" src/app src/features
```

2. UI 线程禁止调用同步 readback：

- Composite 主预览只允许：
  - shared texture path
  - async preview image path
  - viewer-local upload path
- 导出可以同步 readback，但必须在 worker/export 线程。

3. 修改 `CompositeController._on_composite_frame_ready()`：

- adapter 路径必须优先。
- 非 adapter fallback 也不能同步 `request_output_readback()`。
- 如果 context sharing 不可用，只能等待 `preview_image_ready` 异步信号。

4. Composite 进入页面时：

- 不要 replay 旧 texture。
- 不要请求 legacy ColorOutput，除非 graph 中明确有 `color_input`。
- 对 timeline-backed graph 只提交一次 evaluation。
- 缩略图请求延后 300ms，且只为 visible/selected 节点请求。

5. 离开 Composite 时：

- 不销毁 renderer，除非关闭 app 或项目切换。
- 暂停 playback timer。
- 停止缩略图队列。
- 不做同步 flush/readback。

6. 增加切换性能日志：

```text
[PAGE-SWITCH] Edit->Composite start
[PAGE-SWITCH] context set ms=...
[PAGE-SWITCH] renderer evaluate submitted ms=...
[PAGE-SWITCH] first frame ready ms=...
[PAGE-SWITCH] Composite->Edit refresh submitted ms=...
```

通过标准：

- Edit -> Composite 首帧显示时间小于 300ms（已有缓存/同帧）。
- Color -> Composite 不触发 UI 卡死。
- 来回切 20 次无内存持续增长。
- 没有 `request_output_readback()` 出现在页面切换主线程调用链。

自动测试：

```text
tests/composite/test_composite_page_enter_does_not_sync_readback.py
tests/composite/test_composite_page_leave_does_not_sync_readback.py
tests/app/test_page_switch_submits_single_composite_eval.py
```

手工测试：

1. 打开含 3 个视频 clip 的时间线。
2. clip A 创建 Comp Clip 并添加 Blur。
3. 在 Edit / Color / Composite 来回切 20 次。
4. 观察：
   - 首帧不显示旧 clip。
   - UI 不冻结。
   - 日志中无同步 readback。
   - Composite 节点缩略图晚于主画面加载。

### 15.8 R-8：统一 Comp Clip 失败策略

目标：

- 用户必须能知道 Composite graph 失败了。
- 不能有些路径黑屏、有些路径静默回原素材。

建议策略：

| 场景 | 行为 |
|---|---|
| Edit 预览 live composite render 失败 | 显示错误 overlay，可选显示原素材 ghost，但必须有红色状态 |
| Export composite render 失败 | 失败并给出错误，不静默导出原素材 |
| Render Cache render 失败 | cache 状态为 failed，保留旧有效缓存 |
| Composite 页面 preview-only 无源帧 | 显示明确 `Timeline source unavailable` |
| 普通 clip 无 composite_id | 正常显示原素材 |

实施步骤：

1. 新增 `CompositeRenderFailurePolicy`。
2. `TimelinePreviewRenderer._get_layer_frame()` 不直接决定 fallback，而是调用 policy。
3. ProgramViewer 支持错误 overlay：

```python
set_error_state("composite_render_error", message)
```

4. Timeline clip strip 显示小红点或 `Comp Error` 标签。

验证方法：

1. 故意断开 Output。
2. 回 Edit。
3. Program Viewer 显示错误 overlay。
4. 导出时报错。
5. 修复 Output 后恢复。

---

## 16. Program / Source Viewer 预览视图交互统一计划

### 16.1 当前问题结论

用户反馈：

> Edit 页面 Program 时间线预览视图窗口，画面会随着面板尺寸发生改变，希望 Program 与 Source 也能像 Color 或 Composite viewport 一样，鼠标滚轮缩放，按住鼠标滚轮自由移动预览位置。

代码现状：

1. `ProgramViewer` 已有 `_zoom_level/_pan_x/_pan_y`，但行为不完整：
   - `resizeGL()` 在面板尺寸变化时调用 `_fit_to_window()`。
   - 这会重置 zoom/pan，所以用户只要调整 dock 尺寸，画面就重新适配。
   - `wheelEvent()` 只有 `Ctrl + Wheel` 才缩放，不符合用户希望的直接滚轮缩放。
   - pan 使用 NDC 偏移，和像素坐标/鼠标位置锚点之间不够稳定。

2. `SourceViewer` 没有 zoom/pan 状态：
   - `_calculate_display_rect()` 永远根据 widget 尺寸 fit。
   - `resizeGL()` 是空方法。
   - 没有 `wheelEvent()`。
   - 没有中键 pan。

3. 两个 viewer 都在 `_render_texture()` 中每帧重建 quad 顶点，这可以保留，但需要统一 display rect 计算。

4. Color 页面使用 `QGraphicsView` 的 transform/pan 体系；Composite Viewer 使用独立 viewer state。Edit 的两个 viewer 需要一个轻量共享 transform，而不是复制两套半成品逻辑。

### 16.2 目标行为

Program Viewer 和 Source Viewer 都必须支持：

| 操作 | 行为 |
|---|---|
| 鼠标滚轮 | 以鼠标所在位置为锚点缩放画面 |
| Ctrl + 鼠标滚轮 | 同滚轮缩放，保留兼容 |
| 鼠标中键按住拖动 | 平移画面 |
| Space + 左键拖动 | 可选，和 Color viewport 一致 |
| F | Fit to window |
| 1 | Actual size / 100% |
| 双击 | Program 保持全屏切换，Source 可选 fit |
| 面板 resize | 保留用户 zoom/pan，不强制 fit，除非当前处于 fit mode |
| 新素材/新时间线加载 | 默认 fit 一次 |
| 用户手动 zoom/pan 后 | 进入 manual mode，resize 不重置 |

### 16.3 新增共享视图状态类

新增文件：

```text
src/features/edit/ui/preview_view_transform.py
```

核心类：

```python
from dataclasses import dataclass

@dataclass
class PreviewViewTransform:
    zoom: float = 1.0              # 1.0 表示 fit 基础上的倍率
    pan_x_px: float = 0.0          # viewer 像素坐标偏移
    pan_y_px: float = 0.0
    mode: str = "fit"              # fit | manual | actual
    min_zoom: float = 0.05
    max_zoom: float = 16.0

    def reset_fit(self) -> None: ...
    def set_actual_size(self, texture_w: int, texture_h: int, viewport_w: int, viewport_h: int) -> None: ...
    def zoom_at(self, factor: float, anchor_x: float, anchor_y: float, viewport_w: int, viewport_h: int) -> None: ...
    def pan_by(self, dx: float, dy: float) -> None: ...
    def display_rect_px(self, texture_w: int, texture_h: int, viewport_w: int, viewport_h: int) -> tuple[float, float, float, float]: ...
    def display_rect_ndc(self, texture_w: int, texture_h: int, viewport_w: int, viewport_h: int) -> tuple[float, float, float, float]: ...
```

计算规则：

1. 先算 fit 尺寸：

```python
fit_scale = min(viewport_w / texture_w, viewport_h / texture_h)
fit_w = texture_w * fit_scale
fit_h = texture_h * fit_scale
```

2. 再叠加用户 zoom：

```python
display_w = fit_w * zoom
display_h = fit_h * zoom
```

3. 居中加 pan：

```python
x = (viewport_w - display_w) / 2 + pan_x_px
y = (viewport_h - display_h) / 2 + pan_y_px
```

4. 转 NDC：

```python
ndc_x = x / viewport_w * 2.0 - 1.0
ndc_y = 1.0 - (y + display_h) / viewport_h * 2.0
ndc_w = display_w / viewport_w * 2.0
ndc_h = display_h / viewport_h * 2.0
```

注意：

- 像素坐标的 y 向下为正。
- OpenGL NDC 的 y 向上为正。
- 只在 display rect 转 NDC 时转换一次。

### 16.4 鼠标滚轮锚点缩放算法

目标：

- 鼠标指向画面某一点时，滚轮放大后该画面点仍尽量停留在鼠标下。

算法：

```python
def zoom_at(anchor_x, anchor_y, factor):
    old_rect = display_rect_px(...)
    rel_x = (anchor_x - old_rect.x) / old_rect.w
    rel_y = (anchor_y - old_rect.y) / old_rect.h

    zoom = clamp(zoom * factor)

    new_w = fit_w * zoom
    new_h = fit_h * zoom

    pan_x = anchor_x - viewport_w / 2 - (rel_x - 0.5) * new_w
    pan_y = anchor_y - viewport_h / 2 - (rel_y - 0.5) * new_h
```

边界策略：

- 初版不强制 clamp pan，允许用户自由移动到画面边缘之外。
- 后续可加 soft clamp，至少留 10% 画面可见。

滚轮倍率：

```python
angle = event.angleDelta().y()
steps = angle / 120.0
factor = pow(1.12, steps)
```

触控板：

- 如果 `event.pixelDelta()` 有值，使用更小步长：

```python
factor = pow(1.01, pixel_delta_y)
```

### 16.5 ProgramViewer 实施步骤

涉及文件：

```text
src/features/edit/ui/program_viewer.py
src/features/edit/ui/preview_view_transform.py
tests/edit/test_preview_view_transform.py
tests/edit/test_program_viewer_transform_contract.py
```

步骤：

1. 引入 `PreviewViewTransform`。

2. 替换字段：

旧字段：

```python
self._zoom_level
self._pan_x
self._pan_y
self._pan_start_ndc_x
self._pan_start_ndc_y
```

新字段：

```python
self._view_transform = PreviewViewTransform()
self._pan_start_pos = QPoint()
self._pan_start_px = (0.0, 0.0)
```

3. 修改 `_calculate_display_rect()`：

旧逻辑基于 NDC 和 `_zoom_level`。

新逻辑：

```python
return self._view_transform.display_rect_ndc(
    self._tex_width or fallback_width,
    self._tex_height or fallback_height,
    self.width(),
    self.height(),
)
```

4. 修改 `resizeGL()`：

旧逻辑：

```python
if not self._is_fullscreen:
    self._fit_to_window()
```

新逻辑：

```python
if self._view_transform.mode == "fit":
    self._view_transform.reset_fit()
self.update()
emit viewport_resized
```

重点：

- manual mode resize 不重置。
- fit mode resize 只重新计算 fit 基础尺寸，不把用户状态改成 manual。

5. 修改 `_fit_to_window()`：

```python
self._view_transform.reset_fit()
self.zoom_changed.emit(self._view_transform.zoom)
self.update()
```

6. 修改 `_actual_size()`：

```python
self._view_transform.set_actual_size(
    texture_w=self._tex_width,
    texture_h=self._tex_height,
    viewport_w=self.width(),
    viewport_h=self.height(),
)
```

7. 修改 `wheelEvent()`：

```python
delta = event.pixelDelta().y() or event.angleDelta().y()
factor = ...
self._view_transform.zoom_at(
    factor=factor,
    anchor_x=event.position().x(),
    anchor_y=event.position().y(),
    viewport_w=self.width(),
    viewport_h=self.height(),
    texture_w=self._tex_width,
    texture_h=self._tex_height,
)
self.zoom_changed.emit(self._view_transform.zoom)
self.update()
event.accept()
```

8. 修改中键 pan：

```python
mousePress:
    self._pan_start_px = (transform.pan_x_px, transform.pan_y_px)

mouseMove:
    dx = event.position().x() - start.x()
    dy = event.position().y() - start.y()
    transform.pan_x_px = start_pan_x + dx
    transform.pan_y_px = start_pan_y + dy
```

9. 新素材第一次加载时：

- 如果当前没有 texture，或者 clip/source 改变，调用 `_fit_to_window()`。
- 如果只是新帧到来，不要 reset fit。

10. Toolbar zoom label：

- 显示 `Fit` 或 `100%` 或 `125%`。
- 当前 `ProgramViewerBar.update_zoom_label()` 可保留，但建议增强：

```python
update_zoom_label(zoom, mode)
```

验证方法：

1. 打开 Edit 页面，播放一个视频。
2. 拖大/缩小 Program 面板。
3. Fit mode 下画面适配窗口。
4. 滚轮放大到 200%。
5. 再拖大/缩小 Program 面板，画面仍保持 200% 附近，不回到 fit。
6. 中键拖动画面，能自由移动。
7. 按 F 回到 fit。
8. 按 1 显示实际像素尺寸。

### 16.6 SourceViewer 实施步骤

涉及文件：

```text
src/features/edit/ui/source_viewer.py
src/features/edit/ui/preview_view_transform.py
tests/edit/test_source_viewer_transform_contract.py
```

步骤：

1. 给 `SourceViewer` 增加：

```python
zoom_changed = pyqtSignal(float)
self._view_transform = PreviewViewTransform()
self._is_panning = False
self._pan_start_pos = QPointF()
self._pan_start_px = (0.0, 0.0)
```

2. 修改 `_calculate_display_rect()`：

使用 `PreviewViewTransform.display_rect_ndc()`。

3. 新增公开方法：

```python
def fit_to_window(self): ...
def actual_size(self): ...
def zoom_in(self): ...
def zoom_out(self): ...
def set_zoom(self, zoom: float): ...
```

4. 新增 `wheelEvent()`：

- 默认滚轮缩放。
- 保留 Ctrl+Wheel 兼容。
- `event.accept()`。

5. 新增 `mousePressEvent/mouseMoveEvent/mouseReleaseEvent`：

- 中键拖动 pan。
- 可选 Space+左键 pan。

6. 新素材打开时：

- `open_asset()` 加载第一帧后调用 `viewer.fit_to_window()`。
- 每次 `_load_frame()` 不重置 view transform。

7. Source toolbar 可后续增加小型 zoom label。

验证方法：

1. 媒体池双击视频打开 Source Viewer。
2. 滚轮放大，画面以鼠标位置为中心缩放。
3. 中键拖动画面。
4. 拖动 Source Viewer 所在面板尺寸，manual zoom/pan 保持。
5. 切换到另一个素材时自动 fit。
6. 播放 Source 时不因每帧加载而 reset zoom。

### 16.7 Program / Source 视图状态保存

建议先做内存状态，不急着写入 `.topos`。

第一阶段：

- Program Viewer 和 Source Viewer 各自保存当前 transform 到 widget 实例。
- 页面切换回来仍保留。
- 新项目/关闭项目时 reset。

第二阶段：

- 可选写入 `viewer_state`：

```json
{
  "edit_program_viewer": {
    "mode": "manual",
    "zoom": 1.75,
    "pan_x_px": 120,
    "pan_y_px": -40
  },
  "edit_source_viewer": {
    "mode": "fit",
    "zoom": 1.0,
    "pan_x_px": 0,
    "pan_y_px": 0
  }
}
```

不建议第一阶段写项目文件，因为 viewer transform 是 UI 状态，不是创作数据。

### 16.8 自动测试计划

新增纯算法测试：

```text
tests/edit/test_preview_view_transform.py
```

测试点：

1. 16:9 视频在 16:9 viewport 中 fit 后填满。
2. 16:9 视频在竖向 viewport 中上下留黑边。
3. 9:16 视频在横向 viewport 中左右留黑边。
4. zoom_at 后 anchor 对应的图像相对位置保持。
5. pan_by 改变 display rect 位置。
6. actual size 下 display rect 尺寸等于 texture 尺寸。
7. resize 时 manual mode 不 reset zoom。

新增 viewer contract 测试：

```text
tests/edit/test_program_viewer_transform_contract.py
tests/edit/test_source_viewer_transform_contract.py
```

测试点：

1. `resizeGL()` 在 manual mode 不调用 `reset_fit()`。
2. `set_texture()` 第一帧可 fit，后续帧不 reset。
3. `wheelEvent()` 会修改 zoom 并 accept。
4. 中键 pan 会修改 pan px。

### 16.9 手工验收矩阵

| 用例 | 步骤 | 通过标准 |
|---|---|---|
| Program fit | 打开时间线，按 F | 画面完整适配窗口 |
| Program manual zoom | 滚轮放大后 resize 面板 | zoom 不被重置 |
| Program pan | 中键拖动 | 画面自由移动 |
| Program playback | 播放时时滚轮缩放 | 播放不中断，不重新请求解码 |
| Source fit | 双击媒体池素材 | 第一帧自动 fit |
| Source manual zoom | Source 中滚轮放大并播放 | 每帧更新不重置 zoom |
| 切换页面 | Edit -> Color -> Edit | Program transform 保持 |
| 新素材 | Source 打开另一个素材 | 自动回到 fit |

---

## 17. 修订后的后续执行顺序

建议后续 AI 按以下顺序继续，避免同时动渲染管线和 UI 交互造成回归：

1. 先做 `Program/Source Viewer` 的 transform 抽象和交互统一。
   - 这是局部 UI 工作，风险较小。
   - 能立即改善 Edit 页面体验。

2. 再做页面切换卡顿专项。
   - 禁止 UI 线程同步 readback。
   - 延迟节点缩略图。
   - 精简 legacy ColorOutput 触发。

3. 再做 Render Cache。
   - 这是 Comp Clip 真正可播放的关键。
   - 需要 Preferences、cache manifest、cache invalidation。

4. 再做 Bypass / Reveal / Error policy。
   - 这几个是用户交互闭环。

5. 最后清理旧 temporary promotion API。
   - 因为清理会影响旧测试，应该在新测试足够完整后做。

每做完一步都必须运行：

```bash
.venv/bin/python -m pytest tests/edit tests/composite tests/project -q
```

Program/Source Viewer 专项至少运行：

```bash
.venv/bin/python -m pytest \
  tests/edit/test_preview_view_transform.py \
  tests/edit/test_program_viewer_transform_contract.py \
  tests/edit/test_source_viewer_transform_contract.py \
  -q
```

页面切换专项至少运行：

```bash
.venv/bin/python -m pytest \
  tests/composite/test_composite_page_enter_does_not_sync_readback.py \
  tests/composite/test_composite_page_leave_does_not_sync_readback.py \
  tests/app/test_page_switch_submits_single_composite_eval.py \
  -q
```

---

## 测试文件名对照表

> 以下为计划文档中列出的测试文件名与实际实现文件的映射。
> 多个计划阶段的测试被合并到更全面的测试文件中。

| 计划文件名 | 实际覆盖文件 | 说明 |
|---|---|---|
| `test_create_comp_clip_explicit.py` | `test_comp_clip_promotion_lifecycle.py` | 显式创建 + promote 生命周期 |
| `test_remove_comp_clip_binding.py` | `test_comp_clip_reset.py` | Remove binding → reset 覆盖 |
| `test_reset_comp_clip_keeps_color_stack.py` | `test_comp_clip_reset.py` | Reset 保留 color_stack_id |
| `test_timeline_panel_forwards_comp_clip_menu_signals.py` | (集成测试覆盖) | 信号转发在集成测试中覆盖 |
| `test_preview_mode_does_not_emit_graph_changed.py` | `test_comp_clip_reset.py` | Preview-only 模式 graph 不变更 |
| `test_preview_mode_mutation_requests_comp_clip_creation.py` | `test_comp_clip_promotion_lifecycle.py` | 变更触发 promote |
| `test_preview_mode_leave_does_not_promote_comp_clip.py` | `test_no_pending_comp_clip_context_in_page_switch.py` | 页面切换不 promote |
| `test_editable_comp_clip_graph_changed_persists.py` | `test_comp_clip_default_graph_contract.py` | Graph 持久化 |
| `test_timeline_graph_topos_color_repair.py` | `test_default_graph_topos_color_contract.py` | Topos Color 修复 |
| `test_default_clip_graph_always_has_topos_color.py` | `test_default_graph_topos_color_contract.py` | 默认 graph 包含 Topos Color |
| `test_topos_color_missing_stack_passes_through.py` | `test_no_color_output_for_topos_graph.py` | 无 stack 时 pass-through |
| `test_color_updates_topos_color_node.py` | `test_default_graph_topos_color_contract.py` | Color 页面更新同步 |
| `test_topos_color_reads_color_stack_revision.py` | `test_default_graph_topos_color_contract.py` | Revision 读取 |
| `test_comp_clip_color_change_invalidates_preview_cache.py` | `test_comp_clip_timeline_preview.py` | 缓存失效 |
| `test_comp_clip_timeline_preview_uses_composite_output.py` | `test_comp_clip_timeline_preview.py` | Timeline preview 用 composite 输出 |
| `test_comp_clip_export_uses_composite_output.py` | `test_comp_clip_timeline_export.py` | Export 用 composite 输出 |
| `test_schema_v3_rejects_video_single_asset.py` | `test_topos_schema_v3.py` | Schema v3 校验 |
| `test_schema_v3_saves_comp_clip_graph.py` | `test_comp_clip_save_integrity.py` + `test_topos_schema_v3.py` | Comp Clip graph 保存 |
| `test_schema_v3_requires_topos_color_for_timeline_graph.py` | `test_default_graph_topos_color_contract.py` | Timeline graph 必须有 Topos Color |
