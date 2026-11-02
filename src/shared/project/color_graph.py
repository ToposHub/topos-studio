"""ColorGraphData —— 调色节点图的共享工程数据模型（N01，B02）。

纯数据 + 验证 + 版本编解码；**不得** import color UI、Composite
NodeGraph、moderngl（共享 project 层约束，计划 §2.1）。

冻结契约（计划 §2.2/§2.3）：

- 节点稳定 UUID 是身份；显示 ``number`` 只是 UI 排版。边引用 typed
  ports（image/key），禁止 image→key 隐式转换。
- ``params`` 是可序列化的**不可变快照**：parse 边界 deepcopy + 递归
  freeze（dict→mappingproxy、list→tuple），外部原地修改被阻断；
  NaN/Inf、Qt/GPU 对象、机器绝对路径字符串一律拒绝。
- 未知字段 load→save 原样保留（未知 node type 保留 payload，导出依赖
  锥内阻断由 B04/B13 处理）。
- ``ui_state``（布局/标签/缩放/视口/多选/active）与 ``graph_revision``
  （render 语义）分离；移动节点不增加 render revision。
- 资源用工程引用（asset/payload id），不存绝对路径。
- 大小上限：节点 4096、边 32768、compound 嵌套 16（与 UI 草图一致；
  领域侧另行校验，不只信草图）。
"""

from __future__ import annotations

import copy
import math
import re
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence, Tuple

from src.shared.extra_fields import preserves_extras, split_extras

COLOR_GRAPH_SCHEMA_VERSION = 1

MAX_NODES = 4096
MAX_EDGES = 32768
MAX_COMPOUND_DEPTH = 16
MAX_PARAM_RECURSION = 8


