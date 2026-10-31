# Topos Color 时间线剩余优化计划

日期：2026-06-06
基于：三次审计后的完成状态（P0~P2 ~95%, P3~P4 ~80%, P5~P6 ~65%, P7 ~25%）

本文档列出所有剩余问题和欠缺功能，按优先级排序。每个任务都有具体的实施步骤、涉及文件和验证方法。

---

## 总体状态

| 阶段 | 完成度 | 剩余工作性质 |
|---|---:|---|
| P0 播放链路和隐藏BUG | ~95% | 1个隐藏BUG：track visibility 不影响渲染 |
| P1 视觉和坐标稳定 | ~90% | 细节打磨 |
| P2 基础剪辑功能 | ~95% | 基本完成 |
| P3 音频播放和波形 | ~75% | 导出混音、电平表、mixer面板 |
| P4 范围条和导出范围 | ~88% | 导出音频流写入 |
| P5 工具验收 | ~80% | 验收矩阵跑通 |
| P6 性能优化 | ~55% | 大项目 benchmark 和优化 |
| P7 NLE功能扩展 | ~25% | 大量功能待实现 |

---

## Task 1：修复 Track Visibility 不影响渲染（隐藏BUG）

### 问题

`SetTrackVisibilityCommand` 和 `TrackHeaderItem` 的 visibility 切换已完整实现，但 `is_track_renderable()` 只检查 `is_enabled`、`is_muted`、`is_solo`，不检查 `is_visible`。结果：用户关闭视频轨 visibility 后，预览和导出仍然渲染该轨道。

### 涉及文件

- `src/features/edit/core/timeline_render_plan.py` — `is_track_renderable()` 函数
- `src/features/edit/core/timeline_export.py` — 导出时 `_render_frame()` 使用的 render plan
- `tests/edit/test_timeline_render_plan.py` — 新增测试

### 实施步骤

1. **修改 `is_track_renderable()`**（`timeline_render_plan.py` L388-416）：

```python
def is_track_renderable(track: EditTrack, has_solo: bool) -> bool:
    # 轨道不可见时不渲染
    if not track.is_visible:
        return False

    # 轨道禁用时不渲染
    if not track.is_enabled:
        return False

    # 静音轨道不渲染
    if track.is_muted:
        return False

    # 如果有 solo 轨道，只有 solo 轨道渲染
    if has_solo and not track.is_solo:
        return False

    return True
```

2. **清理 render plan 缓存**：`is_visible` 变化时需要 invalidate `_has_solo_cache` 和 `_track_clip_cache`。检查 `TimelineRenderPlan.invalidate_cache()` 是否在 visibility 变化时被调用。

3. **TimelineView 中连接信号**：在 `_on_track_visibility_changed()` 中，除了执行命令，还要调用 render plan 的 cache invalidate 并请求重新渲染当前帧。

### 验证方法

```bash
# 编译检查
python3 -m py_compile src/features/edit/core/timeline_render_plan.py

# 现有测试
python3 -m pytest tests/edit/test_timeline_render_plan.py -q

# 新增测试用例
# test_hidden_track_not_in_render_plan:
#   创建 2 视频轨 timeline，V2 有 clip，设置 V2.is_visible=False
#   调用 get_frame_plan(frame) 确认 V2 的 clip 不在 layers 中
#
# test_hidden_track_not_in_export:
#   同上场景，导出后验证 V2 clip 的帧不在输出中
```

手动验收：
1. 创建 V1 + V2 timeline，V2 有 clip
2. 关闭 V2 visibility
3. 播放到 V2 clip 位置，预览应显示 V1 内容（或黑帧）
4. 导出该范围，导出文件不应包含 V2 clip

---

## Task 2：导出音频混音

### 问题

`TimelineExportConfig.include_audio` 配置已存在，但 `TimelineExporter.export_timeline()` 的逐帧循环只渲染视频帧，没有：
1. 收集音频 clip 并解码
2. 混音（考虑 M/S/volume）
3. 将音频流写入输出文件

### 涉及文件

