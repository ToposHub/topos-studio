# Color 与 Edit 时间线打通实施方案：视频只通过时间线调色

日期：2026-05-31  
适用项目：Topos Color  
技术栈：PyQt6 + ModernGL + GLSL  
目标读者：后续执行开发任务的 AI / 工程师  

---

## 0. 最终产品规则

本方案不考虑旧项目兼容。允许破坏旧的 Color 视频单素材工作流，只保留新的清晰规则。

最终规则如下：

1. **视频文件不能在 Color 页面单独打开调色。**
   - 视频必须先进入 Edit 页面。
   - 视频必须被放到当前活动时间线中，成为 `ClipInstance`。
   - Color 页面只能访问时间线上的视频 clip。

2. **图片文件可以在 Color 页面单独打开调色。**
   - 图片属于独立照片调色工作流。
   - 图片保留原始像素尺寸，不强制进入统一时间线尺寸。
   - 图片单独打开时使用 Standalone Image 模式。

3. **图片如果被放到 Edit 时间线中，也可以作为时间线 clip 调色。**
   - 此时调色目标不是原图文件本身，而是时间线上的 `ClipInstance`。
   - 保存目标是该 clip 的 `color_stack_id`。

4. **Color 页面只有两种有效工作模式。**
   - `STANDALONE_IMAGE`：单图调色。
   - `TIMELINE_CLIP`：时间线 clip 调色，支持视频 clip 和图片 clip。

5. **Edit 时间线是视频调色的唯一入口和真相源。**
   - 所有视频调色必须绑定 `timeline_id + clip_id + asset_id + color_stack_id`。
   - Color 页面不创建视频时间线。
   - Color 页面不直接创建视频 `single_asset` session。

---

## 1. 当前代码现状与问题

### 1.1 已有可利用基础

已有数据模型支持部分 Edit → Color 联动：

- `src/features/edit/core/timeline_v2.py`
  - `ClipInstance.color_stack_id`
  - `ClipInstance.color_shared`
  - `ClipInstance.with_color_stack(...)`

- `src/features/color/core/color_session.py`
  - `SourceKind.SINGLE_ASSET`
  - `SourceKind.PROJECT_ASSET`
  - `SourceKind.TIMELINE_CLIP`
  - `ColorSession.create_timeline_clip(...)`

- `src/shared/project/save_router.py`
  - Color 保存已经能按 `ColorSession` 写入 `color_stacks`。
  - 注释里已经区分 `single_asset` 与 `timeline_clip`。

- `src/app/controllers/page_controller.py`
  - `_handle_enter_color_with_context(...)`
  - `switch_from_edit_to_color(...)`
  - `_load_color_stack_to_grading_panel(...)`
  - `_setup_grading_save_callback(...)`

- `src/app/controllers/edit_controller.py`
  - `_ensure_clip_has_color_stack(...)`
  - `_on_grading_changed_for_color_stack(...)`

### 1.2 必须修复的架构问题

当前代码中有几条路径会破坏“视频只通过时间线调色”的原则：

1. `src/app/controllers/file_controller.py::load_video(...)`
   - 当前会创建单视频 `ColorSession`：
     - `_create_single_asset_color_session(path, frame=0)`
   - 这必须改掉。
   - `load_video()` 只能作为底层加载当前 clip 源媒体的函数，不能再自动声明“当前调色目标是单视频素材”。

2. `src/app/controllers/page_controller.py::_create_single_asset_color_session(...)`
   - 当前支持任意文件，包括视频。
   - 改造后只能用于图片。
   - 函数名建议改为 `_create_standalone_image_color_session(...)`，或者保留函数名但内部强校验图片。

3. `src/shared/video/color_frame_provider_adapter.py`
   - 注释明确写着：
     - Color 直接请求源帧。
     - 不做 timeline offset/speed 转换。
   - 新架构下，Color 页面可以继续向视频解码器请求“源帧”，但源帧号必须由 `ClipInstance` 的时间映射计算出来。
   - 也就是说：解码层仍取源帧，Color 控制层必须负责 `timeline_frame -> source_frame`。

4. `src/app/main_window.py::PageContext`
   - 当前只有一个模糊的 `frame` 字段。
   - 不清楚它是 timeline frame，source frame，还是 clip-local frame。
   - 新架构必须拆成明确字段：
     - `timeline_frame`
     - `source_frame`
     - `clip_local_frame`

5. `src/app/controllers/edit_controller.py::_on_timeline_selection_changed(...)`
   - 当前单选 clip 后会 emit `clip_selected`，再触发 `_on_clip_selected_for_color(...)`，可能自动切到 Color。
   - 新架构下不能“点一下剪辑就跳调色页”。
   - 应改为：
     - Edit 页面只更新属性面板。
     - 用户显式点击“Open in Color”或切换到 Color 页后，由 Color 页片段面板选择 clip。

6. `src/features/edit/ui/timeline_items.py::open_in_color_requested`
   - ClipItem 有右键菜单信号，但在 `TimelineView._create_clip_item(...)` 里没有完整接线。
   - 必须接到 `TimelineView`，再上抛到 `EditPage.signal_hub` 或 MainWindow controller。

7. Color 页面目前缺少“时间线镜头片段面板”。
   - 用户无法在 Color 页面看到当前时间线上的每一个镜头。
   - 这会导致 Color 页无法像达芬奇一样按镜头工作。

---

## 2. 目标架构

### 2.1 页面职责

Edit 页面：

- 创建项目。
- 导入视频/图片/音频素材。
- 创建时间线。
- 把素材放到时间线。
- 维护真实 `TimelineSequence`。
- 维护 `ClipInstance` 的剪辑状态。
- 为 clip 创建或绑定 `color_stack_id`。

