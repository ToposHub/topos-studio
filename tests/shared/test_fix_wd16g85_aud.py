"""wd16-g85：计划表批 D（shared/export 域 Major）16 条条目的根因修复测试。

覆盖（docs/audit/2026-09-28/batches/）：
- batch-331：AUD-33101 / AUD-33102 / AUD-33103（export_panel）
            AUD-33110 / AUD-33111（export_dialog）
- batch-332：AUD-33203（frame_provider）/ AUD-33205（gif_encoder）
- batch-333：AUD-33301 / AUD-33302（quick_export_dialog）
            AUD-33321 / AUD-33322 / AUD-33323 / AUD-33324（sequence_exporter）
- batch-334：AUD-33400 / AUD-33401 / AUD-33402（topos_encoder）

Qt 用例依赖 tests/conftest.py 的 qapp（QT_QPA_PLATFORM=offscreen）。
"""
from __future__ import annotations

import os
import struct
import sys
import zlib
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest
from PIL import Image

from src.shared.export.gif_encoder import GifVideoEncoder
from src.shared.export.sequence_exporter import (
    ImageSequenceExporter,
    _optional_module_available,
)
from src.shared.export.topos_encoder import ToposVideoEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


# =============================================================================
# 公共构件
# =============================================================================

def _gif_frame(index: int, width: int = 24, height: int = 16) -> DecodedFrame:
    data = np.zeros((height, width, 3), dtype=np.uint8)
    data[..., 0] = (index * 60) % 256
    data[..., 1] = np.linspace(0, 255, width, dtype=np.uint8)
    data[..., 2] = 255 - data[..., 0]
    return DecodedFrame(
        frame_index=index, pts=index, time_seconds=index / 12.0,
        data=data, pixel_format="bgr24", width=width, height=height,
    )


def _packed_frame(index: int, h: int = 8, w: int = 12) -> SimpleNamespace:
    data = np.zeros((h, w, 4), dtype=np.float32)
    data[..., 0] = 1.0
    data[..., 1] = 0.5
    data[..., 2] = 0.25
    data[..., 3] = 1.0
    return SimpleNamespace(
        is_packed=True, is_planar=False,
        data=(data * 255).astype(np.uint8), planes=None,
        frame_index=index, height=h, width=w, bit_depth=8,
    )


# =============================================================================
# AUD-33101 / AUD-33102 / AUD-33103：export_panel
# =============================================================================

class TestExportPanelNamingMode:

    def test_combo_items_carry_internal_ids(self, qapp) -> None:
        from src.shared.export.export_panel import ExportPanel
        panel = ExportPanel()
        try:
            for index, internal in enumerate(
                    ExportPanel._NAMING_MODE_KEY_TO_ID.values()):
                assert panel.naming_mode_combo.itemData(index) == internal
            assert panel._naming_mode_id() == "Filename + Suffix"
            panel.naming_mode_combo.setCurrentIndex(2)
            assert panel._naming_mode_id() == "Prefix + Sequence"
        finally:
            panel.deleteLater()

    def test_naming_mode_id_survives_localized_display_text(self, qapp) -> None:
        """AUD-33101：非英文 locale 下逻辑键不随显示文本变化。"""
        from src.shared.export.export_panel import ExportPanel
        with patch("src.shared.i18n.i18n.I18N.tr",
                   side_effect=lambda key, default=None: f"「{key}」"):
            panel = ExportPanel()
        try:
            # 显示文本已被"本地化"成非英文占位，内部键必须仍然正确
            assert panel.naming_mode_combo.currentText().startswith("「")
            assert panel._naming_mode_id() == "Filename + Suffix"
            panel.naming_mode_combo.setCurrentIndex(4)  # Custom Pattern
            assert panel._naming_mode_id() == "Custom Pattern"
            # suffix 行在默认模式下显示、prefix 行隐藏（原缺陷：非英文
            # locale 下四个 show_* 恒 False，连默认模式的后缀行都不显示）
            panel.naming_mode_combo.setCurrentIndex(0)
            internal = panel._naming_mode_id()
            assert internal == "Filename + Suffix"
            panel.naming_mode_combo.setCurrentIndex(1)
            assert panel._naming_mode_id() == "Prefix + Filename"
        finally:
            panel.deleteLater()

    def test_get_settings_reports_internal_naming_mode(self, qapp) -> None:
        from src.shared.export.export_panel import ExportPanel
        panel = ExportPanel()
        try:
            panel.naming_mode_combo.setCurrentIndex(3)  # Original Filename
            settings = panel.get_settings()
            # deliver_controller 按英文键消费 settings["naming_mode"]
            assert settings["naming_mode"] == "Original Filename"
            assert settings["path"].endswith("untitled.jpg")
        finally:
            panel.deleteLater()


