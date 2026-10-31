# Edit / Color / Composite 渲染链路稳定化与全 GPU 优化计划

日期：2026-06-01  
目标读者：后续负责执行修复的 AI / 工程 Agent  
适用范围：Edit 时间线、Color 调色、Composite 合成三页之间的 clip 绑定、渲染方向、页面切换、GPU 解码渲染链路、Composite 主预览显示链路。

---

## 0. 当前结论

### 0.1 现在不是完整全 GPU 闭环

当前项目已经有较多 GPU 化基础设施，但 Edit / Color / Composite 三页之间还没有形成稳定的全 GPU 闭环。

| 页面 / 链路 | 当前状态 | 是否全 GPU | 主要证据 | 风险 |
|---|---|---:|---|---|
| Edit 时间线预览 | 有 `TimelinePreviewRenderer`、共享 GL context、GPU render thread | 部分是 | `src/features/edit/core/timeline_preview_renderer.py`、`src/app/controllers/file_controller.py` | 依赖具体解码路径；proxy/master fallback 可能回 CPU packed frame |
| Color 页面 | `AsyncRenderer` GPU 渲染，`ColorOutput` 离屏纹理 | 部分是 | `src/features/color/core/async_renderer.py`、`src/features/color/core/color_output.py` | ColorOutput 是旧 COLOR 节点输入，不应成为新 Composite 链路必需项 |
| Composite 节点计算 | ModernGL render thread + FBO + shader evaluator | 大部分是 | `src/features/composite/render/composite_renderer.py`、`src/features/composite/evaluators/*` | macOS OpenGL 4.1 不支持 compute shader，部分大半径效果走 fragment fallback |
| Composite 主预览显示 | 当前临时使用 `request_output_readback()` + `set_image_data()` | 否 | `src/features/composite/adapters/main_window_adapter.py`、`src/features/composite/ui/composite_viewport.py` | 同步 readback 会卡顿；方向容易和 GPU texture origin 混乱 |
| Composite TimelineClipInput 视频源 | proxy 失败后 master CPU 解码，packed/YUV 再上传 GPU | 否 | `src/features/edit/core/timeline_clip_frame_provider.py` | CPU decode/upload 打断零拷贝；多次切页会重复解码 |
| macOS VideoToolbox / IOSurface zero-copy | 有探测与基础模块 | 未贯通到 Composite | `src/shared/media/macos_vt/*`、`src/shared/media/frame_path.py` | Composite 仍拿 CPU `DecodedFrame` 或跨 context texture id |

### 0.2 当前用户可见问题

| 问题 | 现象 | 当前判断 |
|---|---|---|
| 从 Edit 直接进 Composite 上下翻转 | 未调色时翻转 | source_raw 路径和 readback/local viewer 路径的纹理 origin 不一致 |
| Color 调色后再进 Composite 方向正常 | 调色后反而正常 | ColorOutput / Topos Color / FBO 路径与 direct source_raw 路径走了不同 origin 约定 |
| Color 与 Composite 来回切换卡死 | 调色后切页很慢甚至卡住 | 同步 readback + ColorOutput 重渲染 + Composite 评估重复触发，UI 线程阻塞 |
| 绿色/红条曾经出现 | 已阶段性缓解 | 绿色来自跨 GL context texture id 直接显示不可靠；红条来自源帧失败或上传失败的 debug placeholder |

### 0.3 必须停止的临时策略

以下做法只能作为诊断阶段临时方案，不能作为最终架构：

1. 主预览每帧同步调用 `CompositeRenderer.readback_output(timeout=5.0)`。
2. 用 `TimelineClipFrameProvider._prepare_packed_frame_upload()` 内部随意 `flipud()` 来修方向。
3. 进入 Composite 时无条件请求 Color 页 `ColorOutput`。
4. 把 render thread 的 texture id 直接给 `QOpenGLWidget` 显示，却只用 texture size 判断 context sharing 是否有效。

---

## 1. 总体目标架构

### 1.1 唯一素材入口

视频进入 Composite 的唯一入口应为 Edit 时间线 clip：

```text
Edit Timeline Clip
  -> TimelineClipInput(source_raw)
  -> Topos Color(optional, reads clip ColorStackData)
  -> Composite nodes
  -> Output
  -> Composite Viewer
```

旧 `COLOR/color_input` 节点只保留为旧项目迁移兼容，不再作为新链路依赖。

### 1.2 统一纹理方向约定

必须定义并执行一个全局约定：

```text
GPU texture storage: OpenGL origin, bottom-left.
CPU image arrays: UI/image origin, top-left.
Renderer FBO readback: returns CPU top-left BGR/RGB by default.
Viewer local upload: accepts CPU top-left image and internally converts once.
```

关键要求：

- 解码器返回的 CPU frame 不直接混入 GPU origin 语义。
- 每个 `TextureResult` 必须携带或可推导 `origin`，至少内部实现要清楚当前纹理是 `gl_bottom_left` 还是 `ui_top_left_uploaded`。
- 翻转只能发生在明确边界：CPU -> GL upload 或 GL -> CPU readback，不能在多个函数里隐式重复。

### 1.3 主预览显示优先级

最终目标：

```text
优先：共享 GL context 直接显示 renderer output texture
  -> 失败：异步 PBO readback 到 viewer-local texture
  -> 最后：同步 readback 仅用于截图/调试/一次性 fallback
```

不能在 UI 线程等待 `readback_output(timeout=5.0)` 作为常规预览路径。

---

## 2. 阶段一：立即修复方向一致性

### 2.1 要改的文件

