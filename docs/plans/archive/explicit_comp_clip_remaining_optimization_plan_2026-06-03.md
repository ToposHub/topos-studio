# Explicit Comp Clip / Topos Color 验收后剩余优化计划（2026-06-03）

## 0. 文档目标

这份计划用于承接 `explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md` 完成后的剩余工作。

当前实现已经完成了以下基础能力：

- Edit / Color / Composite 已经以 timeline clip 作为主入口。
- 普通 clip 不再通过页面切换自动创建 Comp Clip。
- timeline-backed Composite graph 默认链路为 `Timeline Clip Input -> Topos Color -> Output`。
- Color 页面调色与 Topos Color 节点已经共享 `ColorStackData`。
- Program / Source Viewer 已具备基础滚轮缩放和中键平移能力。
- timeline-backed Comp Clip 从媒体池或 graph 直接打开时，已经可以通过 graph 自身的 timeline/clip 信息建立 PageContext。

但以下内容仍不能算完整交付：

1. `Render Cache This Clip` 仍只是 manifest / 状态入口，没有真正渲染缓存文件，也没有让 Edit / Export 优先读取缓存。
2. Composite 页面仍存在同步 readback 兜底路径，页面切换和预览在异常情况下仍可能卡顿。
3. Edit 预览调用 Composite graph 仍偏 headless CPU readback 模型，尚未形成可复用的服务级全 GPU 输出契约。
4. Render Cache 的 Preferences 设置、编码格式、分辨率、路径、清理、失效策略都未闭环。
5. Program / Source Viewer 的交互只有算法测试，缺少 widget contract 测试。
6. legacy temporary promotion / legacy ColorOutput / fallback 原素材策略仍需继续收口。

本计划要求后续 AI 严格按阶段实施。不要一次性同时改 UI、渲染器、工程文件和导出，否则很难定位回归。

---

## 1. 剩余问题总表

| ID | 优先级 | 问题 | 当前风险 | 完成标准 |
|---|---:|---|---|---|
| R-1 | P0 | Render Cache 只有 manifest，没有实际缓存文件 | Comp Clip 播放仍无法实时，菜单行为像“假功能” | 右键 Render Cache 后生成真实缓存文件，Edit 播放优先读缓存 |
| R-2 | P0 | Composite 同步 readback 兜底仍在 UI 路径 | Color/Composite 来回切换仍可能卡顿 | 页面切换和预览热路径禁止调用同步 readback |
| R-3 | P0 | Edit 调用 Composite graph 仍走 CPU readback | 复杂 Comp Clip 播放性能差，无法称为全 GPU 链路 | 建立 Composite graph 服务级 GPU frame provider |
| R-4 | P1 | Render Cache Preferences 未接入项目设置 | 用户无法选择缓存编码、格式、分辨率 | Preferences 可设置并写入 `.topos` |
| R-5 | P1 | Cache invalidation 不完整 | 调色/节点修改后可能播放旧缓存 | ColorStack / graph / source / timeline 改动自动标脏 |
| R-6 | P1 | Cache job 生命周期缺失 | 无进度、无失败状态、无法取消 | 有 queued/rendering/valid/stale/failed/cancelled 全状态 |
| R-7 | P1 | Program / Source Viewer widget contract 测试缺失 | UI 交互后续容易回归 | 补齐 resize、wheel、middle pan 的 widget 测试 |
| R-8 | P1 | Composite graph 失败时 fallback 原素材策略不统一 | 用户以为合成效果生效，实际悄悄播放原素材 | 明确显示错误 overlay 或按 Bypass 策略处理 |
| R-9 | P2 | legacy temporary promotion API 仍保留 | 后续 AI 可能误用旧模型 | 仅 migration/test helper 可访问，生产代码不可调用 |
| R-10 | P2 | legacy ColorOutput 路径仍存在 | Topos Color 与旧 ColorOutput 混用造成旧帧/卡顿 | 新 timeline graph 不依赖 ColorOutput；legacy 图只走 migration |

---

## 2. Phase 1：Render Cache 真闭环

### 2.1 目标

把当前 `Render Cache This Clip` 从“创建 manifest”升级为完整链路：

