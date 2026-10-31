# Color 与 Edit 时间线打通后续优化计划

日期：2026-05-31

目标：在已经完成 Color 与 Edit 时间线打通的基础上，修掉剩余隐患，重点加强 Color 页面时间线片段面板的预览图性能，避免大量镜头时拖慢 UI、播放、解码和调色渲染。

---

## 1. 当前结论

这次打通的方向是正确的：

1. 视频调色已经改为从 Edit 时间线 clip 进入 Color。
2. Color 页面新增了 `TimelineClipStrip`，可以展示时间线上的视频/图片 clip。
3. `ColorSession` 已经有 `timeline_id`、`clip_id`、`timeline_frame`、`source_frame`、`clip_local_frame`。
4. `open_timeline_clip_in_color(timeline_id, clip_id)` 已经成为较明确的入口。

但还有几个必须继续优化的点：

1. 片段预览图当前是“打开 Color 页面时为全部 clip 立即请求缩略图”，长时间线会有明显性能风险。
2. 缩略图 worker 当前在后台线程创建 `QPixmap`，这不符合 Qt 线程模型，可能在某些机器上随机崩溃或出现空图。
3. 预览图信号是全局广播到每张 card，clip 数量大时会出现 O(N * M) 的无效回调。
4. Color 页面刷新片段面板主要发生在进入 Color 时，Edit 时间线变更后的实时同步还不够完整。
5. ColorStack 载入目前重点恢复了基础调色参数，HSL、曲线、LUT、soft clip 等 UI 面板状态还需要统一 hydration。

---

## 2. 已顺手修复的问题

以下问题已经修掉，后续 AI 不要重复实现：

| 编号 | 文件 | 问题 | 修复方式 |
|---|---|---|---|
| F-01 | `src/app/controllers/page_controller.py` | `switch_from_edit_to_color()` 创建 context 时没有 `timeline_id`，会导致 `TIMELINE_CLIP` session 创建失败 | 改为统一委托 `open_timeline_clip_in_color()` |
| F-02 | `src/app/controllers/file_controller.py` | SaveRouter 的 `get_current_timeline_id` 错误返回 `project_id` | 新增并使用 `ColorSessionManager.get_current_timeline_id()` |
| F-03 | `src/app/controllers/file_controller.py` | `load_video(for_timeline_clip=True)` 仍会读取单视频 `.topos` 项目，污染时间线 clip 调色 | timeline clip 加载视频时跳过 `_load_video_project()` |
| F-04 | `src/features/color/core/color_session.py` | `frame = timeline_frame or source_frame` 会把 `0` 当作缺失值 | 改为显式 `is not None` 判断 |
| F-05 | `src/features/color/ui/timeline_clip_strip.py` | 视频缩略图缓存命中时，先请求后连接信号，可能丢失同步 emit 的结果 | 改为先连接 signal 再请求 |
| F-06 | `src/features/color/ui/timeline_clip_strip.py` | 切换 Preview Frame 设置后没有重建 card | `_set_preview_mode()` 后重建 card |

验证命令：

```bash
.venv/bin/python -m py_compile \
  src/app/controllers/page_controller.py \
  src/app/controllers/file_controller.py \
  src/features/color/core/color_session.py \
  src/features/color/ui/timeline_clip_strip.py

.venv/bin/python -m pytest \
  tests/architecture/test_page_context.py \
  tests/color/test_color_session.py \
  tests/edit/test_clip_timeline_color_frame_mapping.py \
  tests/project/test_save_router.py \
  -q
```

当前结果：`129 passed`。

---

## 3. P0：预览图线程安全改造

### 问题

`src/shared/media/thumbnail_provider.py` 中 `ThumbnailRequest`、`FrameThumbnailRequest`、`ClipThumbnailWorker` 在 `QRunnable.run()` 后台线程里创建 `QPixmap`：

```python
pixmap = QPixmap.fromImage(q_img)
```

Qt 官方线程模型里，`QPixmap` 是 GUI 资源，应在主线程创建。后台线程可以生成 `QImage`、numpy bytes 或压缩图像数据，但不要创建 `QPixmap`。

### 实施步骤

1. 新增结果 DTO：

```python
@dataclass(frozen=True)
class ThumbnailImageResult:
    path: str
    frame_index: int | None
    image: QImage | None
```

