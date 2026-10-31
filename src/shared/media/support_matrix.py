"""F5.3 · 媒体支持矩阵与 RAW 诊断——按格式的真实能力声明。

原则（计划 §F5.3）：
- **文件扩展名已识别不算验收完成**——每个 AVAILABLE 记录绑定"真实
  原片打开+解码一帧"证据（tests/test_videos/ 真实文件矩阵测试）；
- 承诺外格式（BRAW/R3D：无 SDK、无许可证、无分发）**不伪装支持**：
  导入尝试给可执行诊断——明确缺失项与**转码建议**（ProRes/DNxHR
  母版路径），不静默失败；
- 照片 RAW（LibRaw/rawpy）与 Topos RAW（TPIC 私有编码）**单列**，
  不与相机视频格式混标。

矩阵字段：container（容器打开）/ decode（实际解码）/ raw_controls
（源级白平衡/曝光/去拜耳）/ proxy_seek / export（预览与成片一致导出）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class FormatSupport:
    """一种格式/家族的能力记录（证据绑定为测试锚点）。"""

    key: str
    label: str
    extensions: Tuple[str, ...]
    container: str          # available / unavailable / diagnostic
    decode: str
    raw_controls: str
    proxy_seek: str
    export: str
    evidence: str
    notes: str = ""


#: 按格式家族的能力矩阵（AVAILABLE 项都有真实原片矩阵测试锚点，
#: 见 tests/media/test_f53_media_matrix.py）。
MEDIA_SUPPORT_MATRIX: Tuple[FormatSupport, ...] = (
    FormatSupport(
        key="h264", label="H.264 (AVC)",
        extensions=(".mp4", ".mov"),
        container="available", decode="available",
        raw_controls="not_applicable", proxy_seek="available",
        export="available",
        evidence="tests/test_videos/2K_h264.mp4、4K_H264.mov",
    ),
    FormatSupport(
        key="h265", label="H.265 (HEVC)",
        extensions=(".mov", ".hevc"),
        container="available", decode="available",
        raw_controls="not_applicable", proxy_seek="available",
        export="available",
        evidence="tests/test_videos/2K_H265.mov、4K_H265.mov",
    ),
    FormatSupport(
        key="prores", label="Apple ProRes 422/4444 家族",
        extensions=(".mov",),
        container="available", decode="available",
        raw_controls="not_applicable", proxy_seek="available",
        export="available",
        evidence="tests/test_videos/2K_prores422*.mov、*_4444*.mov（8 个变体）",
        notes="含 Alpha（4444）经 float 链保留",
    ),
    FormatSupport(
        key="dnxhr", label="DNxHR (HQX/444, 10/12-bit)",
        extensions=(".mov", ".mxf"),
        container="available", decode="available",
        raw_controls="not_applicable", proxy_seek="available",
        export="available",
        evidence="tests/test_videos/*DNxHR*.mov（6 个变体）",
    ),
    FormatSupport(
        key="topos_movie", label="Topos 私有编码 (TPIC)",
        extensions=(".mov", ".mp4"),
        container="available", decode="available",
        raw_controls="not_applicable", proxy_seek="available",
        export="available",
        evidence="tests/test_videos/*topos*.mov/mp4（8 个变体）",
        notes="factory topos 后端；FFmpeg 无解码器经 MediaSourceFactory 路由",
    ),
    FormatSupport(
        key="photo_raw", label="照片 RAW（DNG/CR3/ARW/N EF…via LibRaw）",
        extensions=(".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf"),
        container="available", decode="available",
        raw_controls="available",
        proxy_seek="available", export="available",
        evidence="src/features/color/core/image_engine.py rawpy 路径"
                 "（Auto WB 注释锚点）；rawpy 缺失时降级诊断",
        notes="单列：照片 RAW ≠ 相机视频 RAW；源级 WB/曝光/去拜耳经"
              " ColorStackData.raw_params",
    ),
    FormatSupport(
        key="topos_raw", label="Topos RAW（私有 RAW 封装）",
        extensions=(".traw",),
        container="available", decode="available",
        raw_controls="available", proxy_seek="available", export="available",
        evidence="factory topos 后端 raw 分支；TRAW 参数链"
                 "（ClipInstance.raw，TRAW 批 2）",
        notes="单列：与照片 RAW、相机视频 RAW 分别声明",
    ),
    FormatSupport(
        key="braw", label="Blackmagic BRAW",
        extensions=(".braw",),
        container="unavailable", decode="unavailable",
        raw_controls="unavailable", proxy_seek="unavailable",
        export="unavailable",
        evidence="tests/test_videos/A002_01012049_C013.braw（真实原片在库"
                 "但无 SDK）",
        notes="无 Blackmagic RAW SDK（许可证/分发/ABI 未落实）——"
              "不伪装支持；导入给诊断+转码建议",
    ),
    FormatSupport(
        key="r3d", label="RED R3D",
        extensions=(".r3d",),
        container="unavailable", decode="unavailable",
        raw_controls="unavailable", proxy_seek="unavailable",
        export="unavailable",
        evidence="tests/test_videos/A010_A037_0715UN_001.R3D（真实原片在库"
                 "但无 SDK）",
        notes="无 RED SDK——不伪装支持；导入给诊断+转码建议",
    ),
    FormatSupport(
        key="cinemadng", label="CinemaDNG 序列",
        extensions=(".dng",),
        container="diagnostic", decode="diagnostic",
        raw_controls="diagnostic", proxy_seek="diagnostic",
        export="diagnostic",
        evidence="无真实序列样本在库——按计划不承诺",
        notes="缺真实目标相机/版本素材；照片 RAW 单帧可用但序列导入"
              "未建设（F5.4 图像序列链）",
    ),
)

_MATRIX_BY_EXT: Dict[str, FormatSupport] = {
    ext: entry
    for entry in MEDIA_SUPPORT_MATRIX
    for ext in entry.extensions
}


def support_for_path(path: str) -> Optional[FormatSupport]:
    """按扩展名查矩阵记录（Photo RAW 扩展名命中 photo_raw 而非 cinemadng
    ——按文件形态区分由 probe 的目录/多帧判定，此处返回首个命中）。"""
    lower = str(path).lower()
    # 单列优先级：topos_raw > photo_raw > 家族（.dng 双归属按目录形态
    # 无法在此判定——返回 photo_raw，序列诊断由 probe_media_support）
    for entry in MEDIA_SUPPORT_MATRIX:
        if entry.key in ("cinemadng",):
            continue
        if any(lower.endswith(ext) for ext in entry.extensions):
            if entry.key in ("h264", "h265", "prores", "dnxhr",
                             "topos_movie"):
                continue
            return entry
    for entry in MEDIA_SUPPORT_MATRIX:
        if entry.key != "cinemadng":
            continue
        if any(lower.endswith(ext) for ext in entry.extensions):
            return entry
    for entry in MEDIA_SUPPORT_MATRIX:
        if any(lower.endswith(ext) for ext in entry.extensions):
            return entry
    return None


_TRANSCODE_ADVICE = (
    "转码建议：用 DaVinci Resolve / RAW SDK 主机应用转 ProRes 422 HQ "
    "或 DNxHR HQX 母版后导入（保留最大余量的源级调整请在转码时完成）。"
)


def diagnose_media_path(path: str) -> Dict[str, Any]:
    """导入前诊断：不可用格式给缺失项+转码建议，不静默失败。"""
    entry = support_for_path(path)
    if entry is None:
        return {
            "path": str(path), "format": None,
            "status": "unknown_extension",
            "actionable": False,
            "message": "未识别的媒体扩展名——按通用 FFmpeg 路径尝试打开。",
        }
    if entry.container == "available":
        return {
            "path": str(path), "format": entry.key,
            "status": "supported",
            "actionable": False,
            "message": f"{entry.label}：打开/解码/代理/导出可用。",
        }
    missing = []
    if entry.key == "braw":
        missing.append("Blackmagic RAW SDK（许可证/分发/平台 ABI 未落实）")
    elif entry.key == "r3d":
        missing.append("RED R3D SDK（许可证/分发未落实）")
    elif entry.key == "cinemadng":
        missing.append("CinemaDNG 序列导入链（F5.4 图像序列）")
    return {
        "path": str(path), "format": entry.key,
        "status": "sdk_missing",
        "actionable": True,
        "missing": missing,
        "message": f"{entry.label} 暂不支持：{'; '.join(missing)}。"
                   + _TRANSCODE_ADVICE,
    }


def matrix_to_payload() -> list:
    """矩阵 → capability_registry 载荷（evidence/notes 全量声明）。"""
    return [
        {
            "key": e.key, "label": e.label,
            "extensions": list(e.extensions),
            "container": e.container, "decode": e.decode,
            "raw_controls": e.raw_controls,
            "proxy_seek": e.proxy_seek, "export": e.export,
            "evidence": e.evidence, "notes": e.notes,
        }
        for e in MEDIA_SUPPORT_MATRIX
    ]


__all__ = [
    "FormatSupport",
    "MEDIA_SUPPORT_MATRIX",
    "support_for_path",
    "diagnose_media_path",
    "matrix_to_payload",
]
