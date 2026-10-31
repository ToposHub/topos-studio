"""Commercial-export gates for generated media provenance (P8/P9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommercialExportCheck:
    allowed: bool
    blocking: tuple[str, ...] = ()


def check_generated_media_for_export(
    generation_bin: Any | None,
    *,
    require_commercial_ready: bool = False,
    required_license_region: str | None = None,
) -> CommercialExportCheck:
    """Block commercial export when generated provenance is missing/uncleared.

    New ``GenerationBin`` reports include a complete audit summary.  Older
    injected report implementations remain supported, but only receive the
    legacy license/commercial-ready checks unless they expose the new fields.
    """
    if not require_commercial_ready:
        return CommercialExportCheck(True)
    if generation_bin is None:
        # No generated-bin means there is no reliable provenance source.  Fail
        # closed only for the explicitly requested commercial-ready mode.
        return CommercialExportCheck(False, ("生成素材 provenance 不可用，无法声明商用就绪",))
    report_fn = getattr(generation_bin, "provenance_report", None)
    if not callable(report_fn):
        return CommercialExportCheck(False, ("生成素材 provenance 接口不可用",))
    try:
        report = report_fn()
    except Exception:
        return CommercialExportCheck(False, ("生成素材 provenance 读取失败",))
    if not isinstance(report, dict):
        return CommercialExportCheck(False, ("生成素材 provenance 格式无效",))
    blocking: list[str] = []
    if report.get("any_license_unknown"):
        blocking.append("存在许可未知的生成素材，不能声明商用就绪")
    if report.get("any_license_pending"):
        blocking.append("存在待清权的生成素材，不能声明商用就绪")
    if "audit_complete" in report and not report.get("audit_complete", False):
        counter_messages = {
            "missing_import_audit_count": "缺少媒体池导入审计",
            "missing_approval_count": "缺少导入批准记录",
            "missing_provider_identity_count": "缺少生成 provider 身份",
            "missing_model_identity_count": "缺少生成模型身份",
            "missing_prompt_hash_count": "缺少输入提示词摘要",
            "missing_license_evidence_count": "缺少许可证据引用",
            "missing_license_check_time_count": "缺少许可证核验时间",
            "license_region_missing_count": "缺少许可证适用区域",
            "safety_blocked_count": "存在内容安全阻断素材",
        }
        for key, message in counter_messages.items():
            if report.get(key, 0):
                blocking.append(f"商业审计不完整：{message}")
    entries = report.get("entries") or ()
    # 外部/持久化 provenance 数据必须验证（AUD-13210）：显式 null 或畸形
    # 集合 fail closed，而不是让 ``for entry in None`` 把整个门禁炸成崩溃。
    if not isinstance(entries, (list, tuple)):
        return CommercialExportCheck(False, ("生成素材 provenance entries 格式无效",))
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not entry.get("commercial_ready", False):
            blocking.append(f"生成素材 {entry.get('asset_id', '')!r} 未通过许可/安全商用门禁")
        if required_license_region:
            provenance = entry.get("provenance")
            region = provenance.get("license_region") if isinstance(provenance, dict) else None
            if region != required_license_region:
                blocking.append(
                    f"生成素材 {entry.get('asset_id', '')!r} 的许可证区域不符合目标区域"
                )
    return CommercialExportCheck(not blocking, tuple(blocking))


__all__ = ["CommercialExportCheck", "check_generated_media_for_export"]