2. 修改 worker：
   - `ThumbnailRequest.run()` 返回 `QImage`。
   - `FrameThumbnailRequest.run()` 返回 `QImage`。
   - `ClipThumbnailWorker.run()` 返回 `list[tuple[int, QImage]]`。
   - 不在 worker 里调用 `QPixmap.fromImage()`。

3. 修改 `ThumbnailProvider` 主线程回调：
   - 在 `_on_thumbnail_loaded()` 里把 `QImage` 转为 `QPixmap`。
   - 在 `_on_frame_thumbnail_loaded()` 里把 `QImage` 转为 `QPixmap`。
   - 在 `_on_clip_thumbnails_loaded()` 里把每张 `QImage` 转为 `QPixmap`。

4. 注意事项：
   - `QImage` 构造时必须 copy 数据，避免 numpy buffer 释放后图像变脏。
   - 推荐写法：`q_img = QImage(...).copy()`。
   - 所有 signal 类型可以先保持 `object`，减少 PyQt 类型兼容风险。

### 验证方法

1. 单测：
   - 新增 `tests/media/test_thumbnail_provider_thread_safety.py`。
   - monkeypatch `QPixmap.fromImage`，记录调用线程。
   - 断言 worker 执行期间没有调用 `QPixmap.fromImage`。

2. 手动验证：
   - 导入 200 个视频 clip。
   - 进入 Color 页面。
   - 快速滚动片段面板。
   - 观察无崩溃、无随机空白、无 Qt warning。

---

## 4. P0：片段面板缩略图按可见区域懒加载

### 问题

`TimelineClipStrip._rebuild_cards()` 当前会为全部 entries 创建 `TimelineClipCard`，而每个 card 的构造函数都会立即 `_request_thumbnail()`。

如果时间线上有 500 个镜头，进入 Color 页面会立刻触发 500 次视频 seek/解码请求。这些请求走 `QThreadPool.globalInstance()`，会和播放、导入、AI 工具、其他后台任务抢线程和 CPU。

### 目标行为

1. 首屏只加载可见 card 的缩略图。
2. 额外预取左右各 5-10 张。
3. 滚动停止 80-150ms 后再触发新一批请求。
4. 播放中默认暂停新缩略图解码，只显示已有缓存和占位。
5. 超过阈值的大时间线默认降级：
   - 200 个 clip 以上：只加载可见区域。
   - 500 个 clip 以上：默认 `NO_PREVIEW`，用户右键可打开。

### 实施步骤

1. 修改 `TimelineClipCard`：
   - 构造函数不要自动 `_request_thumbnail()`。
   - 新增方法：

```python
def ensure_thumbnail_requested(self) -> None:
    if self._thumbnail_requested:
        return
    self._thumbnail_requested = True
    self._request_thumbnail()
```

2. 修改 `TimelineClipStrip`：
   - 监听 `self._scroll_area.horizontalScrollBar().valueChanged`。
   - 新增 `_thumbnail_debounce_timer: QTimer`，单次触发，间隔 120ms。
   - 新增 `_request_visible_thumbnails()`：
     - 根据 scroll value、viewport width、card width 计算 visible index range。
     - 预取 `start - 8` 到 `end + 8`。
     - 只对范围内 card 调用 `ensure_thumbnail_requested()`。

3. 在 `_rebuild_cards()` 之后：
   - `QTimer.singleShot(0, self._request_visible_thumbnails)`。
   - 不要立即请求所有 card。

4. 播放中限流：
   - 给 `TimelineClipStrip` 增加 `set_playback_active(active: bool)`。
   - 播放中 `_request_visible_thumbnails()` 只消费缓存，不发新请求。
   - MainWindow 在视频播放状态变化时调用该方法。

### 验证方法

1. 单测：
   - 新增 `tests/color/test_timeline_clip_strip_lazy_thumbnails.py`。
   - 构造 500 个 `ColorClipEntry`。
   - monkeypatch `TimelineClipCard.ensure_thumbnail_requested` 计数。
   - 初始化后断言请求数不超过 `visible_count + 16`。

2. 性能脚本：
   - 新增 `tests/benchmark/benchmark_timeline_clip_strip_thumbnails.py`。
   - 生成 100、500、1000 个假 entry。
   - 测量 `_rebuild_cards()` 时间和首次缩略图请求数量。

通过标准：

| 场景 | 目标 |
|---|---|
| 500 clips 进入 Color | 首次请求缩略图 <= 40 |
| 500 clips 首次构建 UI | < 150ms，不含实际视频解码 |
| 播放中滚动 strip | 不新增视频解码请求 |

