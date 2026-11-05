"""Tests for the structured color-space descriptor and color transform provider.

Covers the color-management MVP core:
  - ColorSpaceDescriptor construction, serialization, OCIO name mapping.
  - sRGB vs Rec.709 transfer functions (the image-vs-video distinction).
  - full↔limited range handling.
  - smart-default descriptor selection.
  - descriptor_from_config preference resolution.
"""
import numpy as np
import pytest

from src.shared.color.color_space_descriptor import (
    ColorSpaceDescriptor,
    LINEAR_SCENE,
    SRGB_IMAGE,
    REC709_VIDEO,
    descriptor_for_source_kind,
    descriptor_from_config,
)
from src.shared.color.types import ColorPrimaries, ColorTransfer, ColorRange
from src.shared.color.color_transform_provider import (
    BuiltinColorProvider,
    get_color_provider,
    transform_color,
)


class TestDescriptor:
    def test_presets_share_primaries_differ_in_transfer_range(self):
        """sRGB and Rec.709 share BT.709 primaries but differ in gamma+range."""
        assert SRGB_IMAGE.primaries == REC709_VIDEO.primaries == ColorPrimaries.BT709
        assert SRGB_IMAGE.transfer == ColorTransfer.SRGB
        assert REC709_VIDEO.transfer == ColorTransfer.BT709
        assert SRGB_IMAGE.transfer != REC709_VIDEO.transfer
        assert SRGB_IMAGE.range == ColorRange.FULL
        assert REC709_VIDEO.range == ColorRange.LIMITED

    def test_serialization_roundtrip(self):
        for d in (LINEAR_SCENE, SRGB_IMAGE, REC709_VIDEO):
            assert ColorSpaceDescriptor.from_dict(d.to_dict()) == d

    def test_serialization_missing_keys_defaults_to_linear(self):
        assert ColorSpaceDescriptor.from_dict(None) == LINEAR_SCENE
        assert ColorSpaceDescriptor.from_dict({}) == LINEAR_SCENE

    def test_ocio_name_mapping(self):
        assert SRGB_IMAGE.to_ocio_name() == "srgb"
        assert REC709_VIDEO.to_ocio_name() == "rec709"
        assert LINEAR_SCENE.to_ocio_name() == "lin"
        # reverse — OCIO names encode primaries+transfer; the conventional
        # range per space is recorded in _OCIO_SPACE_RANGE so the round-trip
        # descriptor preserves LIMITED for video spaces (rec709) instead of
        # collapsing everything to FULL.
        from_srgb = ColorSpaceDescriptor.from_ocio_name("srgb")
        assert from_srgb.primaries == SRGB_IMAGE.primaries
        assert from_srgb.transfer == SRGB_IMAGE.transfer
        from_rec = ColorSpaceDescriptor.from_ocio_name("rec709")
        assert from_rec.primaries == REC709_VIDEO.primaries
        assert from_rec.transfer == REC709_VIDEO.transfer

    def test_ocio_name_range_roundtrip_preserves_limited(self):
        """M-6: OCIO round-trip must keep LIMITED for rec709/bt2020_* spaces.

        OCIO space names don't encode range, but video/HDR spaces are
        conventionally limited-range; the descriptor round-trip should
        preserve that rather than dropping to FULL.
        """
        from src.shared.color.color_space_descriptor import HDR10_VIDEO, HLG_VIDEO
        assert ColorSpaceDescriptor.from_ocio_name("rec709").range == ColorRange.LIMITED
        assert ColorSpaceDescriptor.from_ocio_name("bt2020_pq").range == ColorRange.LIMITED
        assert ColorSpaceDescriptor.from_ocio_name("bt2020_hlg").range == ColorRange.LIMITED
        # Still-image / working spaces stay FULL.
        assert ColorSpaceDescriptor.from_ocio_name("srgb").range == ColorRange.FULL
        assert ColorSpaceDescriptor.from_ocio_name("display_p3").range == ColorRange.FULL
        assert ColorSpaceDescriptor.from_ocio_name("lin").range == ColorRange.FULL
        # HDR10/HLG presets survive a full dict round-trip (range included).
        for d in (HDR10_VIDEO, HLG_VIDEO):
            assert ColorSpaceDescriptor.from_dict(d.to_dict()) == d

    def test_smart_default(self):
        assert descriptor_for_source_kind(is_video=True) == REC709_VIDEO
        assert descriptor_for_source_kind(is_video=False) == SRGB_IMAGE


