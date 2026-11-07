"""KF-04：调色手势自动录制路由（会话域注册表 + 手势绑定）。

职责（计划 §录制规则 7）：

- **origin 隔离**：只有调色手势路径（LegacyGradingAdapter.submit_
  param_change）调用 :meth:`observe`——播放器回填、程序化控件更新、
  seek/播放、只读投影、undo/redo 都不经过此对象，天然不产生关键帧。
- **手势绑定**：同一手势绑定首次观察到的有效帧（源帧域）与各参数的
  最后值；手势结束由适配器取 :meth:`take_pending_edits` 的批处理条目，
  与 base 参数变更进同一事务缓冲——一个历史条目。
- **开关语义**：Corrector 级 auto 开关（面板行按钮）作用于该节点全部
  子轨；真正写入只发生在本次手势实际改动的登记参数。开关状态是会话
  域 UI 状态（不持久化——持久范围按 KF-00 实测后于 KF-05 落工程）。
  关闭 auto 的手势不触碰关键帧（关闭态修改既有锚点的 Resolve 语义
  待 KF-00 量测，见 KF00_RESOLVE_FIXTURES.md OP-15）。

线程边界：observe/开关切换仅在 UI 线程（手势与面板事件）；
take_pending_edits 由适配器在 flush（UI 线程）调用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from src.features.color.core.keyframe_tracks import parse_track_name

logger = logging.getLogger(__name__)


class KeyframeAutoRecorder:
    """自动录制注册表 + 手势字段收集（写入口经批处理命令落工程）。"""

    def __init__(self) -> None:
        self._auto: Dict[str, bool] = {}          # node_id → 开关
        self._gesture_frame: Optional[int] = None
        self._gesture_open = False
        # (node,param) → (最后值, compound 路径)——身份含路径（R6.1），
        # compound 成员的批命令须带 _compound_path 下行才找得到节点
        self._pending: Dict[Tuple[str, str], Tuple[Any, Tuple[str, ...]]] = {}

    # ── 开关（会话域；面板 auto 列）──

    def set_auto(self, node_id: str, enabled: bool) -> None:
        self._auto[str(node_id)] = bool(enabled)

    def auto_enabled(self, node_id: str) -> bool:
        return bool(self._auto.get(str(node_id), False))

    def auto_nodes(self) -> frozenset:
        """当前开启 auto 的节点集合（快照投影用）。"""
        return frozenset(node for node, on in self._auto.items() if on)

    def reset(self) -> None:
        """会话/切片段/版本更换：丢弃开关与半开手势（不落工程）。"""
        self._auto.clear()
        self._discard_gesture()

    # ── 手势收集（仅调色手势路径调用）──

    def observe(self, node_id: str, param_name: str, value: Any,
                frame: Optional[int],
                compound_path: Tuple[str, ...] = ()) -> None:
        """手势中的一次真实参数改动（最后值生效）。

        只收集登记的可动画参数；auto 关闭的节点不收集。首次观察绑定
        有效帧（源帧域；播放上下文缺席 → None，flush 时整批放弃）。
        ``compound_path``：目标节点的 compound 下行路径（R6.1 身份含
        路径——compound 成员的批命令不带路径会被 node_not_found 整批
        拒，录制静默丢失）。
        """
        if parse_track_name(str(param_name)) is None:
            return  # 未登记字段不可动画（工具覆盖清单）
        if not self.auto_enabled(node_id):
            return
        if not self._gesture_open:
            self._gesture_open = True
            self._gesture_frame = int(frame) if frame is not None else None
            self._pending.clear()
        self._pending[(str(node_id), str(param_name))] = (
            value, tuple(str(step) for step in compound_path))

    def take_pending_edits(self) -> list:
        """手势结束：把收集的参数差值转为批处理条目并复位手势。

        - 无播放上下文（帧未绑定）→ 放弃录制（不猜帧）；
        - 值与帧无关的判重交给批处理命令的 no-op 语义（重复置值不入史）。
        """
        return [edit for _path, group in
                self.take_pending_edit_groups().items() for edit in group]

    def take_pending_edit_groups(
            self) -> Dict[Tuple[str, ...], list]:
        """同 take_pending_edits，但按 compound 路径分组返回
        ``{path: [批处理条目]}``——flush 侧每路径一次 apply_intent
        （``_compound_path`` 下行），同一手势事务仍是一个历史条目。
        """
        if not self._gesture_open:
            return {}
        groups: Dict[Tuple[str, ...], list] = {}
        frame = self._gesture_frame
        for (node_id, param), (value, path) in self._pending.items():
            if frame is None:
                logger.debug("auto-record dropped: no effective frame")
                continue
            groups.setdefault(path, []).append({
                "action": "set", "node_id": node_id, "param": param,
                "frame": int(frame), "value": value,
                # 自动录制建立动态帧（linear）；实测档位待 KF-00
                "interpolation": "linear",
            })
        self._discard_gesture()
        return groups

    def _discard_gesture(self) -> None:
        self._gesture_open = False
        self._gesture_frame = None
        self._pending.clear()