---

## 5. P1：缩略图请求不要全局广播给每张 card

### 问题

当前每张 `TimelineClipCard` 都连接：

```python
provider.frame_thumbnail_ready.connect(self._on_frame_thumbnail_ready)
```

当一个缩略图完成时，所有 card 都收到 signal，再各自判断 path/frame。这在小时间线没问题，但 N 张 card、M 次缩略图完成时，会产生 N*M 次 Python 回调。

### 目标架构

只让 `TimelineClipStrip` 连接一次 provider signal，由 strip 维护 request key 到 card 的映射。

### 实施步骤

1. `TimelineClipCard` 不直接连接 `ThumbnailProvider`。
2. `TimelineClipStrip` 维护：

```python
self._pending_frame_cards: dict[tuple[str, int, int], list[TimelineClipCard]]
self._pending_image_cards: dict[str, list[TimelineClipCard]]
```

3. card 需要缩略图时，调用 strip 的请求方法：

```python
strip.request_thumbnail_for_card(card)
```

4. strip 收到 provider signal 后：
   - 根据 key 找对应 card。
   - 更新这些 card。
   - 清理 pending 映射。

### 验证方法

1. 构造 300 个 card。
2. emit 一次 `frame_thumbnail_ready`。
3. 断言只有对应 card 更新，非对应 card 的 handler 不运行。

---

## 6. P1：请求代际与取消，避免刷新后旧图写到新 card

### 问题

`TimelineClipStrip.refresh()` 和 `_rebuild_cards()` 会删除旧 card 并创建新 card。后台缩略图请求可能稍后完成。如果没有 generation token，旧请求可能写入新 card 或造成悬挂引用。

### 实施步骤

1. `TimelineClipStrip` 增加：

```python
self._thumbnail_generation: int = 0
```

2. 每次 `_rebuild_cards()`：

```python
self._thumbnail_generation += 1
```

3. 每个请求 key 带 generation：

```python
request_key = (generation, asset_path, frame_num, icon_size)
```

4. signal 回调中：
   - 如果 generation != current generation，直接丢弃。
   - 不访问已经 deleteLater 的 card。

### 验证方法

1. 发起请求后立刻 `refresh()`。
2. 手动 emit 旧 generation 的完成 signal。
3. 断言没有 card 被旧图更新，没有异常日志。

---

## 7. P1：缓存 key 与容量优化

### 问题

当前视频单帧缓存 key 主要是 `(path, frame_index, icon_size)`，问题是：

1. 文件被替换但路径不变时，缓存可能过期。
2. 同一素材在项目内有 `asset_id`，但没有统一进入 key。
3. `frame_cache = LRUCache(50)` 对长时间线太小。
4. 缓存没有按内存字节预算，只按数量。

### 实施步骤

1. 新增统一 key：

```python
@dataclass(frozen=True)
class FrameThumbnailKey:
    asset_id: str
    path: str
    mtime_ns: int
    frame_index: int
    target_height: int
```

2. `TimelineClipStrip` 请求时传 `asset_id`。
3. `ThumbnailProvider` 内部统一使用该 key。
4. 替换或扩展 `LRUCache`：
   - 支持 `max_items`。
   - 支持 `max_bytes`。
   - 估算 `QPixmap.width * height * 4`。

### 验证方法

1. 修改同路径文件 mtime，旧缓存不能命中。
2. 同一 asset 同一 frame 二次打开必须命中。
3. 缓存超过预算时能稳定淘汰旧图。

---

## 8. P1：ColorStack 完整恢复到所有调色 UI 面板

### 问题

`_load_color_stack_to_grading_panel()` 当前主要恢复基础 grading 参数，并把曲线/HSL/LUT 等写入 engine layer。这样渲染可能有数据，但 UI 面板不一定同步显示当前 clip 的曲线、HSL、LUT、soft clip 状态。

### 实施步骤

1. 新增 `ColorStackHydrator`：

路径建议：

```text
src/features/color/adapters/color_stack_hydrator.py
```

2. 职责：
   - `apply_stack_to_engine(stack, engine)`
   - `apply_stack_to_panels(stack, panels)`
   - `collect_stack_from_engine_and_panels(engine, panels)`

