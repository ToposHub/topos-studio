"""F5.4 · 通用图像序列导入（PNG/TIFF 子集）——纯模型扫描与诊断。

与 ``topos_image_sequence``（.toos 私有序列）并列：这里处理**通用格式**
（PNG/TIFF 子集先明；EXR/DPX 按需求验证位深/alpha/AOV 后再开）。

语义（对齐计划验收）：
- **一个序列 = 一个资产**：``<prefix><frame><suffix>`` 同目录同模式文件
  聚合（shot_1001.png…shot_1080.png → 单 image_sequence 资产）；
- **缺帧诊断不伪造**：``SequenceScanResult.holes`` 明示空洞帧号
  （shot 缺 1007 → 诊断行），消费侧选择拒绝/跳过/保持（沿用
  topos 序列的 MissingFramePolicy 语义）；
- **显式选择，不强制合流**：``classify_directory`` 把"连号照片"与
  "孤立单帧"分开报告——调用方显式选择导入模式（单帧或序列），
  不能强制把连号照片合为序列；
- **指纹**：文件集合（帧号+大小+mtime）哈希——移盘/重开后帧号与
  内容一致性可校验（重扫描指纹不变 ⇒ 同一序列）。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: 本期明确支持的通用序列扩展名（PNG/TIFF 子集；EXR/DPX 待位深/alpha/AOV 验证）
GENERIC_SEQUENCE_EXTENSIONS: Tuple[str, ...] = (".png", ".tif", ".tiff")

_PATTERN = re.compile(
    r"^(?P<prefix>.*?)(?P<frame>-?\d+)(?P<suffix>\.[^.]+)$")


def parse_generic_frame_name(filename: str) -> Optional[Tuple[str, int, str]]:
    """解析 `<prefix><frame><suffix>` → (prefix, 帧号, suffix)。

    padding 由文件名原样保留在 prefix 匹配里（`shot_1001.png` 的
    frame=1001；padding 只影响展示，不影响数值——与 topos 序列冻结
    语法同型）。
    """
    m = _PATTERN.match(filename)
    if m is None:
        return None
    return m.group("prefix"), int(m.group("frame")), m.group("suffix")


@dataclass(frozen=True)
class SequenceCandidate:
    """一个可导入的序列候选（同 prefix+suffix 的连续/近连续文件组）。"""

    directory: str
    prefix: str
    suffix: str
    frames: Tuple[int, ...]          # 升序数值帧号
    file_by_frame: Dict[int, str] = field(default_factory=dict)
    padding: int = 0                 # 首帧文件名的数字位数（展示用）

    @property
    def first_frame(self) -> int:
        return self.frames[0] if self.frames else 0

    @property
    def last_frame(self) -> int:
        return self.frames[-1] if self.frames else 0

    @property
    def holes(self) -> Tuple[int, ...]:
        """[首帧, 末帧] 区间内的空洞帧号（缺帧诊断，不伪造）。"""
        if not self.frames:
            return ()
        expect = range(self.first_frame, self.last_frame + 1)
        return tuple(f for f in expect if f not in self.file_by_frame)

    def fingerprint(self) -> str:
        """文件集合指纹（帧号+大小+mtime）——移盘/重开一致性校验。"""
        h = hashlib.sha256()
        for frame in self.frames:
            path = self.file_by_frame[frame]
            try:
                stat = os.stat(path)
                h.update(f"{frame}:{stat.st_size}:{stat.st_mtime_ns};".encode())
            except OSError:
                h.update(f"{frame}:missing;".encode())
        return h.hexdigest()


@dataclass(frozen=True)
class SequenceScanResult:
    """目录扫描结果：序列候选 + 孤立单帧（显式选择，不合流）。"""

    candidates: Tuple[SequenceCandidate, ...]
    single_frames: Tuple[str, ...]   # 无相邻帧的孤立文件（默认单帧导入）

    def diagnostics(self) -> List[str]:
        lines = []
        for cand in self.candidates:
            tag = f"序列 {cand.prefix}*{cand.suffix}（{len(cand.frames)} 帧，"
            tag += f"{cand.first_frame}-{cand.last_frame}）"
            if cand.holes:
                shown = list(cand.holes[:8])
                more = f"…共 {len(cand.holes)} 帧" if len(cand.holes) > 8 else ""
                lines.append(f"{tag} 缺帧：{shown}{more}")
            else:
                lines.append(f"{tag} 完整")
        return lines


def classify_directory(
    directory: str,
    *,
    extensions: Tuple[str, ...] = GENERIC_SEQUENCE_EXTENSIONS,
    min_sequence_frames: int = 2,
) -> SequenceScanResult:
    """扫描目录：按 (prefix, suffix) 聚合；组内 ≥min_sequence_frames 帧
    才算序列候选，否则归孤立单帧（不能强制把连号照片合为序列——
    min_sequence_frames=2 时两连号即候选，单帧永远单帧）。"""
    groups: Dict[Tuple[str, str], Dict[int, str]] = {}
    if not os.path.isdir(directory):
        return SequenceScanResult(candidates=(), single_frames=())
    for name in sorted(os.listdir(directory)):
        parsed = parse_generic_frame_name(name)
        if parsed is None:
            continue
        prefix, frame, suffix = parsed
        if suffix.lower() not in extensions:
            continue
        groups.setdefault((prefix, suffix), {})[frame] = os.path.join(
            directory, name)
    candidates: List[SequenceCandidate] = []
    singles: List[str] = []
    for (prefix, suffix), files in sorted(groups.items()):
        frames = tuple(sorted(files))
        if len(frames) >= min_sequence_frames:
            first_name = os.path.basename(files[frames[0]])
            digits = re.search(r"(-?\d+)\.$", first_name + ".")
            padding = len(digits.group(1)) if digits else 0
            candidates.append(SequenceCandidate(
                directory=directory, prefix=prefix, suffix=suffix,
                frames=frames, file_by_frame=dict(files),
                padding=padding))
        else:
            singles.append(files[frames[0]])
    return SequenceScanResult(
        candidates=tuple(candidates), single_frames=tuple(singles))


__all__ = [
    "GENERIC_SEQUENCE_EXTENSIONS",
    "SequenceCandidate",
    "SequenceScanResult",
    "classify_directory",
    "parse_generic_frame_name",
]
