"""M04.4 · 通用序列分类（连号照片 → 单一 image_sequence 素材）。

导入批中的连号静帧（``frame_0001.jpg … frame_0099.jpg``）此前逐张
成为独立 image 素材——媒体池冗余、插入时间线需百次拖放。本模块按
**同目录 + 同扩展名 + 同前缀 + 尾号连续**分类：

- 连续（≥ ``min_count``，缺号≤ ``max_gap_tolerance`` 且显式登记缺口）
  → 一组 ``StillSequenceGroup``（按首帧路径作资产路径、``metadata``
  携带帧清单/帧数/缺口诊断），媒体池经 ``project.import_asset`` 建
  **单个** ``image_sequence`` 素材；
- 缺口诊断（"1001 序列缺 1007 可诊断"）：缺口帧号列表进
  ``missing_numbers``，调用方如实呈现（可诊断≠可修复——渲染时缺帧
  由序列消费者按既有语义处理）；
- 不满足成组条件的路径原样返回 singles（不吞、不改判）。

纯函数不改工程；消费方（媒体池导入批）决定建资产口径。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

#: 尾号捕获：stem 以数字结尾（至少 1 位）
_TRAILING_NUM = re.compile(r"^(.*?)(\d+)$")

#: 帧号域上限（合理照片序列；更大的号域视为编号巧合不成组）
_MAX_NUMBER = 1_000_000


@dataclass(frozen=True)
class StillSequenceGroup:
    """一组连号静帧（单资产候选）。"""

    first_path: str
    frame_paths: Tuple[str, ...]
    prefix: str
    extension: str
    numbers: Tuple[int, ...]
    missing_numbers: Tuple[int, ...] = ()

    @property
    def frame_count(self) -> int:
        return len(self.frame_paths)

    def asset_metadata(self, fps: float = 25.0) -> Dict:
        """建 image_sequence 素材的 metadata 载荷。"""
        return {
            "sequence": {
                "frames": list(self.frame_paths),
                "frame_count": self.frame_count,
                "first_number": self.numbers[0],
                "last_number": self.numbers[-1],
                "missing_numbers": list(self.missing_numbers),
                "prefix": self.prefix,
                "extension": self.extension,
                "fps": float(fps),
            },
        }


def classify_import_batch(paths: List[str], *, min_count: int = 3,
                          max_span: int = 10000,
                          ) -> Tuple[List[StillSequenceGroup], List[str]]:
    """把导入批分类为（成组序列, 单张路径）。

    成组条件：同目录 + 同扩展名 + 同前缀 + 尾号在连续域（首尾差
    < ``max_span``；存在缺口时仍成组但登记 ``missing_numbers``）且
    实到帧数 ≥ ``min_count``。排序稳定（按帧号升序）。
    """
    buckets: Dict[Tuple[str, str, str], List[Tuple[int, str]]] = {}
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        match = _TRAILING_NUM.match(stem)
        if match is None:
            continue
        number = int(match.group(2))
        if number > _MAX_NUMBER:
            continue
        key = (os.path.dirname(path) or ".",
               os.path.splitext(path)[1].lower(), match.group(1))
        buckets.setdefault(key, []).append((number, path))

    groups: List[StillSequenceGroup] = []
    grouped_paths = set()
    for (directory, ext, prefix), entries in sorted(buckets.items()):
        entries.sort()
        numbers = [n for n, _ in entries]
        if len(numbers) < min_count:
            continue
        if numbers[-1] - numbers[0] >= max_span:
            continue
        missing = _missing_in_range(numbers)
        # 缺口过多视为编号巧合（实到帧占标称域过半才成组）
        nominal = numbers[-1] - numbers[0] + 1
        if len(numbers) < nominal * 0.5:
            continue
        groups.append(StillSequenceGroup(
            first_path=entries[0][1],
            frame_paths=tuple(p for _, p in entries),
            prefix=prefix, extension=ext,
            numbers=tuple(numbers),
            missing_numbers=tuple(missing)))
        grouped_paths.update(p for _, p in entries)

    singles = [p for p in paths if p not in grouped_paths]
    return groups, singles


def _missing_in_range(numbers: List[int]) -> List[int]:
    """连续域内的缺口帧号（诊断用；域外不判）。"""
    if not numbers:
        return []
    present = set(numbers)
    return [n for n in range(numbers[0], numbers[-1] + 1)
            if n not in present]


__all__ = ["StillSequenceGroup", "classify_import_batch"]