Color 页面：

- 显示当前活动时间线上的可调色 clip 列表。
- 点击 clip 后进入 `TIMELINE_CLIP` 调色模式。
- 加载 clip 对应素材。
- 按 clip 的时间映射定位源帧。
- 读取并修改该 clip 的 `color_stack_id` 对应调色栈。
- 单独打开图片时进入 `STANDALONE_IMAGE` 模式。
- 禁止直接打开视频作为独立调色对象。

SaveRouter：

- `STANDALONE_IMAGE`：保存到单图工程包。
- `TIMELINE_CLIP`：保存到 EditProject 的 `color_stacks[clip.color_stack_id]`。

### 2.2 推荐数据流

```text
EditProject
  └── active_timeline_id
      └── TimelineSequence
          └── video/image ClipInstance
              ├── clip_id
              ├── asset_id
              ├── timeline_start / timeline_duration
              ├── source_start / source_duration
              ├── speed
              └── color_stack_id

Color 页面
  └── TimelineClipStrip
      └── 用户点击 clip
          └── MainWindow.open_timeline_clip_in_color(timeline_id, clip_id)
              ├── ensure clip has color_stack_id
              ├── create ColorSession(TIMELINE_CLIP)
              ├── load source media for preview
              ├── load ColorStackData to grading panels
              └── seek source_frame computed from clip mapping
```

### 2.3 Color 页面模式状态

新增一个明确的页面模式枚举，建议放到：

- `src/features/color/core/color_session.py`

推荐实现：

```python
class ColorWorkMode(Enum):
    EMPTY = "empty"
    STANDALONE_IMAGE = "standalone_image"
    TIMELINE_CLIP = "timeline_clip"
```

也可以复用 `SourceKind`，但必须做以下语义收口：

- `SourceKind.SINGLE_ASSET` 只允许图片。
- `SourceKind.PROJECT_ASSET` 不再用于新流程。
- `SourceKind.TIMELINE_CLIP` 用于视频 clip 和时间线图片 clip。

为了减少一次性改动风险，建议第一阶段保留 `SourceKind`，但增加严格判断：

```python
def is_standalone_image(self) -> bool:
    return self.source_kind == SourceKind.SINGLE_ASSET

def is_timeline_clip(self) -> bool:
    return self.source_kind == SourceKind.TIMELINE_CLIP
```

并在 controller 中确保 `SINGLE_ASSET` 不会用于视频。

---

## 3. 新增 UI：Color 页时间线片段面板

### 3.1 新文件

新增：

- `src/features/color/ui/timeline_clip_strip.py`

该文件只负责 UI 展示，不直接修改项目数据。

### 3.2 类设计

建议实现两个类：

```python
class TimelineClipCard(QFrame):
    clicked = pyqtSignal(str, str)  # timeline_id, clip_id
```

职责：

- 显示单个 clip。
- 显示素材名。
- 显示简略时码或帧范围。
- 显示是否已有调色栈。
- 显示选中状态。
- 不直接访问 MainWindow。

```python
class TimelineClipStrip(QWidget):
    clip_activated = pyqtSignal(str, str)  # timeline_id, clip_id
```

职责：

- 接收当前 `EditProject` 和 `TimelineSequence`。
- 列出所有可调色 clip。
- 横向滚动显示 clip cards。
- 点击 card 后发出 `clip_activated`。
- 支持 `set_active_clip(clip_id)` 高亮当前 clip。
- 支持 `refresh()`。

### 3.3 面板显示内容

每个 card 建议显示：

- 缩略图占位区。
- 素材名。
- `V1` / `V2` 或轨道名。
- 时间线范围：`timeline_start - timeline_end`。
- 源范围：`source_start - source_end`。
- 调色状态：
  - `No Grade`
  - `Local Grade`
  - `Shared Grade`

第一版可以不做真实缩略图，只用色块和素材名。功能打通后再接 thumbnail。

### 3.4 可调色 clip 过滤规则

只显示：

- 视频 clip。
- 图片 clip。

不显示：

- 纯音频 clip。
- 字幕 clip。
- disabled clip 可显示但置灰，第一版也可以直接隐藏。

判断来源：

- 从 `TimelineSequence.all_tracks` 遍历轨道。
- 只取 `track.track_type == "video"` 的轨道。
- 再通过 `asset_id` 查 `EditProject.assets[asset_id]`。
- asset 为 video 或 image 才展示。

### 3.5 排序规则

按时间线视觉顺序排序：

1. `timeline_start` 升序。
2. 同一帧上多轨 clip：
   - 上方视频轨优先。
   - 如果当前模型没有明确上下顺序，就按 `TimelineSequence.all_tracks` 顺序。

实现时可以建立内部 DTO：

```python
@dataclass(frozen=True)
class ColorClipEntry:
    timeline_id: str
    clip_id: str
    track_id: str
    track_name: str
    asset_id: str
    asset_name: str
    asset_path: str
    media_type: str
    timeline_start: int
    timeline_duration: int
    source_start: int
    source_duration: int
    color_stack_id: str | None
    color_shared: bool
```

DTO 可放在 `timeline_clip_strip.py` 内部，不要一开始放到共享模型里。

---

## 4. Color 页接入片段面板

### 4.1 找到 Color 页面 dock / layout 注册点

相关文件：

- `src/features/color/module.py`
- `src/app/controllers/dock_layout_controller.py`
- `src/app/main_window.py`

当前 Color 面板多数通过 dock 注册。新增 panel descriptor：

```python
panel_id="color.timeline_clips"
title_key="panel.timeline_clips"
fallback_title="Timeline Clips"
feature_id="color"
category="Color"
page_ids=("Color",)
dock_attr="dock_color_timeline_clips"
order=50
default_visible=True
```

