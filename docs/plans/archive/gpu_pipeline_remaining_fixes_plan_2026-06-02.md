# GPU 管线稳定化 — 剩余修复实施计划

日期：2026-06-02
基于：前一轮审计发现的 6 个 BUG + 3 个未完成任务
适用范围：帧映射器生效、readback 全异步化、缩略图调度集成、GL Context 加固、全 GPU 链路接入

---

## 当前问题总览

| 编号 | 问题 | 严重性 | 影响 |
|---|---|---|---|
| BUG-1 | fps 元数据未注入 ClipInstance，帧映射器永不生效 | **P0** | 混合帧率播放优化完全失效 |
| BUG-2 | `_present_frame_to_viewer` 仍用同步 readback | **P1** | UI 线程最多阻塞 200ms |
| BUG-3 | 缩略图调度器未接入 UI | **P1** | 大时间线缩略图加载慢、抢资源 |
| BUG-4 | 全 GPU 链路未接入 | **P2** | zero-copy 不生效 |
| BUG-5 | GL Context Hub / Fence / 诊断工具未创建 | **P2** | sharing 判断不可靠 |
| BUG-6 | `_last_readback_time` 未在 `__init__` 初始化 | **P3** | 代码风格问题 |

---

## Task 1（P0）：fps 元数据注入到 ClipInstance

**问题根因**：`ClipInstance` 已添加 `_source_fps_num/den`、`_timeline_fps_num/den` 字段，`timeline_to_source_frame()` 也已实现 mapper 委托，但 `insert_clip_at_playhead()` 创建 `ClipInstance` 时从未设置这些字段，导致 mapper 分支永远不执行。

### 实施步骤

#### 1.1 扩展 `InsertClipCommand` 传递 fps 参数

**修改文件**：`src/features/edit/core/commands.py`（`InsertClipCommand` 类，约 L7052）

在 dataclass 中新增 fps 字段：

```python
@dataclass(frozen=True)
class InsertClipCommand:
    # ... 现有字段 ...
    speed: float = 1.0
    # 新增：fps 元数据
    _source_fps_num: int | None = None
    _source_fps_den: int | None = None
    _timeline_fps_num: int | None = None
    _timeline_fps_den: int | None = None
    _inserted_clip_id: str | None = None
    # ...
```

修改 `apply()` 方法，将 fps 参数传给 `insert_clip_at_playhead()`：

```python
def apply(self, timeline):
    new_timeline = insert_clip_at_playhead(
        timeline, self.asset_id, self.in_frame, self.out_frame,
        self.playhead, self.track_id, self.speed,
        source_fps_num=self._source_fps_num,
        source_fps_den=self._source_fps_den,
        timeline_fps_num=self._timeline_fps_num,
        timeline_fps_den=self._timeline_fps_den,
    )
    # ... 后续代码不变 ...
```

修改 `create()` 工厂方法：

```python
@staticmethod
def create(
    asset_id: str, in_frame: int, out_frame: int,
    playhead: int, track_id: str, speed: float = 1.0,
    source_fps_num: int | None = None,
    source_fps_den: int | None = None,
    timeline_fps_num: int | None = None,
    timeline_fps_den: int | None = None,
) -> InsertClipCommand:
    return InsertClipCommand(
        command_id=str(uuid.uuid4()),
        label=f"Insert Clip at {playhead}",
        asset_id=asset_id, in_frame=in_frame, out_frame=out_frame,
        playhead=playhead, track_id=track_id, speed=speed,
        _source_fps_num=source_fps_num,
        _source_fps_den=source_fps_den,
        _timeline_fps_num=timeline_fps_num,
        _timeline_fps_den=timeline_fps_den,
    )
```

同步修改 `to_dict()` 和 `from_dict()` 序列化方法，增加 fps 字段。

#### 1.2 修改 `insert_clip_at_playhead()` 设置 fps

**修改文件**：`src/features/edit/core/timeline_ops.py`（约 L886）

函数签名新增参数：

```python
def insert_clip_at_playhead(
    sequence, asset_id, in_frame, out_frame,
    playhead, track_id, speed=1.0,
    source_fps_num=None, source_fps_den=None,
    timeline_fps_num=None, timeline_fps_den=None,
) -> TimelineSequence:
```

创建 ClipInstance 时设置 fps 字段：