| 文件 | 目标 |
|---|---|
| `src/features/edit/core/timeline_clip_frame_provider.py` | 去掉临时、路径相关的随意 `flipud()`；引入明确 origin 处理 |
| `src/features/composite/render/composite_renderer.py` | 明确 renderer FBO readback 输出方向 |
| `src/features/composite/ui/composite_viewport.py` | 明确 `set_image_data()` 接收的 CPU 图像方向，统一本地纹理上传 |
| `tests/edit/test_timeline_clip_frame_provider.py` | 增加方向测试 |
| `tests/composite/test_composite_viewer_readback_origin.py`（新建） | 增加 readback -> viewer 上传方向测试 |

### 2.2 实施步骤

1. 在 `TextureResult` 或 `_TexResult` 增加可选字段：

```python
origin: str = "gl_bottom_left"  # 可选值: gl_bottom_left, cpu_top_left
```

如果不想改公共 dataclass，可先在 `frame_metadata` 中放：

```python
frame_metadata={"origin": "gl_bottom_left"}
```

2. 修改 `TimelineClipFrameProvider._prepare_packed_frame_upload()`：

当前有：

```python
np.flipud(data)
np.flipud(upload_data)
```

执行 AI 应当先判断当前代码是否还存在这些翻转。

推荐修法：

- CPU 解码帧是 top-left。
- 上传到 GL texture 时，如果 shader 采样按 OpenGL bottom-left 解释，则 CPU->GPU 上传边界翻转一次。
- 但要保证 ColorOutput / Topos Color / FBO 路径同样遵守同一个约定。

具体实现建议：

```python
def _upload_cpu_top_left_to_gl_texture(data):
    upload_data = np.ascontiguousarray(np.flipud(data))
    tex = ctx.texture(..., data=upload_data.tobytes(), ...)
    return _TexResult(..., frame_metadata={"origin": "gl_bottom_left"})
```

不要在 `set_image_data()` 再假设这类 texture 需要反向处理。

3. 修改 `CompositeRenderer._handle_readback()`：

当前 readback 做了：

```python
arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
bgr = cv2.cvtColor(np.flipud(arr[:, :, :3]).copy(), cv2.COLOR_RGB2BGR)
```

保留这个函数输出为 CPU top-left BGR，并写入注释和测试：

```python
result_holder["data_origin"] = "cpu_top_left"
result_holder["data_color_order"] = "bgr"
```

4. 修改 `CompositeViewer.set_image_data()`：

明确接收 CPU top-left BGR float/uint8。

推荐实现：

```python
# input: CPU top-left BGR
rgb = bgr[:, :, [2, 1, 0]]
# upload to OpenGL texture: flip once to bottom-left
rgb_for_gl = np.ascontiguousarray(np.flipud(rgb))
glTexImage2D(..., GL_RGB, ..., rgb_for_gl)
```

当前 `set_image_data()` 只做 BGR->RGB，没有 flip。若 renderer readback 已经返回 top-left，则本地 viewer 纹理上传时必须 flip 一次，否则 QOpenGLWidget shader 采样会出现上下颠倒。

5. 删除任何“为了某条路径正常”而散落在业务函数中的方向修补。

重点搜索：

```bash
rg -n "flipud|origin|readback|set_image_data|_prepare_packed_frame_upload" src/features
```

### 2.3 验证方法

1. 准备一个方向测试视频：画面上方写 `TOP`，下方写 `BOTTOM`，左上角有红点，右下角有蓝点。
2. 用同一个 clip 验证：

| 场景 | 预期 |
|---|---|
| Edit 直接切 Composite，未调色 | TOP 在上，BOTTOM 在下 |
| Edit -> Color 调色 -> Composite | TOP 在上，BOTTOM 在下 |
| Composite 添加 Topos Color 节点 | TOP 在上，BOTTOM 在下 |
| Composite 添加 Blur / Transform / Output | TOP 在上，BOTTOM 在下 |
| 切回 Edit 再进 Composite | 方向不变 |

3. 自动化测试：

- 新增 `tests/edit/test_timeline_clip_frame_provider.py::test_cpu_top_left_upload_origin_contract`
- 新增 `tests/composite/test_viewer_image_upload_origin.py`
- 测试用 2x2 像素数组：

```text
CPU top-left:
red    green
blue   white
```

readback -> viewer upload -> shader/display 后仍应视觉 top-left 为 red。

---

## 3. 阶段二：解除 Color / Composite 切页卡死

### 3.1 根因

目前切页卡顿来自多个因素叠加：

1. `MainWindowAdapter._present_frame_to_viewer()` 使用同步 `request_output_readback()`。
2. `CompositeRenderer.readback_output()` 在 UI 线程等待 render thread 完成，最长 5 秒。
3. Color 调参后 `ColorOutput` 可能触发离屏渲染。
4. Composite 页面进入时可能重复 `on_enter -> EVALUATE_GRAPH -> color_output_changed -> update_color_output -> EVALUATE_GRAPH`。
5. `TimelineClipFrameProvider` proxy 失败时每次回退 master，可能重复 CPU 解码同一帧。

### 3.2 实施步骤

#### 3.2.1 移除 Topos Color 新链路对 ColorOutput 的依赖

检查并保留以下逻辑：

- `CompositePage.requires_color_output_input()` 只在图中存在 `color_input` 节点时返回 True。
- `PageManagementMixin` 只有 `requires_color_output_input()` 为 True 时才调用 `engine.request_color_output()`。
- `MainWindowAdapter.on_color_output_changed()` 如果当前 graph 不需要 ColorOutput，直接返回。
- `CompositePage.update_color_output()` 如果不需要 ColorOutput，不提交重复评估。

新增测试：

```text
tests/composite/test_no_color_output_for_topos_color_graph.py
```

测试内容：

1. 默认图 `TimelineClipInput -> Output`：进入 Composite 不调用 `engine.request_color_output()`。
2. `TimelineClipInput -> Topos Color -> Output`：进入 Composite 不调用 `engine.request_color_output()`。
3. 旧图 `COLOR -> Output`：进入 Composite 会调用 `engine.request_color_output()`。