class _StubConfig:
    def __init__(self, d):
        self.d = d
    def get(self, key, default=None):
        return self.d.get(key, default)


@pytest.fixture(autouse=True)
def _isolate_project_color_overrides():
    """快照并清空进程级工程色彩覆盖层，测试后还原（同 test_fix_wd15g83_aud）。

    本文件用 _StubConfig 直传键测 descriptor_from_config；工程加载类测试
    （如 test_color_bake_execution 的 save→reopen 往返）会经 edit_project_io
    把 ConfigManager 默认值写入 _PROJECT_COLOR_OVERRIDES 且不清理，若不隔离
    会按键遮蔽 _StubConfig（_cfg 先查覆盖层），导致顺序依赖的假失败。
    """
    from src.shared.color.color_space_descriptor import (
        clear_project_color_overrides,
        get_project_color_overrides,
        set_project_color_overrides,
    )
    saved = get_project_color_overrides()
    clear_project_color_overrides()
    yield
    set_project_color_overrides(saved)


class TestDescriptorFromConfig:
    def test_explicit_prefs_win(self):
        cfg = _StubConfig({"color.preview.image_transfer": "iec61966_2_1"})
        d = descriptor_from_config(cfg, "preview", "image", is_video=False)
        assert d == SRGB_IMAGE

    def test_missing_keys_fall_back_to_smart_default(self):
        cfg = _StubConfig({})
        assert descriptor_from_config(cfg, "output", "timeline", is_video=True) == REC709_VIDEO
        assert descriptor_from_config(cfg, "output", "image", is_video=False) == SRGB_IMAGE


class TestBuiltinTransfer:
    """The builtin provider must distinguish sRGB from Rec.709."""

    def setup_method(self):
        # OCIO is typically not installed in CI, so the builtin provider is
        # the active one. If OCIO is present, these tests still hold because
        # the curves are standard.
        self.provider = get_color_provider(None)

    def test_rec709_darker_than_srgb_at_midtone(self):
        """Video (Rec.709) has higher effective contrast than sRGB stills."""
        lin = np.full((4, 4, 3), 0.5, dtype=np.float32)
        srgb = self.provider.transform(lin, LINEAR_SCENE, SRGB_IMAGE)
        rec709 = self.provider.transform(lin, LINEAR_SCENE, REC709_VIDEO)
        assert abs(srgb[0, 0, 0] - rec709[0, 0, 0]) > 0.01
        # Rec.709 produces a darker (lower) encoded value at midtone.
        assert rec709[0, 0, 0] < srgb[0, 0, 0]

    def test_srgb_roundtrip(self):
        lin = np.full((2, 2, 3), 0.5, dtype=np.float32)
        back = self.provider.transform(
            self.provider.transform(lin, LINEAR_SCENE, SRGB_IMAGE),
            SRGB_IMAGE, LINEAR_SCENE,
        )
        assert np.allclose(back, 0.5, atol=1e-3)

    def test_rec709_roundtrip(self):
        for v in (0.0, 0.018, 0.3, 0.5, 0.9, 1.0):
            lin = np.full((1, 1, 3), v, dtype=np.float32)
            back = self.provider.transform(
                self.provider.transform(lin, LINEAR_SCENE, REC709_VIDEO),
                REC709_VIDEO, LINEAR_SCENE,
            )
            assert abs(back[0, 0, 0] - v) < 1e-3, f"Rec.709 round-trip at {v}"

    def test_src_equals_dst_is_noop(self):
        lin = np.full((2, 2, 3), 0.5, dtype=np.float32)
        assert np.array_equal(
            self.provider.transform(lin, SRGB_IMAGE, SRGB_IMAGE), lin
        )

    def test_convenience_transform_color(self):
        lin = np.full((2, 2, 3), 0.5, dtype=np.float32)
        a = self.provider.transform(lin, LINEAR_SCENE, SRGB_IMAGE)
        b = transform_color(lin, LINEAR_SCENE, SRGB_IMAGE)
        assert np.allclose(a, b)


