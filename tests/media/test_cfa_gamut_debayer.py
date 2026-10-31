"""RC3/RC6（M6-I 组）：debayer IDT gamut + CFA 交付出口测试。

覆盖：
1. CPU debayer gamut 矩阵：identity 零漂移、矩阵应用与 numpy 参考一致、
   gamma_mode（sRGB/2.2/linear）语义（与 GPU shader 同点同式）；
2. RC6 ``_debayer_cfa_to_bgr``：TRWM sensor_matrix_id → 预设矩阵链路、
   未知相机 identity + 显式语义、高精度开关输出位深；
3. RC6 端到端（需 native 库）：TRAW 源 → 标准档（再编码）导出成功——
   此前该路径显式拒绝（"导出暂不支持 RAW/CFA"），现在是 debayer 交付
   出口；与 R2 的 RAW→RAW 直出构成双路径。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")

from src.shared.codec import topos_binding
from src.shared.video.camera_gamut_matrices import (
    gamut_matrix_to_bt709_or_identity,
    matrix3_column_major,
    resolve_gamut_id,
)
from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes


def _lib_available() -> bool:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


def _constant_planes_16(code_r: int, code_g: int, code_b: int,
                        w: int = 8, h: int = 8):
    """16-bit linear 域恒值相位平面（R/Gr/Gb/B；Gr=Gb=code_g）。"""
    pw, ph = (w + 1) // 2, (h + 1) // 2
    return (
        np.full((ph, pw), code_r, dtype=np.uint16),
        np.full((ph, pw), code_g, dtype=np.uint16),
        np.full((ph, pw), code_g, dtype=np.uint16),
        np.full((ph, pw), code_b, dtype=np.uint16),
    )


class TestCpuDebayerGamut:
    def test_identity_matrix_is_zero_drift(self):
        """identity（None）与显式 identity 输出逐位一致（行为零漂移）。"""
        planes = _constant_planes_16(20000, 30000, 8000)
        base = debayer_cfa_planes(planes, 8, 8, bit_depth=16,
                                  gamma_mode=0)
        eye = np.eye(3)
        explicit = debayer_cfa_planes(planes, 8, 8, bit_depth=16,
                                      gamma_mode=0, gamut_matrix=eye)
        np.testing.assert_array_equal(base, explicit)

    def test_matrix_application_matches_reference(self):
        """非平凡矩阵输出 == clip(M @ 线性值)（逐像素 numpy 参考）。"""
        code = (20000, 30000, 8000)
        planes = _constant_planes_16(*code)
        matrix, explicit = gamut_matrix_to_bt709_or_identity("arri-awg3")
        assert explicit
        out = debayer_cfa_planes(planes, 8, 8, bit_depth=16,
                                 gamma_mode=0, gamut_matrix=matrix)
        v = np.array(code, dtype=np.float64) / 65535.0
        expected = np.clip(matrix @ v, 0.0, 1.0)
        # 恒值场全站点同值；debayer 均值不引入误差
        np.testing.assert_allclose(out[0, 0], expected, atol=2e-6)
        np.testing.assert_allclose(
            out.reshape(-1, 3).mean(axis=0), expected, atol=2e-6)

    def test_gamma_modes(self):
        """sRGB 编码 == np 公式；linear 模式不做任何编码。"""
        planes = _constant_planes_16(20000, 30000, 8000)
        lin = debayer_cfa_planes(planes, 8, 8, bit_depth=16, gamma_mode=0)
        srgb = debayer_cfa_planes(planes, 8, 8, bit_depth=16, gamma_mode=2)
        v = lin[0, 0].astype(np.float64)
        hi = 1.055 * np.power(np.maximum(v, 0.0), 1.0 / 2.4) - 0.055
        expected = np.where(v >= 0.0031308, hi, v * 12.92)
        np.testing.assert_allclose(srgb[0, 0], expected, atol=2e-6)
        g22 = debayer_cfa_planes(planes, 8, 8, bit_depth=16, gamma_mode=1)
        assert not np.allclose(g22, srgb)
        # display_ootf 布尔折算：False → linear（mode 0）
        via_bool = debayer_cfa_planes(planes, 8, 8, bit_depth=16,
                                      display_ootf=False)
        np.testing.assert_array_equal(via_bool, lin)

    def test_trwm_resolution_uses_preset_matrix(self):
        """sensor_matrix_id=3（Panasonic）→ 预设矩阵非 identity。"""
        gid = resolve_gamut_id(None, 3)
        assert gid == "panasonic-vgamut"
        matrix, explicit = gamut_matrix_to_bt709_or_identity(gid)
        assert explicit
        assert not np.array_equal(matrix, np.eye(3))


# ---------------------------------------------------------------------------
# RC6：CFA 交付出口（_debayer_cfa_to_bgr，单元级 stub）
# ---------------------------------------------------------------------------


def _cfa_decoded_frame(planes, w=8, h=8, extra=None, bd=16):
    from src.shared.media.frame import DecodedFrame

    return DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0, data=None,
        planes=tuple(planes), pixel_format=f"topos_cfa{bd}le",
        dtype=np.dtype(np.uint16), width=w, height=h, extra=extra or {},
    )


class TestDebayerCfaToBgr:
    def _exporter(self, high_precision=False):
        from src.features.edit.core.timeline_export import TimelineExporter

        exp = object.__new__(TimelineExporter)
        exp._use_high_precision_export = high_precision
        return exp

    def test_trwm_sensor_id_drives_gamut(self):
        """TRWM sensor_matrix_id=3 → V-Gamut 预设矩阵；输出与直接调用
        debayer_cfa_planes(同矩阵) 一致。"""
        planes = _constant_planes_16(20000, 30000, 8000)
        trwm = {"sensor_matrix_id": 3, "wb_gain_r": 1.0, "wb_gain_g": 1.0,
                "wb_gain_b": 1.0, "exposure_stops": 0.0, "black_level": 0.0}
        frame = _cfa_decoded_frame(planes, extra={"topos_trwm": trwm})
        exp = self._exporter()
        out = exp._debayer_cfa_to_bgr(frame, planes, "topos_cfa16le")
        assert out is not None and out.dtype == np.uint8
        matrix, explicit = gamut_matrix_to_bt709_or_identity(
            "panasonic-vgamut")
        assert explicit
        ref = debayer_cfa_planes(planes, 8, 8, bit_depth=16,
                                 display_ootf=True, gamut_matrix=matrix)
        ref_bgr = np.ascontiguousarray(ref[..., ::-1])
        np.testing.assert_allclose(
            out.astype(np.float32),
            np.clip(ref_bgr, 0.0, 1.0).__mul__(255.0).astype(np.uint8).astype(np.float32),
            atol=1.0)

    def test_unknown_camera_identity_flagged(self):
        """无 TRWM → identity 直出 + 显式日志语义（不静默猜）。"""
        planes = _constant_planes_16(20000, 30000, 8000)
        frame = _cfa_decoded_frame(planes, extra={})
        exp = self._exporter()
        out = exp._debayer_cfa_to_bgr(frame, planes, "topos_cfa16le")
        matrix, explicit = gamut_matrix_to_bt709_or_identity(None)
        assert not explicit
        ref = debayer_cfa_planes(planes, 8, 8, bit_depth=16,
                                 display_ootf=True, gamut_matrix=matrix)
        ref_bgr8 = (np.clip(np.ascontiguousarray(ref[..., ::-1]),
                            0.0, 1.0) * 255.0).astype(np.uint8)
        np.testing.assert_array_equal(out, ref_bgr8)

    def test_high_precision_export_outputs_16bit(self):
        planes = _constant_planes_16(20000, 30000, 8000)
        frame = _cfa_decoded_frame(planes, extra={})
        exp = self._exporter(high_precision=True)
        out = exp._debayer_cfa_to_bgr(frame, planes, "topos_cfa16le")
        assert out.dtype == np.uint16

    def test_bad_plane_count_returns_none(self):
        frame = _cfa_decoded_frame(_constant_planes_16(1, 2, 3)[:2])
        exp = self._exporter()
        assert exp._debayer_cfa_to_bgr(frame, frame.planes, "topos_cfa16le") \
            is None

    def test_user_raw_params_override_trwm(self):
        """2026-09-21 复查：导出口与预览同优先级——用户 clip RawParams
        （WB/EI/黑位/色彩空间/伽马）优先于帧 TRWM as-shot。"""
        from src.features.edit.core.clip_params import RawParams
        from src.shared.video.camera_gamut_matrices import (
            gamut_matrix_to_bt709_or_identity, resolve_gamut_id)

        planes = _constant_planes_16(20000, 30000, 8000)
        trwm = {"sensor_matrix_id": 3, "wb_gain_r": 1.0, "wb_gain_g": 1.0,
                "wb_gain_b": 1.0, "exposure_stops": 0.0, "black_level": 0.0}
        frame = _cfa_decoded_frame(planes, extra={"topos_trwm": trwm})
        exp = self._exporter()
        raw = RawParams(wb_gain_r=2.0, wb_gain_g=1.0, wb_gain_b=1.0,
                        exposure_stops=0.0, black_level=0.0,
                        color_space="arri-awg3", gamma="linear")
        out = exp._debayer_cfa_to_bgr(frame, planes, "topos_cfa16le",
                                      raw_params=raw)
        # 参考值：用户 color_space（arri-awg3）矩阵 + WB(2,1,1) + linear
        matrix, _ = gamut_matrix_to_bt709_or_identity(
            resolve_gamut_id("arri-awg3", 3))
        ref = debayer_cfa_planes(
            planes, 8, 8, bit_depth=16,
            wb_gains=(2.0, 1.0, 1.0), gamma_mode=0, gamut_matrix=matrix)
        expected = (np.clip(ref[..., ::-1], 0.0, 1.0) * 255.0).astype(np.uint8)
        np.testing.assert_array_equal(out, expected)
        # 反证：不传 raw（TRWM 路径）时 WB=1 输出必须不同
        out_trwm = exp._debayer_cfa_to_bgr(frame, planes, "topos_cfa16le")
        assert not np.array_equal(out, out_trwm)


# ---------------------------------------------------------------------------
# 2026-09-21 复查：resolve_debayer_params 三链同源优先级 + 奇数几何
# ---------------------------------------------------------------------------


class TestResolveDebayerParams:
    def test_user_over_trwm(self):
        """用户 clip RawParams 全字段优先于 TRWM as-shot。"""
        from src.features.edit.core.clip_params import RawParams
        from src.shared.video.camera_gamut_matrices import (
            resolve_debayer_params)

        raw = RawParams(wb_gain_r=2.0, exposure_stops=1.0, black_level=64.0,
                        color_space="sony-sgamut3", gamma="srgb")
        trwm = {"wb_gain_r": 1.5, "wb_gain_g": 1.5, "wb_gain_b": 1.5,
                "exposure_stops": -1.0, "black_level": 32.0,
                "sensor_matrix_id": 3}
        rp = resolve_debayer_params(raw, trwm)
        assert rp["wb_gains"] == (2.0, 1.0, 1.0)
        assert rp["exposure_stops"] == 1.0
        assert rp["black_level"] == 64.0
        assert rp["gamma_mode"] == 2
        assert rp["explicit_gamut"]
        # 用户显式 color_space 覆盖 TRWM sensor id
        assert not np.allclose(rp["matrix"], np.eye(3))

    def test_trwm_fallback_when_no_user_params(self):
        from src.shared.video.camera_gamut_matrices import (
            gamut_matrix_to_bt709_or_identity, resolve_debayer_params)

        trwm = {"wb_gain_r": 1.5, "wb_gain_g": 1.2, "wb_gain_b": 0.9,
                "exposure_stops": -0.5, "black_level": 32.0,
                "sensor_matrix_id": 3}
        rp = resolve_debayer_params(None, trwm)
        assert rp["wb_gains"] == (1.5, 1.2, 0.9)
        assert rp["exposure_stops"] == -0.5
        assert rp["black_level"] == 32.0
        # 默认查看伽马 = gamma 2.2（mode 1）
        assert rp["gamma_mode"] == 1
        expected, _ = gamut_matrix_to_bt709_or_identity("panasonic-vgamut")
        np.testing.assert_allclose(rp["matrix"], expected)

    def test_defaults_and_identity_when_nothing_declared(self):
        from src.shared.video.camera_gamut_matrices import resolve_debayer_params

        rp = resolve_debayer_params(None, None)
        assert rp["wb_gains"] == (1.0, 1.0, 1.0)
        assert rp["exposure_stops"] == 0.0
        assert rp["black_level"] == 0.0
        assert rp["gamma_mode"] == 1
        assert not rp["explicit_gamut"]
        np.testing.assert_array_equal(rp["matrix"], np.eye(3))

    def test_display_ootf_bool_folds_when_gamma_empty(self):
        """gamma 缺省时按 display_ootf 折算（True→1 / False→0）。"""
        from types import SimpleNamespace
        from src.shared.video.camera_gamut_matrices import resolve_debayer_params

        on = resolve_debayer_params(
            SimpleNamespace(wb_gain_r=1.0, wb_gain_g=1.0, wb_gain_b=1.0,
                            exposure_stops=0.0, black_level=0.0,
                            color_space="native", gamma="",
                            display_ootf=True), None)
        off = resolve_debayer_params(
            SimpleNamespace(wb_gain_r=1.0, wb_gain_g=1.0, wb_gain_b=1.0,
                            exposure_stops=0.0, black_level=0.0,
                            color_space="native", gamma="",
                            display_ootf=False), None)
        assert on["gamma_mode"] == 1
        assert off["gamma_mode"] == 0


class TestCpuDebayerGeometry:
    def test_odd_dimensions_honor_declared_size(self):
        """奇数帧：平面 = ceil(W/2)，组装得 W+1——按声明宽高截取
        （2026-09-21 复查：此前忽略 width/height 恒出 2*ceil）。"""
        w, h = 9, 7
        pw, ph = (w + 1) // 2, (h + 1) // 2
        rng = np.random.default_rng(7)
        planes = tuple(
            rng.integers(0, 4000, size=(ph, pw)).astype(np.uint16)
            for _ in range(4))
        out = debayer_cfa_planes(planes, w, h, bit_depth=12, gamma_mode=0)
        assert out.shape == (h, w, 3)
        # 偶数几何（零漂移）：等尺寸平面 × 2 == 平面形状
        even = debayer_cfa_planes(planes, pw * 2, ph * 2, bit_depth=12,
                                  gamma_mode=0)
        assert even.shape == (ph * 2, pw * 2, 3)


class TestTrawLog0TransferDecode:
    def test_matches_debayer_inverse_curve(self):
        """ColorConverter TRAW_LOG0 解码与 debayer LOG0 逆曲线同式
        （RC7 词表闭环：此前 decode_transfer_to_linear 显式拒绝）。"""
        from src.shared.color.types import ColorTransfer
        from src.shared.media.color_decode import ColorConverter
        from src.shared.video.cfa_cpu_debayer import (
            TRAW_CODE_MAX, TRAW_CURVE_FW_E, TRAW_CURVE_TOE_E,
            traw_log12_constants)

        codes = np.linspace(0.0, 1.0, 257, dtype=np.float32)
        out = ColorConverter().decode_transfer_to_linear(
            codes, ColorTransfer.TRAW_LOG0)
        a, k, c0 = traw_log12_constants()
        cv = codes.astype(np.float64) * TRAW_CODE_MAX
        x = np.where(cv < a, (cv - c0) / k,
                     TRAW_CURVE_TOE_E * (np.exp2(cv / a) - 1.0))
        expected = np.clip(x, 0.0, TRAW_CURVE_FW_E) / TRAW_CURVE_FW_E
        np.testing.assert_allclose(out, expected, atol=1e-6)
        # 端点：码 0 → 0；满码按 PWL 设计略低于满阱（toe 常量项），
        # 与公式逐位一致（上面 allclose 已覆盖，此处只钉边界单调）
        assert out[0] == 0.0
        assert out[-1] > 0.999


# ---------------------------------------------------------------------------
# RC6 端到端：TRAW → 标准档再编码（此前显式拒绝）
# ---------------------------------------------------------------------------


def _cfa_planes_red(seed: int, w: int, h: int, bd: int = 12):
    """红主导内容：R/Gr/Gb 相位高位，B 相位近底——debayer 后应仍偏红。"""
    full = (1 << bd) - 1
    pw, ph = (w + 1) // 2, (h + 1) // 2
    hi = np.full((ph, pw), int(full * 0.72), dtype="<u2")
    lo = np.full((ph, pw), int(full * 0.04), dtype="<u2")
    return [hi, hi, hi, lo]


def _write_traw_source_red(path: Path, frames: int, w: int, h: int,
                           bd: int = 12, crf: int = 39,
                           sensor_matrix_id: int = 3):
    from src.shared.codec.topos_trwm import encode_trwm
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.frame import DecodedFrame

    enc = ToposVideoEncoder(str(path), SimpleNamespace(
        codec="topos", width=w, height=h, fps=24.0,
        pix_fmt=f"topos_cfa{bd}le", profile="raw", crf=crf),
        raw_meta_payload=encode_trwm(
            1.0, 1.0, 1.0, 0.0, 0.0, "RGGB", sensor_matrix_id))
    enc.open()
    for i in range(frames):
        planes = tuple(_cfa_planes_red(i, w, h, bd))
        enc.encode_frame(DecodedFrame(
            frame_index=i, pts=i, time_seconds=i / 24.0, data=None,
            planes=planes, pixel_format=f"topos_cfa{bd}le",
            dtype=np.dtype(np.uint16), width=w, height=h))
    enc.close()


@pytest.mark.skipif(not _lib_available(), reason="libtopos_codec 未构建")
class TestRawStandardTierExport:
    """TRAW 源 → Topos 标准档导出（RC6 出口；此前显式拒绝）。"""

    def test_standard_tier_export_via_debayer(self, tmp_path):
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        w, h, frames = 640, 360, 6
        raw_src = tmp_path / "raw_src.mov"
        _write_traw_source_red(raw_src, frames, w, h)

        # 时间线构建复用 test_topos_raw_direct 的 helper（同构）
        from tests.media.test_topos_raw_direct import _build_raw_timeline
        project, sequence = _build_raw_timeline(raw_src, frames, w, h)

        output = tmp_path / "raw_to_standard.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path != "topos_direct_raw_cpu_encode"

        # 产物可解码 + 红主导内容经 debayer/IDT 后保持红主导
        from src.shared.media.topos_source import (
            ToposMediaSource,
            topos_frame_to_rgb,
        )

        source = ToposMediaSource(str(output))
        try:
            source.open()
            assert int(getattr(source.probe(), "width", 0) or 0) == w
            frame = source.read_frame(0)
            assert frame is not None
            rgb = topos_frame_to_rgb(frame)
            if rgb is not None:
                mean = rgb.reshape(-1, 3).mean(axis=0)
                assert mean[0] > mean[2] * 1.5, (
                    f"红主导内容失守: RGB mean={mean}")
        finally:
            source.close()