- `src/features/edit/core/timeline_export.py` — 主要修改
- `src/features/edit/core/audio_playback.py` — `AudioMixer.mix_samples()` 可复用
- `src/shared/export/video_encoder.py` — `FFmpegVideoEncoder` 需支持音频写入
- `src/shared/export/encoder.py` — `AudioEncoderConfig` 已存在
- `tests/edit/test_timeline_export.py` — 扩展音频导出测试

### 实施步骤

1. **在 `TimelineExportConfig` 增加音频参数**：

```python
@dataclass
class TimelineExportConfig:
    # ... 现有字段 ...
    audio_codec: str = "aac"
    audio_bitrate: int = 192       # kbps
    audio_sample_rate: int = 48000
    audio_channels: int = 2
```

2. **新增 `_collect_audio_clips_for_range()` 方法**（`timeline_export.py`）：

```python
def _collect_audio_clips_for_range(
    self, start_frame: int, end_frame: int
) -> list[tuple[ClipInstance, EditTrack]]:
    """收集范围内的音频 clip 和对应轨道。"""
    result = []
    for track in self._sequence.audio_tracks:
        if not is_track_renderable(track, self._has_audio_solo()):
            continue
        for clip in track.clips:
            if clip.timeline_start < end_frame and clip.timeline_end > start_frame:
                result.append((clip, track))
    return result
```

3. **新增 `_render_audio_for_range()` 方法**：

```python
def _render_audio_for_range(
    self,
    start_frame: int,
    end_frame: int,
    config: TimelineExportConfig,
) -> Optional[np.ndarray]:
    """
    渲染指定范围的混合音频采样。

    返回:
        shape=(num_samples, channels) 的 float32 数组，或 None
    """
    fps = float(self._sequence.fps)
    duration_seconds = (end_frame - start_frame) / fps
    total_samples = int(duration_seconds * config.audio_sample_rate)

    if total_samples <= 0:
        return None

    # 使用 AudioMixer 复用播放时混音逻辑
    mixer = AudioMixer(sample_rate=config.audio_sample_rate)
    mixer.set_tracks_from_sequence(self._sequence, self._project)

    # 逐块混音（每次处理 1024 采样）
    block_size = 1024
    output = np.zeros((total_samples, config.audio_channels), dtype=np.float32)
    samples_written = 0

    while samples_written < total_samples:
        remaining = total_samples - samples_written
        n = min(block_size, remaining)
        # 计算对应的 timeline frame
        current_frame = start_frame + int(samples_written / config.audio_sample_rate * fps)
        block = mixer.mix_samples(
            current_frame, fps, n,
            output_channels=config.audio_channels
        )
        output[samples_written:samples_written + n] = block
        samples_written += n

    return output
```

4. **修改 `FFmpegVideoEncoder` 支持音频写入**：

在 `FFmpegVideoEncoder` 中添加：
```python
def add_audio_stream(self, audio_config: AudioEncoderConfig) -> None:
    """添加音频流到容器。"""
    # 使用 pyav 添加 audio stream
    self._audio_stream = self._container.add_stream(
        audio_config.get_ffmpeg_codec_name(),
        rate=audio_config.sample_rate,
    )
    self._audio_stream.layout = 'stereo' if audio_config.channels == 2 else 'mono'

def write_audio_samples(self, samples: np.ndarray) -> None:
    """写入音频采样。"""
    import av
    frame = av.AudioFrame.from_ndarray(samples.T, format='fltp', layout='stereo')
    frame.sample_rate = self._audio_stream.rate
    for packet in self._audio_stream.encode(frame):
        self._container.mux(packet)
```

5. **修改 `export_timeline()` 主循环**：

