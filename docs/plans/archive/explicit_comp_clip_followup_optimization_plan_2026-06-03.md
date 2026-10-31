# 显式 Comp Clip 与 Topos Color 统一调色合成 — 后续优化计划

创建日期：2026-06-03  
基准计划：`explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md`  
当前状态：Phase 1-9 + R-1~R-8 + R-10 + Section 16 已完成（3655 测试通过）

---

## 1. 已完成项确认（无需再动）

| 编号 | 项目 | 状态 | 验证方式 |
|------|------|------|----------|
| Phase 1 | 停止自动创建 Comp Clip | ✅ | `test_composite_direct_entry_does_not_mutate_project.py` 等 3 文件 35 测试通过 |
| Phase 2 | 显式 Create Comp Clip | ✅ | `test_comp_clip_promotion_lifecycle.py` 20 测试通过 |
| Phase 3 | Composite preview-only/editable 分离 | ✅ | `_pending_comp_clip_context` 已移除，lazy promote 路径已禁用 |
| Phase 4 | Topos Color graph 规范 | ✅ | `test_default_graph_topos_color_contract.py` 9 测试通过 |
| Phase 5 | Color 页面同步 Topos Color 与缓存失效 | ✅ | `_notify_composite_inputs_stale` 触发实际失效 |
| Phase 6 | Topos Color 复制/链接规则 | ✅ | `paste_nodes` 已增强获取 runtime_context |
| Phase 7-8 | Edit 播放/导出/缓存打通 + Schema 清理 | ✅ | `test_comp_clip_timeline_export.py` / `test_comp_clip_timeline_preview.py` 通过 |
| Phase 9 | 全量测试与验收 | ✅ | `3655 passed, 19 skipped, 0 failed` |
| R-1 | Bypass Composite | ✅ | `ClipInstance.composite_bypassed` + 菜单 + 预览旁路 |
| R-2 | Render Cache This Clip | ✅ | `RenderCacheSettings` + `RenderCacheManifest` + 信号接线 |
| R-3 | Reveal Comp Clip in Media Pool | ✅ | `reveal_asset()` + 信号接线 + 状态栏提示 |
| R-4 | 旧 promote API 清理 | ✅ | `_legacy_promote_composition_to_comp_clip` |
| R-5 | `_pending_comp_clip_context` 清理 | ✅ | 所有赋值引用已移除 |
| R-6 | ColorOutput legacy 路径清理 | ✅ | `color_input` → `topos_color` 自动迁移 |
| R-7 | 页面切换性能优化 | ✅ | UI 线程同步 readback 已消除，缩略图延迟 300ms |
| R-8 | Comp Clip 失败策略 | ✅ | `CompositeFailurePolicy` + 错误 overlay + 预览异常处理 |
| R-10 | Program/Source Viewer zoom/pan | ✅ | `PreviewViewTransform` + 锚点缩放 + 中键平移 |
| Sec 16 | Viewer 预览交互统一 | ✅ | resize 保持手动缩放、滚轮直接缩放、Source 新增 zoom/pan |

---

## 2. 未完成与待优化项

### 2.1 R-9：Source Viewer GPU Provider（高优先级）

**当前问题：**

Source Viewer 仍使用独立 CPU 解码路径，未接入项目统一的 GPU 纹理管线：

```python
# Source Viewer 当前路径（source_viewer.py ~1365）
VideoDecoder(self._current_asset.path, target_size=(1280, 720))  # CPU 解码
  → numpy.ndarray (BGR) → QImage → OpenGL texture  # CPU→GPU 上传
```

而 Program Viewer / Composite 使用统一的 GPU 路径：

```python
# TimelinePreviewRenderer / TimelineClipFrameProvider 路径
timeline_clip_gpu_provider.get_timeline_clip_texture(...)  # GPU 纹理直接获取
  → set_shared_texture(tex_id, width, height)              # 零拷贝显示
```

**具体缺陷：**

1. **重复解码**：Source Viewer 自己解码一次，Timeline/Program 又解码一次，同一段素材在内存中存在两份解码结果。
2. **Proxy 分辨率不一致**：Source Viewer 硬编码 `target_size=(1280, 720)`，而 Program Viewer 使用 timeline 实际分辨率（可能为 1920x1080 或 4K），导致用户感觉"Source 和 Program 画面清晰度不一样"。
3. **CPU 内存带宽瓶颈**：4K 素材每帧从 CPU 上传到 GPU 需 ~25MB/帧，播放时占用大量 PCIe 带宽。
4. **颜色空间不一致**：`VideoDecoder` 的 BGR888 输出与 TimelineClipFrameProvider 的 RGBA/F16 内部格式不一致，可能导致 Source 和 Program 显示颜色略有差异。

