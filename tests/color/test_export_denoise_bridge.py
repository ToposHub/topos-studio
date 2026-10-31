"""P1-COLOR-13 收口：视频导出 GPU 降噪桥（headless context）。

覆盖：
- 参数未激活 / GL 不可用时的直通降级（导出永不因降噪中断）
- GL 可用环境下的真实帧往返（格式保持 + 噪声被压制）
- TimelineExporter 的栈查找与挂点行为
"""

from types import SimpleNamespace

import numpy as np
import pytest

from src.features.color.core.export_denoise_bridge import (
    ExportVideoDenoiseBridge,
)

SPATIAL_ON = {
    "spatial": {
        "enabled": True, "mode": "fast", "luma": 80.0,
        "detail_recovery": 0.0,
    },
    "temporal": {"enabled": False},
}
ALL_OFF = {
    "spatial": {"enabled": False},
    "temporal": {"enabled": False},
}


def _noisy_frame(h=64, w=96, seed=7) -> np.ndarray:
    """灰度渐变底 + 大幅高斯噪声的 BGR uint8 帧。"""
    rng = np.random.default_rng(seed)
    base = np.linspace(40, 200, w, dtype=np.float32)
    base = np.repeat(base[None, :], h, axis=0)
    noisy = base + rng.normal(0.0, 45.0, size=(h, w))
    gray = np.clip(noisy, 0, 255).astype(np.uint8)
    return np.repeat(gray[..., None], 3, axis=2)


class TestDegradePaths:
    def test_inactive_params_passthrough_without_gl(self):
        bridge = ExportVideoDenoiseBridge()
        frame = _noisy_frame()
        out = bridge.process_frame(frame, "clip", 0, ALL_OFF)
        assert out is frame
        assert bridge.available is False  # 未触碰 GL

    def test_empty_and_bad_params_passthrough(self):
        bridge = ExportVideoDenoiseBridge()
        frame = _noisy_frame()
        assert bridge.process_frame(frame, "clip", 0, {}) is frame
        assert bridge.process_frame(frame, "clip", 0, None) is frame
        assert bridge.process_frame(frame, "clip", 0, "junk") is frame

    def test_gl_failure_degrades_to_passthrough(self, monkeypatch):
        import sys
        bridge = ExportVideoDenoiseBridge()
        real_mod = sys.modules.get("moderngl")

        class _Broken:
            @staticmethod
            def create_standalone_context(*a, **k):
                raise RuntimeError("no GL in this environment")

        monkeypatch.setitem(sys.modules, "moderngl", _Broken)
        try:
            frame = _noisy_frame()
            out = bridge.process_frame(frame, "clip", 0, SPATIAL_ON)
            assert out is frame
            assert bridge.available is False
            assert bridge._failed is True
        finally:
            if real_mod is not None:
                monkeypatch.setitem(sys.modules, "moderngl", real_mod)

    def test_release_is_idempotent(self):
        bridge = ExportVideoDenoiseBridge()
        bridge.release()
        bridge.release()
        assert bridge.available is False


def _gl_available() -> bool:
    try:
        import moderngl
        ctx = moderngl.create_standalone_context(require=330)
        ctx.release()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
class TestRealGlRoundTrip:
    def test_format_preserved_and_noise_reduced(self):
        bridge = ExportVideoDenoiseBridge()
        try:
            frame = _noisy_frame(seed=11)
            out = bridge.process_frame(frame, "clip", 0, SPATIAL_ON)
            assert bridge.available is True
            assert out is not frame
            assert out.shape == frame.shape
            assert out.dtype == np.uint8
            assert np.isfinite(out.astype(np.float32)).all()
            # 降噪输出应显著更接近无噪底图
            clean = np.repeat(np.linspace(
                40, 200, frame.shape[1], dtype=np.float32)[None, :],
                frame.shape[0], axis=0)
            err_before = float(np.mean(
                (frame[..., 1].astype(np.float32) - clean) ** 2))
            err_after = float(np.mean(
                (out[..., 1].astype(np.float32) - clean) ** 2))
            assert err_after < err_before * 0.75
        finally:
            bridge.release()

    def test_uint16_round_trip(self):
        bridge = ExportVideoDenoiseBridge()
        try:
            frame = (_noisy_frame().astype(np.float32) * 257).astype(np.uint16)
            out = bridge.process_frame(frame, "clip", 0, SPATIAL_ON)
            assert out.dtype == np.uint16
            assert out.shape == frame.shape
            assert out.max() <= 65535
        finally:
            bridge.release()

    def test_inactive_pipeline_passthrough_keeps_reference(self):
        bridge = ExportVideoDenoiseBridge()
        try:
            frame = _noisy_frame()
            # pipeline 尊重 ConfigManager 全局开关——这里只验证参数侧关闭
            out = bridge.process_frame(frame, "clip", 0, ALL_OFF)
            assert out is frame
        finally:
            bridge.release()


class TestExporterHook:
    """TimelineExporter 挂点：栈查找 + 无参数直通（不建 GL）。"""

    @staticmethod
    def _bare_exporter(project):
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._project = project
        exporter._denoise_bridge = None
        return exporter

    @staticmethod
    def _layer_with_stack(stack_id):
        return SimpleNamespace(
            clip_id="c1",
            source_frame=5,
            clip=SimpleNamespace(color_stack_id=stack_id),
        )

    def test_get_clip_video_denoise_from_stack(self):
        from src.features.edit.core.project import EditProject

        project, stack_id = EditProject.create(name="t").create_color_stack()
        project = project.update_color_stack(
            stack_id, video_denoise=dict(SPATIAL_ON))
        exporter = self._bare_exporter(project)
        assert exporter._get_clip_video_denoise(
            self._layer_with_stack(stack_id)) == SPATIAL_ON

    def test_get_returns_none_without_stack_or_params(self):
        from src.features.edit.core.project import EditProject

        project, stack_id = EditProject.create(name="t").create_color_stack()
        exporter = self._bare_exporter(project)
        assert exporter._get_clip_video_denoise(
            self._layer_with_stack(stack_id)) is None
        assert exporter._get_clip_video_denoise(
            self._layer_with_stack(None)) is None

    def test_apply_passthrough_and_no_bridge_created(self):
        """栈无降噪参数时挂点直通且完全不惰性创建 GL 桥。"""
        from src.features.edit.core.project import EditProject

        project, stack_id = EditProject.create(name="t").create_color_stack()
        exporter = self._bare_exporter(project)
        frame = _noisy_frame()
        out = exporter._apply_video_denoise_export(
            frame, self._layer_with_stack(stack_id))
        assert out is frame
        assert exporter._denoise_bridge is None

    def test_apply_with_none_frame(self):
        from src.features.edit.core.project import EditProject

        project, _ = EditProject.create(name="t").create_color_stack()
        exporter = self._bare_exporter(project)
        assert exporter._apply_video_denoise_export(
            None, SimpleNamespace(clip_id="c", clip=None)) is None
