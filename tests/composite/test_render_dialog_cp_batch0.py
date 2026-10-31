"""CP-004 / CP-005 渲染对话框回归测试。

CP-004: parallel_frames 暴露到 UI 且进入 get_settings()（worker 把它
        传给 render_video / render_image_sequence 的同名参数）。
CP-005: codec 列表对齐 shared/export/encoder.py 的 VideoCodec 全集；
        CRF 档仅对支持 CRF 的编码显示；ProRes/DNxHR 使用 4:2:2 像素格式。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render.composite_render_output import CompositeMode
from src.features.composite.ui.composite_render_dialog import (
    CompositeRenderDialog,
    _VIDEO_CODECS,
    _container_for_codec,
)


@pytest.fixture()
def video_dialog(qapp):
    dialog = CompositeRenderDialog(
        mode=CompositeMode.VIDEO,
        default_dir="/tmp",
        frame_range=(0, 24),
        fps=24.0,
    )
    yield dialog
    dialog.close()


class TestParallelFramesExposed:
    """CP-004: 并行帧数控件。"""

    def test_settings_contain_parallel_frames(self, video_dialog):
        settings = video_dialog.get_settings()
        assert settings["parallel_frames"] >= 1

    def test_spin_value_flows_to_settings(self, video_dialog):
        video_dialog.parallel_spin.setValue(3)
        assert video_dialog.get_settings()["parallel_frames"] == 3

    def test_default_seeded_from_ctor(self, qapp):
        dialog = CompositeRenderDialog(
            mode=CompositeMode.VIDEO, parallel_frames_default=2)
        try:
            assert dialog.parallel_spin.value() == 2
        finally:
            dialog.close()

    def test_worker_forwards_parallel_frames_to_renderer(self):
        """composite_render_worker.run 必须把 settings.parallel_frames
        传给渲染器（源码级检查，无需 Qt 线程）。"""
        import inspect
        from src.features.composite.render import composite_render_worker
        src = inspect.getsource(composite_render_worker)
        assert src.count("parallel_frames=settings.get(\"parallel_frames\")") == 2, (
            "render_video 与 render_image_sequence 两个调用都应转发 "
            "parallel_frames")


class TestCodecListAlignedWithEngine:
    """CP-005: codec 全列表。"""

    def test_ui_codecs_cover_engine_codec_set(self):
        from src.shared.export.encoder import VideoCodec
        ui_keys = {key for key, *_ in _VIDEO_CODECS}
        engine_keys = {member.value for member in VideoCodec}
        # gif 在本机 PyAV 构建下编码器打不开（复查实测 EINVAL），已移出
        # 对话框；其余引擎 codec 必须全部可达。
        assert engine_keys - {"gif"} <= ui_keys, (
            f"engine codecs missing from dialog: "
            f"{engine_keys - {'gif'} - ui_keys}")

    def test_gif_not_offered(self):
        keys = {key for key, *_ in _VIDEO_CODECS}
        assert "gif" not in keys

    def test_container_for_each_codec(self):
        for key, _label, ext, _crf in _VIDEO_CODECS:
            assert _container_for_codec(key) == ext

    def test_prores_gets_422_pixel_format(self, video_dialog):
        # 选 ProRes（按 userData 找 index）
        idx = video_dialog.codec_combo.findData("prores")
        assert idx >= 0
        video_dialog.codec_combo.setCurrentIndex(idx)
        settings = video_dialog.get_settings()
        assert settings["pix_fmt"] == "yuv422p10le"
        assert "crf" not in settings  # ProRes 无 CRF 语义

    def test_h264_keeps_crf(self, video_dialog):
        idx = video_dialog.codec_combo.findData("h264")
        video_dialog.codec_combo.setCurrentIndex(idx)
        settings = video_dialog.get_settings()
        assert settings["crf"] is not None
        assert settings["pix_fmt"] == "yuv420p"

    def test_quality_row_hidden_for_non_crf_codecs(self, video_dialog):
        idx = video_dialog.codec_combo.findData("prores")
        video_dialog.codec_combo.setCurrentIndex(idx)
        assert not video_dialog._vquality_row_holder.isVisible()
        idx = video_dialog.codec_combo.findData("hevc")
        video_dialog.codec_combo.setCurrentIndex(idx)
        assert video_dialog._vquality_row_holder.isVisibleTo(
            video_dialog._vquality_row_holder.parentWidget())


class TestParallelVideoStageImportContract:
    """LINEAR_SCENE 修复的回归钉（2026-10-14 基准复现）。

    _run_parallel_video_stage 的内嵌 _convert 使用 LINEAR_SCENE，此前只在
    render_video 的局部作用域导入 → 任何 parallel_frames>=2 的视频导出
    在首帧转换即 NameError。AST 级检查：该 stage 函数自己的 import 集
    必须包含该名字。
    """

    def test_stage_imports_linear_scene(self):
        import ast
        import inspect
        from src.features.composite.render.composite_render_output import (
            CompositeRenderOutput,
        )
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(
            CompositeRenderOutput._run_parallel_video_stage)))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(a.name for a in node.names)
        assert "LINEAR_SCENE" in imported, (
            "_run_parallel_video_stage 丢失 LINEAR_SCENE 导入 — "
            "并行视频导出会 NameError")