```python
new_clip = ClipInstance(
    clip_id=str(uuid.uuid4()),
    asset_id=asset_id, track_id=track_id,
    timeline_start=playhead,
    timeline_duration=timeline_duration,
    source_start=source_start,
    source_duration=source_duration,
    speed=speed,
    _source_fps_num=source_fps_num,
    _source_fps_den=source_fps_den,
    _timeline_fps_num=timeline_fps_num,
    _timeline_fps_den=timeline_fps_den,
)
```

#### 1.3 调用链传递 fps 元数据

**修改文件**：`src/features/edit/ui/timeline_view.py`

`insert_clip()` 方法新增 fps 参数：

```python
def insert_clip(self, asset_id, in_frame, out_frame,
                playhead=None, track_id=None, speed=1.0,
                source_fps_num=None, source_fps_den=None,
                timeline_fps_num=None, timeline_fps_den=None) -> bool:
    cmd = InsertClipCommand.create(
        asset_id=asset_id, in_frame=in_frame, out_frame=out_frame,
        playhead=playhead, track_id=track_id, speed=speed,
        source_fps_num=source_fps_num,
        source_fps_den=source_fps_den,
        timeline_fps_num=timeline_fps_num,
        timeline_fps_den=timeline_fps_den,
    )
```

**修改文件**：`src/features/edit/ui/timeline_panel.py`（`insert_clip` 方法，约 L1340）

同样新增 fps 参数透传。

**修改文件**：`src/features/edit/ui/edit_page.py`（约 L625）

在拖拽插入 clip 时，从 asset 的 VideoMetadata 和 TimelineSequence 读取 fps：

```python
# 在 insert 调用前获取 fps
source_fps = None
timeline_fps = None
asset = self._project.assets.get(asset_id)
if asset and hasattr(asset, 'video_metadata') and asset.video_metadata:
    source_fps = asset.video_metadata.fps  # Rational(num, den)

timeline = self._get_current_timeline()
if timeline:
    timeline_fps = timeline.fps  # Rational(num, den)

success = self._timeline_panel.insert_clip(
    asset_id=asset_id,
    in_frame=in_frame, out_frame=out_frame,
    playhead=playhead,
    source_fps_num=source_fps.num if source_fps else None,
    source_fps_den=source_fps.den if source_fps else None,
    timeline_fps_num=timeline_fps.num if timeline_fps else None,
    timeline_fps_den=timeline_fps.den if timeline_fps else None,
)
```

#### 1.4 新增测试

**新建文件**：`tests/edit/test_clip_fps_metadata_propagation.py`

```python
def test_insert_clip_sets_fps_metadata():
    """insert_clip_at_playhead 创建的 clip 应包含 fps 元数据"""
    seq = TimelineSequence(fps=Rational(30, 1))
    result = insert_clip_at_playhead(
        seq, "asset-1", 0, 99, 0, "track-1",
        source_fps_num=24, source_fps_den=1,
        timeline_fps_num=30, timeline_fps_den=1,
    )
    clip = result.video_tracks[0].clips[0]
    assert clip._source_fps_num == 24
    assert clip._source_fps_den == 1
    assert clip._timeline_fps_num == 30
    assert clip._timeline_fps_den == 1

def test_inserted_clip_uses_mapper():
    """有 fps 元数据的 clip 应走 mapper 路径"""
    seq = TimelineSequence(fps=Rational(30, 1))
    result = insert_clip_at_playhead(
        seq, "asset-1", 0, 23, 0, "track-1",
        source_fps_num=24, source_fps_den=1,
        timeline_fps_num=30, timeline_fps_den=1,
    )
    clip = result.video_tracks[0].clips[0]
    frames = [clip.timeline_to_source_frame(i) for i in range(30)]
    # 24fps in 30fps: cadence 应均匀，最大 gap ≤ 1
    diffs = [frames[i+1] - frames[i] for i in range(29)]
    assert max(diffs) <= 1

def test_insert_without_fps_falls_back():
    """无 fps 元数据时回退到旧公式"""
    seq = TimelineSequence()
    result = insert_clip_at_playhead(seq, "asset-1", 0, 99, 0, "track-1")
    clip = result.video_tracks[0].clips[0]
    assert clip._source_fps_num is None
    # 旧公式仍正常工作
    assert clip.timeline_to_source_frame(5) == 5

def test_with_methods_preserve_fps():
    """所有 with_* 方法应保留 fps 字段"""
    clip = ClipInstance(
        asset_id="a", track_id="t",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
        _source_fps_num=24, _source_fps_den=1,
        _timeline_fps_num=30, _timeline_fps_den=1,
    )
    moved = clip.with_timeline_start(10)
    assert moved._source_fps_num == 24
    assert moved._timeline_fps_num == 30

    trimmed = clip.with_trim_left(5, 5)
    assert trimmed._source_fps_num == 24

    sped = clip.with_speed(2.0)
    assert sped._source_fps_num == 24
```

