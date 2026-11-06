"""KF-02：调色关键帧面板只读宿主（权威快照回填 + 播放头双向同步）。

职责边界（写入口保持关闭，KF-03 起开放领域命令）：

- **下行**：从绑定栈权威图（ColorGraphAppHost.bound_graph）+ 会话身份
  + 播放上下文构建只读 ``Snapshot`` 推给面板；video 帧回调按 clip-local
  域回填播放头（``set_playhead`` 不发 seek，天然无回声）。
- **上行**：``seek_requested`` 复用既有 Color seek 协议；ruler 拖拽手势
  边界（scrub_started/finished）路由到视口非阻塞 scrub 车道，move 合并
  与 release 精确帧由该车道既有逻辑保证（与 tracker_graph 同模式）。
- **只读**：``edit_requested`` 核对会话身份后丢弃并记录日志——过期
  context 的请求不产生任何工程副作用。

会话/切片段/版本/Undo 更换后由宿主 ``refresh()`` 重建投影；context 或
graph_revision 未变化时跳过重复推送。无真实播放上下文（无 clip、无图）
时保留面板演示数据，不推空快照。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from src.features.color.core.color_graph_param_source import (
    node_layer_payload,
)
from src.features.color.core.keyframe_tracks import (
    decode_keyframe,
    parse_track_name,
)
from src.features.color.ui.color_keyframes_view_state import (
    EditRequest,
    Marker,
    Snapshot,
    Track,
    tr,
)

logger = logging.getLogger(__name__)


def _corrector_types() -> tuple:
    from src.shared.project.color_graph import NodeType
    return (NodeType.CORRECTOR, NodeType.SHARED_CORRECTOR)


def _session_context_key(session: Any, stack_id: str) -> str:
    """会话身份键：project|timeline|clip|stack 任一变化都换 context。"""
    if session is None:
        return ""
    parts = (
        getattr(session, "project_id", "") or "",
        getattr(session, "timeline_id", "") or "",
        getattr(session, "clip_id", "") or "",
        stack_id or getattr(session, "color_stack_id", "") or "",
    )
    return "|".join(parts)


def _leaf_label(domain: str, field: str, shapes: Any) -> str:
    """叶轨显示名（display-only，不参与轨道身份）。"""
    if domain == "window":
        for shape in shapes or ():
            if isinstance(shape, dict) and shape.get("name"):
                return f"{shape['name']} · {field}"
        return f"{tr('window', 'Window')} · {field}"
    return field


def build_keyframes_snapshot(
    *,
    context: str,
    frame_count: int,
    timecode_fps: int,
    playhead: int,
    corrector: str,
    graph: Any,
) -> Snapshot:
    """从权威图对象构建只读快照（纯函数，宿主与测试共用）。

    每个正确体节点一行（图序），叶轨来自 ``node_layer_payload`` 的合并
    关键帧（params 域覆盖 legacy 域，与执行求值同源）。锁 = params.locked，
    启停 = 节点 enabled（白圆），auto 留待 KF-04 录制路由（恒 False）。
    Master 汇总行由面板按子轨聚合，不在快照内。
    """
    rows: list[Track] = []
    first_row = ""
    for node in getattr(graph, "nodes", ()) or ():
        try:
            if node.known_type() not in _corrector_types():
                continue
        except Exception:  # noqa: BLE001 — 未知类型节点不阻断投影
            continue
        node_id = node.node_id
        payload = node_layer_payload(node, include_lut_cache=False) or {}
        shapes = payload.get("mask_shapes") or ()
        leaves: list[Track] = []
        for name, track_data in sorted(
                (payload.get("grade_keyframes") or {}).items()):
            # 图域轨道经 freeze_params 是 mappingproxy，不能用 dict 判型
            if not isinstance(track_data, Mapping):
                continue
            address = parse_track_name(str(name))
            if address is None:
                continue  # 未登记轨道不投影（工具覆盖清单的可见原因之外）
            markers: list[Marker] = []
            for kf in track_data.get("keyframes") or ():
                if not isinstance(kf, Mapping):
                    continue
                try:
                    decoded = decode_keyframe(kf)
                except ValueError:
                    continue  # 畸形关键帧不阻断其余投影
                markers.append(Marker(int(decoded.frame), decoded.kind,
                                      decoded.start, decoded.end))
            if not markers:
                continue  # 空轨道不出行（与图命令删空即移除语义一致）
            leaf_id = f"node:{node_id}|{name}"
            leaves.append(Track(
                id=leaf_id,
                label=_leaf_label(address.domain, address.field, shapes),
                scope="color",
                markers=tuple(markers),
            ))
        row_id = f"node:{node_id}"
        label = str(getattr(node, "label", "") or payload.get("label")
                    or f"{tr('corrector', 'Corrector')} {len(rows) + 1}")
        rows.append(Track(
            id=row_id,
            label=label,
            scope="color",
            children=tuple(leaves),
            locked=bool((getattr(node, "params", {}) or {}).get("locked")),
            enabled=bool(getattr(node, "enabled", True)),
        ))
        if not first_row:
            first_row = row_id
    active = corrector if any(row.id == corrector for row in rows) else first_row
    return Snapshot(
        context=context,
        frame_count=max(0, int(frame_count)),
        timecode_fps=max(1, int(timecode_fps)),
        playhead=max(0, int(playhead)),
        corrector=active,
        tracks=tuple(rows),
    )


class ColorKeyframesHost:
    """关键帧面板宿主：投影回填 + 播放头同步 + 只读意图收口。"""

    def __init__(
        self,
        panel: Any,
        *,
        graph_provider: Callable[[], Any],
        active_node_provider: Callable[[], str],
        session_provider: Callable[[], Any],
        playback: Any,
    ) -> None:
        self._panel = panel
        self._graph_provider = graph_provider
        self._active_node_provider = active_node_provider
        self._session_provider = session_provider
        self._playback = playback
        self._context = ""
        self._revision = -1
        self._frame_count = 0
        self._last_frame = 0
        self._last_seek_frame = 0
        self._scrubbing = False
        self._scrub_lane_started = False
        panel.seek_requested.connect(self._on_seek_requested)
        panel.scrub_started.connect(self._on_scrub_started)
        panel.scrub_finished.connect(self._on_scrub_finished)
        panel.edit_requested.connect(self._on_edit_requested)

    # ── 下行：快照与播放头 ──

    def refresh(self, *, force: bool = False) -> bool:
        """重建并回填快照；返回是否实际推送。

        context 或 graph_revision 未变化时跳过（幂等）；无会话/无播放
        上下文/无图时保留面板现有（演示）数据。
        """
        session = self._session_provider()
        playback = self._playback
        total = None
        clip_fps = 0
        context_obj = None
        try:
            context_obj = playback._active_color_timeline_clip_playback_context()
        except Exception:  # noqa: BLE001 — 播放上下文不可用按无上下文处理
            context_obj = None
        if context_obj:
            total = playback._color_timeline_clip_total_frames()
            clip_fps = int(getattr(context_obj.get("clip"), "fps", 0) or 0)
        graph = self._graph_provider()
        if session is None or not total or graph is None:
            return False
        context = _session_context_key(
            session, getattr(session, "color_stack_id", "") or "")
        revision = int(getattr(graph, "graph_revision", -1) or -1)
        if not force and context == self._context and revision == self._revision:
            return False
        playhead = max(0, min(self._last_frame, int(total) - 1))
        snapshot = build_keyframes_snapshot(
            context=context,
            frame_count=int(total),
            timecode_fps=clip_fps or 24,
            playhead=playhead,
            corrector=self._active_node_row_id(),
            graph=graph,
        )
        self._panel.set_snapshot(snapshot)
        self._context = context
        self._revision = revision
        self._frame_count = snapshot.frame_count
        return True

    def on_local_frame_changed(self, frame: int) -> None:
        """video 帧钩子（clip-local 域）：只刷新播放头，不重建轨道树。"""
        frame = int(frame)
        self._last_frame = frame
        if not self._context:
            return  # 演示快照：不回填，避免把演示播放头拖离 fixture
        if self._frame_count:
            frame = max(0, min(frame, self._frame_count - 1))
        self._panel.set_playhead(frame)

    # ── 上行：seek / scrub / 只读编辑意图 ──

    def _on_seek_requested(self, frame: int) -> None:
        frame = int(frame)
        self._last_seek_frame = frame
        playback = self._playback
        if self._scrubbing:
            if not self._scrub_lane_started:
                self._scrub_lane_started = True
                playback.on_video_scrub_started(frame)
            else:
                playback.on_video_scrub_moved(frame)
        else:
            playback.on_video_seek(frame)

    def _on_scrub_started(self) -> None:
        self._scrubbing = True
        self._scrub_lane_started = False

    def _on_scrub_finished(self) -> None:
        if not self._scrubbing:
            return
        self._scrubbing = False
        if self._scrub_lane_started:
            self._scrub_lane_started = False
            # release 的最后一帧经 scrub 车道 settle（与 tracker 同约定）
            self._playback.on_video_scrub_finished(self._last_seek_frame)

    def _on_edit_requested(self, request: Any) -> None:
        """KF-02 只读：核对会话身份后丢弃（不产生工程副作用）。"""
        if not isinstance(request, EditRequest):
            return
        if request.context != self._context:
            logger.debug("keyframes edit dropped: stale context %r",
                         request.context)
            return
        logger.info(
            "keyframes edit ignored (read-only wiring, KF-03 opens the "
            "real entry): action=%s targets=%d", request.action,
            len(request.targets))

    # ── 内部 ──

    def _active_node_row_id(self) -> str:
        try:
            node_id = self._active_node_provider() or ""
        except Exception:  # noqa: BLE001 — 活动节点不可用按无选择处理
            node_id = ""
        return f"node:{node_id}" if node_id else ""

    @property
    def context(self) -> str:
        """当前已推送快照的会话身份键（空 = 尚未推送真实投影）。"""
        return self._context
