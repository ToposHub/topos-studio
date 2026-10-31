# GPU 管线稳定化 — 剩余优化实施计划

日期：2026-06-02
基于：`edit_color_composite_gpu_pipeline_stabilization_plan_2026-06-01.md` 进度审计
状态：标注 ✅ 已完成 / 🔧 部分完成 / ❌ 未开始

---

## 当前完成度总览

| 阶段 | 标题 | 完成度 | 主要缺口 |
|---|---|---:|---|
| 阶段一 (P0) | 方向一致性修复 | ~75% | 缺自动测试 |
| 阶段二 (P0) | 解除 Color/Composite 切页卡死 | ~70% | 缺自动测试、readback 仍非真正异步 |
| 阶段三 (P1) | GL Context Sharing 稳定 | ~50% | 缺 context hub / fence / 诊断工具 |
| 阶段四 (P1) | 异步 PBO Readback | ~60% | PBO 实际仍同步，未集成到 renderer 主流 |
| 阶段五 (P2) | 全 GPU 解码渲染链路 | ~25% | zero-copy 未接入 Composite |
| Section 10 | 混合帧率播放 | ~40% | mapper 模块存在但未集成 |
| Section 11 | 缩略图调度 | ~35% | scheduler 存在但未集成到 UI |

---

## Task 1：补齐已实现功能的自动测试（P0）

**目标**：方向一致性、切页卡死等已改好的核心功能缺少自动测试保护，一旦后续修改容易回归。

### Task 1.1：方向 origin 合约测试

**新建文件**：`tests/edit/test_timeline_clip_frame_provider.py` 追加测试

**测试用例**：

```python
def test_tex_result_default_origin_is_gl_bottom_left():
    """_TexResult 默认 origin 应为 gl_bottom_left"""

def test_upload_cpu_top_left_to_gl_bottom_left_contract():
    """
    构造 2x2 CPU top-left 数组:
      red(1,0,0)   green(0,1,0)
      blue(0,0,1)  white(1,1,1)
    调用 _prepare_packed_frame_upload() 后验证：
      - 返回数组第 0 行 = blue,white (GL bottom-left)
      - 返回数组第 1 行 = red,green
      - frame_metadata["origin"] == "gl_bottom_left"
    """

def test_upload_uint16_packed_frame_origin():
    """rgb48le uint16 帧上传后 origin 仍为 gl_bottom_left"""

def test_upload_float32_frame_origin():
    """float32 帧上传后 origin 仍为 gl_bottom_left"""

def test_source_texture_cache_hit_returns_same_result():
    """同一 (asset_path, source_frame) 二次请求命中缓存"""

def test_source_texture_cache_evicts_oldest():
    """缓存超过 _SOURCE_CACHE_MAX 时淘汰最早条目"""
```

**新建文件**：`tests/composite/test_viewer_image_upload_origin.py`

**测试用例**：

```python
def test_set_image_data_flips_bgr_top_left_to_gl_bottom_left():
    """
    构造 2x2 CPU top-left BGR:
      (0,0,1)   (0,1,0)    # row0: red, green in BGR
      (1,0,0)   (1,1,1)    # row1: blue, white in BGR
    调用 set_image_data() 后通过 glReadPixels 验证：
      - GL 纹理 (0,0) = blue (BGR 行1 翻转到 GL 行0)
      - GL 纹理 (0,1) = red  (BGR 行0 翻转到 GL 行1)
    需要 QOpenGLWidget test fixture。
    """

def test_set_image_data_float32_clipped_to_uint8():
    """float32 输入 0..1 范围正确映射到 0..255"""

def test_set_image_data_preserves_aspect_ratio():
    """非正方形图像宽高正确传递"""
```

### Task 1.2：ColorOutput 条件请求测试

**新建文件**：`tests/composite/test_no_color_output_for_topos_color_graph.py`

**测试用例**：

```python
def test_default_graph_does_not_require_color_output():
    """默认图 TimelineClipInput -> Output: requires_color_output_input() == False"""

def test_topos_color_graph_does_not_require_color_output():
    """TimelineClipInput -> ToposColor -> Output: requires_color_output_input() == False"""

def test_legacy_color_node_requires_color_output():
    """旧图 COLOR -> Output: requires_color_output_input() == True"""

def test_on_color_output_changed_skips_topos_color_graph():
    """ToposColor 图收到 on_color_output_changed 时不触发 update_color_output"""
    # Mock composite_page, 验证 update_color_output 未被调用

def test_on_enter_page_clears_color_texture_for_topos_color_graph():
    """ToposColor 图进入 Composite 时 color_texture_id 设为 None"""
```