class GraphSchemaError(ValueError):
    """parse/serialize 失败：带稳定诊断 code 与定位信息。

    code 取值（B02 冻结，B04 service 诊断沿用）：
    - ``unknown_node_type`` / ``duplicate_node_id`` / ``duplicate_edge_id``
    - ``non_finite_number`` / ``illegal_param_key`` / ``absolute_path``
    - ``port_not_found`` / ``port_direction`` / ``port_kind`` / ``port_multi``
    - ``node_not_found`` / ``edge_endpoint_missing``
    - ``size_limit`` / ``recursion_limit``
    - ``terminal_count`` / ``cycle``
    """

    def __init__(self, code: str, message: str, *,
                 node_id: str | None = None, edge_id: str | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.node_id = node_id
        self.edge_id = edge_id


class PortKind(Enum):
    IMAGE = "image"
    KEY = "key"


class PortDirection(Enum):
    IN = "in"
    OUT = "out"


class NodeType(Enum):
    SOURCE = "source"
    CORRECTOR = "corrector"
    PARALLEL_MIXER = "parallel_mixer"
    LAYER_MIXER = "layer_mixer"
    KEY_MIXER = "key_mixer"
    OUTSIDE = "outside"
    SPLITTER = "splitter"
    COMBINER = "combiner"
    COMPOUND = "compound"
    SHARED_CORRECTOR = "shared_corrector"
    OUTPUT = "output"
    ALPHA_OUTPUT = "alpha_output"


#: 未知 node type 的占位（保留 payload，只读浏览，B04 阻断导出）
UNKNOWN_NODE_TYPE = "__unknown__"

_ABSOLUTE_PATH_RE = re.compile(r"^(?:/|~|[A-Za-z]:[/\\])")


@dataclass(frozen=True)
class ColorPortSpec:
    """typed 端口声明。multi=True 表示有序多连接槽（mixer slots）。"""

    port_id: str
    kind: PortKind
    direction: PortDirection
    multi: bool = False
    label: str = ""


# ── 端口目录（§2.3；Corrector 的 key 语义由 params.key_source_mode 承载）──

_SOURCE_PORTS: Tuple[ColorPortSpec, ...] = (
    ColorPortSpec("image", PortKind.IMAGE, PortDirection.OUT),
)
_CORRECTOR_PORTS: Tuple[ColorPortSpec, ...] = (
    ColorPortSpec("image", PortKind.IMAGE, PortDirection.IN),
    ColorPortSpec("key_in", PortKind.KEY, PortDirection.IN),
    ColorPortSpec("image", PortKind.IMAGE, PortDirection.OUT),
    ColorPortSpec("key_out", PortKind.KEY, PortDirection.OUT),
)
_MIXER_BASE_PORTS: Tuple[ColorPortSpec, ...] = (
    ColorPortSpec("base", PortKind.IMAGE, PortDirection.IN),
    ColorPortSpec("branch", PortKind.IMAGE, PortDirection.IN, multi=True),
    ColorPortSpec("key", PortKind.KEY, PortDirection.IN, multi=True),
    ColorPortSpec("image", PortKind.IMAGE, PortDirection.OUT),
)

NODE_PORT_CATALOG: Mapping[NodeType, Tuple[ColorPortSpec, ...]] = MappingProxyType({
    NodeType.SOURCE: _SOURCE_PORTS,
    NodeType.CORRECTOR: _CORRECTOR_PORTS,
    NodeType.SHARED_CORRECTOR: _CORRECTOR_PORTS,
    NodeType.OUTSIDE: (
        ColorPortSpec("image", PortKind.IMAGE, PortDirection.IN),
        ColorPortSpec("key_in", PortKind.KEY, PortDirection.IN),
        ColorPortSpec("image", PortKind.IMAGE, PortDirection.OUT),
        ColorPortSpec("key_out", PortKind.KEY, PortDirection.OUT),
    ),
    NodeType.PARALLEL_MIXER: _MIXER_BASE_PORTS,
    NodeType.LAYER_MIXER: (
        ColorPortSpec("slot", PortKind.IMAGE, PortDirection.IN, multi=True),
        ColorPortSpec("image", PortKind.IMAGE, PortDirection.OUT),
    ),
    NodeType.KEY_MIXER: (
        ColorPortSpec("key", PortKind.KEY, PortDirection.IN, multi=True),
        ColorPortSpec("key", PortKind.KEY, PortDirection.OUT),
    ),
    NodeType.SPLITTER: (
        ColorPortSpec("image", PortKind.IMAGE, PortDirection.IN),
        ColorPortSpec("r", PortKind.IMAGE, PortDirection.OUT),
        ColorPortSpec("g", PortKind.IMAGE, PortDirection.OUT),
        ColorPortSpec("b", PortKind.IMAGE, PortDirection.OUT),
    ),
    NodeType.COMBINER: (
        ColorPortSpec("r", PortKind.IMAGE, PortDirection.IN),
        ColorPortSpec("g", PortKind.IMAGE, PortDirection.IN),
        ColorPortSpec("b", PortKind.IMAGE, PortDirection.IN),
        ColorPortSpec("image", PortKind.IMAGE, PortDirection.OUT),
    ),
    NodeType.COMPOUND: (),   # 由 compound mapping 定义（B11 展开时校验）
    NodeType.OUTPUT: (
        ColorPortSpec("image", PortKind.IMAGE, PortDirection.IN),
    ),
    NodeType.ALPHA_OUTPUT: (
        ColorPortSpec("key", PortKind.KEY, PortDirection.IN),
    ),
})


# ── params 冻结 ──────────────────────────────────────────────────────────

def freeze_params(value: Any, depth: int = 0, *,
                  context: str = "params",
                  allow_paths: bool = False) -> Any:
    """递归冻结参数容器：dict→mappingproxy、list→tuple；标量校验。

    拒绝：NaN/Inf、绝对路径字符串、不可序列化对象（Qt/GPU 句柄等）。
    parse/构造边界调用一次，之后的实例不可被外部原地修改。
    ``allow_paths=True`` 仅供 B03 ``legacy_payload``（迁移期原始 Layer
    载荷，路径在 B14 资源闭包统一转工程引用）——params 永远走默认严格态。
    """
    if depth > MAX_PARAM_RECURSION:
        raise GraphSchemaError("recursion_limit",
                               f"{context} 嵌套超过 {MAX_PARAM_RECURSION} 层")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GraphSchemaError(
                "non_finite_number", f"{context} 含 NaN/Inf: {value!r}")
        return float(value)
    if isinstance(value, str):
        if _ABSOLUTE_PATH_RE.match(value) and not allow_paths:
            raise GraphSchemaError(
                "absolute_path",
                f"{context} 含机器绝对路径 {value!r}；资源须用工程 asset/payload 引用")
        return str(value)
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise GraphSchemaError(
                    "illegal_param_key", f"{context} 键必须是字符串: {key!r}")
            frozen[key] = freeze_params(item, depth + 1,
                                        context=f"{context}.{key}",
                                        allow_paths=allow_paths)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_params(item, depth + 1,
                                   context=f"{context}[{i}]")
                     for i, item in enumerate(value))
    raise GraphSchemaError(
        "illegal_param_key",
        f"{context} 含不可序列化值 {type(value).__name__}")


