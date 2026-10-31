"""
Audio Mixer 未实现功能的特性开关（AM-11 阶段 0.3）。

AM-11 的处理原则：在功能真正实现前，UI 入口必须**禁用并明确标注"未实现"**，
而不是"可点击但仅记日志后伪装成功"。

本模块集中管理这些开关，未来实现对应功能时只需把标志改为 True，
相关 UI 入口自动恢复可用，避免散落在多处的硬编码。

开关约定：
- True  = 功能已实现，UI 入口可用
- False = 功能未实现，UI 入口应禁用并标注

E17: PURGE_CACHE_IMPLEMENTED 已删除——purge cache 信号/处理器是
"有声明无入口"的 no-op（全仓无 emit），按第五轮审计 E17 清理；
真正实现缓存清理时随新入口重新引入。
"""

from __future__ import annotations


# AM-11: Record Arm（录音预备）—— VO/ADR 录音。
# 阶段 4.4 已完整实现：
#   核心基础设施（recording_manager.py）：输入设备枚举、pre-roll、take 命名、
#   BWF/WAV 落盘、latency calibration、失败恢复。
#   UI 接线（edit_controller）：R 按钮 → RecordingManager → 录音 → take 导入 asset。
# 现已置 True，R 按钮可点击录音。
RECORD_ARM_IMPLEMENTED: bool = True