```text
用户右键 Render Cache This Clip
  -> 创建 RenderCacheJob
  -> 后台逐帧渲染 Color + Composite 输出
  -> 编码成缓存文件
  -> 原子写入 cache path
  -> manifest.status = valid
  -> clip.render_cache_id = cache_id
  -> Edit / Export 优先读取缓存
```

### 2.2 涉及文件

```text
src/features/edit/core/render_cache.py
src/features/edit/core/project.py
src/features/edit/core/timeline_v2.py
src/features/edit/core/timeline_preview_renderer.py
src/features/composite/render/composite_frame_renderer.py
src/app/controllers/edit_controller.py
src/app/controllers/file_controller.py
src/app/ui/preferences_dialog.py
tests/edit/test_render_cache_job_lifecycle.py
tests/edit/test_timeline_preview_uses_render_cache.py
tests/edit/test_render_cache_invalidation.py
tests/project/test_render_cache_settings_roundtrip.py
```

### 2.3 数据模型扩展

在 `src/features/edit/core/render_cache.py` 增加：

```python
class RenderCacheStatus:
    MISSING = "missing"
    QUEUED = "queued"
    RENDERING = "rendering"
    VALID = "valid"
    STALE = "stale"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

扩展 `RenderCacheManifest`：

```python
cache_id: str
timeline_id: str
clip_id: str
composite_id: str
path: str
temp_path: str
codec: str
pixel_format: str
width: int
height: int
fps_num: int
fps_den: int
start_frame: int
duration_frames: int
source_asset_revision: str
color_stack_revision: int
composite_graph_revision: int
timeline_edit_revision: int
created_at: float
updated_at: float
status: str
error: str
```

新增 `RenderCacheJob`：

```python
@dataclass
class RenderCacheJob:
    job_id: str
    cache_id: str
    timeline_id: str
    clip_id: str
    start_frame: int
    duration_frames: int
    current_frame: int = 0
    cancelled: bool = False
```

### 2.4 Worker 实施步骤

新增文件：

```text
src/features/edit/core/render_cache_worker.py
```

职责：

1. 接收 `RenderCacheJob`、`EditProject`、`TimelineSequence`、`RenderCacheSettings`。
2. 为 clip 范围内每一帧构造准确的 `timeline_frame`。
3. 调用 Composite graph 渲染服务得到最终帧。
4. 将帧交给编码器写入临时文件。
5. 结束后将临时文件原子 rename 到正式 cache path。
6. 成功时写 `status=valid`。
7. 失败时写 `status=failed` 和 `error`。
8. 取消时写 `status=cancelled` 并删除 temp file。

第一阶段允许使用 CPU readback 编码，但必须保持架构上可替换为 GPU encoder：

```python
class RenderCacheWorker(QObject):
    progress_changed = pyqtSignal(str, int, int)  # cache_id, done, total
    finished = pyqtSignal(str)                    # cache_id
    failed = pyqtSignal(str, str)                 # cache_id, error

    def run(self) -> None:
        ...
```

### 2.5 编码器接口

新增文件：

```text
src/features/edit/core/render_cache_encoder.py
```

接口：

```python
class RenderCacheEncoder:
    def open(self, path: str, width: int, height: int, fps: Rational, settings: RenderCacheSettings) -> None: ...
    def write_frame_bgr(self, frame: np.ndarray) -> None: ...
    def close(self) -> None: ...
    def abort(self) -> None: ...