```python
def export_timeline(self, config):
    # ... 现有初始化 ...

    # 音频准备
    audio_data = None
    if config.include_audio and self._sequence.audio_tracks:
        audio_data = self._render_audio_for_range(start_frame, end_frame, config)
        if audio_data is not None:
            audio_config = AudioEncoderConfig(
                codec=config.audio_codec,
                bitrate=config.audio_bitrate,
                sample_rate=config.audio_sample_rate,
                channels=config.audio_channels,
            )
            self._encoder.add_audio_stream(audio_config)

    # 逐帧渲染视频
    for frame_idx in range(start_frame, end_frame):
        # ... 现有视频渲染 ...

    # 写入音频
    if audio_data is not None:
        self._encoder.write_audio_samples(audio_data)

    # 关闭编码器
    self._encoder.close()
```

### 验证方法

```bash
python3 -m py_compile src/features/edit/core/timeline_export.py
python3 -m py_compile src/shared/export/video_encoder.py
python3 -m pytest tests/edit/test_timeline_export.py -q

# 新增测试：
# test_export_with_audio_clips:
#   创建含音频 clip 的 timeline，导出后验证输出文件有音频流
#   使用 ffprobe/av 检查 output 的 audio stream 存在
#
# test_export_respects_mute_solo:
#   mute 一条音频轨，导出后该轨内容不在混音中
#
# test_export_audio_duration_matches_video:
#   导出后音频时长和视频时长差 < 50ms
#
# test_export_no_audio_clips_produces_video_only:
#   timeline 无音频 clip 时，include_audio=True 也不崩溃，输出无音频流
```

手动验收：
1. 创建 V1+A1 timeline，A1 有音频 clip
2. 导出，用播放器验证有声音
3. Mute A1，再次导出，验证无声
4. 设置 I/O range，导出，验证音频范围正确

---

## Task 3：音频电平表和 Mixer 面板接入 Edit 页

### 问题

`src/shared/ui/audio_mixer/audio_level_meter.py` 和 `track_mixer_widget.py` 已存在，但未接入 Edit 页 UI。用户无法看到实时电平，也无法通过 mixer 面板调整轨道音量。

### 涉及文件

- `src/features/edit/ui/edit_page.py` — 接入电平表
- `src/features/edit/ui/timeline_panel.py` — 集成 mixer 面板入口
- `src/features/edit/core/audio_playback.py` — `AudioPlaybackManager` 输出电平数据
- `src/shared/ui/audio_mixer/audio_level_meter.py` — 已有组件
- `src/shared/ui/audio_mixer/track_mixer_widget.py` — 已有组件

### 实施步骤

1. **在 `AudioPlaybackManager` 添加电平输出信号**：

```python
class AudioPlaybackManager(QObject):
    # 新增信号
    audio_level_changed = pyqtSignal(float, float)  # left_peak, right_peak (0.0~1.0)

    # 在混音回调中计算 peak 并发射信号
    def _on_mix_output(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        left = np.max(np.abs(samples[:, 0])) if samples.shape[1] > 0 else 0.0
        right = np.max(np.abs(samples[:, 1])) if samples.shape[1] > 1 else 0.0
        self.audio_level_changed.emit(float(left), float(right))
```

2. **在 EditPage 或 TimelinePanel 创建电平表 widget**：

```python
from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter

# 在 TimelinePanel.__init__ 或 EditPage.__init__ 中
self._level_meter = AudioLevelMeter()
self._level_meter.setFixedWidth(24)
# 放到 ProgramViewer 右侧或 TimelinePanel 工具栏

# 连接信号
self._audio_manager.audio_level_changed.connect(self._level_meter.update_levels)
```

3. **在 TimelinePanel 添加 Mixer 面板入口按钮**：

```python
# 工具栏加一个 "Mixer" 按钮
mixer_btn = QPushButton("Mixer")
mixer_btn.clicked.connect(self._toggle_mixer_panel)

def _toggle_mixer_panel(self) -> None:
    if self._mixer_dock is None:
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        self._mixer_dock = QDockWidget("Audio Mixer")
        self._mixer_widget = TrackMixerWidget()
        # 从 timeline 加载轨道
        self._mixer_widget.load_tracks(self._timeline)
        self._mixer_dock.setWidget(self._mixer_widget)
    self._mixer_dock.setVisible(not self._mixer_dock.isVisible())
```