#### 3.2.2 readback 不能阻塞 UI 线程

短期修法：

- 修改 `CompositePage.request_output_readback()`：只允许导出/截图/手动调试使用。
- 主预览改为异步：

```text
CompositeRenderer.frame_ready
  -> UI 收到 tex_id
  -> 如果 direct texture sharing 有效：set_texture()
  -> 如果无效：提交 ASYNC_READBACK_PRESENT，不阻塞 UI
  -> renderer readback 完成后 signal: preview_image_ready(np.ndarray, width, height, request_id)
  -> viewer.set_image_data()
```

新增信号：

```python
CompositeRenderer.preview_image_ready = pyqtSignal(int, object, int, int)
CompositePage.composite_preview_image_ready = pyqtSignal(int, object, int, int)
```

新增命令：

```python
CompositeCommand.READBACK_PRESENT = "readback_present"
```

注意：

- `READBACK_PRESENT` 不能使用 `threading.Event.wait()`。
- 只保留最新 request_id，旧 readback 完成后丢弃。
- readback 频率限流：静止切页一次，播放时最大 15fps 或 24fps，后续通过 PBO 提升。

#### 3.2.3 对同一帧的 master fallback 做缓存

在 `TimelineClipFrameProvider` 增加最近帧缓存：

```python
_source_texture_cache: dict[tuple[str, int, str], _TexResult]
```

key：

```text
(asset_path, source_frame, input_stage)
```

要求：

- 同一帧重复进 Composite 不重复 `service.request_frame()`。
- 播放或 playhead 变化时只保留最近 2-3 帧。
- clip 切换、project 切换、provider cleanup 时清空。

新增测试：

```text
test_source_frame_texture_cache_reuses_same_frame
test_source_frame_texture_cache_invalidates_on_frame_change
```

### 3.3 验证方法

1. 手动操作：

| 操作 | 验收 |
|---|---|
| Color 调节任意参数，切 Composite | UI 不冻结，1 秒内可交互 |
| Composite 切 Color，再切 Composite 10 次 | 不出现卡死，不出现重复长时间等待 |
| 未播放状态停在同一帧反复切页 | 日志中不重复 master decode 同一帧超过 1 次 |

2. 日志验收：

新增一次性 debug 日志：

```text
[COMPOSITE-PRESENT] direct_texture_sharing=false, mode=async_readback
[COMPOSITE-PRESENT] drop stale readback request_id=...
[TIMELINE-CLIP-FRAME] cache hit asset=... frame=...
```

3. 性能指标：

| 指标 | 目标 |
|---|---:|
| Color -> Composite 首帧显示 | < 500ms |
| Composite -> Color 切回 | < 300ms |
| 10 次来回切页 | 无卡死、无 5 秒级阻塞 |
| UI 线程单次阻塞 | < 16ms，最大不超过 50ms |

---

## 4. 阶段三：修好 Qt / ModernGL context sharing

### 4.1 当前风险

`CompositeRenderer` 注释声称 texture id 可在 shared contexts 中有效，但实际出现过绿色单色画面，说明至少存在以下问题之一：

1. `CompositeRenderer.shared_context` 并未和 `CompositeViewer` 的 `QOpenGLWidget.context()` 共享。
2. `CompositeViewer.set_texture()` 只查 texture size，可能误判同编号但不同 context 的纹理为有效。
3. render thread 输出 FBO texture 生命周期与 viewer draw 时序不同步。
4. 缺少 GPU fence / sync，viewer 读取时 renderer 尚未完成写入。

### 4.2 实施步骤

1. 在 app 启动最早阶段设置共享上下文属性。

检查 `src/main.py` 或 app 初始化处，确保在 `QApplication` 创建前：

```python
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
```

2. 创建一个统一 GL context hub。

新增：

```text
src/shared/gpu/gl_context_hub.py
```

职责：

- 记录主 UI GL context。
- 创建 Composite render context。
- 创建 Edit timeline render context。
- 创建 Color async render context。
- 提供 `are_sharing(ctx_a, ctx_b)` 诊断函数。

3. 加强 `CompositeViewer.set_texture()` 的有效性验证。

不能只验证 width/height。增加一个 debug probe：

```text
renderer 写入 2x2 测试纹理：
red green
blue white

viewer context 直接采样/读回该纹理，验证像素是否匹配。
```

如果 probe 失败：

- 禁用 direct texture path。
- 自动切到 async readback path。
- 不再每帧重复尝试 direct texture。

4. 输出 texture 生命周期管理。

`CompositeRenderer._render_to_output()` 当前双缓冲 `tex_pair` 是对的，但要补：

- frame_ready emit 前插入 `glFenceSync` 或 ModernGL fence。
- viewer 确认绘制完成后，renderer 才允许复用该 texture，或至少三缓冲。

建议短期改为三缓冲：

```python
self.tex_ring = [tex0, tex1, tex2]
self.write_idx = (write_idx + 1) % 3
```

5. 建立 context sharing 回归测试/诊断工具。

新增脚本：

```text
tools/diagnose_gl_context_sharing.py
```

输出：

```text
main_ctx format
viewer_ctx format
composite_ctx format
AA_ShareOpenGLContexts enabled
share probe: pass/fail
direct texture presentation: enabled/disabled
```

### 4.3 验证方法

1. 如果 context sharing 通过：

| 场景 | 验收 |
|---|---|
| Composite 主预览 | 不走 readback，直接显示 GPU texture |
| 切页 | 不冻结 |
| 节点缩略图和主视图 | 同方向、同颜色 |
| GPU texture probe | 2x2 像素完全匹配 |

2. 如果 context sharing 不通过：

