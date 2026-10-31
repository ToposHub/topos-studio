"""T-P1-70：统一 DeliveryCapability——一次交付的能力/限制单一描述。

所有导出入口（UI/快速导出/AI request_export/CLI/批量 Deliver）共享：
- codec 档位与像素格式事实；
- alpha / audio / 字幕 / HDR 能力（含无音轨 codec 限制）；
- 效果可交付性投影（T-P1-20 统一 EffectExecutionCapability 的投影）。

这不是新的真值源：每一项都从既有实现表/契约解析，未知显式标
unknown 而非猜测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# v1.1 音频轨（container_spec_v1.3 附录 A / ADR-C051）：Topos 容器档
# （mov）已携带单音频轨（ToposVideoEncoder.supports_audio=True），audio
# 需求无需改走其它 codec。仍无音轨的只有逐帧产物：图片序列（无容器）
# 与 GIF（编码器显式跳过音频，timeline_export P1-02）。
# G14：PNG/TIFF 标准序列同样逐帧产物（无音轨）
AUDIO_INCAPABLE_CODECS = frozenset({
    "topos_image_sequence", "gif", "png_sequence", "tiff_sequence",
})

#: 支持 alpha 直通的 codec 家族（ProRes 4444 系 + Topos 4444 系；
#: R20——PNG/TIFF 序列逐帧产物按 rgba/rgba64 直通 alpha，此前误报
#: alpha_capable=False）
ALPHA_CAPABLE_CODECS = frozenset({
    "prores_4444", "prores_4444xq", "topos_4444", "topos_4444xq",
    "png_sequence", "tiff_sequence",
})

_HDR_PIXEL_FORMATS = frozenset({"yuv420p10le", "yuv422p10le", "p010le",
                                "yuv444p10le", "gbrp10le"})


@dataclass(frozen=True)
class DeliveryCapability:
    """一次交付的统一能力描述（JSON-safe）。"""

    codec: str
    pixel_format: str
    container: str
    alpha_capable: bool
    audio_capable: bool
    subtitle_burn_capable: bool
    hdr: bool
    audio_limit: Optional[str]  # 非 None = 该 codec 无音轨的说明
    effects: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "codec": self.codec,
            "pixel_format": self.pixel_format,
            "container": self.container,
            "alpha_capable": self.alpha_capable,
            "audio_capable": self.audio_capable,
            "subtitle_burn_capable": self.subtitle_burn_capable,
            "hdr": self.hdr,
            "audio_limit": self.audio_limit,
            "effects": dict(self.effects),
        }


def describe_delivery_capability(
    config: Any,
    sequence: Optional[Any] = None,
) -> DeliveryCapability:
    """从导出配置解析统一交付能力（T-P1-70 步骤 1）。

    Args:
        config: ``TimelineExportConfig``（或同构：codec/pixel_format/
            has_alpha/include_audio/burn_subtitles 属性）。
        sequence: 可选时间线——提供时 effects 按实际链上效果解析，
            否则给出内建效果与插件策略的通用投影。
    """
    # D10（第六轮审计）：先读 enum 的 value 再 str——此前 str(ExportCodec.GIF)
    # 得 "ExportCodec.GIF"，后续 value 提取拿到的是字符串自身，GIF/ProRes4444
    # 的容器/音频/alpha 能力全部按未知 codec 误报。
    codec_raw = getattr(config, "codec", "") or ""
    codec_value = str(getattr(codec_raw, "value", codec_raw)).strip().lower()
    pix_fmt = str(getattr(config, "pixel_format", "") or "").strip().lower()
    # R20（第七轮审计）：输出形态按事实——GIF/PNG/TIFF 序列此前全部
    # 误报 container=mov（GIF 是 gif 容器；PNG/TIFF 是帧目录交付）。
    if codec_value in ("png_sequence", "tiff_sequence",
                       "topos_image_sequence"):
        container = "sequence"
    elif codec_value == "gif":
        container = "gif"
    elif codec_value.startswith(("topos", "prores")):
        container = "mov"
    elif codec_value in ("h264", "hevc"):
        container = "mp4"
    else:
        container = "mov"

    audio_less = codec_value in AUDIO_INCAPABLE_CODECS
    alpha = codec_value in ALPHA_CAPABLE_CODECS and bool(
        getattr(config, "has_alpha", False))
    audio = (not audio_less) and bool(getattr(config, "include_audio", False))
    hdr = pix_fmt in _HDR_PIXEL_FORMATS
    # 字幕烧录是像素级合成（渲染管线逐帧完成），与容器音轨能力无关——
    # v1.1 起 topos 容器档同样可烧录（旧 video-only 门控随音频轨解冻移除）
    subtitle = bool(getattr(config, "burn_subtitles", False))

    effects: Dict[str, Dict[str, Any]] = {}
    try:
        from src.features.edit.core.timeline_export import (
            ExportPreflightResult,  # noqa: F401 — 类型可读性
        )
        from src.features.shared.effects import (
            EXPORT_EFFECT_TYPES, get_effect_catalog,
            resolve_execution_capability,
        )

        catalog = get_effect_catalog()
        # 音频 DSP 由音频交付路径管辖（无音轨 codec 时该限制已在
        # audio_capable 体现），不进视频效果投影。
        from src.features.edit.core.effect_registry import (
            AUDIO_MIXER_EFFECT_TYPES,
        )

        effect_types = set(EXPORT_EFFECT_TYPES)
        if sequence is not None:
            for track in getattr(sequence, "all_tracks", ()) or ():
                for clip in getattr(track, "clips", ()) or ():
                    for effect in (getattr(clip, "effects", ()) or ()):
                        if getattr(effect, "is_enabled", True):
                            etype = str(getattr(effect, "effect_type", "") or "")
                            if etype and etype not in AUDIO_MIXER_EFFECT_TYPES:
                                effect_types.add(etype)
        for effect_type in sorted(t for t in effect_types if t):
            execution = resolve_execution_capability(
                effect_type, catalog_known=catalog.is_known(effect_type))
            effects[effect_type] = {
                "exportable": execution.exportable,
                "preview_only": execution.preview_only,
                "restriction": execution.delivery_restriction()[0] or None,
            }
    except Exception:  # noqa: BLE001 — 能力求证失败显式标注 unknown
        effects = {"_error": {"exportable": False,
                              "preview_only": False,
                              "restriction": "capability_unknown"}}

    return DeliveryCapability(
        codec=codec_value,
        pixel_format=pix_fmt,
        container=container,
        alpha_capable=alpha,
        audio_capable=audio,
        subtitle_burn_capable=subtitle,
        hdr=hdr,
        audio_limit=("逐帧产物无音频轨（图片序列/GIF 不含音频）"
                     if audio_less else None),
        effects=effects,
    )