### 验证方法

```bash
# 编译检查
.venv/bin/python -m py_compile \
  src/features/edit/core/commands.py \
  src/features/edit/core/timeline_ops.py \
  src/features/edit/ui/timeline_view.py \
  src/features/edit/ui/timeline_panel.py \
  src/features/edit/ui/edit_page.py

# 运行测试
.venv/bin/python -m pytest \
  tests/edit/test_clip_fps_metadata_propagation.py \
  tests/edit/test_mixed_fps_playback_cadence.py \
  tests/edit/test_timeline_frame_mapper.py \
  -v --tb=short

# 手动验证
# 1. 导入 24fps 素材到 30fps 时间线
# 2. 播放时检查日志中 timeline_to_source_frame 调用是否走 mapper
# 3. 横移线条视频应无不规则抖动
```

---

## Task 2（P1）：`_present_frame_to_viewer` 改为全异步

**问题根因**：当 direct texture sharing 失败时，fallback 路径仍在 UI 线程调用 `request_output_readback(timeout=0.2)`，最多阻塞 200ms。renderer 端已实现 `_handle_readback_present_async()` 和 PBO 集成，但 adapter 没有调用。

### 实施步骤

#### 2.1 修改 `_present_frame_to_viewer` 提交异步命令

**修改文件**：`src/features/composite/adapters/main_window_adapter.py`（约 L204-L239）

当前代码：
```python
def _present_frame_to_viewer(self, tex_id, width, height):
    if tex_id > 0 and self._runtime.viewer.set_texture(tex_id, width, height):
        return
    # ... rate limit ...
    data = self._runtime.page.request_output_readback(timeout=0.2)  # ❌ 同步
```

改为：
```python
def _present_frame_to_viewer(self, tex_id: int, width: int, height: int) -> None:
    """Present the renderer output in the Composite viewer.

    Strategy:
    1. Try direct texture sharing (set_texture) - zero-copy, no readback
    2. If that fails, submit READBACK_PRESENT_ASYNC to render thread
       (non-blocking for UI). Result arrives via preview_image_ready signal.
    """
    if not self._runtime.viewer:
        return

    # Try direct texture sharing first (zero-copy, no readback)
    if tex_id > 0 and self._runtime.viewer.set_texture(tex_id, width, height):
        return

    # Rate-limit readback to max ~24fps to avoid GPU saturation
    import time as _time
    now = _time.monotonic()
    if now - self._last_readback_time < 1.0 / 24.0:
        return  # Skip — too soon since last readback
    self._last_readback_time = now

    # Submit async readback to render thread (non-blocking for UI)
    page = self._runtime.page
    if page and page._renderer:
        from src.features.composite.render.composite_renderer import CompositeCommand
        page._renderer.submit_command(
            CompositeCommand.READBACK_PRESENT_ASYNC,
            {"request_id": page._request_id_gen.next_id()}
        )
        logger.debug(
            "[COMPOSITE-PRESENT] direct_texture_sharing=false, "
            "mode=async_readback"
        )
    else:
        logger.debug(
            "[COMPOSITE-PRESENT] direct_texture_sharing=false, "
            "mode=readback_fallback (renderer unavailable)"
        )
```

#### 2.2 在 `__init__` 中初始化 `_last_readback_time`

**修改文件**：`src/features/composite/adapters/main_window_adapter.py`

在 `MainWindowAdapter.__init__()` 中添加：

```python
self._last_readback_time: float = 0.0
```

#### 2.3 更新测试

**修改文件**：`tests/composite/test_async_present_readback.py`

追加测试：

```python
def test_present_uses_async_command_not_sync_readback():
    """_present_frame_to_viewer 应提交 READBACK_PRESENT_ASYNC 而非同步 readback"""
    from unittest.mock import MagicMock, patch, call
    adapter = MagicMock()
    adapter._runtime = MagicMock()
    adapter._runtime.viewer = MagicMock()
    adapter._runtime.viewer.set_texture.return_value = False  # direct sharing fails
    adapter._runtime.page._renderer = MagicMock()
    adapter._runtime.page._request_id_gen = MagicMock()
    adapter._runtime.page._request_id_gen.next_id.return_value = 42
    adapter._last_readback_time = 0.0  # long ago

    MainWindowAdapter._present_frame_to_viewer(adapter, 1, 100, 100)

    # Should submit async command, NOT call request_output_readback
    adapter._runtime.page._renderer.submit_command.assert_called_once()
    adapter._runtime.page.request_output_readback.assert_not_called()

def test_last_readback_time_initialized_in_init():
    """__init__ 应初始化 _last_readback_time 为 0.0"""
    import inspect
    source = inspect.getsource(MainWindowAdapter.__init__)
    assert '_last_readback_time' in source
```