```

第一阶段实现：

- macOS 优先 ProRes Proxy / ProRes 422。
- 如果项目已有 FFmpeg/GStreamer encoder helper，复用现有模块。
- 不要在 UI 线程编码。
- temp 文件路径使用：

```text
<cache_root>/render_cache/<project_id>/<cache_id>.tmp.mov
```

正式路径：

```text
<cache_root>/render_cache/<project_id>/<cache_id>.mov
```

### 2.6 Edit 预览读取缓存

在 `TimelinePreviewRenderer._get_layer_frame()` 或更早的 layer resolve 阶段增加：

1. 如果 `clip.render_cache_id` 存在：
   - 从 `EditProject.render_cache_manifests` 找 manifest。
   - 检查 `status == valid`。
   - 检查文件存在。
   - 检查 `manifest.is_valid_for(...)`。
2. 如果有效：
   - `cache_local_frame = timeline_frame - manifest.start_frame`
   - 从 cache 视频读取对应帧。
   - 返回缓存帧。
3. 如果无效：
   - 不删除 manifest，只标记 stale 或跳过。
   - 继续走实时 Composite 渲染路径。

注意：

- 缓存读取必须支持随机 seek。
- 不要因为缓存 miss 卡 UI。
- 预览线程可以读缓存；UI 线程不能直接读文件帧。

### 2.7 Export 读取缓存

Deliver / Export timeline 时：

1. 对每个 layer 判断是否有 valid render cache。
2. 有 valid cache 时直接读缓存帧。
3. 没有 cache 时走正常 render graph。
4. 导出最终画面必须与未缓存结果一致。

### 2.8 Controller 行为

修改 `src/app/controllers/edit_controller.py::_on_render_cache_clip_requested()`：

当前行为：

```text
创建 manifest -> status=rendering -> 结束
```

目标行为：

```text
创建或复用 manifest
status=queued
启动 RenderCacheWorker
status=rendering
progress 更新 timeline clip cache line
完成后 status=valid
clip.with_render_cache_id(cache_id)
刷新 timeline + program monitor
```

### 2.9 验证方法

自动测试：

```bash
.venv/bin/python -m pytest \
  tests/edit/test_render_cache_job_lifecycle.py \
  tests/edit/test_timeline_preview_uses_render_cache.py \
  tests/edit/test_render_cache_invalidation.py \
  -q
```

手工验收：

1. 时间线放入一个视频 clip。
2. 右键 `Create Comp Clip`。
3. 进入 Composite 添加明显效果，例如 Blur / Invert / Transform。
4. 回 Edit，播放确认实时路径可看到效果。
5. 右键 `Render Cache This Clip`。
6. 等进度完成，clip 顶部 cache line 变为 ready。
7. 关闭 Composite 页面。
8. 播放该 clip，画面仍包含合成效果，且不会重新触发 Composite graph 实时渲染日志。
9. 修改 Color 页面曝光，cache line 变 dirty/stale。
10. 再次 Render Cache，生成新缓存。

---

## 3. Phase 2：Render Cache 失效策略

### 3.1 目标

任何影响最终输出的操作都必须让对应缓存失效。

### 3.2 必须触发失效的操作

| 操作 | 失效范围 |
|---|---|
| ColorStack 参数变化 | 引用该 stack 的 clip cache |
| Topos Color 节点参数变化 | 当前 graph 对应 clip cache |
| Composite 节点增删连线 | 当前 graph 对应 clip cache |
| Composite 节点参数变化 | 当前 graph 对应 clip cache |
| clip trim / move / speed / source_start 变化 | 当前 clip cache |
| timeline fps / 分辨率变化 | 整条 timeline cache |
| 原始媒体 relink / proxy 切换 | 引用该 asset 的所有 cache |
| Bypass Composite 切换 | 当前 clip cache 可不删，但播放选择变更必须刷新 |

### 3.3 实施方法

在 `EditProject.invalidate_render_cache_for_clip()` 基础上扩展：

```python
def invalidate_render_cache_for_color_stack(self, stack_id: str) -> EditProject: ...
def invalidate_render_cache_for_composite(self, composite_id: str) -> EditProject: ...
def invalidate_render_cache_for_asset(self, asset_id: str) -> EditProject: ...
def invalidate_render_cache_for_timeline(self, timeline_id: str) -> EditProject: ...
```

失效时：

1. manifest.status 改为 `stale`。
2. 不立即删除缓存文件。
3. timeline clip UI 显示 dirty cache line。
4. 下次播放默认实时渲染，不读 stale cache。
5. 用户可手动清理 stale cache。

### 3.4 测试

新增：

```text
tests/edit/test_render_cache_invalidation.py
```

测试点：

1. 修改 ColorStack 后对应 cache stale。
2. 修改 Composite graph 后对应 cache stale。
3. 移动 clip 后对应 cache stale。
4. 修改无关 clip 不影响其它 cache。
5. stale cache 不被 TimelinePreviewRenderer 使用。

---

## 4. Phase 3：Preferences 接入 Render Cache 设置

### 4.1 目标

用户可以在 Preferences 中设置：

- Render Cache 启用模式：Off / User / Smart。
- 编码格式：ProRes Proxy / ProRes 422 / H.264 Proxy / DNxHR LB。
- Pixel format：8-bit / 10-bit。
- 分辨率：Timeline / Half / Quarter / Custom。
- Cache root 路径。
- 最大缓存大小。

### 4.2 涉及文件

```text
src/app/ui/preferences_dialog.py
src/features/edit/core/render_cache.py
src/features/edit/core/project.py
src/shared/project/topos_project_io.py
src/shared/project/manifest.py
tests/project/test_render_cache_settings_roundtrip.py
```

### 4.3 实施步骤

1. 扩展 `RenderCacheSettings`：

```python
auto_mode: str          # off/user/smart
codec: str             # prores_422_proxy/prores_422/h264_proxy/dnxhr_lb
pixel_format: str      # yuv420p/yuv422p10
resolution_mode: str   # timeline/half/quarter/custom
custom_width: int | None
custom_height: int | None
cache_root: str
max_size_gb: float
```

2. Preferences UI 增加 `Render Cache` 分组，不要混在普通 image cache 里。
3. `EditProject.render_cache_settings` 保存为 dict。
4. `.topos` 保存/加载必须 roundtrip。
5. 修改设置后不立即删除旧缓存，只影响新 cache job。

### 4.4 验证

```bash
.venv/bin/python -m pytest tests/project/test_render_cache_settings_roundtrip.py -q
```

手工：

1. 打开 Preferences。
2. 修改 Render Cache codec 为 ProRes Proxy。
3. 保存项目。
4. 重新打开项目。
5. 设置保持不丢。
6. 新生成缓存文件使用该设置。

---

## 5. Phase 4：同步 readback 从页面切换热路径移除

### 5.1 当前风险

代码中仍能看到类似路径：

```text
Composite frame ready
  -> direct texture 失败
  -> request_output_readback(timeout=...)
  -> UI 线程等待
