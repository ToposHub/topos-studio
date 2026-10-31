"""F6.2 · 安全重连匹配器——多因子候选与"仅同名不替换"守卫。

现状缺口（调研）：``relink_media_dialog._on_relink_by_directory`` 按
**文件名匹配**即批量重连——重名异片会被静默误连（计划明文"不能仅
同名就替换"）。本模块提供可测试的匹配核：

- ``scan_candidates``：在目录中为缺失素材找候选（文件名 → 大小 →
  内容短哈希三级因子）；
- 唯一候选且**名+大小一致** → ``auto_relinkable``（可自动）；
- 同名但大小/哈希不同 → ``same_name_different_content``（必须人工
  确认，绝不自动）；
- 多候选 → 全部人工确认；
- 时间码/卷名/时长等元数据因子由调用方补充进 ``extra_factors``
  （重名异片场景的进一步裁决依据）。

代理/原片重套的兼容验证（分辨率/时间基/源入点）在 ``verify_relink``
——不匹配返回可操作诊断而非静默错链。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _quick_hash(path: str, head_bytes: int = 1 << 20) -> str:
    """内容短哈希（头 1MB + 尾 1MB + 大小；重名异片的廉价判别）。"""
    import hashlib

    h = hashlib.sha256()
    size = os.path.getsize(path)
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        chunk = fh.read(head_bytes)
        h.update(chunk)
        if size > head_bytes * 2:
            fh.seek(-head_bytes, os.SEEK_END)
            h.update(fh.read(head_bytes))
    return h.hexdigest()


@dataclass(frozen=True)
class RelinkCandidate:
    """一个重连候选 + 已验证的因子。"""

    path: str
    basename_match: bool
    size_match: Optional[bool] = None      # None = 原文件大小未知
    content_hash: Optional[str] = None
    extra_factors: Dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> int:
        s = 0
        if self.basename_match:
            s += 1
        if self.size_match:
            s += 2
        return s


@dataclass(frozen=True)
class RelinkMatchResult:
    """匹配结论：auto / manual / none（绝不静默替换）。"""

    candidates: List[RelinkCandidate] = field(default_factory=list)
    original_size: Optional[int] = None
    original_hash: Optional[str] = None

    @property
    def auto_relinkable(self) -> bool:
        """唯一候选且名+大小+哈希全一致（或大小一致且无法取哈希）。"""
        if len(self.candidates) != 1:
            return False
        c = self.candidates[0]
        if not c.basename_match or c.size_match is not True:
            return False
        if self.original_hash and c.content_hash:
            return c.content_hash == self.original_hash
        return True

    @property
    def same_name_different_content(self) -> bool:
        """存在同名但内容不一致的候选——必须人工确认。"""
        if self.original_hash:
            return any(
                c.basename_match and c.content_hash
                and c.content_hash != self.original_hash
                for c in self.candidates)
        return any(
            c.basename_match and c.size_match is False
            for c in self.candidates)


def scan_candidates(
    scan_dir: str,
    missing_basename: str,
    *,
    original_size: Optional[int] = None,
    original_hash: Optional[str] = None,
    extra_factor_fn=None,
) -> RelinkMatchResult:
    """在目录中扫描候选并做名/大小/哈希三级匹配。

    extra_factor_fn(path) -> dict：调用方补充的时间码/卷名/时长等
    裁决因子（写入候选的 extra_factors，供人工确认界面展示）。
    """
    candidates: List[RelinkCandidate] = []
    if not os.path.isdir(scan_dir):
        return RelinkMatchResult(
            candidates=[], original_size=original_size,
            original_hash=original_hash)
    for name in sorted(os.listdir(scan_dir)):
        path = os.path.join(scan_dir, name)
        if not os.path.isfile(path):
            continue
        basename_match = name == missing_basename
        size_match = None
        content_hash = None
        if basename_match:
            try:
                size = os.path.getsize(path)
                size_match = (size == original_size
                              if original_size is not None else None)
                content_hash = _quick_hash(path)
            except OSError:
                pass
        extras = {}
        if extra_factor_fn is not None:
            try:
                extras = dict(extra_factor_fn(path) or {})
            except Exception:  # noqa: BLE001 — 补充因子失败不阻断匹配
                extras = {}
        candidates.append(RelinkCandidate(
            path=path, basename_match=basename_match,
            size_match=size_match, content_hash=content_hash,
            extra_factors=extras))
    candidates.sort(key=lambda c: (-c.score, c.path))
    # 只保留有意义候选：同名，或调用方因子标记的（保守：仅同名参与）
    meaningful = [c for c in candidates if c.basename_match]
    return RelinkMatchResult(
        candidates=meaningful, original_size=original_size,
        original_hash=original_hash)


def verify_relink(
    original_metadata: Optional[Dict[str, Any]],
    new_metadata: Dict[str, Any],
) -> List[str]:
    """代理/原片重套兼容验证——返回可操作诊断（空列表=兼容）。

    校验分辨率/时间基（fps）/总帧数；裁剪与源入点属时间线域（重连不
    改时间线，切点恒定），此处只验证源素材身份兼容。色彩解释经
    F2.7 解释覆盖保持（重连接管 metadata 按 media_replacement 语义
    整域替换）。
    """
    issues: List[str] = []
    if not original_metadata:
        return issues  # 原信息缺失（离线已久）——无法比对，交人工
    for key, label in (("width", "分辨率宽"), ("height", "分辨率高"),
                       ("total_frames", "总帧数")):
        old_v = original_metadata.get(key)
        new_v = new_metadata.get(key)
        if old_v and new_v and int(old_v) != int(new_v):
            issues.append(
                f"{label}不一致（原 {old_v} → 新 {new_v}）：代理/原片"
                "换档需确认时间线切点仍正确")
    old_fps = original_metadata.get("fps")
    new_fps = new_metadata.get("fps")
    if old_fps and new_fps:
        try:
            if abs(float(old_fps) - float(new_fps)) > 1e-3:
                issues.append(
                    f"时间基不一致（原 {old_fps} → 新 {new_fps} fps）："
                    "混合帧率映射将按源 fps 重锚（F2.7 语义），请确认")
        except (TypeError, ValueError):
            pass
    return issues


__all__ = [
    "RelinkCandidate",
    "RelinkMatchResult",
    "scan_candidates",
    "verify_relink",
]