### Task 1.3：异步 readback 信号链测试

**新建文件**：`tests/composite/test_async_present_readback.py`

**测试用例**：

```python
def test_readback_present_async_command_emits_signal():
    """READBACK_PRESENT_ASYNC 命令完成后 emit preview_image_ready"""

def test_stale_readback_discarded():
    """旧 request_id 的 readback 结果被 UI 丢弃（仅显示最新）"""

def test_readback_output_is_cpu_top_left_bgr():
    """readback 输出数组 row0 = 图像顶部（CPU top-left），BGR 色序"""

def test_readback_timeout_200ms_not_5s():
    """fallback readback timeout 为 0.2 秒而非 5 秒"""
```

### 验证方法

```bash
.venv/bin/python -m pytest \
  tests/edit/test_timeline_clip_frame_provider.py \
  tests/composite/test_viewer_image_upload_origin.py \
  tests/composite/test_no_color_output_for_topos_color_graph.py \
  tests/composite/test_async_present_readback.py \
  -v --tb=short
```

---

## Task 2：GL Context Sharing 加固（P1）

**目标**：当前 probe 机制过于简单（只检查 texture size），缺少 fence 同步和统一 context 管理。

### Task 2.1：创建 GL Context Hub

**新建文件**：`src/shared/gpu/gl_context_hub.py`

**职责**：

```python
class GLContextHub:
    """统一 GL context 管理中心。

    职责:
    1. 记录主 UI GL context (QOpenGLWidget context)
    2. 创建/注册各渲染 context (Composite, Edit preview, Color async)
    3. 提供 are_sharing(ctx_a, ctx_b) 诊断
    4. 提供 2x2 像素 probe 验证（比 size 检查更可靠）
    """

    def __init__(self):
        self._contexts: dict[str, Any] = {}  # name -> QOpenGLContext/ModernGL ctx
        self._sharing_matrix: dict[tuple[str, str], bool] = {}

    def register_context(self, name: str, ctx: Any) -> None: ...
    def are_sharing(self, name_a: str, name_b: str) -> bool: ...
    def probe_sharing(self, ctx_a, ctx_b) -> bool:
        """
        写入 2x2 测试纹理到 ctx_a，从 ctx_b 读取验证像素匹配。
        比 glGetTexLevelParameter 查 size 更可靠。
        """
    def get_direct_texture_enabled(self, renderer_name: str, viewer_name: str) -> bool:
        """返回是否应启用 direct texture sharing。"""
```

**修改文件**：`src/features/composite/ui/composite_viewport.py`

当前 `_probe_context_sharing()` 只检查 texture size，改为调用 `GLContextHub.probe_sharing()`：

```python
def _probe_context_sharing(self, renderer_tex_id: int) -> bool:
    # 改用 GLContextHub 的 2x2 probe
    hub = GLContextHub.instance()
    result = hub.probe_sharing("composite_renderer", "composite_viewer")
    if not result:
        logger.info("[COMPOSITE-CONTEXT-SHARING] 2x2 probe FAILED, disabling direct texture")
    return result
```

### Task 2.2：输出纹理 Fence 同步

**修改文件**：`src/features/composite/render/composite_renderer.py`

当前 `frame_ready` emit 前无 fence，viewer 可能在 renderer 写入完成前读取。

在 `_render_to_output()` emit `frame_ready` 前插入 fence：

```python
# 在 frame_ready.emit() 前
try:
    from OpenGL import GL as _gl
    fence = _gl.glFenceSync(_gl.GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
    _gl.glFlush()  # 确保 GPU 命令已提交
    self._pending_fences[request_id] = fence
except Exception:
    pass  # fence 不可用时降级

self.frame_ready.emit(request_id, tex.glo, w, h)
```

在 viewer 端 `set_texture()` 绘制前等待 fence：