| 场景 | 验收 |
|---|---|
| 自动 fallback | 使用 async readback，不阻塞 UI |
| 日志 | 只打印一次 sharing failed，不刷屏 |
| 画面 | 正常显示，不绿、不红条、不翻转 |

---

## 5. 阶段四：异步 PBO readback 方案

### 5.1 目标

在无法可靠共享 GL texture 的机器上，用 PBO 将同步 readback 改成异步，避免 UI 卡死。

### 5.2 实施文件

| 文件 | 任务 |
|---|---|
| `src/features/composite/render/pbo_readback.py`（新建） | 管理 PBO ring buffer |
| `src/features/composite/render/composite_renderer.py` | 增加 `READBACK_PRESENT_ASYNC` 命令 |
| `src/features/composite/ui/composite_page.py` | 转发异步 preview image signal |
| `src/features/composite/adapters/main_window_adapter.py` | viewer 收到异步图像后上传 |
| `src/features/composite/ui/composite_viewport.py` | 支持 async image request id 去重 |

### 5.3 PBO 设计

使用 3 个 PBO：

```text
Frame N:
  bind PBO[i]
  glReadPixels(..., offset=0)  # DMA starts

Frame N+1:
  map PBO[i-1] if ready
  convert to numpy
  emit preview_image_ready

Frame N+2:
  recycle PBO
```

伪代码：

```python
class AsyncPBOReadback:
    def enqueue(fbo, width, height, request_id): ...
    def poll_ready() -> list[ReadbackResult]: ...
```

注意：

- macOS OpenGL 4.1 支持 PBO，但不支持 compute shader 4.3。
- PBO readback 仍是 GPU->CPU 拷贝，不是全 GPU；它只是 UI fallback。
- 主线目标仍是修好 context sharing 后不走 readback。

### 5.4 验证方法

| 测试 | 验收 |
|---|---|
| 4K 图像 readback | UI 不阻塞 |
| 连续切页 20 次 | 无卡死 |
| 快速调色滑块后切 Composite | 只显示最新 request_id，旧图丢弃 |
| 性能日志 | readback enqueue < 2ms，map 不在 UI 线程等待 |

---

## 6. 阶段五：三页全 GPU 解码渲染链路补齐

### 6.1 当前缺口清单

| 缺口 | 当前表现 | 目标 |
|---|---|---|
| Composite TimelineClipInput 不走 zero-copy | proxy None 后 master CPU DecodedFrame，再上传 GPU | 直接拿 GPU resident frame 或 IOSurface texture |
| proxy/master fallback 策略不统一 | Composite 自己 retry master | 统一由 `VideoFrameProviderService` 提供 fallback policy |
| packed rgb48le 上传是 CPU->GPU copy | 高位深 packed frame 归一化后上传 | 优先 YUV/IOSurface GPU shader 转换 |
| Composite 主预览 readback | 当前 readback-first | direct shared texture 或 PBO fallback |
| ColorOutput 旧链路干扰 | 新 Topos Color 不应依赖 ColorOutput | 旧 COLOR 节点迁移或隔离 |
| render cache 未覆盖 source frame texture | 同一帧可能重复解码 | frame texture cache + render scheduler dirty policy |
| 方向 origin 无统一 metadata | 不同路径上下翻转 | `TextureResult.origin` contract |

### 6.2 统一帧提供接口

新增或改造接口：

```python
class TimelineFrameGPUHandle:
    texture_id: int
    width: int
    height: int
    color_space: str
    bit_depth: int
    origin: str
    zero_copy: bool
    owner: str
    fence: object | None
```

`TimelineClipFrameProvider.get_timeline_clip_texture()` 应优先返回：

1. `GPUHandle` from zero-copy decoder。
2. `GPUHandle` from shared GPU upload cache。
3. CPU decoded frame upload fallback。
4. debug placeholder。

### 6.3 macOS zero-copy 接入

已有基础模块：

```text
src/shared/media/macos_vt/source.py
src/shared/media/macos_vt/capability.py
src/shared/media/macos_vt/vt_zero_copy_probe.py
src/shared/media/frame_path.py
```

执行步骤：

1. 在 `VideoFrameProviderService.open_clip()` 里记录 decode path：

```python
DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY
DecodePath.CPU_PACKED
DecodePath.CPU_PLANAR
```

2. 对 zero-copy source 暴露 `request_gpu_frame()`：

```python
gpu_frame = service.request_gpu_frame(clip_id, frame_index, quality="proxy")
```

3. `TimelineClipFrameProvider` 优先：

```python
if hasattr(service, "request_gpu_frame"):
    gpu_frame = service.request_gpu_frame(...)
    if gpu_frame and gpu_frame.texture_id:
        return _TexResult(texture_id=gpu_frame.texture_id, ...)
```

4. 对 CPU fallback 保留现有 `_upload_decoded_frame()`。

5. 在 HUD / 日志显示：

```text
Composite input path: zero-copy / gpu-upload / cpu-upload / placeholder
```

### 6.4 Color 页与 Topos Color 节点统一

目标：

- Color 页面调色参数保存到 clip-local `ColorStackData`。
- Composite 的 `Topos Color` 节点直接读取同一个 `ColorStackData`。
- 不通过 ColorOutput texture 传调色结果。

执行步骤：

1. 确认 Color 调参后 clip.color_stack_id 更新。
2. `CompositePage.ensure_topos_color_node(color_stack_id)` 必须更新已有 Topos Color 节点参数。
3. `ColorStackRenderService.resolve_grading_params()` 必须能通过 runtime_context 找到当前 clip stack。
4. 删除或隔离新图中自动创建 `color_input` 的路径。
5. 添加测试：