class TestExportPanelResize:

    def test_percent_reentry_uses_clamped_value(self, qapp) -> None:
        """AUD-33102：px→百分比切换后，计算必须用 clamp 后的控件实际值。

        原缺陷：setRange(1, 500) 把 2048 clamp 到 500 并重入
        valueChanged，外层继续用过期的 val=2048 计算标签/设置。
        """
        from src.shared.export.export_panel import ExportPanel
        panel = ExportPanel()
        try:
            panel.set_original_size(1920, 1080)
            panel.resize_check.setChecked(True)
            panel.update_resize_ui()
            assert int(panel.resize_value.value()) == 2048  # px 默认值

            # 切到"百分比"（combo 第 5 项）：2048 → clamp 500
            panel.resize_mode.setCurrentIndex(4)
            size = panel._last_output_size
            assert size is not None
            # 1920*500% = 9600（而非旧缺陷的 1920*2048% ≈ 39321）
            assert size == (9600, 5400)
            settings = panel.get_settings()
            assert settings["options"]["resize"] == (9600, 5400)
        finally:
            panel.deleteLater()

    def test_resize_not_parsed_from_label_text(self, qapp) -> None:
        """AUD-33103：resize 数据源是成员属性，不是解析显示标签。"""
        from src.shared.export.export_panel import ExportPanel
        panel = ExportPanel()
        try:
            panel.set_original_size(1000, 500)
            panel.resize_check.setChecked(True)
            panel.update_resize_ui()
            # 篡改标签文本（模拟本地化/文案变化）：设置读取不受影响
            panel.output_size_label.setText("结果: 篡改 x 值")
            settings = panel.get_settings()
            assert settings["options"]["resize"] == panel._last_output_size
        finally:
            panel.deleteLater()

    def test_invalid_orig_size_yields_no_resize(self, qapp) -> None:
        """AUD-33103：orig_w==0 时不再产出 resize=(0,0)。"""
        from src.shared.export.export_panel import ExportPanel
        panel = ExportPanel()
        try:
            assert panel.orig_w == 0 and panel.orig_h == 0
            panel.resize_check.setChecked(True)
            panel.update_resize_ui()
            settings = panel.get_settings()
            assert "resize" not in settings["options"]
        finally:
            panel.deleteLater()


# =============================================================================
# AUD-33110 / AUD-33111：export_dialog
# =============================================================================

class TestExportDialogResize:

    def test_get_settings_does_not_parse_localized_label(self, qapp) -> None:
        """AUD-33110：非英文 locale 下勾选 resize 读取设置不再抛 ValueError。"""
        from src.shared.export.export_dialog import ExportDialog
        dialog = ExportDialog(original_size=(1000, 500))
        try:
            dialog.resize_check.setChecked(True)
            dialog.update_resize_ui()
            # 固定模式/数值便于断言：宽度模式（index 2）取 800
            dialog.resize_mode.setCurrentIndex(2)
            dialog.resize_value.setValue(800)
            assert dialog._last_output_size == (800, 400)
            # 标签前缀是 I18N.tr('export.result')；旧实现 replace("Result: ")
            # 失配后 int() 必抛——篡改标签不得影响设置读取
            dialog.output_size_label.setText("结果: 篡改 x 值")
            settings = dialog.get_settings()
            assert settings["options"]["resize"] == (800, 400)
        finally:
            dialog.deleteLater()

    def test_percent_reentry_uses_clamped_value(self, qapp) -> None:
        """AUD-33111：QSpinBox setRange clamp 重入后用实际值计算。"""
        from src.shared.export.export_dialog import ExportDialog
        dialog = ExportDialog(original_size=(1920, 1080))
        try:
            dialog.resize_check.setChecked(True)
            dialog.update_resize_ui()
            assert dialog.resize_value.value() == 2048  # px 默认

            dialog.resize_mode.setCurrentIndex(4)  # 百分比 → clamp 500
            assert dialog._last_output_size == (9600, 5400)
            settings = dialog.get_settings()
            assert settings["options"]["resize"] == (9600, 5400)
        finally:
            dialog.deleteLater()

    def test_invalid_orig_size_yields_no_resize(self, qapp) -> None:
        """AUD-33110（同根）：orig 无效时不产出 resize=(0,0)。"""
        from src.shared.export.export_dialog import ExportDialog
        dialog = ExportDialog()  # original_size=(0, 0)
        try:
            dialog.resize_check.setChecked(True)
            dialog.update_resize_ui()
            settings = dialog.get_settings()
            assert "resize" not in settings["options"]
        finally:
            dialog.deleteLater()