3. 覆盖面板：
   - `grading_panel`
   - `hsl_curves_panel`
   - `lut_panel`
   - `mixer_panel`
   - `soft_clip_panel`
   - 后续可加 qualifier/mask。

4. 改造 `_load_color_stack_to_grading_panel()`：
   - 不再在 controller 中手写每种参数。
   - 调用 hydrator。

### 验证方法

1. Clip A 设置 HSL 曲线。
2. Clip B 设置不同 LUT。
3. 在 Color 片段面板 A/B 来回点击。
4. UI 面板和画面都必须切换到对应参数。

---

## 9. P2：Color 片段面板与 Edit 时间线实时同步

### 问题

当前 `refresh_color_timeline_clip_strip()` 主要在进入 Color 页面时调用。若用户在 Edit 中增删/移动 clip 后切回 Color，一般能刷新；但如果未来支持浮动面板或跨页修改，仍需更明确的事件绑定。

### 实施步骤

1. 找到 Edit 时间线变更信号：
   - `timeline_changed`
   - `timeline_switched`
   - `project_changed`
   - clip added/moved/trimmed/deleted 后的统一 signal。

2. 在 MainWindow/DockLayout 中统一连接：

```python
edit_page.signal_hub.timeline_changed.connect(refresh_color_timeline_clip_strip)
edit_page.signal_hub.timeline_switched.connect(refresh_color_timeline_clip_strip)
media_pool_panel.project_changed.connect(refresh_color_timeline_clip_strip)
```

3. 刷新需要 debounce：
   - 新增 `_color_clip_strip_refresh_timer`。
   - 50-100ms 合并多次 timeline_changed。

### 验证方法

1. 在 Edit 加入 3 个 clip，进 Color 能看到 3 个。
2. 回 Edit 删除 1 个，进 Color 只看到 2 个。
3. 回 Edit 移动 clip 顺序，Color 面板排序同步。

---

## 10. P2：逐步移除 `PROJECT_ASSET` 视频调色路径

### 问题

产品规则已经变成：

1. 视频必须进入 Edit 时间线才能 Color 调色。
2. 单独打开图片仍可进入 Color。
3. 时间线里的图片 clip 也走 `TIMELINE_CLIP`。

但 `SourceKind.PROJECT_ASSET` 仍然存在，且 `ColorSession.from_page_context()` 仍会在有 `asset_id`、无 `clip_id` 时创建 `PROJECT_ASSET`。

### 实施建议

第一阶段不一定删除枚举，但要加硬防线：

1. `from_page_context()` 如果 asset 是视频但没有 clip_id，必须拒绝。
2. Media Pool 右键不要提供 “Open Asset Grade in Color” 给视频。
3. `PROJECT_ASSET` 只允许未来非时间线图片资产，或直接废弃。

### 验证方法

1. Media Pool 视频不能直接进入 Color。
2. Media Pool 图片可按产品决定是否允许单图 Color。
3. 时间线图片/video clip 都能进入 Color。

---

## 11. P0：`.topos` 工程结构同步升级为 Timeline Clip Grading

### 背景

Color 与 Edit 的新产品规则已经改变：

1. 视频不能作为单素材在 Color 页面独立调色。
2. 视频必须先进入 Edit 时间线，成为 `ClipInstance`。
3. Color 页面调色视频时，只能通过 `timeline_id + clip_id` 找到 `clip.color_stack_id`。
4. 图片可以继续作为独立图片打开 Color，也可以作为时间线图片 clip 被 Color 访问。

因此 `.topos` 工程文件也必须同步收口。否则项目里会同时存在两套互相冲突的保存语义：

1. 旧语义：`single_asset` 可以表示单张图片或单个视频。
2. 新语义：独立 Color 工程只允许图片，视频调色只能挂在 Edit 时间线 clip 上。

项目仍在开发期，不需要兼容旧项目，所以本次升级可以直接做破坏性清理。

### 目标工程语义

新 `.topos` 只保留两类有效 Color 入口：

| 入口 | 适用素材 | 是否需要 Edit 时间线 | 保存位置 |
|---|---|---:|---|
| `standalone_image` | 单张图片 | 否 | 单图 `.topos` 的 `color/color_stacks.json` |
| `timeline_clip` | 视频 clip、时间线图片 clip | 是 | EditProject `.topos` 的 `color/color_stacks.json`，由 `clip.color_stack_id` 引用 |

不再支持：