def unfreeze_params(value: Any) -> Any:
    """freeze 的逆操作（to_dict 用）：mappingproxy→dict、tuple→list。"""
    if isinstance(value, MappingProxyType) or isinstance(value, Mapping):
        return {k: unfreeze_params(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [unfreeze_params(v) for v in value]
    return value


@dataclass(frozen=True)
class ColorNodeData:
    """图节点：稳定 UUID 身份 + 类型版本 + 冻结参数快照。"""

    node_id: str
    node_type: str                    # NodeType.value 或未知类型字符串
    type_version: int = 1
    params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    label: str = ""                   # UI 元信息（不是身份/执行顺序）
    shared_definition_id: str | None = None
    # B03 迁移期显式载体：Layer.to_dict(None) 原始载荷（含 active_lut_path
    # 等路径字符串与 bitmap 引用——freeze 豁免路径检查，B14 资源闭包时
    # 统一转工程 asset 引用）。反向投影 legacy layers 的事实来源。
    legacy_payload: Mapping[str, Any] = field(default_factory=dict)
    # Compound 内部子图（嵌套 ColorGraphData；depth 校验在 parse）
    compound_children: "ColorGraphData | None" = None
    # 未知 node type 的原始 payload 保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.node_id:
            raise GraphSchemaError("illegal_param_key", "node_id 不能为空")
        object.__setattr__(self, "params", freeze_params(
            dict(self.params), context=f"node[{self.node_id}].params"))
        if self.legacy_payload:
            object.__setattr__(self, "legacy_payload", freeze_params(
                dict(self.legacy_payload),
                context=f"node[{self.node_id}].legacy_payload",
                allow_paths=True))
        if self.node_type not in {t.value for t in NodeType}:
            # 未知类型：params 保留原样（只读浏览），不在此抛错
            pass
        if self.compound_children is not None and self.node_type != NodeType.COMPOUND.value:
            raise GraphSchemaError(
                "illegal_param_key",
                f"node[{self.node_id}] 类型 {self.node_type} 不能携带 compound_children")

    def known_type(self) -> NodeType | None:
        try:
            return NodeType(self.node_type)
        except ValueError:
            return None

    def ports(self) -> Tuple[ColorPortSpec, ...]:
        known = self.known_type()
        if known is None:
            return ()
        return NODE_PORT_CATALOG[known]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "type_version": self.type_version,
            "params": unfreeze_params(dict(self.params)),
            "enabled": self.enabled,
        }
        if self.label:
            data["label"] = self.label
        if self.shared_definition_id is not None:
            data["shared_definition_id"] = self.shared_definition_id
        if self.legacy_payload:
            data["legacy_payload"] = unfreeze_params(dict(self.legacy_payload))
        if self.compound_children is not None:
            data["compound_children"] = self.compound_children.to_dict()
        data.update(self._extra_fields)
        return data

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ColorNodeData":
        if not isinstance(data, Mapping):
            raise GraphSchemaError("illegal_param_key",
                                   f"node 载荷必须是 Mapping: {type(data)!r}")
        node_id = data.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise GraphSchemaError("illegal_param_key",
                                   f"node 缺 node_id: {data!r}"[:200])
        node_type = data.get("node_type")
        if not isinstance(node_type, str) or not node_type:
            raise GraphSchemaError("unknown_node_type",
                                   f"node[{node_id}] 缺 node_type")
        type_version = int(data.get("type_version", 1))
        if type_version < 1:
            raise GraphSchemaError("unknown_node_type",
                                   f"node[{node_id}] type_version < 1")
        legacy_raw = data.get("legacy_payload")
        children_raw = data.get("compound_children")
        children = (ColorGraphData.from_dict(children_raw)
                    if isinstance(children_raw, Mapping) else None)
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise GraphSchemaError("illegal_param_key",
                                   f"node[{node_id}] enabled 必须是 bool")
        return ColorNodeData(
            node_id=node_id,
            node_type=node_type,
            type_version=type_version,
            params=dict(data.get("params") or {}),
            enabled=enabled,
            label=str(data.get("label") or ""),
            shared_definition_id=data.get("shared_definition_id"),
            legacy_payload=(dict(legacy_raw)
                            if isinstance(legacy_raw, Mapping) else dict()),
            compound_children=children,
            _extra_fields=split_extras(data, ColorNodeData._KNOWN_KEYS),
        )

    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "node_id", "node_type", "type_version", "params", "enabled",
        "label", "shared_definition_id", "legacy_payload", "compound_children",
    })