### 验证方法

```bash
.venv/bin/python -m pytest tests/composite/test_async_present_readback.py -v --tb=short

# 手动验证:
# 1. Composite 页面 direct texture 失败时（日志 mode=async_readback）
# 2. UI 不冻结，画面在 <500ms 内显示
# 3. 快速调色滑块后切 Composite：只显示最新帧
```

---

## Task 3（P1）：缩略图调度器集成到 UI

**问题根因**：`TimelineThumbnailScheduler` 模块完整实现（369 行 + 298 行测试），但 `timeline_view.py` 和 `timeline_items.py` 完全未引用它。

### 实施步骤

#### 3.1 TimelineView 集成 scheduler

**修改文件**：`src/features/edit/ui/timeline_view.py`

在 `__init__` 中创建 scheduler：

```python
from src.features.edit.core.timeline_thumbnail_scheduler import (
    TimelineThumbnailScheduler, TimelineThumbnailJob,
)

# 在 TimelineView.__init__() 中
self._thumb_scheduler = TimelineThumbnailScheduler()
self._thumb_scheduler.set_batch_callback(self._on_thumbnail_batch_ready)
self._scroll_debounce_timer = None
```

监听滚动/缩放事件：

```python
def _on_scroll_or_zoom_changed(self):
    """Debounce 80ms 后提交缩略图批次"""
    if self._scroll_debounce_timer is not None:
        self._scroll_debounce_timer.stop()

    self._scroll_debounce_timer = QTimer(self)
    self._scroll_debounce_timer.setSingleShot(True)
    self._scroll_debounce_timer.setInterval(80)
    self._scroll_debounce_timer.timeout.connect(self._submit_viewport_thumbnails)
    self._scroll_debounce_timer.start()

def _submit_viewport_thumbnails(self):
    """收集可见 clip 并提交给 scheduler"""
    if self._timeline is None:
        return
    visible_rect = self.viewport().rect()
    visible_clips = self._compute_visible_clip_info(visible_rect)
    jobs = self._thumb_scheduler.update_viewport(
        visible_clips=visible_clips,
        viewport_width=visible_rect.width(),
        pixels_per_frame=self._pixels_per_frame,
        zoom_level=self._zoom_level,
    )
    if jobs:
        self._submit_thumbnail_jobs(jobs)

def _compute_visible_clip_info(self, visible_rect) -> list[dict]:
    """遍历可见区域的 clip，构建 clip_info 列表"""
    result = []
    for track in self._timeline.video_tracks:
        for clip in track.clips:
            clip_x = clip.timeline_start * self._pixels_per_frame
            clip_w = clip.timeline_duration * self._pixels_per_frame
            clip_right = clip_x + clip_w
            # 检查是否在可见区域内
            if clip_right < visible_rect.left() or clip_x > visible_rect.right():
                continue
            vis_start = max(clip_x, visible_rect.left())
            vis_end = min(clip_right, visible_rect.right())
            result.append({
                "asset_id": clip.asset_id,
                "file_path": self._resolve_asset_path(clip.asset_id),
                "clip_id": clip.clip_id,
                "source_start": clip.source_start,
                "source_duration": clip.source_duration,
                "visible_px_start": int(vis_start),
                "visible_px_end": int(vis_end),
                "mode": "filmstrip",
            })
    return result

def _on_thumbnail_batch_ready(self, jobs: list[TimelineThumbnailJob]):
    """scheduler 的 batch callback — 提交给 ThumbnailProvider"""
    for job in jobs:
        if not self._thumb_scheduler.is_job_valid(job):
            continue
        # 提交给现有 ThumbnailProvider 解码
        self._thumb_provider.request_frame_pixmap_async(
            job.file_path, job.source_frame, job.target_height,
            callback=lambda pixmap, j=job: self._on_thumbnail_decoded(j, pixmap),
        )

def _on_thumbnail_decoded(self, job: TimelineThumbnailJob, pixmap):
    """解码完成回调 — 缓存并刷新 ClipItem"""
    if not self._thumb_scheduler.is_job_valid(job):
        return
    self._thumb_scheduler.cache_thumbnail(
        job.asset_id, job.source_frame, job.target_height, pixmap,
    )
    # 通知对应的 ClipItem 刷新
    clip_item = self._find_clip_item(job.clip_id)
    if clip_item:
        clip_item.update()
```