class TestRangeHandling:
    """Full↔limited range conversion around the transfer step."""

    def setup_method(self):
        self.provider = get_color_provider(None)

    def test_limited_range_compresses(self):
        """linear→REC709(limited) produces a value inside the legal band."""
        lin = np.full((1, 1, 3), 0.5, dtype=np.float32)
        out = self.provider.transform(lin, LINEAR_SCENE, REC709_VIDEO)
        # Rec.709 limited range encodes into [16/255, 235/255] ≈ [0.063, 0.922].
        assert 0.06 < out[0, 0, 0] < 0.93

    def test_limited_roundtrip(self):
        lin = np.full((1, 1, 3), 0.5, dtype=np.float32)
        back = self.provider.transform(
            self.provider.transform(lin, LINEAR_SCENE, REC709_VIDEO),
            REC709_VIDEO, LINEAR_SCENE,
        )
        assert abs(back[0, 0, 0] - 0.5) < 5e-2  # range round-trip is lossier


class TestPreferenceUiRuntimeAlignment:
    """P0-COLOR-04：偏好页暴露的键与 descriptor_from_config 读取的键对齐。"""

    def setup_method(self):
        # 工程级覆盖层是进程级全局（_PROJECT_COLOR_OVERRIDES），且在
        # descriptor_from_config 的 _cfg 中优先于传入 config。先前的
        # load_project/convert 往返测试会把本机用户全局配置注入该层，
        # 掩盖这里的 _StubConfig。测试前后清空，恢复全局偏好语义。
        from src.shared.color import color_space_descriptor as csd
        csd.clear_project_color_overrides()
        self._csd = csd

    def teardown_method(self):
        self._csd.clear_project_color_overrides()

    def test_primaries_and_range_keys_resolve(self):
        """primaries/_range 键（偏好页新增入口）能被运行时正确解析。"""
        cfg = _StubConfig({
            "color.output.timeline_primaries": "bt2020",
            "color.output.timeline_transfer": "smpte2084",
            "color.output.timeline_range": "limited",
        })
        d = descriptor_from_config(cfg, "output", "timeline", is_video=True)
        assert d.primaries == ColorPrimaries.BT2020
        assert d.transfer == ColorTransfer.PQ
        assert d.range == ColorRange.LIMITED

    def test_hdr10_descriptor_ocio_name_resolves(self):
        """HDR10 组合解析出有效 OCIO 名称（不静默回退）。"""
        cfg = _StubConfig({
            "color.output.timeline_primaries": "bt2020",
            "color.output.timeline_transfer": "smpte2084",
            "color.output.timeline_range": "limited",
        })
        d = descriptor_from_config(cfg, "output", "timeline", is_video=True)
        assert d.to_ocio_name() == "bt2020_pq"

    def test_display_p3_full_roundtrip(self):
        cfg = _StubConfig({
            "color.preview.image_primaries": "smpte432",
            "color.preview.image_transfer": "iec61966_2_1",
            "color.preview.image_range": "full",
        })
        d = descriptor_from_config(cfg, "preview", "image", is_video=False)
        assert d.primaries == ColorPrimaries.P3_DISPLAY
        assert d.transfer == ColorTransfer.SRGB
        assert d.range == ColorRange.FULL

    def test_available_transfers_includes_hdr(self):
        """能力声明与 UI 一致：PQ/HLG 均在可选传输函数中。"""
        from src.shared.color.color_space_descriptor import available_transfers
        transfers = available_transfers()
        assert ColorTransfer.PQ in transfers
        assert ColorTransfer.HLG in transfers


