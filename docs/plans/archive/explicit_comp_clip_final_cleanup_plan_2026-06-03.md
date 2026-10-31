# 显式 Comp Clip 与 Topos Color 统一调色合成 — 最终收尾计划

创建日期：2026-06-03  
基准计划：
1. `explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md`（原始完成计划）
2. `explicit_comp_clip_followup_optimization_plan_2026-06-03.md`（后续优化计划）
当前状态：前两轮优化已完成（3660 测试通过，19 skipped，0 failed）

---

## 1. 已完成项确认（无需再动）

| 项目 | 完成度 | 验证方式 |
|------|--------|----------|
| Phase 1-9 | 100% | `3660 passed, 0 failed` |
| R-1 ~ R-8 | 100% | 测试 + 代码审查 |
| R-10 / Section 16 | 100% | zoom/pan 交互已统一 |
| 2.2 Program Viewer GPU Path | 100% | `_execute_render()` 先尝试 `_try_gpu_render()`，成功后走 `_on_gpu_frame_ready()` → `frame_ready.emit((tex_id, w, h))` → `ProgramViewer.set_frame(tuple)` → `set_shared_texture()`， triple-buffer 回收策略和 generation gate 已就位 |
| 2.5 Source Viewer Proxy 分辨率 | 100% | `source_viewer.py:1492` 已改为 `max(self.viewer.width(), 640)` / `max(self.viewer.height(), 360)` 动态计算 |

---

## 2. 遗留问题清单（本轮需全部解决）

### 2.1 Source GPU Provider 空壳（最高优先级）

**问题本质：**

`FileController._setup_source_gpu_provider()` 中的 provider 是一个**永远返回 None 的空壳**：

```python
# file_controller.py:963-983
def source_gpu_provider(asset_path: str, frame_index: int):
    decoded_frame, frame_type = gpu_decode_manager.get_frame(asset_path, frame_index)
    if decoded_frame is None:
        return None
    # 获取到 GPU decoded frame 后立刻释放，然后返回 None
    safe_release_gpu_frame(decoded_frame)
    return None   # ← 永远返回 None，GPU 路径永不生效
```

注释解释：
> "Full GPU texture rendering for Source Viewer requires a shared GL context renderer."

**现状：**

Source Viewer 的**架构接口已全部就绪**：
- `SourceViewer._load_video_frame()` 已改为 "GPU 优先 + CPU fallback" 结构 ✅
- `SourceViewer.set_shared_texture(tex_id, width, height)` 已实现 ✅
- `SourceViewer.set_gpu_frame_provider(provider)` 已实现 ✅
- `FileController._setup_source_gpu_provider()` 已连接 ✅

**唯一缺失的是**：把 `GpuDecodedFrame` 或 `DecodedFrame` 转换成 Source Viewer 需要的 `(tex_id, width, height)` tuple。

**根本原因：**

`EditGpuDecodeManager.get_frame()` 返回三种 frame type：
- `'gpu'` → `GpuDecodedFrame`（VideoToolbox zero-copy CVPixelBuffer，不能直接获取 OpenGL texture ID）
- `'planar'` → `DecodedFrame` with planar YUV（需 GPU 上传）
- `'cpu'` → `DecodedFrame` with packed RGB（CPU 回退）

Program Viewer 之所以不需要这个转换，是因为 `EditGpuPreviewRenderer` 内部负责把 `GpuDecodedFrame` 合成到 FBO，然后输出 FBO 的 texture ID。Source Viewer 只有一个图层，没有合成器，所以需要一个新的**轻量 GPU 渲染器**或直接从 `GpuDecodedFrame` 提取 texture。

**建议方案：**

**方案 A（推荐，与 Program 统一架构）：**

复用 `EditGpuPreviewRenderer` 的单图层渲染能力。创建一个轻量任务，把 Source 的 decoded frame 提交给 `EditGpuPreviewRenderer` 渲染到 FBO，然后获取 FBO texture ID。