4. **TrackMixerWidget 与 AudioPlaybackManager 双向连接**：

```python
# mixer widget 的 volume/mute/solo 变化 → audio manager
self._mixer_widget.track_volume_changed.connect(
    lambda tid, vol: self._audio_manager.set_track_volume(tid, vol)
)
self._mixer_widget.track_mute_changed.connect(
    lambda tid, muted: self._audio_manager.set_track_mute(tid, muted)
)
self._mixer_widget.track_solo_changed.connect(
    lambda tid, solo: self._audio_manager.set_track_solo(tid, solo)
)
```

### 验证方法

```bash
python3 -m py_compile src/features/edit/ui/timeline_panel.py
python3 -m py_compile src/features/edit/ui/edit_page.py

# 手动验收：
# 1. 播放含音频 clip 的 timeline，电平表有实时跳动
# 2. 打开 Mixer 面板，显示所有音频轨
# 3. 在 Mixer 中调整 volume，播放音量跟随变化
# 4. 在 Mixer 中 mute/solo，播放行为正确
# 5. 在 Mixer 中的修改同步到轨道头按钮状态
```

---

## Task 4：性能 Benchmark 和大项目优化

### 问题

`tools/benchmark_timeline_ui.py` 和 `tools/benchmark_source_viewer_playback.py` 已创建，但缺少性能阈值定义和 CI 集成。大项目（100+ clip, 20+ track）下的性能未系统测试。

### 涉及文件

- `tools/benchmark_timeline_ui.py` — 扩展
- `tools/benchmark_source_viewer_playback.py` — 扩展
- `tests/benchmark/test_playback_performance.py` — 已有
- `src/features/edit/core/timeline_thumbnail_scheduler.py` — 缩略图调度
- `src/features/edit/core/audio_waveform.py` — 波形缓存

### 实施步骤

1. **定义性能阈值**（添加到 benchmark 脚本）：

```python
PERFORMANCE_THRESHOLDS = {
    "avg_layout_ms": 16.0,        # 单次布局 < 16ms (60fps)
    "p95_paint_ms": 33.0,         # P95 绘制 < 33ms (30fps)
    "scroll_jank_count": 5,       # 10秒滚动卡顿 < 5次
    "thumbnail_hit_rate": 0.7,    # 缩略图缓存命中 > 70%
    "waveform_hit_rate": 0.8,     # 波形缓存命中 > 80%
    "max_memory_mb": 2048,        # 内存 < 2GB
    "source_playback_fps_ratio": 0.9,  # Source播放 > 90% 目标fps
    "source_dropped_frame_ratio": 0.1, # Source丢帧 < 10%
}
```

2. **扩展 benchmark 脚本输出 PASS/FAIL**：

```python
def run_benchmark(clips, video_tracks, audio_tracks, duration):
    result = { ... }
    result["pass"] = all(
        result[k] <= v if "hit_rate" not in k and "fps_ratio" not in k
        else result[k] >= v
        for k, v in PERFORMANCE_THRESHOLDS.items()
        if k in result
    )
    return result
```

3. **优化缩略图调度**（如果 benchmark 显示瓶颈）：

- 播放时暂停非 viewport 可见区域的缩略图请求
- zoom 变化时取消旧 generation 的排队任务
- 长 clip 不拆过多 thumbnail item

4. **优化波形缓存**（如果 benchmark 显示瓶颈）：

- 实现 LRU 缓存，限制内存上限
- zoom bucket 合并相近请求
- 播放时降低波形抽取优先级

### 验证方法

```bash
# 运行 benchmark
python3 tools/benchmark_timeline_ui.py --clips 100 --video-tracks 10 --audio-tracks 10 --duration 600 --json
python3 tools/benchmark_timeline_ui.py --clips 500 --video-tracks 20 --audio-tracks 20 --duration 1800 --json

# 验收标准：
# - 所有指标在阈值内 → PASS
# - 任何指标超标 → FAIL 并输出具体数字

# 性能测试
python3 -m pytest tests/benchmark/test_playback_performance.py -q
```

---