# ---------------------------------------------------------------------------
# P0-COLOR-04 复查：工程级色彩管理覆盖层
# ---------------------------------------------------------------------------

class TestProjectColorOverrides:
    CFG = {
        "color.preview.timeline_primaries": "bt709",
        "color.preview.timeline_transfer": "bt709",
        "color.preview.timeline_range": "limited",
    }

    def setup_method(self):
        from src.shared.color import color_space_descriptor as csd
        csd.clear_project_color_overrides()
        self._csd = csd

    def teardown_method(self):
        self._csd.clear_project_color_overrides()

    def test_overrides_take_precedence_over_global_prefs(self):
        csd = self._csd
        csd.set_project_color_overrides({
            "color.preview.timeline_primaries": "bt2020",
            "color.preview.timeline_transfer": "smpte2084",
            "color.preview.timeline_range": "limited",
        })
        d = csd.descriptor_from_config(self.CFG, "preview", "timeline", True)
        assert d.primaries is ColorPrimaries.BT2020
        assert d.transfer is ColorTransfer.PQ

    def test_clear_restores_global_preference(self):
        csd = self._csd
        csd.set_project_color_overrides({
            "color.preview.timeline_primaries": "bt2020",
            "color.preview.timeline_transfer": "smpte2084",
        })
        csd.clear_project_color_overrides()
        d = csd.descriptor_from_config(self.CFG, "preview", "timeline", True)
        assert d.primaries is ColorPrimaries.BT709

    def test_empty_overrides_fall_back_to_smart_default(self):
        csd = self._csd
        csd.set_project_color_overrides({})  # 工程无该键 → 回退
        d = csd.descriptor_from_config({}, "preview", "timeline", True)
        # 无全局键且无覆盖 → smart default（video → Rec.709）
        assert d is csd.REC709_VIDEO

    def test_partial_overrides_need_primaries_and_transfer(self):
        csd = self._csd
        # 只有 range 没有 primaries/transfer → 仍走 smart default
        csd.set_project_color_overrides({
            "color.preview.timeline_range": "full"})
        d = csd.descriptor_from_config({}, "preview", "timeline", True)
        assert d is csd.REC709_VIDEO

    def test_collect_color_management_settings_reads_config(self, monkeypatch):
        csd = self._csd
        import src.shared.config.config_manager as cm_mod

        class _FakeCM:
            def __init__(self, *a, **k): pass
            def get(self, key):
                return {"color.preview.timeline_transfer": "pq"}.get(key)
        monkeypatch.setattr(cm_mod, "ConfigManager", _FakeCM)
        values = csd.collect_color_management_settings()
        assert values == {"color.preview.timeline_transfer": "pq"}

    def test_load_side_conversion_applies_overrides(self):
        """convert_topos_project_to_edit_project 注入工程设定到覆盖层。"""
        from src.shared.project.topos_project import ToposProject
        from src.features.edit.core.edit_project_io import (
            convert_topos_project_to_edit_project,
        )
        import dataclasses
        base = ToposProject.create(name="t")
        topos = dataclasses.replace(base, settings={
            "color_management": {
                "color.preview.timeline_primaries": "smpte432",
                "color.preview.timeline_transfer": "iec61966_2_1",
            }
        })
        convert_topos_project_to_edit_project(topos, None)
        d = self._csd.descriptor_from_config({}, "preview", "timeline", True)
        assert d.primaries is ColorPrimaries.P3_DISPLAY
        # 无该键的工程 → 清空覆盖层
        topos2 = ToposProject.create(name="t2")  # settings 默认空 dict
        convert_topos_project_to_edit_project(topos2, None)
        assert self._csd.get_project_color_overrides() == {}