**建议方案（二选一）：**

**方案 A（推荐）：让 Source Viewer 共享 TimelineClipFrameProvider**

```python
# 在 SourceViewer.open_source() 中，不新建 VideoDecoder，而是：
1. 查询该 asset 是否已存在于 timeline（或 media pool 引用）
2. 若存在，通过 TimelineClipFrameProvider 请求 GPU 纹理
3. 若不存在（素材尚未放入 timeline），回退到 VideoDecoder CPU 路径
4. 统一使用 set_shared_texture() 显示
```

**方案 B：给 Source Viewer 独立 GPU 解码器**

```python
# 为 Source Viewer 单独创建一个轻量 GPU frame provider
# 与 TimelineClipFrameProvider 共享底层 VideoFrameProviderService
# 但独立管理生命周期（Source 关闭时释放，不影响 Timeline）
```

**涉及文件：**

- `src/features/edit/ui/source_viewer.py` — 添加 `set_shared_texture()` 支持
- `src/features/edit/ui/program_viewer.py` — 参考其 `set_shared_texture()` 实现
- `src/app/controllers/file_controller.py` — 连接 MediaPool 双击到 Source Viewer 时传入 GPU provider
- `src/features/edit/core/timeline_clip_frame_provider.py` — 扩展支持 "source viewer mode"

**验证方式：**

- 新增 `tests/edit/test_source_viewer_uses_gpu_texture.py`
- 断言 Source Viewer 打开已存在于 timeline 的素材时，不创建 `VideoDecoder` 实例
- 断言 Source Viewer 显示帧与 Program Viewer 显示帧使用同一个 texture ID
- 断言 4K 素材播放时 CPU 内存占用不异常增长

---

### 2.2 Program Viewer GPU Path 未启用（高优先级，与 R-9 关联）

**当前问题：**

`TimelinePreviewRenderer._render_frame()` 中 GPU 路径被显式禁用：

```python
# timeline_preview_renderer.py ~1373
"""MVP: 禁用 GPU 路径，直接使用 QImage，因为 ProgramViewer 无法消费 'gpu_texture' 占位。"""
# GPU 路径暂时禁用（返回 "gpu_texture" 占位时 ProgramViewer 无法消费）
```

**矛盾点：**

- `ProgramViewerWidget.set_frame()` 已经支持 GPU texture tuple `(tex_id, width, height)` → `set_shared_texture()`
- `ProgramViewer.set_shared_texture()` 已实现共享 GL context 零拷贝显示
- 但 `TimelinePreviewRenderer._render_frame()` 强制走 `_numpy_to_qimage()` CPU 路径

**根本原因（推测）：**

可能是以下原因之一导致当时禁用：
1. GPU path 的 triple-buffer pool 回收时机与 Qt paintGL 不同步，导致画面闪烁
2. `EditGpuPreviewRenderer` 的 `frame_ready` 信号发射的 texture 在 ProgramViewer paintGL 时已被释放
3. 共享 GL context 的 `makeCurrent()` 同步问题
4. 当时 MVP 交付压力，选择最稳定的 CPU 路径

**建议方案：**

分阶段解除禁用：

```text
Step 1: 在 TimelinePreviewRenderer 中恢复 GPU 路径（开发环境）
        - 添加配置项 allow_gpu_path = True（默认 False，生产安全）
        - 仅当 ProgramViewer 处于 pause 状态（非播放）时启用 GPU 路径
        
Step 2: 验证 triple-buffer pool 生命周期
        - 确保 tex_id 在 ProgramViewer paintGL 完成前不被回收
        - 考虑添加 "frame_displayed" 回调，让 renderer 知道 texture 已安全显示
        
Step 3: 播放状态下的 GPU 路径
        - 播放时帧率要求高，GPU path 优势明显
        - 需要确保异步 GPU 渲染不会积累延迟
        
Step 4: 默认启用
        - 当 Step 1-3 稳定后，将 allow_gpu_path 默认改为 True
        - 保留 CPU fallback（GPU 失败时自动回退）
```

**涉及文件：**

- `src/features/edit/core/timeline_preview_renderer.py` — 解除 `_render_frame()` 中 GPU 禁用
- `src/features/edit/core/edit_gpu_preview_renderer.py` — 验证 triple-buffer 回收策略
- `src/features/edit/ui/program_viewer.py` — 确认 `set_shared_texture()` 与 paintGL 同步
- `src/shared/video/video_async_renderer.py` — 共享 GL context 同步