连接播放状态：

```python
# 在播放开始处
self._thumb_scheduler.set_playback_active(True)

# 在播放结束处
self._thumb_scheduler.set_playback_active(False)
```

连接 scroll bar 信号（在 `__init__` 中）：

```python
self.horizontalScrollBar().valueChanged.connect(
    lambda _: self._on_scroll_or_zoom_changed()
)
```

#### 3.2 ClipItem.paint() 改为只读缓存

**修改文件**：`src/features/edit/ui/timeline_items.py`

在 `ClipItem.paint()` 中（当前方法较长，找到缩略图绘制部分）：

```python
def paint(self, painter, option, widget=None):
    # ... 现有背景和选中框绘制 ...

    # 缩略图绘制：只查缓存，不触发解码
    if self._thumbnail_mode == "filmstrip":
        self._paint_filmstrip_from_cache(painter)
    elif self._thumbnail_mode in ("single", "start_end"):
        self._paint_thumbnail_from_cache(painter)

    # ... 现有文字、边框绘制 ...

def _paint_thumbnail_from_cache(self, painter):
    """从 L1/L2 缓存获取缩略图，无缓存则画 placeholder"""
    # L1: self._cached_thumbnail_set (已有)
    if self._cached_thumbnail_set:
        painter.drawPixmap(...)
        return

    # L2: scheduler 缓存
    scheduler = self._get_scheduler()
    if scheduler:
        cached = scheduler.get_cached_thumbnail(
            self._asset_id, self._source_start, 80,
        )
        if cached:
            painter.drawPixmap(...)
            return

    # 无缓存：画轻量 placeholder
    gradient = QLinearGradient(0, 0, 0, self.rect().height())
    gradient.setColorAt(0, QColor(60, 60, 60))
    gradient.setColorAt(1, QColor(40, 40, 40))
    painter.fillRect(self.rect(), gradient)

    # 发 signal 请求生成（不在 paint 中触发解码）
    if not self._thumbnail_request_sent:
        self._thumbnail_request_sent = True
        # 通过 scene 或 parent view 发 signal
```

关键约束：**`paint()` 中不能调用 `ThumbnailProvider.get_thumbnail()` 触发解码**。

### 验证方法

```bash
.venv/bin/python -m py_compile \
  src/features/edit/ui/timeline_view.py \
  src/features/edit/ui/timeline_items.py

.venv/bin/python -m pytest \
  tests/edit/test_timeline_thumbnail_scheduler.py \
  -v --tb=short

# 手动验证:
# 1. 创建 50+ clip 时间线
# 2. 开启 filmstrip，快速滚动：UI 不卡，缩略图渐进加载
# 3. 播放中：不新增缩略图解码
# 4. 停止滚动后 1 秒内可见区缩略图加载完成
```

---

## Task 4（P2）：GL Context Hub + Fence 同步 + 诊断工具

### 4.1 创建 GL Context Hub

**新建文件**：`src/shared/gpu/gl_context_hub.py`

