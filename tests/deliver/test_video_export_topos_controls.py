"""R7 交付面收口：Topos 专属导出控件（pix_fmt 选择器 + Alpha 预算策略）。

ADR-C020（444 档 pix_fmt 显式选择）与 ADR-C014 C-114（alpha_budget_policy
UI 授权开关）的 UI 接线测试——档位格式域来自 topos_profiles 注册表
（单一真相源，O5），选择经 VideoConfig → to_timeline_export_config →
TimelineExportConfig 全链透传，非 Topos codec 不得污染 pixel_format。

链路语义（编码端已由 O1/O2 覆盖）在此只验 UI/配置层：
- 选择器可见性与档位格式域一致；
- 选择值写入 job.video_config.pixel_format / alpha_budget_policy；
- to_timeline_export_config 透传（Topos）/ 置空（非 Topos，防 yuv420p
  默认值闯入编码域被拒）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication

from src.features.deliver.ui.video_export_settings_panel import (
    VideoExportSettingsPanel,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _select_codec(panel: VideoExportSettingsPanel, codec: str) -> None:
    # topos/ProRes 4444 等仅挂 mov 容器——combo 按容器过滤，先切容器再选
    if panel._codec_combo.findData(codec) < 0:
        ci = panel._container_combo.findData("mov")
        assert ci >= 0, "mov 容器不在容器列表"
        panel._container_combo.setCurrentIndex(ci)
    idx = panel._codec_combo.findData(codec)
    assert idx >= 0, f"codec {codec} 不在目录中"
    panel._codec_combo.setCurrentIndex(idx)


def _pix_fmt_choices(panel: VideoExportSettingsPanel):
    combo = panel._pix_fmt_combo
    return [combo.itemData(i) for i in range(combo.count())]


class TestToposPixFmtSelector:
    def test_selector_visible_only_for_topos_codecs(self, qapp):
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "h264")
        assert panel._pix_fmt_combo.isHidden()
        _select_codec(panel, "topos_hq")
        assert not panel._pix_fmt_combo.isHidden()

    def test_choices_match_tier_format_domain(self, qapp):
        """档位格式域 = topos_profiles format_bases × bit_depths（单一真相源）。"""
        # 2026-09-17：HQ 档补齐 16-bit 位深（format_bases × bit_depths
        # 单一真相源扩展），期望域随之更新。
        cases = {
            "topos_proxy": ["yuv422p10le"],
            "topos_lt": ["yuv422p10le"],
            "topos": ["yuv422p10le"],
            "topos_hq": ["yuv422p10le", "yuv422p12le", "yuv422p16le",
                         "yuv444p10le", "yuv444p12le", "yuv444p16le",
                         "gbrp10le", "gbrp12le", "gbrp16le"],
            "topos_4444": ["yuv444p10le", "yuv444p12le", "yuv444p16le",
                             "gbrp10le", "gbrp12le", "gbrp16le"],
            "topos_4444xq": ["yuv444p12le", "gbrp12le"],
        }
        for codec, expected in cases.items():
            panel = VideoExportSettingsPanel()
            _select_codec(panel, codec)
            assert _pix_fmt_choices(panel) == expected, codec

    def test_alpha_checked_adds_explicit_alpha_depth_variants(self, qapp):
        """勾 Alpha 后额外给出 a{8,10,12,16} 显式格式契约（O1 域内名字）。"""
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "topos_hq")
        panel._alpha_check.setChecked(True)
        choices = _pix_fmt_choices(panel)
        for extra in ("topos_yuva444p10a8", "topos_yuva444p10a16",
                      "topos_gbrap12a12", "topos_yuva422p12a10"):
            assert extra in choices, extra
        # 未勾 Alpha 时不得出现显式 alpha 变体（has_alpha 与格式互斥混乱源）
        panel._alpha_check.setChecked(False)
        assert not any("a8" in c or "a16" in c for c in _pix_fmt_choices(panel))

    def test_default_selection_matches_tier_default(self, qapp):
        for codec, default in (("topos_proxy", "yuv422p10le"),
                               ("topos_4444", "yuv444p10le"),
                               ("topos_4444xq", "yuv444p12le")):
            panel = VideoExportSettingsPanel()
            _select_codec(panel, codec)
            assert panel._pix_fmt_combo.currentData() == default, codec

    def test_selected_pix_fmt_flows_to_timeline_config(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_revision=1,
                          project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel.set_active_timeline("t1")
        panel._output_dir_edit.setText("/tmp")
        _select_codec(panel, "topos_4444")
        idx = panel._pix_fmt_combo.findData("gbrp12le")
        assert idx >= 0
        panel._pix_fmt_combo.setCurrentIndex(idx)

        jobs, _ = panel.build_jobs(preflight=False)
        assert jobs[0].video_config.pixel_format == "gbrp12le"
        tl = jobs[0].to_timeline_export_config()
        assert tl.pixel_format == "gbrp12le"

    def test_non_topos_codec_leaves_pixel_format_unoverridden(self, qapp):
        """非 Topos codec：VideoConfig.pixel_format 默认 yuv420p 不得透传
        （TimelineExportConfig.pixel_format 空串 = 用 codec_map 档位默认）。"""
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_revision=1,
                          project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel.set_active_timeline("t1")
        panel._output_dir_edit.setText("/tmp")
        _select_codec(panel, "h264")

        jobs, _ = panel.build_jobs(preflight=False)
        tl = jobs[0].to_timeline_export_config()
        assert tl.pixel_format == ""


class TestAlphaBudgetPolicyControl:
    def test_policy_choices_and_default(self, qapp):
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "topos_hq")
        combo = panel._alpha_budget_policy_combo
        data = [combo.itemData(i) for i in range(combo.count())]
        assert data == ["record", "error", "continue"]
        assert combo.currentData() == "record"

    def test_policy_visible_only_for_topos(self, qapp):
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "prores_4444")
        assert panel._alpha_budget_policy_combo.isHidden()
        _select_codec(panel, "topos")
        assert not panel._alpha_budget_policy_combo.isHidden()

    def test_policy_flows_to_timeline_config(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_revision=1,
                          project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel.set_active_timeline("t1")
        panel._output_dir_edit.setText("/tmp")
        _select_codec(panel, "topos")
        panel._alpha_budget_policy_combo.setCurrentIndex(
            panel._alpha_budget_policy_combo.findData("error"))

        jobs, _ = panel.build_jobs(preflight=False)
        assert jobs[0].video_config.alpha_budget_policy == "error"
        tl = jobs[0].to_timeline_export_config()
        assert tl.alpha_budget_policy == "error"


class TestHevcBitDepthSelector:
    """HEVC 8/10-bit 交付档选择器（2026-09-16 平面直通扩展）。

    复用 Topos 的 pix_fmt 选择器承载 HEVC 位深档；选择经
    VideoConfig.pixel_format → _hevc_pixel_format_override →
    TimelineExportConfig.pixel_format 透传，profile main/main10 与位深
    档双向联动。
    """

    def test_selector_visible_for_hevc_with_two_tiers(self, qapp):
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "hevc")
        assert not panel._pix_fmt_combo.isHidden()
        assert _pix_fmt_choices(panel) == ["yuv420p", "yuv420p10le"]
        # 默认 8-bit 平面直通档 + profile main 对齐
        assert panel._pix_fmt_combo.currentData() == "yuv420p"
        assert panel._profile_combo.currentData() == "main"

    def test_selection_writes_video_config_pixel_format(self, qapp):
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "hevc")
        idx = panel._pix_fmt_combo.findData("yuv420p10le")
        assert idx >= 0
        panel._pix_fmt_combo.setCurrentIndex(idx)
        vc = panel._read_video_config_from_draft()
        assert vc.pixel_format == "yuv420p10le"

    def test_profile_and_bit_depth_stay_aligned(self, qapp):
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "hevc")
        # 选 10-bit 档 → profile 联动 main10
        panel._pix_fmt_combo.setCurrentIndex(
            panel._pix_fmt_combo.findData("yuv420p10le"))
        assert panel._profile_combo.currentData() == "main10"
        # 反向：profile 选 main → 位深档回 8-bit
        panel._profile_combo.setCurrentIndex(
            panel._profile_combo.findData("main"))
        assert panel._pix_fmt_combo.currentData() == "yuv420p"

    def test_other_codecs_keep_placeholder_pixel_format(self, qapp):
        """h264 不显示位深档；占位 pixel_format 由 job 层置空不透传。"""
        panel = VideoExportSettingsPanel()
        _select_codec(panel, "h264")
        assert panel._pix_fmt_combo.isHidden()
        vc = panel._read_video_config_from_draft()
        assert vc.pixel_format == "yuv420p"