```python
# 在 FileController._setup_source_gpu_provider() 中
from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask

def source_gpu_provider(asset_path: str, frame_index: int):
    decoded_frame, frame_type = gpu_decode_manager.get_frame(asset_path, frame_index)
    if decoded_frame is None:
        return None
    
    if frame_type == 'cpu':
        safe_release_gpu_frame(decoded_frame)
        return None
    
    # 提交到 EditGpuPreviewRenderer 的单图层渲染
    task = EditRenderTask(
        frame=frame_index,
        generation_id=0,  # Source viewer 不需要 generation gate
        layers=[{'gpu_frame': decoded_frame, 'frame_type': frame_type, 'track_info': {}}],
        viewport_size=(decoded_frame.width, decoded_frame.height),
        output_fbo_size=(decoded_frame.width, decoded_frame.height),
    )
    
    # 使用独立的 source_gpu_renderer 实例（避免与 timeline preview 冲突）
    submitted = self._source_gpu_renderer.submit(task)
    if not submitted:
        safe_release_gpu_frame(decoded_frame)
        return None
    
    # 等待渲染完成（同步，因为 Source Viewer 是逐帧 seek 模式）
    tex_id, width, height = self._source_gpu_renderer.wait_for_result()
    return (tex_id, width, height)
```

**方案 B（如果 GpuDecodedFrame 可直接获取 texture）：**

检查 `GpuDecodedFrame` 是否有 `texture_id` 属性或方法。如果是 ModernGL 或 OpenGL texture，直接提取返回，无需额外渲染器。

```python
# 需要检查 src/shared/media/gpu_frame.py 中 GpuDecodedFrame 的定义
if hasattr(decoded_frame, 'texture_id') and decoded_frame.texture_id is not None:
    return (decoded_frame.texture_id, decoded_frame.width, decoded_frame.height)
```

**涉及文件：**

- `src/app/controllers/file_controller.py`
  - `_setup_source_gpu_provider()` — provider 空壳修复
  - 可能需新增 `self._source_gpu_renderer` 生命周期管理
- `src/features/edit/core/edit_gpu_preview_renderer.py`
  - 确认 `EditGpuPreviewRenderer` 支持单图层 FBO 渲染
  - 可能需要新增 `wait_for_result()` 同步 API（如果当前只有异步 `frame_ready` 信号）
- `src/features/edit/ui/source_viewer.py`
  - `set_shared_texture()` 已就绪，无需修改
- `src/shared/media/gpu_frame.py`
  - 检查 `GpuDecodedFrame` 是否可直接提供 texture_id（方案 B 用）

**风险与缓解：**

| 风险 | 影响 | 缓解 |
|------|------|------|
| 创建独立的 `source_gpu_renderer` 导致 GPU 资源双倍占用 | 中 | 与 `timeline_preview_renderer` 共享同一个 `EditGpuPreviewRenderer` 实例，但使用独立的 FBO；或确保 Source Viewer 关闭时释放 FBO |
| 同步 `wait_for_result()` 阻塞 UI 线程 | 高 | Source Viewer 是逐帧 seek（非播放），阻塞 16ms 是可接受的；或者在 Source 播放时使用异步模式 |
| GpuDecodedFrame 的 CVPixelBuffer 无法直接转 OpenGL texture | 中 | 方案 A 的 FBO 渲染路径可处理任何 decoded frame 类型 |

**验证方式：**

新增 `tests/edit/test_source_viewer_actually_uses_gpu_texture.py`：

```python
class TestSourceViewerGpuProvider:
    """Verify Source Viewer GPU provider returns actual texture, not always None."""
    
    def test_gpu_provider_returns_tuple_for_gpu_decoded_frame(self):
        """When GpuDecodeManager returns 'gpu' frame, provider should return (tex_id, w, h)."""
        pass
    
    def test_gpu_provider_returns_none_for_cpu_frame(self):
        """When GpuDecodeManager returns 'cpu' frame, provider should return None (fallback)."""
        pass
    
    def test_source_viewer_prefers_gpu_over_cpu_decoder(self):
        """When GPU provider returns texture, VideoDecoder should not be created."""
        pass
    
    def test_source_viewer_releases_gpu_texture_on_close(self):
        """Source Viewer close() should release any pending GPU texture."""
        pass
```