## Task 5：P7 主流 NLE 功能 — Clip Fade Handles

### 问题

Clip 的淡入淡出（fade in/out）没有可视化拖拽 handle。用户无法在时间线上直接拖 clip 边缘来创建淡入淡出效果。

### 涉及文件

- `src/features/edit/core/timeline_v2.py` — `ClipInstance` 增加 `fade_in_frames`/`fade_out_frames`
- `src/features/edit/core/commands.py` — 新增 `ChangeClipFadeCommand`
- `src/features/edit/ui/timeline_items.py` — `ClipItem` 绘制 fade 三角/handle
- `src/features/edit/ui/timeline_view.py` — fade handle 拖拽交互
- `src/features/edit/core/timeline_render_plan.py` — fade 影响渲染（alpha 渐变）
- `src/features/edit/core/timeline_export.py` — fade 影响导出

### 实施步骤

1. **数据模型**（`timeline_v2.py`）：

```python
@dataclass(frozen=True)
class ClipInstance:
    # ... 现有字段 ...
    fade_in_frames: int = 0    # 淡入持续帧数
    fade_out_frames: int = 0   # 淡出持续帧数
```

确保 `to_dict()`/`from_dict()` 序列化这两个字段。

2. **命令**（`commands.py`）：

```python
class ChangeClipFadeCommand:
    """修改 clip 的淡入/淡出帧数。"""

    def __init__(self, clip_id: str, track_id: str,
                 fade_in: int | None = None, fade_out: int | None = None):
        self._clip_id = clip_id
        self._track_id = track_id
        self._new_fade_in = fade_in
        self._new_fade_out = fade_out
        self._old_fade_in: int | None = None
        self._old_fade_out: int | None = None

    def execute(self, sequence):
        track = sequence.get_track_by_id(self._track_id)
        clip = track.get_clip_by_id(self._clip_id)
        self._old_fade_in = clip.fade_in_frames
        self._old_fade_out = clip.fade_out_frames
        # 用新值替换
        ...

    def undo(self, sequence):
        # 恢复旧值
        ...

    @staticmethod
    def create(clip_id, track_id, fade_in=None, fade_out=None):
        return ChangeClipFadeCommand(clip_id, track_id, fade_in, fade_out)
```

3. **UI 绘制**（`timeline_items.py` 的 `ClipItem`）：

```python
# 在 ClipItem 中绘制 fade handle
def _paint_fade_handles(self, painter, rect):
    fade_in = self._clip_data.get("fade_in_frames", 0)
    fade_out = self._clip_data.get("fade_out_frames", 0)

    if fade_in > 0:
        # 左侧画三角形：从 0 高度到 clip 高度
        fade_width = fade_in * self._ppf
        points = [
            QPointF(rect.left(), rect.bottom()),
            QPointF(rect.left(), rect.top()),
            QPointF(rect.left() + fade_width, rect.top()),
            QPointF(rect.left() + fade_width, rect.bottom()),
        ]
        painter.setBrush(QColor(255, 255, 255, 60))
        painter.drawPolygon(QPolygonF(points))

    if fade_out > 0:
        # 右侧同理，镜像
        ...

    # 画 handle 小三角
    handle_size = 8
    # 左 handle
    if fade_in > 0 or self._hover_fade_in:
        hx = rect.left() + fade_in * self._ppf
        painter.setBrush(QColor(255, 255, 0, 180))
        painter.drawPolygon(QPolygonF([
            QPointF(hx, rect.top()),
            QPointF(hx - handle_size/2, rect.top()),
            QPointF(hx, rect.top() + handle_size),
        ]))
```

4. **拖拽交互**（`timeline_view.py`）：

- mousePressEvent 检测点击在 fade handle 区域
- mouseMoveEvent 更新 fade 帧数
- mouseReleaseEvent 执行 `ChangeClipFadeCommand`

5. **渲染集成**（`timeline_render_plan.py`）：

