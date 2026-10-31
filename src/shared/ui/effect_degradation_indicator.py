# P1-COLOR-13 起接线：挂在调色页色彩监看状态栏（ColorMonitorStatusBar）内，
# 提供播放降级状态显示与全局开关。
"""
Effect Degradation Indicator - 播放时效果降级 UI 指示器

P3-4: 在播放时显示被降级的效果列表，并提供全局开关控制。

功能：
1. 显示当前被降级的效果名称列表
2. 提供全局开关控制降级功能
3. 播放状态变化时自动更新显示
4. 导出时不受影响（自动恢复完整质量）

使用方式：
    indicator = EffectDegradationIndicator()
    # 添加到 MainWindow 或视频控制面板

参考：
- docs/edit_page_timeline_architecture.md P3-4
"""

from __future__ import annotations

import logging
from typing import Optional
from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QCheckBox,
    QFrame,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from src.shared.effects.preview_quality import (
    get_effect_preview_quality_manager,
    EffectPreviewQualityManager,
)
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


class EffectDegradationIndicator(QWidget):
    """
    播放时效果降级 UI 指示器。

    显示：
    - 降级状态图标（播放时激活）
    - 被降级的效果列表
    - 全局开关控制

    Signals:
        degradation_toggled: 用户切换降级功能状态 (enabled: bool)
    """

    degradation_toggled = pyqtSignal(bool)  # 降级功能开关状态

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化指示器"""
        super().__init__(parent)

        self._manager = get_effect_preview_quality_manager()
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_display)
        self._update_timer.start(500)  # 每 500ms 更新

        self._setup_ui()
        self._update_display()

    def _setup_ui(self) -> None:
        """设置 UI"""
        # 主布局
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(4, 2, 4, 2)
        main_layout.setSpacing(8)

        # 状态指示图标/标签
        self._status_label = QLabel()
        self._status_label.setFixedWidth(20)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setToolTip(
            I18N.tr(
                "effect_degradation.status_tooltip",
                "播放时效果降级状态：绿色=播放中降级激活，灰色=暂停或禁用"
            )
        )

        # 降级效果列表标签
        self._effects_label = QLabel()
        self._effects_label.setStyleSheet("color: #ffaa00; font-size: 11px;")
        self._effects_label.setToolTip(
            I18N.tr(
                "effect_degradation.effects_tooltip",
                "播放时被降级的效果列表"
            )
        )

        # 全局开关复选框
        self._enable_checkbox = QCheckBox(
            I18N.tr("effect_degradation.enable_label", "降级")
        )
        self._enable_checkbox.setChecked(self._manager.is_degradation_enabled())
        self._enable_checkbox.stateChanged.connect(self._on_checkbox_changed)
        self._enable_checkbox.setToolTip(
            I18N.tr(
                "effect_degradation.enable_tooltip",
                "播放时自动降级重型效果以保持流畅性\n"
                "关闭此选项将在播放时也保持完整质量（可能影响帧率）"
            )
        )

        # 添加到布局
        main_layout.addWidget(self._status_label)
        main_layout.addWidget(self._effects_label)
        main_layout.addWidget(self._enable_checkbox)

        # 设置整体样式
        self.setStyleSheet("""
            EffectDegradationIndicator {
                background-color: transparent;
                border: none;
            }
        """)

    def _update_display(self) -> None:
        """更新显示内容"""
        summary = self._manager.get_degradation_summary()
        is_playing = summary["is_playing"]
        degradation_enabled = summary["degradation_enabled"]
        degraded_count = summary["degraded_count"]
        degraded_effects = summary["degraded_effects"]

        # 更新状态图标
        if is_playing and degradation_enabled and degraded_count > 0:
            # 播放中且有降级效果 -> 显示活跃状态（黄色）
            self._status_label.setText("⚡")
            self._status_label.setStyleSheet("color: #ffaa00; font-weight: bold;")
            self._effects_label.setText(
                f"{I18N.tr('effect_degradation.effects_prefix', '跳过')}: "
                f"{', '.join(degraded_effects[:3])}"
                + (f"... ({degraded_count})" if degraded_count > 3 else "")
            )
            self._effects_label.setVisible(True)
        elif is_playing and not degradation_enabled:
            # 播放中但禁用降级 -> 显示警告（红色）
            self._status_label.setText("⚠")
            self._status_label.setStyleSheet("color: #ff6b6b;")
            self._effects_label.setText(
                I18N.tr("effect_degradation.disabled_warning", "完整质量播放（可能影响帧率）")
            )
            self._effects_label.setStyleSheet("color: #ff6b6b; font-size: 11px;")
            self._effects_label.setVisible(True)
        else:
            # 暂停或无降级 -> 显示正常（灰色）
            self._status_label.setText("●")
            self._status_label.setStyleSheet("color: #888888;")
            self._effects_label.setVisible(False)

        # 更新复选框状态（不触发信号）
        self._enable_checkbox.blockSignals(True)
        self._enable_checkbox.setChecked(degradation_enabled)
        self._enable_checkbox.blockSignals(False)

    def _on_checkbox_changed(self, state: int) -> None:
        """复选框状态变化"""
        enabled = state == Qt.CheckState.Checked.value
        self._manager.set_degradation_enabled(enabled)
        self.degradation_toggled.emit(enabled)
        self._update_display()
        logger.info(f"P3-4: Effect degradation toggled: {enabled}")

    def set_playback_state(self, is_playing: bool) -> None:
        """
        设置播放状态（外部调用接口）。

        Args:
            is_playing: 是否正在播放
        """
        # PlaybackCoordinator 已经设置了状态，这里只更新显示
        self._update_display()

    def get_manager(self) -> EffectPreviewQualityManager:
        """获取效果预览质量管理器"""
        return self._manager


class EffectDegradationPanel(QWidget):
    """
    效果降级设置面板。

    用于偏好设置或视频设置面板中，提供完整的降级配置。

    包含：
    - 全局开关
    - 效果降级列表
    - 自定义阈值设置
    """

    degradation_settings_changed = pyqtSignal(dict)  # 设置变化信号

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化面板"""
        super().__init__(parent)

        self._manager = get_effect_preview_quality_manager()
        self._setup_panel_ui()

    def _setup_panel_ui(self) -> None:
        """设置面板 UI"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 8)
        main_layout.setSpacing(8)

        # 标题
        title_label = QLabel(
            I18N.tr("effect_degradation.panel_title", "播放时效果降级")
        )
        title_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        main_layout.addWidget(title_label)

        # 描述
        desc_label = QLabel(
            I18N.tr(
                "effect_degradation.panel_desc",
                "播放时自动跳过或降级重型效果以保持流畅性。\n"
                "暂停/Seek/导出时恢复完整质量，不影响最终输出。"
            )
        )
        desc_label.setStyleSheet("color: #888888; font-size: 11px;")
        desc_label.setWordWrap(True)
        main_layout.addWidget(desc_label)

        # 全局开关
        self._enable_checkbox = QCheckBox(
            I18N.tr("effect_degradation.enable_global", "启用播放时效果降级")
        )
        self._enable_checkbox.setChecked(self._manager.is_degradation_enabled())
        self._enable_checkbox.stateChanged.connect(self._on_global_toggle)
        main_layout.addWidget(self._enable_checkbox)

        # 效果列表框架
        effects_frame = QFrame()
        effects_frame.setStyleSheet("""
            QFrame {
                background-color: #2a2a2a;
                border: 1px solid #444444;
                border-radius: 4px;
            }
        """)
        effects_layout = QVBoxLayout(effects_frame)
        effects_layout.setContentsMargins(8, 8, 8, 8)
        effects_layout.setSpacing(4)

        # 效果列表标题
        effects_title = QLabel(
            I18N.tr("effect_degradation.effects_list_title", "可被降级的效果")
        )
        effects_title.setStyleSheet("font-size: 11px; color: #aaaaaa;")
        effects_layout.addWidget(effects_title)

        # 效果列表内容
        self._effects_list_label = QLabel()
        self._effects_list_label.setStyleSheet("font-size: 11px;")
        self._effects_list_label.setWordWrap(True)
        effects_layout.addWidget(self._effects_list_label)
        self._update_effects_list()

        main_layout.addWidget(effects_frame)

        # 提示
        hint_label = QLabel(
            I18N.tr(
                "effect_degradation.hint",
                "提示：导出时不受影响，始终使用完整质量。"
            )
        )
        hint_label.setStyleSheet("color: #ffaa00; font-size: 10px;")
        main_layout.addWidget(hint_label)

        main_layout.addStretch()

    def _update_effects_list(self) -> None:
        """更新效果列表显示"""
        effects = self._manager.get_all_heavy_effects()
        lines = []

        for effect_type, definition in effects.items():
            quality_name = definition.preview_quality.name
            ui_label = definition.ui_label or effect_type
            lines.append(f"• {ui_label} ({quality_name})")

        self._effects_list_label.setText("\n".join(lines))

    def _on_global_toggle(self, state: int) -> None:
        """全局开关变化"""
        enabled = state == Qt.CheckState.Checked.value
        self._manager.set_degradation_enabled(enabled)
        self.degradation_settings_changed.emit({
            "degradation_enabled": enabled
        })

    def refresh_settings(self) -> None:
        """刷新设置显示"""
        # AUD-38002: 程序化 setChecked 按 Qt 语义会发射 stateChanged（伪信号），
        # 触发 _on_global_toggle 回写 manager 并外发 degradation_settings_changed，
        # 与同文件 _update_display（164-166 行）的正确范式一致，这里同样加信号锁。
        self._enable_checkbox.blockSignals(True)
        self._enable_checkbox.setChecked(self._manager.is_degradation_enabled())
        self._enable_checkbox.blockSignals(False)
        self._update_effects_list()