| 旧入口 | 处理方式 |
|---|---|
| 单视频 `.topos` 调色工程 | 删除/拒绝 |
| `single_asset` 表示视频 | 改为非法 |
| `PROJECT_ASSET` 视频调色 | 删除或加硬防线拒绝 |
| `primary_asset_id + primary_color_stack_id` 指向视频 | 不再写入，不再作为 Color 视频入口 |

### 新 `.topos` 推荐结构

#### A. Standalone Image `.topos`

用于单张图片调色。这个工程没有 Edit 时间线。

```text
image.topos
├── manifest.json
├── project.json
├── color/
│   └── color_stacks.json
└── ui/
    └── workspace_state.json
```

`manifest.json`：

```json
{
  "format": "topos_project_archive",
  "schema_version": 2,
  "project_mode": "standalone_image",
  "features": ["color"],
  "primary_asset_id": "asset-image-001"
}
```

`project.json` 必须满足：

```json
{
  "project_mode": "standalone_image",
  "primary_asset_id": "asset-image-001",
  "primary_color_stack_id": "stack-image-001",
  "assets": {
    "asset-image-001": {
      "media_type": "image"
    }
  },
  "timelines": {},
  "active_timeline_id": null
}
```

硬规则：

1. `project_mode == "standalone_image"` 时，`primary_asset_id` 对应 asset 的 `media_type` 必须是 `image`。
2. `project_mode == "standalone_image"` 时，不能保存 `media_type == "video"` 的 primary asset。
3. `standalone_image` 不需要 `timeline_sequences.json`。

#### B. Edit Project `.topos`

用于剪辑、时间线调色、导出。

```text
edit_project.topos
├── manifest.json
├── project.json
├── edit/
│   ├── media_pool.json
│   ├── bins.json
│   ├── timelines.json
│   └── timeline_sequences.json
├── color/
│   └── color_stacks.json
├── composite/
│   ├── graphs.json
│   └── viewer_state.json
└── ui/
    └── workspace_state.json
```

`manifest.json`：

```json
{
  "format": "topos_project_archive",
  "schema_version": 2,
  "project_mode": "edit_project",
  "features": ["edit", "color", "deliver"]
}
```

`project.json`：

```json
{
  "project_mode": "edit_project",
  "primary_asset_id": null,
  "primary_color_stack_id": null,
  "active_timeline_id": "timeline-001",
  "assets": {
    "asset-video-001": {
      "media_type": "video"
    }
  },
  "color_stacks": {
    "stack-clip-001": {
      "scope": "clip_local",
      "asset_id": "asset-video-001"
    }
  }
}
```

`edit/timeline_sequences.json` 中的 clip 必须引用调色栈：

```json
{
  "timeline-001": {
    "video_tracks": [
      {
        "clips": [
          {
            "clip_id": "clip-001",
            "asset_id": "asset-video-001",
            "color_stack_id": "stack-clip-001",
            "color_shared": false
          }
        ]
      }
    ]
  }
}
```

`color/color_stacks.json`：

```json
{
  "stack-clip-001": {
    "stack_id": "stack-clip-001",
    "asset_id": "asset-video-001",
    "name": "Clip clip-001",
    "scope": "clip_local",
    "timeline_id": "timeline-001",
    "clip_id": "clip-001",
    "grading_params": {},
    "curves_data": {},
    "hsl_curve_data": {},
    "mixer_params": {},
    "soft_clip_params": {},
    "lut_path": null,
    "lut_intensity": 1.0
  }
}
```

建议给 `ColorStackData` 增加字段：

```python
scope: str = "clip_local"  # standalone_image, clip_local, asset_shared, timeline_adjustment
timeline_id: str | None = None
clip_id: str | None = None
source_kind: str = "timeline_clip"  # standalone_image or timeline_clip
```

这样保存文件自身就能表达调色栈属于谁，不必只靠 `ClipInstance.color_stack_id` 反查。

### 需要修改的文件

