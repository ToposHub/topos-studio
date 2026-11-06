"""ColorGraphService —— 图编辑/验证的领域服务（N02，B04）。

纯 CPU、无 Qt/GL/engine。每个操作都是**一次原子事务**：

    验证前置条件 → 构建候选图（新对象，不碰原图）→ 候选图全量校验
    → 通过：返回新图（render 语义变更 bump graph_revision）；
      失败：返回原 图 + 稳定诊断（图完全不变，可恢复）。

诊断沿用 :class:`GraphSchemaError` 的稳定 code；``apply_intent`` 是
N08 规范化 intent 的统一入口（B01 ACTION_CONTRACTS.command 分派）。

边 ID 由端点 uuid5 派生（同端点同 ID——重连/重建确定性）；节点 ID 由
可注入 ``id_factory`` 分配（测试可注入确定性工厂）。
"""

from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorGraphUiState,
    ColorNodeData,
    GraphSchemaError,
    NodeType,
    PortDirection,
    PortKind,
    SharedDefinitionData,
    unfreeze_params,
)


@dataclass(frozen=True)
class GraphEditResult:
    """一次事务的结果：成功带新图与 ID 映射；失败原图原样带回。"""

    ok: bool
    graph: ColorGraphData
    diagnostics: tuple[GraphSchemaError, ...] = ()
    id_mapping: Mapping[str, str] = field(default_factory=dict)

    @property
    def primary_code(self) -> str | None:
        return self.diagnostics[0].code if self.diagnostics else None


def _edge_id(src: str, src_port: str, dst: str, dst_port: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"topos-color-edge/{src}/{src_port}->{dst}/{dst_port}"))


def _make_edge(src: str, src_port: str, dst: str, dst_port: str,
               slot_id: str = "") -> ColorEdgeData:
    return ColorEdgeData(edge_id=_edge_id(src, src_port, dst, dst_port),
                         src_node=src, src_port=src_port,
                         dst_node=dst, dst_port=dst_port, slot_id=slot_id)