```text
Color 页面修改 exposure
-> clip ColorStackData 更新
-> Composite Topos Color 节点读取同一个 stack
-> 不请求 ColorOutput
-> 输出亮度改变
```

### 6.5 Edit 预览与 Composite 输入共享缓存

目标：

Edit program viewer 和 Composite TimelineClipInput 请求同一 clip 同一 source_frame 时，共享 decode/upload 结果。

执行步骤：

1. 在 `VideoFrameProviderService` 增加 GPU frame cache。
2. key：

```text
(asset_path, source_frame, quality, decode_path, color_space, bit_depth)
```

3. Edit `TimelinePreviewRenderer` 和 Composite `TimelineClipFrameProvider` 都通过该服务拿 GPU frame。
4. 引入引用计数或 fence，防止 texture 被提前释放。
5. 播放时缓存窗口：

```text
current frame +/- 3 frames
```

### 6.6 验证方法

| 验证项 | 方法 | 目标 |
|---|---|---|
| 是否 zero-copy | 日志打印 `Composite input path=zero-copy` | 支持机型上为 True |
| 是否 CPU upload fallback | 关闭 VT 或不支持格式时打印 fallback | 有明确 fallback reason |
| 同帧重复解码 | 统计 `request_frame` 次数 | 切页不重复解码同一帧 |
| GPU memory | 监控 texture cache | 无持续增长 |
| 方向 | 方向测试视频 | 所有页面方向一致 |
| 颜色 | Color exposure + Composite Topos Color | 结果一致 |
| 性能 | 4K clip 切页 | < 500ms 首帧 |

---

## 7. 阶段六：测试矩阵

### 7.1 手动测试矩阵

| 编号 | 素材 | 操作 | 预期 |
|---|---|---|---|
| M1 | 方向测试 mp4/h264 8-bit | Edit -> Composite | 不翻转 |
| M2 | ProRes / rgb48le 或高位深 mov | Edit -> Composite | 不红条、不绿、不翻转 |
| M3 | 同 M2 | Color 调 exposure -> Composite | 不翻转，颜色正确 |
| M4 | 同 M2 | Composite -> Color -> Composite 20 次 | 不卡死 |
| M5 | 4K 60fps h264 | 播放中切 Composite | UI 不冻结，允许降帧 |
| M6 | 有 Topos Color 节点 | 删除 Topos Color | 回到 source_raw，画面仍显示 |
| M7 | 旧 COLOR 节点图 | 进入 Composite | 仍请求 ColorOutput，兼容旧图 |
| M8 | macOS OpenGL 4.1 | 添加大半径 Blur | fragment fallback 可用，不卡死 |

### 7.2 自动测试清单

新增或更新：

```text
tests/edit/test_timeline_clip_frame_provider.py
tests/composite/test_timeline_clip_input_render_contract.py
tests/composite/test_default_graph_topos_color_contract.py
tests/composite/test_no_color_output_for_topos_color_graph.py
tests/composite/test_viewer_image_upload_origin.py
tests/composite/test_async_present_readback.py
tests/media/test_video_frame_provider_gpu_handle.py
tests/media/test_zero_copy_fallback_contract.py
```

### 7.3 必跑命令

```bash
.venv/bin/python -m py_compile \
  src/features/edit/core/timeline_clip_frame_provider.py \
  src/features/composite/ui/composite_page.py \
  src/features/composite/adapters/main_window_adapter.py \
  src/features/composite/render/composite_renderer.py \
  src/features/composite/ui/composite_viewport.py \
  src/app/controllers/page_controller.py \
  src/app/controllers/composite_controller.py
```

```bash
.venv/bin/python -m pytest \
  tests/composite/test_render_scheduler.py \
  tests/edit/test_timeline_clip_frame_provider.py \
  tests/edit/test_comp_clip_promotion_lifecycle.py \
  tests/composite/test_topos_color_evaluator_contract.py \
  tests/composite/test_default_graph_topos_color_contract.py \
  tests/composite/test_topos_color_node_contract.py \
  tests/composite/test_eval_context.py \
  tests/composite/test_timeline_clip_input_render_contract.py \
  tests/edit/test_composite_graph_data.py \
  -q
```

---

## 8. 给执行 AI 的优先级

### P0：先止血

1. 建立统一 origin contract。
2. 修复 `set_image_data()` 的 CPU top-left -> GL bottom-left 上传。
3. 去掉路径依赖的翻转补丁。
4. 禁止新 Topos Color 链路请求 ColorOutput。
5. 将主预览同步 readback 改成异步 readback，避免 UI 卡死。

### P1：稳定直显

1. 增加 GL context sharing probe。
2. direct texture sharing 通过才用 `set_texture()`。
3. direct texture sharing 失败自动 async PBO fallback。
4. 输出 texture 三缓冲或 fence 同步。

### P2：补全全 GPU

1. `VideoFrameProviderService.request_gpu_frame()`。
2. macOS VT / IOSurface zero-copy 接入 Composite。
3. Edit/Composite 共享 GPU frame cache。
4. YUV planar GPU shader conversion 成为主路径，CPU packed 只是 fallback。

### P3：清理旧架构

1. 旧 `COLOR/color_input` 只做迁移兼容。
2. 新 `.topos` 工程保存不再依赖 ColorOutput。
3. 文档中明确 Edit / Color / Composite 的唯一数据流。

---

## 9. 完成标准

功能完成必须同时满足：

1. Edit 直接进 Composite 不翻转。
2. Color 调色后进 Composite 不翻转。
3. Color / Composite 来回切换 20 次不卡死。
4. 新链路不请求 ColorOutput。
5. 主预览不使用 UI 线程同步 readback。
6. 支持 direct texture sharing 的机器上主预览全 GPU 显示。
7. 不支持 direct texture sharing 的机器上自动 async PBO fallback。
8. Composite TimelineClipInput 至少能报告当前路径：zero-copy / gpu-upload / cpu-upload / placeholder。
9. 有方向、性能、fallback、旧 COLOR 兼容的自动测试。