```python
def _wait_for_fence(self, request_id: int):
    fence = self._pending_fences.pop(request_id, None)
    if fence:
        from OpenGL import GL as _gl
        _gl.glWaitSync(fence, 0, _gl.GL_TIMEOUT_IGNORED)
        _gl.glDeleteSync(fence)
```

**注意**：fence 跨 context 依赖 context sharing 是否正常工作。如果 sharing 不可用，fence 也无意义，此时应走 async readback 路径。

### Task 2.3：Context Sharing 诊断工具

**新建文件**：`tools/diagnose_gl_context_sharing.py`

```python
#!/usr/bin/env python3
"""GL Context Sharing 诊断工具。

输出:
- AA_ShareOpenGLContexts 是否启用
- 各 context format (version, profile, samples)
- 2x2 probe 结果 (pass/fail)
- direct texture presentation: enabled/disabled
- 建议的修复操作
"""
```

实现步骤：
1. 创建 QApplication + AA_ShareOpenGLContexts
2. 创建 3 个 QOpenGLWidget（模拟 viewer、composite renderer、edit preview）
3. 在每个 context 中创建 ModernGL context
4. 执行 2x2 probe
5. 打印结果和建议

### 验证方法

```bash
# 运行诊断工具
.venv/bin/python tools/diagnose_gl_context_sharing.py

# 期望输出:
# AA_ShareOpenGLContexts: enabled
# composite_renderer <-> composite_viewer: sharing=pass
# direct texture presentation: enabled

# 手动验证:
# 1. Composite 主预览画面正常（不绿、不翻转）
# 2. 节点缩略图和主视图方向/颜色一致
```

---

## Task 3：时间线帧映射器集成（P1）

**目标**：`timeline_frame_mapper.py` 模块已完整实现并有 362 行测试，但未被任何播放/渲染代码调用。需将其集成到实际管线中。

### Task 3.1：改造 `ClipInstance.timeline_to_source_frame()`

**修改文件**：`src/features/edit/core/timeline_v2.py`（第 242-261 行）

当前代码：
```python
def timeline_to_source_frame(self, timeline_frame: int) -> int:
    relative = timeline_frame - self.timeline_start
    if isinstance(self.speed, SpeedParams):
        start, _ = self.speed.timeline_to_source_frames(relative, self.source_duration)
        return self.source_start + start
    return self.source_start + int(relative * float(self.speed))
```

改为调用 mapper：
```python
def timeline_to_source_frame(self, timeline_frame: int) -> int:
    """将时间线帧转换为素材帧（委托给 timeline_frame_mapper）。"""
    if not (self.timeline_start <= timeline_frame < self.timeline_end):
        raise ValueError(f"Timeline frame {timeline_frame} outside clip range")

    # 如果 clip 有 source fps 信息，使用精确 mapper
    if self._source_fps_num and self._timeline_fps_num:
        from src.features.edit.core.timeline_frame_mapper import map_frame, FrameMapRequest
        req = FrameMapRequest(
            timeline_frame=timeline_frame,
            timeline_fps_num=self._timeline_fps_num,
            timeline_fps_den=self._timeline_fps_den or 1,
            clip_timeline_start=self.timeline_start,
            clip_timeline_duration=self.timeline_duration,
            source_start_frame=self.source_start,
            source_duration_frames=self.source_duration,
            source_fps_num=self._source_fps_num,
            source_fps_den=self._source_fps_den or 1,
            speed=Fraction(self.effective_speed()).limit_denominator(10000),
            reverse=self.effective_speed() < 0,
            source_pts_index=self._source_pts_index,
        )
        result = map_frame(req)
        return result.source_frame

    # 回退：旧公式（无 fps 信息时）
    relative = timeline_frame - self.timeline_start
    if isinstance(self.speed, SpeedParams):
        start, _ = self.speed.timeline_to_source_frames(relative, self.source_duration)
        return self.source_start + start
    return self.source_start + int(relative * float(self.speed))
```

**需要新增的 `ClipInstance` 字段**：

```python
# ClipInstance.__init__ 或 dataclass 中新增：
_source_fps_num: int | None = None
_source_fps_den: int | None = None
_timeline_fps_num: int | None = None
_timeline_fps_den: int | None = None
_source_pts_index: tuple[float, ...] | None = None  # VFR 素材 PTS
```

### Task 3.2：在素材插入时保存 fps 元数据

**修改文件**：`src/features/edit/ui/timeline_view.py`