---

### 2.2 Schema v3 测试文件名与测试补全（中优先级）

**问题 1：测试文件名未更新**

```bash
# 当前文件名
tests/project/test_topos_schema_v2.py   # 文件内容已经是 v3 测试
```

虽然文件内部的 docstring 和测试方法已经是 v3（`test_schema_version_is_3`），但文件名仍是 v2，容易引起混淆。

**修复：**

```bash
mv tests/project/test_topos_schema_v2.py tests/project/test_topos_schema_v3.py
```

**问题 2：缺少 v3 专用测试**

当前 `test_topos_schema_v2.py`（内容实为 v3）缺少以下测试：

| 缺失测试 | 说明 | 原始计划位置 |
|----------|------|-------------|
| `test_schema_v3_rejects_video_single_asset` | 保存时拒绝 `project_mode="standalone_image"` + video asset 的组合 | 原始计划 12.1 |
| `test_schema_v3_saves_comp_clip_graph` | 验证 comp_clip 的 graph 保存为 `Input → Topos Color → Output` | 原始计划 12.1 |
| `test_schema_v3_requires_topos_color_for_timeline_graph` | timeline graph 必须包含 Topos Color 节点 | 原始计划 12.1 |

**建议方案：**

1. 重命名测试文件
2. 在 `test_topos_schema_v3.py` 中新增上述 3 个测试类
3. 更新 `topos_project_io.py` 或 `save_router.py` 中的保存校验逻辑（如果尚未实现）

**涉及文件：**

- `tests/project/test_topos_schema_v2.py` → `tests/project/test_topos_schema_v3.py`
- `src/shared/project/save_router.py` — 添加保存时校验
- `tests/project/test_topos_schema_v3.py` — 新增测试

**验证方式：**

```bash
.venv/bin/python -m pytest tests/project/test_topos_schema_v3.py -q
# 期望：全部通过，且包含新的 3 个测试
```

---

### 2.3 文档测试文件名对照表（低优先级）

**问题：**

原始完成计划 `explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md` 的 12.1 节仍列出 16 个不存在的测试文件名，如：
- `tests/edit/test_create_comp_clip_explicit.py` → 实际不存在
- `tests/composite/test_preview_mode_does_not_emit_graph_changed.py` → 实际不存在
- `tests/color/test_color_updates_topos_color_node.py` → 实际不存在

这些功能实际已被其他测试覆盖，但文档未更新。

**修复：**

不需要创建这 16 个文件（已有测试覆盖），只需在两个计划文档的末尾添加**"计划文件名 → 实际文件名"对照表**：

```markdown
### 测试文件名对照表

| 计划文件名（不存在） | 实际覆盖文件 | 说明 |
|----------------------|-------------|------|
| test_create_comp_clip_explicit.py | test_comp_clip_promotion_lifecycle.py | 显式 create 生命周期 |
| test_preview_mode_does_not_emit_graph_changed.py | test_comp_clip_reset.py | preview-only 模式下 graph 不变更 |
| test_color_updates_topos_color_node.py | test_default_graph_topos_color_contract.py | Color 页面修改同步到 Topos Color 节点 |
| ... | ... | ... |
```

**涉及文件：**

- `docs/explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md` — 12.1 节更新
- `docs/explicit_comp_clip_followup_optimization_plan_2026-06-03.md` — 2.4 节更新

---

## 3. 执行顺序与依赖

```text
Phase 1（高优先级）:
  2.1 Source GPU Provider 空壳修复
    - 先调查 GpuDecodedFrame 是否可直接提供 texture_id（方案 B，最简单）
    - 若不可行，实施方案 A（轻量 FBO 渲染）
    - 新增测试 test_source_viewer_actually_uses_gpu_texture.py
    - 验证：运行 .venv/bin/python -m pytest tests/edit/ -q，确保无回归

Phase 2（中优先级，可与 Phase 1 并行）:
  2.2 Schema v3 测试文件重命名和补全
    - mv test_topos_schema_v2.py test_topos_schema_v3.py
    - 新增 3 个测试类
    - 如有需要，在 save_router.py 添加校验逻辑
    - 验证：pytest tests/project/test_topos_schema_v3.py -q

Phase 3（低优先级，收尾）:
  2.3 更新两个计划文档的 12.1 / 2.4 节
    - 添加"计划文件名 → 实际文件名"对照表
```