```python
# 在 _create_layer_info_for_clip 中添加 fade alpha
if clip.fade_in_frames > 0:
    frames_into_clip = frame - clip.timeline_start
    if frames_into_clip < clip.fade_in_frames:
        layer.alpha = frames_into_clip / clip.fade_in_frames

if clip.fade_out_frames > 0:
    frames_from_end = clip.timeline_end - frame
    if frames_from_end < clip.fade_out_frames:
        layer.alpha = frames_from_end / clip.fade_out_frames
```

### 验证方法

```bash
python3 -m py_compile src/features/edit/ui/timeline_items.py
python3 -m py_compile src/features/edit/core/commands.py

# 新增测试 tests/edit/test_timeline_clip_fade.py：
# test_fade_in_reduces_alpha: fade_in=24 时，clip 第 12 帧 alpha=0.5
# test_fade_out_reduces_alpha: fade_out=24 时，clip 倒数第 12 帧 alpha=0.5
# test_fade_command_undo_redo: 修改 fade 后可 undo/redo
# test_fade_export_applies_alpha: 导出时 fade 帧的 alpha 正确

# 手动验收：
# 1. 拖 clip 左上角 handle 创建淡入
# 2. 拖 clip 右上角 handle 创建淡出
# 3. 播放验证渐变效果
# 4. Undo/Redo fade 操作
```

---

## Task 6：P7 主流 NLE 功能 — Transition Browser/Drop

### 问题

转场的 UI 浏览和拖放到切点的功能不完整。

### 涉及文件

- `src/features/edit/ui/timeline_items.py` — `TransitionItem` 绘制
- `src/features/edit/ui/timeline_view.py` — 转场拖放目标
- `src/features/edit/core/commands.py` — `AddTransitionCommand` 已有
- `src/features/edit/ui/effects_panel.py` — 转场列表来源

### 实施步骤

1. **TransitionItem 绘制**：在切点位置绘制菱形/交叉图标，宽度等于转场时长。
2. **拖放支持**：从 EffectsPanel 拖转场到两个 clip 之间。
3. **转场参数编辑**：双击 TransitionItem 打开参数面板（duration、type、curve）。
4. **渲染集成**：确保 `timeline_render_plan.py` 中转场混合逻辑正确作用于预览和导出。

### 验证方法

```bash
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q

# 手动验收：
# 1. 在两个相邻 clip 之间拖入 Cross Dissolve
# 2. 播放切点，看到渐变过渡
# 3. 双击转场修改 duration
# 4. Undo/Redo 添加/删除转场
# 5. 导出验证转场效果
```

---

## Task 7：P7 主流 NLE 功能 — Speed/Retime UI

### 问题

Clip 变速播放（如 0.5x、2x）的 UI 和参数编辑不完整。`ClipInstance` 可能已有 speed 字段但 UI 未暴露。

### 涉及文件

- `src/features/edit/core/timeline_v2.py` — `ClipInstance.speed` 或 `SpeedParams`
- `src/features/edit/ui/timeline_items.py` — `ClipItem` 显示速度标签
- `src/features/edit/ui/timeline_view.py` — 右键菜单 "Change Speed"
- `src/features/edit/core/timeline_render_plan.py` — 变速帧映射
- `src/features/edit/core/timeline_export.py` — 变速导出一致性

### 实施步骤

1. **右键菜单添加 "Change Speed"**：提供 0.25x、0.5x、1x、2x、4x 选项。
2. **新增 `ChangeClipSpeedCommand`**：修改 speed 后重新计算 `timeline_duration`。
3. **ClipItem 显示速度标签**：当 speed != 1.0 时在 clip 右上角显示 "2x"/"0.5x"。
4. **帧映射验证**：确保 `timeline_to_source_frame()` 在变速下正确。
5. **导出一致性**：导出时变速 clip 取正确的 source frame。

### 验证方法

```bash
# 新增测试 tests/edit/test_timeline_clip_speed.py：
# test_speed_2x_halves_duration: speed=2.0 时 timeline_duration 减半
# test_speed_0_5x_doubles_duration: speed=0.5 时 timeline_duration 加倍
# test_speed_frame_mapping: 变速 clip 的 timeline_to_source_frame 正确
# test_speed_export_consistency: 同一帧预览和导出取同一 source frame
```