```

这会造成：

- Color -> Composite 切换卡顿。
- Composite -> Edit 切换卡顿。
- 大分辨率/复杂 graph 下 UI 假死。

### 5.2 目标

热路径规则：

```text
页面切换：0 次同步 readback
播放：0 次同步 readback
拖动节点参数：0 次同步 readback
调色滑块实时变化：0 次同步 readback
```

允许 readback 的场景：

- 最终编码器需要 CPU 帧时。
- 手动截图 / debug probe。
- 单元测试中显式调用。
- PBO async readback 回调完成后交给 viewer。

### 5.3 涉及文件

```text
src/app/controllers/composite_controller.py
src/features/composite/adapters/main_window_adapter.py
src/features/composite/render/composite_renderer.py
src/features/composite/render/pbo_readback.py
src/features/composite/ui/composite_page.py
tests/composite/test_async_present_readback.py
tests/composite/test_no_sync_readback_on_page_switch.py
```

### 5.4 实施步骤

1. 搜索所有同步 readback：

```bash
rg -n "request_output_readback|read\\(|glReadPixels|timeout=.*readback|sync readback" src/app src/features
```

2. 对 UI 热路径做硬性替换：

```python
if tex_id > 0:
    viewer.set_texture(tex_id, width, height)
else:
    renderer.submit(CompositeCommand.READBACK_PRESENT_ASYNC, ...)
```

3. `CompositePage.request_output_readback()` 保留，但加注释：

```text
Only for explicit debug/export fallback. Never call from page switch or playback.
```

4. 给 `MainWindowAdapter` / controller 增加测试，确保页面切换时不会调用 `request_output_readback()`。

5. 对 fallback 做限速：

```text
async readback max 24fps
只显示最新 request_id
旧 request_id 丢弃
```

### 5.5 验证

自动测试：

```bash
.venv/bin/python -m pytest \
  tests/composite/test_async_present_readback.py \
  tests/composite/test_no_sync_readback_on_page_switch.py \
  -q
```

手工：

1. 打开 4K 视频时间线。
2. 选中 Comp Clip，Edit -> Composite 连续切换 20 次。
3. 日志不能出现 UI thread sync readback。
4. 页面切换平均耗时应低于 100ms。
5. 调色滑块拖动后切 Composite 不假死。

---

## 6. Phase 5：Composite 服务级全 GPU 输出契约

### 6.1 目标

把 Composite graph 从“Composite 页面内部渲染器”升级成可被 Edit / Render Cache / Deliver 共同调用的服务。

目标数据流：

```text
Timeline clip GPU frame
  -> Topos Color GPU shader
  -> Composite graph GPU evaluator
  -> TextureResult / GpuFrameHandle
  -> Edit Program Viewer 或 Render Cache Encoder