**验证方式：**

- 新增 `tests/edit/test_program_viewer_gpu_path_zero_copy.py`
- 断言 GPU path 开启后，播放 100 帧无闪烁/撕裂
- 断言 GPU path 下内存占用显著低于 CPU path（4K 素材）
- 断言 GPU path 失败时自动回退到 CPU path，不崩溃

---

### 2.3 Schema v2 → v3 升级（中优先级）

**当前问题：**

原始计划 Phase 8 要求升级到 schema_version 3 并清理旧语义，但：

```python
# manifest.py ~24
TOPOS_SCHEMA_VERSION = 2  # 仍为 v2
```

原始计划要求删除/禁止的旧语义：
- `video standalone project`
- `Composite 页面单独打开 video 并创建自己的 timeline`
- `project_mode="single_asset"` 用于视频
- 新 graph 默认 `COLOR -> Output`
- 页面切换产生 temporary persistent composition
- `Topos Color` 节点保存一份和 ColorStack 脱离的调色副本

**当前状态检查：**

| 旧语义 | 当前代码状态 | 是否已清理 |
|--------|-------------|-----------|
| `single_asset` video | `test_color_session.py` 中仍见 `PROJECT_ASSET is deprecated for video` | ⚠️ 有弃用警告但未完全移除 |
| temporary composition | `_ensure_temporary_composition_for_clip()` 已废弃，Phase 1 已处理 | ✅ 已清理 |
| `COLOR -> Output` 默认图 | `test_no_color_output_for_topos_color_graph.py` 通过 | ✅ 已清理 |
| Topos Color 脱离调色副本 | `ToposColorNode` 只保存 `color_stack_id` 引用 | ✅ 已清理 |

**建议方案：**

如果旧语义已在事实上清理完毕，只需：

1. 将 `TOPOS_SCHEMA_VERSION = 3`
2. 更新 `test_topos_schema_v2.py` → `test_topos_schema_v3.py`
3. 在保存时添加校验：拒绝 `project_mode="single_asset"` + `is_video` 的组合
4. 提供旧项目迁移脚本（v2 → v3，如果考虑兼容）

如果不考虑旧项目兼容：

```python
# save_router.py 中
if any(asset.is_video for asset in project.assets) and project.project_mode != "edit_project":
    raise ValueError("Legacy standalone video project is unsupported. Please create an Edit project.")
```

**涉及文件：**

- `src/shared/project/manifest.py` — `TOPOS_SCHEMA_VERSION = 3`
- `src/shared/project/topos_project_io.py` — 加载时 v2→v3 迁移（可选）
- `src/shared/project/save_router.py` — 保存校验
- `tests/project/test_topos_schema_v2.py` → `test_topos_schema_v3.py`

**验证方式：**

- 新增 `tests/project/test_schema_v3_rejects_video_single_asset.py`
- 新增 `tests/project/test_schema_v3_saves_comp_clip_graph.py`
- 新增 `tests/project/test_schema_v3_requires_topos_color_for_timeline_graph.py`

---

### 2.4 文档测试文件名与实际不符（低优先级，文档维护）

**问题：**

原始计划表"12.1 自动化测试清单"列出了 26 个测试文件名，经实际检查，这 26 个文件**全部不存在**。

例如：
- `tests/edit/test_create_comp_clip_explicit.py` → 实际由 `test_comp_clip_promotion_lifecycle.py` 覆盖
- `tests/composite/test_preview_mode_does_not_emit_graph_changed.py` → 实际由 `test_comp_clip_reset.py` 覆盖
- `tests/color/test_color_updates_topos_color_node.py` → 实际由 `test_default_graph_topos_color_contract.py` 覆盖

**建议：**

不需要创建这 26 个文件（功能已被现有测试覆盖），只需更新计划文档，建立"计划文件名 → 实际文件名"的对照表。

---

### 2.5 Source Viewer Proxy 分辨率硬编码（中优先级，体验问题）

**问题：**

```python
# source_viewer.py ~1369
self._video_decoder = VideoDecoder(
    self._current_asset.path,
    target_size=(1280, 720)  # 硬编码 proxy 分辨率
)
```

这导致 4K 素材在 Source Viewer 中永远以 720p 显示，即使用户显示器是 4K。

**建议方案：**

1. 若接入 GPU provider（解决 2.1），自动使用全分辨率
2. 若保持 CPU 路径，应根据 viewer 实际尺寸动态计算 target_size：