---

## Task 8：P7 主流 NLE 功能 — Marker 完整功能

### 问题

Marker 基础功能可能存在，但编辑（改名/改色）、移动、导航、列表视图不完整。

### 涉及文件

- `src/features/edit/core/timeline_v2.py` — `TimelineMarker`
- `src/features/edit/core/commands.py` — marker 相关命令
- `src/features/edit/ui/timeline_items.py` — marker 绘制
- `src/features/edit/ui/timeline_view.py` — marker 交互

### 实施步骤

1. **右键 marker**：Edit Marker（改名、改色、删除）。
2. **Marker 导航**：`Shift+M` 跳下一个 marker，`Ctrl+Shift+M` 跳上一个。
3. **Marker 列表面板**：在 TimelinePanel 添加 marker 列表，双击跳转。
4. **Marker 吸附**：拖播放头、拖 clip 边缘、range handle 都吸附 marker。
5. **Marker 序列化**：确保保存/加载保留所有 marker 属性。

### 验证方法

```bash
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q

# 手动验收：
# 1. M 添加 marker
# 2. 右键 marker → Edit → 改名改色
# 3. Shift+M 在 marker 间跳转
# 4. 拖 range handle 吸附到 marker
# 5. 保存/重新打开，marker 保留
```

---

## Task 9：P7 主流 NLE 功能 — 轨道管理增强

### 问题

Add/Delete/Rename/Move track 的基础命令可能已有，但 UI 交互不完整。

### 涉及文件

- `src/features/edit/core/commands.py` — `AddTrackCommand`、`DeleteTrackCommand` 等
- `src/features/edit/ui/timeline_items.py` — `TrackHeaderItem` 右键菜单
- `src/features/edit/ui/timeline_view.py` — 轨道操作入口

### 实施步骤

1. **轨道头右键菜单**：
   - Add Track Above / Below
   - Delete Track（含 clip 处理策略：删除或移到相邻轨）
   - Rename Track（内联编辑名称）
   - Move Track Up / Down

2. **Delete Track 策略**：
   - 如果轨道有 clip：弹窗 "Delete track and all clips" / "Move clips to track above" / "Cancel"
   - 用 MacroCommand 包装所有操作

3. **Rename Track**：
   - 双击轨道名进入编辑模式
   - Enter 确认，Escape 取消

4. **Move Track**：
   - 拖拽轨道头改变轨道顺序
   - 或右键菜单 Move Up/Down

### 验证方法

```bash
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q

# 手动验收：
# 1. 右键 V1 → Add Track Above → V2 出现
# 2. 右键 V2 → Delete Track（有 clip） → 选择移动 clip 到 V1
# 3. 双击 A1 → 重命名为 "Music"
# 4. 拖拽轨道头改变顺序
# 5. 以上全部 Undo/Redo
```

---

## Task 10：P7 主流 NLE 功能 — Ripple Delete 完善

### 问题

Ripple delete 命令可能存在但边界情况未处理：linked A/V ripple、多 clip ripple、undo 后位置恢复。

### 涉及文件

- `src/features/edit/core/commands.py` — `RippleDeleteCommand`
- `src/features/edit/ui/timeline_view.py` — 右键菜单 "Ripple Delete"

### 实施步骤

1. **右键菜单添加 "Ripple Delete"**（如果尚未有）。
2. **Linked A/V ripple**：删除 video clip 时，linked audio clip 也 ripple delete。
3. **多选 ripple**：框选多个 clip 后 ripple delete，后续 clip 统一左移。
4. **边界检查**：ripple 后不允许 timeline_duration < 0。
5. **Undo 恢复**：ripple delete 后 undo，所有被移动的 clip 回到原位。

### 验证方法

```bash
# 新增测试 tests/edit/test_timeline_ripple_delete.py：
# test_ripple_delete_closes_gap: 删除 clip 后后续 clip 左移
# test_ripple_delete_linked_av: linked video 删除时 linked audio 也 ripple
# test_ripple_delete_multi_select: 多选 ripple delete
# test_ripple_delete_undo_restores_positions: undo 后所有 clip 回到原位
```