在插入素材到时间线时，从 `VideoMetadata` 读取 fps 信息并存入 clip：

```python
# 在 _insert_asset_to_timeline() 或类似方法中：
metadata = asset.get_video_metadata()
if metadata:
    clip._source_fps_num = metadata.fps_num or int(metadata.frame_rate * 1001)
    clip._source_fps_den = metadata.fps_den or (1001 if metadata.frame_rate in (23.976, 29.97, 59.94) else 1)

# 从时间线获取 timeline fps
clip._timeline_fps_num = self._project.timeline_fps_num or 24
clip._timeline_fps_den = self._project.timeline_fps_den or 1
```

**修改文件**：`src/shared/media/metadata.py`

确认 `VideoMetadata` 已包含以下字段（如果没有则添加）：

```python
fps_num: int | None = None        # 有理数 fps 分子
fps_den: int | None = None        # 有理数 fps 分母
is_vfr: bool = False              # 是否变帧率
avg_frame_rate: float | None = None
r_frame_rate: float | None = None
time_base: str | None = None
```

### Task 3.3：播放控制器使用 timeline time

**修改文件**：`src/features/edit/core/edit_playback_controller.py`

当前 `tick()` 使用 `time.monotonic()` 计算帧号，但未使用 mapper 做 source 映射。

在 `tick()` 中，目标 timeline frame 计算后，通过 mapper 获取 source frame：

```python
def tick(self, now: float) -> int:
    """返回当前应显示的 timeline frame。"""
    if self._state != PlaybackState.PLAYING:
        return self._current_frame

    elapsed = now - self._playback_start_time
    ideal_frame = self._playback_start_frame + elapsed * self._fps * self._playback_speed
    target_frame = int(ideal_frame)

    # 边界检查
    target_frame = max(0, min(target_frame, self._timeline_duration - 1))
    self._current_frame = target_frame
    return target_frame
```

播放控制器已经使用 monotonic clock（✅），关键是确保调用方使用 mapper 做 timeline→source 映射。

**修改文件**：`src/features/edit/core/edit_frame_coordinator.py`

在帧协调器中，从 `tick()` 拿到 timeline frame 后，通过 clip 的 `timeline_to_source_frame()` 获取 source frame（此路径已在 Task 3.1 中改走 mapper）。

### Task 3.4：Composite 输入也使用 mapper

**修改文件**：`src/features/edit/core/timeline_clip_frame_provider.py`

当前第 175 行：
```python
source_frame = clip.timeline_to_source_frame(timeline_frame)
```

此调用已在 Task 3.1 中间接走 mapper，无需额外修改。但需确认 `clip` 对象有 fps 元数据。

### Task 3.5：新增集成测试

**新建文件**：`tests/edit/test_mixed_fps_playback_cadence.py`

```python
def test_24fps_source_in_30fps_timeline_via_clip_instance():
    """24fps 素材在 30fps 时间线中，clip.timeline_to_source_frame() cadence 均匀"""

def test_30fps_source_in_24fps_timeline_via_clip_instance():
    """30fps 素材在 24fps 时间线中，跳帧均匀"""

def test_23_976fps_in_25fps_no_drift_over_1000_frames():
    """23.976 素材在 25fps 时间线中 1000 帧无累积漂移"""

def test_speed_half_via_clip_instance():
    """speed=0.5 时 source time 半速推进"""

def test_reverse_via_clip_instance():
    """reverse 时 source frame 单调递减"""
```

### 验证方法

```bash
# 编译检查
.venv/bin/python -m py_compile \
  src/features/edit/core/timeline_v2.py \
  src/features/edit/core/timeline_clip_frame_provider.py \
  src/features/edit/core/edit_playback_controller.py

# 运行测试
.venv/bin/python -m pytest \
  tests/edit/test_timeline_frame_mapper.py \
  tests/edit/test_mixed_fps_playback_cadence.py \
  -v --tb=short

# 手动验证:
# 1. 准备 24fps 和 30fps 视频各一段（匀速横移线条）
# 2. 放入 30fps 和 24fps 时间线
# 3. 播放时观察横移无不规则抖动
# 4. 检查日志中 cadence_action 分布均匀
```

---

## Task 4：缩略图调度器集成到 UI（P1）