建议位置：

- Color 页面底部。
- 类似达芬奇 Color 页的 clip strip。
- 如果 dock 系统短期不好接，第一版可以创建成底部 dock，默认显示。

### 4.2 MainWindow 创建实例

在 MainWindow 初始化 UI 的位置创建：

```python
from src.features.color.ui.timeline_clip_strip import TimelineClipStrip

self.color_timeline_clip_strip = TimelineClipStrip()
self.color_timeline_clip_strip.clip_activated.connect(
    self.open_timeline_clip_in_color
)
```

如果项目已有 panel registry 自动创建机制，则按现有 dock 方式创建。

### 4.3 提供刷新方法

在 controller 中新增方法，建议放到：

- `src/app/controllers/page_controller.py`

```python
def refresh_color_timeline_clip_strip(self) -> None:
    project = self._current_edit_project
    sequence = None
    if project and project.active_timeline_id:
        sequence = self._current_timeline_sequences.get(project.active_timeline_id)
    self.color_timeline_clip_strip.set_project_and_timeline(project, sequence)
```

调用时机：

- 切换到 Color 页时。
- Edit 时间线变化时。
- active timeline 切换时。
- clip 的 `color_stack_id` 改变时。
- 导入/删除素材后。

已有可接入点：

- `src/app/controllers/page_controller.py::switch_to_page(...)`
- `src/app/controllers/edit_controller.py::_on_edit_timeline_changed(...)`
- `src/app/controllers/file_controller.py::_on_media_pool_project_changed(...)`

### 4.4 Color 页进入时的行为

当进入 Color 页时：

1. 刷新片段面板。
2. 如果当前有 active timeline：
   - 如果已有当前 `ColorSession` 且是该 timeline 的 clip，保持当前选中。
   - 否则默认选中播放头所在的第一个可调色 clip。
   - 如果播放头没有落在任何 clip 上，则选中第一个可调色 clip。
3. 如果没有 active timeline：
   - 显示空状态：“No timeline clips”。
   - 不允许打开视频。
4. 如果当前是 standalone image session：
   - 允许继续显示图片调色。
   - clip strip 显示当前 Edit timeline 的 clips，但不强制切换。

---

## 5. 重构 PageContext 与 ColorSession

### 5.1 修改 PageContext

文件：

- `src/app/main_window.py`

当前字段：

```python
frame: int | None = None
```

建议替换为：

```python
timeline_frame: int | None = None
source_frame: int | None = None
clip_local_frame: int | None = None
```

保留 `frame` 会继续制造语义混乱。因为无需兼容旧项目，建议直接删除 `frame`。

更新：

- `PageContext.to_dict()`
- `PageContext.from_dict()`
- `PageContextManager.create_context_for_edit_to_color(...)`

新签名：

```python
def create_context_for_edit_to_color(
    self,
    timeline_id: str,
    clip_id: str,
    asset_id: str,
    timeline_frame: int | None = None,
    source_frame: int | None = None,
    clip_local_frame: int | None = None,
    color_stack_id: str | None = None,
) -> PageContext:
```

### 5.2 修改 ColorSession

文件：

- `src/features/color/core/color_session.py`

新增字段：

```python
timeline_frame: int | None = None
source_frame: int | None = None
clip_local_frame: int | None = None
```

删除或停止使用：

```python
frame: int | None = None
```

如果暂时不删除，为了降低改动面，可以保留但标记不使用：

```python
# Deprecated. Use source_frame/timeline_frame instead.
frame: int | None = None
```

但后续 AI 执行时必须把所有新代码都使用明确字段。

### 5.3 TIMELINE_CLIP 校验

`ColorSession.__post_init__` 中：

```python
elif self.source_kind == SourceKind.TIMELINE_CLIP:
    if not self.asset_id:
        raise ValueError("asset_id required for TIMELINE_CLIP")
    if not self.timeline_id:
        raise ValueError("timeline_id required for TIMELINE_CLIP")
    if not self.clip_id:
        raise ValueError("clip_id required for TIMELINE_CLIP")
```

当前 `timeline_id` 不是强制字段，必须变成强制。

### 5.4 SINGLE_ASSET 只允许图片

`ColorSession` 本身不知道文件类型，不要在 dataclass 里查文件。  
在 controller 创建 session 前做校验：

- `load_image(...)` 可以创建 standalone image session。
- `load_video(...)` 不可以创建 standalone session。

---

## 6. 视频打开路径改造

### 6.1 禁止 Color 直接打开视频

文件：

- `src/app/controllers/file_controller.py`
- `src/app/controllers/menu_toolbar_controller.py`

当前多处会在用户打开文件时：

```python
if VideoDecoder.is_video_file(filepath):
    self.load_video(filepath)
else:
    self.load_image(filepath)
```

新规则：

- 如果当前用户从 Library/File/Open 在 Color 上下文打开视频：
  - 不调用 `load_video(filepath)`。
  - 弹出提示：
    - “视频调色必须先放入剪辑时间线。请在 Edit 页面创建时间线并添加该视频。”
  - 可选：提供“导入到 Edit 并创建默认时间线”的按钮，第一版可以不做。

实现建议：

新增：

```python
def _reject_standalone_video_for_color(self, path: str) -> None:
    QMessageBox.information(
        self,
        I18N.tr("color.video_requires_timeline.title", "Video grading uses the timeline"),
        I18N.tr(
            "color.video_requires_timeline.message",
            "Video files must be added to an Edit timeline before they can be graded in Color."
        ),
    )
```

然后在打开文件入口中：

