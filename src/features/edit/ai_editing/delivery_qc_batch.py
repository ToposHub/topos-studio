"""M21.2b · 逐版本 LUFS/字幕 QC 批量测量（多版本交付的质量侧）。

对多版本编译产物逐文件跑 measure_delivery_quality（LUFS/静音/黑帧
等实测）+ 逐版本字幕 QC（复用既有 inspect 门槛：行长/速率/重叠/
越界），汇总成 per-version 报告——未测字段保持 None（不冒充 0）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence


logger = logging.getLogger(__name__)


def measure_version_lufs(
    output_paths: Sequence[str],
    *,
    expected_lufs: Optional[float] = None,
    max_silence_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """逐版本 LUFS/静音/黑帧实测汇总。

    Returns:
        {path: {"input": DeliveryQualityInput|None, "error": str}}
        ——探测失败 path 的 input=None + error（≠通过）。
    """
    from src.features.edit.ai_editing.delivery_quality_probe import (
        measure_delivery_quality,
    )
    report: Dict[str, Any] = {}
    for path in output_paths:
        try:
            inp = measure_delivery_quality(
                path,
                expected_lufs=expected_lufs,
                max_silence_seconds=max_silence_seconds)
        except Exception as exc:  # noqa: BLE001 — 探测异常 ≠ 通过
            inp = None
            logger.warning("version LUFS measure failed for %s: %s",
                           path, exc)
        entry: Dict[str, Any] = {
            "input": inp,
            "error": "" if inp is not None else "测量失败（文件不可读？）",
        }
        if inp is not None and inp.integrated_lufs is not None:
            entry["integrated_lufs"] = inp.integrated_lufs
            if expected_lufs is not None:
                entry["lufs_deviation"] = round(
                    inp.integrated_lufs - expected_lufs, 2)
        report[path] = entry
    return report


def subtitle_qc_summary(
    inspect_reports: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """逐版本字幕 inspect 汇总（复用既有 inspect 判定字段）。

    Args:
        inspect_reports: 每版本的 inspect 输出（含 violations/error 级
            计数字段——调用方取自既有 subtitle inspect 入口）。
    """
    total_blocking = 0
    per_version = []
    for i, report in enumerate(inspect_reports):
        blocking = int(report.get("blocking", 0) or 0)
        total_blocking += blocking
        per_version.append({
            "version_index": i,
            "blocking": blocking,
            "warning": int(report.get("warning", 0) or 0),
            "ok": blocking == 0,
        })
    return {
        "all_ok": total_blocking == 0,
        "total_blocking": total_blocking,
        "versions": per_version,
    }


__all__ = ["measure_version_lufs", "subtitle_qc_summary"]
