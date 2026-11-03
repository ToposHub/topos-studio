"""ColorGraphSnapshot —— 渲染请求的不可变图快照（N04，B08.1）。

``freeze_graph`` 在**提交渲染请求的瞬间**深复制图 + 一次求值 frame /
working color context / viewer target / 源内容 revision。此后 UI 线程
对活模型的任何编辑都不影响正在执行的快照（计划 §5.1：渲染线程不访问
UI 活模型）。

快照携带请求身份（``request_id`` / ``session_id`` / ``graph_revision`` /
``source_frame``），供执行器（B09）做取消匹配与缓存键。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.shared.project.color_graph import ColorGraphData, NodeType


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
    # R06.1：资源身份指纹（插件实例顺序/启用/参数 + 内容哈希 + LUT 引用）。
    # graph_revision 只覆盖图内编辑；插件文件在磁盘上热更（同 revision）时
    # 由本指纹驱动 plan/纹理缓存失效。
    resource_fingerprint: str = ""

    def cache_key(self) -> tuple:
        """编译缓存键：拓扑/参数版本/帧/上下文/目标/资源指纹（§5.3 + R06）。"""
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
            self.resource_fingerprint,
        )


def _iter_resource_nodes(graph: ColorGraphData) -> Iterable[Any]:
    """全部 Corrector 家族节点（含 compound children，确定性顺序）。"""
    for node in graph.nodes:
        if getattr(node, "compound_children", None) is not None:
            yield from _iter_resource_nodes(node.compound_children)
        elif node.node_type in (NodeType.CORRECTOR.value,
                                NodeType.SHARED_CORRECTOR.value):
            yield node


def compute_resource_fingerprint(
        graph: ColorGraphData,
        resource_hashes: Mapping[str, str] | None = None) -> str:
    """图内资源身份指纹（sha256 of canonical JSON）。

    覆盖：每个 Corrector 的插件实例**有序** (plugin_id, instance_id,
    enabled, params, 内容哈希) + LUT 引用/强度。内容哈希默认查
    PluginManager 注册表（无注册/测试注入 ``resource_hashes`` 时为空串，
    图内字段仍参与指纹）。
    """
    if resource_hashes is None:
        resource_hashes = _registry_content_hashes()
    entries: list[dict[str, Any]] = []
    for node in _iter_resource_nodes(graph):
        payload = node.legacy_payload or {}
        plugins = payload.get("plugins")
        instances = []
        if isinstance(plugins, (list, tuple)):
            for plugin in plugins:
                if not isinstance(plugin, Mapping):
                    continue
                plugin_id = str(plugin.get("plugin_id", ""))
                instances.append({
                    "plugin_id": plugin_id,
                    "instance_id": str(plugin.get("instance_id", "")),
                    "enabled": bool(plugin.get("enabled", True)),
                    "params": dict(plugin.get("params") or {}),
                    "content_hash": str(resource_hashes.get(plugin_id, "")),
                })
        resources = node.params.get("resources") if isinstance(
            node.params, Mapping) else None
        lut = {
            "lut_ref": str(resources.get("lut_ref") or "")
            if isinstance(resources, Mapping) else "",
            "lut_strength": float(resources.get("lut_strength", 1.0))
            if isinstance(resources, Mapping) else 1.0,
        } if isinstance(resources, Mapping) else {"lut_ref": "",
                                                  "lut_strength": 1.0}
        entries.append({"node_id": node.node_id,
                        "instances": instances, "lut": lut})
    blob = json.dumps(entries, sort_keys=True, ensure_ascii=True,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _registry_content_hashes() -> dict[str, str]:
    """PluginManager 注册表的 plugin_id → content_hash（注册表缺席为空）。"""
    try:
        from src.features.color.core.plugin_manager import get_plugin_manager
        return {pid: str(meta.get("content_hash") or "")
                for pid, meta in get_plugin_manager().get_all_plugins().items()
                if isinstance(meta, Mapping)}
    except Exception:  # noqa: BLE001 —— 指纹不因注册表缺席而失败
        return {}


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
        resource_fingerprint=compute_resource_fingerprint(graph),
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
