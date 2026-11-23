"""阶段 5.4 · Metal 合成链的历史与多帧契约（纯逻辑，无 GPU）。

当前 Metal 链没有已迁移的时域节点；本模块把**契约**先立起来并为后续
节点铺路：

1. **历史跨度**（:func:`evaluate_temporal_requirements`）：对参与渲染锥
   的每个时域节点声明「产出当前帧需要先前多少帧」以及窗口语义。目前
   唯一给出**精确跨度**的是 ``deflicker``（因果滑动窗口，窗口大小来自
   节点参数，跨度 = window - 1；随机跳帧时按节点定义降级为 warming
   直通而不是假装窗口有效——Metal 侧对应行为是整图回退，绝不静默
   近似）。其余时域类型只登记分类，不给出未经证实的跨度数字。
2. **确定性种子**（:func:`deterministic_seed`）：依赖随机数/按帧演化
   的节点（粒子/噪波/颗粒）必须对「相同输入序列 + 相同帧」得到相同
   种子，与播放顺序、seek 与分段导出无关。种子只由（图修订、节点 ID、
   参数摘要、帧号）决定。
3. **起点与重置边界**（:class:`TemporalHistoryTracker`）：正放/倒放的
   连续 ±1 帧推进延续历史；seek（非 ±1 跳变）、跨剪切（segment_id
   变化）、代际变化（generation）触发**重置**。单帧重试（同一帧再次
   请求）不重置——缓存按内容寻址，同键必同像素。分段导出在段边界调用
   :meth:`TemporalHistoryTracker.reset`。

关键帧契约（计划原文「关键帧仍由现有求值器按指定时间采样」）：Metal
链消费**已按指定时间采样解析后的参数**；携带未解析动画/表达式的图在
预检（`node_support.evaluate_metal_support` 的
``node.metal_animated_params``）被显式整图回退，直到接线方把解析后的
参数喂进来。

缓存一致性：`tile_cache.composite_cache_key` 已含图修订/帧/端口/尺寸/
色彩描述/参数摘要；本契约补充的纪律是——时域图的缓存/种子都以
（图修订, 参数摘要, 帧号）为唯一输入，播放顺序不影响命中内容。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# ── 时域节点登记 ─────────────────────────────────────────────────────────
#
# 分类语义：
#   trailing_window  因果滑动窗口（依赖先前 N 帧的像素）
#   frame_remap      帧重映射（按重映射后的时间取源帧；不依赖自身历史）
#   optical_flow     光流/运动估计（需要相邻源帧对）
#   tracker          跟踪分析（依赖分析区间内的帧序列）
#   seeded           按帧演化的确定性随机（种子契约；无像素历史）
TEMPORAL_NODE_CLASSES: Dict[str, str] = {
    "deflicker": "trailing_window",
    "retime": "frame_remap",
    "time_offset": "frame_remap",
    "frame_hold": "frame_remap",
    "video_frame": "frame_remap",
    "frame_interpolate": "optical_flow",
    "face_track": "tracker",
    "particle": "seeded",
    "noise": "seeded",
    "grain": "seeded",
}

# deflicker 合法窗口（nodes/deflicker_node.py::WINDOWS）。
DEFLICKER_WINDOWS = (3, 5, 7, 9)
DEFLICKER_DEFAULT_WINDOW = 5

# 种子契约的输出域（32 位非负；足够散列且便于 MSL uint 处理）。
SEED_MODULUS = 2 ** 32


@dataclass(frozen=True)
class TemporalNodeRequirement:
    """一个时域节点的历史需求声明。"""

    node_id: str
    node_type: str
    category: str                 # TEMPORAL_NODE_CLASSES 的值
    # 产出当前帧需要先前的帧数；None = 节点定义不足以无歧义声明。
    history_frames: Optional[int]
    detail: str

    def format(self) -> str:
        span = (f"需要先前 {self.history_frames} 帧历史"
                if self.history_frames is not None else "历史跨度未声明")
        return f"[{self.node_id}] {self.node_type}（{self.category}）：{span}；{self.detail}"


def deflicker_history_frames(window: object) -> int:
    """deflicker 的因果窗口需要的先前帧数（window - 1）。

    与 ``DeflickerNode._trailing_frames`` 同式：窗口 = ``window`` 帧结束于
    当前帧（``start = max(0, frame - 2*half)``，``half = window // 2``，
    窗口共 ``2*half + 1 == window`` 帧，因此先前帧数 = window - 1）。
    非法窗口回退默认值 5（与节点实现一致）。
    """
    try:
        size = int(window)
    except (TypeError, ValueError):
        size = DEFLICKER_DEFAULT_WINDOW
    if size not in DEFLICKER_WINDOWS:
        size = DEFLICKER_DEFAULT_WINDOW
    return size - 1


def _contributing_nodes(graph_data: Optional[dict]) -> List[dict]:
    """参与渲染锥的节点（从带输入连接的 Output 反向可达；含 Output）。"""
    if not graph_data:
        return []
    nodes = graph_data.get("nodes") or []
    connections = graph_data.get("connections") or []
    node_ids = {str(nd.get("id", "")) for nd in nodes}
    upstream: Dict[str, List[str]] = {}
    connected_inputs = set()
    for conn in connections:
        src = str(conn.get("src_node_id", ""))
        dst = str(conn.get("dst_node_id", ""))
        if src in node_ids and dst in node_ids:
            upstream.setdefault(dst, []).append(src)
            connected_inputs.add(dst)
    by_id = {str(nd.get("id", "")): nd for nd in nodes}
    outputs = [nid for nid, nd in by_id.items()
               if str(nd.get("type", "")) == "output" and nid in connected_inputs]
    contributing: Dict[str, dict] = {}
    stack = list(outputs)
    while stack:
        nid = stack.pop()
        if nid in contributing:
            continue
        nd = by_id.get(nid)
        if nd is not None:
            contributing[nid] = nd
        stack.extend(upstream.get(nid, []))
    return [contributing[nid] for nid in sorted(contributing)]


def evaluate_temporal_requirements(
    graph_data: Optional[dict],
) -> List[TemporalNodeRequirement]:
    """登记参与渲染锥的时域节点及其历史需求。

    只有节点定义能无歧义推出跨度的才填 ``history_frames``（当前仅
    deflicker）；其余时域类型给出分类与定性说明，避免伪造精度。
    """
    requirements: List[TemporalNodeRequirement] = []
    for nd in _contributing_nodes(graph_data):
        nid = str(nd.get("id", ""))
        ntype = str(nd.get("type", ""))
        category = TEMPORAL_NODE_CLASSES.get(ntype)
        if category is None:
            continue
        params = nd.get("parameters")
        params = params if isinstance(params, dict) else {}
        if ntype == "deflicker":
            window = deflicker_history_frames(params.get("window"))
            requirements.append(TemporalNodeRequirement(
                node_id=nid, node_type=ntype, category=category,
                history_frames=window,
                detail=f"因果滑动窗口 size={window + 1}；跳帧时按节点定义"
                       "降级 warming 直通，Metal 侧整图回退"))
        elif category == "frame_remap":
            requirements.append(TemporalNodeRequirement(
                node_id=nid, node_type=ntype, category=category,
                history_frames=None,
                detail="按重映射后的时间取源帧；需要源帧序列访问，"
                       "Metal 链未定义源帧提供器"))
        elif category == "optical_flow":
            requirements.append(TemporalNodeRequirement(
                node_id=nid, node_type=ntype, category=category,
                history_frames=None,
                detail="光流补帧需要相邻源帧对；Metal 链未实现"))
        elif category == "tracker":
            requirements.append(TemporalNodeRequirement(
                node_id=nid, node_type=ntype, category=category,
                history_frames=None,
                detail="跟踪分析依赖分析区间内的帧序列；Metal 链未实现"))
        else:  # seeded
            requirements.append(TemporalNodeRequirement(
                node_id=nid, node_type=ntype, category=category,
                history_frames=0,
                detail="无像素历史；需要确定性种子契约"
                       "（相同图修订+参数+帧 ⇒ 相同种子）"))
    return requirements


def graph_history_span(graph_data: Optional[dict]) -> int:
    """整图最大历史跨度（先前帧数；无时域节点 = 0）。"""
    return max((r.history_frames for r in
                evaluate_temporal_requirements(graph_data)
                if r.history_frames is not None), default=0)


def deterministic_seed(
    graph_revision: int,
    node_id: str,
    frame: int,
    params_digest: str,
) -> int:
    """时域/随机节点的确定性种子（与播放顺序无关）。

    相同（图修订, 节点, 帧号, 参数摘要）⇒ 相同种子；不同帧不同种子。
    正放、倒放、seek 后重渲染与分段导出得到完全一致的序列。
    """
    payload = f"{int(graph_revision)}|{node_id}|{int(frame)}|{params_digest}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % SEED_MODULUS


# ── 重置边界状态机 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class HistoryDecision:
    """一次帧请求的历史边界判定。"""

    reset: bool
    reason: str
    # 请求帧相对上一请求帧的步进：+1 正放 / -1 倒放 / 0 同帧重试 /
    # 其他 = seek 跳变（None = 首帧）。
    step: Optional[int]
    warmup_frames: int = 0


@dataclass
class _TrackerState:
    last_frame: Optional[int] = None
    last_step: Optional[int] = None
    generation: int = 0
    segment_id: str = ""


class TemporalHistoryTracker:
    """跟踪帧请求序列，判定「延续历史」还是「重置历史」。

    线程安全（渲染线程单线程使用为主，锁仅为防御并发入口）。

    重置规则（计划 5.4）：

    - 首帧（无上一请求）→ 重置（起点）。
    - 代际（generation）变化 → 重置（工程/设备重建）。
    - 段（segment_id）变化 → 重置（跨剪切 / 分段导出边界）。
    - 帧跳变（|Δ| != 1）→ 重置（seek 或跳剪；倒放本身合法，但方向
      翻转同样意味着因果窗口失效 → 重置）。
    - 方向翻转（+1 → -1 或反之）→ 重置。
    - 同帧重试（Δ == 0）→ 不重置（缓存/种子一致性由内容寻址保证）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = _TrackerState()
        self.last_reset_reason: str = "尚未开始"

    def note_request(
        self,
        frame: int,
        generation: int = 0,
        segment_id: str = "",
    ) -> HistoryDecision:
        """记录一次帧请求并返回边界判定。"""
        with self._lock:
            state = self._state
            if state.last_frame is None:
                decision = HistoryDecision(
                    reset=True, reason="起点（首个帧请求）", step=None)
            elif generation != state.generation:
                decision = HistoryDecision(
                    reset=True,
                    reason=f"代际变化 {state.generation} → {generation}",
                    step=(frame - state.last_frame))
            elif segment_id != state.segment_id:
                decision = HistoryDecision(
                    reset=True,
                    reason=f"段边界变化 {state.segment_id!r} → {segment_id!r}",
                    step=(frame - state.last_frame))
            else:
                step = frame - state.last_frame
                if step == 0:
                    decision = HistoryDecision(
                        reset=False, reason="同帧重试", step=0)
                elif abs(step) != 1:
                    decision = HistoryDecision(
                        reset=True, reason=f"帧跳变（Δ={step}，seek）",
                        step=step)
                elif state.last_step is not None and step != state.last_step:
                    decision = HistoryDecision(
                        reset=True,
                        reason=f"播放方向翻转（{state.last_step} → {step}）",
                        step=step)
                else:
                    decision = HistoryDecision(
                        reset=False,
                        reason="连续推进" + ("（倒放）" if step < 0 else ""),
                        step=step)
            self._state = _TrackerState(
                last_frame=int(frame), last_step=decision.step,
                generation=int(generation), segment_id=str(segment_id))
            if decision.reset:
                self.last_reset_reason = decision.reason
            return decision

    def reset(self, reason: str = "显式重置") -> None:
        """显式重置（seek 前 / 分段导出前由调用方执行）。"""
        with self._lock:
            self._state = _TrackerState(generation=self._state.generation,
                                        segment_id=self._state.segment_id)
            self.last_reset_reason = reason

    @property
    def last_frame(self) -> Optional[int]:
        with self._lock:
            return self._state.last_frame


def temporal_fallback_reason(
    requirements: Sequence[TemporalNodeRequirement],
    decision: HistoryDecision,
) -> str:
    """时域图被 Metal 链拒绝时的诊断（含节点 ID/类型与所需历史）。"""
    lines = [req.format() for req in requirements[:6]]
    if len(requirements) > 6:
        lines.append(f"… 共 {len(requirements)} 项")
    return ("Metal 合成链不包含时域历史实现（"
            f"触发边界：{decision.reason}）："
            + "; ".join(lines)
            + "；整图回退 CPU/GL，不做静默近似")