---

## Task 11：测试覆盖补全

### 问题

虽然已添加了大量测试，但仍有测试缺口。

### 需要新增的测试

| 测试文件 | 覆盖范围 | 优先级 |
|---|---|---|
| `tests/edit/test_timeline_clip_fade.py` | fade in/out 命令、渲染、导出 | High |
| `tests/edit/test_timeline_clip_speed.py` | 变速命令、帧映射、导出一致性 | High |
| `tests/edit/test_timeline_ripple_delete.py` | ripple delete 各种边界 | High |
| `tests/edit/test_timeline_marker.py` | marker CRUD、导航、吸附 | Medium |
| `tests/edit/test_timeline_track_management.py` | 轨道增删改移 | Medium |
| `tests/edit/test_timeline_visibility_render.py` | track visibility 影响渲染和导出 | High |

### 实施步骤

逐个创建测试文件，每个文件包含：
1. fixture 创建标准 timeline（3V+3A，每轨 3 clip）
2. 核心功能测试（正常路径）
3. 边界测试（空 timeline、locked track、linked A/V）
4. Undo/Redo 测试
5. 与其他功能的交互测试（如 visibility + export）

### 验证方法

```bash
python3 -m pytest tests/edit/ -q --tb=short
# 所有测试 PASS，无 skip（除 rawpy 依赖导致的已知 skip）
```

---

## 建议执行顺序

| 顺序 | Task | 原因 | 预估工作量 |
|---|---|---|---|
| 1 | Task 1: Track Visibility BUG | 隐藏 BUG，修复简单 | 小 |
| 2 | Task 11: 测试补全（visibility 部分） | 先测试后实施 | 小 |
| 3 | Task 2: 导出音频混音 | 功能缺口，影响导出完整性 | 大 |
| 4 | Task 3: 音频电平表/Mixer | 用户体验 | 中 |
| 5 | Task 4: 性能 Benchmark | 大项目稳定性 | 中 |
| 6 | Task 5: Clip Fade Handles | 高频剪辑功能 | 中 |
| 7 | Task 11: 测试补全（fade 部分） | | 小 |
| 8 | Task 6: Transition Browser | 转场可视化 | 中 |
| 9 | Task 7: Speed/Retime UI | 变速编辑 | 中 |
| 10 | Task 8: Marker 完整功能 | 导航和标记 | 小 |
| 11 | Task 9: 轨道管理增强 | 轨道操作 | 中 |
| 12 | Task 10: Ripple Delete 完善 | 剪辑闭环 | 小 |
| 13 | Task 11: 测试补全（剩余部分） | 防回归 | 小 |

---

## 通用执行约束

执行以上任何 Task 时，必须遵守：

1. **TimelineLayoutEngine 是坐标唯一来源** — 禁止在 ClipItem/TimelineView 中手算坐标
2. **所有可撤销编辑必须走 CommandStack 和 `_execute_command()`** — 不要直接改 TimelineSequence
3. **`paint()` 只绘制** — 不解码、不发请求、不改 scene 状态、不发信号
4. **缩略图/波形/音频解码要有取消过期请求和缓存上限**
5. **每次改动后运行对应测试**，并说明未能运行的原因
6. **小步提交**，每个 Task 独立 commit

---

## 完成定义

所有 Task 完成后，时间线应达到：

1. Track visibility 正确影响预览和导出
2. 导出文件包含混音后的音频流
3. 播放时有实时电平表
4. 100 clip / 20 track 下滚动、缩放、播放不卡顿（benchmark PASS）
5. Clip 可拖拽 fade handle 创建淡入淡出
6. 转场可拖放到切点并可视化
7. Clip 可变速播放且导出一致
8. Marker 可编辑、导航、吸附
9. 轨道可增删改移
10. Ripple delete 处理 linked A/V 和多选
11. 所有新功能有对应测试覆盖