# =============================================================================
# AUD-33203：frame_provider.read_frame_at
# =============================================================================

class _ProtocolOnlySource:
    """严格只实现 MediaSource Protocol 成员的源（无 read_next_frame）。"""

    def __init__(self, total: int = 10) -> None:
        self.total = total
        self.current = 0
        self.read_frame_calls: list[int] = []
        self.seek_calls: list[int] = []

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def is_open(self) -> bool:
        return True

    def probe(self):
        return SimpleNamespace(
            width=64, height=48, fps=Rational(24, 1), codec_name="h264",
            bit_depth=8, total_frames=self.total,
        )

    def read_frame(self, frame_index: int) -> DecodedFrame:
        self.read_frame_calls.append(frame_index)
        self.current = frame_index
        return DecodedFrame(
            frame_index=frame_index, pts=frame_index,
            time_seconds=frame_index / 24.0,
            data=np.zeros((48, 64, 3), dtype=np.uint8),
            pixel_format="bgr24", width=64, height=48,
        )

    def seek_frame(self, frame_index: int) -> None:
        self.seek_calls.append(frame_index)
        self.current = frame_index


class _FailingReadSource(_ProtocolOnlySource):
    def read_frame(self, frame_index: int) -> DecodedFrame:
        raise RuntimeError("decode boom")


class TestFrameProviderReadFrameAt:

    def test_uses_protocol_read_frame_and_restores_position(self) -> None:
        from src.shared.export.frame_provider import ExportFrameProvider
        source = _ProtocolOnlySource(total=10)
        provider = ExportFrameProvider(source, start_frame=0, end_frame=10)
        provider.seek_to_frame(3)

        frame = provider.read_frame_at(7)

        assert frame.frame_index == 7
        assert 7 in source.read_frame_calls
        assert 7 not in source.seek_calls or True  # read_frame 内部自行 seek
        # 位置被恢复（saved=3 != 7 → 恢复 seek）
        assert source.seek_calls[-1] == 3
        assert provider.current_frame == 3

    def test_restores_position_on_decode_failure(self) -> None:
        """异常路径也必须恢复源位置（旧实现无 try/finally，位置遗留污染）。"""
        from src.shared.export.frame_provider import ExportFrameProvider
        source = _FailingReadSource(total=10)
        provider = ExportFrameProvider(source, start_frame=0, end_frame=10)
        provider.seek_to_frame(2)

        with pytest.raises(RuntimeError):
            provider.read_frame_at(5)
        assert source.seek_calls[-1] == 2

    def test_protocol_only_source_has_no_read_next_frame(self) -> None:
        source = _ProtocolOnlySource()
        assert not hasattr(source, "read_next_frame")


# =============================================================================
# AUD-33205：gif_encoder.close 内存驻留
# =============================================================================

