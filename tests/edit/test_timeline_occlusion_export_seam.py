"""
O3 接缝测试：时间线导出的遮挡裁剪接入共享模块。

用 ``TimelineExporter.__new__`` 直构实例并替换重副作用方法，验证
``_composite_layers``：
- 上轨全屏不透明 → 下轨 ``_get_layer_frame`` 解码请求为 0（§6.2-1）；
- 上轨缩小 → 下轨仍被请求，输出四角为下轨内容（§6.2-2）；
- 同帧开启/关闭 culling 输出逐像素一致（§6.2-4）；
- 源 Alpha / 开关关闭 → fail-closed 保留全部层；
- telemetry 以 mode="export" 记录。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core import timeline_occlusion as oc
from src.features.edit.core.timeline_export import TimelineExporter, TimelineExportConfig

from tests.edit.test_timeline_occlusion import FakeLayer


class FakeAsset:
    media_type = "video"
    metadata = {"pixel_format": "yuv422p10le"}


class FakeProject:
    def __init__(self, assets=None):
        self._assets = assets or {}

    def get_asset_by_id(self, asset_id):
        return self._assets.get(asset_id) or FakeAsset()


def _make_host(
    decode_calls: list,
    frame_size=(48, 64),
    project=None,
    frame_color=(0, 0, 255),
    colors_by_clip=None,
):
    # 直接 __new__ 构造，绕过 __init__ 的重依赖（project/sequence/provider）。
    host = TimelineExporter.__new__(TimelineExporter)
    host._project = project if project is not None else FakeProject()
    host._export_has_alpha = False
    colors_by_clip = colors_by_clip or {}

    def fake_get_layer_frame(layer):
        decode_calls.append(layer.clip_id)
        frame = np.zeros((frame_size[0], frame_size[1], 3), dtype=np.uint8)
        frame[:] = colors_by_clip.get(layer.clip_id, frame_color)
        return frame

    host._get_layer_frame = fake_get_layer_frame
    host._apply_color_grade = lambda frame, layer: frame
    host._apply_video_denoise_export = lambda frame, layer: frame
    host._apply_clip_effects_cpu = lambda frame, layer, idx: frame
    host._apply_crop_cpu = lambda frame, layer: frame
    host._resize_frame = lambda frame, w, h: frame
    host._gpu_yuv420_output_allowed = lambda *a, **k: False
    host._gpu_yuv422p10_output_allowed = lambda *a, **k: False
    host._gpu_render_composite = lambda *a, **k: None
    host._get_output_resolution = lambda config: (frame_size[1], frame_size[0])
    host._get_sequence_resolution = lambda: (frame_size[1], frame_size[0])
    return host


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_CULL", raising=False)
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_RECT", raising=False)
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_DEBUG", raising=False)
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()
    yield
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()


def _red_frame(size=(48, 64)):
    frame = np.zeros((size[0], size[1], 3), dtype=np.uint8)
    frame[:] = (0, 0, 255)
    return frame


class TestExportOcclusion:
    def test_full_opaque_upper_skips_lower_decode(self):
        calls: list = []
        host = _make_host(calls, frame_color=(255, 0, 0))
        lower = FakeLayer("v1", 0)
        upper = FakeLayer("v2", 1)
        result = host._composite_layers(
            (lower, upper), (), TimelineExportConfig(), 0,
        )
        # §6.2-1：下轨解码请求为 0；单层不透明快速路径直接返回上轨帧。
        assert calls == ["v2"]
        assert result is not None
        expected = np.zeros((48, 64, 3), dtype=np.uint8)
        expected[:] = (255, 0, 0)
        assert np.array_equal(np.asarray(result), expected)

    def test_scaled_upper_keeps_lower_and_requests_both(self):
        calls: list = []
        # 下轨红色（BGR 0,0,255）、上轨绿色（BGR 0,255,0）以区分来源。
        host = _make_host(
            calls, colors_by_clip={"v1": (0, 0, 255), "v2": (0, 255, 0)},
        )
        lower = FakeLayer("v1", 0)
        upper = FakeLayer(
            "v2", 1,
            transform=(0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0),
        )
        result = host._composite_layers(
            (lower, upper), (), TimelineExportConfig(), 0,
        )
        # §6.2-2：下轨仍被请求。
        assert calls == ["v1", "v2"]
        # 上层缩小 50%：四角应仍是下层红色（BGR = [0,0,255]）。
        arr = np.asarray(result)
        assert tuple(arr[0, 0]) == (0, 0, 255)
        assert tuple(arr[-1, -1]) == (0, 0, 255)
        # 中心应被上轨绿色覆盖。
        assert tuple(arr[24, 32]) == (0, 255, 0)

    def test_cull_on_and_off_pixel_identical(self):
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        config = TimelineExportConfig()

        calls_on: list = []
        host_on = _make_host(calls_on, frame_color=(30, 60, 90))
        result_on = host_on._composite_layers(layers, (), config, 0)

        calls_off: list = []
        host_off = _make_host(calls_off, frame_color=(30, 60, 90))
        oc._ENV_CACHE["TOPOS_TIMELINE_OCCLUSION_CULL"] = False
        result_off = host_off._composite_layers(layers, (), config, 0)

        assert calls_on == ["v2"]
        assert calls_off == ["v1", "v2"]
        # §6.2-4：同帧开/关裁剪输出逐像素一致。
        assert np.array_equal(np.asarray(result_on), np.asarray(result_off))

    def test_alpha_source_keeps_lower(self):
        calls: list = []
        project = FakeProject(assets={
            "v2": FakeAssetWithAlpha(),
        })
        host = _make_host(calls, project=project)
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1, asset_id="v2"))
        host._composite_layers(layers, (), TimelineExportConfig(), 0)
        assert calls == ["v1", "v2"]

    def test_transitions_keep_all_layers(self):
        calls: list = []
        host = _make_host(calls)
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        host._composite_layers(
            layers, (FakeTransition(),), TimelineExportConfig(), 0,
        )
        assert calls == ["v1", "v2"]

    def test_cull_disabled_env_requests_all(self, monkeypatch):
        monkeypatch.setenv("TOPOS_TIMELINE_OCCLUSION_CULL", "0")
        oc.reset_env_cache()
        calls: list = []
        host = _make_host(calls)
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        host._composite_layers(layers, (), TimelineExportConfig(), 0)
        assert calls == ["v1", "v2"]

    def test_facts_failure_keeps_all(self):
        calls: list = []
        host = _make_host(calls)

        def boom(layer):
            raise RuntimeError("boom")
        host._occlusion_facts_for_layer = boom
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        host._composite_layers(layers, (), TimelineExportConfig(), 0)
        assert calls == ["v1", "v2"]

    def test_telemetry_records_export_mode(self):
        calls: list = []
        host = _make_host(calls)
        host._composite_layers(
            (FakeLayer("v1", 0), FakeLayer("v2", 1)),
            (), TimelineExportConfig(), 33,
        )
        snap = oc.get_occlusion_telemetry().snapshot()
        assert snap["modes"] == {"export": 1}
        assert snap["culled_layers"] == 1

    def test_nested_single_layer_passthrough_untouched(self):
        # 单层时间线：无裁剪候选，输出等于该层帧（现有行为不变）。
        calls: list = []
        host = _make_host(calls, frame_color=(10, 20, 30))
        solo = FakeLayer("v1", 0)
        result = host._composite_layers(
            (solo,), (), TimelineExportConfig(), 0,
        )
        assert calls == ["v1"]
        assert np.array_equal(np.asarray(result), _red_frame() * 0 + (10, 20, 30))


class FakeAssetWithAlpha(FakeAsset):
    metadata = {"pixel_format": "yuva444p10le"}


class FakeTransition:
    """满足 _composite_layers 迭代契约的"未知类型"转场替身。"""

    transition_type = "unknown_type"
    left_clip_id = ""
    right_clip_id = ""
