"""TAEP M8 — 镜头区间（ShotSpan）派生 + 关键帧抽样 + 可重建 manifest。

现状：scene cut 在 ``AssetAnalysis.scene_cuts`` 里只是**边界点**（``{frame, ...}``），
``analysis_to_segments`` 把每个 cut 渲染成 ``FrameRange(frame, 1)`` 的 SHOT segment
（边界事件）。M8 视觉分析（OCR / 标签）需要**镜头区间**（span）概念来避免逐帧跑、避免
单帧幻觉、并按 shot 聚合置信度（§18.1）。

本模块从 scene cut 边界点 + 总帧数派生 ``ShotSpan`` 列表，并为每个 shot 选代表帧 /
关键帧采样点。它**不**碰 ``VideoFrameSampler``（那是 scene_cut 层的 PyAV 帧解码器）；
本模块定义 ai_editing 层的 ``FrameSamplerProtocol`` 作为注入点——生产由控制器层包装
``VideoFrameSampler.sample_single_frame``，测试用 ``StubFrameSampler``。

红线（§18.1 / §18.6）：

- ``KeyframeManifest`` **只记录哪些帧被采样**（帧索引 + 采样策略 hash），**不存图像 /
  embedding / 缩略图**。这些是可重建缓存，删除后可从 manifest 重抽，不进工程文件。
- 关键帧抽样按 shot 的首/中/尾（``head_mid_tail``），**不逐帧**——避免单帧幻觉 + 重复
  检测，并使置信度可在 shot 内聚合。
- 输入的 scene cuts / total_frames 来自既有 ``AssetAnalysis``；本模块纯函数，无 Qt、
  无 IO、无 GPU。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


# ---------------------------------------------------------------------- #
# 采样策略
# ---------------------------------------------------------------------- #


# 关键帧采样策略标签（人类可读，用于 manifest config_hash + 对比）。
# 首版只支持 head_mid_tail；后续可加 even / scene_change_only 等。
STRATEGY_HEAD_MID_TAIL = "head_mid_tail"
SUPPORTED_STRATEGIES: tuple[str, ...] = (STRATEGY_HEAD_MID_TAIL,)


# ---------------------------------------------------------------------- #
# ShotSpan：从 scene cut 边界派生的镜头区间
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShotSpan:
    """从相邻 scene cuts 派生的镜头区间（§18.1）。半开 ``[start_frame, end_frame)``。

    ``shot_index`` 从 0 起，按 start 升序。``duration = end_frame - start_frame``。
    """

    shot_index: int
    start_frame: int
    end_frame: int

    def __post_init__(self) -> None:
        if self.shot_index < 0:
            raise ValueError(f"shot_index must be >= 0, got {self.shot_index}")
        if self.start_frame < 0:
            raise ValueError(f"start_frame must be >= 0, got {self.start_frame}")
        if self.end_frame <= self.start_frame:
            raise ValueError(
                f"end_frame must be > start_frame, got [{self.start_frame}, {self.end_frame})"
            )

    @property
    def duration(self) -> int:
        return self.end_frame - self.start_frame

    def representative_frame(self) -> int:
        """代表帧：选中点（避免单帧幻觉）；极短 shot（duration<2）用首帧。

        §18.1：单帧只代表局部画面；代表帧取 shot 内中部更稳。
        """
        if self.duration < 2:
            return self.start_frame
        return self.start_frame + self.duration // 2

    def keyframe_indices(self, *, strategy: str = STRATEGY_HEAD_MID_TAIL) -> tuple[int, ...]:
        """该 shot 的关键帧采样点。

        ``head_mid_tail``：首 / 中 / 尾三帧（去重；极短 shot 收敛到 1-2 帧）。
        帧号钳制在 ``[start_frame, end_frame)`` 内。
        """
        if strategy != STRATEGY_HEAD_MID_TAIL:
            raise ValueError(
                f"unsupported keyframe strategy {strategy!r}; "
                f"supported: {SUPPORTED_STRATEGIES}"
            )
        if self.duration < 2:
            return (self.start_frame,)
        if self.duration < 3:
            # 2 帧：首 + 尾（去重后 2 帧）
            return (self.start_frame, self.start_frame + 1)
        head = self.start_frame
        mid = self.start_frame + self.duration // 2
        tail = self.end_frame - 1
        # 去重保序（mid 可能等于 head/tail 在极短 shot，但上面已挡）
        seen: list[int] = []
        for f in (head, mid, tail):
            if f not in seen:
                seen.append(f)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_index": self.shot_index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "duration": self.duration,
        }


def derive_shot_spans(
    scene_cuts: Sequence[dict[str, Any]],
    *,
    total_frames: int,
) -> tuple[ShotSpan, ...]:
    """从 scene cut 边界点 + 总帧数派生 shot 区间列表（§18.1）。

    scene cuts 中的 ``frame`` 字段是边界点。本函数把边界点排序、去重，相邻边界构成
    一个 shot；第一个 shot 从 0 开始，最后一个到 ``total_frames``。

    Args:
        scene_cuts: ``AssetAnalysis.scene_cuts``（每个含 ``frame`` int）。无序/重复都安全。
        total_frames: 素材总帧数（>0）。无 cut → 单 shot 覆盖 ``[0, total_frames)``。

    Returns:
        按 start 升序的 ``ShotSpan`` 元组（至少 1 个，若 ``total_frames > 0``）。
    """
    if total_frames <= 0:
        return ()
    # 收集边界点，排序去重，过滤掉 [0, total_frames] 外的点
    boundaries: set[int] = {0}
    for cut in scene_cuts:
        try:
            f = int(cut.get("frame", 0))
        except (TypeError, ValueError):
            continue
        if 0 < f < total_frames:
            boundaries.add(f)
    sorted_bounds = sorted(boundaries)
    # 末尾补 total_frames 作为最后一个 shot 的 end
    spans: list[ShotSpan] = []
    for i, start in enumerate(sorted_bounds):
        end = sorted_bounds[i + 1] if i + 1 < len(sorted_bounds) else total_frames
        if end > start:
            spans.append(ShotSpan(shot_index=len(spans), start_frame=start, end_frame=end))
    return tuple(spans)


# ---------------------------------------------------------------------- #
# FrameSamplerProtocol：帧抽取注入点
# ---------------------------------------------------------------------- #


class FrameSamplerProtocol(Protocol):
    """帧抽取契约（注入点；测试用 stub，生产包装 VideoFrameSampler）。

    ``sample(frame_index)`` 返回 BGR ``np.ndarray``（HxWx3）或 ``None``（帧不存在）。
    生产实现由控制器层注入，包装 ``VideoFrameSampler.sample_single_frame``（PyAV，VFR
    安全）。本协议使 provider 可单测、不硬编码路径。
    """

    def sample(self, frame_index: int) -> Any: ...


class StubFrameSampler:
    """确定性 stub：返回与 frame_index 相关的固定 ``np.ndarray``（测试用）。

    无 GPU / 无真实文件依赖。返回的 ndarray 形状固定 ``(2, 2, 3)``，像素值由
    ``frame_index`` 决定（使测试能区分不同帧）。需要 numpy（已是项目依赖）。
    """

    def __init__(self, *, width: int = 2, height: int = 2) -> None:
        self._w = max(1, width)
        self._h = max(1, height)
        self._sampled: list[int] = []  # 记录被采样的帧（测试断言用）

    def sample(self, frame_index: int) -> Any:
        import numpy as np

        self._sampled.append(frame_index)
        # 像素 = frame_index 取模 256（BGR 三通道同值，灰度）
        val = frame_index % 256
        return np.full((self._h, self._w, 3), val, dtype=np.uint8)

    @property
    def sampled_frames(self) -> tuple[int, ...]:
        return tuple(self._sampled)


# ---------------------------------------------------------------------- #
# KeyframeManifest：可重建关键帧清单
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class KeyframeEntry:
    """单个关键帧采样记录（§18.6）。不含图像，只记帧号 + 所属 shot。"""

    shot_index: int
    frame: int

    def to_dict(self) -> dict[str, Any]:
        return {"shot_index": self.shot_index, "frame": self.frame}


@dataclass(frozen=True)
class KeyframeManifest:
    """可重建关键帧清单（§18.6：embedding / 缩略图是可重建缓存，不进工程）。

    只记录"哪些帧被采样"（``entries``）+ 采样策略 ``config_hash``。``config_hash`` 随
    策略变化（``head_mid_tail`` vs 未来 ``even``）→ 据此判断 stale。真实图像/embedding
    按需从 manifest + ``FrameSamplerProtocol`` 重抽，删除缓存不影响工程可打开性。
    """

    asset_id: str
    entries: tuple[KeyframeEntry, ...]
    config_hash: str

    @property
    def frames(self) -> tuple[int, ...]:
        return tuple(e.frame for e in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "entries": [e.to_dict() for e in self.entries],
            "config_hash": self.config_hash,
            "frame_count": len(self.entries),
        }


def build_keyframe_manifest(
    shot_spans: Sequence[ShotSpan],
    *,
    asset_id: str,
    strategy: str = STRATEGY_HEAD_MID_TAIL,
) -> KeyframeManifest:
    """从 shot spans 构建关键帧清单（每 shot 按策略抽样）。

    Args:
        shot_spans: ``derive_shot_spans`` 的输出。
        asset_id: 关联素材 id（manifest 标注用）。
        strategy: 采样策略（首版只 ``head_mid_tail``）。
    """
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(
            f"unsupported strategy {strategy!r}; supported: {SUPPORTED_STRATEGIES}"
        )
    entries: list[KeyframeEntry] = []
    for span in shot_spans:
        for frame in span.keyframe_indices(strategy=strategy):
            entries.append(KeyframeEntry(shot_index=span.shot_index, frame=frame))
    return KeyframeManifest(
        asset_id=asset_id,
        entries=tuple(entries),
        config_hash=_strategy_hash(strategy),
    )


def _strategy_hash(strategy: str) -> str:
    """采样策略的稳定 hash（变更 → manifest stale）。"""
    return hashlib.sha256(f"keyframe-strategy:{strategy}".encode("utf-8")).hexdigest()[:16]