---

## 4. 技术上下文（供后续 Agent 参考）

### 4.1 GPU 架构现状

```
TimelinePreviewRenderer (Program Viewer)
  ├── _execute_render()
  │     ├── _try_gpu_render() → EditGpuPreviewRenderer.submit(task)
  │     │       → EditGpuPreviewRenderer.frame_ready → _on_gpu_frame_ready()
  │     │       → frame_ready.emit((tex_id, w, h)) → ProgramViewer.set_frame(tuple)
  │     └── CPU fallback: _composite_layers() → _numpy_to_qimage() → frame_rendered.emit(frame, qimage)
  └── GPU 路径已稳定工作（含 generation gate、单调帧守卫、triple-buffer 不缓存）

Source Viewer（当前）
  ├── _load_video_frame()
  │     ├── _gpu_frame_provider(asset_path, frame) ← 来自 FileController
  │     │       当前实现：get_frame() → safe_release() → return None
  │     └── CPU fallback: VideoDecoder → QImage → set_texture_from_qimage()
  └── GPU 路径接口已就绪，但 provider 空壳
```

### 4.2 GpuDecodedFrame 结构

位于 `src/shared/media/gpu_frame.py`。需要检查：
- 是否有 `.texture_id` 属性（OpenGL texture）
- 是否有 `.pixel_buffer` / `.cv_pixel_buffer` 属性（VideoToolbox）
- `.width` / `.height` 是否可用

如果底层是 `ModernGL Texture` 对象，通常可以直接获取 `texture.glo`（OpenGL texture name）。

### 4.3 Source Viewer 与 Program Viewer 的关键差异

| 特性 | Source Viewer | Program Viewer |
|------|---------------|----------------|
| 输入 | 单 asset | timeline 多图层 |
| 模式 | 逐帧 seek + 播放 | 逐帧 seek + 播放 + 实时 scrub |
| GPU provider | 空壳 | `EditGpuPreviewRenderer` 完整渲染链 |
| 显示方法 | `set_texture_from_qimage()` / `set_shared_texture()` | `set_qimage()` / `set_shared_texture()` |
| generation gate | 不需要 | 有（避免乱序帧） |
| 合成 | 无 | 多轨合成 + 转场 |

---

## 5. 测试基线

任何修改前必须先确认：

```bash
.venv/bin/python -m pytest tests/edit/ tests/composite/ tests/color/ tests/project/ -q
# 期望: 3660 passed, 19 skipped, 0 failed
```

每个 Phase 完成后重新运行，确保无回归。

---

## 6. 参考文件

| 文件 | 说明 |
|------|------|
| `src/app/controllers/file_controller.py:949-990` | `_setup_source_gpu_provider()` — 空壳 provider 位置 |
| `src/features/edit/ui/source_viewer.py:1470-1528` | `_load_video_frame()` — GPU 优先 + CPU fallback 结构 |
| `src/features/edit/core/timeline_preview_renderer.py:554-672` | `_try_gpu_render()` — Program GPU 路径参考 |
| `src/features/edit/core/edit_gpu_preview_renderer.py` | `EditGpuPreviewRenderer` — GPU 渲染器参考 |
| `src/shared/media/gpu_frame.py` | `GpuDecodedFrame` / `safe_release_gpu_frame()` 定义 |
| `src/features/edit/ui/program_viewer.py:1580-1617` | `set_frame()` — GPU tuple 处理参考 |
| `tests/project/test_topos_schema_v2.py` | 内容实为 v3，需重命名 |
| `src/shared/project/save_router.py` | 保存校验逻辑位置 |

---

*目标读者：后续执行收尾开发的 AI / 工程 Agent*  
*技术栈：PyQt6 + ModernGL + GLSL*