```python
"""GL Context Hub — 统一 GL context 管理中心。

职责:
1. 记录主 UI GL context (QOpenGLWidget context)
2. 注册各渲染 context (Composite, Edit preview, Color async)
3. 提供 2x2 像素 probe 验证 sharing 是否有效
4. 缓存 sharing 结果，避免重复探测
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


class GLContextHub:
    _instance = None

    def __init__(self):
        self._contexts: dict[str, Any] = {}
        self._sharing_cache: dict[tuple[str, str], bool] = {}

    @classmethod
    def instance(cls) -> GLContextHub:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_context(self, name: str, ctx: Any) -> None:
        self._contexts[name] = ctx
        logger.info(f"[GL-HUB] Registered context: {name}")

    def get_context(self, name: str) -> Any:
        return self._contexts.get(name)

    def probe_sharing(self, name_a: str, name_b: str) -> bool:
        """2x2 像素 probe — 比 glGetTexLevelParameter 查 size 更可靠"""
        key = tuple(sorted([name_a, name_b]))
        if key in self._sharing_cache:
            return self._sharing_cache[key]

        ctx_a = self._contexts.get(name_a)
        ctx_b = self._contexts.get(name_b)
        if ctx_a is None or ctx_b is None:
            self._sharing_cache[key] = False
            return False

        try:
            result = self._do_pixel_probe(ctx_a, ctx_b)
            self._sharing_cache[key] = result
            level = logging.INFO if result else logging.WARNING
            logger.log(level,
                f"[GL-HUB] Probe {name_a} <-> {name_b}: "
                f"{'PASS' if result else 'FAIL'}"
            )
            return result
        except Exception as e:
            logger.warning(f"[GL-HUB] Probe error: {e}")
            self._sharing_cache[key] = False
            return False

    def _do_pixel_probe(self, ctx_a, ctx_b) -> bool:
        """在 ctx_a 写 2x2 测试纹理，从 ctx_b 读回验证像素匹配"""
        import numpy as np
        # 测试像素: red, green, blue, white
        test_data = np.array([
            [255, 0, 0, 255],     # red
            [0, 255, 0, 255],     # green
            [0, 0, 255, 255],     # blue
            [255, 255, 255, 255], # white
        ], dtype=np.uint8)

        # 在 ctx_a 创建纹理
        tex = ctx_a.texture(size=(2, 2), components=4, dtype='f1',
                            data=test_data.tobytes())

        # 从 ctx_b 读取
        try:
            fbo = ctx_b.framebuffer(
                color_attachments=[ctx_b.texture(size=(2, 2), components=4, dtype='f1')]
            )
            fbo.use()
            # 如果 sharing 有效，应该能读到同一纹理
            # 简化验证：检查 tex.glo 在 ctx_b 中是否可查询
            import ctypes
            from OpenGL import GL as _gl
            w = ctypes.c_int()
            _gl.glBindTexture(_gl.GL_TEXTURE_2D, tex.glo)
            _gl.glGetTexLevelParameteriv(
                _gl.GL_TEXTURE_2D, 0, _gl.GL_TEXTURE_WIDTH, ctypes.byref(w)
            )
            _gl.glBindTexture(_gl.GL_TEXTURE_2D, 0)
            return w.value == 2
        except Exception:
            return False
        finally:
            tex.release()

    def get_direct_texture_enabled(self, renderer: str, viewer: str) -> bool:
        return self.probe_sharing(renderer, viewer)
```

### 4.2 升级 CompositeViewer 的 probe

**修改文件**：`src/features/composite/ui/composite_viewport.py`（`_probe_context_sharing`，约 L4064）

```python
def _probe_context_sharing(self, renderer_tex_id: int) -> bool:
    if self._sharing_probe_attempted:
        return self._sharing_probe_passed or False
    self._sharing_probe_attempted = True

    if not self._gl_initialized or renderer_tex_id <= 0:
        self._sharing_probe_passed = False
        return False

    # 优先使用 GLContextHub 的 2x2 probe
    try:
        from src.shared.gpu.gl_context_hub import GLContextHub
        hub = GLContextHub.instance()
        hub.register_context("composite_viewer", self.context())
        result = hub.probe_sharing("composite_renderer", "composite_viewer")
        self._sharing_probe_passed = result
        return result
    except Exception:
        pass

    # Fallback: 旧 size 检查
    try:
        self.makeCurrent()
        import ctypes
        from OpenGL import GL as _gl
        w = ctypes.c_int()
        h = ctypes.c_int()
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, renderer_tex_id)
        _gl.glGetTexLevelParameteriv(_gl.GL_TEXTURE_2D, 0, _gl.GL_TEXTURE_WIDTH, ctypes.byref(w))
        _gl.glGetTexLevelParameteriv(_gl.GL_TEXTURE_2D, 0, _gl.GL_TEXTURE_HEIGHT, ctypes.byref(h))
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, 0)
        self.doneCurrent()

        self._sharing_probe_passed = (w.value > 0 and h.value > 0)
        return self._sharing_probe_passed
    except Exception as e:
        self._sharing_probe_passed = False
        return False
```

### 4.3 Fence 同步（viewer 端）

**修改文件**：`src/features/composite/ui/composite_viewport.py`

在 `set_texture()` 绘制前添加 fence wait：

```python
def set_texture(self, tex_id: int, width: int, height: int) -> bool:
    # ... 现有验证 ...

    # 如果有 pending fence，等待 GPU 写入完成
    self._wait_for_fence()

    self._current_tex_id = tex_id
    # ... 现有代码 ...

def _wait_for_fence(self):
    """等待 renderer 端的 GPU fence"""
    fence = getattr(self, '_pending_fence', None)
    if fence is not None:
        try:
            from OpenGL import GL as _gl
            _gl.glWaitSync(fence, 0, _gl.GL_TIMEOUT_IGNORED)
            _gl.glDeleteSync(fence)
        except Exception:
            pass
        self._pending_fence = None
```

