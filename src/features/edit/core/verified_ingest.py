"""F6.5 · 受控校验拷贝（verified ingest 核心）——单目的地复制+整文件校验。

语义（对齐计划）：
- **单目的地受控拷贝 → 源/目标整文件 SHA-256 校验 → 成功登记**——
  快速指纹（F6.2 匹配器）不冒充整文件拷贝完整性；
- **同名异卡/异内容**：目标已有**良好拷贝**（校验通过的同内容）→
  skip_existing_good（不覆盖良好拷贝）；同内容 → 登记 verified；异
  内容 → 冲突条目（人工裁决，绝不覆盖）；
- **逐项报告**：目的盘断开/写入失败/取消 → 可操作状态与原因；取消
  保留已完成部分，未完成项标记 skipped_not_started；
- **未验证文件不能标成功**：只有 source_hash == target_hash 才进
  verified 列表；
- **源卡只读**：全程只读源（复制流），无任何源写操作；
- 保留摄影卡目录结构/卷名/相机/scene/shot metadata：目标路径 =
  dest_root/<相对源卡目录>（目录结构即摄影卡组织）。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

CHUNK = 1 << 20


class _IngestCancelled(Exception):
    """M12.1（S07）：拷贝中途取消（逐 chunk 轮询触发）。"""



def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class IngestItemResult:
    """单文件入库结果（未验证文件绝不进 verified）。"""

    source: str
    target: str
    status: str      # verified / skipped_existing_good / conflict /
                     # failed / skipped_not_started
    detail: str = ""
    sha256: str = ""


@dataclass(frozen=True)
class IngestReport:
    results: Tuple[IngestItemResult, ...] = ()

    @property
    def verified(self) -> List[IngestItemResult]:
        return [r for r in self.results if r.status == "verified"]

    @property
    def conflicts(self) -> List[IngestItemResult]:
        return [r for r in self.results if r.status == "conflict"]

    @property
    def failures(self) -> List[IngestItemResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def all_passed(self) -> bool:
        """全部成功 = 无失败/冲突/取消未跑项——取消过的批次不冒称全过
        （M12.1/S07：取消报告不能因无 failed 就显示全部成功）。"""
        return not self.failures and not self.conflicts and not [
            r for r in self.results
            if r.status.startswith("skipped")]


def ingest_files(
    sources: List[str],
    dest_root: str,
    *,
    relative_to: Optional[str] = None,
    cancelled_fn: Optional[Callable[[], bool]] = None,
    on_item: Optional[Callable[["IngestItemResult"], None]] = None,
) -> IngestReport:
    """受控校验拷贝：sources → dest_root（保留源相对目录结构）。

    Args:
        sources: 源文件绝对路径列表（源卡全程只读）。
        dest_root: 单目的地根目录（自动创建）。
        relative_to: 目录结构保留的基准（缺省各源文件的 dirname——
            单文件平铺到 dest_root）。
        cancelled_fn: 取消钩子（逐 chunk 轮询；已完成部分保留，其余
            skipped_not_started）。
        on_item: 每项结果即产即报（N08——生产入口的逐项 UI 进度；
            worker 线程调用，UI 侧经信号转发）。

    未验证文件不能标成功：校验失败（写入中断/目的盘断开）→ failed
    带原因，目标半成品删除。
    """
    results: List[IngestItemResult] = []
    for src in sources:

        def _emit(result: IngestItemResult) -> None:
            results.append(result)
            if on_item is not None:
                try:
                    on_item(result)
                except Exception:  # noqa: BLE001 — 回调失败不阻断拷贝
                    pass

        if cancelled_fn is not None and cancelled_fn():
            _emit(IngestItemResult(
                source=src, target="", status="skipped_not_started",
                detail="ingest cancelled by caller"))
            continue
        if not os.path.isfile(src):
            _emit(IngestItemResult(
                source=src, target="", status="failed",
                detail="源文件不存在或不完整（读取失败）"))
            continue
        try:
            rel = (os.path.relpath(src, relative_to)
                   if relative_to else os.path.basename(src))
        except ValueError as exc:
            _emit(IngestItemResult(
                source=src, target="", status="failed",
                detail=f"相对路径计算失败：{exc}"))
            continue
        # M12.1（S07）：源在基准目录之外（rel 含 ".." 或绝对）→ 目标
        # 逃逸 dest_root，拒绝该项（不写到目录树外）
        if os.path.isabs(rel) or rel.startswith(".." + os.sep) \
                or rel == ".." or rel.startswith("/"):
            _emit(IngestItemResult(
                source=src, target="", status="failed",
                detail=f"源路径在基准目录之外（{rel}）——拒绝，不写"
                       f"入目的地目录树外"))
            continue
        target = os.path.join(dest_root, rel)
        try:
            os.makedirs(os.path.dirname(target) or dest_root, exist_ok=True)
        except OSError as exc:
            _emit(IngestItemResult(
                source=src, target=target, status="failed",
                detail=f"目的目录创建失败（目的盘断开？）：{exc}"))
            continue

        # M12.1（S07）：源 hash 读失败逐项 failed（源卡中途拔出等），
        # 不再穿透整个批次
        try:
            source_hash = _sha256(src)
        except OSError as exc:
            _emit(IngestItemResult(
                source=src, target=target, status="failed",
                detail=f"源文件读取失败（源卡拔出？）：{exc}"))
            continue
        if os.path.exists(target):
            # 同名：内容一致 = 良好拷贝已存在（不覆盖）；异内容 = 冲突
            try:
                if _sha256(target) == source_hash:
                    _emit(IngestItemResult(
                        source=src, target=target,
                        status="skipped_existing_good",
                        sha256=source_hash))
                    continue
            except OSError:
                pass  # 目标读失败按冲突/失败路径处理
            _emit(IngestItemResult(
                source=src, target=target, status="conflict",
                detail="同名异内容（异卡同名？）——绝不覆盖，人工裁决"))
            continue

        tmp = target + ".topos-partial"
        try:
            with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
                # M12.1（S07）：逐 chunk 轮询取消——大文件拷贝中取消
                # 及时退出（此前只在文件边界轮询）
                while True:
                    if cancelled_fn is not None and cancelled_fn():
                        raise _IngestCancelled()
                    block = fsrc.read(CHUNK)
                    if not block:
                        break
                    fdst.write(block)
            target_hash = _sha256(tmp)
            if target_hash != source_hash:
                os.remove(tmp)
                _emit(IngestItemResult(
                    source=src, target=target, status="failed",
                    detail=f"整文件校验失败（源 {source_hash[:8]} ≠ "
                           f"目标 {target_hash[:8]}）"))
                continue
            os.replace(tmp, target)
            _emit(IngestItemResult(
                source=src, target=target, status="verified",
                sha256=source_hash))
        except _IngestCancelled:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            _emit(IngestItemResult(
                source=src, target=target, status="skipped_not_started",
                detail="ingest cancelled mid-copy（半成品已清理）"))
        except OSError as exc:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            _emit(IngestItemResult(
                source=src, target=target, status="failed",
                detail=f"复制/写入失败（目的盘断开？）：{exc}"))
    return IngestReport(results=tuple(results))


__all__ = ["IngestItemResult", "IngestReport", "ingest_files"]