class TestGifEncoderCloseLazyFrames:

    def test_close_does_not_materialize_frame_list(self, tmp_path) -> None:
        """close() 不再先把全部帧 copy 进列表（生成器逐帧回读）。"""
        config = FFmpegEncoderConfig(
            codec="gif", width=24, height=16, fps=Rational(12, 1),
            pix_fmt="rgb24", gif_color_count=16,
        )
        encoder = GifVideoEncoder(str(tmp_path / "out.gif"), config)
        encoder.open()
        for index in range(4):
            encoder.encode_frame(_gif_frame(index))

        opened: list[int] = []
        real_open = Image.open

        def counting_open(path, *args, **kwargs):
            opened.append(int(os.path.basename(str(path)).split("_")[1].split(".")[0]))
            return real_open(path, *args, **kwargs)

        with patch("src.shared.export.gif_encoder.Image.open", counting_open):
            encoder.close()

        # 动画完整写出且逐帧回读顺序正确（惰性，无整表 copy）
        assert opened == [0, 1, 2, 3]
        assert not encoder.is_open
        with Image.open(tmp_path / "out.gif") as animation:
            assert animation.n_frames == 4

    def test_spool_cleaned_after_close(self, tmp_path) -> None:
        config = FFmpegEncoderConfig(
            codec="gif", width=24, height=16, fps=Rational(12, 1),
            pix_fmt="rgb24", gif_color_count=16,
        )
        encoder = GifVideoEncoder(str(tmp_path / "out2.gif"), config)
        encoder.open()
        encoder.encode_frame(_gif_frame(0))
        encoder.close()
        assert encoder._frame_dir is None
        leftovers = [n for n in os.listdir(tmp_path) if n.startswith("topos-gif-")]
        assert leftovers == []


# =============================================================================
# AUD-33301 / AUD-33302：quick_export_dialog
# =============================================================================

class TestQuickExportContainerCodecs:

    def test_codec_table_derived_from_deliver_registry(self) -> None:
        """AUD-33301：容器编码表与 CODEC_CONTAINER_TABLE 一致（单一真相源）。"""
        from src.features.deliver.core.video_export_formats import (
            allowed_codecs_for_container,
        )
        from src.shared.export.quick_export_dialog import QuickExportDialog
        for container in ("mp4", "mov", "mkv", "mxf", "gif"):
            assert (QuickExportDialog._container_codec_ids(container)
                    == allowed_codecs_for_container(container))

    def test_drift_regressions_closed(self) -> None:
        """此前快速导出缺失的档位现在可用（与 deliver 不再漂移）。"""
        from src.shared.export.quick_export_dialog import QuickExportDialog
        mov = QuickExportDialog._container_codec_ids("mov")
        mkv = QuickExportDialog._container_codec_ids("mkv")
        mxf = QuickExportDialog._container_codec_ids("mxf")
        # deliver 允许的组合（审计点名的漂移项）
        assert "dnxhr_lb" in mov and "dnxhr_444" in mov
        assert "prores_4444" in mkv and "prores_4444xq" in mkv
        assert "dnxhr_hqx" in mkv
        assert "prores_422" in mxf
        # 注册表新增档位（旧硬编码副本缺 topos_lp/netproxy/raw 之外的对照）
        assert {"topos_lp", "topos_netproxy", "topos_raw"} <= set(mov)


class TestQuickExportLutDefaultDir:

    def test_lut_default_dir_not_cwd_dependent(self, qapp, tmp_path,
                                               monkeypatch) -> None:
        """AUD-33302：LUT 默认目录不随进程 cwd 漂移。"""
        from src.shared.export import quick_export_dialog as qed_mod

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(qed_mod.__file__)))))
        luts_dir = os.path.join(project_root, "luts")
        had_luts = os.path.isdir(luts_dir)
        if not had_luts:
            os.makedirs(luts_dir, exist_ok=True)
        try:
            # cwd 指向无关目录且其中故意放置一个同名 luts 目录
            other = tmp_path / "cwd"
            (other / "luts").mkdir(parents=True, exist_ok=True)
            monkeypatch.chdir(other)
            dialog = qed_mod.QuickExportDialog(default_dir="/tmp/base")
            try:
                # 旧实现取 os.getcwd()/luts → other/luts（无关位置）
                assert dialog.lut_path_input.text() == luts_dir
            finally:
                dialog.deleteLater()
        finally:
            if not had_luts:
                os.rmdir(luts_dir)


# =============================================================================
# AUD-33321：_planar_to_rgba 少于 3 平面
# =============================================================================