| 文件 | 修改内容 |
|---|---|
| `src/shared/project/manifest.py` | `ProjectMode` 去掉“单个视频调色工程”语义，新增/改名 `standalone_image` |
| `src/shared/project/topos_project.py` | `ProjectMode` 同步；`create_single_asset()` 改为 `create_standalone_image()`；视频直接拒绝 |
| `src/shared/project/color_stack.py` | 给 `ColorStackData` 增加 `scope/timeline_id/clip_id/source_kind` |
| `src/features/edit/core/project.py` | 同步 `ColorStackData` 字段，避免 edit 内部模型和 shared 模型分叉 |
| `src/features/edit/core/edit_project_io.py` | `convert_edit_project_to_topos_project()` 正确写入 `ColorStackRef.scope` |
| `src/features/edit/core/serialization.py` | 序列化/反序列化新字段 |
| `src/shared/project/topos_project_io.py` | 保存时 schema_version 写 2；校验 standalone image 不含视频 |
| `src/shared/project/save_router.py` | Color 保存按 `ColorSession.source_kind` 强制分流 |
| `src/app/controllers/file_controller.py` | 单独打开视频不能创建 standalone 工程 |
| `src/app/controllers/page_controller.py` | `timeline_clip` session 必须带 `timeline_id/clip_id/source_frame` |
| `tests/project/test_project_document.py` | 更新单素材工程测试为 standalone image |
| `tests/project/test_project_module_preservation.py` | 更新 manifest/schema 预期 |
| `tests/project/test_save_router.py` | 增加视频 timeline clip 保存断言 |

### 具体实施步骤

#### Step 1：定义 schema v2 常量

新增或修改：

```python
TOPOS_SCHEMA_VERSION = 2
PROJECT_MODE_STANDALONE_IMAGE = "standalone_image"
PROJECT_MODE_EDIT_PROJECT = "edit_project"
```

推荐位置：

```text
src/shared/project/schema_v1_spec.py
```

如果文件名继续叫 `schema_v1_spec.py` 不合适，可以新增：

```text
src/shared/project/schema.py
```

第一阶段为了少改动，也可以保留旧文件名，但常量要清晰。

#### Step 2：收口 ProjectMode

修改 `src/shared/project/manifest.py` 和 `src/shared/project/topos_project.py`：

```python
ProjectMode = Literal[
    "standalone_image",
    "edit_project",
    "composite_shot",
    "mixed_project",
]
```

删除注释中的“单个视频调色工程”。

`ToposManifest.__post_init__()` 和 `ToposProject.__post_init__()` 的 valid modes 同步改为：

```python
valid_modes = ("standalone_image", "edit_project", "composite_shot", "mixed_project")
```

#### Step 3：替换 `create_single_asset`

在 `ToposProject` 中新增：

```python
@staticmethod
def create_standalone_image(name: str, image_path: str) -> ToposProject:
    ...
```

规则：

1. 只允许图片扩展名，或显式传入 `media_type="image"`。
2. 生成 `primary_asset_id`。
3. 生成 `primary_color_stack_id`。
4. `project_mode="standalone_image"`。
5. `features=("color",)`。

旧的 `create_single_asset()`：

1. 如果调用方很多，先保留 alias。
2. alias 内部必须检查视频并拒绝。
3. 文档标记 deprecated。

建议实现：

```python
@staticmethod
def create_single_asset(name: str, asset_path: str, media_type: str = "image") -> ToposProject:
    if media_type != "image":
        raise ValueError("single asset projects now support standalone images only")
    return ToposProject.create_standalone_image(name, asset_path)
```

#### Step 4：升级 ColorStackData

`src/shared/project/color_stack.py` 和 `src/features/edit/core/project.py` 中都有 `ColorStackData`，必须同步。

新增字段：

```python
scope: str = "clip_local"
source_kind: str = "timeline_clip"
timeline_id: str | None = None
clip_id: str | None = None
```

合法值：

```python
scope in ("standalone_image", "clip_local", "asset_shared", "timeline_adjustment")
source_kind in ("standalone_image", "timeline_clip")
```

工厂方法：

```python
@staticmethod
def create_standalone_image(asset_id: str, name: str = "Image Grade") -> ColorStackData

@staticmethod
def create_timeline_clip(
    asset_id: str,
    timeline_id: str,
    clip_id: str,
    name: str = "Clip Grade",
) -> ColorStackData
```

`to_dict()` 必须写出新字段。

`from_dict()` 对开发期可以直接要求字段存在；如果想让测试更顺滑，可给默认值：

```python
source_kind=data.get("source_kind", "timeline_clip" if data.get("clip_id") else "standalone_image")
```

#### Step 5：EditProject 创建 clip 调色栈时写入 timeline/clip 归属

当前 `_ensure_clip_has_color_stack()` 只传 `asset_id` 和 name。

需要改为：

