"""M19.2b · 工程色彩上下文注入时机生命周期（模型层契约）。

三个时机全部落在同一进程级覆盖层（``color_space_descriptor`` 单点存储，
Color/Edit/Composite 三页 ``descriptor_from_config`` 共读）：

- **工程加载完成**（Edit 工程包 / standalone 图片工程，均经
  ``convert_topos_project_to_edit_project``）→ ``on_project_loaded``：
  注入工程 ``settings["color_management"]`` 载荷；空/None = 清空回
  用户全局偏好（不残留上一工程的覆盖）。
- **工程关闭** → ``on_project_closed``：清空覆盖层。新建空文档
  （无作者设定）同此语义——"新工程看起来像作者所见"的正确基线是
  用户全局偏好，而不是上一个工程。
- **工程切换**（A→B 直通路径）→ ``on_project_switched``：以 B 载荷
  **全量替换**（非 merge）——A 独有键不残留；覆盖层是单次原子写入，
  读者不会看到 A/B 混合状态。

控制器侧接线（打开=converter 注入 / 关闭=``_close_current_project``
/ 新建空文档=``_auto_create_edit_project``）见各控制器；本模块冻结
时机契约与替换语义，供三页与测试共同引用。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.shared.color.color_space_descriptor import (
    apply_project_color_context,
    clear_project_color_overrides,
    get_project_color_overrides,
)


def on_project_loaded(
    color_management: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """工程加载完成：注入作者色彩设定（空/None = 清空回全局偏好）。

    Returns:
        注入后处于活动状态的覆盖层快照（供调用方/测试断言）。
    """
    apply_project_color_context(
        dict(color_management) if color_management else {})
    return get_project_color_overrides()


def on_project_closed() -> Dict[str, Any]:
    """工程关闭/新建空文档：清空工程级覆盖（回退用户全局偏好）。"""
    clear_project_color_overrides()
    return get_project_color_overrides()


def on_project_switched(
    color_management: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """工程切换：以新工程载荷全量替换（前一工程独有键不残留）。

    覆盖层经 ``on_project_loaded`` 同一原子写入口替换——不经
    "先清空再注入"两步，中间不存在空窗供读者观察到混合/丢失状态。
    """
    return on_project_loaded(color_management)


__all__ = [
    "on_project_closed",
    "on_project_loaded",
    "on_project_switched",
]