### 4.4 诊断工具

**新建文件**：`tools/diagnose_gl_context_sharing.py`

```python
#!/usr/bin/env python3
"""GL Context Sharing 诊断工具。

输出:
- AA_ShareOpenGLContexts 是否启用
- 各 context format (version, profile)
- 2x2 probe 结果 (pass/fail)
- direct texture presentation: enabled/disabled

用法: .venv/bin/python tools/diagnose_gl_context_sharing.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    print("=== GL Context Sharing Diagnostic ===")
    print(f"AA_ShareOpenGLContexts: enabled")

    # 创建 2 个 QOpenGLWidget
    w1 = QOpenGLWidget()
    w2 = QOpenGLWidget()
    w1.show(); w1.hide()
    w2.show(); w2.hide()

    ctx1 = w1.context()
    ctx2 = w2.context()

    fmt1 = ctx1.format()
    fmt2 = ctx2.format()
    print(f"Widget1: version={fmt1.majorVersion()}.{fmt1.minorVersion()}, "
          f"profile={fmt1.profile()}")
    print(f"Widget2: version={fmt2.majorVersion()}.{fmt2.minorVersion()}, "
          f"profile={fmt2.profile()}")

    # 2x2 probe
    try:
        import moderngl
        w1.makeCurrent()
        mgl1 = moderngl.create_context()
        w2.makeCurrent()
        mgl2 = moderngl.create_context()

        # 写测试纹理
        tex = mgl1.texture(size=(2, 2), components=4, dtype='f1',
                           data=b'\xff\x00\x00\xff\x00\xff\x00\xff'
                                b'\x00\x00\xff\xff\xff\xff\xff\xff')
        # 从 ctx2 读
        w2.makeCurrent()
        import ctypes
        from OpenGL import GL as _gl
        w_val = ctypes.c_int()
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, tex.glo)
        _gl.glGetTexLevelParameteriv(_gl.GL_TEXTURE_2D, 0,
                                     _gl.GL_TEXTURE_WIDTH, ctypes.byref(w_val))
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, 0)
        result = w_val.value == 2
        print(f"2x2 probe: {'PASS' if result else 'FAIL'}")
        print(f"Direct texture presentation: {'enabled' if result else 'disabled'}")
    except Exception as e:
        print(f"2x2 probe: ERROR ({e})")
        print("Direct texture presentation: disabled")

    print("=== Done ===")
    app.quit()

if __name__ == "__main__":
    main()
```

### 验证方法

```bash
.venv/bin/python tools/diagnose_gl_context_sharing.py
# 期望输出:
# AA_ShareOpenGLContexts: enabled
# 2x2 probe: PASS
# Direct texture presentation: enabled
```

---

## Task 5（P2）：全 GPU 解码链路接入

### 5.1 VideoFrameProviderService 暴露 GPU frame API

**修改文件**：`src/shared/video/frame_provider_service.py`

在 `VideoFrameProviderService` 类中新增方法：

```python
def request_gpu_frame(
    self, clip_id: str, frame_index: int, quality: str = "proxy",
) -> Any | None:
    """请求 GPU resident 帧。

    优先级:
    1. macOS VT IOSurface zero-copy (如果可用)
    2. CPU decode + GPU upload fallback

    Returns:
        GpuDecodedFrame (有 texture_id) 或 DecodedFrame (CPU) 或 None
    """
    session = self._sessions.get(clip_id)
    if session is None:
        return None

    # 尝试 zero-copy
    try:
        from src.shared.media.macos_vt import is_zero_copy_available
        if is_zero_copy_available() and hasattr(session, '_vt_source'):
            gpu_frame = session._vt_source.read_frame(frame_index)
            if gpu_frame and getattr(gpu_frame, 'texture_id', 0) > 0:
                logger.debug(
                    f"[GPU-FRAME] zero-copy clip={clip_id} frame={frame_index} "
                    f"path=macos_vt_iosurface_zero_copy"
                )
                return gpu_frame
    except ImportError:
        pass

    # CPU fallback
    frame = self.request_frame(clip_id, frame_index, quality)
    if frame is not None:
        logger.debug(
            f"[GPU-FRAME] cpu-fallback clip={clip_id} frame={frame_index} "
            f"path=cpu_packed_rgb"
        )
    return frame
```

