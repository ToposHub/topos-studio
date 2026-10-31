#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Timeline Preview - 独立时间线 UI 预览工具

用于单独打开时间线面板查看视频/音频轨道绘制效果。

运行方式:
    cd "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02"
    python tools/timeline_preview.py
"""

import sys
import os

# 添加项目根目录到 Python 路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout
from PyQt6.QtCore import Qt

# 导入时间线数据模型
from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TransitionInstance,
)
from src.shared.media.metadata import Rational

# 导入时间线视图
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.edit_style import timeline_style


def create_mock_timeline() -> TimelineSequence:
    """
    创建模拟时间线数据。

    包含:
    - 2 个视频轨道（各 3 个片段）
    - 2 个音频轨道（各 2 个片段）
    - 不同颜色、时长、位置
    """
    # 视频轨道 1
    video_track_1 = EditTrack(
        track_id="v1",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(
            ClipInstance(
                clip_id="v1_c1",
                asset_id="asset_video_1",
                track_id="v1",
                timeline_start=0,
                timeline_duration=120,  # 5秒 @ 24fps
                source_start=0,
                source_duration=120,
            ),
            ClipInstance(
                clip_id="v1_c2",
                asset_id="asset_video_2",
                track_id="v1",
                timeline_start=130,  # 留 10 帧间隙
                timeline_duration=180,  # 7.5秒
                source_start=0,
                source_duration=180,
            ),
            ClipInstance(
                clip_id="v1_c3",
                asset_id="asset_video_3",
                track_id="v1",
                timeline_start=320,
                timeline_duration=96,  # 4秒
                source_start=0,
                source_duration=96,
            ),
        ),
    )

    # 视频轨道 2
    video_track_2 = EditTrack(
        track_id="v2",
        name="Video 2",
        track_type="video",
        index=1,
        clips=(
            ClipInstance(
                clip_id="v2_c1",
                asset_id="asset_video_4",
                track_id="v2",
                timeline_start=50,
                timeline_duration=60,  # 2.5秒
                source_start=0,
                source_duration=60,
            ),
            ClipInstance(
                clip_id="v2_c2",
                asset_id="asset_video_5",
                track_id="v2",
                timeline_start=200,
                timeline_duration=100,  # 约4秒
                source_start=0,
                source_duration=100,
            ),
        ),
    )

    # 音频轨道 1
    audio_track_1 = EditTrack(
        track_id="a1",
        name="Audio 1",
        track_type="audio",
        index=0,
        clips=(
            ClipInstance(
                clip_id="a1_c1",
                asset_id="asset_audio_1",
                track_id="a1",
                timeline_start=0,
                timeline_duration=300,  # 12.5秒
                source_start=0,
                source_duration=300,
            ),
            ClipInstance(
                clip_id="a1_c2",
                asset_id="asset_audio_2",
                track_id="a1",
                timeline_start=310,
                timeline_duration=150,  # 6.25秒
                source_start=0,
                source_duration=150,
            ),
        ),
    )

    # 音频轨道 2
    audio_track_2 = EditTrack(
        track_id="a2",
        name="Audio 2",
        track_type="audio",
        index=1,
        clips=(
            ClipInstance(
                clip_id="a2_c1",
                asset_id="asset_audio_3",
                track_id="a2",
                timeline_start=100,
                timeline_duration=200,  # 8.3秒
                source_start=0,
                source_duration=200,
            ),
        ),
    )

    # 创建时间线序列
    timeline = TimelineSequence(
        timeline_id="mock_timeline_001",
        name="Mock Timeline Preview",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(video_track_1, video_track_2),
        audio_tracks=(audio_track_1, audio_track_2),
    )

    return timeline


class TimelinePreviewWindow(QMainWindow):
    """时间线预览窗口"""

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("Timeline Preview - Topos Color")
        self.setGeometry(100, 100, 1200, 400)

        # 创建中央 widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 布局
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # 创建时间线视图
        self.timeline_view = TimelineView()
        self.timeline_view.setStyleSheet(timeline_style())

        layout.addWidget(self.timeline_view)

        # 加载模拟时间线
        mock_timeline = create_mock_timeline()
        self.timeline_view.load_timeline(mock_timeline)

        # 设置初始缩放
        self.timeline_view.set_zoom(5.0)  # 5 pixels per frame


def main() -> int:
    """主入口"""
    app = QApplication(sys.argv)

    # 设置应用样式
    app.setStyle("Fusion")

    # 创建并显示窗口
    window = TimelinePreviewWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())