```

在播放热路径中不要变成：

```text
GPU -> CPU readback -> numpy -> QImage -> GPU upload
```

### 6.2 新增接口

新增文件：

```text
src/features/composite/render/composite_graph_render_service.py
src/shared/gpu/gpu_frame_handle.py
```

建议接口：

```python
@dataclass(frozen=True)
class GpuFrameHandle:
    texture_id: int
    width: int
    height: int
    color_space: str
    orientation: str        # gl_bottom_left / image_top_left
    owner_context_id: str
    revision: int

class CompositeGraphRenderService:
    def render_gpu(
        self,
        graph_data: dict,
        runtime_context: CompositeRuntimeContext,
        quality: str,
    ) -> GpuFrameHandle | None:
        ...

    def render_cpu_for_encoder(
        self,
        graph_data: dict,
        runtime_context: CompositeRuntimeContext,
        quality: str,
    ) -> np.ndarray | None:
        ...
```

### 6.3 实施步骤

1. 把 `CompositeFrameRenderer` 中“构造 graph + runtime context + provider”的逻辑抽成服务。
2. `render_gpu()` 返回 texture handle，不读回 CPU。
3. `render_cpu_for_encoder()` 只给 Render Cache / Export fallback 使用。
4. Edit Program Viewer 优先接收 `GpuFrameHandle`。
5. 如果 GL context sharing 不可用：
   - 明确日志 `gpu_path=unavailable reason=...`
   - 降级到 async readback，而不是 sync readback。
6. 每次渲染记录 telemetry：

```text
clip_id
timeline_frame
source_frame
path=zero_copy/gpu_upload/async_readback/cpu_fallback
elapsed_ms
```

### 6.4 验证

新增测试：

```text
tests/composite/test_composite_graph_render_service_contract.py
tests/edit/test_edit_preview_prefers_gpu_composite_output.py
tests/composite/test_composite_gpu_fallback_is_async.py
```

手工：

1. 打开带 Topos Color + Blur 的 Comp Clip。
2. Edit 页面播放。
3. 日志显示 `path=zero_copy` 或 `path=gpu_upload`。
4. 不出现每帧 QImage upload。
5. 关闭 Composite 页面后，Edit 仍能通过服务渲染该 graph。

---

## 7. Phase 6：Composite 失败策略统一

### 7.1 当前风险

当前部分路径在 Composite graph 渲染失败时会 fallback 到原素材。这会造成：

- 用户添加了合成效果，但 Edit 里看不到。
- 用户以为效果没有生效。
- 真正错误被隐藏。

### 7.2 目标

分三种情况：

| 情况 | 行为 |
|---|---|
| 用户主动 Bypass Composite | 显示原素材，timeline clip 有 bypass 标记 |
| Composite graph 渲染失败 | 显示错误 overlay，不静默 fallback |
| graph 丢失/工程损坏 | 显示 missing comp graph 状态 |

### 7.3 实施步骤

1. 在 `TimelinePreviewRenderer._get_layer_frame()` 中：
   - `clip.composite_bypassed=True` 才允许原素材 fallback。
   - graph render failed 时返回 `CompositeFailureFrame` 或设置错误状态。
2. Program Viewer 显示：

```text
Composite render failed
<short error>
```

3. Timeline clip badge 显示红色 warning。
4. 右键菜单提供：
   - Retry Composite Render
   - Bypass Composite
   - Reset Composite

### 7.4 测试

```text
tests/edit/test_composite_failure_policy.py
```

测试点：

1. graph 失败不 fallback 原素材。
2. bypass 时才 fallback 原素材。
3. missing graph 显示错误状态。
4. reset graph 后错误消失。

---

## 8. Phase 7：Program / Source Viewer widget contract 测试补齐

### 8.1 当前状态

已有：

- `PreviewViewTransform` 算法。
- ProgramViewer 基础滚轮缩放。
- SourceViewer 基础滚轮缩放。
- 中键平移。

缺少：

- Qt widget 层 contract 测试。
- `resizeGL()` 不重置 manual zoom 的测试。
- Source 每帧更新不 reset zoom 的测试。

### 8.2 涉及文件

```text
src/features/edit/ui/program_viewer.py
src/features/edit/ui/source_viewer.py
tests/edit/test_program_viewer_transform_contract.py
tests/edit/test_source_viewer_transform_contract.py
```

### 8.3 测试要求

`test_program_viewer_transform_contract.py`：

1. 新建 ProgramViewer。
2. 设置 texture size。
3. wheelEvent 后 `mode == manual`。
4. resizeGL 后 zoom 不变。
5. 中键拖动后 pan 改变。
6. 按 F 后回到 fit。
7. 按 1 后进入 actual。

`test_source_viewer_transform_contract.py`：

1. 新建 SourceViewer。
2. set_texture 后 fit。
3. wheelEvent 后 manual。
4. 连续 set_texture 同尺寸不 reset manual zoom。
5. `SourceViewerWidget.open_asset()` 新素材时 reset fit。
6. 播放帧更新不 reset zoom。

### 8.4 验证

```bash
.venv/bin/python -m pytest \
  tests/edit/test_preview_view_transform.py \
  tests/edit/test_program_viewer_transform_contract.py \
  tests/edit/test_source_viewer_transform_contract.py \
  -q