```python
if VideoDecoder.is_video_file(filepath):
    if self.current_page == "Color" or self.current_page == "Library":
        self._reject_standalone_video_for_color(filepath)
        return
```

注意：如果是 `open_timeline_clip_in_color(...)` 内部为了加载 clip 源视频调用底层视频加载，不能被这个拒绝逻辑拦住。  
所以要区分：

- 用户入口：拒绝 standalone video。
- 内部 clip 调色入口：允许加载源视频作为当前 clip 的 preview source。

### 6.2 拆分 load_video 的职责

当前：

```python
def load_video(self, path: str):
    ...
    self._create_single_asset_color_session(path, frame=0)
    if self.video_controller.load_video(path):
        ...
```

新结构建议：

```python
def load_video_source_for_timeline_clip(self, path: str) -> bool:
    """Only load source media for the active timeline clip. Does not create ColorSession."""
```

或者：

```python
def load_video(self, path: str, *, create_color_session: bool = False) -> bool:
```

因为无需兼容，推荐更清晰的拆分：

- `load_video_source_for_timeline_clip(path) -> bool`
- `load_image(path) -> bool` 保留 standalone image session。

第一版可用最小改法：

```python
def load_video(self, path: str, *, allow_timeline_clip: bool = False):
    if not allow_timeline_clip:
        self._reject_standalone_video_for_color(path)
        return False
    # 不创建 single asset session
    ...
```

在 `open_timeline_clip_in_color(...)` 中调用：

```python
self.load_video(asset.path, allow_timeline_clip=True)
```

但更好的长期命名是 `load_video_source_for_timeline_clip`。

### 6.3 load_image 保持 standalone image

文件：

- `src/app/controllers/file_controller.py`

`load_image(path)` 可以继续创建单素材 ColorSession，但语义改为 standalone image。

建议改名：

- `_create_single_asset_color_session(...)`
- 改为 `_create_standalone_image_color_session(...)`

并加校验：

```python
if VideoDecoder.is_video_file(path):
    raise ValueError("Standalone ColorSession only supports images")
```

---

## 7. 新增核心入口：open_timeline_clip_in_color

### 7.1 新方法位置

建议放在：

- `src/app/controllers/page_controller.py`

新增：

```python
def open_timeline_clip_in_color(
    self,
    timeline_id: str,
    clip_id: str,
    timeline_frame: int | None = None,
) -> None:
```

职责：

1. 找到 timeline。
2. 找到 clip。
3. 找到 asset。
4. 确认 asset 是 video 或 image。
5. 确保 clip 有 `color_stack_id`。
6. 根据 `timeline_frame` 算出：
   - `clip_local_frame`
   - `source_frame`
7. 创建 `PageContext`。
8. 切换到 Color 页。
9. 加载素材到 Color 引擎。
10. 加载调色栈。
11. 更新 clip strip 高亮。

### 7.2 伪代码

```python
def open_timeline_clip_in_color(self, timeline_id: str, clip_id: str, timeline_frame: int | None = None) -> None:
    project = self._current_edit_project
    if project is None:
        self.status_bar.showMessage("No edit project", 3000)
        return

    timeline = self._current_timeline_sequences.get(timeline_id)
    if timeline is None:
        self.status_bar.showMessage("Timeline not found", 3000)
        return

    clip = timeline.get_clip_by_id(clip_id)
    if clip is None:
        self.status_bar.showMessage("Clip not found", 3000)
        return

    asset = project.assets.get(clip.asset_id)
    if asset is None:
        self.status_bar.showMessage("Asset not found", 3000)
        return

    if not (asset.is_video or asset.is_image):
        self.status_bar.showMessage("Only video and image clips can be graded", 3000)
        return

    if timeline_frame is None:
        timeline_frame = self.timeline_panel.get_playhead_frame()

    if timeline_frame < clip.timeline_start or timeline_frame >= clip.timeline_end:
        timeline_frame = clip.timeline_start

    clip_local_frame = timeline_frame - clip.timeline_start
    source_frame = clip.timeline_to_source_frame(timeline_frame)

    color_stack_id = clip.color_stack_id
    if not color_stack_id:
        color_stack_id = self._ensure_clip_has_color_stack(clip_id, clip.asset_id)
        timeline = self._current_timeline_sequences.get(timeline_id)
        clip = timeline.get_clip_by_id(clip_id) if timeline else clip

    context = self._page_context_manager.create_context_for_edit_to_color(
        timeline_id=timeline_id,
        clip_id=clip_id,
        asset_id=clip.asset_id,
        timeline_frame=timeline_frame,
        source_frame=source_frame,
        clip_local_frame=clip_local_frame,
        color_stack_id=color_stack_id,
    )
    self.switch_to_page_with_context(context)
```

### 7.3 frame 映射注意事项

不要直接写：

```python
source_frame = clip.source_start + (timeline_frame - clip.timeline_start)
```

必须使用 clip 的统一映射方法：

```python
source_frame = clip.timeline_to_source_frame(timeline_frame)
```

原因：

- trim 会改变 source_start。
- speed 会改变映射。
- 后续 reverse / retime / freeze frame 也应统一进入同一映射函数。

如果 `timeline_to_source_frame(...)` 当前对 `SpeedParams` 支持不完整，先补这个底层方法。

---

## 8. 改造 _handle_enter_color_with_context

文件：

- `src/app/controllers/page_controller.py`

当前函数里有：

```python
if asset.is_video:
    frame = context.frame or 0
    if self.current_file_path != asset.path:
        self.load_video(asset.path)
    if frame > 0 and self.video_controller:
        self.video_controller.seek_to_frame(frame)
```

新逻辑：

