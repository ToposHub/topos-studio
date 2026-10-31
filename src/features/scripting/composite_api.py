"""Python facade for the same Composite edit protocol used by AI/MCP."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from src.features.composite.edit_service import (
    CompositeEditPlan,
    CompositeEditService,
)


class CompositeScriptAPI:
    """受信任脚本的 Composite API。

    每个公开方法最终都构造 ``CompositeEditPlan``，因此脚本和 AI 使用完全相同
    的操作名、值域、曲线手柄格式和校验逻辑。所有 graph 访问仍通过主线程 bridge。
    """

    def __init__(self, bridge: Any, page_getter: Callable[[], Any | None]):
        self._bridge = bridge
        self._page_getter = page_getter

    def snapshot(self) -> dict[str, Any]:
        return self._bridge.call_sync(lambda: self._service().snapshot())

    def validate(self, plan: CompositeEditPlan | Mapping[str, Any]) -> dict[str, Any]:
        return self._bridge.call_sync(lambda: self._service().validate(plan).to_dict())

    def apply(self, plan: CompositeEditPlan | Mapping[str, Any]) -> dict[str, Any]:
        """应用一个计划；宿主会把它作为一个 Composite undo group 记录。"""
        return self._bridge.call_sync(lambda: self._apply_plan(plan))

    def add_keyframe(
        self,
        node_id: str,
        param_name: str,
        frame: int,
        value: float,
        *,
        interpolation: str = "linear",
        tangent_mode: str = "auto",
        handle_left: tuple[float, float] | list[float] | None = None,
        handle_right: tuple[float, float] | list[float] | None = None,
    ) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-add-keyframe",
            "operations": [{
                "op": "add_keyframe", "node_id": node_id, "param_name": param_name,
                "frame": frame, "value": value, "interpolation": interpolation,
                "tangent_mode": tangent_mode, "handle_left": handle_left,
                "handle_right": handle_right,
            }],
        })

    def set_keyframes(
        self,
        node_id: str,
        param_name: str,
        keyframes: list[dict[str, Any]],
        *,
        pre_extrap: str = "constant",
        post_extrap: str = "constant",
    ) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-set-keyframes",
            "operations": [{
                "op": "set_keyframes", "node_id": node_id, "param_name": param_name,
                "keyframes": keyframes, "pre_extrap": pre_extrap,
                "post_extrap": post_extrap,
            }],
        })

    def remove_keyframe(self, node_id: str, param_name: str, frame: int) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-remove-keyframe",
            "operations": [{
                "op": "remove_keyframe", "node_id": node_id,
                "param_name": param_name, "frame": frame,
            }],
        })

    def set_interpolation(self, node_id: str, param_name: str, frame: int, interpolation: str) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-set-interpolation",
            "operations": [{
                "op": "set_interpolation", "node_id": node_id,
                "param_name": param_name, "frame": frame,
                "interpolation": interpolation,
            }],
        })

    def set_bezier_handles(
        self, node_id: str, param_name: str, frame: int, *,
        handle_left: tuple[float, float] | list[float] | None = None,
        handle_right: tuple[float, float] | list[float] | None = None,
    ) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-set-bezier-handles",
            "operations": [{
                "op": "set_bezier_handles", "node_id": node_id,
                "param_name": param_name, "frame": frame,
                "handle_left": handle_left, "handle_right": handle_right,
            }],
        })

    def set_tangent_mode(self, node_id: str, param_name: str, frame: int, tangent_mode: str) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-set-tangent-mode",
            "operations": [{
                "op": "set_tangent_mode", "node_id": node_id,
                "param_name": param_name, "frame": frame,
                "tangent_mode": tangent_mode,
            }],
        })

    def set_extrapolation(
        self, node_id: str, param_name: str, *,
        pre_extrap: str = "constant", post_extrap: str = "constant",
    ) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-set-extrapolation",
            "operations": [{
                "op": "set_extrapolation", "node_id": node_id,
                "param_name": param_name, "pre_extrap": pre_extrap,
                "post_extrap": post_extrap,
            }],
        })

    def set_parameter(self, node_id: str, param_name: str, value: Any) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-set-parameter",
            "operations": [{
                "op": "set_parameter", "node_id": node_id,
                "param_name": param_name, "value": value,
            }],
        })

    def set_expression(self, node_id: str, param_name: str, expression: str) -> dict[str, Any]:
        return self.apply({
            "plan_id": "script-set-expression",
            "operations": [{
                "op": "set_expression", "node_id": node_id,
                "param_name": param_name, "expression": expression,
            }],
        })

    def _service(self) -> CompositeEditService:
        page = self._page_getter()
        if page is None or getattr(page, "node_graph", None) is None:
            raise RuntimeError("Composite page is not active")
        return CompositeEditService(page.node_graph)

    def _apply_plan(self, plan: CompositeEditPlan | Mapping[str, Any]) -> dict[str, Any]:
        page = self._page_getter()
        service = self._service()
        result = service.apply(plan)
        if result.applied:
            _notify_page_changed(page, result.before or {}, result.after)
        return result.to_dict()


def _notify_page_changed(page: Any, before: dict[str, Any], after: dict[str, Any] | None) -> None:
    """让 UI、工程保存、预览和 Composite undo 都收到同一份变更。"""
    if page is None:
        return
    if hasattr(page, "record_external_composite_edit"):
        page.record_external_composite_edit(before, after)
        return
    # 兼容尚未接入新 hook 的旧页面；至少触发同一套评估/持久化信号。
    if hasattr(page, "_emit_user_edit_once"):
        page._emit_user_edit_once()
    if hasattr(page, "_evaluate_graph_now"):
        page._evaluate_graph_now()


__all__ = ["CompositeScriptAPI"]