```

---

## 9. Phase 8：legacy 路径收口

### 9.1 目标

减少后续 AI 误用旧模型的机会。

### 9.2 必须处理的遗留点

1. `_legacy_promote_composition_to_comp_clip()`
   - 保留仅用于 migration / legacy tests。
   - 生产 controller 不允许调用。
   - 函数 docstring 明确 `DO NOT CALL FROM UI`.

2. `_ensure_temporary_composition_for_clip()`
   - 保持 stub。
   - 加测试确保普通 clip 返回 None。

3. legacy `ColorOutput` / `color_input`
   - timeline-backed 新 graph 不允许创建 `color_input`。
   - 旧 graph load 时可以 migration 到 `Topos Color`。
   - `requires_color_output_input()` 只能对旧图返回 True。

4. `request_output_readback()`
   - 不允许被 page switch / playback 调用。
   - 只允许 debug/export fallback。

### 9.3 搜索命令

```bash
rg -n "_legacy_promote_composition_to_comp_clip|_ensure_temporary_composition_for_clip|color_input|request_output_readback" src tests
```

### 9.4 验证

```bash
.venv/bin/python -m pytest \
  tests/app/test_no_pending_comp_clip_context_in_page_switch.py \
  tests/edit/test_composite_direct_entry_does_not_mutate_project.py \
  tests/composite/test_no_color_output_for_topos_graph.py \
  -q
```

---

## 10. 建议执行顺序

不要按文件顺序做，按风险顺序做：

1. **先做 Phase 7 widget contract 测试。**
   - 风险最低。
   - 可以快速锁住已实现的 Program / Source 交互。

2. **再做 Phase 4 同步 readback 清理。**
   - 直接解决页面切换卡顿。
   - 不应依赖 Render Cache。

3. **再做 Phase 6 Composite 失败策略。**
   - 避免用户误以为合成效果没生效。
   - 能让后续 Render Cache 失败更可见。

4. **再做 Phase 1 + Phase 2 Render Cache 真闭环。**
   - 工作量最大。
   - 必须分 worker、encoder、preview reader、invalidation 四步做。

5. **再做 Phase 3 Preferences。**
   - 在 Render Cache worker 可用后接入设置。

6. **最后做 Phase 5 服务级全 GPU 输出契约。**
   - 这是性能终局。
   - 可以先让 Render Cache 用 CPU encoder fallback，但接口要为 GPU 输出预留。

7. **最后收口 Phase 8 legacy 路径。**
   - 等新路径测试足够完整后再删旧入口。

---

## 11. 每阶段必须运行的测试

基础回归：

```bash
.venv/bin/python -m pytest \
  tests/app/test_composite_graph_open_context.py \
  tests/edit/test_composite_direct_entry_does_not_mutate_project.py \
  tests/edit/test_comp_clip_reset.py \
  tests/edit/test_comp_clip_timeline_preview.py \
  tests/edit/test_comp_clip_promotion_lifecycle.py \
  tests/composite/test_default_graph_topos_color_contract.py \
  tests/composite/test_timeline_clip_input_render_contract.py \
  tests/project/test_topos_schema_v3.py \
  tests/project/test_comp_clip_save_integrity.py \
  -q