@dataclass(frozen=True)
class ColorEdgeData:
    """typed 边：src/dst node+port。multi 端口按 slot_id 保序。"""

    edge_id: str
    src_node: str
    src_port: str
    dst_node: str
    dst_port: str
    slot_id: str = ""                 # multi 输入端口的稳定槽 ID（B04 分配）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "edge_id": self.edge_id,
            "src_node": self.src_node,
            "src_port": self.src_port,
            "dst_node": self.dst_node,
            "dst_port": self.dst_port,
        }
        if self.slot_id:
            data["slot_id"] = self.slot_id
        data.update(self._extra_fields)
        return data

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ColorEdgeData":
        try:
            edge = ColorEdgeData(
                edge_id=data["edge_id"],
                src_node=data["src_node"],
                src_port=data["src_port"],
                dst_node=data["dst_node"],
                dst_port=data["dst_port"],
                slot_id=str(data.get("slot_id") or ""),
                _extra_fields=split_extras(data, ColorEdgeData._KNOWN_KEYS),
            )
        except KeyError as exc:
            raise GraphSchemaError(
                "edge_endpoint_missing", f"edge 缺字段 {exc}: {dict(data)!r}"[:200]
            ) from None
        return edge

    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "edge_id", "src_node", "src_port", "dst_node", "dst_port", "slot_id",
    })


@dataclass(frozen=True)
class SharedDefinitionData:
    """共享 Corrector 定义：参数共享 ≠ 输入/缓存共享（B11 执行语义）。"""

    definition_id: str
    name: str
    node_type: str = NodeType.CORRECTOR.value
    type_version: int = 1
    version: int = 1
    params: Mapping[str, Any] = field(default_factory=dict)
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", freeze_params(
            dict(self.params), context=f"shared[{self.definition_id}].params"))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "definition_id": self.definition_id,
            "name": self.name,
            "node_type": self.node_type,
            "type_version": self.type_version,
            "version": self.version,
            "params": unfreeze_params(dict(self.params)),
        }
        data.update(self._extra_fields)
        return data

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "SharedDefinitionData":
        return SharedDefinitionData(
            definition_id=data["definition_id"],
            name=str(data.get("name") or ""),
            node_type=str(data.get("node_type") or NodeType.CORRECTOR.value),
            type_version=int(data.get("type_version", 1)),
            version=int(data.get("version", 1)),
            params=dict(data.get("params") or {}),
            _extra_fields=split_extras(data, SharedDefinitionData._KNOWN_KEYS),
        )

    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "definition_id", "name", "node_type", "type_version", "version", "params",
    })