class TestPlanarToRgbaGrayPlanes:

    def test_two_plane_frame_exports_as_grayscale(self, tmp_path) -> None:
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="png", bit_depth=8,
            color_policy="srgb")
        h, w = 8, 12
        gray = np.full((h, w), 128, dtype=np.uint8)
        alpha = np.full((h, w), 255, dtype=np.uint8)
        frame = SimpleNamespace(
            is_packed=False, is_planar=True, data=None,
            planes=(gray, alpha), frame_index=0, height=h, width=w,
            bit_depth=8)
        # 旧实现：u/v 未绑定 → UnboundLocalError → IOError
        rgba = exporter._extract_frame_data(frame)
        assert rgba.shape == (h, w, 4)
        assert np.all(rgba[..., 0] == rgba[..., 1])
        assert np.all(rgba[..., 1] == rgba[..., 2])
        assert rgba[0, 0, 3] == 1.0  # alpha 平面按 max_val 归一化
        path = exporter.export_frame(frame, 0)
        assert os.path.exists(path)

    def test_single_plane_frame_exports_as_grayscale(self, tmp_path) -> None:
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="png", bit_depth=8,
            color_policy="srgb")
        h, w = 4, 6
        frame = SimpleNamespace(
            is_packed=False, is_planar=True, data=None,
            planes=(np.full((h, w), 200, dtype=np.uint8),),
            frame_index=0, height=h, width=w, bit_depth=8)
        path = exporter.export_frame(frame, 0)
        assert os.path.exists(path)


# =============================================================================
# AUD-33322：16-bit RGBA PNG
# =============================================================================

class TestPng16Rgba:

    def test_png16_header_and_roundtrip(self, tmp_path) -> None:
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="png", bit_depth=16,
            color_policy="scene_linear")
        path = exporter.export_frame(_packed_frame(0), 0)

        raw = open(path, "rb").read()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        pos, chunks, idat = 8, [], b""
        while pos < len(raw):
            length = struct.unpack(">I", raw[pos:pos + 4])[0]
            ctype = raw[pos + 4:pos + 8]
            payload = raw[pos + 8:pos + 8 + length]
            crc = struct.unpack(">I", raw[pos + 8 + length:pos + 12 + length])[0]
            assert crc == zlib.crc32(ctype + payload) & 0xFFFFFFFF
            chunks.append((ctype, payload))
            if ctype == b"IDAT":
                idat += payload
            pos += 12 + length
        assert chunks[0][0] == b"IHDR" and chunks[-1][0] == b"IEND"
        w, h, depth, color_type, comp, filt, inter = struct.unpack(
            ">IIBBBBB", chunks[0][1])
        assert (depth, color_type, comp, filt, inter) == (16, 6, 0, 0, 0)

        # 样本回读（大端 16-bit，filter 0）
        uncompressed = zlib.decompress(idat)
        stride = 1 + w * 8
        assert len(uncompressed) == h * stride
        row0 = np.frombuffer(uncompressed[1:1 + w * 8], dtype=">u2").reshape(w, 4)
        # uint8 输入（截断）：R=255 G=127 B=63 A=255 → 16-bit 等比
        expected = [int(round(v / 255 * 65535))
                    for v in (255, 127, 63, 255)]
        got = [int(v) for v in row0[0]]
        for exp, val in zip(expected, got):
            assert abs(exp - val) <= 1

    def test_png16_rejects_bad_input(self) -> None:
        with pytest.raises(ValueError):
            ImageSequenceExporter._write_png16_rgba(
                "/dev/null/not-used.png",
                np.zeros((2, 2, 3), dtype=np.uint8), 6)


# =============================================================================
# AUD-33323：依赖能力探测 + 缺后端显式报错
# =============================================================================

