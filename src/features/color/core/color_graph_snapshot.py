"""ColorGraphSnapshot —— 渲染请求的不可变图快照（N04，B08.1）。

``freeze_graph`` 在**提交渲染请求的瞬间**深复制图 + 一次求值 frame /
working color context / viewer target / 源内容 revision。此后 UI 线程
对活模型的任何编辑都不影响正在执行的快照（计划 §5.1：渲染线程不访问
UI 活模型）。

快照携带请求身份（``request_id`` / ``session_id`` / ``graph_revision`` /
``source_frame``），供执行器（B09）做取消匹配与缓存键。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.shared.project.color_graph import ColorGraphData


@dataclass(frozen=True)
class ColorGraphSnapshot:
    """不可变渲染输入（deepcopy 冻结；无 Qt/GL 引用）。"""

    request_id: str
    session_id: str
    graph: ColorGraphData
    graph_revision: int
    source_frame: int
    source_content_revision: int
    working_color_context: Mapping[str, Any]
    viewer_target: str                     # "output" 或 "viewer:<node_id>"
    quality: str = "full"
    roi: tuple[float, float, float, float] | None = None
    output_size: tuple[int, int] = (0, 0)  # 0 = 源尺寸

    def cache_key(self) -> tuple:
        """编译缓存键：拓扑/参数版本/帧/上下文/目标（§5.3）。"""
        return (
            self.graph.graph_id,
            self.graph_revision,
            self.source_frame,
            self.source_content_revision,
            tuple(sorted(self.working_color_context.items())),
            self.viewer_target,
            self.quality,
            self.roi,
            self.output_size,
        )


def freeze_graph(
    graph: ColorGraphData,
    *,
    session_id: str,
    source_frame: int = 0,
    source_content_revision: int = 0,
    working_color_context: Mapping[str, Any] | None = None,
    viewer_target: str = "output",
    quality: str = "full",
    roi=None,
    output_size: tuple[int, int] = (0, 0),
    request_id: str | None = None,
) -> ColorGraphSnapshot:
    """提交瞬间的深复制冻结（B08.1 唯一入口）。"""
    frozen_graph = graph.deepcopy()
    return ColorGraphSnapshot(
        request_id=request_id or str(uuid.uuid4()),
        session_id=session_id,
        graph=frozen_graph,
        graph_revision=graph.graph_revision,
        source_frame=int(source_frame),
        source_content_revision=int(source_content_revision),
        working_color_context=dict(
            working_color_context
            if working_color_context is not None
            else graph.working_color_context),
        viewer_target=viewer_target,
        quality=quality,
        roi=roi,
        output_size=tuple(output_size),
    )