---

## 10. 不同素材帧率与时间线帧率不匹配的播放抖动优化

### 10.1 当前问题判断

当前 Edit 时间线已经在部分地方考虑了素材帧率和时间线帧率：

- 插入素材时，`src/features/edit/ui/timeline_view.py` 中 `_get_asset_duration_frames()` 使用 `asset.duration_seconds * timeline_fps` 计算时间线长度。
- `_get_asset_source_duration_frames()` 使用素材 fps 计算 source duration。
- `ClipInstance.timeline_to_source_frame()` 当前主要按 `relative * speed` 映射。

但播放抖动说明渲染播放路径还缺少统一的“时间驱动帧映射”：

```text
timeline_frame -> timeline_time_seconds -> clip_local_time_seconds -> source_time_seconds -> source_frame/PTS
```

如果只用整数帧乘法或 `int(relative * speed)`，当素材 fps 与时间线 fps 不整除时会产生不均匀重复/跳帧，例如：

| 素材 fps | 时间线 fps | 错误现象 |
|---:|---:|---|
| 23.976 | 25 | 重复帧间隔不均，慢速抖动 |
| 24 | 30 | 每 4 帧附近重复一次，但若用 int 截断会节奏偏斜 |
| 25 | 24 | 间歇性跳帧 |
| 29.97 | 24 | motion cadence 不稳定 |
| VFR 手机视频 | 任意 CFR timeline | PTS 不连续导致画面抖动/音画漂移 |

### 10.2 目标原则

1. 时间线播放必须以时间线 clock 为主，而不是以素材帧号递增为主。
2. 所有页面共用同一个帧映射函数：Edit Program Viewer、Color clip strip、Composite TimelineClipInput、Deliver export。
3. 使用有理数 fps 或整数 tick，避免 float 累积误差。
4. CFR 素材使用可预测 cadence；VFR 素材优先按 PTS 查找。
5. 播放预览可以丢帧，但不能错误地重复上一帧造成抖动。
6. 导出必须 deterministic，同一 timeline frame 永远映射到同一 source frame。

### 10.3 新增统一模块

新增文件：

```text
src/features/edit/core/timeline_frame_mapper.py
```

职责：

- 统一 timeline frame 到 source frame / source PTS 的映射。
- 支持 CFR / VFR。
- 支持 speed、reverse、trim、source_start。
- 输出 cadence 诊断信息。

核心数据结构：

```python
@dataclass(frozen=True)
class FrameMapRequest:
    timeline_frame: int
    timeline_fps_num: int
    timeline_fps_den: int
    clip_timeline_start: int
    clip_timeline_duration: int
    source_start_frame: int
    source_duration_frames: int
    source_fps_num: int | None
    source_fps_den: int | None
    speed: Fraction
    reverse: bool = False
    source_pts_index: tuple[int, ...] | None = None
    source_time_index: tuple[float, ...] | None = None

@dataclass(frozen=True)
class FrameMapResult:
    source_frame: int
    source_time_seconds: float
    timeline_time_seconds: float
    cadence_action: str  # exact, repeat, skip, interpolate
    fractional_source_frame: Fraction
```

### 10.4 CFR 映射算法

不要使用：

```python
source_frame = source_start + int(relative_timeline_frame * speed)
```

应使用有理数时间映射：

```python
timeline_local_frame = timeline_frame - clip.timeline_start
timeline_local_time = Fraction(timeline_local_frame * timeline_fps_den, timeline_fps_num)
source_local_time = timeline_local_time * speed
source_fractional_frame = source_local_time * Fraction(source_fps_num, source_fps_den)
source_frame = source_start + round_half_even_or_nearest(source_fractional_frame)
```

推荐默认策略：

```text
预览播放：nearest frame + cadence cache
逐帧/暂停：nearest exact
导出：nearest deterministic
慢动作高质量：frame blending 或 optical flow（后续）
```

Cadence 例子：

```text
24fps source -> 30fps timeline:
source frames: 0,1,2,2,3,4,5,6,6,7...
```

重复帧应该按分数时间均匀分布，不能连续抖动。

### 10.5 VFR 映射算法

VFR 素材必须建立 PTS/time index：

```python
source_time_index = [frame0.time_seconds, frame1.time_seconds, ...]
```

映射步骤：

```python
target_source_time = source_start_time + clip_local_time * speed
source_frame = bisect_nearest(source_time_index, target_source_time)
```

要求：

- `MediaSource` probe 阶段保存 VFR 标记。
- 如果 `avg_frame_rate != r_frame_rate` 或 PTS delta 方差超过阈值，标记为 VFR。
- VFR 不允许用 `frame_index / fps` 猜时间。

### 10.6 播放调度算法

`EditPlaybackController` 不应该简单靠 timer 每次 +1 frame。推荐：

```python
elapsed = monotonic_now - playback_start_time
ideal_timeline_frame_float = start_frame + elapsed * timeline_fps * playback_speed
target_timeline_frame = floor_or_round(ideal_timeline_frame_float)
```

然后：

```text
target_timeline_frame -> FrameMapper -> source_frame/PTS -> request_frame
```

播放队列策略：

| 情况 | 策略 |
|---|---|
| 解码赶得上 | 显示所有 timeline frame |
| 解码落后 | 丢弃旧请求，只保留最新 timeline frame |
| source frame 与上一帧相同 | 复用上一 GPU texture，不重新解码 |
| 需要跳过 source frame | 直接请求目标 source frame，不顺序等待 |

### 10.7 音频同步

视频播放抖动经常来自视频按帧推进、音频按时间推进不一致。

短期：