def _check_reachability(nodes: Mapping[str, ColorNodeData],
                        edges: Sequence[ColorEdgeData],
                        source_id: str, output_id: str
                        ) -> list["GraphSchemaError"]:
    """每个节点必须从 Source 可达且可达 Output；否则报 isolated_node。

    未接线的合法形态（用户新建后暂未连线）在 schema 层允许——这是
    ``validate()`` 的**图级**规则，service 提交前可按阶段放宽提示级别；
    但引擎/hydrator 消费的图必须全连通（断链=丢效果）。
    """
    forward: dict[str, list[str]] = {nid: [] for nid in nodes}
    backward: dict[str, list[str]] = {nid: [] for nid in nodes}
    for edge in edges:
        if edge.src_node in nodes and edge.dst_node in nodes:
            forward[edge.src_node].append(edge.dst_node)
            backward[edge.dst_node].append(edge.src_node)

    def _reachable(start: str, adjacency: Mapping[str, list[str]]) -> set[str]:
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for nxt in adjacency.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    from_source = _reachable(source_id, forward)
    to_output = _reachable(output_id, backward)
    errors: list[GraphSchemaError] = []
    for nid in nodes:
        if nid not in from_source or nid not in to_output:
            errors.append(GraphSchemaError(
                "isolated_node",
                f"node[{nid}] 不在 Source→Output 连通域内（断链或孤立）",
                node_id=nid))
    return errors


@dataclass(frozen=True)
@preserves_extras
class ColorGraphUiState:
    """布局/UI 元信息（持久化但**不**参与 render revision）。"""

    positions: Mapping[str, Tuple[float, float]] = field(default_factory=dict)
    collapsed: bool = False
    zoom: float = 1.0
    viewport_x: float = 0.0
    viewport_y: float = 0.0
    selected_ids: Tuple[str, ...] = ()
    active_node_id: str = ""
    layout_revision: int = 0
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "positions": {k: list(v) for k, v in dict(self.positions).items()},
            "collapsed": self.collapsed,
            "zoom": self.zoom,
            "viewport_x": self.viewport_x,
            "viewport_y": self.viewport_y,
            "selected_ids": list(self.selected_ids),
            "active_node_id": self.active_node_id,
            "layout_revision": self.layout_revision,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ColorGraphUiState":
        if not isinstance(data, Mapping):
            return ColorGraphUiState()
        positions = {
            str(k): (float(v[0]), float(v[1]))
            for k, v in dict(data.get("positions") or {}).items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        }
        return ColorGraphUiState(
            positions=MappingProxyType(positions),
            collapsed=bool(data.get("collapsed", False)),
            zoom=float(data.get("zoom", 1.0)),
            viewport_x=float(data.get("viewport_x", 0.0)),
            viewport_y=float(data.get("viewport_y", 0.0)),
            selected_ids=tuple(str(s) for s in data.get("selected_ids") or ()),
            active_node_id=str(data.get("active_node_id") or ""),
            layout_revision=int(data.get("layout_revision", 0)),
            _extra_fields=split_extras(data, ColorGraphUiState._KNOWN_KEYS),
        )

    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "positions", "collapsed", "zoom", "viewport_x", "viewport_y",
        "selected_ids", "active_node_id", "layout_revision",
    })


def _validate_no_cycles(nodes: Mapping[str, ColorNodeData],
                        edges: Sequence[ColorEdgeData], *,
                        node_id: str | None = None) -> None:
    """image+key 边共同参与的有向无环校验（迭代 DFS，防递归爆栈）。"""
    adjacency: dict[str, list[str]] = {nid: [] for nid in nodes}
    for edge in edges:
        if edge.src_node in nodes and edge.dst_node in nodes:
            adjacency[edge.src_node].append(edge.dst_node)

    state: dict[str, int] = {}  # 0=visiting 1=done

    for start in nodes:
        if state.get(start):
            continue
        stack = [(start, iter(adjacency[start]))]
        state[start] = 0
        while stack:
            current, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[current] = 1
                stack.pop()
                continue
            if state.get(nxt) == 0:
                raise GraphSchemaError(
                    "cycle", f"图含环: {nxt} 参与环", node_id=nxt)
            if not state.get(nxt):
                state[nxt] = 0
                stack.append((nxt, iter(adjacency[nxt])))


