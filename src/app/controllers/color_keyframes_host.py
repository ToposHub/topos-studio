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
    WINDOW_TRACK_FIELDS,
    decode_keyframe,
    expected_value_size,
    mask_track_name,
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

#: 值采样哨兵：字段不在载荷中（与合法的 None/0 值区分）。
_UNRESOLVED = object()


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


def _leaf_label(domain: str, field: str, shapes: Any,
                owner_id: str = "") -> str:
    """叶轨显示名（display-only，不参与轨道身份）。"""
    if domain == "window":
        for shape in shapes or ():
            if (isinstance(shape, dict)
                    and owner_id and str(shape.get("id")) == owner_id):
                name = shape.get("name")
                if name:
                    return f"{name} · {field}"
                break
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
    auto_nodes: frozenset = frozenset(),
) -> Snapshot:
    """从权威图对象构建只读快照（纯函数，宿主与测试共用）。

    每个正确体节点一行（图序），叶轨来自 ``node_layer_payload`` 的合并
    关键帧（params 域覆盖 legacy 域，与执行求值同源）。锁 = params.locked，
    启停 = 节点 enabled（白圆），auto 留待 KF-04 录制路由（恒 False）。
    Master 汇总行由面板按子轨聚合，不在快照内。
    """
    rows: list[Track] = []
    first_row = ""
    from src.features.color.core.keyframe_tracks import (
        GRADE_TRACK_FIELDS,
        QUALIFIER_TRACK_FIELDS,
        qualifier_track_name,
    )
    for node in getattr(graph, "nodes", ()) or ():
        try:
            if node.known_type() not in _corrector_types():
                continue
        except Exception:  # noqa: BLE001 — 未知类型节点不阻断投影
            continue
        node_id = node.node_id
        payload = node_layer_payload(node, include_lut_cache=False) or {}
        shapes = payload.get("mask_shapes") or ()
        # 既有轨道 → 标记（图域 mappingproxy 与 legacy dict 并存）
        existing: dict[str, tuple[Any, list[Marker]]] = {}
        for name, track_data in (payload.get("grade_keyframes") or {}).items():
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
            existing[str(name)] = (address, markers)

        def _leaf(domain: str, track_name: str, field: str,
                  markers: list[Marker], owner_id: str = "") -> Track:
            return Track(id=f"node:{node_id}|{track_name}",
                         label=_leaf_label(domain, field, shapes, owner_id),
                         scope="color", markers=tuple(markers),
                         auto=node_id in auto_nodes)  # Corrector 开关作用后代

        leaves: list[Track] = []
        # grade/qualifier：登记字段全量常驻（空轨道无标记）——否则首个
        # 关键帧无处可加（面板只对树内叶轨发请求）。窗口域同策略：
        # 每个实际形状 × 登记字段常驻（形状删除 → 叶行随载荷消失）。
        for spec in GRADE_TRACK_FIELDS:
            _, markers = existing.get(spec.name, (None, []))
            if not markers and spec.name in existing:
                continue  # 有轨道无有效标记（畸形载荷）：不显示空行
            leaves.append(_leaf("grade", spec.name, spec.name, markers))
        for spec in QUALIFIER_TRACK_FIELDS:
            name = qualifier_track_name(spec.name)
            _, markers = existing.get(name, (None, []))
            if not markers and name in existing:
                continue
            leaves.append(_leaf("qualifier", name, spec.name, markers))
        shape_ids = [str(s.get("id")) for s in (shapes or ())
                     if isinstance(s, dict) and s.get("id")]
        for shape_id in shape_ids:
            for spec in WINDOW_TRACK_FIELDS:
                name = mask_track_name(shape_id, spec.name)
                _, markers = existing.get(name, (None, []))
                if not markers and name in existing:
                    continue
                leaves.append(_leaf("window", name, spec.name, markers,
                                    shape_id))
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
            auto=node_id in auto_nodes,
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
    """关键帧面板宿主：投影回填 + 播放头同步 + 编辑意图 → 领域命令。

    KF-03：写入口经 ``submit_batch``（batch_edit_keyframes 批处理命令）
    与 ``submit_action``（lock/enable 既有命令）开放；同一用户动作一个
    历史条目。``submit_batch``/``submit_action`` 缺席（未注入）时保持
    KF-02 只读语义：核对会话身份后丢弃并记录日志。
    """

    def __init__(
        self,
        panel: Any,
        *,
        graph_provider: Callable[[], Any],
        active_node_provider: Callable[[], str],
        session_provider: Callable[[], Any],
        playback: Any,
        submit_batch: Optional[Callable[[list[dict]], bool]] = None,
        submit_action: Optional[Callable[[str, dict], bool]] = None,
        auto_recorder: Any = None,
    ) -> None:
        self._panel = panel
        self._graph_provider = graph_provider
        self._active_node_provider = active_node_provider
        self._session_provider = session_provider
        self._playback = playback
        self._submit_batch = submit_batch
        self._submit_action = submit_action
        self._recorder = auto_recorder  # KF-04：自动录制注册表（可缺席）
        self._context = ""
        self._revision = -1
        self._frame_count = 0
        self._last_frame = 0
        self._last_seek_frame = 0
        self._scrubbing = False
        self._scrub_lane_started = False
        # 正式剪贴板（计划 §edit_requested）：复制时捕获不可变载荷
        # （值/动态属性/能力签名），粘贴不读已删改的源；跨 context 粘贴
        # 必须有捕获。
        self._clipboard_entries: dict[tuple[str, int], dict] = {}
        self._clipboard_source_context = ""
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
        if context != self._context:
            # 切片段/版本/页面：丢弃旧会话的录制开关与半开手势
            if self._recorder is not None:
                self._recorder.reset()
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
            auto_nodes=(self._recorder.auto_nodes()
                        if self._recorder is not None else frozenset()),
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
        """编辑意图 → 领域命令（KF-03 真实写入口）。

        会话身份核对后翻译：关键帧动作合并为一个批处理命令（一次
        提交、一个历史条目）；lock/enable 走既有节点级命令；auto 留待
        KF-04 录制路由。无 submit 通道（宿主未注入）或翻译结果为空
        时保持只读语义并记录原因。
        """
        if not isinstance(request, EditRequest):
            return
        if request.context != self._context:
            logger.debug("keyframes edit dropped: stale context %r",
                         request.context)
            return
        if request.action == "copy":
            # 复制时捕获不可变载荷（正式跨片段剪贴板）
            self._capture_clipboard(request)
            return
        if request.action == "paste" and not request.sources:
            logger.info("keyframes paste dropped: no sources")
            return
        if (request.action == "paste"
                and request.source_context != self._context
                and not self._clipboard_entries):
            # 跨 context 粘贴必须有复制时捕获的载荷——不读已离开片段
            # 的源（同 context 且无捕获时回退实时读源）。
            logger.info("keyframes paste dropped: no captured payload for "
                        "cross-context paste (%r != %r)",
                        request.source_context, self._context)
            return
        translated = self._translate_request(request)
        if translated is None:
            return
        edits, node_actions, skipped, auto_toggles = translated
        for node_id, enabled in auto_toggles:
            if self._recorder is not None:
                self._recorder.set_auto(node_id, enabled)
                # 会话域开关：不推 revision，强制重建投影刷新 auto 列
                self.refresh(force=True)
        if skipped:
            logger.info(
                "keyframes edit skipped targets (not writable yet): %s",
                sorted(set(skipped)))
        if edits and self._submit_batch is not None:
            accepted = self._submit_batch(edits)
            if not accepted:
                logger.info("keyframes batch rejected by domain (no partial "
                            "write): action=%s", request.action)
        elif edits:
            logger.info(
                "keyframes edit ignored (no submit channel): action=%s "
                "targets=%d", request.action, len(edits))
        for action, arguments in node_actions:
            if self._submit_action is not None:
                self._submit_action(action, arguments)
            else:
                logger.info("keyframes node action ignored (no channel): %s",
                            action)

    # ── EditRequest → 领域语言 ──

    def _translate_request(self, request: EditRequest):
        """把面板请求翻译为 (批处理条目, 节点级命令, 跳过原因)。

        值由宿主从权威图当前载荷采样（面板只发 intent，值归宿主——
        计划 §UI 契约）；窗口/Sizing/FX/Master 轨道不可写目标原样跳过
        并记录（与工具覆盖清单一致）。
        """
        graph = self._graph_provider()
        if graph is None:
            return None
        nodes = {node.node_id: node for node in getattr(graph, "nodes", ())}
        edits: list[dict] = []
        node_actions: list[tuple[str, dict]] = []
        auto_toggles: list[tuple[str, bool]] = []
        skipped: list[str] = []
        locked_state: dict[str, bool] = {}
        enabled_state: dict[str, bool] = {}
        if request.action == "paste":
            if not self._translate_paste(request, nodes, edits):
                return None
            return (edits, node_actions, skipped, auto_toggles)
        for track_id, frame in request.targets:
            parsed = self._parse_track_id(track_id)
            if parsed is None:
                skipped.append(track_id)
                continue
            node_id, address = parsed
            if request.action in ("lock", "enable"):
                # 面板行按钮：node 行直发，叶行映射回父节点
                if request.action == "lock":
                    locked_state[node_id] = request.enabled
                else:
                    enabled_state[node_id] = request.enabled
                continue
            if request.action == "auto":
                # KF-04：Corrector 级开关（作用于全部子轨，写入只落在
                # 本次手势改动的参数）；会话域状态，非工程命令
                auto_toggles.append((node_id, request.enabled))
                continue
            if address is None or address.domain not in (
                    "grade", "qualifier", "window"):
                skipped.append(track_id)  # Sizing/FX/节点行等不可写目标
                continue
            # 命令域一律用 canonical 轨道名（qualifier.*/mask.<id>.<f>）——
            # 裸字段名不在批命令白名单内（会被 illegal_param_key 整批拒）
            param = address.track_name
            if request.action == "add":
                value = self._current_value(nodes.get(node_id), address)
                if value is _UNRESOLVED:
                    skipped.append(track_id)
                    continue
                edits.append({
                    "action": "set", "node_id": node_id, "param": param,
                    "frame": int(frame), "value": value,
                    "interpolation": ("hold" if request.kind == "static"
                                      else "linear"),
                })
            elif request.action == "delete":
                edits.append({"action": "remove", "node_id": node_id,
                              "param": param, "frame": int(frame)})
            elif request.action == "move":
                edits.append({"action": "move", "node_id": node_id,
                              "param": param, "frame": int(frame),
                              "to_frame": int(frame) + int(request.delta)})
            elif request.action == "convert":
                edits.append({"action": "dynamics", "node_id": node_id,
                              "param": param, "frame": int(frame),
                              "kind": request.kind})
            elif request.action == "dynamics":
                edits.append({"action": "dynamics", "node_id": node_id,
                              "param": param, "frame": int(frame),
                              "kind": "dynamic",
                              "start": float(request.start),
                              "end": float(request.end)})
            else:
                skipped.append(track_id)
        if locked_state:
            node_actions.append(("lock_node", {
                "node_ids": sorted(locked_state),
                "locked": next(iter(locked_state.values())),
            }))
        if enabled_state:
            node_actions.append(("set_enabled", {
                "node_ids": sorted(enabled_state),
                "enabled": next(iter(enabled_state.values())),
            }))
        return (edits, node_actions, skipped, auto_toggles)

    def _translate_paste(self, request: EditRequest, nodes: dict,
                         edits: list[dict]) -> bool:
        """粘贴：按序对齐目标写入捕获/实时的源键载荷（深拷贝语义）。

        优先用复制时捕获的不可变载荷（源删改/源片段离开不失效）；
        无捕获且同 context 时回退实时读源；跨 context 无捕获 → 整批
        拒绝。能力签名（值分量数）与目标轨不匹配 → 清晰拒绝（计划
        §剪贴板规则 8）。
        """
        for (src_track, src_frame), (dst_track, dst_frame) in zip(
                request.sources, request.targets):
            src = self._parse_track_id(src_track)
            dst = self._parse_track_id(dst_track)
            if (src is None or dst is None
                    or src[1] is None or dst[1] is None):
                logger.info("keyframes paste dropped: unparsable ref")
                return False
            src_node, src_address = src
            dst_node, dst_address = dst
            if src_address.domain not in ("grade", "qualifier", "window"):
                logger.info("keyframes paste dropped: source domain %s",
                            src_address.domain)
                return False
            entry = self._clipboard_entries.get(
                (src_track, int(src_frame)))
            if entry is None:
                if request.source_context != self._context:
                    logger.info(
                        "keyframes paste dropped: source key %s@%s not "
                        "captured (cross-context)", src_track, src_frame)
                    return False
                entry = self._resolve_live_source(
                    nodes, src_node, src_address, int(src_frame))
                if entry is None:
                    logger.info(
                        "keyframes paste dropped: source key %s@%s gone",
                        src_track, src_frame)
                    return False
            if (expected_value_size(dst_address.track_name)
                    != entry["size"]):
                logger.info(
                    "keyframes paste dropped: value arity mismatch "
                    "%s → %s", src_address.track_name,
                    dst_address.track_name)
                return False
            edits.append({
                "action": "set", "node_id": dst_node,
                "param": dst_address.track_name,
                "frame": int(dst_frame), "value": entry["value"],
                "interpolation": entry["interpolation"],
                "kind": entry["kind"], "start": entry["start"],
                "end": entry["end"],
            })
        return True

    def _capture_clipboard(self, request: EditRequest) -> None:
        """复制时捕获不可变载荷（值/动态属性/能力签名，计划 §8）。

        任一源解析失败 → 放弃本次捕获（保留旧捕获；粘贴时按缺捕获
        规则回退或拒绝），不产生半份剪贴板。
        """
        graph = self._graph_provider()
        if graph is None:
            return
        nodes = {node.node_id: node for node in getattr(graph, "nodes", ())}
        entries: dict[tuple[str, int], dict] = {}
        for track_id, frame in request.targets:
            parsed = self._parse_track_id(track_id)
            if parsed is None or parsed[1] is None:
                continue
            node_id, address = parsed
            resolved = self._resolve_live_source(
                nodes, node_id, address, int(frame))
            if resolved is None:
                logger.info("keyframes copy capture incomplete: %s@%s",
                            track_id, frame)
                return
            entries[(track_id, int(frame))] = resolved
        if entries:
            self._clipboard_entries = entries
            self._clipboard_source_context = (
                request.source_context or request.context)

    def _resolve_live_source(self, nodes: dict, node_id: str,
                             address: Any, frame: int) -> Optional[dict]:
        """从当前权威图解析一个源键 → 不可变载荷条目（无则 None）。"""
        payload = self._payload_of(nodes.get(node_id)) or {}
        track = (payload.get("grade_keyframes") or {}).get(
            address.track_name)
        keyframes = (track or {}).get("keyframes") or ()
        entry = next((k for k in keyframes
                      if isinstance(k, Mapping)
                      and int(k.get("frame", -1)) == int(frame)), None)
        if entry is None:
            return None
        try:
            decoded = decode_keyframe(dict(entry))
        except ValueError:
            return None
        return {
            "value": decoded.value,
            "kind": decoded.kind, "start": decoded.start,
            "end": decoded.end, "interpolation": decoded.interpolation,
            "size": expected_value_size(address.track_name),
            "src_node": node_id, "src_track": address.track_name,
        }

    # ── 解析辅助 ──

    @staticmethod
    def _parse_track_id(track_id: str):
        """面板轨道 id → (node_id, TrackAddress|None)；不可解析为 None。

        id 形态：``node:{node_id}|{track_name}``（叶，携带完整轨道
        地址——窗口域含 shape_id）与 ``node:{node_id}``（节点行，
        lock/enable 用，address=None）。
        """
        if not isinstance(track_id, str) or not track_id.startswith("node:"):
            return None
        rest = track_id[len("node:"):]
        node_id, sep, track_name = rest.partition("|")
        if not node_id:
            return None
        if not sep:
            return (node_id, None)
        address = parse_track_name(track_name)
        if address is None:
            return None
        return (node_id, address)

    def _payload_of(self, node: Any):
        if node is None:
            return None
        try:
            return node_layer_payload(node, include_lut_cache=False)
        except Exception:  # noqa: BLE001 — 载荷不可读按未解析处理
            return None

    def _current_value(self, node: Any, address: Any):
        """从节点当前载荷采样参数现值（添加键=当前值，计划 §录制规则 7）。

        grade → grading_params[field]；qualifier → qualifier_params[field]；
        window → 形状 dict 的字段现值（owner_id 定位形状）。
        """
        payload = self._payload_of(node)
        if payload is None:
            return _UNRESOLVED
        if address.domain == "grade":
            params = payload.get("grading_params") or {}
            if address.field not in params:
                return _UNRESOLVED
            return params[address.field]
        if address.domain == "qualifier":
            params = payload.get("qualifier_params") or {}
            if address.field not in params:
                return _UNRESOLVED
            return params[address.field]
        # window：形状字段现值
        for shape in payload.get("mask_shapes") or ():
            if (isinstance(shape, Mapping)
                    and str(shape.get("id")) == address.owner_id):
                if address.field not in shape:
                    return _UNRESOLVED
                return shape[address.field]
        return _UNRESOLVED

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
