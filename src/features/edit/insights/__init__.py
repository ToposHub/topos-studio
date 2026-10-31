"""Edit Insights —— 只读跨序列洞察工具包（T 线，计划 T01+）。

本包对当前工程做**只读**聚合：跨序列联合搜索（T10）、镜头/对白/marker
总表（T11）、素材使用与剪辑节奏统计（T12）、联系表（T13）、审片资料包
（T14）与快照对照（T15）。

边界纪律（docs/upgrade_plan_T_third_machine_2026-10-24.md §2）：

- 不导入 ``src.app.*``；宿主状态经注入的只读 adapter 进入（T02/G1）。
- core 查询/报表/统计只接本包 DTO（``dto.InsightSnapshot``），不直接
  持有工程模型对象、QObject 或 GPU 资源。
- 只读报告中不存在的字段写"未收集/未提供"，不默认补 0 冒充事实。
"""

from src.features.edit.insights.dto import (
    SCHEMA_VERSION,
    AssetEntry,
    ClipEntry,
    CueEntry,
    FpsSpec,
    InsightSnapshot,
    MarkerEntry,
    ProjectEntry,
    SequenceEntry,
    TrackEntry,
)
from src.features.edit.insights.capture import capture_readonly_state

__all__ = [
    "SCHEMA_VERSION",
    "AssetEntry",
    "ClipEntry",
    "CueEntry",
    "FpsSpec",
    "InsightSnapshot",
    "MarkerEntry",
    "ProjectEntry",
    "SequenceEntry",
    "TrackEntry",
    "capture_readonly_state",
]