**目标**：`timeline_thumbnail_scheduler.py` 模块已完整实现（369 行 + 298 行测试），但未被 UI 调用。

### Task 4.1：TimelineView 集成 scheduler

**修改文件**：`src/features/edit/ui/timeline_view.py`

```python
# 在 __init__ 中创建 scheduler
from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
self._thumb_scheduler = TimelineThumbnailScheduler()
self._thumb_scheduler.set_batch_callback(self._on_thumbnail_batch_ready)

# 监听滚动/缩放事件，提交 viewport 更新
def _on_scroll_or_zoom_changed(self):
    visible_clips = self._compute_visible_clips()
    jobs = self._thumb_scheduler.update_viewport(
        visible_clips=visible_clips,
        viewport_width=self.viewport().width(),
        pixels_per_frame=self._pixels_per_frame,
        zoom_level=self._zoom_level,
    )
    if jobs:
        self._submit_thumbnail_jobs(jobs)

# 播放状态控制
def _on_playback_started(self):
    self._thumb_scheduler.set_playback_active(True)

def _on_playback_stopped(self):
    self._thumb_scheduler.set_playback_active(False)
```

### Task 4.2：ClipItem.paint() 改为只读缓存

**修改文件**：`src/features/edit/ui/timeline_items.py`

```python
class ClipItem(QGraphicsRectItem):
    def paint(self, painter, option, widget=None):
        # 1. 查 L1 缓存（self._cached_thumbnail_set）
        # 2. 查 L2 缓存（scheduler.get_cached_thumbnail）
        # 3. 如果有缓存，绘制 pixmap
        # 4. 如果无缓存，绘制轻量 placeholder（纯 QPainter 渐变）
        # 5. 发 signal 请求生成（不在 paint 中触发解码）
        if not self._thumbnail_request_sent:
            self._thumbnail_request_sent = True
            self._request_thumbnail_signal.emit(self._clip_id)
```

关键约束：
- `paint()` 中**不能**调用 `ThumbnailProvider.get_thumbnail()` 触发解码
- placeholder 使用 `QLinearGradient` + `QPainter.fillRect()`
- 滚动/缩放时使用 debounce（80ms 滚动停止后提交）

### Task 4.3：添加批量解码回调

**修改文件**：`src/shared/video/frame_provider_service.py`

新增批量请求方法：

```python
def request_frames_batch(
    self,
    path: str,
    frame_indices: list[int],
    quality: str = "proxy",
) -> list[DecodedFrame | None]:
    """批量请求同一文件的多帧。

    优势：文件只打开一次，GOP 内按序解码避免重复 seek。

    Args:
        path: 视频文件路径
        frame_indices: 帧号列表（建议升序）
        quality: "proxy" 或 "master"

    Returns:
        与 frame_indices 等长的结果列表
    """
    clip_id = self.open_clip(path)
    results = []
    for idx in sorted(frame_indices):
        frame = self.request_frame(clip_id, idx, quality)
        results.append(frame)
    return results
```

### Task 4.4：缩略图磁盘缓存（L3）

**新建文件**：`src/shared/media/thumbnail_disk_cache.py`

```python
class ThumbnailDiskCache:
    """磁盘缩略图缓存。

    存储路径: .topos_cache/thumbnails/{asset_hash}/{height}/{frame}.webp
    Key: sha1(file_path + mtime_ns + file_size + frame + height)
    """

    def __init__(self, cache_dir: str): ...
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes) -> None: ...
    def invalidate(self, file_path: str, mtime_ns: int) -> None: ...
    def compute_key(self, file_path, mtime_ns, file_size, frame, height) -> str: ...
```

**修改文件**：`src/shared/media/thumbnail_provider.py`

在 `get_thumbnail()` / `request_frame_pixmap_async()` 中集成磁盘缓存：

```python
def get_thumbnail(self, ...) -> QPixmap | None:
    # 1. 查内存 LRU
    # 2. 查磁盘缓存
    # 3. 解码
    # 4. 写入磁盘缓存
    # 5. 写入内存 LRU
```

### 验证方法

