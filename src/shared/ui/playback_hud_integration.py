# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Playback HUD Integration Module
播放 HUD 与视频播放系统的集成指南和辅助函数。

## 集成方式

PlaybackHUD 可以通过以下方式集成：

1. **VideoSession 集成**: 在 VideoSession 中自动收集性能数据
2. **EnhancedVideoPlaybackEngine 集成**: 在播放引擎中自动更新 HUD
3. **MainWindow 集成**: 在主窗口中显示 HUD 面板

## 使用示例

### 方式一：直接集成到 MainWindow

```python
from src.shared.ui.playback_hud import PlaybackHUDPanel

class MainWindow(QMainWindow):
    def __init__(self):
        # 创建 HUD 面板
        self.playback_hud = PlaybackHUDPanel()
        self.playback_hud.setParent(self)

        # 连接快捷键
        self.hud_action = QAction("显示播放 HUD", self)
        self.hud_action.setShortcut("Ctrl+Shift+H")
        self.hud_action.triggered.connect(self.playback_hud.toggle_visibility)
        self.addAction(self.hud_action)

    def on_video_loaded(self, path, metadata, pipeline_decision):
        # 开始新会话
        self.playback_hud.start_session(path, metadata, pipeline_decision)

    def on_play_tick(self):
        # 更新性能数据
        self.playback_hud.update_from_monitor(get_performance_monitor())
```

### 方式二：通过 VideoSession 集成

```python
from src.shared.ui.playback_hud import PlaybackHUDModel, PlaybackHUDPanel
from src.shared.video.session import VideoSession
from src.shared.media.performance_monitor import get_performance_monitor

# 创建 VideoSession
session = VideoSession()
session.load('/path/to/video.mp4')

# 创建 HUD Model
hud_model = PlaybackHUDModel()
hud_model.start_session(
    video_path='/path/to/video.mp4',
    metadata=session.metadata,
    pipeline_decision=pipeline_decision,
    hwaccel_status=session.source_manager.get_hwaccel_status(),
)

# 在播放 tick 中更新
def on_play_tick():
    hud_model.update_from_monitor(get_performance_monitor())
    hud_model.update_from_session(session)

    # 更新 HUD 面板显示
    if hud_panel.is_visible():
        hud_panel._update_display()
```

## 数据流

```
VideoSession                    VideoPerformanceMonitor
    |                                    |
    | metadata                           | decode/upload/tick 统计
    | source_type                        | queue_depth
    | hwaccel_status                     | degradation_level
    v                                    v
PlaybackHUDModel  <--aggregate stats-->  PlaybackHUDPanel
    |                                    |
    | 统计聚合                            | UI 显示
    | 导出日志                            | 导出按钮
    v                                    v
JSON Log File                         User
```

## 性能开销

PlaybackHUD 的设计目标是低开销：

- 数据收集：使用已有的 VideoPerformanceMonitor，无额外开销
- UI 更新：定时更新（默认 500ms），避免频繁刷新
- 百分位计算：仅在更新时计算，不影响播放 tick

