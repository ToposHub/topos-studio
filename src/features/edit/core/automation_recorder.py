"""
Automation Recorder — Write/Touch/Latch 自动化录制（§3.1）。

专业 DAW 自动化录制：播放时（Write/Touch/Latch 模式）用户调推子，
系统采集 fader 值 + 时间戳，停止时简化为关键帧并合并为单次 undo 命令。

三种模式的写入窗口语义（计划表 §3.1）：
- Write：播放开始即持续写入，覆盖整段已录曲线。
- Touch：仅当用户**按住**推子时写入；松手后经 release 时间回到原曲线（停止写入）。
- Latch：用户**首次触碰**推子后持续写入（即使松手），直到停止播放。

设计原则：
- 单次 undo：一次录制只产生一个命令（OverwriteAutomationCommand，覆盖录制区间）。
- 重开后恢复：录制结果落库（命令修改 EditTrack.automation，持久化）。
- 采样简化：连续 fader 采样按阈值简化为关键帧（避免每帧一个 keyframe）。

模块边界：只负责"采集 + 简化 + 生成命令"。命令执行由调用方（controller）处理。

已知行为边界（AUD-15611 / E16 更新）：
- Touch 模式松手后的 release 回归**已实现**（E16）：``start()`` 接受
  ``release_provider``（调用方从当前 timeline 读底层曲线在松手帧的原值），
  松手后经 ``release_frames``（默认 12 帧）线性回归到原值，斜坡并入同一
  录制段（单次 undo 语义不变）。未提供 provider 时退化为旧行为：松手即
  停写，段后曲线朝下一个既有/新关键帧线性插值。
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 录制样本与简化
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _FaderSample:
    """单个 fader 采样（帧 + 值）。"""
    frame: int
    value: float


def simplify_samples(
    samples: List[_FaderSample],
    threshold: float = 0.005,
    *,
    protected_frames: Optional[set] = None,
) -> List[_FaderSample]:
    """将连续 fader 采样简化为关键帧（Douglas-Peucker 简化思想）。

    规则：
    - 起点/终点必保留。
    - 相邻关键帧间，若任意中间样本偏离线性插值 > threshold，则保留偏离最大的点。
    - 重复此过程直到无点需保留。
    - B07/§13 步骤 8：``protected_frames``（hold 边界/触摸按下-释放边界/
      陡边）必须保留——RDP 不得删去语义边界。线性参数（pan 等）的线性
      插值重放 == 直线偏离，本函数的阈值即重放误差界。

    Args:
        samples: 按 frame 排序的采样列表
        threshold: 偏离阈值（参数值单位，如 pan 0.005）

    Returns:
        简化后的关键帧列表（frame 单调递增）
    """
    if len(samples) <= 2:
        return list(samples)

    # 先去重同帧（保留最后值）
    deduped: Dict[int, float] = {}
    for s in samples:
        deduped[s.frame] = s.value
    frames_sorted = sorted(deduped.keys())
    pts = [_FaderSample(f, deduped[f]) for f in frames_sorted]
    if len(pts) <= 2:
        return pts

    # AUD-15608: Douglas-Peucker 改为显式栈的迭代实现。旧递归版本在噪声
    # 之字形采样下每层只剥掉一个点、递归深度 O(n)，约 2000+ 样本（30 s@
    # 60fps 的抖动推子）即触发 RecursionError，controller 的宽泛 except
    # 将其降级为 warning——整段录制数据丢失。保留点集合与递归版等价。
    frames = [p.frame for p in pts]
    values = [p.value for p in pts]
    protected = protected_frames or set()
    kept = [False] * len(pts)
    kept[0] = kept[len(pts) - 1] = True
    for i, frame in enumerate(frames):
        if frame in protected:
            kept[i] = True
    stack: List[Tuple[int, int]] = [(0, len(pts) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        # 找区间内偏离起点-终点连线最大的点
        x0, y0 = frames[start], values[start]
        x1, y1 = frames[end], values[end]
        max_dist = 0.0
        max_idx = -1
        vertical = x1 == x0
        for i in range(start + 1, end):
            if kept[i]:
                continue  # 受保护/已保留的点不参与删除判定
            if vertical:
                dist = abs(values[i] - y0)
            else:
                # 点到直线的垂直距离
                t = (frames[i] - x0) / (x1 - x0)
                dist = abs(values[i] - (y0 + t * (y1 - y0)))
            if dist > max_dist:
                max_dist = dist
                max_idx = i
        if max_dist > threshold and max_idx > 0:
            kept[max_idx] = True
            stack.append((start, max_idx))
            stack.append((max_idx, end))

    result = [p for p, keep in zip(pts, kept) if keep]
    # 去重连续重复
    out: List[_FaderSample] = []
    for p in result:
        if not out or out[-1].frame != p.frame:
            out.append(p)
    return out


def simplify_volume_db(
    samples: List[_FaderSample],
    max_error_db: float = 0.1,
    *,
    protected_frames: Optional[set] = None,
) -> List[_FaderSample]:
    """volume 线性倍率采样简化，保证**线性重放**误差 ≤ max_error_db dB。

    §13 步骤 8 的 volume 误差界在 dB 域定义，但生产求值对关键帧做**线性**
    插值——对指数形衰减，dB 域 DP 的界在线性重放下不成立。本函数按真实
    重放误差迭代细化：从端点（+受保护帧）出发，反复加入重放 dB 误差最大
    的样本，直到全部样本误差 ≤ 阈值。保留点数最少化（贪心，非全局最优，
    但误差界严格成立）。

    Args:
        samples: 按 frame 排序的采样列表（值为线性倍率，>0）
        max_error_db: 重放 dB 误差上界（默认 0.1 dB）
        protected_frames: 必须保留的帧集合（hold/触摸边界）

    Returns:
        简化后的关键帧列表（frame 单调递增）
    """
    if max_error_db <= 0:
        raise ValueError("max_error_db must be positive")
    if len(samples) <= 2:
        return list(samples)
    deduped: Dict[int, float] = {}
    for s in samples:
        deduped[s.frame] = s.value
    frames = sorted(deduped.keys())
    values = [max(1e-6, float(deduped[f])) for f in frames]
    dbs = [20.0 * math.log10(v) for v in values]
    n = len(frames)
    protected = protected_frames or set()
    kept: List[int] = [0, n - 1]
    kept.extend(i for i in range(n) if frames[i] in protected and 0 < i < n - 1)
    kept = sorted(set(kept))

    def replay_db(idx: int) -> float:
        # 生产求值方式：关键帧**值**线性插值，再换算 dB 误差。
        if idx in kept_set:
            return dbs[idx]
        import bisect as _bisect
        pos = _bisect.bisect_right(kept, idx)
        lo, hi = kept[pos - 1], kept[pos]
        t = (idx - lo) / (hi - lo)
        replay_value = values[lo] + t * (values[hi] - values[lo])
        return 20.0 * math.log10(max(replay_value, 1e-6))

    for _ in range(n):
        kept_set = set(kept)
        worst_idx = -1
        worst_err = 0.0
        for i in range(n):
            if i in kept_set:
                continue
            err = abs(replay_db(i) - dbs[i])
            if err > worst_err:
                worst_err = err
                worst_idx = i
        if worst_idx < 0 or worst_err <= max_error_db:
            break
        kept.append(worst_idx)
        kept.sort()
    out = [_FaderSample(frames[i], values[i]) for i in kept]
    # 去重连续重复
    deduped_out: List[_FaderSample] = []
    for p in out:
        if not deduped_out or deduped_out[-1].frame != p.frame:
            deduped_out.append(p)
    return deduped_out


# ─────────────────────────────────────────────────────────────────────────────
# 录制结果命令（覆盖语义）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OverwriteAutomationCommand:
    """AUD-15609：以"覆盖"语义写入一段自动化录制结果。

    模块契约（Write：覆盖整段已录曲线；Touch：松手回到原曲线）此前未实现：
    旧实现只追加关键帧（MacroCommand of AddAutomationKeyframeCommand），录制
    区间内的旧关键帧残留，新旧曲线混杂插值。本命令在 apply 时：

    1. 快照 track 的整个 automation dict（undo 完整恢复，含段外关键帧）；
    2. 删除每个录制段帧区间 [lo, hi] 内的旧关键帧；
    3. 写入简化后的新关键帧。

    单命令 = 单次 undo。注意：本类型仅作为 undo 栈顶命令使用，不注册进
    commands.py 的 ``_MACRO_SUBCOMMAND_TYPES``，不应嵌套进可序列化的宏。

    Attributes:
        command_id / label / track_id / param: 标准字段
        keyframes: ((frame, value), ...) 简化后的新关键帧
        overwrite_ranges: ((lo, hi), ...) 每个连续录制段的帧区间
        interpolation / min_val / max_val: 与 AddAutomationKeyframeCommand 一致
        _old_track_snapshot: apply 前的 automation dict 快照（dict-of-dict）
    """

    command_id: str
    label: str
    track_id: str
    param: str
    keyframes: Tuple[Tuple[int, float], ...]
    overwrite_ranges: Tuple[Tuple[int, int], ...]
    interpolation: str = "linear"
    min_val: float = 0.0
    max_val: float = 1.0
    _old_track_snapshot: Optional[Dict[str, Any]] = None

    def apply(self, timeline: Any) -> Tuple[Any, "OverwriteAutomationCommand"]:
        """执行覆盖写入，返回 (新 timeline, 携带旧值快照的命令)。"""
        from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_snap = {k: v.to_dict() for k, v in track.automation.items()}

        new_auto = track._clone_automation()
        kf_track = new_auto.get(self.param)
        if kf_track is None:
            kf_track = KeyframeTrack(
                param_name=self.param, min_val=self.min_val, max_val=self.max_val,
            )
            new_auto[self.param] = kf_track

        if self.overwrite_ranges:
            stale_frames = [
                kf.frame
                for kf in list(kf_track.keyframes)
                if any(lo <= kf.frame <= hi for lo, hi in self.overwrite_ranges)
            ]
            for frame in stale_frames:
                kf_track.remove_keyframe(frame)

        interp = InterpolationType.LINEAR
        try:
            interp = InterpolationType(str(self.interpolation).upper())
        except ValueError:
            interp = InterpolationType.LINEAR
        for frame, value in self.keyframes:
            kf_track.add_keyframe(
                Keyframe(frame=int(frame), value=float(value), interpolation=interp)
            )

        new_timeline = timeline.with_track_updated(
            track._rebuild(automation=new_auto)
        )
        updated_cmd = OverwriteAutomationCommand(
            command_id=self.command_id, label=self.label, track_id=self.track_id,
            param=self.param, keyframes=self.keyframes,
            overwrite_ranges=self.overwrite_ranges,
            interpolation=self.interpolation, min_val=self.min_val,
            max_val=self.max_val, _old_track_snapshot=old_snap,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: Any) -> Any:
        """按 apply 前快照完整恢复 automation（含段外关键帧）。"""
        if self._old_track_snapshot is None:
            return timeline
        from src.shared.keyframe import KeyframeTrack

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        new_auto: Dict[str, Any] = {}
        for param, track_dict in self._old_track_snapshot.items():
            try:
                new_auto[param] = KeyframeTrack.from_dict(track_dict)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "OverwriteAutomationCommand.undo: 无法恢复参数 %r 的自动化快照",
                    param,
                )
        return timeline.with_track_updated(
            track._rebuild(automation=new_auto)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "OverwriteAutomationCommand",
            "label": self.label,
            "track_id": self.track_id,
            "param": self.param,
            "keyframes": [{"frame": int(f), "value": float(v)} for f, v in self.keyframes],
            "overwrite_ranges": [
                {"lo": int(lo), "hi": int(hi)} for lo, hi in self.overwrite_ranges
            ],
            "interpolation": self.interpolation,
            "min_val": self.min_val,
            "max_val": self.max_val,
            "old_track_snapshot": self._old_track_snapshot,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "OverwriteAutomationCommand":
        keyframes = tuple(
            (int(kf["frame"]), float(kf["value"]))
            for kf in (data.get("keyframes") or [])
        )
        ranges = tuple(
            (int(r["lo"]), int(r["hi"]))
            for r in (data.get("overwrite_ranges") or [])
        )
        return OverwriteAutomationCommand(
            command_id=str(data.get("command_id", "")),
            label=str(data.get("label", "Record Automation")),
            track_id=str(data.get("track_id", "")),
            param=str(data.get("param", "volume")),
            keyframes=keyframes,
            overwrite_ranges=ranges,
            interpolation=str(data.get("interpolation", "linear")),
            min_val=float(data.get("min_val", 0.0)),
            max_val=float(data.get("max_val", 1.0)),
            _old_track_snapshot=data.get("old_track_snapshot"),
        )

    @staticmethod
    def create(
        track_id: str,
        param: str,
        keyframes: Tuple[Tuple[int, float], ...],
        overwrite_ranges: Tuple[Tuple[int, int], ...],
        interpolation: str = "linear",
        min_val: float = 0.0,
        max_val: float = 1.0,
    ) -> "OverwriteAutomationCommand":
        import uuid

        return OverwriteAutomationCommand(
            command_id=str(uuid.uuid4()),
            label=f"Record Automation ({param}, {len(keyframes)} keyframes)",
            track_id=track_id,
            param=param,
            keyframes=keyframes,
            overwrite_ranges=overwrite_ranges,
            interpolation=interpolation,
            min_val=min_val,
            max_val=max_val,
        )


# ─────────────────────────────────────────────────────────────────────────────
# AutomationRecorder
# ─────────────────────────────────────────────────────────────────────────────

class AutomationRecorder:
    """自动化录制器（§3.1 Write/Touch/Latch）。

    用法：
        recorder = AutomationRecorder()
        recorder.start("volume", mode="write", track_id="t1")
        # 播放循环中，每帧调 record（Write/Latch）或仅按住时调（Touch）
        recorder.record(frame=100, value=0.8)
        recorder.record(frame=101, value=0.82)
        ...
        # 停止：生成覆盖式写入命令（清录制区间旧关键帧 + 写入新关键帧）
        cmd = recorder.stop()
        tview._execute_command(cmd)  # 单次 undo

    线程约束（AUD-15610）：所有公开方法（start/record/stop/cancel）内部以
    ``_lock`` 串行化对 ``_samples``/``_segments`` 的读-改-写——允许采集线程
    调 record 与 UI 线程调 stop 并发（stop 先原子取走样本，record 的尾部
    采样落入新列表的问题不会发生）。
    """

    def __init__(self) -> None:
        self._active: bool = False
        self._param: str = ""
        self._track_id: str = ""
        self._mode: str = "off"
        self._samples: List[_FaderSample] = []
        self._touched: bool = False  # Touch/Latch：是否已触碰
        # AUD-15609: 录制段追踪（Touch 段间"回到原曲线"，Write/Latch 整段覆盖）。
        self._segments: List[Tuple[int, int]] = []
        self._segment_start: Optional[int] = None
        self._segment_end: int = 0
        # E16: Touch 松手 release——release_provider(frame) 返回底层曲线在
        # 该帧的原值（None=无曲线），松手后经 release_frames 帧线性回归。
        # 由调用方提供（调用方持有 timeline/track 引用），本模块不伪造目标值。
        self._release_provider: Optional[Callable[[int], Optional[float]]] = None
        self._release_frames: int = 12
        # AUD-15610: 保护 record/stop 并发的状态锁（非重入即可——两个方法
        # 互不调用，锁内不做任何回调/IO）。
        self._lock: threading.Lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._active

    @property
    def mode(self) -> str:
        return self._mode

    def start(
        self,
        param: str,
        mode: str,
        track_id: str,
        release_provider: Optional[Callable[[int], Optional[float]]] = None,
        release_frames: int = 12,
    ) -> None:
        """开始录制。

        Args:
            param: 参数名（"volume"/"pan"/"mute"）
            mode: "write"/"touch"/"latch"
            track_id: 轨道 ID
            release_provider: E16 Touch release——给定帧返回底层曲线原值
                （None=该参数无曲线/不可知）。仅 Touch 模式松手时调用；
                由调用方从当前 timeline 读取，本模块不做模型访问。
            release_frames: 松手后回归原曲线的斜坡长度（帧）。
        """
        with self._lock:
            self._active = True
            self._param = param
            self._track_id = track_id
            self._mode = mode.lower()
            self._samples = []
            self._touched = False
            self._segments = []
            self._segment_start = None
            self._segment_end = 0
            self._release_provider = release_provider
            self._release_frames = max(1, int(release_frames))

    def record(self, frame: int, value: float, fader_down: bool = True) -> None:
        """采集一个 fader 样本。

        Args:
            frame: 当前帧
            value: fader 值（参数值域）
            fader_down: 用户是否按住推子（Touch 模式用）
                - Write：忽略，总是记录
                - Touch：仅 fader_down=True 时记录
                - Latch：首次 fader_down=True 后持续记录
        """
        if not self._active:
            return
        mode = self._mode
        if mode == "write":
            should_record = True
        elif mode == "touch":
            should_record = fader_down
        elif mode == "latch":
            if fader_down:
                self._touched = True
            should_record = self._touched
        else:
            should_record = False

        # AUD-15610: 状态读-改-写整体持锁，避免与并发 stop() 的样本表重绑竞态。
        with self._lock:
            if should_record:
                frame_i = int(frame)
                if self._segment_start is None:
                    self._segment_start = frame_i
                    self._segment_end = frame_i
                else:
                    self._segment_start = min(self._segment_start, frame_i)
                    self._segment_end = max(self._segment_end, frame_i)
                self._samples.append(_FaderSample(frame=frame_i, value=float(value)))
            elif self._segment_start is not None:
                # 停写（Touch 松手）：闭合当前录制段，并写入 release 斜坡——
                # 从最后采样值线性回归到底层曲线原值（E16，此前 AUD-15611
                # 声明的行为边界）。目标值由 release_provider 提供；无曲线
                # 时保持旧行为（段间旧曲线自然延续）。
                release_frame = int(frame)
                segment_lo = self._segment_start
                segment_hi = self._segment_end
                self._segment_start = None
                if mode == "touch" and self._release_provider is not None:
                    last_value = (
                        float(self._samples[-1].value)
                        if self._samples else None
                    )
                    target = None
                    try:
                        target = self._release_provider(release_frame)
                    except Exception:  # noqa: BLE001 目标值不可得=退化为旧行为
                        logger.debug("release provider failed", exc_info=True)
                    if (
                        last_value is not None
                        and target is not None
                        and abs(float(target) - last_value) > 1e-6
                    ):
                        ramp_frames = self._release_frames
                        # 5 个中间采样 + 终点；simplify_samples 会进一步简化
                        for i in range(1, 6):
                            t = i / 6.0
                            self._samples.append(_FaderSample(
                                frame=release_frame + int(round(t * ramp_frames)),
                                value=last_value + (float(target) - last_value) * t,
                            ))
                        self._samples.append(_FaderSample(
                            frame=release_frame + ramp_frames,
                            value=float(target),
                        ))
                        segment_hi = max(segment_hi, release_frame + ramp_frames)
                self._segments.append((segment_lo, segment_hi))

    def stop(
        self,
        simplify_threshold: float = 0.005,
        make_command: Optional[Callable] = None,
    ) -> Optional[Any]:
        """停止录制，生成覆盖式写入命令（单次 undo）。

        Args:
            simplify_threshold: 关键帧简化阈值
            make_command: 命令工厂 (track_id, param, keyframes) -> command。
                None=默认构建 OverwriteAutomationCommand（清录制区间旧关键帧
                后写入新关键帧，AUD-15609）。

        Returns:
            合并的命令，或 None（无样本）
        """
        # AUD-15610: 原子取走录制状态（active 翻转 + 样本/段表重绑整体持锁），
        # 并发 record() 的尾部采样不会丢进已被取走的旧列表。简化/建命令是
        # 纯计算，放在锁外执行。
        with self._lock:
            if not self._active:
                return None
            self._active = False
            samples = self._samples
            self._samples = []
            segments = self._segments
            if self._segment_start is not None:
                segments = segments + [(self._segment_start, self._segment_end)]
                self._segment_start = None
            self._segments = []

        if not samples:
            return None

        # 简化为关键帧
        keyframes = simplify_samples(samples, threshold=simplify_threshold)
        if not keyframes:
            return None

        if make_command is not None:
            return make_command(self._track_id, self._param, keyframes)

        # 默认：覆盖式写入（删除录制区间内旧关键帧 + 添加新关键帧）
        return self._build_overwrite_command(
            self._track_id, self._param, keyframes, tuple(segments)
        )

    def _build_overwrite_command(
        self,
        track_id: str,
        param: str,
        keyframes: List[_FaderSample],
        overwrite_ranges: Tuple[Tuple[int, int], ...],
    ) -> OverwriteAutomationCommand:
        """构建覆盖式写入命令（单命令、单次 undo，AUD-15609）。"""
        # 参数值域（与 KeyframeTrack 一致）
        param_bounds = {
            "volume": (0.0, 2.0),
            "pan": (-1.0, 1.0),
            "mute": (0.0, 1.0),
        }
        min_val, max_val = param_bounds.get(param, (0.0, 1.0))

        return OverwriteAutomationCommand.create(
            track_id=track_id,
            param=param,
            keyframes=tuple((int(kf.frame), float(kf.value)) for kf in keyframes),
            overwrite_ranges=tuple(
                (int(lo), int(hi)) for lo, hi in overwrite_ranges
            ),
            interpolation="linear",
            min_val=min_val,
            max_val=max_val,
        )

    def stop_for_preview(
        self, simplify_threshold: float = 0.005,
    ) -> List[Tuple[int, float]]:
        """P1-5：停止录制并返回简化关键帧（不构建命令——预览捕获用）。

        返回 [(frame, value), ...]；无样本返回空列表。
        """
        with self._lock:
            if not self._active:
                return []
            self._active = False
            samples = self._samples
            self._samples = []
            self._segments = []
            self._segment_start = None
        keyframes = simplify_samples(samples, threshold=simplify_threshold)
        return [(int(kf.frame), float(kf.value)) for kf in keyframes]

    def cancel(self) -> None:
        """取消录制（不生成命令）。"""
        with self._lock:
            self._active = False
            self._samples = []
            self._touched = False
            self._segments = []
            self._segment_start = None

class AutomationPreviewCapture:
    """P1-5：自动化预览捕获——采集样本不进命令栈，预听后可提交/丢弃。

    与 :class:`AutomationRecorder` 同一采集语义（write/touch/latch），
    区别在 ``finish()``：不构建命令，而是返回简化后的关键帧样本，供
    引擎预览层（``AudioMixer.set_track_automation_preview``）挂载预听。
    ``commit()`` 由调用方据预览样本构建 ``OverwriteAutomationCommand``
    （单次 undo）；``discard()`` 直接丢弃预览层。
    """

    def __init__(self, param: str, mode: str, track_id: str = "") -> None:
        self._inner = AutomationRecorder()
        self.param = str(param)
        self.mode = str(mode)
        self.track_id = str(track_id)

    def start(self) -> None:
        self._inner.start(self.param, self.mode, self.track_id)

    def record(self, frame: int, value: float, fader_down: bool = True) -> None:
        self._inner.record(frame, value, fader_down=fader_down)

    @property
    def is_recording(self) -> bool:
        return self._inner.is_recording

    def finish(self, simplify_threshold: float = 0.005) -> list[tuple[int, float]]:
        """停止采集并返回简化关键帧 [(frame, value), ...]（不建命令）。"""
        return self._inner.stop_for_preview(simplify_threshold)

    def cancel(self) -> None:
        self._inner.cancel()


# ─────────────────────────────────────────────────────────────────────────────
# B07/§13 步骤 6：Trim 烘入曲线（coalesce）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CoalesceAutomationTrimCommand:
    """把 volume trim 恒定倍率烘入自动化曲线后复位 trim（单次 undo）。

    合同（主计划 §13 步骤 6）：coalesce 前后 ``get_automation_value`` 逐帧
    相等（声音不变），不累计不可逆偏置。trim × 曲线值若溢出曲线
    [min_val, max_val]（KeyframeTrack 求值时 clamp），烘入会改变声音 →
    拒绝执行并保留 trim（显式诊断，不静默改变听感）。无曲线时烘入
    track.volume（静态默认值的唯一存储；仅 volume 参数参与 trim）。

    apply 返回携带完整快照的新命令实例（undo 一次恢复 automation/trim/
    volume）。
    """

    command_id: str
    label: str
    track_id: str
    param: str = "volume"
    _old_automation: Optional[Dict[str, Any]] = None
    _old_trim: Optional[Dict[str, float]] = None
    _old_volume: Optional[float] = None

    @staticmethod
    def create(track_id: str, param: str = "volume",
               command_id: str = "") -> "CoalesceAutomationTrimCommand":
        if param != "volume":
            raise ValueError(
                "trim coalescing is only defined for volume (pan/mute have "
                "no trim contract)")
        return CoalesceAutomationTrimCommand(
            command_id=command_id or f"coalesce-trim-{track_id}",
            label=f"Coalesce volume trim ({track_id})",
            track_id=track_id, param=param,
        )

    def apply(
        self, timeline: Any,
    ) -> Tuple[Any, "CoalesceAutomationTrimCommand"]:
        from src.shared.keyframe import Keyframe, KeyframeTrack

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        factor = track.automation_trim_factor(self.param)
        if factor == 1.0:
            raise ValueError(
                f"track {self.track_id} has no volume trim to coalesce")
        old_auto = {k: v.to_dict() for k, v in track.automation.items()}
        old_trim = dict(track.automation_trim)
        old_volume = float(track.volume)

        curve = track.automation.get(self.param)
        if curve is not None and getattr(curve, "keyframes", None):
            baked: list = []
            for kf in curve.keyframes:
                value = float(kf.value) * factor
                if value < curve.min_val - 1e-9 or value > curve.max_val + 1e-9:
                    raise ValueError(
                        f"coalescing trim {factor} would clamp keyframe at "
                        f"frame {kf.frame} ({value} outside "
                        f"[{curve.min_val}, {curve.max_val}]); sound would "
                        "change — trim kept, adjust curve first")
                baked.append(Keyframe(
                    frame=int(kf.frame), value=value,
                    interpolation=kf.interpolation))
            new_curve = KeyframeTrack(
                param_name=curve.param_name,
                min_val=float(curve.min_val), max_val=float(curve.max_val))
            for kf in baked:
                new_curve.add_keyframe(kf)
            new_auto = track._clone_automation()
            new_auto[self.param] = new_curve
            new_track = track._rebuild(
                automation=new_auto,
                automation_trim={k: v for k, v in old_trim.items()
                                 if k != self.param},
            )
        else:
            # 无曲线：静态默认（track.volume）承担 trim（模型 get_automation_value
            # 的 default×factor 语义 → volume×factor、trim=1 等价）。
            new_volume = old_volume * factor
            if new_volume < 0.0 - 1e-9 or new_volume > 2.0 + 1e-9:
                raise ValueError(
                    f"coalescing trim {factor} would push volume {new_volume} "
                    "outside [0, 2]; trim kept")
            new_track = track._rebuild(
                volume=new_volume,
                automation_trim={k: v for k, v in old_trim.items()
                                 if k != self.param},
            )
        new_timeline = timeline.with_track_updated(new_track)
        updated = CoalesceAutomationTrimCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, param=self.param,
            _old_automation=old_auto, _old_trim=old_trim,
            _old_volume=old_volume,
        )
        return new_timeline, updated

    def undo(self, timeline: Any) -> Any:
        if self._old_automation is None:
            return timeline
        from src.shared.keyframe import KeyframeTrack

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        restored: Dict[str, Any] = {}
        for param, snap in self._old_automation.items():
            try:
                restored[param] = KeyframeTrack.from_dict(snap)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "CoalesceAutomationTrimCommand.undo: 无法恢复参数 %r 快照",
                    param)
        return timeline.with_track_updated(track._rebuild(
            automation=restored,
            automation_trim=dict(self._old_trim or {}),
            volume=float(self._old_volume if self._old_volume is not None
                         else track.volume),
        ))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "CoalesceAutomationTrimCommand",
            "label": self.label,
            "track_id": self.track_id,
            "param": self.param,
            "old_automation": self._old_automation,
            "old_trim": self._old_trim,
            "old_volume": self._old_volume,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "CoalesceAutomationTrimCommand":
        return CoalesceAutomationTrimCommand(
            command_id=str(data.get("command_id", "")),
            label=str(data.get("label", "Coalesce trim")),
            track_id=str(data.get("track_id", "")),
            param=str(data.get("param", "volume")),
            _old_automation=data.get("old_automation"),
            _old_trim=data.get("old_trim"),
            _old_volume=data.get("old_volume"),
        )