```bash
# 编译检查
.venv/bin/python -m py_compile \
  src/features/edit/ui/timeline_view.py \
  src/features/edit/ui/timeline_items.py \
  src/shared/video/frame_provider_service.py \
  src/shared/media/thumbnail_disk_cache.py

# 运行测试
.venv/bin/python -m pytest \
  tests/edit/test_timeline_thumbnail_scheduler.py \
  -v --tb=short

# 手动验证:
# 1. 创建包含 50+ clip 的时间线
# 2. 开启 filmstrip 模式
# 3. 快速滚动：UI 不卡顿，缩略图渐进加载
# 4. 播放中：不新增缩略图解码
# 5. 关闭再打开项目：磁盘缓存命中，首屏快速显示
```

---

## Task 5：异步 Readback 真正非阻塞化（P1）

**目标**：当前 `_handle_readback_present_async()` 虽然不阻塞 UI 线程，但 render 线程内的 `fbo.read()` 仍是同步 DMA。`pbo_readback.py` 模块存在但 enqueue 注释承认 ModernGL 不支持真正的 PBO async。

### Task 5.1：renderer 内集成 PBO 或 render-thread pipelining

**修改文件**：`src/features/composite/render/composite_renderer.py`

方案：利用 render thread 的非阻塞特性做 readback pipelining。

```python
# 在 __init__ 中初始化 PBO readback
from src.features.composite.render.pbo_readback import AsyncPBOReadback
self._pbo_readback: AsyncPBOReadback | None = None

# 在 _initialize_gl() 中
self._pbo_readback = AsyncPBOReadback(self.ctx)
self._pbo_readback.initialize()

# 修改 _handle_readback_present_async()：
def _handle_readback_present_async(self, data: dict):
    request_id = data.get('request_id', 0)
    front_idx = (self.write_idx - 1) % 3
    fbo = self.fbo_ring[front_idx]
    tex = self.tex_ring[front_idx]
    if fbo is None or tex is None:
        return
    w, h = tex.size
    fbo.use()
    self.ctx.viewport = (0, 0, w, h)

    # 使用 PBO sync readback（render thread 上执行，不阻塞 UI）
    if self._pbo_readback:
        self._pbo_readback.enqueue_sync(fbo, w, h, request_id)
        results = self._pbo_readback.poll_ready()
        for r in results:
            self.preview_image_ready.emit(r.request_id, r.data, r.width, r.height)
    else:
        # 降级：直接 readback
        raw = fbo.read(components=4, dtype='f4')
        arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
        bgr = cv2.cvtColor(np.flipud(arr[:, :, :3]).copy(), cv2.COLOR_RGB2BGR)
        self.preview_image_ready.emit(request_id, bgr, w, h)
```

### Task 5.2：readback 频率限流

**修改文件**：`src/features/composite/adapters/main_window_adapter.py`

```python
# 在 _present_frame_to_viewer() 中添加限流
import time as _time
_last_readback_time: float = 0.0
_READBACK_MIN_INTERVAL = 1.0 / 24.0  # 最大 24fps readback

def _present_frame_to_viewer(self, tex_id, width, height):
    if tex_id > 0 and self._runtime.viewer.set_texture(tex_id, width, height):
        return  # direct texture 成功

    # 限流 readback
    now = _time.monotonic()
    if now - self._last_readback_time < self._READBACK_MIN_INTERVAL:
        return  # 跳过，避免 readback 堆积

    self._last_readback_time = now
    if self._runtime.page:
        self._runtime.page.renderer.submit_command(
            CompositeCommand.READBACK_PRESENT_ASYNC,
            {"request_id": self._runtime.page._request_id_gen.next_id()}
        )
```

### 验证方法

```bash
# 编译检查
.venv/bin/python -m py_compile \
  src/features/composite/render/composite_renderer.py \
  src/features/composite/adapters/main_window_adapter.py

# 手动验证:
# 1. 快速调色滑块后切 Composite：只显示最新帧，旧帧丢弃
# 2. 4K 图像 readback 时 UI 不阻塞
# 3. 连续切页 20 次无卡死
# 4. 日志中无 "[PBO-READBACK] Initialization failed"
```

---

## Task 6：全 GPU 解码链路接入（P2）

**目标**：macOS VT zero-copy 基础模块已就位（`source.py`、`iosurface_gl_bridge.py`、`capability.py`），但未接入 Composite 管线。

### Task 6.1：VideoFrameProviderService 暴露 GPU frame API

**修改文件**：`src/shared/video/frame_provider_service.py`

新增方法：