```python
if asset.is_video:
    source_frame = context.source_frame or 0
    if self.current_file_path != asset.path:
        self.load_video_source_for_timeline_clip(asset.path)
    if self.video_controller:
        self.video_controller.seek_to_frame(source_frame)
elif asset.is_image:
    if self.current_file_path != asset.path:
        self.load_image_for_timeline_clip(asset.path)
```

注意：图片如果是时间线 clip，不能创建 standalone image session。  
所以需要拆分图片加载：

- `load_image(path)`：用户单独打开图片，创建 standalone image session。
- `load_image_source_for_timeline_clip(path)`：Color 加载时间线图片 clip，不创建 standalone image session。

如果短期不想拆两套图片加载函数，则给 `load_image` 加参数：

```python
def load_image(self, path: str, *, create_standalone_session: bool = True) -> bool:
```

Timeline clip 模式调用：

```python
self.load_image(asset.path, create_standalone_session=False)
```

---

## 9. Edit 页面到 Color 页的入口改造

### 9.1 移除单击自动跳 Color

文件：

- `src/app/controllers/edit_controller.py`

当前：

```python
if len(clip_ids) == 1:
    self.edit_effects_panel.set_selected_clip(clip_ids[0])
    self.edit_page.signal_hub.clip_selected.emit(clip_ids[0])
```

修改为：

```python
if len(clip_ids) == 1:
    self.edit_effects_panel.set_selected_clip(clip_ids[0])
```

不要发 `clip_selected` 用于切页。

如果 `clip_selected` 还被 ProgramViewer 或属性面板依赖，保留信号但不要连接到 `_on_clip_selected_for_color`。  
更简单的做法：

- 在 `src/app/controllers/dock_layout_controller.py` 移除：

```python
self.edit_page.signal_hub.clip_selected.connect(self._on_clip_selected_for_color)
```

### 9.2 右键 Open in Color 接线

文件：

- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/ui/timeline_view.py`
- `src/features/edit/ui/edit_page.py`
- `src/app/controllers/edit_controller.py` 或 `page_controller.py`

当前 `ClipItem.open_in_color_requested` 存在，但 `TimelineView._create_clip_item(...)` 未连接。

步骤：

1. 在 `TimelineView` 新增信号：

```python
open_in_color_requested = pyqtSignal(str, str, str)  # clip_id, track_id, stack_id
```

2. 在 `_create_clip_item(...)` 中连接：

```python
item.open_in_color_requested.connect(self.open_in_color_requested.emit)
```

3. 在 `TimelinePanel` 转发：

```python
open_in_color_requested = pyqtSignal(str, str, str)
self.timeline_view.open_in_color_requested.connect(self.open_in_color_requested.emit)
```

4. 在 `EditPage` signal hub 增加：

```python
open_in_color_requested = pyqtSignal(str, str, str)
```

5. 在 MainWindow / controller 中连接到：

```python
def _on_open_in_color_requested(self, clip_id: str, track_id: str, stack_id: str) -> None:
    timeline_id = self._current_edit_project.active_timeline_id
    self.open_timeline_clip_in_color(timeline_id, clip_id)
```

### 9.3 Color 页 clip strip 点击入口

`TimelineClipStrip.clip_activated(timeline_id, clip_id)` 直接连接：

```python
self.open_timeline_clip_in_color(timeline_id, clip_id)
```

这应成为主要用户入口。

---

## 10. 调色栈创建与保存规则

### 10.1 clip 没有 color_stack_id 时

调用：

- `src/app/controllers/edit_controller.py::_ensure_clip_has_color_stack(...)`

当前逻辑可以复用，但要确认：

1. 创建 `ColorStackData` 后，`EditProject.color_stacks` 更新。
2. `ClipInstance.color_stack_id` 更新。
3. `_current_timeline_sequences[timeline_id]` 更新。
4. `timeline_panel.set_timeline_sequence(new_timeline)` 或 `timeline_view.set_timeline(new_timeline)` 被调用，让 UI 同步。
5. `color_timeline_clip_strip.refresh()` 被调用。

### 10.2 保存调色参数

保存仍由：

- `src/shared/project/save_router.py::_save_from_color()`
- `src/shared/project/save_router.py::_save_color_to_edit_project(...)`

目标行为：

- 如果 `ColorSession.is_timeline_clip()`：
  - 必须有 `timeline_id`
  - 必须有 `clip_id`
  - 必须有 `color_stack_id`
  - 保存到 `EditProject.color_stacks[color_stack_id]`

- 如果 `ColorSession.is_single_asset()`：
  - 必须是图片。
  - 保存单图工程包。

### 10.3 调色实时更新

当前 `_setup_grading_save_callback(...)` 会将 grading panel 变更写回 ColorStack。  
需要补齐：

- 曲线面板。
- HSL 曲线面板。
- Mixer 面板。
- Soft Clip 面板。
- LUT。
- Film grain / effects 如果属于调色栈，也要统一写回。

第一阶段最低要求：

- Color wheel / grading panel 参数必须写回。
- SaveRouter 手动保存必须写回完整 engine 状态。

---

## 11. Frame 与播放联动

### 11.1 Color 页播放时间语义

在 `TIMELINE_CLIP` 模式下，Color 页播放器显示的范围应是当前 clip 范围，而不是整个源视频。

第一版可以简化：

- 播放器仍使用源视频帧号。
- 但 seek 范围限制在：

```python
clip.source_start <= source_frame <= clip.source_end
```

更理想的实现：

- UI 显示 clip-local frame：`0..clip.timeline_duration-1`
- 内部请求源帧：`clip.timeline_to_source_frame(clip.timeline_start + clip_local_frame)`

### 11.2 新增 helper

建议在 `ClipInstance` 增加或修复：

文件：

- `src/features/edit/core/timeline_v2.py`

```python
def timeline_to_source_frame(self, timeline_frame: int) -> int:
    ...

