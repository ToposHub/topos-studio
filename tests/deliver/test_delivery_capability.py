"""T-P1-70：统一 Deliver Contract 聚焦测试（批次一）。

覆盖：三档严重度（BLOCKING/DEGRADED/WARNING）语义、DeliveryCapability
统一能力描述（v1.1 音频轨——topos 容器档含音轨、逐帧产物无音轨、
alpha/HDR 门控、效果可交付性投影）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.deliver.core.video_export_preflight import (
    PreflightResult,
    PreflightSeverity,
)


class TestPreflightSeverityTiers:
    def test_degraded_does_not_block_enqueue(self) -> None:
        result = PreflightResult()
        result.add_degraded("gpu.unavailable",
                            "GPU 不可用，将以 CPU 管线交付")
        assert result.ok is True
        assert len(result.degraded) == 1
        assert result.degraded[0].code == "gpu.unavailable"

    def test_blocking_still_blocks(self) -> None:
        result = PreflightResult()
        result.add_degraded("gpu.unavailable", "cpu fallback")
        result.add_blocking("range.invalid", "bad range")
        assert result.ok is False
        assert [i.code for i in result.blocking] == ["range.invalid"]

    def test_warning_is_informational(self) -> None:
        result = PreflightResult()
        result.add_warning("font.subset", "字体子集缺失")
        assert result.ok is True
        assert not result.degraded
        assert len(result.warnings) == 1


class TestDeliveryCapability:
    def _config(self, **overrides):
        base = dict(codec="h264", pixel_format="yuv420p", has_alpha=False,
                    include_audio=True, burn_subtitles=False)
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_topos_container_carries_audio(self) -> None:
        """v1.1（ADR-C051）：topos 容器档带单音轨——audio_capable 且无限制说明。"""
        from src.features.deliver.core.delivery_capability import (
            describe_delivery_capability,
        )

        capability = describe_delivery_capability(self._config(
            codec="topos_proxy", pixel_format="yuv422p10le"))
        d = capability.to_dict()
        assert d["codec"] == "topos_proxy"
        assert d["audio_capable"] is True
        assert not d["audio_limit"]
        assert d["hdr"] is True

    def test_frame_products_are_audio_less(self) -> None:
        """逐帧产物（图片序列/GIF）无容器音轨——限制须显式可见。"""
        from src.features.deliver.core.delivery_capability import (
            describe_delivery_capability,
        )

        for codec in ("topos_image_sequence", "gif"):
            capability = describe_delivery_capability(self._config(
                codec=codec))
            d = capability.to_dict()
            assert d["audio_capable"] is False
            assert d["audio_limit"]

    def test_alpha_requires_capable_codec(self) -> None:
        from src.features.deliver.core.delivery_capability import (
            describe_delivery_capability,
        )

        h264_alpha = describe_delivery_capability(self._config(
            has_alpha=True))
        assert h264_alpha.alpha_capable is False
        prores_alpha = describe_delivery_capability(self._config(
            codec="prores_4444", has_alpha=True))
        assert prores_alpha.alpha_capable is True

    def test_effects_projection_uses_unified_contract(self) -> None:
        from src.features.deliver.core.delivery_capability import (
            describe_delivery_capability,
        )

        capability = describe_delivery_capability(self._config())
        effects = capability.to_dict()["effects"]
        assert effects["blur"]["exportable"] is True
        plugin = effects.get("plugin:highpass_retouch")
        if plugin is not None:
            assert plugin["exportable"] is False
            assert plugin["restriction"] == "plugin_preview_only"

    def test_json_safe_roundtrip(self) -> None:
        import json

        from src.features.deliver.core.delivery_capability import (
            describe_delivery_capability,
        )

        capability = describe_delivery_capability(self._config(
            codec="topos_4444xq", pixel_format="yuv422p10le",
            has_alpha=True))
        payload = json.loads(json.dumps(capability.to_dict()))
        assert payload["codec"] == "topos_4444xq"
        assert payload["hdr"] is True


from src.features.deliver.core.delivery_capability import (
    describe_delivery_capability,
)


class TestD10EnumValueExtraction:
    """第六轮审计 D10：enum 与字符串输入等价，GIF/ProRes4444/序列不再误报。"""

    def _config(self, codec, **kw):
        from types import SimpleNamespace

        return SimpleNamespace(
            codec=codec, pixel_format=kw.get("pix_fmt", ""),
            has_alpha=kw.get("has_alpha", False),
            include_audio=kw.get("include_audio", True),
            burn_subtitles=False)

    def test_enum_and_string_give_same_capability(self):
        from src.features.edit.core.timeline_export import ExportCodec

        for member in (
            ExportCodec.GIF, ExportCodec.PRORES_4444, ExportCodec.H264,
            ExportCodec.PNG_SEQUENCE,
        ):
            by_enum = describe_delivery_capability(self._config(member))
            by_str = describe_delivery_capability(self._config(member.value))
            assert by_enum.codec == by_str.codec == member.value, member
            assert by_enum.container == by_str.container, member
            assert by_enum.audio_capable == by_str.audio_capable, member
            assert by_enum.alpha_capable == by_str.alpha_capable, member

    def test_gif_enum_reports_no_audio(self):
        from src.features.edit.core.timeline_export import ExportCodec

        cap = describe_delivery_capability(
            self._config(ExportCodec.GIF, include_audio=True))
        assert cap.codec == "gif"
        assert not cap.audio_capable, "enum GIF 输入不得误报音频能力"

    def test_prores4444_enum_alpha(self):
        from src.features.edit.core.timeline_export import ExportCodec

        cap = describe_delivery_capability(
            self._config(ExportCodec.PRORES_4444, has_alpha=True))
        assert cap.alpha_capable, "enum ProRes4444 + alpha 不得误报不支持"

    def test_png_tiff_sequence_no_audio(self):
        from src.features.edit.core.timeline_export import ExportCodec

        for member in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE):
            cap = describe_delivery_capability(
                self._config(member, include_audio=True))
            assert not cap.audio_capable, member