```python
def request_gpu_frame(
    self,
    clip_id: str,
    frame_index: int,
    quality: str = "proxy",
) -> "GpuDecodedFrame | None":
    """请求 GPU resident 帧（zero-copy 或 GPU upload）。

    优先级:
    1. macOS VT IOSurface zero-copy (如果可用)
    2. GPU upload cache (已上传过的帧)
    3. CPU decode + GPU upload fallback

    Returns:
        GpuDecodedFrame 或 None (失败时)
    """
    session = self._sessions.get(clip_id)
    if session is None:
        return None

    # 检查 zero-copy 能力
    from src.shared.media.macos_vt import is_zero_copy_available
    if is_zero_copy_available() and hasattr(session, '_zero_copy_source'):
        gpu_frame = session._zero_copy_source.read_frame(frame_index)
        if gpu_frame and gpu_frame.texture_id > 0:
            return gpu_frame

    # Fallback: CPU decode
    frame = self.request_frame(clip_id, frame_index, quality)
    if frame is None:
        return None

    # 标记 decode path
    from src.shared.media.frame_path import DecodePath
    frame._decode_path = DecodePath.CPU_PACKED_RGB.value
    return frame
```

**新建文件**：`src/shared/media/gpu_frame_handle.py`

```python
@dataclass
class TimelineFrameGPUHandle:
    """统一的 GPU 帧句柄，Composite / Edit / Color 共用。"""
    texture_id: int
    width: int
    height: int
    color_space: str = "srgb"
    bit_depth: int = 8
    origin: str = "gl_bottom_left"
    zero_copy: bool = False
    decode_path: str = "unknown"
    owner: str = "unknown"
    fence: object | None = None
    release_callback: Callable | None = None
```

### Task 6.2：TimelineClipFrameProvider 优先走 GPU frame

**修改文件**：`src/features/edit/core/timeline_clip_frame_provider.py`

在 `_get_source_frame_texture()` 中优先调用 `request_gpu_frame()`：

```python
def _get_source_frame_texture(self, ...):
    service = self._get_frame_provider_service()
    if service is None:
        return None

    asset_path = self._resolve_asset_path(runtime_context)
    if not asset_path:
        return None

    # 缓存检查
    cache_key = (asset_path, source_frame)
    if cache_key in self._source_texture_cache:
        ...

    # 新增：优先 GPU frame
    if hasattr(service, 'request_gpu_frame'):
        vid_clip_id = service.open_clip(asset_path)
        gpu_frame = service.request_gpu_frame(vid_clip_id, source_frame, quality='proxy')
        if gpu_frame and hasattr(gpu_frame, 'texture_id') and gpu_frame.texture_id > 0:
            result = _TexResult(
                texture_id=gpu_frame.texture_id,
                width=gpu_frame.width,
                height=gpu_frame.height,
                color_space="srgb",
                bit_depth=getattr(gpu_frame, 'bit_depth', 8),
                frame_metadata={
                    "origin": "gl_bottom_left",
                    "zero_copy": getattr(gpu_frame, 'zero_copy', False),
                    "decode_path": getattr(gpu_frame, 'decode_path', 'unknown'),
                },
            )
            # 缓存
            self._source_texture_cache[cache_key] = result
            logger.info(
                f"[TIMELINE-CLIP-FRAME] GPU frame path="
                f"{getattr(gpu_frame, 'decode_path', 'unknown')} "
                f"zero_copy={getattr(gpu_frame, 'zero_copy', False)}"
            )
            return result

    # 回退：CPU decode + upload（现有代码）
    ...
```

### Task 6.3：日志显示当前输入路径

**修改文件**：`src/features/edit/core/timeline_clip_frame_provider.py`

在每次返回 `_TexResult` 时打印路径信息：

```python
# 在 get_timeline_clip_texture() 返回前
decode_path = "placeholder"
if result and result.frame_metadata:
    if isinstance(result.frame_metadata, dict):
        decode_path = result.frame_metadata.get("decode_path", "unknown")
    elif result.is_valid:
        decode_path = "cpu-upload"
logger.info(f"Composite input path: {decode_path}")
```

### 验证方法