class ColorGraphService:
    """结构/参数/布局编辑服务（B04 全量操作 + B05 command 的执行体）。"""

    def __init__(self,
                 id_factory: Callable[[], str] | None = None,
                 effect_schema_provider: Callable[[str], Any] | None = None,
                 lut_reference_validator: Callable[[str], bool] | None = None,
                 bake_reference_validator: Callable[[str], bool] | None = None) -> None:
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        # R03：资源权威注入（生产默认 PluginManager/LutResourceSession，
        # 测试注入 fake）。None 值 = 用懒默认；显式传入 False 禁用校验。
        self._effect_schema_provider = effect_schema_provider
        self._lut_reference_validator = lut_reference_validator
        self._bake_reference_validator = bake_reference_validator

    # ── 统一 intent 入口（N08 → N02） ──

    def apply_intent(self, graph: ColorGraphData, command: str,
                     arguments: Mapping[str, Any]) -> GraphEditResult:
        """按 B01 契约表的 command 名分派；未知 command 显式拒绝。"""
        arguments = dict(arguments)
        compound_path = tuple(arguments.pop("_compound_path", ()))
        if compound_path:
            compound = self._node(graph, str(compound_path[0]))
            if compound is None or compound.compound_children is None:
                return self._fail(graph, "node_not_found", "compound 路径不存在")
            child_result = self.apply_intent(
                compound.compound_children, command,
                {**arguments, "_compound_path": compound_path[1:]})
            if not child_result.ok:
                return GraphEditResult(False, graph, child_result.diagnostics)
            replacement = replace(compound, compound_children=child_result.graph)
            candidate = replace(graph, nodes=tuple(
                replacement if node.node_id == compound.node_id else node
                for node in graph.nodes))
            # Layout-only child updates do not invalidate root pixels.
            render_change = (child_result.graph.graph_revision !=
                             compound.compound_children.graph_revision)
            return self._commit(graph, candidate, child_result.id_mapping,
                                render_change=render_change)
        handler = getattr(self, f"op_{command}", None)
        if handler is None or command.startswith("_"):
            return GraphEditResult(
                False, graph,
                (GraphSchemaError("unknown_command",
                                  f"未实现的图命令: {command}"),))
        return handler(graph, **dict(arguments)) if arguments else handler(graph)

    # ── 内部工具 ──

    def _fail(self, graph: ColorGraphData, code: str, message: str, *,
              node_id: str | None = None) -> GraphEditResult:
        return GraphEditResult(False, graph,
                               (GraphSchemaError(code, message, node_id=node_id),))

    def _commit(self, graph: ColorGraphData, candidate: ColorGraphData,
                mapping: Mapping[str, str], *,
                render_change: bool = True) -> GraphEditResult:
        errors = candidate.validate()
        if errors:
            return GraphEditResult(False, graph, tuple(errors))
        if render_change:
            candidate = replace(candidate,
                                graph_revision=graph.graph_revision + 1)
        return GraphEditResult(True, candidate, (), mapping)

    def _node(self, graph: ColorGraphData, node_id: str) -> ColorNodeData | None:
        return next((n for n in graph.nodes if n.node_id == node_id), None)

    def _edges_into(self, graph: ColorGraphData, node_id: str,
                    port: str | None = None) -> list[ColorEdgeData]:
        return [e for e in graph.edges
                if e.dst_node == node_id and (port is None or e.dst_port == port)]

    def _edges_out(self, graph: ColorGraphData, node_id: str,
                   port: str | None = None) -> list[ColorEdgeData]:
        return [e for e in graph.edges
                if e.src_node == node_id and (port is None or e.src_port == port)]

    def _upstream_edge(self, graph: ColorGraphData,
                       node_id: str) -> ColorEdgeData | None:
        ins = self._edges_into(graph, node_id, "image")
        return ins[0] if len(ins) == 1 else None

    def _downstream_edge(self, graph: ColorGraphData,
                         node_id: str) -> ColorEdgeData | None:
        outs = self._edges_out(graph, node_id, "image")
        return outs[0] if len(outs) == 1 else None

    def _check_port(self, graph: ColorGraphData, node_id: str, port_id: str,
                    direction: PortDirection) -> GraphSchemaError | None:
        node = self._node(graph, node_id)
        if node is None:
            return GraphSchemaError("node_not_found",
                                    f"节点不存在: {node_id}", node_id=node_id)
        spec = next((p for p in node.ports()
                     if p.port_id == port_id
                     and p.direction is direction), None)
        if spec is None:
            return GraphSchemaError(
                "port_not_found",
                f"端口 {node_id}.{port_id}({direction.value}) 不存在",
                node_id=node_id)
        return None

    # ════════════════════════════════════════════════════════════════
    # 串行插入 / 加节点 / 连接
    # ════════════════════════════════════════════════════════════════

    def op_insert_corrector(self, graph: ColorGraphData,
                            active_node_id: str | None = None,
                            before: bool = False, *,
                            position: Sequence[float] | None = None,
                            positions: Mapping[str, Sequence[float]] | None = None,
                            ) -> GraphEditResult:
        """在 active 节点之后（或之前）插入空 Corrector（serial 命令）。

        策略：anchor 是 Corrector 时按其 image 边拆入；anchor 是
        Source/Output 时在相邻边内插入；无合法插入点返回诊断。
        """
        anchor_id = active_node_id or graph.source_node_id
        anchor = self._node(graph, anchor_id)
        if anchor is None:
            return self._fail(graph, "node_not_found",
                              f"插入锚点不存在: {anchor_id}", node_id=anchor_id)
        if before:
            edge = self._upstream_edge(graph, anchor_id)
            if edge is None:
                return self._fail(graph, "edge_endpoint_missing",
                                  f"{anchor_id} 无上游 image 边，无法 before 插入",
                                  node_id=anchor_id)
        else:
            edge = self._downstream_edge(graph, anchor_id)
            if edge is None:
                return self._fail(graph, "edge_endpoint_missing",
                                  f"{anchor_id} 无下游 image 边，无法 after 插入",
                                  node_id=anchor_id)

        return self._insert_corrector_on_edge(graph, edge, position, positions)

    def _insert_corrector_on_edge(
        self, graph: ColorGraphData, edge: ColorEdgeData,
        position: Sequence[float] | None = None,
        positions: Mapping[str, Sequence[float]] | None = None,
        *, label: str = "",
    ) -> GraphEditResult:
        new_id = self._id_factory()
        node = ColorNodeData(node_id=new_id, node_type=NodeType.CORRECTOR.value,
                             params={"key_source_mode": "node_input"}, label=label)
        kept = tuple(e for e in graph.edges if e.edge_id != edge.edge_id)
        downstream = _make_edge(new_id, "image", edge.dst_node, edge.dst_port, edge.slot_id)
        if any(e.dst_node == edge.dst_node and e.dst_port == edge.dst_port
               and e.slot_id == edge.slot_id for e in kept):
            # A legacy mixer can share a slot ID. The compiler orders ties by edge ID.
            downstream = replace(edge, src_node=new_id, src_port="image")
        edges = kept + (
            _make_edge(edge.src_node, edge.src_port, new_id, "image"),
            downstream,
        )
        candidate = replace(graph, nodes=graph.nodes + (node,), edges=edges)
        layout = dict(positions or {})
        if position is not None:
            layout[new_id] = position
        if layout:
            updated = self.op_update_layout(candidate, layout)
            if not updated.ok:
                return GraphEditResult(False, graph, updated.diagnostics)
            candidate = updated.graph
        return self._commit(graph, candidate, {new_id: new_id})

    # ═══ N10：节点 grade 域关键帧（实例 params；CORRECTOR 专属）═══

    @staticmethod
    def _normalized_keyframe_track(track: Any) -> list:
        """关键帧序列化形态校验/规范化（[{frame,value,interpolation}]）。"""
        if not isinstance(track, Mapping):
            return []
        raw = track.get("keyframes")
        if not isinstance(raw, (list, tuple)):
            return []
        normalized = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            frame = int(item.get("frame", -1))
            value = item.get("value")
            if frame < 0 or value is None:
                continue
            entry = {"frame": frame, "value": value}
            interp = str(item.get("interpolation", "linear"))
            entry["interpolation"] = interp if interp in (
                "linear", "ease", "hold") else "linear"
            normalized.append(entry)
        normalized.sort(key=lambda e: e["frame"])
        return normalized

    #: N10-B：关键帧可动画轨道名白名单（grade ∪ qualifier；窗口实例轨
    #: 经 track_name_is_writable 解析谓词准入，不入可枚举集）。
    def _keyframe_track_whitelist(self) -> frozenset:
        from src.features.color.core.keyframe_tracks import (
            node_command_track_whitelist,
        )
        return node_command_track_whitelist()

    def _keyframe_target_or_fail(self, graph: ColorGraphData,
                                 node_id: str,
                                 param: str | None = None) -> Any:
        """N10 目标校验：Corrector/SharedCorrector 实例（N10-B 扩 shared
        实例级；定义级扇出/compound 成员仍 fail-closed）。"""
        node = self._node(graph, node_id)
        if node is None or node.known_type() not in (
                NodeType.CORRECTOR, NodeType.SHARED_CORRECTOR):
            return self._fail(graph, "node_not_found",
                              f"关键帧目标不是 Corrector 实例: {node_id}",
                              node_id=node_id)
        if param is not None:
            from src.features.color.core.keyframe_tracks import (
                parse_track_name, track_name_is_writable,
            )
            if not track_name_is_writable(param):
                return self._fail(graph, "illegal_param_key",
                                  f"关键帧轨道名不在白名单: {param}",
                                  node_id=node_id)
            address = parse_track_name(param)
            if address is not None and address.domain == "window":
                # 窗口实例轨：目标形状必须存在于节点载荷（合并视图）——
                # 删除的形状不复活轨道（形状删除随 Layer 域事务清理）
                from src.features.color.core.color_graph_param_source import (
                    node_layer_payload,
                )
                shapes = ((node_layer_payload(node,
                                              include_lut_cache=False) or {})
                          .get("mask_shapes") or ())
                if not any(isinstance(s, dict)
                           and str(s.get("id")) == address.owner_id
                           for s in shapes):
                    return self._fail(
                        graph, "illegal_param_key",
                        f"窗口形状不存在: {param}", node_id=node_id)
        if node.params.get("locked"):
            return self._fail(graph, "node_locked",
                              "节点已锁定，先解锁再编辑关键帧",
                              node_id=node_id)
        return node

    def _patch_node_keyframes(self, graph: ColorGraphData, node_id: str,
                              mutate, *, param: str | None = None) -> GraphEditResult:
        node = self._keyframe_target_or_fail(graph, node_id, param)
        if not isinstance(node, ColorNodeData):
            return node  # type: ignore[return-value]
        source = node.params.get("grade_keyframes") or {}
        tracks = {str(k): self._normalized_keyframe_track(v)
                  for k, v in source.items() if isinstance(v, Mapping)}
        mutate(tracks)
        # KF-05：删空轨道的墓碑语义（与批处理命令一致）——legacy 基底
        # 存在同名轨道或原为墓碑时写显式空覆盖，其余图域私有轨道删空
        # 即消失。
        legacy_kf = getattr(node, "legacy_payload", None) or {}
        raw_legacy = (legacy_kf.get("grade_keyframes")
                      if isinstance(legacy_kf, Mapping) else None)
        legacy_names = {str(k) for k, v in (raw_legacy or {}).items()
                        if isinstance(v, Mapping) and v.get("keyframes")}
        new_kf: dict[str, Any] = {}
        for name, keyframes in tracks.items():
            if keyframes:
                new_kf[name] = {"keyframes": keyframes}
            elif name in legacy_names or (
                    isinstance(source.get(name), Mapping)
                    and source[name].get("tombstone")):
                new_kf[name] = {"keyframes": [], "tombstone": True}
        params = dict(node.params)
        params["grade_keyframes"] = new_kf
        try:
            new_node = replace(node, params=params)
        except GraphSchemaError as exc:
            return self._fail(graph, exc.code, str(exc), node_id=node_id)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        return self._commit(graph, replace(graph, nodes=nodes), {})

    def op_set_node_keyframe(self, graph: ColorGraphData, node_id: str,
                             param: str, frame: int, value: Any,
                             interpolation: str = "linear",
                             ) -> GraphEditResult:
        """N10：插入/更新节点 grade 域关键帧（源帧域；一 op 一事务）。"""
        def mutate(tracks: dict) -> None:
            track = list(tracks.get(str(param)) or [])
            entry = {"frame": int(frame), "value": value,
                     "interpolation": str(interpolation)}
            track = [k for k in track if k["frame"] != int(frame)]
            track.append(entry)
            track.sort(key=lambda e: e["frame"])
            tracks[str(param)] = track
        return self._patch_node_keyframes(graph, node_id, mutate,
                                          param=str(param))

    def op_remove_node_keyframe(self, graph: ColorGraphData, node_id: str,
                                param: str, frame: int,
                                ) -> GraphEditResult:
        def mutate(tracks: dict) -> None:
            track = [k for k in (tracks.get(str(param)) or [])
                     if k["frame"] != int(frame)]
            if track:
                tracks[str(param)] = track
            else:
                tracks.pop(str(param), None)
        return self._patch_node_keyframes(graph, node_id, mutate,
                                          param=str(param))

    def op_move_node_keyframe(self, graph: ColorGraphData, node_id: str,
                              param: str, from_frame: int, to_frame: int,
                              ) -> GraphEditResult:
        def mutate(tracks: dict) -> None:
            track = list(tracks.get(str(param)) or [])
            moving = [k for k in track if k["frame"] == int(from_frame)]
            if not moving:
                return
            track = [k for k in track if k["frame"] != int(from_frame)]
            for k in moving:
                k = dict(k)
                k["frame"] = int(to_frame)
                track.append(k)
            track.sort(key=lambda e: e["frame"])
            tracks[str(param)] = track
        return self._patch_node_keyframes(graph, node_id, mutate,
                                          param=str(param))

    # ═══ KF-03：关键帧批处理领域命令（一次校验、一个候选、一次 commit）═══

    #: 批处理条目动作全集（面板 EditRequest → 宿主翻译后的领域形态）。
    BATCH_KEYFRAME_ACTIONS = ("set", "remove", "move", "dynamics")

    @staticmethod
    def _normalized_keyframe_track_dynamic(track: Any) -> list:
        """与 ``_normalized_keyframe_track`` 同校验容错，但保留显式键型
        （kind/start/end 走 KF-01 编解码：等价档位不落盘，非等价档位
        显式持久化）。畸形条目逐条跳过（与既有容错一致）。"""
        from src.features.color.core.keyframe_tracks import (
            decode_keyframe, encode_keyframe,
        )
        if not isinstance(track, Mapping):
            return []
        raw = track.get("keyframes")
        if not isinstance(raw, (list, tuple)):
            return []
        normalized = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            try:
                normalized.append(encode_keyframe(decode_keyframe(dict(item))))
            except (ValueError, TypeError):
                continue
        normalized.sort(key=lambda e: e["frame"])
        return normalized

    def op_batch_edit_keyframes(self, graph: ColorGraphData,
                                edits: Sequence[Mapping[str, Any]],
                                ) -> GraphEditResult:
        """关键帧批处理命令：多节点/多轨道/多帧一次事务。

        条目形态（宿主翻译 EditRequest 后的领域语言；窗口实例轨经
        ``track_name_is_writable`` 谓词准入 + 形状存在性校验；
        Sizing/FX 轨道未登记不可写）::

            {"action": "set",     "node_id", "param", "frame",
             "value", "interpolation"?, "kind"?, "start"?, "end"?}
            {"action": "remove",  "node_id", "param", "frame"}
            {"action": "move",    "node_id", "param", "frame", "to_frame"}
            {"action": "dynamics","node_id", "param", "frame",
             "kind", "start"?, "end"?}

        语义（计划 §轨道规则 5）：
        - 一次校验 fail-closed：任一条目非法（锁定节点/白名单外/值形态/
          权重越界/移动目标冲突）整批拒绝，无部分写入；
        - 批量 move 先移除全部源帧再插入目标帧（相邻帧交换不自覆盖）；
        - 空批 / 全部条目为重复置值或原位（候选与现状逐字节一致）不推
          revision、不入史。
        """
        from src.features.color.core.keyframe_tracks import (
            KEY_KINDS, decode_keyframe, encode_keyframe, make_keyframe,
            validate_dynamic_weights, validate_value_shape,
        )
        if not isinstance(edits, (list, tuple)) or not edits:
            return GraphEditResult(True, graph, ())  # 空动作不入史
        grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        for index, edit in enumerate(edits):
            if not isinstance(edit, Mapping):
                return self._fail(graph, "illegal_arguments",
                                  f"edits[{index}] 不是 Mapping")
            action = str(edit.get("action", ""))
            if action not in self.BATCH_KEYFRAME_ACTIONS:
                return self._fail(graph, "illegal_arguments",
                                  f"edits[{index}] action 非法: {action!r}")
            node_id = str(edit.get("node_id", ""))
            param = str(edit.get("param", ""))
            frame = int(edit.get("frame", -1))
            node = self._keyframe_target_or_fail(graph, node_id, param)
            if not isinstance(node, ColorNodeData):
                return node  # type: ignore[return-value]
            if frame < 0:
                return self._fail(graph, "illegal_frame",
                                  f"edits[{index}] 帧号非法: {frame}",
                                  node_id=node_id)
            if action == "move":
                to_frame = int(edit.get("to_frame", -1))
                if to_frame < 0:
                    return self._fail(graph, "illegal_frame",
                                      f"edits[{index}] 目标帧非法: {to_frame}",
                                      node_id=node_id)
            elif action == "set":
                reason = validate_value_shape(param, edit.get("value"))
                if reason is not None:
                    return self._fail(graph, reason,
                                      f"edits[{index}] 值形态非法: {reason}",
                                      node_id=node_id)
            if action in ("set", "dynamics"):
                kind = edit.get("kind")
                if kind is not None and str(kind) not in KEY_KINDS:
                    return self._fail(graph, "bad_key_kind",
                                      f"edits[{index}] 键型非法: {kind!r}",
                                      node_id=node_id)
                weights = validate_dynamic_weights(
                    edit.get("start", 1.0), edit.get("end", 1.0))
                if weights is not None:
                    return self._fail(graph, weights,
                                      f"edits[{index}] 动态权重非法",
                                      node_id=node_id)
            grouped.setdefault(node_id, []).append((param, edit))

        # ── 逐节点构造候选（深拷贝 params；事务无部分写入）──
        moves_by_key: dict[tuple[str, str],
                           list[tuple[int, int]]] = {}
        for node_id, entries in grouped.items():
            for param, edit in entries:
                if str(edit.get("action")) == "move":
                    moves_by_key.setdefault((node_id, param), []).append(
                        (int(edit["frame"]), int(edit["to_frame"])))

        updated: dict[str, dict[str, Any]] = {}
        changed_any = False
        applied_moves: set[tuple[str, str]] = set()
        for node_id, entries in grouped.items():
            node = self._node(graph, node_id)
            assert node is not None  # 校验阶段已确认
            source = node.params.get("grade_keyframes") or {}
            legacy_kf = getattr(node, "legacy_payload", None) or {}
            raw_legacy = (legacy_kf.get("grade_keyframes")
                          if isinstance(legacy_kf, Mapping) else None)
            legacy_raw = raw_legacy if isinstance(raw_legacy, Mapping) else {}
            legacy_names = {
                str(k) for k, v in legacy_raw.items()
                if isinstance(v, Mapping) and v.get("keyframes")}
            touch_names = {str(param) for param, _ in entries}

            # ── 工作集（params 现状 + 被触及轨道的 legacy 懒导入）──
            # KF-05：legacy 起源轨道经合并视图对外可见，编辑必须落在
            # 合并视图上再写回 params（写回即完成一次性导入）。未被
            # 本批触及的 legacy 轨道保持原域，不随编辑扩散。
            tracks: dict[str, list] = {}
            tombstones: set[str] = set()
            for name, item in source.items():
                if not isinstance(item, Mapping):
                    continue
                normalized = self._normalized_keyframe_track_dynamic(item)
                if normalized:
                    tracks[str(name)] = normalized
                elif item.get("tombstone"):
                    tombstones.add(str(name))
                    tracks[str(name)] = []
            for param in sorted(touch_names):
                if tracks.get(param):
                    continue  # params 已是权威（非空）
                legacy_item = legacy_raw.get(param)
                legacy_norm = (
                    self._normalized_keyframe_track_dynamic(legacy_item)
                    if isinstance(legacy_item, Mapping) else [])
                if legacy_norm and param not in tombstones:
                    tracks[param] = legacy_norm  # 懒导入
                elif param not in tracks:
                    tracks[param] = []  # set 等动作将写入的新轨道

            # ── 基线（"本批无任何效果时 params 应有的形态"）──
            baseline: dict[str, dict[str, Any]] = {}
            for name, item in source.items():
                if not isinstance(item, Mapping):
                    continue
                normalized = self._normalized_keyframe_track_dynamic(item)
                if normalized:
                    baseline[str(name)] = {"keyframes": normalized}
                elif item.get("tombstone"):
                    baseline[str(name)] = {"keyframes": [], "tombstone": True}
            for param in sorted(touch_names):
                merged = tracks.get(param) or []
                if merged:
                    baseline[param] = {"keyframes": [dict(k) for k in merged]}
                elif param in tombstones or param in legacy_names:
                    baseline[param] = {"keyframes": [], "tombstone": True}
            for param, edit in entries:
                action = str(edit["action"])
                frame = int(edit["frame"])
                track = list(tracks.get(param) or [])
                if action == "set":
                    try:
                        entry = encode_keyframe(make_keyframe(
                            frame, edit.get("value"),
                            interpolation=str(edit.get("interpolation",
                                                     "linear")),
                            kind=(str(edit["kind"])
                                  if edit.get("kind") is not None else None),
                            start=float(edit.get("start", 1.0)),
                            end=float(edit.get("end", 1.0))))
                    except ValueError as exc:
                        return self._fail(graph, str(exc),
                                          f"edits[{param}@{frame}] 非法: {exc}",
                                          node_id=node_id)
                    existing = next((k for k in track
                                     if k["frame"] == frame), None)
                    if existing is not None and existing == entry:
                        continue  # 重复置值：不推进历史
                    track = [k for k in track if k["frame"] != frame]
                    track.append(entry)
                    track.sort(key=lambda e: e["frame"])
                    tracks[param] = track
                elif action == "remove":
                    kept = [k for k in track if k["frame"] != frame]
                    if len(kept) != len(track):
                        tracks[param] = kept
                elif action == "move":
                    if (node_id, param) in applied_moves:
                        continue  # 组级操作：整组一次，防重复重放换回
                    applied_moves.add((node_id, param))
                    pairs = moves_by_key[(node_id, param)]
                    moving_frames = {f for f, _ in pairs}
                    target_frames = [t for _, t in pairs]
                    if len(set(target_frames)) != len(target_frames):
                        return self._fail(
                            graph, "frame_conflict",
                            f"move 目标帧冲突（同轨道多键落同帧）: "
                            f"{param}@{node_id}", node_id=node_id)
                    others = {k["frame"] for k in track
                              if k["frame"] not in moving_frames}
                    clash = sorted(set(target_frames) & others)
                    if clash:
                        return self._fail(
                            graph, "frame_conflict",
                            f"move 目标与既有键冲突: {param} 帧 {clash}",
                            node_id=node_id)
                    moving = [k for k in track
                              if k["frame"] in moving_frames]
                    # 先移除全部源帧再插入目标（相邻帧交换不自覆盖）
                    track = [k for k in track
                             if k["frame"] not in moving_frames]
                    for keyframe in moving:
                        target = next(t for f, t in pairs
                                      if f == keyframe["frame"])
                        track.append({**keyframe, "frame": target})
                    track.sort(key=lambda e: e["frame"])
                    tracks[param] = track
                else:  # dynamics
                    existing = next((k for k in track
                                     if k["frame"] == frame), None)
                    if existing is None:
                        return self._fail(
                            graph, "keyframe_not_found",
                            f"dynamics 目标键不存在: {param}@{frame}",
                            node_id=node_id)
                    try:
                        decoded = decode_keyframe(existing)
                        requested_kind = str(edit.get("kind", decoded.kind))
                        if requested_kind != decoded.kind:
                            # 转换键型：保留原值与帧号，取消旧动态属性
                            # （默认权重随新键型；实测默认待 KF-00 核对）
                            start_value, end_value = 1.0, 1.0
                        else:
                            start_value = float(edit.get("start",
                                                         decoded.start))
                            end_value = float(edit.get("end",
                                                       decoded.end))
                        replacement = make_keyframe(
                            frame, decoded.value, kind=requested_kind,
                            start=start_value, end=end_value)
                    except ValueError as exc:
                        return self._fail(graph, str(exc),
                                          f"dynamics 非法: {exc}",
                                          node_id=node_id)
                    entry = encode_keyframe(replacement)
                    if existing == entry:
                        continue
                    track = [k for k in track if k["frame"] != frame]
                    track.append(entry)
                    track.sort(key=lambda e: e["frame"])
                    tracks[param] = track
            candidate_params: dict[str, Any] = {}
            # KF-05：删空轨道的删除语义——legacy 基底存在同名轨道（或
            # 原为墓碑）时写显式空覆盖，防止保存/重开后旧轨道复活；
            # 图域私有轨道删空即消失（既有语义）。
            for name, kfs in tracks.items():
                if kfs:
                    candidate_params[name] = {"keyframes": kfs}
                elif name in legacy_names or name in tombstones:
                    candidate_params[name] = {"keyframes": [],
                                              "tombstone": True}
            if candidate_params != baseline:
                changed_any = True
            updated[node_id] = candidate_params
        if not changed_any:
            return GraphEditResult(True, graph, ())  # 拖回原位/空动作
        nodes = tuple(
            replace(node, params={**node.params, "grade_keyframes":
                                  updated[node.node_id]})
            if node.node_id in updated else node
            for node in graph.nodes)
        return self._commit(graph, replace(graph, nodes=nodes), {})

    # ═══ N15：Resolve 窗口组合（CPW/LPW/PPW/PCW 一键 = Serial+Window）═══

    #: Resolve 20.2.1 Color 页实机默认值：CPW 圆，LPW 矩形，PPW
    #: 四角多边形，PCW 空曲线（等待用户落点）。曲线点必须使用
    #: MaskShape 的 pos/c1/c2 字典格式，不能保存裸坐标列表。
    _WINDOW_COMBO_DEFAULTS = {
        "cpw": {"type": "circle", "size": [0.25, 0.25], "feather": 4.13,
                "name": "Circular Power Window"},
        "lpw": {"type": "rect", "size": [0.4, 0.4],
                "edge_softness": [1.25, 1.25, 1.25, 1.25],
                "name": "Linear Power Window"},
        "ppw": {"type": "curve", "size": [0.4, 0.4], "feather": 0.0,
                "points": [
                    {"pos": [x, y], "c1": [x, y], "c2": [x, y],
                     "type": "corner"}
                    for x, y in ((0.3, 0.3), (0.7, 0.3),
                                 (0.7, 0.7), (0.3, 0.7))
                ], "name": "Polygon"},
        "pcw": {"type": "curve", "points": [], "feather": 0.0,
                "name": "Power Curve Window"},
    }

    def op_add_window_combo(self, graph: ColorGraphData,
                            active_node_id: str | None = None,
                            combo: str = "cpw", *,
                            position: Sequence[float] | None = None,
                            positions: Mapping[str, Sequence[float]] | None = None,
                            ) -> GraphEditResult:
        """N15：一键 = 新建 Serial Corrector + 挂默认形状窗口（一命令一 undo）。

        - 插入位置与 ``insert_corrector`` 同策略（anchor 后串入）；
        - 窗口写入新节点的 ``legacy_payload.mask_shapes``（N03 语义：
          身份独立、可跟踪绑定）；
        - 默认中性（grading 空、opacity 1、形状居中占画面约 1/4）。
        """
        from copy import deepcopy

        shape = deepcopy(self._WINDOW_COMBO_DEFAULTS.get(str(combo).lower()) or {})
        if not shape:
            return self._fail(graph, "illegal_param_key",
                              f"未知窗口组合: {combo}（cpw/lpw/ppw/pcw）")
        inserted = self.op_insert_corrector(graph, active_node_id,
                                            position=position, positions=positions)
        if not inserted.ok:
            return inserted
        new_id = next(iter(inserted.id_mapping.values()))
        node = self._node(inserted.graph, new_id)
        window = {
            "id": self._id_factory(),
            "type": shape["type"],
            "name": shape.get("name", combo),
            "center": [0.5, 0.5],
            "size": shape.get("size", [0.25, 0.25]),
            "rotation": 0.0,
            "feather": shape.get("feather", 2.0),
            "opacity": 1.0,
            "visible": True,
            "invert": False,
            "operation": "add",
        }
        if "points" in shape:
            window["points"] = shape["points"]
        if "grad_points" in shape:
            window["grad_points"] = shape["grad_points"]
        if "edge_softness" in shape:
            window["edge_softness"] = shape["edge_softness"]
        legacy = dict(node.legacy_payload or {})
        # 串行图可投影为 engine.layers 时，活动层只允许按 layer id 对齐。
        # 新 Corrector 没有迁移载荷；给它稳定身份，否则 Layer.from_dict
        # 每次水合都会造新 UUID，Window/Tracker 仍指向上一个图层。
        legacy["id"] = new_id
        legacy["mask_shapes"] = [window]
        new_node = replace(node, legacy_payload=legacy)
        nodes = tuple(new_node if n.node_id == new_id else n
                      for n in inserted.graph.nodes)
        candidate = replace(inserted.graph, nodes=nodes)
        return self._commit(inserted.graph, candidate, inserted.id_mapping)

    def op_add_node(self, graph: ColorGraphData, kind: str,
                    position: Sequence[float] = (0.0, 0.0)
                    ) -> GraphEditResult:
        """未连接新节点（§6.3：允许保留，导出只取依赖锥）。"""
        try:
            node_type = NodeType(kind)
        except ValueError:
            return self._fail(graph, "unknown_node_type", f"未知节点类型: {kind}")
        if node_type in (NodeType.SOURCE, NodeType.OUTPUT):
            return self._fail(graph, "terminal_count",
                              f"禁止新增 {node_type.value}（端子唯一）")
        new_id = self._id_factory()
        params = ({"key_source_mode": "node_input"}
                  if node_type is NodeType.CORRECTOR else {})
        node = ColorNodeData(node_id=new_id, node_type=node_type.value,
                             params=params)
        ui = replace(graph.ui_state,
                     positions={**dict(graph.ui_state.positions),
                                new_id: (float(position[0]), float(position[1]))})
        candidate = replace(graph, nodes=graph.nodes + (node,), ui_state=ui)
        # 未连接节点结构合法（孤立允许），revision 仍 bump：图内容变了
        return self._commit(graph, candidate, {new_id: new_id})

    def op_connect_ports(self, graph: ColorGraphData, src_node: str,
                         src_port: str, dst_node: str, dst_port: str,
                         *, replace_existing: bool = True,
                         slot_id: str = "") -> GraphEditResult:
        """先完整验证新边，成功后才替换旧边（§6.2 原则）。"""
        for node_id, port_id, direction in (
                (src_node, src_port, PortDirection.OUT),
                (dst_node, dst_port, PortDirection.IN)):
            err = self._check_port(graph, node_id, port_id, direction)
            if err is not None:
                return GraphEditResult(False, graph, (err,))
        src_node_obj = self._node(graph, src_node)
        dst_spec = next(
            p for p in self._node(graph, dst_node).ports()
            if p.port_id == dst_port and p.direction is PortDirection.IN)
        src_spec = next(
            p for p in src_node_obj.ports()
            if p.port_id == src_port and p.direction is PortDirection.OUT)
        if src_spec.kind is not dst_spec.kind:
            return self._fail(
                graph, "port_kind",
                f"禁止 {src_spec.kind.value}→{dst_spec.kind.value} 隐式转换")
        edges = list(graph.edges)
        if not dst_spec.multi:
            # 单输入端口：验证通过后原子替换旧边
            existing = [e for e in edges
                        if e.dst_node == dst_node and e.dst_port == dst_port]
            if existing and not replace_existing:
                return self._fail(graph, "port_multi",
                                  f"{dst_node}.{dst_port} 已有连接")
            edges = [e for e in edges if e not in existing]
        elif slot_id:
            occupied = [edge for edge in edges if edge.dst_node == dst_node
                        and edge.dst_port == dst_port and edge.slot_id == slot_id]
            if occupied and not replace_existing:
                return self._fail(graph, "port_multi", "目标 mixer 槽已有连接")
            edges = [edge for edge in edges if edge not in occupied]
        # 成环检查：dst 的依赖锥不得包含 src
        reach = self._downstream_closure(graph, dst_node)
        if src_node in reach or src_node == dst_node:
            return self._fail(graph, "cycle",
                              f"连接 {src_node}→{dst_node} 会成环",
                              node_id=dst_node)
        new_edge = _make_edge(src_node, src_port, dst_node, dst_port, slot_id=slot_id)
        if slot_id:
            new_edge = replace(new_edge, edge_id=str(uuid.uuid5(
                uuid.NAMESPACE_URL, new_edge.edge_id + ":" + slot_id)))
        candidate = replace(graph, edges=tuple(edges) + (new_edge,))
        return self._commit(graph, candidate,
                            {new_edge.edge_id: new_edge.edge_id})

    def _downstream_closure(self, graph: ColorGraphData,
                            start: str) -> set[str]:
        adjacency: dict[str, list[str]] = {}
        for e in graph.edges:
            adjacency.setdefault(e.src_node, []).append(e.dst_node)
        seen = {start}
        stack = [start]
        while stack:
            for nxt in adjacency.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def op_disconnect_edge(self, graph: ColorGraphData,
                           edge_id: str) -> GraphEditResult:
        edge = next((e for e in graph.edges if e.edge_id == edge_id), None)
        if edge is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"边不存在: {edge_id}")
        candidate = replace(
            graph, edges=tuple(e for e in graph.edges if e.edge_id != edge_id))
        return self._commit(graph, candidate, {})

    def op_disconnect_edges(self, graph: ColorGraphData,
                            edge_ids: Sequence[str]) -> GraphEditResult:
        """Remove a set of existing wires as one validated transaction."""
        requested = set(edge_ids)
        if not requested or not requested.issubset({edge.edge_id for edge in graph.edges}):
            return self._fail(graph, "edge_endpoint_missing", "断开连接包含不存在的边")
        candidate = replace(graph, edges=tuple(
            edge for edge in graph.edges if edge.edge_id not in requested))
        return self._commit(graph, candidate, {})

    # ════════════════════════════════════════════════════════════════
    # 分支结构（parallel/layer/outside/splitter/key mixer/alpha）
    # ════════════════════════════════════════════════════════════════

    def op_add_parallel_branch(self, graph: ColorGraphData,
                               branch_origin_id: str | None = None
                               ) -> GraphEditResult:
        """在 origin 与其下游之间加 Parallel Mixer + 空分支 Corrector。

        origin.image 原下游边改接 mixer.base；新建分支 C 与 mixer；
        C 挂 mixer.branch[0]；mixer.image → 原下游。
        """
        origin_id = branch_origin_id or graph.source_node_id
        down = self._downstream_edge(graph, origin_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{origin_id} 无唯一下游 image 边（分支需先有链）",
                              node_id=origin_id)
        mixer_id, branch_id = self._id_factory(), self._id_factory()
        mixer = ColorNodeData(node_id=mixer_id,
                              node_type=NodeType.PARALLEL_MIXER.value)
        branch = ColorNodeData(
            node_id=branch_id, node_type=NodeType.CORRECTOR.value,
            params={"key_source_mode": "node_input"})
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [
            _make_edge(origin_id, "image", mixer_id, "base"),
            _make_edge(origin_id, "image", branch_id, "image"),
            _make_edge(branch_id, "image", mixer_id, "branch",
                       slot_id=f"{mixer_id}/branch/0"),
            _make_edge(mixer_id, "image", down.dst_node, down.dst_port, slot_id=down.slot_id),
        ]
        candidate = replace(graph,
                            nodes=graph.nodes + (mixer, branch),
                            edges=tuple(edges))
        return self._commit(graph, candidate,
                            {mixer_id: mixer_id, branch_id: branch_id})

    def op_add_layer_branch(self, graph: ColorGraphData,
                            base_input_id: str | None = None
                            ) -> GraphEditResult:
        """Layer Mixer：有序 slot（由下到上），空分支占位 slot 0。"""
        origin_id = base_input_id or graph.source_node_id
        down = self._downstream_edge(graph, origin_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{origin_id} 无唯一下游 image 边", node_id=origin_id)
        mixer_id, branch_id = self._id_factory(), self._id_factory()
        mixer = ColorNodeData(node_id=mixer_id,
                              node_type=NodeType.LAYER_MIXER.value)
        branch = ColorNodeData(
            node_id=branch_id, node_type=NodeType.CORRECTOR.value,
            params={"key_source_mode": "node_input"})
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [
            _make_edge(origin_id, "image", mixer_id, "slot",
                       slot_id=f"{mixer_id}/slot/0"),   # 基底（最底）
            _make_edge(origin_id, "image", branch_id, "image"),
            _make_edge(branch_id, "image", mixer_id, "slot",
                       slot_id=f"{mixer_id}/slot/1"),   # 分支压在基底上
            _make_edge(mixer_id, "image", down.dst_node, down.dst_port, slot_id=down.slot_id),
        ]
        candidate = replace(graph,
                            nodes=graph.nodes + (mixer, branch),
                            edges=tuple(edges))
        return self._commit(graph, candidate,
                            {mixer_id: mixer_id, branch_id: branch_id})

    def op_add_outside_corrector(self, graph: ColorGraphData,
                                 source_key_node_id: str) -> GraphEditResult:
        """Outside：引用原节点 key_out 的补集；颜色输入显式连接。"""
        source = self._node(graph, source_key_node_id)
        if source is None:
            return self._fail(graph, "node_not_found",
                              f"key 源节点不存在: {source_key_node_id}",
                              node_id=source_key_node_id)
        key_out_spec = next(
            (p for p in source.ports()
             if p.port_id == "key_out" and p.direction is PortDirection.OUT),
            None)
        if key_out_spec is None:
            return self._fail(graph, "port_not_found",
                              f"{source_key_node_id} 无 key_out（Corrector 才有 outside）",
                              node_id=source_key_node_id)
        down = self._downstream_edge(graph, source_key_node_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{source_key_node_id} 无唯一下游 image 边",
                              node_id=source_key_node_id)
        outside_id = self._id_factory()
        outside = ColorNodeData(
            node_id=outside_id, node_type=NodeType.OUTSIDE.value,
            params={"key_source_mode": "node_input",
                    "outside_key_source": source_key_node_id})
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [
            _make_edge(source_key_node_id, "image", outside_id, "image"),
            _make_edge(source_key_node_id, "key_out", outside_id, "key_in"),
            _make_edge(outside_id, "image", down.dst_node, down.dst_port, slot_id=down.slot_id),
        ]
        candidate = replace(graph,
                            nodes=graph.nodes + (outside,),
                            edges=tuple(edges))
        return self._commit(graph, candidate, {outside_id: outside_id})

    def op_add_splitter_pair(self, graph: ColorGraphData,
                             node_id: str | None = None) -> GraphEditResult:
        """Splitter + Combiner 成对原子插入（§2.3 一次事务）。"""
        anchor_id = node_id or graph.source_node_id
        down = self._downstream_edge(graph, anchor_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{anchor_id} 无唯一下游 image 边", node_id=anchor_id)
        split_id, comb_id = self._id_factory(), self._id_factory()
        splitter = ColorNodeData(node_id=split_id,
                                 node_type=NodeType.SPLITTER.value)
        combiner = ColorNodeData(node_id=comb_id,
                                 node_type=NodeType.COMBINER.value)
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [_make_edge(anchor_id, "image", split_id, "image")]
        edges += [_make_edge(split_id, ch, comb_id, ch) for ch in ("r", "g", "b")]
        edges += [_make_edge(comb_id, "image", down.dst_node, down.dst_port, slot_id=down.slot_id)]
        candidate = replace(graph,
                            nodes=graph.nodes + (splitter, combiner),
                            edges=tuple(edges))
        return self._commit(graph, candidate,
                            {split_id: split_id, comb_id: comb_id})

    def op_add_key_mixer(self, graph: ColorGraphData,
                         key_source_ids: Sequence[str] = ()
                         ) -> GraphEditResult:
        """Key Mixer：多个 key 源混合；无源时允许未连接创建。"""
        mixer_id = self._id_factory()
        mixer = ColorNodeData(node_id=mixer_id,
                              node_type=NodeType.KEY_MIXER.value)
        edges = list(graph.edges)
        for slot_index, source_id in enumerate(key_source_ids):
            source = self._node(graph, source_id)
            if source is None:
                return self._fail(graph, "node_not_found",
                                  f"key 源不存在: {source_id}", node_id=source_id)
            if not any(p.port_id == "key_out" and p.direction is PortDirection.OUT
                       for p in source.ports()):
                return self._fail(graph, "port_not_found",
                                  f"{source_id} 无 key_out", node_id=source_id)
            edges.append(_make_edge(source_id, "key_out", mixer_id, "key",
                                    slot_id=f"{mixer_id}/key/{slot_index}"))
        candidate = replace(graph, nodes=graph.nodes + (mixer,),
                            edges=tuple(edges))
        return self._commit(graph, candidate, {mixer_id: mixer_id})

    def op_add_alpha_output(self, graph: ColorGraphData,
                            key_source_id: str | None = None
                            ) -> GraphEditResult:
        if graph.alpha_output_node_id:
            return self._fail(graph, "terminal_count", "Alpha Output 已存在（≤1）")
        alpha_id = self._id_factory()
        alpha = ColorNodeData(node_id=alpha_id,
                              node_type=NodeType.ALPHA_OUTPUT.value)
        edges = list(graph.edges)
        if key_source_id is not None:
            source = self._node(graph, key_source_id)
            if source is None:
                return self._fail(graph, "node_not_found",
                                  f"key 源不存在: {key_source_id}",
                                  node_id=key_source_id)
            edges.append(_make_edge(key_source_id, "key_out", alpha_id, "key"))
        candidate = replace(graph, nodes=graph.nodes + (alpha,),
                            edges=tuple(edges),
                            alpha_output_node_id=alpha_id)
        return self._commit(graph, candidate, {alpha_id: alpha_id})

    def op_remove_alpha_output(self, graph: ColorGraphData,
                               node_id: str | None = None) -> GraphEditResult:
        target = node_id or graph.alpha_output_node_id
        if not target or self._node(graph, target) is None:
            return self._fail(graph, "node_not_found", f"Alpha Output 不存在: {target}")
        if target == graph.output_node_id:
            return self._fail(graph, "terminal_count",
                              "不能删除主 Output（目标是 Alpha Output）")
        nodes = tuple(n for n in graph.nodes if n.node_id != target)
        edges = tuple(e for e in graph.edges
                      if target not in (e.src_node, e.dst_node))
        candidate = replace(graph, nodes=nodes, edges=edges,
                            alpha_output_node_id=None)
        return self._commit(graph, candidate, {})

    # ════════════════════════════════════════════════════════════════
    # 删除 / 提取 / 复制
    # ════════════════════════════════════════════════════════════════

    def op_delete_nodes(self, graph: ColorGraphData,
                        node_ids: Sequence[str],
                        reconnect: str = "auto") -> GraphEditResult:
        """删除节点。``reconnect='auto'``：单进单出节点桥接上下游；
        多分支/端子节点删除需显式策略，否则诊断拒绝（不静默断效果）。
        """
        ids = set(node_ids)
        if not ids:
            return self._fail(graph, "illegal_param_key", "删除集合为空")
        protected = {graph.source_node_id, graph.output_node_id,
                     graph.alpha_output_node_id} - {None}
        hit = ids & protected
        if hit:
            return self._fail(graph, "terminal_count",
                              f"禁止删除受保护端子: {sorted(hit)}",
                              node_id=sorted(hit)[0])
        for node_id in ids:
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)

        remove_edges: set[str] = set()
        add_edges: list[ColorEdgeData] = []
        for node_id in sorted(ids):
            ins = self._edges_into(graph, node_id)
            outs = self._edges_out(graph, node_id)
            remove_edges.update(e.edge_id for e in ins + outs)
            image_ins = [e for e in ins if _port_kind_of(graph, e) is PortKind.IMAGE]
            image_outs = [e for e in outs if _port_kind_of(graph, e) is PortKind.IMAGE]
            if reconnect == "auto":
                if len(image_ins) <= 1 and len(image_outs) <= 1:
                    if image_ins and image_outs:
                        # 上下游桥接（类型一致已由原图保证）
                        add_edges.append(_make_edge(
                            image_ins[0].src_node, image_ins[0].src_port,
                            image_outs[0].dst_node, image_outs[0].dst_port, slot_id=image_outs[0].slot_id))
                    # 端点缺失 = 链端删除，允许
                else:
                    return self._fail(
                        graph, "ambiguous_reconnect",
                        f"{node_id} 有 {len(image_ins)} 进 {len(image_outs)} 出，"
                        "自动重连策略不安全（需显式 extract/选择保留分支）",
                        node_id=node_id)
        kept = tuple(e for e in graph.edges if e.edge_id not in remove_edges)
        # 去重（桥接可能与既有边重复）
        seen: set[str] = set()
        final: list[ColorEdgeData] = []
        for e in kept + tuple(add_edges):
            if e.edge_id in seen:
                continue
            seen.add(e.edge_id)
            final.append(e)
        nodes = tuple(n for n in graph.nodes if n.node_id not in ids)
        candidate = replace(graph, nodes=nodes, edges=tuple(final))
        return self._commit(graph, candidate, {})

    def op_extract_nodes(self, graph: ColorGraphData,
                         node_ids: Sequence[str]) -> GraphEditResult:
        """提取节点：桥接其上下游、节点保留为未连接（UI 语义 Alt+Backspace）。"""
        ids = set(node_ids)
        for node_id in ids:
            node = self._node(graph, node_id)
            if node is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)
        remove_edges: set[str] = set()
        bridges: list[ColorEdgeData] = []
        for node_id in sorted(ids):
            ins = [e for e in self._edges_into(graph, node_id)
                   if _port_kind_of(graph, e) is PortKind.IMAGE]
            outs = [e for e in self._edges_out(graph, node_id)
                    if _port_kind_of(graph, e) is PortKind.IMAGE]
            remove_edges.update(e.edge_id
                                for e in self._edges_into(graph, node_id)
                                + self._edges_out(graph, node_id))
            if len(ins) == 1 and len(outs) == 1:
                bridges.append(_make_edge(ins[0].src_node, ins[0].src_port,
                                          outs[0].dst_node, outs[0].dst_port, slot_id=outs[0].slot_id))
        kept = tuple(e for e in graph.edges if e.edge_id not in remove_edges)
        seen = {e.edge_id for e in kept}
        for bridge in bridges:
            if bridge.edge_id not in seen:
                seen.add(bridge.edge_id)
                kept = kept + (bridge,)
        candidate = replace(graph, edges=kept)
        return self._commit(graph, candidate, {})

    def op_duplicate_subgraph(self, graph: ColorGraphData,
                              source_ids: Sequence[str],
                              offset: Sequence[float] = (40.0, 40.0)
                              ) -> GraphEditResult:
        """复制子图：内部 ID 全重写，内部边保持；外部边不复制。"""
        return self._copy_nodes(graph, source_ids, offset)

    def op_paste_subgraph(self, graph: ColorGraphData,
                          payload_nodes: Sequence[Mapping[str, Any]],
                          payload_edges: Sequence[Mapping[str, Any]] = (),
                          offset: Sequence[float] = (40.0, 40.0),
                          payload_positions: Mapping[str, Sequence[float]] | None = None,
                          ) -> GraphEditResult:
        """Paste independent nodes, including complete compound payloads."""
        originals = tuple(ColorNodeData.from_dict(dict(item)) for item in payload_nodes)
        if not originals or any(node.known_type() in (NodeType.SOURCE, NodeType.OUTPUT)
                                for node in originals):
            return self._fail(graph, "illegal_param_key", "粘贴集合为空或包含主端子")
        remap: dict[str, str] = {}
        def collect(node: ColorNodeData) -> None:
            remap[node.node_id] = self._id_factory()
            if node.compound_children is not None:
                for child in node.compound_children.nodes:
                    collect(child)
        for original in originals:
            collect(original)
        def identity(value: Any) -> Any:
            if isinstance(value, str):
                if value in remap:
                    return remap[value]
                for old, new in remap.items():
                    if value.startswith(old + "/"):
                        return new + value[len(old):]
                return value
            if isinstance(value, Mapping):
                return {key: identity(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [identity(item) for item in value]
            return value
        def clone_edge(edge: ColorEdgeData) -> ColorEdgeData:
            slot_id = identity(edge.slot_id)
            cloned = _make_edge(remap[edge.src_node], edge.src_port,
                                remap[edge.dst_node], edge.dst_port, slot_id=slot_id)
            if slot_id:
                cloned = replace(cloned, edge_id=str(uuid.uuid5(
                    uuid.NAMESPACE_URL, cloned.edge_id + ":" + slot_id)))
            return cloned
        def clone_node(node: ColorNodeData) -> ColorNodeData:
            child = node.compound_children
            if child is not None:
                child = replace(child,
                    nodes=tuple(clone_node(item) for item in child.nodes),
                    edges=tuple(clone_edge(edge) for edge in child.edges),
                    source_node_id=remap[child.source_node_id],
                    output_node_id=remap[child.output_node_id],
                    ui_state=replace(child.ui_state, positions={
                        remap[key]: position for key, position in child.ui_state.positions.items()
                        if key in remap}))
            legacy = dict(node.legacy_payload or {})
            if legacy:
                legacy["id"] = remap[node.node_id]
            return replace(node, node_id=remap[node.node_id],
                           params=identity(node.params), legacy_payload=legacy,
                           compound_children=child)
        originals_ids = {node.node_id for node in originals}
        edges = tuple(ColorEdgeData.from_dict(dict(item)) for item in payload_edges)
        edges = tuple(clone_edge(edge) for edge in edges
                      if edge.src_node in originals_ids and edge.dst_node in originals_ids)
        positions = dict(graph.ui_state.positions)
        for old, position in (payload_positions or {}).items():
            if old in originals_ids:
                positions[remap[old]] = (float(position[0]) + float(offset[0]),
                                         float(position[1]) + float(offset[1]))
        candidate = replace(graph, nodes=graph.nodes + tuple(clone_node(node) for node in originals),
                            edges=graph.edges + edges,
                            ui_state=replace(graph.ui_state, positions=positions))
        return self._commit(graph, candidate, remap)

    def _copy_nodes(self, graph: ColorGraphData,
                    source_ids: Sequence[str],
                    offset: Sequence[float]) -> GraphEditResult:
        ids = set(source_ids)
        if not ids or not ids.issubset({node.node_id for node in graph.nodes}):
            return self._fail(graph, "node_not_found", "复制集合为空或包含不存在的节点")
        return self.op_paste_subgraph(
            graph, [node.to_dict() for node in graph.nodes if node.node_id in ids],
            [edge.to_dict() for edge in graph.edges
             if edge.src_node in ids and edge.dst_node in ids], offset,
            {key: position for key, position in graph.ui_state.positions.items() if key in ids})

    def op_add_mixer_slot(self, graph: ColorGraphData,
                          node_id: str) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        if node.node_type not in (NodeType.PARALLEL_MIXER.value,
                                  NodeType.LAYER_MIXER.value, NodeType.KEY_MIXER.value):
            return self._fail(graph, "port_not_found",
                              f"{node_id} 不是 mixer，无 slot 可加", node_id=node_id)
        max_slots = 8
        edge_slots = {e.slot_id for e in self._edges_into(graph, node_id)
                      if e.slot_id}
        param_slots = set(node.params.get("slots", ())
                          if isinstance(node.params.get("slots", ()),
                                        (tuple, list)) else ())
        existing = edge_slots | param_slots
        if len(existing) >= max_slots:
            return self._fail(graph, "size_limit",
                              f"mixer slot 上限 {max_slots}", node_id=node_id)
        # slot 分配：稳定 slot ID（新分支输入在 B10 执行语义接线时连接）
        indices = [int(item.rsplit("/", 1)[-1]) for item in existing
                   if item.rsplit("/", 1)[-1].isdigit()]
        minimum_index = 1 if node.node_type == NodeType.PARALLEL_MIXER.value else 2
        next_index = max(minimum_index, max(indices, default=-1) + 1)
        prefix = {NodeType.PARALLEL_MIXER.value: "branch",
                  NodeType.LAYER_MIXER.value: "slot", NodeType.KEY_MIXER.value: "key"}[node.node_type]
        params = dict(node.params)
        params["slots"] = sorted(existing | {f"{node_id}/{prefix}/{next_index}"})
        new_node = replace(node, params=params)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    def op_change_mixer_type(self, graph: ColorGraphData, node_id: str,
                             kind: str) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        try:
            target = NodeType(kind)
        except ValueError:
            return self._fail(graph, "unknown_node_type", f"未知类型: {kind}")
        if target not in (NodeType.PARALLEL_MIXER, NodeType.LAYER_MIXER):
            return self._fail(graph, "port_not_found",
                              f"morph 目标必须是 mixer: {kind}", node_id=node_id)
        if node.node_type not in (NodeType.PARALLEL_MIXER.value,
                                  NodeType.LAYER_MIXER.value):
            return self._fail(graph, "port_not_found",
                              f"{node_id} 不是 mixer，不能 morph", node_id=node_id)
        # 端口目录不同（base/branch ↔ slot）——双向重写相邻边端口
        edges = list(graph.edges)
        to_parallel = target is NodeType.PARALLEL_MIXER
        for i, e in enumerate(edges):
            if e.dst_node == node_id:
                if to_parallel:
                    if e.dst_port == "slot":
                        idx = (e.slot_id.rsplit("/", 1)[-1]
                               if e.slot_id else "0")
                        index = int(idx) if idx.isdigit() else 0
                        if index == 0:
                            edges[i] = _make_edge(e.src_node, e.src_port,
                                                  node_id, "base")
                        else:
                            edges[i] = _make_edge(
                                e.src_node, e.src_port, node_id, "branch",
                                slot_id=f"{node_id}/branch/{index - 1}")
                else:  # → layer mixer
                    if e.dst_port == "base":
                        edges[i] = _make_edge(e.src_node, e.src_port,
                                              node_id, "slot",
                                              slot_id=f"{node_id}/slot/0")
                    elif e.dst_port == "branch":
                        idx = e.slot_id.rsplit("/", 1)[-1]
                        edges[i] = _make_edge(e.src_node, e.src_port,
                                              node_id, "slot",
                                              slot_id=f"{node_id}/slot/{int(idx) + 1}")
        new_node = replace(node, node_type=target.value)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes, edges=tuple(edges))
        return self._commit(graph, candidate, {})

    def op_set_bypass(self, graph: ColorGraphData,
                      node_ids: Sequence[str], enabled: bool) -> GraphEditResult:
        return self._patch_nodes(graph, node_ids, enabled=enabled)

    def op_set_all_enabled(self, graph: ColorGraphData,
                           enabled: bool = True) -> GraphEditResult:
        ids = [n.node_id for n in graph.nodes
               if n.node_type not in (NodeType.SOURCE.value,
                                      NodeType.OUTPUT.value)]
        return self._patch_nodes(graph, ids, enabled=enabled)

    def op_set_node_option(self, graph: ColorGraphData, node_id: str,
                           name: str, value: Any) -> GraphEditResult:
        """按 name 分派到领域 schema 键（§6.3：不做通用 dict update）。"""
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        if node.params.get("locked"):
            return self._fail(graph, "node_locked", "节点已锁定，先解锁再修改参数", node_id=node_id)
        allowed = {
            "key_invert": ("key_settings", "invert_key_in"),
            "blend_mode": ("mixer", "blend_mode"),
            "lut_ref": ("resources", "lut_ref"),
            "color_space": ("color", "color_space"),
            "gamma": ("color", "gamma"),
        }
        if name == "grade_patch":
            # B06.2 旧面板手势过渡：图内 grade 域单键 patch（B09 前旧
            # 引擎经 legacy 投影消费；服务端仍走完整事务/校验）
            if not isinstance(value, tuple) or len(value) != 2:
                return self._fail(graph, "illegal_param_key",
                                  "grade_patch 需要 (param_name, value) 元组",
                                  node_id=node_id)
            grade = dict(node.params.get("grade") or {})
            grade[str(value[0])] = value[1]
            params = dict(node.params)
            params["grade"] = grade
            new_node = replace(node, params=params)
            nodes = tuple(new_node if n.node_id == node_id else n
                          for n in graph.nodes)
            candidate = replace(graph, nodes=nodes)
            return self._commit(graph, candidate, {})
        if name not in allowed:
            return self._fail(graph, "illegal_param_key",
                              f"未知节点选项: {name}（须显式登记 schema 键）",
                              node_id=node_id)
        group, key = allowed[name]
        params = dict(node.params)
        section = dict(params.get(group, {}))
        section[key] = value
        params[group] = section
        new_node = replace(node, params=params)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    def op_patch_key_settings(self, graph: ColorGraphData, node_id: str,
                              **settings: Any) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        params = dict(node.params)
        section = dict(params.get("key_settings", {}))
        section.update(settings)
        params["key_settings"] = section
        new_node = replace(node, params=params)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    def op_reset_corrector_parameters(self, graph: ColorGraphData,
                                      node_ids: Sequence[str]) -> GraphEditResult:
        def reset(node: ColorNodeData) -> ColorNodeData:
            # N10：重置清 params 关键帧轨道（防 seek 复活；与 legacy
            # reset_params 清 grade_keyframes 同语义）
            params = {"key_source_mode": node.params.get(
                "key_source_mode", "node_input")}
            if "outside_key_source" in node.params:
                params["outside_key_source"] = node.params["outside_key_source"]
            legacy = node.legacy_payload
            if legacy:
                # Reset the same complete grading payload as the legacy UI,
                # preserving stable identity and unknown extension fields.
                from src.features.color.core.layer import Layer
                layer = Layer.from_dict(dict(legacy))
                layer.reset_params()
                legacy = {**dict(legacy), **layer.to_dict(None)}
            return replace(node, params=params, legacy_payload=legacy)
        locked = next((node for node in graph.nodes if node.node_id in node_ids and node.params.get("locked")), None)
        if locked is not None:
            return self._fail(graph, "node_locked", "节点已锁定，先解锁再重置", node_id=locked.node_id)
        return self._patch_nodes_with(graph, node_ids, reset)

    def op_paste_grade_domains(self, graph: ColorGraphData,
                              node_ids: Sequence[str],
                              grade_domains: Mapping[str, Any]) -> GraphEditResult:
        """Paste the copied primary-grade domain to validated Correctors."""
        if not isinstance(grade_domains, Mapping):
            return self._fail(graph, "illegal_param_key", "grade_domains 必须是 Mapping")
        for node_id in node_ids:
            node = self._node(graph, node_id)
            if node is not None and node.params.get("locked"):
                return self._fail(graph, "node_locked", "节点已锁定，先解锁再粘贴", node_id=node_id)
            if node is None or node.known_type() not in (
                    NodeType.CORRECTOR, NodeType.SHARED_CORRECTOR, NodeType.OUTSIDE):
                return self._fail(graph, "node_not_found", "调色粘贴目标不是 Corrector")
        def paste(node: ColorNodeData) -> ColorNodeData:
            return replace(node, params={**dict(node.params), "grade": dict(grade_domains)})
        return self._patch_nodes_with(graph, node_ids, paste)

    # ═══ N12：选择性域粘贴 ══════════════════════════════════════════

    #: 可粘贴域 → (params 键, legacy_payload 键)；None = 域不在该载体。
    # N12-B：域表对齐 GradeClipboard 类别粒度（grade/curves(三捆)/hsl/
    # mixer/soft_clip/qualifier/keyframes/bake）；warper（ndarray 载体）
    # 与栈级 tracking/raw 登记后续批次。
    _PASTE_DOMAIN_TARGETS = {
        "grade": ("grade", None),
        "curves": (None, "curves_data"),
        "curves_edit": (None, "curves_edit"),
        "curves_soft_clip": (None, "curves_soft_clip"),
        "hsl": (None, "hsl_curve_data"),
        "mixer": (None, "mixer_params"),
        "soft_clip": (None, "soft_clip_params"),
        "qualifier": ("qualifier", None),
        "keyframes": ("grade_keyframes", None),
        "lut": ("resources", None),
        "bake": ("resources", None),
        "window": (None, "mask_shapes"),
        "effects": (None, "plugins"),
    }
    #: 域粘贴时的资源引用校验域（fail-visible：缺资源拒绝而非植悬空引用）
    _RESOURCE_VALIDATED_DOMAINS = ("lut", "bake")

    def op_paste_selected_domains(self, graph: ColorGraphData,
                                  node_ids: Sequence[str],
                                  payload: Mapping[str, Any],
                                  domains: Sequence[str]) -> GraphEditResult:
        """把剪贴板载荷的**选定域**粘贴到目标 Correctors（N12）。

        - 只写选定的域（仅粘曲线不改 LUT/Window…）；未选域原样保留；
        - 效果实例/窗口形状**重建本地身份**（新 instance_id/shape id，
          不共享 legacy Layer ID）；
        - LUT 以资源引用 + 强度成对粘贴（R05 域）；
        - 锁定/非 Corrector 目标显式拒绝。
        """
        unknown = [d for d in domains if d not in self._PASTE_DOMAIN_TARGETS]
        if unknown:
            return self._fail(graph, "illegal_param_key",
                              f"未知粘贴域: {unknown}")
        if not isinstance(payload, Mapping):
            return self._fail(graph, "illegal_param_key",
                              "payload 必须是 Mapping")
        # N12-B：资源引用域粘贴前校验——跨工程/缺资源 fail-visible 拒绝
        # （此前 lut 域直接写入可植入未注册 topos-res:// 引用，保存期
        # 闭包才标 missing）。
        for domain in self._RESOURCE_VALIDATED_DOMAINS:
            if domain not in domains:
                continue
            data = payload.get(domain)
            if not isinstance(data, Mapping):
                continue
            ref = str(data.get(f"{domain}_ref") or "")
            if not ref:
                continue
            valid = (self._lut_reference_valid(ref) if domain == "lut"
                     else self._bake_reference_valid(ref))
            if not valid:
                return self._fail(graph, "resource_unknown",
                                  f"粘贴的 {domain} 资源未注册: {ref}")
        # N12-B：缺插件占位诊断（保留实例 + 标记，不静默丢弃/不阻断）
        plugin_placeholders: list[str] = []
        if "effects" in domains:
            for plugin in (payload.get("effects")
                           if isinstance(payload.get("effects"),
                                         (list, tuple)) else ()):
                if isinstance(plugin, Mapping):
                    pid = str(plugin.get("plugin_id") or "")
                    if pid and self._effect_schema(pid) is None:
                        plugin_placeholders.append(pid)
        targets = list(node_ids)
        if not targets:
            return self._fail(graph, "illegal_param_key", "粘贴目标为空")
        for node_id in targets:
            node = self._node(graph, node_id)
            if node is None or node.known_type() not in (
                    NodeType.CORRECTOR, NodeType.SHARED_CORRECTOR,
                    NodeType.OUTSIDE):
                return self._fail(graph, "node_not_found",
                                  f"粘贴目标不是 Corrector: {node_id}",
                                  node_id=node_id)
            if node.params.get("locked"):
                return self._fail(graph, "node_locked",
                                  "节点已锁定，先解锁再粘贴", node_id=node_id)

        def paste(node: ColorNodeData) -> ColorNodeData:
            params = dict(node.params)
            legacy = dict(node.legacy_payload or {})
            for domain in domains:
                data = payload.get(domain)
                if data is None:
                    continue
                param_key, legacy_key = self._PASTE_DOMAIN_TARGETS[domain]
                if domain == "effects":
                    # 重建本地身份（新 instance_id；不共享来源身份）。
                    # N12-B：缺插件实例保留 + missing 占位标记（执行期
                    # shader_missing 既有 fail-closed；UI/预检可见）。
                    rebuilt = []
                    for plugin in data if isinstance(data, (list, tuple)) else ():
                        if isinstance(plugin, Mapping):
                            plugin = dict(plugin)
                            plugin["instance_id"] = self._id_factory()
                            pid = str(plugin.get("plugin_id") or "")
                            if pid and self._effect_schema(pid) is None:
                                plugin["missing"] = True
                            rebuilt.append(plugin)
                    legacy["plugins"] = rebuilt
                elif domain == "lut":
                    resources = dict(params.get("resources") or {})
                    if isinstance(data, Mapping) and data.get("lut_ref"):
                        resources["lut_ref"] = data["lut_ref"]
                        resources["lut_strength"] = data.get("lut_strength", 1.0)
                    else:
                        resources.pop("lut_ref", None)
                        resources.pop("lut_strength", None)
                    params["resources"] = resources
                elif domain == "bake":
                    # bake 结果引用单槽粘贴（R06-B 资源域；校验已在前置）
                    resources = dict(params.get("resources") or {})
                    if isinstance(data, Mapping) and data.get("bake_ref"):
                        resources["bake_ref"] = data["bake_ref"]
                    else:
                        resources.pop("bake_ref", None)
                    params["resources"] = resources
                elif domain == "window":
                    shapes = []
                    for shape in data if isinstance(data, (list, tuple)) else ():
                        if isinstance(shape, Mapping):
                            shape = dict(shape)
                            if isinstance(shape.get("id"), str):
                                shape["id"] = self._id_factory()
                            shapes.append(shape)
                    legacy["mask_shapes"] = shapes
                elif param_key is not None:
                    params[param_key] = copy.deepcopy(data)
                elif legacy_key is not None:
                    legacy[legacy_key] = copy.deepcopy(data)
            return replace(node, params=params, legacy_payload=legacy)

        return self._patch_nodes_with(graph, targets, paste)

    # ═══ N03：非 primary 工具域（Qualifier / Window）typed handle 写入 ═══

    def _tool_target_or_fail(self, graph: ColorGraphData,
                             node_id: str, *, domain: str) -> Any:
        """Qualifier/Window 域的目标节点校验（Corrector 家族 + 锁）。"""
        node = self._node(graph, node_id)
        if node is None or node.known_type() not in (
                NodeType.CORRECTOR, NodeType.SHARED_CORRECTOR,
                NodeType.OUTSIDE):
            return self._fail(graph, "node_not_found",
                              f"{domain}目标不是 Corrector: {node_id}",
                              node_id=node_id)
        if node.params.get("locked"):
            return self._fail(graph, "node_locked",
                              f"节点已锁定，先解锁再修改{domain}", node_id=node_id)
        return node

    def op_set_node_qualifier(self, graph: ColorGraphData, node_id: str,
                              qualifier: Mapping[str, Any]) -> GraphEditResult:
        """N03：Qualifier 域 merge patch（``params["qualifier"]``）。

        投影侧 ``_DICT_PARAM_OVERLAYS`` 已把该域并入 ``qualifier_params``
        （legacy 基底 + 图命令域覆盖）——本 op 是其第一个写入者。浅合并：
        面板/吸管手势只 patch 变化的键；数值/枚举值经 schema 冻结校验。
        """
        node = self._tool_target_or_fail(graph, node_id, domain="Qualifier ")
        if not isinstance(node, ColorNodeData):
            return node  # type: ignore[return-value] — _fail 的 GraphEditResult
        if not isinstance(qualifier, Mapping):
            return self._fail(graph, "illegal_param_key",
                              "qualifier 必须是 Mapping", node_id=node_id)
        merged = dict(node.params.get("qualifier") or {})
        merged.update({str(key): value
                       for key, value in qualifier.items()})
        params = dict(node.params)
        params["qualifier"] = merged
        try:
            new_node = replace(node, params=params)
        except GraphSchemaError as exc:
            return self._fail(graph, exc.code, str(exc), node_id=node_id)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        return self._commit(graph, replace(graph, nodes=nodes), {})

    @staticmethod
    def _patch_shape_tree(shapes: Sequence[Any], shape_id: str,
                          attributes: Mapping[str, Any]) -> tuple[list, bool]:
        """窗口 dict 树内定位 shape_id 并浅合并 attributes。

        返回 (新树, 是否命中)。群组经 ``children`` 递归；未命中分支原样
        保留（结构共享，不整体深拷贝）。
        """
        new_items: list = []
        patched = False
        for item in shapes:
            if (not patched and isinstance(item, Mapping)
                    and item.get("id") == shape_id):
                candidate = dict(item)
                candidate.update(dict(attributes))
                new_items.append(candidate)
                patched = True
            elif (isinstance(item, Mapping)
                    and isinstance(item.get("children"), (list, tuple))):
                children, child_patched = ColorGraphService._patch_shape_tree(
                    list(item["children"]), shape_id, attributes)
                if child_patched:
                    candidate = dict(item)
                    candidate["children"] = children
                    new_items.append(candidate)
                    patched = True
                else:
                    new_items.append(item)
            else:
                new_items.append(item)
        return new_items, patched

    def op_update_node_window_shape(self, graph: ColorGraphData,
                                    node_id: str, shape_id: str,
                                    attributes: Mapping[str, Any],
                                    ) -> GraphEditResult:
        """N03：单窗口形状属性 patch（几何/羽化/不透明度等拖动防抖）。

        身份不变（shape_id 原样保留——画布选中/跟踪绑定稳定）；窗口域
        存于 ``legacy_payload.mask_shapes``（bitmap 引用等路径字符串只在
        迁移载体豁免，params 禁止）。MaskGroup 仅接受 opacity/visible/
        invert/locked/operation（与 legacy ``process_mask_update`` 同集）。
        """
        node = self._tool_target_or_fail(graph, node_id, domain="窗口 ")
        if not isinstance(node, ColorNodeData):
            return node  # type: ignore[return-value]
        if not isinstance(attributes, Mapping) or not attributes:
            return self._fail(graph, "illegal_param_key",
                              "attributes 必须是非空 Mapping", node_id=node_id)
        legacy = unfreeze_params(dict(node.legacy_payload or {}))
        shapes = list(legacy.get("mask_shapes") or [])
        new_shapes, patched = self._patch_shape_tree(
            shapes, str(shape_id), dict(attributes))
        if not patched:
            return self._fail(graph, "shape_not_found",
                              f"窗口形状不存在: {shape_id}",
                              node_id=node_id)
        legacy["mask_shapes"] = new_shapes
        try:
            new_node = replace(node, legacy_payload=legacy)
        except GraphSchemaError as exc:
            return self._fail(graph, exc.code, str(exc), node_id=node_id)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        return self._commit(graph, replace(graph, nodes=nodes), {})

    def _rebuild_window_ids(self, shapes: Sequence[Any]) -> list:
        """递归为缺 id 的窗口项分配新身份（给定 id 保留——画布/跟踪绑定
        稳定；跨节点复制场景由调用方先改 id）。"""
        rebuilt: list = []
        for item in shapes:
            if not isinstance(item, Mapping):
                rebuilt.append(item)
                continue
            candidate = dict(item)
            if not isinstance(candidate.get("id"), str) or not candidate["id"]:
                candidate["id"] = self._id_factory()
            children = candidate.get("children")
            if isinstance(children, (list, tuple)):
                candidate["children"] = self._rebuild_window_ids(list(children))
            rebuilt.append(candidate)
        return rebuilt

    def op_set_node_window_shapes(self, graph: ColorGraphData, node_id: str,
                                  shapes: Sequence[Any]) -> GraphEditResult:
        """N03：窗口域整体替换（结构操作：增/删/复制/建组/层级/清空）。

        ``shapes`` 为 ``MaskShape/MaskGroup.to_dict()`` 列表（活对象由
        调用方序列化）；给定 id 保留，缺 id 补新。一次调用 = 一次原子
        command = 一次 undo；legacy undo 快照（history.push_state）在图
        模式由调用方跳过。
        """
        node = self._tool_target_or_fail(graph, node_id, domain="窗口 ")
        if not isinstance(node, ColorNodeData):
            return node  # type: ignore[return-value]
        if not isinstance(shapes, (list, tuple)):
            return self._fail(graph, "illegal_param_key",
                              "shapes 必须是列表", node_id=node_id)
        legacy = unfreeze_params(dict(node.legacy_payload or {}))
        legacy["mask_shapes"] = self._rebuild_window_ids(list(shapes))
        try:
            new_node = replace(node, legacy_payload=legacy)
        except GraphSchemaError as exc:
            return self._fail(graph, exc.code, str(exc), node_id=node_id)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        return self._commit(graph, replace(graph, nodes=nodes), {})

    def op_reset_graph_parameters(self, graph: ColorGraphData) -> GraphEditResult:
        ids = [n.node_id for n in graph.nodes
               if n.known_type() not in (NodeType.SOURCE, NodeType.OUTPUT)]
        return self.op_reset_corrector_parameters(graph, ids)

    def op_reset_graph(self, graph: ColorGraphData) -> GraphEditResult:
        """Restore a neutral Corrector while retaining the graph's boundaries."""
        source = self._node(graph, graph.source_node_id)
        output = self._node(graph, graph.output_node_id)
        if source is None or output is None:
            return self._fail(graph, "terminal_count", "重置需要 Source 与 Output")
        node_id = self._id_factory()
        corrector = ColorNodeData(node_id=node_id, node_type=NodeType.CORRECTOR.value,
                                  params={"key_source_mode": "node_input"})
        positions = {key: pos for key, pos in graph.ui_state.positions.items()
                     if key in (source.node_id, output.node_id)}
        positions[node_id] = (0.0, 0.0)
        ui = replace(graph.ui_state, positions=positions, selected_ids=(node_id,),
                     active_node_id=node_id, layout_revision=graph.ui_state.layout_revision + 1)
        candidate = replace(
            graph, nodes=(source, corrector, output),
            edges=(_make_edge(source.node_id, "image", node_id, "image"),
                   _make_edge(node_id, "image", output.node_id, "image")),
            alpha_output_node_id=None, shared_definitions=(), ui_state=ui)
        return self._commit(graph, candidate, {node_id: node_id})

    # ── 布局/元信息（不动 render revision） ──

    def op_update_layout(self, graph: ColorGraphData,
                         positions: Mapping[str, Sequence[float]]
                         ) -> GraphEditResult:
        merged = dict(graph.ui_state.positions)
        for node_id, pos in positions.items():
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"布局引用不存在节点: {node_id}")
            if (not isinstance(pos, Sequence) or len(pos) != 2
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in pos)):
                return self._fail(graph, "illegal_param_key",
                                  f"布局坐标必须是两个有限数值: {node_id}", node_id=node_id)
            merged[node_id] = (float(pos[0]), float(pos[1]))
        ui = replace(graph.ui_state,
                     positions=merged,
                     layout_revision=graph.ui_state.layout_revision + 1)
        candidate = graph.with_layout(ui)
        return GraphEditResult(True, candidate, {}, {})

    def op_move_and_insert(self, graph: ColorGraphData,
                           positions: Mapping[str, Sequence[float]],
                           node_id: str, edge_id: str) -> GraphEditResult:
        """布局+结构一次事务（§6.2）：一个 command、一次 revision。"""
        step1 = self.op_update_layout(graph, positions)
        if not step1.ok:
            return step1
        edge = next((e for e in step1.graph.edges
                     if e.edge_id == edge_id), None)
        node = self._node(step1.graph, node_id)
        if edge is None or node is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"插入目标边/节点不存在: {edge_id}/{node_id}")
        ins = self._edges_into(step1.graph, node_id, "image")
        outs = self._edges_out(step1.graph, node_id, "image")
        if ins or outs:
            return self._fail(graph, "port_multi",
                              f"{node_id} 已接入链，move_and_insert 仅插入自由节点",
                              node_id=node_id)
        err = self._check_port(step1.graph, node_id, "image", PortDirection.IN)
        if err is not None:
            return GraphEditResult(False, graph, (err,))
        edges = [e for e in step1.graph.edges if e.edge_id != edge_id]
        edges += [
            _make_edge(edge.src_node, edge.src_port, node_id, "image"),
            _make_edge(node_id, "image", edge.dst_node, edge.dst_port, slot_id=edge.slot_id),
        ]
        candidate = replace(step1.graph, edges=tuple(edges),
                            graph_revision=graph.graph_revision + 1)
        errors = candidate.validate()
        if errors:
            return GraphEditResult(False, graph, tuple(errors))
        return GraphEditResult(True, candidate, {}, {})

    def op_set_node_metadata(self, graph: ColorGraphData, node_id: str,
                             label: str) -> GraphEditResult:
        return self._patch_nodes(graph, [node_id], label=str(label))

    def op_set_node_color(self, graph: ColorGraphData,
                          node_ids: Sequence[str], color: str) -> GraphEditResult:
        if color.lower() not in {
                "default", "red", "orange", "yellow", "green", "cyan",
                "blue", "purple", "pink"}:
            return self._fail(graph, "illegal_param_key", f"未知节点颜色: {color}")
        return self._patch_nodes(graph, node_ids, ui_color=color)

    def op_set_edit_lock(self, graph: ColorGraphData,
                         node_ids: Sequence[str], locked: bool) -> GraphEditResult:
        return self._patch_nodes(graph, node_ids, locked=locked)

    def op_set_cache_policy(self, graph: ColorGraphData,
                            node_ids: Sequence[str], mode: str) -> GraphEditResult:
        if mode.lower() not in {"auto", "on", "off"}:
            return self._fail(graph, "illegal_param_key", f"未知缓存策略: {mode}")
        return self._patch_nodes(graph, node_ids, cache_mode=mode.lower())

    # ════════════════════════════════════════════════════════════════
    # R03：资源操作（节点 effects 实例 + 节点 LUT）。
    # 事实来源：实例列表在 node.legacy_payload["plugins"]（B03 迁移载体，
    # legacy Layer 反向投影同源）；节点 LUT 在 node.params["resources"]
    # （lut_ref/lut_strength，B03 已登记的 schema 键）。所有 op 先验证
    # 目标 Corrector/锁/资源存在/实例归属，失败原图返回（reject 不入史）。
    # ════════════════════════════════════════════════════════════════

    #: 可挂资源的节点类型（Source/Output/Mixer/Compound 外壳显式拒绝）。
    _EFFECTABLE_TYPES = (NodeType.CORRECTOR.value,
                         NodeType.SHARED_CORRECTOR.value)

    def _default_effect_schema(self, resource_id: str):
        from src.features.color.core.plugin_manager import get_plugin_manager
        plugin = get_plugin_manager().get_plugin(resource_id)
        return plugin.get("params") if plugin else None

    def _default_lut_reference_valid(self, lut_ref: str) -> bool:
        from src.shared.color.lut_resource_registry import get_lut_resource_session
        return get_lut_resource_session().status_of(lut_ref) == "registered"

    def _default_bake_reference_valid(self, bake_ref: str) -> bool:
        from src.features.color.core.color_bake_result_registry import (
            get_bake_result_session,
        )
        return get_bake_result_session().status_of(bake_ref) == "registered"

    def _effect_schema(self, resource_id: str):
        provider = self._effect_schema_provider
        if provider is None:
            provider = self._default_effect_schema
        return provider(resource_id) if provider else None

    def _lut_reference_valid(self, lut_ref: str) -> bool:
        provider = self._lut_reference_validator
        if provider is None:
            provider = self._default_lut_reference_valid
        return bool(provider(lut_ref)) if provider else True

    def _bake_reference_valid(self, bake_ref: str) -> bool:
        provider = getattr(self, "_bake_reference_validator", None)
        if provider is None:
            provider = self._default_bake_reference_valid
        return bool(provider(bake_ref)) if provider else True

    def _resource_target(self, graph: ColorGraphData,
                         node_id: str) -> ColorNodeData | GraphEditResult:
        """目标校验：存在 → Corrector 家族 → 未锁。失败返回 _fail 结果。"""
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        if node.node_type not in self._EFFECTABLE_TYPES:
            return self._fail(
                graph, "node_not_effectable",
                f"目标必须是 Corrector（当前 {node.node_type}）", node_id=node_id)
        if node.params.get("locked"):
            return self._fail(graph, "node_locked",
                              "节点已锁定，先解锁再修改资源", node_id=node_id)
        return node

    @staticmethod
    def _validate_effect_param(entry: Mapping[str, Any], value: Any) -> str | None:
        """按 schema 验证单参数值；返回错误码或 None。

        未知类型不拦截（future schema 透传，UI 显示 unsupported）。
        """
        import math
        kind = str(entry.get("type", "slider"))
        if kind in ("slider", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "param_type_mismatch"
            if not math.isfinite(float(value)):
                return "param_type_mismatch"
            low, high = entry.get("min"), entry.get("max")
            if low is not None and value < low:
                return "param_out_of_range"
            if high is not None and value > high:
                return "param_out_of_range"
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or float(value) != int(value):
                return "param_type_mismatch"
            low, high = entry.get("min"), entry.get("max")
            if low is not None and value < low:
                return "param_out_of_range"
            if high is not None and value > high:
                return "param_out_of_range"
        elif kind in ("checkbox", "bool", "boolean"):
            if not isinstance(value, (bool, int)) or (
                    isinstance(value, int) and value not in (0, 1)):
                return "param_type_mismatch"
        elif kind in ("select", "enum", "choice"):
            options = entry.get("options") or []
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                return "param_type_mismatch"
            if isinstance(value, int) and options and not (0 <= value < len(options)):
                return "param_out_of_range"
        elif kind == "color":
            if not isinstance(value, (list, tuple)) or len(value) < 3 \
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                               and math.isfinite(float(v)) for v in value[:3]):
                return "param_type_mismatch"
        return None

    def _merge_effect_params(self, schema: Any, params: Any,
                             node_id: str, graph: ColorGraphData,
                             *, resource_id: str) -> tuple[dict | None, GraphEditResult | None]:
        """默认值构造 + 显式参数校验；未知键 fail-visible。"""
        entries = [p for p in (schema or []) if isinstance(p, Mapping)]
        merged: dict[str, Any] = {}
        for entry in entries:
            name = entry.get("var_name")
            if isinstance(name, str) and name:
                merged[name] = copy.deepcopy(entry.get("default"))
        if params is None:
            return merged, None
        if not isinstance(params, Mapping):
            return None, self._fail(graph, "illegal_param_key",
                                    "params 必须是 Mapping", node_id=node_id)
        known = {str(e.get("var_name")): e for e in entries}
        for key, value in dict(params).items():
            entry = known.get(str(key))
            if entry is None:
                return None, self._fail(
                    graph, "param_unknown",
                    f"参数不在资源 schema 内: {key}（资源 {resource_id}）",
                    node_id=node_id)
            error = self._validate_effect_param(entry, value)
            if error is not None:
                return None, self._fail(
                    graph, error,
                    f"参数 {key} 的值 {value!r} 不满足 schema 约束", node_id=node_id)
            merged[str(key)] = copy.deepcopy(value)
        return merged, None

    def _node_plugins(self, node: ColorNodeData) -> list[Any]:
        plugins = node.legacy_payload.get("plugins")
        return list(plugins) if isinstance(plugins, (list, tuple)) else []

    def _replace_node_payload(self, graph: ColorGraphData, node: ColorNodeData,
                              plugins: list[Any]) -> GraphEditResult:
        payload = dict(node.legacy_payload)
        payload["plugins"] = [dict(p) if isinstance(p, Mapping) else p
                              for p in plugins]
        new_node = replace(node, legacy_payload=payload)
        candidate = replace(graph, nodes=tuple(
            new_node if n.node_id == node.node_id else n for n in graph.nodes))
        return self._commit(graph, candidate, {})

    @staticmethod
    def _find_instance(plugins: list[Any], instance_id: str) -> int:
        for index, plugin in enumerate(plugins):
            if isinstance(plugin, Mapping) and \
                    plugin.get("instance_id") == instance_id:
                return index
        return -1

    def op_add_node_effect(self, graph: ColorGraphData, node_id: str,
                           resource_id: str, params: Any = None, *,
                           replace_existing: bool = False) -> GraphEditResult:
        """Add an effect, or replace the first slot on a Library drop."""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        schema = self._effect_schema(resource_id)
        if schema is None:
            return self._fail(graph, "resource_unknown",
                              f"效果资源不存在或不可用: {resource_id}",
                              node_id=node_id)
        merged, failure = self._merge_effect_params(
            schema, params, node_id, graph, resource_id=resource_id)
        if failure is not None or merged is None:
            return failure  # type: ignore[return-value]
        plugins = self._node_plugins(target)
        instance = {
            "instance_id": self._id_factory(),
            "plugin_id": resource_id,
            "params": merged,
            "enabled": True,
            "type": self._effect_plugin_type(resource_id),
        }
        if replace_existing:
            plugins[:1] = [instance]
        else:
            plugins.append(instance)
        result = self._replace_node_payload(graph, target, plugins)
        if replace_existing and result.ok:
            return replace(result, id_mapping={node_id: node_id})
        return result

    def op_insert_effect_on_edge(
        self, graph: ColorGraphData, edge_id: str, resource_id: str,
        label: str, position: Sequence[float],
    ) -> GraphEditResult:
        """Split the exact image wire and attach the effect in one graph edit."""
        edge = next((edge for edge in graph.edges if edge.edge_id == edge_id), None)
        if edge is None or _port_kind_of(graph, edge) is not PortKind.IMAGE:
            return self._fail(graph, "edge_endpoint_missing", "滤镜只能插入图像连线")
        inserted = self._insert_corrector_on_edge(graph, edge, position, label=label)
        if not inserted.ok:
            return inserted
        node_id = next(iter(inserted.id_mapping.values()))
        applied = self.op_add_node_effect(inserted.graph, node_id, resource_id)
        if not applied.ok:
            return GraphEditResult(False, graph, applied.diagnostics)
        return replace(applied, graph=replace(applied.graph, graph_revision=graph.graph_revision + 1),
                       id_mapping=inserted.id_mapping)

    def _effect_plugin_type(self, resource_id: str) -> str:
        """实例 type 字段与 legacy Layer.add_plugin 对齐（dctl/ai）。

        注入 schema 表（测试）不带类型信息时退回 dctl；生产默认查
        PluginManager 注册表。
        """
        if self._effect_schema_provider is not None:
            return "dctl"
        from src.features.color.core.plugin_manager import get_plugin_manager
        plugin = get_plugin_manager().get_plugin(resource_id)
        return str(plugin.get("type", "dctl")) if plugin else "dctl"
        plugins = self._node_plugins(target)
        plugins.append({
            "instance_id": self._id_factory(),
            "plugin_id": resource_id,
            "params": merged,
            "enabled": True,
            "type": plugin_type,
        })
        return self._replace_node_payload(graph, target, plugins)

    def op_remove_node_effect(self, graph: ColorGraphData, node_id: str,
                              instance_id: str) -> GraphEditResult:
        """删除指定实例（其他实例身份/顺序保持）。"""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        plugins = self._node_plugins(target)
        index = self._find_instance(plugins, instance_id)
        if index < 0:
            return self._fail(graph, "instance_not_found",
                              f"实例不属于该节点: {instance_id}", node_id=node_id)
        del plugins[index]
        return self._replace_node_payload(graph, target, plugins)

    def op_move_node_effect(self, graph: ColorGraphData, node_id: str,
                            instance_id: str, index: int) -> GraphEditResult:
        """重排实例（其他身份保持；越界拒绝）。"""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        plugins = self._node_plugins(target)
        current = self._find_instance(plugins, instance_id)
        if current < 0:
            return self._fail(graph, "instance_not_found",
                              f"实例不属于该节点: {instance_id}", node_id=node_id)
        if isinstance(index, bool) or not isinstance(index, int) \
                or not 0 <= index < len(plugins):
            return self._fail(graph, "illegal_index",
                              f"重排下标越界: {index}", node_id=node_id)
        plugin = plugins.pop(current)
        plugins.insert(index, plugin)
        return self._replace_node_payload(graph, target, plugins)

    def op_set_node_effect_enabled(self, graph: ColorGraphData, node_id: str,
                                   instance_id: str, enabled: Any) -> GraphEditResult:
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        if not isinstance(enabled, bool):
            return self._fail(graph, "param_type_mismatch",
                              "enabled 必须是 bool", node_id=node_id)
        plugins = self._node_plugins(target)
        index = self._find_instance(plugins, instance_id)
        if index < 0:
            return self._fail(graph, "instance_not_found",
                              f"实例不属于该节点: {instance_id}", node_id=node_id)
        plugins[index] = dict(plugins[index], enabled=enabled)
        return self._replace_node_payload(graph, target, plugins)

    def op_set_node_effect_parameter(self, graph: ColorGraphData, node_id: str,
                                      instance_id: str, name: str,
                                      value: Any) -> GraphEditResult:
        """单参数编辑（按资源 schema 验证类型/范围；未知参数拒绝——
        控件只编辑已知字段，旧实例的未知字段由本 op 天然保留）。"""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        plugins = self._node_plugins(target)
        index = self._find_instance(plugins, instance_id)
        if index < 0:
            return self._fail(graph, "instance_not_found",
                              f"实例不属于该节点: {instance_id}", node_id=node_id)
        resource_id = str(plugins[index].get("plugin_id", ""))
        schema = self._effect_schema(resource_id)
        if schema is None:
            return self._fail(graph, "resource_unknown",
                              f"实例引用的资源不可用: {resource_id}",
                              node_id=node_id)
        entry = next((e for e in schema if isinstance(e, Mapping)
                      and e.get("var_name") == name), None)
        if entry is None:
            return self._fail(graph, "param_unknown",
                              f"参数不在资源 schema 内: {name}", node_id=node_id)
        error = self._validate_effect_param(entry, value)
        if error is not None:
            return self._fail(graph, error,
                              f"参数 {name} 的值 {value!r} 不满足 schema 约束",
                              node_id=node_id)
        params = dict(plugins[index].get("params") or {})
        params[name] = copy.deepcopy(value)
        plugins[index] = dict(plugins[index], params=params)
        return self._replace_node_payload(graph, target, plugins)

    def op_reset_node_effect(self, graph: ColorGraphData, node_id: str,
                             instance_id: str) -> GraphEditResult:
        """重置该实例的已知参数为 schema 默认值；未知字段原样保留。"""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        plugins = self._node_plugins(target)
        index = self._find_instance(plugins, instance_id)
        if index < 0:
            return self._fail(graph, "instance_not_found",
                              f"实例不属于该节点: {instance_id}", node_id=node_id)
        resource_id = str(plugins[index].get("plugin_id", ""))
        schema = self._effect_schema(resource_id)
        if schema is None:
            return self._fail(graph, "resource_unknown",
                              f"实例引用的资源不可用: {resource_id}",
                              node_id=node_id)
        known = {str(e.get("var_name")) for e in schema
                 if isinstance(e, Mapping)}
        params = {key: value for key, value in
                  dict(plugins[index].get("params") or {}).items()
                  if key not in known}
        for entry in schema:
            if isinstance(entry, Mapping) and entry.get("var_name"):
                params[str(entry["var_name"])] = copy.deepcopy(entry.get("default"))
        plugins[index] = dict(plugins[index], params=params)
        return self._replace_node_payload(graph, target, plugins)

    def _replace_node_resources(self, graph: ColorGraphData, node: ColorNodeData,
                                 resources: dict[str, Any]) -> GraphEditResult:
        params = dict(node.params)
        params["resources"] = resources
        new_node = replace(node, params=params)
        candidate = replace(graph, nodes=tuple(
            new_node if n.node_id == node.node_id else n for n in graph.nodes))
        return self._commit(graph, candidate, {})

    def op_set_node_lut(self, graph: ColorGraphData, node_id: str,
                        lut_ref: str, strength: Any = 1.0) -> GraphEditResult:
        """设置/替换节点 LUT（单槽替换，不叠加；强度一并落盘）。"""
        import math
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        if lut_ref and not self._lut_reference_valid(lut_ref):
            return self._fail(graph, "resource_unknown",
                              f"LUT 资源未注册: {lut_ref}", node_id=node_id)
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) \
                or not math.isfinite(float(strength)) or not 0.0 <= float(strength) <= 1.0:
            return self._fail(graph, "param_out_of_range",
                              f"lut strength 非法: {strength!r}", node_id=node_id)
        resources = dict(target.params.get("resources") or {})
        if lut_ref:
            resources["lut_ref"] = lut_ref
            resources["lut_strength"] = float(strength)
        else:
            resources.pop("lut_ref", None)
            resources.pop("lut_strength", None)
        return self._replace_node_resources(graph, target, resources)

    def op_set_node_lut_strength(self, graph: ColorGraphData, node_id: str,
                                 strength: Any) -> GraphEditResult:
        import math
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) \
                or not math.isfinite(float(strength)) or not 0.0 <= float(strength) <= 1.0:
            return self._fail(graph, "param_out_of_range",
                              f"lut strength 非法: {strength!r}", node_id=node_id)
        if not target.params.get("resources", {}).get("lut_ref"):
            return self._fail(graph, "param_unknown",
                              "节点未持有 LUT，先设置 LUT 再调强度", node_id=node_id)
        resources = dict(target.params.get("resources") or {})
        resources["lut_strength"] = float(strength)
        return self._replace_node_resources(graph, target, resources)

    def op_remove_node_lut(self, graph: ColorGraphData,
                           node_id: str) -> GraphEditResult:
        """只移除 LUT（保留其他调色与 resources 内其他键）。"""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        resources = dict(target.params.get("resources") or {})
        resources.pop("lut_ref", None)
        resources.pop("lut_strength", None)
        return self._replace_node_resources(graph, target, resources)

    def op_set_node_bake(self, graph: ColorGraphData, node_id: str,
                         bake_ref: str) -> GraphEditResult:
        """R06-B：设置/替换节点 bake 结果引用（单槽；验证同 LUT 模式）。"""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        if bake_ref and not self._bake_reference_valid(bake_ref):
            return self._fail(graph, "resource_unknown",
                              f"bake 结果未注册: {bake_ref}", node_id=node_id)
        resources = dict(target.params.get("resources") or {})
        if bake_ref:
            resources["bake_ref"] = bake_ref
        else:
            resources.pop("bake_ref", None)
        return self._replace_node_resources(graph, target, resources)

    def op_remove_node_bake(self, graph: ColorGraphData,
                            node_id: str) -> GraphEditResult:
        """R06-B：移除节点 bake 引用（保留 resources 其他键）。"""
        target = self._resource_target(graph, node_id)
        if isinstance(target, GraphEditResult):
            return target
        resources = dict(target.params.get("resources") or {})
        resources.pop("bake_ref", None)
        return self._replace_node_resources(graph, target, resources)

    # ════════════════════════════════════════════════════════════════
    # Compound / Shared 定义
    # ════════════════════════════════════════════════════════════════

    def op_group_subgraph(self, graph: ColorGraphData,
                          member_ids: Sequence[str]) -> GraphEditResult:
        """成员打包 compound：成员整体移入 children 子图，外部边改接
        compound 边界端口（B11 完整 mapping；此处落定结构）。
        """
        ids = set(member_ids)
        if not ids:
            return self._fail(graph, "illegal_param_key", "group 集合为空")
        protected = {graph.source_node_id, graph.output_node_id,
                     graph.alpha_output_node_id} - {None}
        if ids & protected:
            return self._fail(graph, "terminal_count",
                              "端子节点不能进 compound")
        for node_id in ids:
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"成员不存在: {node_id}", node_id=node_id)
        compound_id = self._id_factory()
        # 边界：指向成员集合的外部边 → compound.image 入；成员集合出 → 出
        incoming = [e for e in graph.edges
                    if e.dst_node in ids and e.src_node not in ids]
        outgoing = [e for e in graph.edges
                    if e.src_node in ids and e.dst_node not in ids]
        if len(incoming) > 1 or len(outgoing) > 1:
            return self._fail(
                graph, "ambiguous_reconnect",
                f"成员集合有 {len(incoming)} 入 {len(outgoing)} 出边界，"
                "第一版 compound 只支持单入单出连续段", node_id=sorted(ids)[0])
        # 子图：成员 + 内部边 + 虚拟 Source/Output（对齐端子规则）
        inner_nodes = tuple(replace(n) for n in graph.nodes if n.node_id in ids)
        inner_edges = tuple(_remap_edge(e, {}) for e in graph.edges
                            if e.src_node in ids and e.dst_node in ids)
        inner_source = ColorNodeData(
            node_id=f"{compound_id}/source", node_type=NodeType.SOURCE.value)
        inner_output = ColorNodeData(
            node_id=f"{compound_id}/output", node_type=NodeType.OUTPUT.value)
        child_edges = list(inner_edges)
        if incoming:
            head = incoming[0].dst_node
            child_edges.append(_make_edge(inner_source.node_id, "image",
                                          head, "image"))
        if outgoing:
            tail = outgoing[0].src_node
            child_edges.append(_make_edge(tail, "image",
                                          inner_output.node_id, "image"))
        children = ColorGraphData(
            nodes=(inner_source,) + inner_nodes + (inner_output,),
            edges=tuple(child_edges),
            source_node_id=inner_source.node_id,
            output_node_id=inner_output.node_id)
        if children.validate():
            return GraphEditResult(
                False, graph,
                (GraphSchemaError("invalid_graph",
                                  f"compound 内部图非法: {children.validate()[0]}"),))
        compound = ColorNodeData(
            node_id=compound_id, node_type=NodeType.COMPOUND.value,
            compound_children=children)
        # 外层：成员移除，边界边改接 compound
        outer_edges = [e for e in graph.edges
                       if e.src_node not in ids and e.dst_node not in ids]
        if incoming:
            outer_edges.append(_make_edge(incoming[0].src_node,
                                          incoming[0].src_port,
                                          compound_id, "image"))
        if outgoing:
            outer_edges.append(_make_edge(compound_id, "image",
                                          outgoing[0].dst_node,
                                          outgoing[0].dst_port))
        candidate = replace(graph,
                            nodes=tuple(n for n in graph.nodes
                                        if n.node_id not in ids) + (compound,),
                            edges=tuple(outer_edges))
        return self._commit(graph, candidate, {compound_id: compound_id})

    def op_ungroup_subgraph(self, graph: ColorGraphData,
                            node_id: str) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None or node.node_type != NodeType.COMPOUND.value:
            return self._fail(graph, "node_not_found",
                              f"compound 不存在: {node_id}", node_id=node_id)
        children = node.compound_children
        if children is None:
            return self._fail(graph, "invalid_graph",
                              "compound 无内部子图（损坏数据）", node_id=node_id)
        # 内部节点抬升（去掉虚拟 Source/Output），边界边恢复
        promoted = tuple(n for n in children.nodes
                         if n.node_type not in (NodeType.SOURCE.value,
                                                NodeType.OUTPUT.value))
        remap = {n.node_id: n.node_id for n in promoted}
        outer_edges = [e for e in graph.edges
                       if e.src_node != node_id and e.dst_node != node_id]
        # 内部边抬升（去掉指向/来自虚拟端子的部分）
        outer_edges += [e for e in children.edges
                        if e.src_node != children.source_node_id
                        and e.dst_node != children.output_node_id]
        into_compound = [e for e in graph.edges if e.dst_node == node_id]
        out_compound = [e for e in graph.edges if e.src_node == node_id]
        if into_compound and out_compound:
            # 内部链头尾（对齐 children 的虚拟端子连接）
            head = next((e.dst_node for e in children.edges
                         if e.src_node == children.source_node_id), None)
            tail = next((e.src_node for e in children.edges
                         if e.dst_node == children.output_node_id), None)
            if head and tail:
                outer_edges.append(_make_edge(
                    into_compound[0].src_node, into_compound[0].src_port,
                    head, "image"))
                outer_edges.append(_make_edge(
                    tail, "image", out_compound[0].dst_node,
                    out_compound[0].dst_port))
        candidate = replace(
            graph,
            nodes=tuple(n for n in graph.nodes if n.node_id != node_id)
            + promoted,
            edges=tuple(outer_edges))
        return self._commit(graph, candidate, remap)

    def op_create_shared_definition(self, graph: ColorGraphData,
                                    node_id: str,
                                    name: str = "Shared",
                                    param_domains: Sequence[str] = (),
                                    locked: bool = False,
                                    ) -> GraphEditResult:
        """把 Corrector 参数抽为共享定义；节点变 SharedCorrector 引用。"""
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found",
                              f"节点不存在: {node_id}", node_id=node_id)
        if node.node_type not in (NodeType.CORRECTOR.value,
                                  NodeType.SHARED_CORRECTOR.value):
            return self._fail(graph, "unknown_node_type",
                              f"{node.node_type} 不能抽为共享定义",
                              node_id=node_id)
        definition_id = self._id_factory()
        definition = SharedDefinitionData(
            definition_id=definition_id, name=name,
            node_type=NodeType.CORRECTOR.value,
            params=dict(node.params))
        new_node = replace(node, node_type=NodeType.SHARED_CORRECTOR.value,
                           shared_definition_id=definition_id,
                           params={**dict(node.params), **({"locked": True} if locked else {})})
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes,
                            shared_definitions=graph.shared_definitions
                            + (definition,))
        return self._commit(graph, candidate,
                            {definition_id: definition_id})

    def op_update_shared_definition_params(self, graph: ColorGraphData,
                                            definition_id: str,
                                            params: Mapping[str, Any]
                                            ) -> GraphEditResult:
        """更新共享定义参数：所有引用该定义的图 revision 一起失效（fanout
        由 bridge 跨栈执行；本 op 负责单图内定义版本+引用节点缓存失效）。"""
        definition = next((d for d in graph.shared_definitions
                           if d.definition_id == definition_id), None)
        if definition is None:
            return self._fail(graph, "node_not_found",
                              f"shared 定义不存在: {definition_id}")
        referenced = any(n.shared_definition_id == definition_id
                         for n in graph.nodes)
        new_definition = replace(definition,
                                 params=dict(params),
                                 version=definition.version + 1)
        definitions = tuple(new_definition if d.definition_id == definition_id
                            else d for d in graph.shared_definitions)
        candidate = replace(graph, shared_definitions=definitions)
        # 无引用节点时定义更新不改 render revision（执行策略级）
        return self._commit(graph, candidate, {}, render_change=referenced)

    def op_unlock_stack(self, graph: ColorGraphData) -> GraphEditResult:
        """解锁由 stack 级元信息承载（with_graph_locked）；占位 op 用于
        command 路由统一。"""
        return GraphEditResult(True, graph, {}, {})

    def op_make_local_corrector(self, graph: ColorGraphData,
                                node_ids: Sequence[str]) -> GraphEditResult:
        """深复制定义为本地参数，解除引用（不解除用户锁）。"""
        definitions = {d.definition_id: d for d in graph.shared_definitions}
        patched: list[ColorNodeData] = []
        for node_id in node_ids:
            node = self._node(graph, node_id)
            if node is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)
            ref = node.shared_definition_id
            if ref is None or ref not in definitions:
                return self._fail(graph, "node_not_found",
                                  f"{node_id} 无可解除的 shared 引用",
                                  node_id=node_id)
            local_params = dict(definitions[ref].params)
            local_params.update({k: v for k, v in node.params.items()
                                 if k not in definitions[ref].params})
            patched.append(replace(node,
                                   node_type=NodeType.CORRECTOR.value,
                                   shared_definition_id=None,
                                   params=local_params))
        by_id = {n.node_id: n for n in patched}
        nodes = tuple(by_id.get(n.node_id, n) for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    # ── 参数/元信息内部工具 ──

    def _patch_nodes(self, graph: ColorGraphData,
                     node_ids: Sequence[str], **fields: Any) -> GraphEditResult:
        param_fields = {"ui_color", "locked", "cache_mode"}
        direct = {k: v for k, v in fields.items() if k not in param_fields}

        def patch(node: ColorNodeData) -> ColorNodeData:
            if set(fields) & param_fields:
                params = dict(node.params)
                for key in set(fields) & param_fields:
                    params[key] = fields[key]
                return replace(node, params=params, **direct)
            return replace(node, **direct)
        return self._patch_nodes_with(graph, node_ids, patch)

    def _patch_nodes_with(self, graph: ColorGraphData,
                          node_ids: Sequence[str],
                          transform: Callable[[ColorNodeData], ColorNodeData]
                          ) -> GraphEditResult:
        ids = set(node_ids)
        if not ids:
            return self._fail(graph, "illegal_param_key", "目标集合为空")
        for node_id in ids:
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)
        nodes = tuple(transform(n) if n.node_id in ids else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})