- 以音频 clock 为主。
- 视频每 tick 根据音频播放时间反推 timeline frame。

伪代码：

```python
if audio_active:
    timeline_time = audio_playback_position_seconds
else:
    timeline_time = monotonic_elapsed
timeline_frame = round(timeline_time * timeline_fps)
```

长期：

- 音频 resampler 负责素材音频 fps/timebase 转换。
- 视频 frame mapper 和音频 sample mapper 使用同一个 timeline time。

### 10.8 需要修改的文件

| 文件 | 修改 |
|---|---|
| `src/features/edit/core/timeline_frame_mapper.py` | 新增统一帧映射模块 |
| `src/features/edit/core/timeline_v2.py` | `ClipInstance.timeline_to_source_frame()` 改为调用 mapper 或保留包装 |
| `src/features/edit/core/timeline_render_plan.py` | 统一使用 mapper 输出 source_frame |
| `src/features/edit/core/timeline_preview_renderer.py` | 播放请求按 timeline time 映射，不再局部猜 source frame |
| `src/features/edit/core/edit_playback_controller.py` | 使用 monotonic/audio clock 计算目标 timeline frame |
| `src/features/edit/core/timeline_clip_frame_provider.py` | Composite 输入也调用同一 mapper |
| `src/features/edit/ui/timeline_view.py` | 插入素材时保存 source fps/timebase 元数据 |
| `src/shared/media/metadata.py` | 保存 avg/r/native fps、time_base、is_vfr、pts_index 摘要 |

### 10.9 自动测试

新增：

```text
tests/edit/test_timeline_frame_mapper.py
tests/edit/test_mixed_fps_playback_cadence.py
tests/media/test_vfr_timeline_mapping.py
```

测试案例：

| 测试 | 预期 |
|---|---|
| 24 -> 30 | 30 个 timeline frame 映射约 24 个 source frame，重复均匀 |
| 30 -> 24 | 24 个 timeline frame 映射约 30 个 source frame，跳帧均匀 |
| 23.976 -> 25 | 1000 帧内无累积漂移超过 1 source frame |
| 25 -> 24 | cadence 稳定，不出现连续异常跳动 |
| speed=0.5 | source time 以半速推进 |
| speed=2.0 | source time 以两倍推进 |
| reverse | source frame 单调递减 |
| VFR PTS | 按最近 PTS 选帧，不按平均 fps 猜 |

### 10.10 手动验收

1. 准备 24、25、29.97、30、60 fps 视频各一段，内容为匀速横向移动线条。
2. 分别放入 24、25、30 fps 时间线。
3. 播放时观察：

| 验收项 | 标准 |
|---|---|
| 横向移动 | 无不规则抖动 |
| 播放头 | 匀速推进 |
| 音画同步 | 30 秒内无明显漂移 |
| frame log | cadence repeat/skip 分布均匀 |
| Composite 同一 clip | 与 Edit 映射同一 source_frame |

---

## 11. 时间线片段预览图加载速度与性能优化

### 11.1 当前问题判断

当前缩略图系统已经有基础缓存：

- `src/shared/media/thumbnail_provider.py` 有 `ThumbnailProvider`、`LRUCache`、`MemoryAwareLRUCache`、`request_frame_pixmap_async()`。
- `src/features/edit/ui/timeline_items.py` 有 `ClipThumbnailRequest / ClipThumbnailSet / ClipThumbnailCache` 相关路径。
- `ClipItem` 支持 `none / start_end / filmstrip` 显示模式。

但加载慢的常见原因：

1. 每个 clip 单独触发缩略图请求，缺少 viewport 级批调度。
2. filmstrip 模式可能请求过多帧。
3. 滚动/缩放时重复取消不及时，旧请求占满线程池。
4. 缩略图解码没有优先使用已有 proxy / GOP cache / VideoFrameProviderService。
5. 缓存 key 没有充分包含 timeline zoom、target height、source range，导致复用不足或重复生成。
6. QPixmap 只能主线程创建，后台任务如果返回太多大图会堵主线程。
7. 播放期间缩略图异步解码可能和播放解码抢资源。

### 11.2 优化目标

| 指标 | 目标 |
|---|---:|
| 打开包含 100 个 clip 的时间线 | 1 秒内显示占位，3 秒内显示可见区首批缩略图 |
| 滚动时间线 | UI 不掉帧，缩略图渐进加载 |
| 播放中 | 不启动新缩略图解码，只读缓存 |
| filmstrip 模式 | 可见区优先，最多按宽度预算请求 |
| 重复打开项目 | 命中磁盘缩略图缓存，首屏 < 500ms |

### 11.3 新增统一调度器

新增：

```text
src/features/edit/core/timeline_thumbnail_scheduler.py
```

职责：

- 以 viewport 为单位收集可见 clip。
- 计算每个 clip 当前缩放级别需要的缩略图数量。
- 合并重复请求。
- 按优先级提交给 `ThumbnailProvider`。
- 播放中暂停新请求。
- 滚动/缩放后取消不可见请求。

请求结构：

```python
@dataclass(frozen=True)
class TimelineThumbnailJob:
    asset_id: str
    file_path: str
    clip_id: str
    source_start: int
    source_duration: int
    timeline_start: int
    timeline_duration: int
    visible_px_start: int
    visible_px_end: int
    target_height: int
    mode: str  # single, start_end, filmstrip
    priority: int
    generation: int
```

### 11.4 缩略图采样算法

根据显示宽度决定采样数量：

```python
thumb_w = max(48, target_height * aspect_ratio)
max_count = floor(visible_clip_width / thumb_w)
count = clamp(max_count, 1, THUMBNAIL_MAX_PER_VISIBLE_CLIP)
```

模式策略：