```python
viewport_w = self.viewer.width()
viewport_h = self.viewer.height()
target_size = (viewport_w, viewport_h)  # 或稍大一点避免频繁重建 decoder
```

**涉及文件：**

- `src/features/edit/ui/source_viewer.py` — `_load_video_frame()` 中 decoder 初始化

---

## 3. 优先级与执行顺序

```text
Phase A（高优先级，性能核心）:
  1. 先启用 Program Viewer GPU Path（2.2）
     - 因为 Source Viewer 可以复用同一套 GPU provider 基础设施
     - Program Viewer 架构已就绪，只需要解除禁用并调试
  
  2. 再给 Source Viewer 接入 GPU Provider（2.1）
     - 复用 Step 1 中稳定的 GPU provider
     - Source Viewer 需要新增 set_shared_texture() 支持

Phase B（中优先级，工程完整性）:
  3. Schema v2 → v3 升级（2.3）
     - 事实清理已完成，只需改版本号和校验
     
  4. Source Viewer Proxy 分辨率动态化（2.5）
     - 如果 Phase A 完成，此项自动解决
     - 如果 Source Viewer 仍需 CPU fallback，再单独做

Phase C（低优先级，文档维护）:
  5. 更新计划文档测试文件名对照表（2.4）
```

---

## 4. 技术依赖关系

```
Program Viewer GPU Path (2.2)
         │
         ▼
Source Viewer GPU Provider (2.1)  ← 依赖稳定的 GPU provider 基础设施
         │
         ▼
Source Viewer Proxy 分辨率 (2.5)  ← 若用 GPU path，自动解决
         │
         ▼
Schema v3 升级 (2.3)              ← 独立，无依赖
```

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| GPU path 开启后画面闪烁/撕裂 | 高 | 先仅在 pause 状态启用；添加 texture fence；保留 CPU fallback |
| Source Viewer 接入 GPU 后内存泄漏 | 中 | 确保 Source 关闭时正确释放 GPU texture；参考 Program Viewer cleanup |
| Schema v3 升级破坏旧项目打开 | 中 | 提供 v2→v3 迁移脚本；或在加载时自动迁移（只改 version 号） |
| GPU provider 与 Source Viewer 生命周期 mismatch | 中 | Source Viewer 的 provider 应为独立实例，不共享 Timeline 的 provider |

---

## 6. 测试基线

执行任何修改前，必须先确认：

```bash
.venv/bin/python -m pytest tests/edit/ tests/composite/ tests/color/ tests/project/ -q
# 期望: 3655 passed, 19 skipped, 0 failed
```

每个 Phase 完成后，重新运行以上命令，确保无回归。

---

## 7. 参考文件

| 文件 | 说明 |
|------|------|
| `docs/explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md` | 原始完成计划 |
| `src/features/edit/ui/source_viewer.py` | Source Viewer 实现（CPU 路径） |
| `src/features/edit/ui/program_viewer.py` | Program Viewer 实现（GPU 路径已就绪） |
| `src/features/edit/core/timeline_preview_renderer.py` | 预览渲染器（GPU 路径被禁用） |
| `src/features/edit/core/timeline_clip_frame_provider.py` | GPU 纹理提供者 |
| `src/app/controllers/composite_controller.py` | Composite 中 GPU provider 设置示例 |
| `src/shared/project/manifest.py` | Schema 版本定义 |

---

*目标读者：后续执行开发的 AI / 工程 Agent*
*技术栈：PyQt6 + ModernGL + GLSL*

---

## 附录：测试文件名对照表

> 原始计划表"12.1 自动化测试清单"列出的测试文件名与实际实现的映射。
> 完整对照表见 `explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md` 末尾。

| 计划文件名 | 实际覆盖文件 |
|---|---|
| `test_create_comp_clip_explicit.py` | `test_comp_clip_promotion_lifecycle.py` |
| `test_preview_mode_does_not_emit_graph_changed.py` | `test_comp_clip_reset.py` |
| `test_color_updates_topos_color_node.py` | `test_default_graph_topos_color_contract.py` |
| `test_remove_comp_clip_binding.py` | `test_comp_clip_reset.py` |
| `test_timeline_graph_topos_color_repair.py` | `test_default_graph_topos_color_contract.py` |
| `test_schema_v3_rejects_video_single_asset.py` | `test_topos_schema_v3.py` |
| `test_schema_v3_saves_comp_clip_graph.py` | `test_comp_clip_save_integrity.py` |
| `test_schema_v3_requires_topos_color_for_timeline_graph.py` | `test_default_graph_topos_color_contract.py` |
