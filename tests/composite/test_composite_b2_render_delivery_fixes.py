"""B2 批次（可靠渲染与交付）focused 回归测试。

对应 2026-10-14 复合页审计计划的 R-01～R-13（本批次范围）：
上传位宽/alpha 预乘、输出色彩合同（full-range 中间值/通道序/位深/
HDR 标签）、交付完整性状态（complete/partial/cancelled/failed）、
序列资源清理/串行回退、导出项目快门。
真实 GPU 像素验证（Paint 8×8 生产 pool）已由审计复现脚本复核，此处
锁定可离线稳定断言的合同单元。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core

import numpy as np


# ── R-01：统一 CPU→纹理上传 ────────────────────────────────────────────


class TestUploadPooledTexture:
    def _fake_pool(self, acquire_dtype="f2"):
        class Tex:
            def __init__(self, dtype):
                self.dtype = dtype
                self.written = None
                self.released = False

            def write(self, payload):
                expected = {
                    "f2": 2, "f4": 4}[self.dtype] * 4 * 8 * 8
                if len(payload) != expected:
                    raise ValueError(
                        f"data size mismatch {len(payload)} != {expected}")
                self.written = payload

        class Pool:
            def __init__(self):
                self.acquired = []
                self.released = []

            def acquire_texture(self, w, h, components=4, dtype=None):
                tex = Tex(dtype or acquire_dtype)
                self.acquired.append(tex)
                return tex

            def release_texture(self, tex, force_evict=False):
                self.released.append(tex)

        return Pool()

    def test_r01_f4_array_into_f2_pool_converts(self):
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )

        pool = self._fake_pool(acquire_dtype="f2")
        array = np.ones((8, 8, 4), dtype=np.float32)

        tex = upload_pooled_texture(pool, array, 8, 8, dtype="f2")

        # f4 数组已转换为 f2——不再 data-size mismatch（旧路径直接抛错
        # 并被上层当作“Paint 失败”静默直通）。
        assert tex.written is not None
        assert len(pool.acquired) == 1
        assert pool.released == []

    def test_r01_write_failure_returns_texture_to_pool(self):
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )

        pool = self._fake_pool(acquire_dtype="f2")

        class BrokenTex:
            dtype = "f2"

            def write(self, payload):
                raise ValueError("GL write failed")

        pool.acquire_texture = lambda w, h, components=4, dtype=None: BrokenTex()
        array = np.ones((8, 8, 4), dtype=np.float32)
        broken = BrokenTex()
        pool.acquire_texture = lambda w, h, components=4, dtype=None: broken

        with pytest.raises(ValueError):
            upload_pooled_texture(pool, array, 8, 8, dtype="f2")
        # write 失败时新获取的纹理归还 pool，而不是泄漏 active 计数
        assert pool.released == [broken]

    def test_r01_f4_passthrough_no_conversion(self):
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )

        pool = self._fake_pool(acquire_dtype="f4")
        array = np.ones((8, 8, 4), dtype=np.float32)
        tex = upload_pooled_texture(pool, array, 8, 8, dtype="f4")
        assert tex.written is not None


# ── R-03/R-06：色彩/位深打包合同 ───────────────────────────────────────


class TestColorPackingContract:
    def test_r03_intermediate_descriptor_forced_full(self):
        from src.features.composite.render.composite_render_output import (
            _rgb_intermediate_descriptor,
        )
        from src.shared.color.color_space_descriptor import REC709_VIDEO
        from src.shared.color.types import ColorRange

        inter = _rgb_intermediate_descriptor(REC709_VIDEO)
        assert inter.range == ColorRange.FULL
        assert inter.primaries == REC709_VIDEO.primaries
        assert inter.transfer == REC709_VIDEO.transfer
        # full 输入原样返回
        assert _rgb_intermediate_descriptor(inter) is inter

    def test_r06_bit_depth_from_pix_fmt(self):
        from src.features.composite.render.composite_render_output import (
            _pix_fmt_bit_depth,
        )

        assert _pix_fmt_bit_depth("yuv420p") == 8
        assert _pix_fmt_bit_depth("yuv420p10le") == 10
        assert _pix_fmt_bit_depth("yuv422p12le") == 12
        assert _pix_fmt_bit_depth("") == 8

    def test_r06_packing_depth_selection(self):
        from src.features.composite.render.composite_render_output import (
            _pack_encoded_rgb,
        )

        arr = np.linspace(0, 1, 12, dtype=np.float32).reshape(2, 2, 3)
        high, high_fmt = _pack_encoded_rgb(arr, high_depth=True)
        assert high.dtype == np.uint16 and high_fmt == "bgr48le"
        low, low_fmt = _pack_encoded_rgb(arr, high_depth=False)
        assert low.dtype == np.uint8 and low_fmt == "bgr24"


# ── R-05：图片序列原色矩阵通道序 ───────────────────────────────────────


class TestImageSequencePrimariesOrder:
    def test_r05_p3_red_bgr_uses_rgb_matrix(self):
        from src.features.composite.render.composite_render_output import (
            _convert_for_image_ext,
        )
        from src.shared.color.color_space_descriptor import DISPLAY_P3_SRGB

        class Config:
            def get(self, k, default=None):
                return ('builtin' if k == 'composite.color_engine'
                        else default)

        linear_bgr = np.zeros((2, 2, 3), np.float32)
        linear_bgr[..., 2] = 1.0  # BGR 读回的红色

        out = _convert_for_image_ext(
            linear_bgr, "tif", DISPLAY_P3_SRGB, Config())

        # 审计期望：P3 红色 BGR16 = [9080, 13125, 60127]（旧行为把 BGR
        # 当 RGB 喂原色矩阵，输出 [0, 0, 62886]）。
        assert out.dtype == np.uint16
        assert np.allclose(out[0, 0], [9080, 13125, 60127], atol=40)


# ── R-04：BT.2020 编码通道序（PyAV 精确转换路径）──────────────────────


class TestBT2020ChannelOrder:
    def test_r04_bgr_input_red_y(self):
        from src.shared.export.video_encoder import (
            FFmpegVideoEncoder, FFmpegEncoderConfig, ColorMetadataConfig,
        )
        from src.shared.media.metadata import Rational
        from src.shared.media.frame import DecodedFrame

        enc = FFmpegVideoEncoder('/tmp/not-created-r04.mp4', FFmpegEncoderConfig(
            codec='hevc', width=8, height=8, fps=Rational(24, 1),
            pix_fmt='yuv420p10le',
            color_metadata=ColorMetadataConfig.hdr10()))
        red_bgr = np.zeros((8, 8, 3), np.uint16)
        red_bgr[..., 2] = 65535  # BGR 红色

        frame = DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                             data=red_bgr, pixel_format='bgr48le',
                             width=8, height=8)
        converted = enc._convert_packed_frame(frame, 'yuv420p10le')

        y = int(np.frombuffer(converted.planes[0], np.uint16)[0])
        # BT.2020 红 Y≈294（旧行为把通道 0 当 R：蓝的权重 → Y≈116）。
        assert 280 <= y <= 310


# ── R-08/R-09/R-10：交付完整性状态 ─────────────────────────────────────


class _FakeEncoder:
    def __init__(self, path=None, config=None):
        self.frames = []
        self.closed = False

    def open(self):
        pass

    def close(self):
        self.closed = True

    def encode_frame(self, frame):
        self.frames.append(frame)


class TestDeliveryStatusContract:
    def _render_video(self, monkeypatch, tmp_path, render_one, end_frame=2,
                      cancelled=lambda: False):
        import src.shared.export.video_encoder as ve
        import src.shared.color.color_transform_provider as ctp
        from src.features.composite.render.composite_render_output import (
            CompositeRenderOutput,
        )

        monkeypatch.setattr(ve, "FFmpegVideoEncoder", _FakeEncoder)
        monkeypatch.setattr(
            ctp, "transform_color",
            lambda arr, src, dst, config_manager=None: arr)
        out = CompositeRenderOutput()
        monkeypatch.setattr(out, "_resolve_parallel_frames", lambda n: 1)
        monkeypatch.setattr(out, "_render_one_frame", render_one)
        monkeypatch.setattr(out, "_cleanup_frame_renderer", lambda: None)
        return out.render_video(
            graph_data={}, output_path=str(tmp_path / "v.mp4"),
            width=4, height=4, fps=24.0, start_frame=0, end_frame=end_frame,
            is_cancelled=cancelled)

    def test_r09_all_frames_failed_is_failed(self, monkeypatch, tmp_path):
        result = self._render_video(
            monkeypatch, tmp_path, lambda **kw: None, end_frame=2)
        assert result.success is False
        assert result.status == "failed"
        assert result.frames_dropped == 3

    def test_r08_complete_when_all_rendered(self, monkeypatch, tmp_path):
        frame = np.full((4, 4, 3), 0.5, np.float32)
        result = self._render_video(
            monkeypatch, tmp_path, lambda **kw: frame, end_frame=2)
        assert result.success is True
        assert result.status == "complete"

    def test_r08_midstream_cancel_is_partial(self, monkeypatch, tmp_path):
        flag = {"v": False}

        def render_one(**kw):
            if kw["timeline_frame"] >= 1:
                flag["v"] = True
                return None
            return np.full((4, 4, 3), 0.5, np.float32)

        result = self._render_video(
            monkeypatch, tmp_path, render_one, end_frame=3,
            cancelled=lambda: flag["v"])
        assert result.success is False
        assert result.status == "partial"
        assert result.error == "EXPORT_CANCELLED"

    def test_r10_sequence_missing_frame_is_partial(self, monkeypatch,
                                                   tmp_path):
        from src.features.composite.render.composite_render_output import (
            CompositeRenderOutput,
        )
        from src.shared.io.file_io import FileIO

        saved = []

        def fake_save(path, img, options=None):
            saved.append(path)
            return True

        monkeypatch.setattr(FileIO, "save_image",
                            staticmethod(fake_save))
        out = CompositeRenderOutput()
        cleaned = []
        monkeypatch.setattr(out, "_cleanup_frame_renderer",
                            lambda: cleaned.append(1))
        monkeypatch.setattr(out, "_resolve_parallel_frames", lambda n: 1)

        def render_one(**kw):
            if kw["timeline_frame"] == 1:
                return None  # 中间帧渲染失败
            return np.full((4, 4, 3), 0.5, np.float32)

        monkeypatch.setattr(out, "_render_one_frame", render_one)
        result = out.render_image_sequence(
            graph_data={}, output_dir=str(tmp_path), base_name="x",
            ext="png", start_frame=0, end_frame=2)

        # 3 帧请求、2 帧成功 → partial，不报 success；缺帧计入 dropped
        assert result.success is False
        assert result.status == "partial"
        assert result.frames_written == 2
        assert result.frames_dropped == 1
        assert len(saved) == 2
        # R-11：串行序列出口释放渲染器（3 次渲染调用后清理一次）
        assert cleaned == [1]

    def test_r10_sequence_all_failed_is_failed(self, monkeypatch, tmp_path):
        from src.features.composite.render.composite_render_output import (
            CompositeRenderOutput,
        )

        out = CompositeRenderOutput()
        monkeypatch.setattr(out, "_resolve_parallel_frames", lambda n: 1)
        monkeypatch.setattr(out, "_render_one_frame", lambda **kw: None)
        result = out.render_image_sequence(
            graph_data={}, output_dir=str(tmp_path), base_name="x",
            ext="png", start_frame=0, end_frame=2)
        assert result.success is False
        assert result.status == "failed"


# ── R-13：导出项目运动快门 ─────────────────────────────────────────────


class TestExportProjectShutter:
    def test_r13_shutter_survives_context_rebuild(self):
        from types import SimpleNamespace
        from src.features.composite.render.composite_export_renderer import (
            CompositeExportRenderer,
        )
        from src.features.composite.core.eval_context import CompositeEvalContext
        from src.features.composite.core.graph_snapshot import (
            GraphSnapshot, NodeSnapshot,
        )
        from src.features.composite.nodes.base_node import TextureResult

        r = CompositeExportRenderer(context=SimpleNamespace(info={}))
        r._persistent = True
        r.fb_manager = object()
        r._resource_pool = object()
        r._cache_manager = object()
        r._eval_ctx = CompositeEvalContext(gl=r.ctx)
        r._scheduler = SimpleNamespace(
            plan=lambda *a, **k: SimpleNamespace(
                output_dimensions=(1, 1), needs_tiling=False))
        r._sync_render_nodes = lambda snapshot: None

        observed = []

        def evaluate(**kwargs):
            observed.append(r._eval_ctx.motion_blur_shutter)
            return TextureResult(texture_id=1, width=1, height=1)

        r._evaluate_snapshot = evaluate
        r._readback = lambda result: np.ones((1, 1, 3), np.float32)
        ns = NodeSnapshot('out', 'output', {'motion_blur_shutter': 0.9},
                          False, [], [])
        snapshot = GraphSnapshot(generation=1, nodes={'out': ns})

        # _ensure_gpu_resources 会在渲染中重建 ctx——pending shutter
        # 必须被新 ctx 继承（旧行为新 ctx 一律 None）。
        def ensure():
            r._install_eval_context()
        r._ensure_gpu_resources = ensure

        result = r.render_and_readback(snapshot)

        assert result.error is None
        assert observed and all(s == 0.9 for s in observed), observed
