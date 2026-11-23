"""Metal 合成链的节点能力判定（迁移计划阶段 5.1/5.2/5.3 预检）。

预检策略（计划阶段 5 的「回退单元」纪律）：

- 以整张图为单位：**任何一个参与渲染的节点未被迁移，整图回退**到现有
  CPU/GL 路径，绝不跳过未迁移节点继续产出。
- 每条不支持诊断都带节点 ID 与类型，未迁移类型统一报告
  ``node.metal_unmigrated``（"未迁移 → 回退"）。
- 参数级限制（如 Transform 的运动模糊、Merge 的 PSD sRGB 混合）同样
  阻断并给出具体原因——不留半成品路径。

迁移登记（本批次，阶段 5.1 + 5.2 部分）：

====================  =========================================
节点类型              状态
====================  =========================================
constant              已迁移（solid/checker/gradient/radial）
transform             已迁移（无运动模糊；linear/nearest 滤镜）
merge                 已迁移（32 种混合模式、mask、alpha 策略；
                      不含 psd_srgb 与受管图层 matte 语义）
output                已迁移（input/custom 画布、alpha 策略、背景、
                      srgb 输出编码；project 画布需编辑上下文）
crop                  已迁移（reformat/canvas 两种模式、4 种越界策略）
premult/unpremult     已迁移
mask                  仅 rectangle（ellipse/polygon/circle 未迁移）
noop/dot              直通
其余全部节点          未迁移 → 整图回退
====================  =========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.features.composite.core.graph_preflight import (
    MODE_PREVIEW,
    PreflightReport,
    run_preflight_data,
)

# 已迁移的节点类型（参数级限制见 _node_param_blockers）。
METAL_COMPOSITE_SUPPORTED_TYPES = frozenset({
    "constant", "transform", "merge", "output", "crop",
    "premult", "unpremult", "mask", "noop", "dot",
})

_CODE_UNMIGRATED = "node.metal_unmigrated"
_CODE_PARAM = "node.metal_param_unsupported"


@dataclass(frozen=True)
class MetalSupportIssue:
    """一条阻断 Metal 链路的原因（含节点 ID 与类型）。"""

    node_id: str
    node_type: str
    reason: str
    code: str = _CODE_UNMIGRATED

    def format(self) -> str:
        return f"[{self.node_id}] {self.node_type}: {self.reason}"


@dataclass
class MetalSupportReport:
    """整图能力判定结果。"""

    supported: bool = False
    issues: List[MetalSupportIssue] = field(default_factory=list)
    # 结构预检（graph_preflight）本身的报告；errors 同样阻断 Metal 链路。
    base_preflight: Optional[PreflightReport] = None
    # 参与渲染锥的节点 id → 类型（诊断/测试用）。
    contributing: Dict[str, str] = field(default_factory=dict)

    @property
    def fallback_summary(self) -> str:
        """人类可读的回退诊断（含节点 ID 与类型）。"""
        if not self.issues:
            return ""
        lines = [issue.format() for issue in self.issues[:8]]
        if len(self.issues) > 8:
            lines.append(f"… 共 {len(self.issues)} 项")
        return "Metal 合成链不可用，整图回退 CPU/GL：" + "; ".join(lines)


def _node_param_blockers(node_type: str, params: dict) -> Optional[str]:
    """参数级限制：返回阻断原因，None 表示可用。"""
    if node_type == "transform":
        if bool(params.get("motion_blur", False)):
            return "运动模糊子帧采样尚未迁移到 Metal"
        return None
    if node_type == "merge":
        if bool(params.get("psd_srgb", False)):
            return "PSD sRGB 混合语义尚未迁移到 Metal"
        return None
    if node_type == "output":
        if str(params.get("format_mode", "input")) == "project":
            return "project 画布模式需要编辑工程上下文，尚未迁移"
        return None
    if node_type == "mask":
        mask_type = str(params.get("mask_type", "rectangle"))
        if mask_type not in ("rectangle",):
            return f"mask_type={mask_type!r} 尚未迁移（本批次仅 rectangle）"
        return None
    if node_type == "constant":
        mode = str(params.get("mode", "solid"))
        if mode not in ("solid", "checker", "gradient", "radial"):
            return f"constant mode={mode!r} 未知"
        return None
    return None


def evaluate_metal_support(
    graph_data: Optional[dict],
    mode: str = MODE_PREVIEW,
) -> MetalSupportReport:
    """判定一张序列化合成图能否整体走 Metal 链。

    Args:
        graph_data: ``NodeGraph.to_dict()`` 形式的图数据。
        mode: 预检模式（preview/export），透传给结构预检。

    Notes:
        结构性错误（环、输出未连接等）由 ``run_preflight_data`` 报告并
        同样阻断；本函数不吞掉其 error。
    """
    report = MetalSupportReport()
    base = run_preflight_data(graph_data, mode=mode, check_fs=False)
    report.base_preflight = base
    if not graph_data:
        report.issues.append(MetalSupportIssue(
            node_id="", node_type="", reason="合成图为空",
            code="graph.empty"))
        return report
    for issue in base.errors:
        report.issues.append(MetalSupportIssue(
            node_id=issue.node_id, node_type="",
            reason=f"结构预检错误 [{issue.code}] {issue.message}",
            code=issue.code))

    nodes = graph_data.get("nodes") or []
    connections = graph_data.get("connections") or []
    node_types: Dict[str, str] = {}
    node_params: Dict[str, dict] = {}
    for nd in nodes:
        nid = str(nd.get("id", ""))
        node_types[nid] = str(nd.get("type", ""))
        params = nd.get("parameters")
        node_params[nid] = params if isinstance(params, dict) else {}

    # 参与渲染锥：从有输入连接的 Output 反向可达集合。
    upstream: Dict[str, List[str]] = {}
    output_ids: List[str] = []
    connected_inputs = {str(c.get("dst_node_id", "")) for c in connections}
    for conn in connections:
        src = str(conn.get("src_node_id", ""))
        dst = str(conn.get("dst_node_id", ""))
        upstream.setdefault(dst, []).append(src)
    for nid, ntype in node_types.items():
        if ntype == "output" and nid in connected_inputs:
            output_ids.append(nid)
    contributing: Dict[str, str] = {}
    stack = list(output_ids)
    while stack:
        nid = stack.pop()
        if nid in contributing:
            continue
        contributing[nid] = node_types.get(nid, "")
        stack.extend(upstream.get(nid, []))
    report.contributing = contributing

    for nid, ntype in sorted(contributing.items()):
        if ntype not in METAL_COMPOSITE_SUPPORTED_TYPES:
            report.issues.append(MetalSupportIssue(
                node_id=nid, node_type=ntype,
                reason="未迁移 → 回退（该节点类型尚无 Metal 实现）"))
            continue
        blocker = _node_param_blockers(ntype, node_params.get(nid, {}))
        if blocker is not None:
            report.issues.append(MetalSupportIssue(
                node_id=nid, node_type=ntype, reason=blocker,
                code=_CODE_PARAM))

    # 整图判定：结构预检无 error + 无未迁移/参数阻断 + 存在带输入的
    # Output（无输入的 Output 是结构预检 error，同样整图回退）。
    report.supported = bool(output_ids) and not report.issues and base.ok
    return report


def merged_node_parameters(node_type: str, parameters: dict) -> dict:
    """节点参数与注册类默认值合并（缺省键取默认，未知键保留）。

    用于 Metal 链路的参数解析：序列化图可能缺少新参数（旧工程），
    ``Node.from_dict`` 的 ``_init_params_from_defs`` 语义在此复刻，避免
    为取默认值而实例化节点。
    """
    merged = dict(parameters)
    defaults = _NODE_DEFAULT_PARAMS.get(node_type)
    if defaults is not None:
        for key, value in defaults.items():
            merged.setdefault(key, value)
    return merged


def _collect_default_parameters() -> Dict[str, dict]:
    """从 NODE_REGISTRY 取已迁移类型的默认参数（懒加载，进程内缓存）。"""
    defaults: Dict[str, dict] = {}
    try:
        from src.features.composite.core.node_engine import NODE_REGISTRY
    except Exception:  # pragma: no cover - 注册表导入失败属环境问题
        return defaults
    for node_type in sorted(METAL_COMPOSITE_SUPPORTED_TYPES):
        cls = NODE_REGISTRY.get(node_type)
        if not isinstance(cls, type):
            continue
        try:
            instance = cls(node_id=f"__metal_defaults__")
            defaults[node_type] = {
                str(key): value for key, value in instance.parameters.items()
            }
        except Exception:  # pragma: no cover - 防御第三方节点构造异常
            continue
    return defaults


_NODE_DEFAULT_PARAMS: Dict[str, dict] = {}


def node_default_parameters(node_type: str) -> Dict[str, object]:
    """已迁移节点类型的默认参数（懒加载一次）。"""
    if not _NODE_DEFAULT_PARAMS:
        _NODE_DEFAULT_PARAMS.update(_collect_default_parameters())
    return dict(_NODE_DEFAULT_PARAMS.get(node_type, {}))
