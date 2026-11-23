"""4.4 帧色彩描述：词汇表复用 / 校验 / 共享描述映射（纯逻辑层）。"""

from __future__ import annotations

import pytest

from src.features.color.core.frame_color_description import (
    CHROMA_LOCATIONS,
    ColorFrameDescription,
    DisplayOutputDescription,
    display_output_for,
)
from src.shared.gpu.present_frame import PresentColorInfo


# ── 归一化与校验 ─────────────────────────────────────────────────────────

class TestNormalization:
    def test_yuv_matrix_uses_shared_gpu_frame_vocab(self):
        # 解码器侧整数/别名 → 共享 normalize_color_matrix 词汇
        desc = ColorFrameDescription("nv12", 64, 32, yuv_matrix=9)
        assert desc.yuv_matrix == "bt2020"
        assert ColorFrameDescription(
            "nv12", 64, 32, yuv_matrix="smpte170m").yuv_matrix == "smpte170m"

    def test_yuv_range_normalizes(self):
        assert ColorFrameDescription(
            "nv12", 64, 32, yuv_range="tv").yuv_range == "limited"
        assert ColorFrameDescription(
            "nv12", 64, 32, yuv_range=1).yuv_range == "full"

    def test_transfer_alias_and_unknown(self):
        assert ColorFrameDescription(
            "nv12", 64, 32, transfer="smpte2084").transfer == "pq"
        with pytest.raises(ValueError, match="transfer"):
            ColorFrameDescription("nv12", 64, 32, transfer="gamma28")

    def test_primaries_alias_and_unknown(self):
        assert ColorFrameDescription(
            "p010", 64, 32, bit_depth=10, primaries="rec2020").primaries \
            == "bt2020"
        with pytest.raises(ValueError, match="primaries"):
            ColorFrameDescription("nv12", 64, 32, primaries="acescc")

    def test_chroma_location_default_left(self):
        # NV12 解码默认 = MPEG-2 共位左（与 GL gid>>1 寻址一致）
        assert ColorFrameDescription("nv12", 64, 32).chroma_location == "left"
        assert ColorFrameDescription(
            "nv12", 64, 32, chroma_location="center").chroma_location \
            == "center"
        with pytest.raises(ValueError, match="色度采样位置"):
            ColorFrameDescription("nv12", 64, 32, chroma_location="diagonal")

    def test_bit_depth_consistency_enforced(self):
        with pytest.raises(ValueError, match="位深"):
            ColorFrameDescription("p010", 64, 32, transfer="pq")
        with pytest.raises(ValueError, match="位深"):
            ColorFrameDescription("nv12", 64, 32, bit_depth=10)
        assert ColorFrameDescription(
            "p010", 64, 32, bit_depth=10).bit_depth == 10

    def test_rgba_input_has_no_yuv_semantics(self):
        desc = ColorFrameDescription("rgba16f", 64, 32, bit_depth=16,
                                     yuv_matrix="bt2020",
                                     yuv_range="limited")
        # RGBA 输入的矩阵/range/色度位置不适用：强制中性（不冒充信号语义）
        assert desc.yuv_matrix == "bt709"
        assert desc.yuv_range == "full"
        assert desc.chroma_location == "unknown"
        assert not desc.is_planar_yuv

    def test_unknown_pixel_format_rejected(self):
        with pytest.raises(ValueError, match="输入格式"):
            ColorFrameDescription("yuv444p16", 64, 32, bit_depth=16)

    def test_visible_rect_validation(self):
        ok = ColorFrameDescription("nv12", 64, 32, visible_rect=(8, 4, 48, 28))
        assert ok.visible_rect == (8, 4, 48, 28)
        assert ColorFrameDescription("nv12", 64, 32).visible_rect == ()
        with pytest.raises(ValueError, match="visible_rect"):
            ColorFrameDescription("nv12", 64, 32, visible_rect=(0, 0, 65, 32))


# ── HDR 语义 ─────────────────────────────────────────────────────────────

