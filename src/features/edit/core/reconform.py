"""P2 · 画面改版 Reconform 核心算法（2026-11-19 计划第二阶段）。

镜头 ID / 源时间码 / 内容指纹对照 → 输出插删、替换、移位的候选变更表
（编辑者逐段确认或回滚；音乐连贯、音频 handles 与保护轨由命令层在
确认后处理）。纯函数：

- ``shot_fingerprint``：镜头内容指纹（帧亮度/色度直方图序列的稳定哈希
  ——对小幅噪点鲁棒）；
- ``match_shots``：旧版/新版镜头序列对齐（LCS——保留段直接映射，
  差异输出 insert/delete/move 候选）；
- ``plan_audio_reconform``：对齐结果 → 音频轨的移动/裁剪计划（保留
  音频 handles：移动边界两侧各留 handle_frames 不裁）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np


def shot_fingerprint(frames: np.ndarray, bins: int = 32) -> str:
    """镜头内容指纹：帧块亮度/色度直方图序列的 SHA-256。

    ``frames``: (n, h, w) 或 (n, h, w, 3) uint8。分 4 个时间块各自
    取亮度+两色度通道直方图，拼接后哈希——小幅噪点/编码差异不改变
    直方图桶占位，指纹稳定。
    """
    if frames is None or frames.size == 0:
        return ""
    arr = np.asarray(frames)
    luminance = (arr[..., :3].mean(axis=-1) if arr.ndim == 4
                 else arr.astype(np.float64))
    # 按块统计指纹（4 时间块；块内亮度均值量化到 4 级、标准差到 8 级）
    # ——小幅噪点/重编码不改变块统计的量化桶；真实内容变化（分布
    # 移动 > 4 灰阶）改变指纹。
    blocks = np.array_split(
        np.arange(luminance.shape[0]), max(1, min(4, luminance.shape[0])))
    parts: List[str] = []
    for block in blocks:
        if block.size == 0:
            continue
        values = luminance[block]
        mean_q = int(float(values.mean()) // 4)
        std_q = int(float(values.std()) // 8)
        parts.append(f"{mean_q}:{std_q}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ShotRef:
    """一个镜头的稳定身份（ID/源时间码/指纹任一命中即候选）。"""

    shot_id: str
    source_tc_frames: int
    duration_frames: int
    fingerprint: str = ""


@dataclass(frozen=True)
class ReconformChange:
    """一个候选变更（编辑者确认/回滚）。"""

    kind: str                    # "keep" | "insert" | "delete" | "move"
    old_shot: Optional[ShotRef] = None
    new_shot: Optional[ShotRef] = None
    old_start: int = 0           # 旧版时间线帧（keep/move/delete）
    new_start: int = 0           # 新版时间线帧（keep/move/insert）


def match_shots(
    old_shots: Sequence[ShotRef],
    new_shots: Sequence[ShotRef],
) -> List[ReconformChange]:
    """LCS 对齐（身份 = shot_id 或指纹命中）：输出候选变更表。"""
    n, m = len(old_shots), len(new_shots)
    same = [[False] * m for _ in range(n)]
    for i, old in enumerate(old_shots):
        for j, new in enumerate(new_shots):
            if old.shot_id and old.shot_id == new.shot_id:
                same[i][j] = True
            elif old.fingerprint and old.fingerprint == new.fingerprint:
                same[i][j] = True

    # LCS 表。
    lcs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if same[i][j]:
                lcs[i][j] = lcs[i + 1][j + 1] + 1
            else:
                lcs[i][j] = max(lcs[i + 1][j], lcs[i][j + 1])

    changes: List[ReconformChange] = []
    old_cursor = 0
    new_cursor = 0
    old_start = 0
    new_start = 0
    while old_cursor < n and new_cursor < m:
        if same[old_cursor][new_cursor]:
            changes.append(ReconformChange(
                kind="keep", old_shot=old_shots[old_cursor],
                new_shot=new_shots[new_cursor],
                old_start=old_start, new_start=new_start))
            old_start += old_shots[old_cursor].duration_frames
            new_start += new_shots[new_cursor].duration_frames
            old_cursor += 1
            new_cursor += 1
        elif lcs[old_cursor + 1][new_cursor] >= lcs[old_cursor][new_cursor + 1]:
            changes.append(ReconformChange(
                kind="delete", old_shot=old_shots[old_cursor],
                old_start=old_start))
            old_start += old_shots[old_cursor].duration_frames
            old_cursor += 1
        else:
            changes.append(ReconformChange(
                kind="insert", new_shot=new_shots[new_cursor],
                new_start=new_start))
            new_start += new_shots[new_cursor].duration_frames
            new_cursor += 1
    while old_cursor < n:
        changes.append(ReconformChange(
            kind="delete", old_shot=old_shots[old_cursor],
            old_start=old_start))
        old_start += old_shots[old_cursor].duration_frames
        old_cursor += 1
    while new_cursor < m:
        changes.append(ReconformChange(
            kind="insert", new_shot=new_shots[new_cursor],
            new_start=new_start))
        new_start += new_shots[new_cursor].duration_frames
        new_cursor += 1

    # move 候选：delete+insert 对里指纹命中但顺序被打散的镜头。
    deletes = {change.old_shot.fingerprint: change
               for change in changes
               if change.kind == "delete" and change.old_shot.fingerprint}
    refined: List[ReconformChange] = []
    consumed = set()
    for change in changes:
        if change.kind == "insert" and change.new_shot.fingerprint \
                and change.new_shot.fingerprint in deletes:
            old_change = deletes[change.new_shot.fingerprint]
            if id(old_change) not in consumed:
                refined.append(ReconformChange(
                    kind="move", old_shot=old_change.old_shot,
                    new_shot=change.new_shot,
                    old_start=old_change.old_start,
                    new_start=change.new_start))
                consumed.add(id(old_change))
                continue
        refined.append(change)
    return [change for change in refined
            if not (change.kind == "delete"
                    and change.old_shot
                    and change.old_shot.fingerprint in deletes
                    and id(change) in consumed)]


@dataclass(frozen=True)
class AudioReconformPlan:
    """音频轨的重排计划（确认后由命令层执行）。"""

    moves: Tuple = field(default=())   # (old_start, old_len, new_start)
    trims: Tuple = field(default=())   # (start, keep_frames, handle_frames)
    keeps: Tuple = field(default=())   # (old_start, new_start, frames)


def plan_audio_reconform(
    changes: Sequence[ReconformChange],
    *,
    handle_frames: int = 12,
) -> AudioReconformPlan:
    """对齐结果 → 音频移动/裁剪计划（保留音频 handles）。"""
    moves: list = []
    trims: list = []
    keeps: list = []
    for change in changes:
        if change.kind == "keep" and change.old_shot:
            length = change.old_shot.duration_frames
            keeps.append((change.old_start, change.new_start, length))
            # handles：保留段两端各留 handle 不参与后续裁剪对齐。
            trims.append((change.old_start, length, handle_frames))
        elif change.kind == "move" and change.old_shot:
            moves.append((change.old_start,
                          change.old_shot.duration_frames,
                          change.new_start))
    return AudioReconformPlan(
        moves=tuple(moves), trims=tuple(trims), keeps=tuple(keeps))