```bash
# 编译检查
.venv/bin/python -m py_compile \
  src/shared/video/frame_provider_service.py \
  src/shared/media/gpu_frame_handle.py \
  src/features/edit/core/timeline_clip_frame_provider.py

# 手动验证:
# 1. macOS 上打开 H.264/ProRes 视频
# 2. 进入 Composite，查看日志中 "Composite input path:" 输出
# 3. 支持 VT 的机型应显示 "macos_vt_iosurface_zero_copy"
# 4. 不支持时显示 "cpu_packed_rgb" 并有明确 fallback reason
```

---

## Task 7：旧架构清理（P3）

### Task 7.1：隔离 COLOR 节点

**修改文件**：`src/features/composite/nodes/color_node.py`

在文件顶部确认已有 DEPRECATED 注释（✅ 已有）。

确保新建默认图时**不**创建 `color_input` 节点：

**检查文件**：`src/features/composite/core/node_graph.py`

```python
def create_default_graph(self):
    """创建默认图：TimelineClipInput -> Output"""
    # 确认不包含 color_input
    # 如果有，移除
```

### Task 7.2：新 .topos 工程不依赖 ColorOutput

**检查文件**：`src/products/` 目录下的保存逻辑

确保保存 Composite graph 时：
- 如果图中有 `ToposColor` 节点，不保存 `color_input` 节点
- `color_texture_id` 字段只在有 `color_input` 时保存

### 验证方法

```bash
# 新建项目，进入 Composite
# 验证默认图只有 TimelineClipInput -> Output
# 保存项目，检查 .topos 文件中无 color_input 引用

# 打开旧项目（有 COLOR 节点）
# 验证仍能正常工作（向后兼容）
```

---

## 执行顺序

| 优先级 | Task | 预估工作量 | 依赖 |
|---:|---|---|---|
| 1 | Task 1: 补齐自动测试 | 2-3 天 | 无 |
| 2 | Task 2: GL Context Sharing 加固 | 2-3 天 | 无 |
| 3 | Task 3: 帧映射器集成 | 2-3 天 | 无 |
| 4 | Task 5: 异步 Readback 加固 | 1-2 天 | 无 |
| 5 | Task 4: 缩略图调度器集成 | 2-3 天 | 无 |
| 6 | Task 6: 全 GPU 链路接入 | 3-5 天 | Task 2 (context sharing) |
| 7 | Task 7: 旧架构清理 | 1 天 | Task 1 (测试保护) |

**总预估**：13-20 天

---

## 必跑验证命令

每个 Task 完成后执行：

```bash
# 全量编译检查
.venv/bin/python -m py_compile \
  src/features/edit/core/timeline_clip_frame_provider.py \
  src/features/edit/core/timeline_v2.py \
  src/features/edit/core/timeline_frame_mapper.py \
  src/features/edit/core/edit_playback_controller.py \
  src/features/edit/core/timeline_thumbnail_scheduler.py \
  src/features/composite/ui/composite_page.py \
  src/features/composite/adapters/main_window_adapter.py \
  src/features/composite/render/composite_renderer.py \
  src/features/composite/render/pbo_readback.py \
  src/features/composite/ui/composite_viewport.py \
  src/shared/video/frame_provider_service.py \
  src/app/controllers/page_controller.py \
  src/app/controllers/composite_controller.py \
  src/main.py

# 全量测试
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
  tests/edit/test_timeline_frame_mapper.py \
  tests/edit/test_timeline_thumbnail_scheduler.py \
  tests/edit/test_mixed_fps_playback_cadence.py \
  tests/composite/test_viewer_image_upload_origin.py \
  tests/composite/test_no_color_output_for_topos_color_graph.py \
  tests/composite/test_async_present_readback.py \
  -q --tb=short
```

---

## 完成标准

所有 Task 完成后应满足：

1. ✅ 所有已有功能有自动测试保护（Task 1）
2. ✅ GL context sharing 有 2x2 probe + fence + 诊断工具（Task 2）
3. ✅ 混合帧率播放无抖动，Edit/Color/Composite 共用同一映射（Task 3）
4. ✅ 缩略图 viewport 级调度，播放中不抢资源（Task 4）
5. ✅ Readback 不阻塞 UI，有限流和去重（Task 5）
6. ✅ Composite 至少能报告 zero-copy/gpu-upload/cpu-upload 路径（Task 6）
7. ✅ 旧 COLOR 节点只做兼容，新图不依赖（Task 7）