class TestHdrSemantics:
    def test_is_hdr(self):
        hdr = ColorFrameDescription("p010", 64, 32, bit_depth=10,
                                    transfer="hlg", primaries="bt2020")
        assert hdr.is_hdr
        assert not ColorFrameDescription("nv12", 64, 32).is_hdr

    def test_chroma_size_half(self):
        desc = ColorFrameDescription("nv12", 65, 33)
        assert desc.chroma_size == (33, 17)
        with pytest.raises(ValueError, match="色度平面"):
            _ = ColorFrameDescription(
                "rgba16f", 64, 32, bit_depth=16).chroma_size


# ── 共享描述映射（不建并行结构） ─────────────────────────────────────────

class TestSharedMapping:
    def test_present_color_info_sdr_matches_native_preview_tuple(self):
        # 与 native_metal_preview 的发布四元组一致（full/bt709/bt709/bt709）
        desc = ColorFrameDescription("nv12", 64, 32)
        info = desc.present_color_info("sdr")
        assert isinstance(info, PresentColorInfo)
        assert (info.color_range, info.color_matrix,
                info.color_primaries, info.color_transfer) \
            == ("full", "bt709", "bt709", "bt709")

    def test_present_color_info_hdr_modes(self):
        desc = ColorFrameDescription("p010", 64, 32, bit_depth=10,
                                     transfer="pq", primaries="bt2020")
        info = desc.present_color_info("hdr_pq")
        assert info.color_transfer == "pq"
        assert info.color_primaries == "bt2020"
        assert info.color_matrix in ("bt2020", "bt2020nc")  # 共享词汇表内
        assert desc.present_color_info("edr_linear").color_transfer == "linear"
        with pytest.raises(ValueError, match="展示模式"):
            desc.present_color_info("dolby")

    def test_working_color_context_feeds_demand_key(self):
        from src.features.color.core.color_graph_entry import (
            color_graph_frame_demand_key,
        )
        sdr = ColorFrameDescription("nv12", 64, 32)
        hdr = ColorFrameDescription("p010", 64, 32, bit_depth=10,
                                    transfer="pq", primaries="bt2020")
        base = dict(media_id="m", frame=1, effect_revision=0)
        key_sdr = color_graph_frame_demand_key(
            working_color_context=sdr.working_color_context(), **base)
        key_hdr = color_graph_frame_demand_key(
            working_color_context=hdr.working_color_context(), **base)
        # 色彩语义（输入 transfer/primaries → 工作空间身份）变化键必变
        assert key_sdr != key_hdr
        assert color_graph_frame_demand_key(
            working_color_context=hdr.working_color_context(), **base) == key_hdr


# ── 展示输出描述（施加点唯一） ───────────────────────────────────────────

class TestDisplayOutput:
    def test_sdr_input_passthrough(self):
        # SDR 编码信号直通（F5.1 语义表）——不做线性化往返
        desc = ColorFrameDescription("nv12", 64, 32)
        assert display_output_for(desc).kind == "sdr_passthrough"

    def test_hdr_input_display_matrix(self):
        hdr = ColorFrameDescription("p010", 64, 32, bit_depth=10,
                                    transfer="pq", primaries="bt2020")
        assert display_output_for(hdr, hdr_display=False).kind \
            == "hdr_sdr_tonemap"
        assert display_output_for(hdr, hdr_display=True).kind == "hdr_pq"
        assert display_output_for(
            hdr, hdr_display=True, edr_headroom=4.0).kind == "edr_linear"
        # EDR 能力不足时回落 PQ，不冒充 EDR
        assert display_output_for(
            hdr, hdr_display=True, edr_headroom=1.0).kind == "hdr_pq"

    def test_display_output_validation(self):
        with pytest.raises(ValueError, match="展示输出类型"):
            DisplayOutputDescription("dolby_vision")
        assert DisplayOutputDescription("hdr_pq").is_hdr_display()
        with pytest.raises(ValueError, match="edr_headroom"):
            DisplayOutputDescription("edr_linear", edr_headroom=0.5)


# ── 序列化 ───────────────────────────────────────────────────────────────

class TestSerialization:
    def test_roundtrip(self):
        desc = ColorFrameDescription(
            "p010", 64, 32, bit_depth=10, transfer="pq", primaries="bt2020",
            chroma_location="center", visible_rect=(0, 0, 32, 16))
        clone = ColorFrameDescription.from_dict(desc.to_dict())
        assert clone == desc

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError, match="未知字段"):
            ColorFrameDescription.from_dict({"dolby": 1})