def _port_kind_of(graph: ColorGraphData, edge: ColorEdgeData) -> PortKind | None:
    node = next((n for n in graph.nodes if n.node_id == edge.src_node), None)
    if node is None:
        return None
    spec = next((p for p in node.ports()
                 if p.port_id == edge.src_port
                 and p.direction is PortDirection.OUT), None)
    return spec.kind if spec else None


def _remap_edge(edge: ColorEdgeData, remap: Mapping[str, str]) -> ColorEdgeData:
    return ColorEdgeData(
        edge_id=edge.edge_id,
        src_node=remap.get(edge.src_node, edge.src_node),
        src_port=edge.src_port,
        dst_node=remap.get(edge.dst_node, edge.dst_node),
        dst_port=edge.dst_port,
        slot_id=edge.slot_id)


def can_export_lut(graph: ColorGraphData) -> tuple[bool, str]:
    # 可投影类型集合（与迁移严格投影同口径）
    from src.features.color.core.color_graph_migration import (
        _PROJECTABLE_TYPES,
    )
    """LUT 导出资格（B14.5）：图必须 pointwise 可表达。

    严格判定：仅 Source→Corrector…→Output 单链（无 key 边/混合器/
    splitter/compound/shared/alpha）；每个 Corrector 的 legacy_payload
    不含蒙版/限定器/跟踪/AI matte/插件/关键帧/变形器（空间或时域算子
    使 3D LUT 不可表达）。不满足返回 (False, 原因)——上层必须拒绝或
    要求显式排除，不自动丢弃效果导出"看似成功"的 cube。
    """
    if not isinstance(graph, ColorGraphData) or graph.validate():
        return False, "graph_invalid"
    if graph.shared_definitions or graph.alpha_output_node_id:
        return False, "shared_definitions_or_alpha_present"
    for node in graph.nodes:
        if node.node_type not in _PROJECTABLE_TYPES:
            return False, f"node_type_{node.node_type}"
        if node.node_type == NodeType.CORRECTOR.value:
            payload = dict(node.legacy_payload or {})
            # N10：图命令域（params.grade_keyframes）同为时域数据——
            # 同样阻断 pointwise LUT 导出
            params_keyframes = node.params.get("grade_keyframes") \
                if isinstance(node.params, Mapping) else None
            # KF-05：仅含墓碑（显式空覆盖）不算时域数据——删除后的
            # 轨道不再阻断 pointwise LUT 导出
            if isinstance(params_keyframes, Mapping) and any(
                    isinstance(t, Mapping) and t.get("keyframes")
                    for t in params_keyframes.values()):
                return False, "keyframes_temporal"
            for blocker, label in (
                    ("mask_shapes", "windows_masks"),
                    ("grade_keyframes", "keyframes_temporal"),
                    ("plugins", "plugins"),
                    ("warper_params", "warper_spatial"),
                    ("ai_matte", "ai_matte")):
                if payload.get(blocker):
                    return False, label
            qualifier = payload.get("qualifier_params") or {}
            if qualifier.get("enabled", False):
                return False, "qualifier_spatial"
    return True, "pointwise_serial_chain"
