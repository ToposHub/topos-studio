"""R06: 图资源能力预检（effect+参数+图结构 → fail-visible 诊断）。

按图结构逐 Corrector 检查资源可用性，**不执行渲染**：

- 插件实例：资源缺失（注册表无此 plugin_id）/ shader 源缺失 / AI 模型
  未安装（离线 bake 域，图内直接执行不可用）；
- LUT 引用：未注册 / 不可解析（topos-res 无激活 resolver 或文件缺失）；
- 实例归属：载荷畸形（缺 instance_id/plugin_id）。

诊断是机器可读码列表（R11 投影消费）；预检**不代替**执行期校验——
GL 执行的编译失败/纹理缺失仍走 GraphExecutionFailure fail-closed。
CPU/Metal 后端的域级能力（插件/HSL/LUT/空间效果显式拒绝）见各自
``*_CAPABILITY_CATALOG`` 与 ``production_layer_domain_errors``（B12.1），
本模块只做资源维度的前置汇总。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from src.shared.project.color_graph import ColorGraphData, NodeType


@dataclass(frozen=True)
class ResourcePreflightIssue:
    """单条预检诊断（code 见模块 docstring）。"""

    node_id: str
    code: str                 # resource_unknown / shader_missing /
    #                            ai_model_missing / lut_unresolvable /
    #                            instance_malformed
    resource_id: str = ""
    instance_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "code": self.code,
                "resource_id": self.resource_id,
                "instance_id": self.instance_id, "detail": self.detail}


def _default_effect_probe(plugin_id: str) -> Mapping[str, Any] | None:
    """PluginManager 探测：None=未知；Mapping 携带 type/shader_code/model。"""
    try:
        from src.features.color.core.plugin_manager import get_plugin_manager
        return get_plugin_manager().get_plugin(plugin_id)
    except Exception:  # noqa: BLE001
        return None


def _default_lut_probe(lut_ref: str) -> bool:
    """LUT 引用可解析性（topos-res 经会话/激活 resolver，路径经文件存在）。"""
    from src.shared.color.lut_resource_registry import get_lut_resource_session
    return get_lut_resource_session().status_of(lut_ref) == "registered"


def _default_bake_probe(bake_ref: str) -> bool:
    """bake 结果引用可解析性（R06-B：结果会话 registered 才可用）。"""
    from src.features.color.core.color_bake_result_registry import (
        get_bake_result_session,
    )
    return get_bake_result_session().status_of(bake_ref) == "registered"


def preflight_graph_resources(
        graph: ColorGraphData, *,
        effect_probe: Callable[[str], Mapping[str, Any] | None] | None = None,
        lut_probe: Callable[[str], bool] | None = None,
        bake_probe: Callable[[str], bool] | None = None,
        ai_model_available: Callable[[str], bool] | None = None,
) -> list[ResourcePreflightIssue]:
    """逐 Corrector 预检资源可用性（compound children 一并遍历）。"""
    effect_probe = effect_probe or _default_effect_probe
    lut_probe = lut_probe or _default_lut_probe
    bake_probe = bake_probe or _default_bake_probe
    issues: list[ResourcePreflightIssue] = []
    stack = list(graph.nodes)
    ordered: list[Any] = []
    while stack:
        node = stack.pop(0)
        if getattr(node, "compound_children", None) is not None:
            stack = list(node.compound_children.nodes) + stack
        else:
            ordered.append(node)
    for node in ordered:
        if node.node_type not in (NodeType.CORRECTOR.value,
                                  NodeType.SHARED_CORRECTOR.value):
            continue
        payload = node.legacy_payload or {}
        plugins = payload.get("plugins")
        if not isinstance(plugins, (list, tuple)):
            plugins = ()
        for plugin in plugins:
            if not isinstance(plugin, Mapping):
                issues.append(ResourcePreflightIssue(
                    node.node_id, "instance_malformed",
                    detail=f"非 dict 实例载荷: {type(plugin).__name__}"))
                continue
            plugin_id = str(plugin.get("plugin_id") or "")
            instance_id = str(plugin.get("instance_id") or "")
            if not plugin_id or not instance_id:
                issues.append(ResourcePreflightIssue(
                    node.node_id, "instance_malformed",
                    resource_id=plugin_id, instance_id=instance_id,
                    detail="缺 plugin_id/instance_id"))
                continue
            meta = effect_probe(plugin_id)
            if meta is None:
                issues.append(ResourcePreflightIssue(
                    node.node_id, "resource_unknown",
                    resource_id=plugin_id, instance_id=instance_id,
                    detail="插件不在注册表（缺失或未安装）"))
                continue
            if str(meta.get("type") or "dctl") == "ai":
                model = str(meta.get("model_path") or "")
                ok = ai_model_available(model) if ai_model_available \
                    else _default_ai_model_available(model)
                if not ok:
                    issues.append(ResourcePreflightIssue(
                        node.node_id, "ai_model_missing",
                        resource_id=plugin_id, instance_id=instance_id,
                        detail=f"模型未安装: {model}"))
                continue
            if not meta.get("shader_code"):
                issues.append(ResourcePreflightIssue(
                    node.node_id, "shader_missing",
                    resource_id=plugin_id, instance_id=instance_id,
                    detail="注册表条目无 shader 源"))
        params = node.params if isinstance(node.params, Mapping) else {}
        resources = params.get("resources")
        if isinstance(resources, Mapping):
            lut_ref = resources.get("lut_ref")
            if isinstance(lut_ref, str) and lut_ref and not lut_probe(lut_ref):
                issues.append(ResourcePreflightIssue(
                    node.node_id, "lut_unresolvable",
                    resource_id=lut_ref,
                    detail="LUT 引用未注册或不可解析"))
            bake_ref = resources.get("bake_ref")
            if isinstance(bake_ref, str) and bake_ref \
                    and not bake_probe(bake_ref):
                issues.append(ResourcePreflightIssue(
                    node.node_id, "bake_unresolvable",
                    resource_id=bake_ref,
                    detail="bake 结果未注册或不可解析"))
    return issues


def _default_ai_model_available(model_path: str) -> bool:
    if not model_path:
        return False
    from pathlib import Path
    path = Path(model_path).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    return path.is_file()