```python
ColorStackData.create_timeline_clip(
    asset_id=asset_id,
    timeline_id=timeline.timeline_id,
    clip_id=clip_id,
    name=f"Clip_{clip_id[:8]}",
)
```

如果当前 command `CreateColorStackCommand.create()` 还不支持 `timeline_id/clip_id/scope/source_kind`：

1. 扩展 command 参数。
2. command apply 时创建完整 `ColorStackData`。
3. 测试覆盖 command 序列化/反序列化。

#### Step 6：ToposProject 转换时写正确 scope

`src/features/edit/core/edit_project_io.py::convert_edit_project_to_topos_project()` 当前写死：

```python
scope="asset_default"
```

这是新规则下的隐藏 bug。必须改为：

```python
scope=stack.scope
```

如果 stack 没有 scope：

1. 有 `clip_id` 或 `timeline_id`：`clip_local`。
2. 无 clip 但 standalone image：`standalone_image`。
3. 其他情况才允许 `asset_shared`。

#### Step 7：SaveRouter 强化保存目标

`SaveRouter._save_from_color()` 必须明确：

1. `SourceKind.SINGLE_ASSET` 只允许图片，并保存为 standalone image `.topos`。
2. `SourceKind.TIMELINE_CLIP` 必须有：
   - `project_id`
   - `timeline_id`
   - `clip_id`
   - `asset_id`
   - `color_stack_id`
3. 如果当前 session 是 timeline video，却找不到 EditProject，保存失败，不允许 fallback 到单素材路径。
4. 如果当前 session 是 standalone image，却发现 `current_file_is_video=True`，保存失败。

建议错误信息要直白：

```text
Video color grading must be saved through an Edit timeline clip.
```

#### Step 8：读取时拒绝不合法工程

开发期不兼容旧项目，所以读取时可以直接拒绝：

1. `project_mode == "single_asset"` 且 primary asset 是 video：拒绝。
2. `project_mode == "standalone_image"` 但 primary asset 不是 image：拒绝。
3. `project_mode == "edit_project"` 中 video clip 没有 `color_stack_id`：允许打开，但进入 Color 时必须自动创建。
4. `color/color_stacks.json` 中 `source_kind == "timeline_clip"` 但缺 `timeline_id/clip_id`：拒绝或标记损坏。

实现位置：

```text
src/shared/project/topos_project_io.py
src/features/edit/core/edit_project_io.py
src/shared/project/topos_project_read_service.py
```

#### Step 9：删除旧单视频读取路径

`src/app/controllers/file_controller.py::_load_video_project()` 是旧单视频调色工程逻辑。

处理方式：

1. 不一定马上删除函数。
2. 但不能从 Color 页面单独打开视频触发。
3. 不能在 `for_timeline_clip=True` 时触发。
4. 后续可移动到 legacy/unused 并最终删除。

推荐新增测试证明：

```python
def test_timeline_clip_video_load_does_not_load_single_video_topos()
```

断言 `load_video(path, for_timeline_clip=True)` 不调用 `_load_video_project()`。

#### Step 10：工程文件写入验收样例

新增测试生成一个真实 `.topos` ZIP：

```text
tests/project/test_topos_schema_v2_timeline_clip_grading.py
```

测试流程：

1. 创建 EditProject。
2. 导入一个 video asset。
3. 创建 timeline。
4. 添加一个 video clip。
5. 给 clip 创建 color stack。
6. 保存 `.topos`。
7. 打开 ZIP 检查：
   - `manifest.json.schema_version == 2`
   - `manifest.json.project_mode == "edit_project"`
   - `project.json.primary_asset_id is None`
   - `project.json.primary_color_stack_id is None`
   - `edit/timeline_sequences.json` 中 clip 有 `color_stack_id`
   - `color/color_stacks.json[stack_id].source_kind == "timeline_clip"`
   - `color/color_stacks.json[stack_id].timeline_id == timeline_id`
   - `color/color_stacks.json[stack_id].clip_id == clip_id`

再新增 standalone image 测试：

1. 创建 standalone image project。
2. 保存 `.topos`。
3. 检查：
   - `project_mode == "standalone_image"`
   - primary asset 是 image。
   - 没有 `edit/timeline_sequences.json`，或该模块为空。

再新增拒绝测试：

1. 调用 `ToposProject.create_standalone_image()` 传视频路径，必须 `ValueError`。
2. 构造 `standalone_image` + `media_type=video`，保存或 validate 必须失败。
3. Color 页面没有 timeline context 时，视频保存必须失败。