### 5.2 新建 GPU frame handle

**新建文件**：`src/shared/media/gpu_frame_handle.py`

```python
"""统一的 GPU 帧句柄，Composite / Edit / Color 共用。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable

@dataclass
class TimelineFrameGPUHandle:
    texture_id: int
    width: int
    height: int
    color_space: str = "srgb"
    bit_depth: int = 8
    origin: str = "gl_bottom_left"
    zero_copy: bool = False
    decode_path: str = "unknown"
    owner: str = "unknown"
    release_callback: Callable | None = None
```

### 5.3 TimelineClipFrameProvider 优先走 GPU frame

**修改文件**：`src/features/edit/core/timeline_clip_frame_provider.py`

在 `_get_source_frame_texture()` 的缓存检查之后、CPU decode 之前插入：

```python
# 优先尝试 GPU frame (zero-copy 或 GPU upload)
if hasattr(service, 'request_gpu_frame'):
    vid_clip_id = service.open_clip(asset_path)
    try:
        gpu_frame = service.request_gpu_frame(
            vid_clip_id, source_frame, quality='proxy'
        )
        if (gpu_frame is not None
            and hasattr(gpu_frame, 'texture_id')
            and gpu_frame.texture_id > 0):
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
            cache_key = (asset_path, source_frame)
            self._source_texture_cache[cache_key] = result
            logger.info(
                f"Composite input path: "
                f"{getattr(gpu_frame, 'decode_path', 'unknown')} "
                f"zero_copy={getattr(gpu_frame, 'zero_copy', False)}"
            )
            return result
    except Exception as e:
        logger.debug(f"GPU frame request failed: {e}, falling back to CPU")
```

### 验证方法

```bash
.venv/bin/python -m py_compile \
  src/shared/video/frame_provider_service.py \
  src/shared/media/gpu_frame_handle.py \
  src/features/edit/core/timeline_clip_frame_provider.py

# 手动验证:
# 1. macOS 上打开 H.264 视频，进入 Composite
# 2. 查看日志 "Composite input path:" 输出
# 3. 支持 VT 的机型: macos_vt_iosurface_zero_copy
# 4. 不支持时: cpu_packed_rgb
```

---

## 执行顺序

| 顺序 | Task | 预估 | 依赖 |
|---:|---|---|---|
| 1 | Task 1: fps 元数据注入（P0） | 1-2 天 | 无 |
| 2 | Task 2: readback 全异步化（P1） | 0.5 天 | 无 |
| 3 | Task 3: 缩略图调度器集成（P1） | 2-3 天 | 无 |
| 4 | Task 4: GL Context Hub（P2） | 2 天 | 无 |
| 5 | Task 5: 全 GPU 链路（P2） | 2-3 天 | Task 4 |

**总预估**：7-10 天

---

## 必跑验证命令

```bash
# 全量编译检查
.venv/bin/python -m py_compile \
  src/features/edit/core/commands.py \
  src/features/edit/core/timeline_ops.py \
  src/features/edit/core/timeline_v2.py \
  src/features/edit/core/timeline_frame_mapper.py \
  src/features/edit/core/timeline_clip_frame_provider.py \
  src/features/edit/ui/timeline_view.py \
  src/features/edit/ui/timeline_panel.py \
  src/features/edit/ui/edit_page.py \
  src/features/composite/ui/composite_page.py \
  src/features/composite/adapters/main_window_adapter.py \
  src/features/composite/render/composite_renderer.py \
  src/features/composite/render/pbo_readback.py \
  src/features/composite/ui/composite_viewport.py \
  src/shared/video/frame_provider_service.py \
  src/shared/gpu/gl_context_hub.py \
  src/shared/media/gpu_frame_handle.py \
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
  tests/edit/test_clip_fps_metadata_propagation.py \
  tests/composite/test_no_color_output_for_topos_color_graph.py \
  tests/composite/test_async_present_readback.py \
  -q --tb=short
```

---

## 完成标准

1. ✅ ClipInstance 在 insert 时自动获取 fps 元数据（Task 1）
2. ✅ `timeline_to_source_frame()` 走 mapper 路径（有 fps 时）
3. ✅ `_present_frame_to_viewer` 不阻塞 UI 线程（Task 2）
4. ✅ 缩略图按 viewport 调度，播放中不抢资源（Task 3）
5. ✅ GL context sharing 有 2x2 probe 和 fence（Task 4）
6. ✅ Composite 日志输出 `input path: zero-copy/gpu-upload/cpu-upload`（Task 5）