@dataclass(frozen=True)
@preserves_extras
class ColorGraphData:
    """调色节点图权威数据（schema v1）。

    结构合法性（端口类型/方向/单连接/端子数量/环/大小）在 ``validate()``
    返回诊断列表；``from_dict`` 只拒绝 malformed 载荷（拒绝后不产生
    部分实例）。
    """

    graph_id: str = ""
    nodes: Tuple[ColorNodeData, ...] = ()
    edges: Tuple[ColorEdgeData, ...] = ()
    shared_definitions: Tuple[SharedDefinitionData, ...] = ()
    source_node_id: str = ""
    output_node_id: str = ""
    alpha_output_node_id: str | None = None
    working_color_context: Mapping[str, Any] = field(default_factory=dict)
    graph_revision: int = 0
    semantic_version: int = 1
    ui_state: ColorGraphUiState = field(default_factory=ColorGraphUiState)
    # 未来 reader 兼容门（含未知 node type 时写当前 schema + 1）
    minimum_reader_version: int = COLOR_GRAPH_SCHEMA_VERSION
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "graph_id", "nodes", "edges", "shared_definitions",
        "source_node_id", "output_node_id", "alpha_output_node_id",
        "working_color_context", "graph_revision", "semantic_version",
        "ui_state", "minimum_reader_version",
    })

    def __post_init__(self) -> None:
        if not self.graph_id:
            object.__setattr__(self, "graph_id", str(uuid.uuid4()))
        if len(self.nodes) > MAX_NODES:
            raise GraphSchemaError(
                "size_limit", f"节点数 {len(self.nodes)} > {MAX_NODES}")
        if len(self.edges) > MAX_EDGES:
            raise GraphSchemaError(
                "size_limit", f"边数 {len(self.edges)} > {MAX_EDGES}")
        ids = [n.node_id for n in self.nodes]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise GraphSchemaError(
                "duplicate_node_id", f"重复 node_id: {dupes[:4]}", node_id=dupes[0])
        edge_ids = [e.edge_id for e in self.edges]
        if len(set(edge_ids)) != len(edge_ids):
            raise GraphSchemaError("duplicate_edge_id", "重复 edge_id")
        object.__setattr__(self, "working_color_context", MappingProxyType(
            dict(self.working_color_context)))
        self._check_compound_depth(self.nodes, 1)

    @staticmethod
    def _check_compound_depth(nodes: Tuple[ColorNodeData, ...], depth: int) -> None:
        if depth > MAX_COMPOUND_DEPTH:
            raise GraphSchemaError(
                "recursion_limit", f"compound 嵌套超过 {MAX_COMPOUND_DEPTH}")
        for node in nodes:
            if node.compound_children is not None:
                ColorGraphData._check_compound_depth(
                    node.compound_children.nodes, depth + 1)

    # ── 工厂 ──

    def with_layout(self, ui_state: ColorGraphUiState) -> "ColorGraphData":
        """布局变更：ui_state/layout_revision 变，graph_revision 不变。"""
        return replace(self, ui_state=ui_state)

    def bump_revision(self) -> "ColorGraphData":
        return replace(self, graph_revision=self.graph_revision + 1)

    # ── 验证 ──

    def validate(self) -> list[GraphSchemaError]:
        """结构/语义校验，返回诊断列表（空 = 合法）。

        B04 service 在每个 intent 提交前调用；此处只做纯数据校验。
        """
        errors: list[GraphSchemaError] = []
        nodes = {n.node_id: n for n in self.nodes}

        # 端子规则：恰一 Source、恰一主 Output、AlphaOutput ≤1
        sources = [n for n in self.nodes
                   if n.node_type == NodeType.SOURCE.value]
        outputs = [n for n in self.nodes
                   if n.node_type == NodeType.OUTPUT.value]
        alphas = [n for n in self.nodes
                  if n.node_type == NodeType.ALPHA_OUTPUT.value]
        if len(sources) != 1:
            errors.append(GraphSchemaError(
                "terminal_count", f"Source 数量必须为 1，得到 {len(sources)}"))
        if len(outputs) != 1:
            errors.append(GraphSchemaError(
                "terminal_count", f"主 Output 数量必须为 1，得到 {len(outputs)}"))
        if len(alphas) > 1:
            errors.append(GraphSchemaError(
                "terminal_count", f"Alpha Output 最多 1 个，得到 {len(alphas)}"))
        if sources and self.source_node_id != sources[0].node_id:
            errors.append(GraphSchemaError(
                "terminal_count", "source_node_id 与 Source 节点不一致"))
        if outputs and self.output_node_id != outputs[0].node_id:
            errors.append(GraphSchemaError(
                "terminal_count", "output_node_id 与 Output 节点不一致"))

        # 边校验
        port_use: dict[Tuple[str, str], int] = {}
        for edge in self.edges:
            src = nodes.get(edge.src_node)
            dst = nodes.get(edge.dst_node)
            if src is None:
                errors.append(GraphSchemaError(
                    "node_not_found", f"边 {edge.edge_id} src 不存在: {edge.src_node}",
                    edge_id=edge.edge_id))
                continue
            if dst is None:
                errors.append(GraphSchemaError(
                    "node_not_found", f"边 {edge.edge_id} dst 不存在: {edge.dst_node}",
                    edge_id=edge.edge_id))
                continue
            src_port = next((p for p in src.ports()
                             if p.port_id == edge.src_port
                             and p.direction is PortDirection.OUT), None)
            dst_port = next((p for p in dst.ports()
                             if p.port_id == edge.dst_port
                             and p.direction is PortDirection.IN), None)
            if src_port is None:
                errors.append(GraphSchemaError(
                    "port_not_found",
                    f"边 {edge.edge_id} src 端口非法: {src.node_type}.{edge.src_port}",
                    edge_id=edge.edge_id))
                continue
            if dst_port is None:
                errors.append(GraphSchemaError(
                    "port_not_found",
                    f"边 {edge.edge_id} dst 端口非法: {dst.node_type}.{edge.dst_port}",
                    edge_id=edge.edge_id))
                continue
            if src_port.kind is not dst_port.kind:
                errors.append(GraphSchemaError(
                    "port_kind",
                    f"边 {edge.edge_id} 禁止 {src_port.kind.value}→{dst_port.kind.value}"
                    " 隐式转换", edge_id=edge.edge_id))
                continue
            if not dst_port.multi:
                key = (edge.dst_node, edge.dst_port)
                port_use[key] = port_use.get(key, 0) + 1
                if port_use[key] > 1:
                    errors.append(GraphSchemaError(
                        "port_multi",
                        f"单输入端口 {edge.dst_node}.{edge.dst_port} 有多连接",
                        edge_id=edge.edge_id))

        # shared 定义引用闭包
        definitions = {d.definition_id for d in self.shared_definitions}
        for node in self.nodes:
            if node.shared_definition_id and node.shared_definition_id not in definitions:
                errors.append(GraphSchemaError(
                    "node_not_found",
                    f"node[{node.node_id}] 引用未登记 shared 定义 "
                    f"{node.shared_definition_id}", node_id=node.node_id))

        # 环（image+key 共同）
        if not any(e.code == "node_not_found" for e in errors):
            try:
                _validate_no_cycles(nodes, self.edges)
            except GraphSchemaError as exc:
                errors.append(exc)

        # compound 端口 mapping 合法性（B11 展开；此处校验子图端子）
        for node in self.nodes:
            if node.compound_children is not None:
                child_errors = [
                    e for e in node.compound_children.validate()
                    if e.code in ("terminal_count", "cycle")]
                for err in child_errors:
                    errors.append(GraphSchemaError(
                        err.code, f"compound[{node.node_id}] 内部: {err}",
                        node_id=node.node_id))
        return errors

    def is_valid(self) -> bool:
        return not self.validate()

    def validate_execution(self) -> list[GraphSchemaError]:
        """执行级校验（B08 编译/B09 执行前）：结构合法 + 全连通。

        结构层允许未连接节点（用户新建暂未连线，§6.3"未连接节点允许
        保留，导出只取依赖锥"）；执行/引擎消费前必须全连通——断链 =
        丢效果，必须显式拒绝。
        """
        errors = list(self.validate())
        nodes = {n.node_id: n for n in self.nodes}
        if (self.source_node_id in nodes and self.output_node_id in nodes
                and not any(e.code == "node_not_found" for e in errors)):
            errors.extend(_check_reachability(
                nodes, self.edges, self.source_node_id, self.output_node_id))
        return errors

    # ── 序列化 ──

    def to_dict(self) -> dict[str, Any]:
        has_unknown = any(n.known_type() is None for n in self.nodes)
        return {
            "schema_version": COLOR_GRAPH_SCHEMA_VERSION,
            "graph_id": self.graph_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "shared_definitions": [d.to_dict() for d in self.shared_definitions],
            "source_node_id": self.source_node_id,
            "output_node_id": self.output_node_id,
            "working_color_context": dict(self.working_color_context),
            "graph_revision": self.graph_revision,
            "semantic_version": self.semantic_version,
            "ui_state": self.ui_state.to_dict(),
            "minimum_reader_version": (
                self.minimum_reader_version + 1 if has_unknown
                else self.minimum_reader_version),
            **({"alpha_output_node_id": self.alpha_output_node_id}
               if self.alpha_output_node_id else {}),
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ColorGraphData":
        if not isinstance(data, Mapping):
            raise GraphSchemaError(
                "illegal_param_key", f"graph 载荷必须是 Mapping: {type(data)!r}")
        schema_version = int(data.get("schema_version", COLOR_GRAPH_SCHEMA_VERSION))
        if schema_version > COLOR_GRAPH_SCHEMA_VERSION:
            # future schema：未知结构整体保留为 extra，不产生伪实例
            raise GraphSchemaError(
                "unknown_node_type",
                f"color_graph schema {schema_version} > 本版 {COLOR_GRAPH_SCHEMA_VERSION}"
                "（future 字段须整体保留并阻断读取）")
        nodes = tuple(ColorNodeData.from_dict(n) for n in data.get("nodes") or ())
        edges = tuple(ColorEdgeData.from_dict(e) for e in data.get("edges") or ())
        shared = tuple(SharedDefinitionData.from_dict(d)
                       for d in data.get("shared_definitions") or ())
        ctx_raw = data.get("working_color_context") or {}
        if not isinstance(ctx_raw, Mapping):
            raise GraphSchemaError("illegal_param_key",
                                   "working_color_context 必须是 Mapping")
        return ColorGraphData(
            graph_id=str(data.get("graph_id") or ""),
            nodes=nodes,
            edges=edges,
            shared_definitions=shared,
            source_node_id=str(data.get("source_node_id") or ""),
            output_node_id=str(data.get("output_node_id") or ""),
            alpha_output_node_id=data.get("alpha_output_node_id"),
            working_color_context=dict(ctx_raw),
            graph_revision=int(data.get("graph_revision", 0)),
            semantic_version=int(data.get("semantic_version", 1)),
            ui_state=ColorGraphUiState.from_dict(data.get("ui_state")),
            minimum_reader_version=int(
                data.get("minimum_reader_version", COLOR_GRAPH_SCHEMA_VERSION)),
            _extra_fields=split_extras(data, ColorGraphData._KNOWN_KEYS),
        )

    def deepcopy(self) -> "ColorGraphData":
        """防御性深拷贝（undo 基线、迁移对照用）。

        params 是 mappingproxy（不可 pickle），经 to_dict/from_dict 往返
        实现等价深拷贝——roundtrip 无损由 schema 测试保证。
        """
        return copy.deepcopy(self)

    def __deepcopy__(self, memo: dict) -> "ColorGraphData":
        return ColorGraphData.from_dict(self.to_dict())

    # ── 便捷构造 ──

    @staticmethod
    def create_serial(source_label: str = "Source",
                      output_label: str = "Output") -> "ColorGraphData":
        """最小合法图：Source → Output（B03 迁移的骨架）。"""
        source = ColorNodeData(
            node_id=str(uuid.uuid4()), node_type=NodeType.SOURCE.value,
            label=source_label)
        output = ColorNodeData(
            node_id=str(uuid.uuid4()), node_type=NodeType.OUTPUT.value,
            label=output_label)
        edge = ColorEdgeData(
            edge_id=str(uuid.uuid4()),
            src_node=source.node_id, src_port="image",
            dst_node=output.node_id, dst_port="image")
        graph = ColorGraphData(
            nodes=(source, output), edges=(edge,),
            source_node_id=source.node_id,
            output_node_id=output.node_id)
        assert graph.is_valid(), "create_serial 产物必须合法"
        return graph