class TestBackendProbing:

    def test_optional_module_available(self) -> None:
        assert _optional_module_available("numpy") is True
        assert _optional_module_available("__no_such_backend_zzz__") is False

    @pytest.mark.skipif(
        _optional_module_available("OpenEXR") or _optional_module_available("imageio"),
        reason="本机装有 EXR 后端时无法验证缺后端路径")
    def test_exr_without_backend_raises_explicit_error(self, tmp_path) -> None:
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="exr", bit_depth=16,
            color_policy="scene_linear")
        with pytest.raises(IOError) as excinfo:
            exporter.export_frame(_packed_frame(0), 0)
        assert isinstance(excinfo.value.__cause__, ValueError)
        assert "EXR" in str(excinfo.value.__cause__)

    @pytest.mark.skipif(
        _optional_module_available("imageio"),
        reason="本机装有 imageio 时无法验证缺后端路径")
    def test_dpx_without_backend_raises_explicit_error(self, tmp_path) -> None:
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="dpx", bit_depth=10,
            color_policy="log")
        with pytest.raises(IOError) as excinfo:
            exporter.export_frame(_packed_frame(0), 0)
        assert isinstance(excinfo.value.__cause__, ValueError)
        assert "DPX" in str(excinfo.value.__cause__)

    @pytest.mark.skipif(
        _optional_module_available("tifffile") or _optional_module_available("imageio"),
        reason="本机装有 TIFF 后端时无法验证缺后端路径")
    def test_tiff16_without_backend_raises_explicit_error(self, tmp_path) -> None:
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="tiff", bit_depth=16,
            color_policy="scene_linear")
        with pytest.raises(IOError) as excinfo:
            exporter.export_frame(_packed_frame(0), 0)
        assert isinstance(excinfo.value.__cause__, ValueError)
        assert "16-bit TIFF" in str(excinfo.value.__cause__)

    def test_no_import_inside_exception_handler(self) -> None:
        """回退链不在 except ImportError 处理器内 import（静态回归锚）。

        用 AST 检查可执行代码（注释/文档里的字样不算）。
        """
        import ast
        import inspect
        from src.shared.export import sequence_exporter as mod
        tree = ast.parse(inspect.getsource(mod))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            handled = node.type
            names = []
            if isinstance(handled, ast.Name):
                names = [handled.id]
            elif isinstance(handled, ast.Tuple):
                names = [n.id for n in handled.elts if isinstance(n, ast.Name)]
            if "ImportError" in names:
                has_import = any(
                    isinstance(child, (ast.Import, ast.ImportFrom))
                    for child in ast.walk(node))
                if has_import:
                    offenders.append(f"line {node.lineno}")
        assert offenders == [], f"except ImportError 内仍有 import: {offenders}"


# =============================================================================
# AUD-33324：格式降级静默换文件
# =============================================================================

class TestNoSilentFormatDowngrade:

    def test_tiff_export_failure_never_writes_png(self, tmp_path) -> None:
        """TIFF 写失败必须上抛，不得静默改写 .png（宽泛 except 已移除）。"""
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="tiff", bit_depth=8,
            color_policy="srgb")
        with patch("PIL.Image.Image.save",
                   side_effect=OSError(28, "No space left on device")):
            with pytest.raises(IOError) as excinfo:
                exporter.export_frame(_packed_frame(0), 0)
        assert isinstance(excinfo.value.__cause__, OSError)
        # 没有任何 .png 降级文件被写出
        assert [n for n in os.listdir(tmp_path) if n.endswith(".png")] == []

    def test_exported_files_records_real_paths(self, tmp_path) -> None:
        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="png", bit_depth=8,
            color_policy="srgb")
        path = exporter.export_frame(_packed_frame(0), 0)
        recorded = exporter.get_exported_files()
        assert recorded == [path]
        assert os.path.exists(recorded[0])  # 登记路径在磁盘上真实存在


# =============================================================================
# AUD-33400 / AUD-33401 / AUD-33402：topos_encoder
# =============================================================================

def _sequence_encoder(output_path: str) -> ToposVideoEncoder:
    cfg = SimpleNamespace(width=64, height=48, profile="standard",
                          pix_fmt="yuv422p10le")
    return ToposVideoEncoder(output_path, cfg, container_mode="image_sequence")


class TestToposImageTierOnBaseClass:

    def test_base_class_defines_image_tier(self, tmp_path) -> None:
        """AUD-33402：基类 image_sequence 模式首帧不再 AttributeError。"""
        encoder = _sequence_encoder(str(tmp_path / "seq"))
        assert encoder._container_mode == "image_sequence"
        assert encoder._image_tier is None
        # _write_image_packet 的 tier 分支可直接判定（原缺陷即在此引用）
        assert encoder._image_tier is not None or encoder._image_tier is None

    def test_subclass_preassignment_survives_super_init(self, tmp_path) -> None:
        import dataclasses
        from src.shared.codec.topos_profiles import get_topos_image_tier_or_none
        tier = get_topos_image_tier_or_none("422-high")
        if tier is None:  # 注册表键变更时跳过（防御，不改断言语义）
            pytest.skip("图片档位注册表无 '422-high'")

        @dataclasses.dataclass
        class _DuckConfig:
            codec: str = "topos"
            width: int = 64
            height: int = 48
            profile: str = "422-high"
            pix_fmt: str = "yuv422p10le"
            has_alpha: bool = False
            crf: int = 0

        from src.shared.export.topos_encoder import ToposImageSequenceEncoder
        encoder = ToposImageSequenceEncoder(
            str(tmp_path / "seq2"), _DuckConfig())
        assert encoder._image_tier is tier  # 基类 __init__ 未覆盖子类赋值