def clip_local_to_source_frame(self, clip_local_frame: int) -> int:
    timeline_frame = self.timeline_start + clip_local_frame
    return self.timeline_to_source_frame(timeline_frame)

def source_to_clip_local_frame(self, source_frame: int) -> int:
    ...
```

必须兼容 `speed: SpeedParams | float`：

```python
speed = self.effective_speed()
```

不要直接用 `clip.speed` 乘法。

### 11.3 切换 clip 时的 seek

当用户点击 clip strip 中的 clip：

1. 如果 Edit playhead 在该 clip 范围内：
   - 使用 Edit playhead。
2. 否则：
   - 使用 clip 起点。
3. 计算 source frame。
4. Color video controller seek 到 source frame。
5. 更新 ColorSession 的 `timeline_frame/source_frame/clip_local_frame`。
6. 更新 clip strip 高亮。

---

## 12. Standalone Image 模式

### 12.1 允许入口

用户可以从 Library/File/Open 打开图片进入 Color。

行为：

1. `load_image(path)`。
2. 创建 `ColorSession(SourceKind.SINGLE_ASSET)`。
3. 设置 mode 为 `STANDALONE_IMAGE`。
4. clip strip 可以显示 active timeline clips，但当前高亮为空。
5. 保存到单图工程包。

### 12.2 禁止入口

用户从 Library/File/Open 打开视频：

1. 不调用 `load_video()`。
2. 显示提示。
3. 不创建 ColorSession。
4. 不污染当前已有 ColorSession。

### 12.3 时间线图片 clip

如果图片在时间线中被点击：

1. 使用 `TIMELINE_CLIP` 模式。
2. 加载图片源到 engine。
3. 不创建 standalone image session。
4. 保存到 `clip.color_stack_id`。

---

## 13. 多轨道视频的 Color 页 clip strip 规则

Color 页片段面板应该显示时间线上的每个可调色镜头，而不是只显示 V1。

第一版规则：

- 所有 video track 的 video/image clip 都显示。
- 按 `timeline_start` 排序。
- 多轨同帧 clip 全部显示。
- 当前播放头处如果多个视频 clip 叠加，默认选中最上层可见 clip。

最上层判断：

- 如果 `TimelineSequence.all_tracks` 的顺序就是视觉顺序，则用最后一个 video track 作为上层。
- 如果项目里已有明确 track order，请按现有 track order。

注意：

- Color 页面是“单 clip 调色”，不是“对合成后的整帧调色”。
- 点击哪个 clip，就编辑哪个 clip 的 `color_stack_id`。
- 多轨合成预览属于 Edit/Deliver 渲染管线，不是第一阶段 Color clip strip 必须解决的问题。

---

## 14. 文件级实施任务

### T1. PageContext 字段拆分

涉及文件：

- `src/app/main_window.py`
- `src/app/controllers/page_controller.py`
- `src/app/controllers/edit_controller.py`
- tests under `tests/architecture` 或新增测试

实施：

1. 修改 `PageContext`：
   - 删除或停用 `frame`。
   - 新增 `timeline_frame/source_frame/clip_local_frame`。
2. 修改 `to_dict/from_dict`。
3. 修改 `PageContextManager.create_context_for_edit_to_color(...)`。
4. 全项目搜索 `context.frame`，逐个替换。
5. 全项目搜索 `create_context_for_edit_to_color(`，更新参数。

验证：

```bash
python3 -m py_compile src/app/main_window.py src/app/controllers/page_controller.py src/app/controllers/edit_controller.py
pytest tests/architecture/test_page_context.py -q
```

### T2. ColorSession 字段与模式收口

涉及文件：

- `src/features/color/core/color_session.py`
- `tests/color/test_color_session.py`

实施：

1. 新增 `timeline_frame/source_frame/clip_local_frame`。
2. `TIMELINE_CLIP` 强制要求 `timeline_id`。
3. `create_timeline_clip(...)` 接收明确 frame 字段。
4. `from_page_context(...)` 使用 `context.timeline_frame/source_frame/clip_local_frame`。
5. `to_dict/from_dict` 更新。
6. 测试覆盖：
   - timeline clip session 没有 timeline_id 抛错。
   - standalone image session 不需要 timeline_id/clip_id。
   - timeline/source/clip_local 三个 frame 字段能正确序列化。

验证：

```bash
python3 -m py_compile src/features/color/core/color_session.py
pytest tests/color/test_color_session.py -q
```

### T3. 禁止 standalone video

涉及文件：

- `src/app/controllers/file_controller.py`
- `src/app/controllers/menu_toolbar_controller.py`

实施：

1. 新增 `_reject_standalone_video_for_color(path)`。
2. 修改用户打开文件入口：
   - 图片：`load_image(path)`。
   - 视频：拒绝，提示必须进入 Edit 时间线。
3. 删除 `load_video()` 中的 `_create_single_asset_color_session(path, frame=0)`。
4. 将 `load_video()` 改成内部函数或加受控参数。
5. 确保只有 `open_timeline_clip_in_color(...)` 可以加载视频源。

验证：

```bash
python3 -m py_compile src/app/controllers/file_controller.py src/app/controllers/menu_toolbar_controller.py
```

人工验收：

1. 在 Color/Library 中打开 `.mp4`。
2. 应显示提示。
3. 不应创建 `ColorSession(source_kind=single_asset)`。
4. 当前页面不应加载该视频。

### T4. 新增 TimelineClipStrip UI

涉及文件：

- 新增 `src/features/color/ui/timeline_clip_strip.py`
- `src/features/color/module.py`
- `src/app/controllers/dock_layout_controller.py`

实施：

1. 创建 `ColorClipEntry` dataclass。
2. 创建 `TimelineClipCard`。
3. 创建 `TimelineClipStrip`。
4. 实现：
   - `set_project_and_timeline(project, timeline)`
   - `set_active_clip(clip_id)`
   - `clear()`
   - `refresh()`
5. 过滤 video/image clip。
6. 点击 card 发出 `clip_activated(timeline_id, clip_id)`。
7. 接入 Color 页面 dock。

验证：

```bash
python3 -m py_compile src/features/color/ui/timeline_clip_strip.py
```

可新增测试：

- `tests/color/test_timeline_clip_strip_model.py`

测试建议只测 entry 构建逻辑，不启动完整 UI。

### T5. 新增 open_timeline_clip_in_color

涉及文件：

- `src/app/controllers/page_controller.py`
- `src/app/controllers/edit_controller.py`

实施：

1. 新增 `open_timeline_clip_in_color(timeline_id, clip_id, timeline_frame=None)`。
2. 使用 active timeline 查找 clip。
3. 查找 asset。
4. 校验 video/image。
5. 确保 color stack。
6. 计算 source frame。
7. 创建 PageContext。
8. `switch_to_page_with_context(context)`。
9. 刷新 clip strip 高亮。

验证：

```bash
python3 -m py_compile src/app/controllers/page_controller.py src/app/controllers/edit_controller.py
```

人工验收：

1. Edit 页面创建时间线。
2. 放入视频。
3. 切到 Color 页面。
4. clip strip 显示该视频 clip。
5. 点击 clip。
6. Color viewer 加载该视频源。
7. grading panel 显示该 clip 的 color stack。

### T6. 改造 _handle_enter_color_with_context

涉及文件：

- `src/app/controllers/page_controller.py`
- `src/app/controllers/file_controller.py`

实施：

1. `TIMELINE_CLIP + video`：
   - 调用内部视频源加载。
   - seek 到 `context.source_frame`。
   - 不创建 single asset session。
2. `TIMELINE_CLIP + image`：
   - 调用内部图片源加载。
   - 不创建 standalone image session。
3. 加载 color stack。
4. 创建 ColorSession。
5. 设置 grading save callback。
6. 更新 clip strip active state。

验证：

```bash
python3 -m py_compile src/app/controllers/page_controller.py src/app/controllers/file_controller.py
```

### T7. Edit 右键 Open in Color 接线

涉及文件：

- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/ui/timeline_view.py`
- `src/features/edit/ui/timeline_panel.py`
- `src/features/edit/ui/edit_page.py`
- `src/app/controllers/dock_layout_controller.py`
- `src/app/controllers/page_controller.py`

实施：

1. `TimelineView` 新增 `open_in_color_requested`。
2. `_create_clip_item(...)` 连接 `item.open_in_color_requested`。
3. `TimelinePanel` 转发。
4. `EditPage` 或 MainWindow 连接到 `open_timeline_clip_in_color(...)`。
5. 移除 clip 单选自动跳 Color。

验证：

人工：

1. Edit 时间线视频 clip 右键。
2. 点击 `Open in Color...`。
3. 应切到 Color。
4. Color clip strip 高亮对应 clip。
5. 当前 ColorSession 是 `TIMELINE_CLIP`。

### T8. Frame 映射测试

涉及文件：

- `src/features/edit/core/timeline_v2.py`
- 新增 `tests/edit/test_clip_timeline_color_frame_mapping.py`

测试用例：

1. normal speed:
   - `timeline_start=100`
   - `source_start=50`
   - timeline frame 110 -> source frame 60
2. trimmed clip:
   - `source_start=200`
   - timeline frame at clip start -> source 200
3. speed 2.0:
   - clip local 10 -> source offset 20
4. speed 0.5:
   - clip local 10 -> source offset 5
5. frame outside clip:
   - 抛 `ValueError` 或 clamp，按现有模型决定。

验证：

```bash
pytest tests/edit/test_clip_timeline_color_frame_mapping.py -q
```

### T9. SaveRouter 验证

涉及文件：

- `src/shared/project/save_router.py`
- `tests/project/test_save_router.py`

实施：

1. 增加 timeline clip save 测试。
2. 构造 `ColorSession(TIMELINE_CLIP)`。
3. 修改 engine grading params。
4. 调用保存。
5. 断言 `EditProject.color_stacks[color_stack_id]` 被更新。
6. 断言不是写入 primary standalone stack。

验证：

```bash
pytest tests/project/test_save_router.py -q
```

### T10. 端到端手工验收脚本

执行完所有任务后，必须做以下手工验证：

1. 启动应用。
2. 新建 Edit Project。
3. 导入一个视频。
4. 将视频拖到时间线。
5. 切到 Color 页面。
6. 底部/指定位置出现 Timeline Clips 面板。
7. 面板里出现该视频 clip。
8. 点击该 clip。
9. Viewer 显示该 clip 对应视频帧。
10. 调整 Lift/Gamma/Gain 或曝光。
11. 保存工程。
12. 切回 Edit 页面。
13. 再切回 Color 页面并点击同一 clip。
14. 调色参数仍然存在。
15. 在 Color 页尝试直接打开另一个视频。
16. 应被拒绝并提示先放到时间线。
17. 在 Color 页直接打开一张图片。
18. 图片可以调色并保存。

---

## 15. 自动化测试清单

建议新增或修改这些测试：

1. `tests/color/test_color_session.py`
   - `TIMELINE_CLIP` 必须有 `timeline_id`。
   - `source_frame/timeline_frame/clip_local_frame` 序列化。

2. `tests/architecture/test_page_context.py`
   - PageContext 明确 frame 字段。
   - `create_context_for_edit_to_color(...)` 必须写入 timeline_id。

3. `tests/edit/test_clip_timeline_color_frame_mapping.py`
   - 时间线帧到源帧映射。

4. `tests/color/test_timeline_clip_strip_model.py`
   - clip strip 只列 video/image clip。
   - 不列 audio clip。
   - 排序稳定。
   - active clip 高亮状态可设置。

5. `tests/project/test_save_router.py`
   - timeline clip 调色保存到对应 color stack。
   - standalone image 保存不影响 EditProject timeline clip stack。

6. `tests/app/test_color_video_standalone_rejection.py`
   - 用户入口打开视频时拒绝。
   - 内部 timeline clip 加载视频不拒绝。

如果 Qt UI 测试成本高，T4/T6/T7 可以先用 controller 单元测试和人工验收覆盖。

---

## 16. 完成标准

功能完成必须同时满足：

1. Color 页面能显示当前活动 Edit 时间线上的所有视频/图片 clip。
2. 点击 Color 页片段面板中的 clip 后，Color 页面进入 `TIMELINE_CLIP` session。
3. 视频无法在 Color 页面作为 standalone 文件打开。
4. 图片仍可 standalone 打开调色。
5. 时间线图片 clip 进入 Color 时走 `TIMELINE_CLIP`，不是 standalone image。
6. `ColorSession` 中可以明确看到：
   - `timeline_id`
   - `clip_id`
   - `asset_id`
   - `color_stack_id`
   - `timeline_frame`
   - `source_frame`
   - `clip_local_frame`
7. 修改调色参数后保存，数据写入 `EditProject.color_stacks[color_stack_id]`。
8. 切换 clip 后不会污染上一个 clip 的调色栈。
9. 多个 clip 使用同一素材时，可以拥有不同 `color_stack_id`。
10. 单击 Edit 时间线 clip 不会自动跳 Color。
11. 右键 `Open in Color...` 能打开对应 timeline clip。
12. 所有新增/修改代码通过 `py_compile`。
13. 相关 pytest 通过。

---

## 17. 推荐执行顺序

严格按这个顺序做，不要跳步：

1. T1：PageContext 字段拆分。
2. T2：ColorSession 字段与校验。
3. T8：Clip frame 映射测试和修复。
4. T3：禁止 standalone video，拆分加载职责。
5. T5：新增 `open_timeline_clip_in_color(...)`。
6. T6：改造 `_handle_enter_color_with_context(...)`。
7. T4：新增 Color TimelineClipStrip UI。
8. T7：接入 Edit 右键 Open in Color。
9. T9：SaveRouter 验证。
10. T10：端到端人工验收。

原因：

- 先改数据语义，避免 UI 先接上后继续传错 frame。
- 先禁止 standalone video，避免新旧入口共存造成假通过。
- UI 放后面，避免只做出面板但业务仍未打通。

---

## 18. 给后续 AI 的第一条执行 Prompt

建议将下面这段直接交给后续 AI 执行第一阶段：

```text
请在 Topos Color 项目中执行 docs/color_timeline_clip_grading_unification_plan_2026-05-31.md 的 T1 和 T2。

项目技术栈：PyQt6 + ModernGL + GLSL。
严格遵守 .clinerules：
- UI 线程不要直接乱调 OpenGL。
- 所有函数补类型注解。
- 信号连接避免递归。
- 不要做无关重构。

本次只做：
1. PageContext 字段从 frame 拆成 timeline_frame/source_frame/clip_local_frame。
2. create_context_for_edit_to_color 必须接收 timeline_id。
3. ColorSession 增加 timeline_frame/source_frame/clip_local_frame。
4. TIMELINE_CLIP 必须要求 timeline_id/clip_id/asset_id/color_stack_id。
5. 更新相关测试。

不要实现 UI 面板，不要改视频加载入口。
完成后运行：
python3 -m py_compile src/app/main_window.py src/app/controllers/page_controller.py src/features/color/core/color_session.py
pytest tests/architecture/test_page_context.py tests/color/test_color_session.py -q
```

---

## 19. 风险提示

1. `load_video()` 当前承担太多职责。
   - 它既是用户打开视频入口，又是 Color 页面显示视频源的工具。
   - 必须拆出内部源加载路径，否则很容易误创建 standalone video session。

2. `frame` 字段语义混乱。
   - 如果后续 AI 不拆这个字段，只在 UI 上加面板，视频 trim/speed 后一定会错帧。

3. `clip_selected` 自动跳 Color 不适合新产品规则。
   - 必须移除自动跳转。
   - Color 页 clip strip 才是调色页内切换镜头的主入口。

4. 保存路径要防止写错栈。
   - timeline clip 调色只能写 `clip.color_stack_id`。
   - standalone image 只能写图片自己的单图工程包。

5. 多轨道预览不是第一阶段核心。
   - 第一阶段只保证“点击哪个 clip，调哪个 clip”。
   - 多轨合成后的整帧调色属于后续更复杂的 Timeline Render Plan + Color pipeline 整合。

---

## 20. 本方案不做的事

第一阶段不做：

- 不做达芬奇完整 node graph。
- 不做 Color 页完整时间线编辑。
- 不在 Color 页移动/裁剪 clip。
- 不做视频 standalone 调色兼容。
- 不做旧项目迁移。
- 不做真实缩略图生成的 GPU 优化。
- 不做多轨合成结果调色。

第一阶段只做一件事：

**让 Color 页面以当前 Edit 时间线上的 clip 为唯一视频调色入口，并能稳定选择、加载、调色、保存。**