建议：
- 仅在需要调试时显示 HUD
- 导出日志用于后续分析，而不是实时监控
"""

from __future__ import annotations

import logging
from typing import Optional, Any

from PyQt6.QtWidgets import QWidget, QMainWindow, QDockWidget
from PyQt6.QtGui import QAction
from PyQt6.QtCore import Qt
from src.shared.ui.panel_divider import PanelDockWidget

from src.shared.ui.playback_hud import PlaybackHUDPanel, PlaybackHUDModel
from src.shared.media.performance_monitor import get_performance_monitor

logger = logging.getLogger(__name__)


# =============================================================================
# PlaybackHUDController: 简化集成
# =============================================================================

class PlaybackHUDController:
    """
    播放 HUD 控制器。

    简化 PlaybackHUD 与视频播放系统的集成，提供：
    - 自动连接 VideoSession
    - 自动更新 PerformanceMonitor
    - 快捷键管理
    - Dock 部件管理

    使用方式：
    ```python
    # 创建控制器
    hud_controller = PlaybackHUDController(main_window)

    # 连接 VideoSession
    hud_controller.connect_session(video_session)

    # 在播放开始时调用
    hud_controller.on_playback_started(path, metadata, pipeline_decision)

    # 在播放 tick 时自动更新（通过定时器）
    ```
    """

    def __init__(
        self,
        main_window: Optional[QMainWindow] = None,
        parent_widget: Optional[QWidget] = None,
        auto_create_dock: bool = True,
    ) -> None:
        """
        初始化控制器。

        Args:
            main_window: 主窗口（用于添加 Dock 和菜单）
            parent_widget: 父控件（如果不想使用 Dock）
            auto_create_dock: 是否自动创建 Dock 部件
        """
        self._main_window = main_window
        self._parent_widget = parent_widget
        self._session: Optional[Any] = None
        self._monitor = get_performance_monitor()

        # 创建 HUD 面板
        self._panel = PlaybackHUDPanel(parent_widget)

        # Dock 部件（如果使用）
        self._dock: Optional[QDockWidget] = None

        if main_window and auto_create_dock:
            self._create_dock_widget()

        # 快捷键
        self._shortcut_action: Optional[QAction] = None
        if main_window:
            self._create_shortcut_action()

        logger.debug("PlaybackHUDController initialized")

    @property
    def panel(self) -> PlaybackHUDPanel:
        """获取 HUD 面板"""
        return self._panel

    @property
    def model(self) -> PlaybackHUDModel:
        """获取数据模型"""
        return self._panel.model

    def _create_dock_widget(self) -> None:
        """创建 Dock 部件"""
        if self._main_window is None:
            return

        self._dock = PanelDockWidget("播放性能监控", self._main_window)
        self._dock.setWidget(self._panel)
        self._dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea |
            Qt.DockWidgetArea.RightDockWidgetArea |
            Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self._dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable |
            QDockWidget.DockWidgetFeature.DockWidgetMovable
        )

        # 添加到右侧
        self._main_window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)

        # 默认隐藏
        self._dock.setVisible(False)

    def _create_shortcut_action(self) -> None:
        """创建快捷键动作"""
        if self._main_window is None:
            return

        from src.shared.i18n.i18n import I18N

        self._shortcut_action = QAction(I18N.tr("hud.show_action", "显示播放 HUD"), self._main_window)
        self._shortcut_action.setShortcut("Ctrl+Shift+H")
        self._shortcut_action.setCheckable(True)
        self._shortcut_action.setChecked(False)
        self._shortcut_action.triggered.connect(self.toggle_visibility)

        # 添加到窗口
        self._main_window.addAction(self._shortcut_action)

        # 如果有视图菜单，添加到菜单
        # view_menu = self._main_window.findChild(QMenu, "view_menu")
        # if view_menu:
        #     view_menu.addAction(self._shortcut_action)

    def toggle_visibility(self) -> None:
        """切换 HUD 可见性"""
        self._panel.toggle_visibility()

        if self._dock:
            self._dock.setVisible(self._panel.is_visible())

        if self._shortcut_action:
            self._shortcut_action.setChecked(self._panel.is_visible())

    def show_hud(self) -> None:
        """显示 HUD"""
        self._panel.show_hud()

        if self._dock:
            self._dock.setVisible(True)

        if self._shortcut_action:
            self._shortcut_action.setChecked(True)

    def hide_hud(self) -> None:
        """隐藏 HUD"""
        self._panel.hide_hud()

        if self._dock:
            self._dock.setVisible(False)

        if self._shortcut_action:
            self._shortcut_action.setChecked(False)

    def connect_session(self, session: Any) -> None:
        """
        连接 VideoSession。

        Args:
            session: VideoSession 实例
        """
        self._session = session

    def on_playback_started(
        self,
        video_path: str,
        metadata: Any,
        pipeline_decision: Any,
        hwaccel_status: Any = None,
    ) -> None:
        """
        播放开始事件处理。

        Args:
            video_path: 视频文件路径
            metadata: 视频元数据
            pipeline_decision: 管线决策
            hwaccel_status: 硬件解码状态
        """
        # 开始新会话
        self._panel.start_session(
            video_path,
            metadata,
            pipeline_decision,
            hwaccel_status,
        )

        # 设置源类型
        # AUD-38004: source_manager 是外部对象（VideoSession），其私有属性
        # （_disk_proxy_source/_proxy_source/_master_source）不保证存在 ——
        # 此前裸属性访问在缺属性的 session/桩对象上直接 AttributeError。
        # 全部改为 getattr 防御访问；缺属性按"无该源"处理，探测方法
        # （is_planar_mode）用 callable() 防御。
        source_manager = getattr(self._session, "source_manager", None)
        if source_manager is not None:
            disk_proxy = getattr(source_manager, "_disk_proxy_source", None)
            proxy_source = getattr(source_manager, "_proxy_source", None)
            master_source = getattr(source_manager, "_master_source", None)

            # disk_proxy/memory_proxy/master_only
            if disk_proxy is not None:
                source_type = 'disk_proxy'
            elif proxy_source is not None and proxy_source is not master_source:
                source_type = 'memory_proxy'
            else:
                source_type = 'master_only'

            # frame_path (使用 RenderFramePath 枚举值)
            frame_path = 'unknown'
            is_planar = getattr(proxy_source, "is_planar_mode", None)
            if callable(is_planar):
                if is_planar():
                    frame_path = 'planar_yuv_r8_upload'
                else:
                    frame_path = 'packed_rgb_cpu_upload'

            self._panel.model.set_source_type(source_type, frame_path)

        logger.info(
            f"PlaybackHUD session started: {video_path}, "
            f"pipeline={self._panel.model.pipeline_info.pipeline}"
        )

    def on_play_tick(self) -> None:
        """
        播放 tick 事件处理。

        自动从 PerformanceMonitor 和 VideoSession 更新数据。
        """
        if self._session:
            self._panel.update_from_session(self._session)

        self._panel.update_from_monitor(self._monitor)

    def on_playback_stopped(self) -> None:
        """
        播放停止事件处理。

        可选择自动导出日志。
        """
        # 可选：自动导出日志
        # if self._panel.model.perf_stats.frames_displayed > 0:
        #     self._panel.export_log()

    def export_log(self, output_path: Optional[str] = None) -> str:
        """
        导出性能日志。

        Args:
            output_path: 输出路径（可选）

        Returns:
            导出文件路径
        """
        return self._panel.export_log(output_path)


# =============================================================================
# 工具函数
# =============================================================================

def create_playback_hud_for_main_window(
    main_window: QMainWindow,
    session: Optional[Any] = None,
    add_shortcut: bool = True,
) -> PlaybackHUDController:
    """
    为主窗口创建播放 HUD。

    Args:
        main_window: 主窗口
        session: VideoSession（可选）
        add_shortcut: 是否添加快捷键

    Returns:
        PlaybackHUDController 实例
    """
    controller = PlaybackHUDController(
        main_window=main_window,
        auto_create_dock=True,
    )

    if session:
        controller.connect_session(session)

    return controller


def get_hud_report_text(model: PlaybackHUDModel) -> str:
    """
    获取 HUD 报告文本。

    用于控制台输出或日志记录。

    Args:
        model: PlaybackHUDModel 实例

    Returns:
        格式化的报告文本
    """
    pipeline_info = model.pipeline_info
    perf_stats = model.perf_stats

    lines = [
        "=== 播放性能报告 ===",
        "",
        "--- 管线信息 ---",
        f"  Pipeline: {pipeline_info.pipeline}",
        f"  Backend:  {pipeline_info.backend}",
        f"  Codec:    {pipeline_info.codec}",
        f"  PixFmt:   {pipeline_info.pixel_format}",
        f"  Source:   {pipeline_info.source_type}",
        f"  Proxy:    {pipeline_info.proxy_size}",
        f"  FramePath:{pipeline_info.frame_path}",
        f"  HWAccel:  {pipeline_info.hwaccel}",
        f"  Fallback: {pipeline_info.fallback_reason}",
        "",
        "--- 性能统计 ---",
        f"  Decode:   {perf_stats.decode_ms.format_display()}",
        f"  Upload:   {perf_stats.upload_ms.format_display()}",
        f"  Render:   {perf_stats.render_ms.format_display()}",
        f"  UI Tick:  {perf_stats.ui_tick_ms.format_display()}",
        f"  Queue:    p95={perf_stats.queue_depth.p95:.0f}",
        "",
        "--- 帧统计 ---",
        f"  Displayed:{perf_stats.frames_displayed}",
        f"  Dropped:  {perf_stats.frames_dropped}",
        f"  Held:     {perf_stats.frames_held}",
        f"  FPS:      {perf_stats.fps:.1f}",
        f"  CacheHit: {perf_stats.cache_hit_rate:.1%}",
        f"  Duration: {perf_stats.session_duration:.1f}s",
        f"  DegLevel: {perf_stats.degradation_level}",
    ]

    return "\n".join(lines)


# =============================================================================
# 测试入口
# =============================================================================

if __name__ == '__main__':
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    # 测试控制器
    controller = PlaybackHUDController()
    controller.show_hud()

    # 模拟数据
    controller.on_playback_started(
        video_path='/test/video.mp4',
        metadata=None,
        pipeline_decision=None,
    )

    # 打印报告
    print(get_hud_report_text(controller.model))

    sys.exit(app.exec())