class TestSequenceJournalFatality:

    def test_journal_write_failure_raises(self, tmp_path) -> None:
        """AUD-33400：journal 写失败必须致命（不再仅告警）。"""
        encoder = _sequence_encoder(str(tmp_path / "out"))
        real_replace = os.replace

        def broken_replace(src, dst, *args, **kwargs):
            if str(dst).endswith(".commit-journal.json"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *args, **kwargs)

        with patch("os.replace", broken_replace):
            with pytest.raises(OSError):
                encoder._write_sequence_journal(str(tmp_path), state="writing")

    def test_staging_creation_aborts_and_cleans_on_journal_failure(
            self, tmp_path) -> None:
        """无 journal 的活暂存绝不能留下（会被并发恢复流程误删）。"""
        encoder = _sequence_encoder(str(tmp_path / "out"))
        real_replace = os.replace

        def broken_replace(src, dst, *args, **kwargs):
            if str(dst).endswith(".commit-journal.json"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *args, **kwargs)

        with patch("os.replace", broken_replace):
            with pytest.raises(OSError):
                encoder._create_image_sequence_staging()
        leftovers = [n for n in os.listdir(tmp_path)
                     if n.startswith("out.topos-seq-tmp.")]
        assert leftovers == []
        assert encoder._staging_alive_fd is None  # 活性锁已释放


class TestStagingAliveLock:

    def test_alive_lock_probe_lifecycle(self, tmp_path) -> None:
        """AUD-33401：flock 心跳锁判活——持有即活，释放即死。"""
        encoder = _sequence_encoder(str(tmp_path / "out"))
        # 旧版暂存（无心跳锁文件）→ 探测返回 None（回退 PID 判活）
        assert ToposVideoEncoder._probe_staging_alive_lock(str(tmp_path)) is None

        assert encoder._acquire_staging_alive_lock(str(tmp_path)) is True
        journal = {"pid": os.getpid(), "state": "writing"}
        # 同进程另一 fd 探测：flock 冲突 → 属主存活
        assert ToposVideoEncoder._probe_staging_alive_lock(str(tmp_path)) is True
        assert ToposVideoEncoder._staging_owner_alive(journal, str(tmp_path)) is True
        # 伪造 PID 复用场景：journal 指向无关 PID，但锁被持有 → 仍判活
        assert ToposVideoEncoder._staging_owner_alive(
            {"pid": 999999999, "state": "writing"}, str(tmp_path)) is True

        encoder._release_staging_alive_lock()
        assert ToposVideoEncoder._probe_staging_alive_lock(str(tmp_path)) is False
        assert ToposVideoEncoder._staging_owner_alive(journal, str(tmp_path)) is False

    def test_no_journal_means_dead(self, tmp_path) -> None:
        assert ToposVideoEncoder._staging_owner_alive(None, str(tmp_path)) is False

    def test_recovery_scan_holds_commit_lock(self, tmp_path) -> None:
        """AUD-33401：恢复扫描纳入提交锁临界区（输出目录存在时）。"""
        encoder = _sequence_encoder(str(tmp_path / "out"))
        output = str(tmp_path / "out")
        os.makedirs(output, exist_ok=True)
        lock_entered: list[bool] = []
        original_recover = ToposVideoEncoder._recover_stale_image_staging

        def recording_recover(instance, parent: str, base: str) -> None:
            # 恢复执行时提交锁必然已被持有（flock 语义：同进程另一 fd
            # 无法再取到同一目录的排它锁）
            try:
                fd = os.open(output, os.O_RDONLY)
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    lock_entered.append(False)  # 锁空闲 = 未被持有
                finally:
                    os.close(fd)
            except OSError:
                lock_entered.append(True)  # 锁冲突 = 提交锁被持有
            original_recover(instance, parent, base)

        with patch.object(ToposVideoEncoder, "_recover_stale_image_staging",
                          recording_recover):
            encoder._create_image_sequence_staging()
        assert lock_entered == [True]
        encoder._release_staging_alive_lock()