| 模式 | 算法 |
|---|---|
| single | 只取 source_start |
| start_end | 取 source_start 和 source_end - 1 |
| filmstrip | 在可见 source range 内均匀取 count 个时间点 |
| zoomed out | 自动降级 single 或 start_end |
| 正在播放 | 只显示已有缓存，不发新请求 |

关键：filmstrip 采样必须按“可见区域”而不是整段 clip 全量生成。

```python
visible_timeline_range -> clip local range -> source frame range -> sample frames
```

这可以避免长 clip 在很小可见区域内生成大量无用缩略图。

### 11.5 三层缓存设计

#### L1：ClipItem 内存引用

当前 `ClipItem._cached_thumbnail_set` 保留，但只能保存当前 request_key。

#### L2：进程内 LRU

放在 `ThumbnailProvider` 或新 scheduler：

```text
key = (
  asset_id,
  file_mtime_ns,
  source_frame,
  target_height,
  color_management_version,
)
```

要求：

- `QImage` 和 `QPixmap` 分开缓存。
- worker 线程只产出 `QImage`。
- 主线程按需转换 `QPixmap`。

#### L3：磁盘缓存

新增：

```text
.topos_cache/thumbnails/{asset_hash}/{height}/{frame}.webp
```

或项目内：

```text
project_payload_cache/thumbnails/
```

磁盘 key：

```text
sha1(file_path + mtime_ns + file_size + frame + height + decoder_version)
```

打开项目先加载磁盘缓存，后台再校验 mtime。

### 11.6 解码加速策略

1. 优先使用 proxy：

```python
VideoFrameProviderService.request_frame(quality="proxy")
```

2. 如果已有 GPU/proxy cache，缩略图不要重新打开文件。
3. 同一 asset 的多个 frame 请求要批处理：

```python
request_frames_batch(asset_id, sorted_frame_indices, quality="proxy")
```

4. 对 GOP 视频按升序解码，避免每个 thumbnail 都独立 seek。
5. 对 start/end 缩略图优先生成：

```text
first visible frame -> last visible frame -> middle frames
```

6. 对超长 clip：

- 先生成低清 32px 高度。
- UI 立即显示。
- 空闲时替换成 64/96px。

### 11.7 调度优先级

优先级从高到低：

1. 当前 viewport 中可见 clip 的 single/start_end。
2. 当前 playhead 附近 clip。
3. 用户鼠标悬停或选中的 clip。
4. 可见 clip 的 filmstrip 中间帧。
5. viewport 前后预取一屏。
6. 不可见 clip 禁止生成。

播放中：

```text
if playback_active:
    scheduler.pause_new_decode_jobs()
    UI only reads cached thumbnails
```

### 11.8 UI 绘制优化

`ClipItem.paint()` 中不能做重活。

要求：

- paint 只做：
  - 查 L1/L2 缓存。
  - 绘制 pixmap 或 lightweight placeholder。
  - 如果需要请求，发一个轻量 signal 给 scheduler，不能直接启动大量 QRunnable。
- placeholder 使用纯 QPainter 渐变/色块，不触发解码。
- zoom/scroll 时使用 debounce：

```text
滚动停止 80ms 后提交新缩略图批次
缩放停止 120ms 后重新计算 filmstrip
```

### 11.9 需要修改的文件

| 文件 | 修改 |
|---|---|
| `src/features/edit/core/timeline_thumbnail_scheduler.py` | 新增 viewport 级调度器 |
| `src/features/edit/ui/timeline_view.py` | 监听 visible rect/zoom/playback state，向 scheduler 提交可见 clip |
| `src/features/edit/ui/timeline_items.py` | `ClipItem.paint()` 改为只读缓存 + 轻量 request signal |
| `src/shared/media/thumbnail_provider.py` | 增加 batch request、disk cache、proxy 优先 |
| `src/shared/video/frame_provider_service.py` | 增加 `request_frames_batch()` 或 thumbnails 专用 batch API |
| `src/features/edit/preferences.py` | 增加缩略图质量、缓存大小、播放中暂停加载选项 |

### 11.10 自动测试

新增：

```text
tests/edit/test_timeline_thumbnail_scheduler.py
tests/media/test_thumbnail_batch_decode.py
tests/edit/test_timeline_clip_paint_no_decode.py
```

测试项：

| 测试 | 预期 |
|---|---|
| viewport 只调度可见 clip | 不可见 clip 没有 job |
| zoomed out 自动 single | 不生成大量 filmstrip 帧 |
| playback_active | 不提交新 decode job |
| 重复 clip 同 asset | 合并 frame 请求 |
| batch frames | 同 asset 多帧只打开一次 source |
| mtime changed | 磁盘缓存失效 |
| paint path | 不调用 `ThumbnailProvider.get_thumbnail()` 触发解码 |

### 11.11 手动验收

1. 创建 100 个视频 clip，打开时间线。
2. 开启 filmstrip。
3. 快速横向滚动和缩放。

验收：

| 场景 | 目标 |
|---|---|
| 首屏 | 立即显示占位，逐步替换缩略图 |
| 快速滚动 | UI 不明显卡顿，旧请求被丢弃 |
| 停止滚动 | 1 秒内可见区缩略图加载完成 |
| 播放中 | 不新增缩略图解码，不影响播放 |
| 再次打开项目 | 命中磁盘缓存，首屏快速显示 |

### 11.12 与全 GPU 计划的关系

缩略图不必追求全 GPU 主链路，但不能破坏播放主链路：

- 播放/Composite/Color 优先级高于 thumbnail。
- 缩略图可以用 proxy CPU decode，但必须低优先级、可取消、批处理。
- 如果未来 `VideoFrameProviderService.request_gpu_frame()` 完成，缩略图可以从 GPU frame 异步 downsample/readback，但这只能作为优化，不应阻塞主播放。