```

Render Cache 专项：

```bash
.venv/bin/python -m pytest \
  tests/edit/test_render_cache_job_lifecycle.py \
  tests/edit/test_timeline_preview_uses_render_cache.py \
  tests/edit/test_render_cache_invalidation.py \
  tests/project/test_render_cache_settings_roundtrip.py \
  -q
```

GPU / readback 专项：

```bash
.venv/bin/python -m pytest \
  tests/composite/test_async_present_readback.py \
  tests/composite/test_no_sync_readback_on_page_switch.py \
  tests/composite/test_composite_gpu_fallback_is_async.py \
  -q
```

Viewer 专项：

```bash
.venv/bin/python -m pytest \
  tests/edit/test_preview_view_transform.py \
  tests/edit/test_program_viewer_transform_contract.py \
  tests/edit/test_source_viewer_transform_contract.py \
  -q
```

最终整体验收：

```bash
.venv/bin/python -m pytest tests/edit tests/composite tests/project tests/app -q
```

---

## 12. 手工总体验收矩阵

| 用例 | 操作 | 通过标准 |
|---|---|---|
| 普通 clip 直接进 Composite | 选中普通视频 clip，切 Composite | 不创建 Comp Clip，不修改工程，预览当前 clip |
| 显式创建 Comp Clip | 右键 Create Comp Clip | 媒体池出现 Comp Clip，时间线 clip 有 COMP/C 标记 |
| 媒体池打开 timeline-backed Comp Clip | 双击媒体池 Comp Clip | 打开 graph 自己绑定的 clip，不显示其它选中镜头 |
| 空白 Comp Clip | 媒体池 New Comp Clip 后双击 | 不绑定 Edit 当前 clip，显示空白 graph |
| Color -> Composite | 调色后进入 Composite | Topos Color 节点读取同一 ColorStack |
| Composite -> Edit | Composite 加 Invert/Blur 后回 Edit | Edit Program 显示合成效果，不静默原素材 |
| Render Cache | 对 Comp Clip 执行 Render Cache | 生成文件，cache line ready，播放读缓存 |
| Cache invalidation | 修改 Color 或 Composite 参数 | cache line dirty/stale，不读旧缓存 |
| Bypass Composite | 右键 Bypass Composite | 播放原素材，clip 有 bypass 标记 |
| Composite render failed | 制造坏 graph | Program 显示错误 overlay，不静默原素材 |
| 页面切换性能 | Edit/Color/Composite 快速切换 20 次 | 无明显卡死，无 sync readback 日志 |
| Program Viewer | 鼠标滚轮缩放，中键拖动，resize 面板 | manual zoom/pan 保持 |
| Source Viewer | 打开素材、播放、滚轮缩放 | 播放帧更新不重置 zoom |

---

## 13. 明确不要做的事

1. 不要恢复“切到 Composite 自动创建 Comp Clip”的旧方案。
2. 不要让视频绕过 Edit 时间线进入 standalone Color / Composite。
3. 不要在播放热路径做同步 `glReadPixels`。
4. 不要让 Render Cache manifest 显示 valid，但磁盘文件不存在。
5. 不要在 Composite render failed 时静默 fallback 原素材，除非用户明确开启 Bypass。
6. 不要把 viewer transform 写进 `.topos` 第一阶段；它是 UI 状态，不是创作数据。
7. 不要把旧 `ColorOutput` 当成新 graph 的主输入。

---

## 14. 当前验收结论

当前版本可以继续在显式 Comp Clip / Topos Color 统一方向上推进，不需要推倒重做。

真正剩下的大问题是：

1. **Render Cache 尚未成为真实播放能力。**
2. **Composite 输出尚未完全服务化为 Edit / Cache / Export 共用的 GPU frame provider。**
3. **同步 readback 和 CPU fallback 还需要从页面切换、播放热路径里彻底拿掉。**

建议后续 AI 先完成“测试补齐 + readback 清理 + failure policy”，再进入 Render Cache 真闭环。这样每一步都有明确可验证结果，不会把渲染管线改成一团难追的状态。