### 验证命令

```bash
.venv/bin/python -m py_compile \
  src/shared/project/manifest.py \
  src/shared/project/topos_project.py \
  src/shared/project/color_stack.py \
  src/features/edit/core/project.py \
  src/features/edit/core/edit_project_io.py \
  src/features/edit/core/serialization.py \
  src/shared/project/topos_project_io.py \
  src/shared/project/save_router.py

.venv/bin/python -m pytest \
  tests/project/test_project_document.py \
  tests/project/test_project_module_preservation.py \
  tests/project/test_save_router.py \
  tests/color/test_color_session.py \
  tests/architecture/test_page_context.py \
  -q
```

新增测试后再跑：

```bash
.venv/bin/python -m pytest \
  tests/project/test_topos_schema_v2_timeline_clip_grading.py \
  tests/project/test_save_router.py \
  tests/edit/test_clip_timeline_color_frame_mapping.py \
  -q
```

### 完成标准

1. 代码中不再出现“single asset can be image or video”的注释或产品语义。
2. 单独打开视频不会创建或保存 standalone `.topos`。
3. `.topos` schema v2 的 EditProject 中，视频调色只能通过 timeline clip 保存。
4. `ColorSession.TIMELINE_CLIP` 永远有 `timeline_id + clip_id`。
5. `ColorStackData` 可以自描述自己的归属：standalone image 或 timeline clip。
6. 保存、读取、SaveRouter、EditProjectIO 的测试全部覆盖新规则。

---

## 12. 推荐开发顺序

1. 先做 P0 `.topos` schema v2 清理，保证保存语义先稳定。
2. 再做 P0 线程安全：worker 返回 `QImage`，主线程创建 `QPixmap`。
3. 再做 P0 懒加载：只请求可见区域缩略图。
4. 再做 P1 单一 signal 管理：避免每张 card 连接全局 signal。
5. 再做 P1 generation token：刷新后旧请求不能污染新 UI。
6. 再做 P1 缓存 key/容量。
7. 最后做 ColorStack Hydrator 和实时同步。

---

## 13. 给 AI 的第一条实施 Prompt

```text
请按 docs/color_edit_timeline_clip_strip_followup_optimization_plan_2026-05-31.md 的第 11 章执行。

范围：
1. src/shared/project/manifest.py
2. src/shared/project/topos_project.py
3. src/shared/project/color_stack.py
4. src/features/edit/core/project.py
5. src/features/edit/core/edit_project_io.py
6. src/features/edit/core/serialization.py
7. src/shared/project/topos_project_io.py
8. src/shared/project/save_router.py
9. 相关 tests/project、tests/color、tests/architecture 测试

任务：
把 .topos 工程结构升级为 schema v2：
- 独立 Color 工程只允许 standalone image。
- 视频调色只能通过 Edit timeline clip。
- TIMELINE_CLIP 必须保存 timeline_id、clip_id、asset_id、color_stack_id。
- ColorStackData 要能自描述归属：standalone_image 或 timeline_clip。

约束：
- 项目仍在开发期，不需要兼容旧单视频 .topos。
- PyQt6 + ModernGL + GLSL 项目。
- 不要在 UI 线程调用 OpenGL。
- 所有新增函数写类型注解。
- 不做无关重构。
- 修改后运行 py_compile 和相关 pytest。

输出：
1. 修改文件列表。
2. 新增/修改测试。
3. 验证命令与结果。
```

## 14. 给 AI 的第二条实施 Prompt

```text
请按 docs/color_edit_timeline_clip_strip_followup_optimization_plan_2026-05-31.md 的 P0 缩略图线程安全执行。

范围：
1. src/shared/media/thumbnail_provider.py
2. src/features/color/ui/timeline_clip_strip.py
3. 必要测试文件

任务：
先修复缩略图线程安全问题：后台 QRunnable 只能生成 QImage 或 bytes，禁止创建 QPixmap；QPixmap 必须在主线程 Provider 回调里创建。

约束：
- PyQt6 + ModernGL + GLSL 项目。
- 不要在 UI 线程调用 OpenGL。
- 所有新增函数写类型注解。
- 不做无关重构。
- 修改后运行 py_compile 和相关 pytest。

输出：
1. 修改文件列表。
2. 新增/修改测试。
3. 验证命令与结果。
```
