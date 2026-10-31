"""wd12-g62 批 D 修复回归测试（AUD-18322/18401-18409/18501/18502/18508/
18601/18602/18700）。

对应修复报告：docs/plans/2026-09-28-multi-machine-program/fixes/reports/
wd12-g62.md。除标注外均不依赖 GPU/Metal 设备可用性（18601 的设备相关
路径由 tests/edit/test_timeline_metal_export_renderer.py 中按锁定旧行为
协议更新的用例覆盖）。
"""

from __future__ import annotations

import threading
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportOutcome,
    ExportPreflightResult,
    ExportRangeType,
    TimelineExportConfig,
    TimelineExporter,
    _ManifestSnapshot,
)
from src.features.edit.core.timeline_frame_mapper import (
    FrameMapRequest,
    map_frame,
    map_vfr_frame,
)


# =====================================================================
# AUD-18404 / AUD-18405：配置层校验
# =====================================================================


def test_aud_18404_png_tiff_sequence_rejected_at_validation(tmp_path):
    """W07 订正：AUD-18404 的拒绝已被 G14（第六轮审计）撤销——PNG/TIFF
    标准序列有了专用编码器（StandardImageSequenceEncoder），旧断言
    「校验期拒绝」描述的实现已不存在。现行契约：三种序列格式配置均
    通过校验（不再出现 not supported）；位深/帧号 padding 的序列专属
    校验仍生效（见后续用例）。"""
    for codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE,
                  ExportCodec.TOPOS_IMAGE_SEQUENCE):
        config = TimelineExportConfig(
            codec=codec, output_path=str(tmp_path / "seq_dir"))
        errors = config.validate()
        assert not [e for e in errors if "not supported" in e], (codec, errors)


def test_aud_18405_negative_start_frame_rejected(tmp_path):
    config = TimelineExportConfig(
        range_type=ExportRangeType.CUSTOM,
        start_frame=-50,
        end_frame=-1,
        codec=ExportCodec.PRORES_422,
        output_path=str(tmp_path / "out.mov"),
    )
    errors = config.validate()
    assert any("start_frame must be >= 0" in e for e in errors)


def _make_stub_sequence(duration: int = 100) -> SimpleNamespace:
    track = SimpleNamespace(duration_frames=duration, clips=())
    return SimpleNamespace(
        name="stub",
        duration_frames=duration,
        video_tracks=(track,),
        audio_tracks=(),
        subtitle_tracks=(),
        format_settings={},
        range_start=None,
        range_end=None,
    )


def test_aud_18405_empty_custom_range_fails_structured(tmp_path, monkeypatch):
    """start ≥ 时长：CUSTOM 范围解析为空 → 结构化 EXPORT_EMPTY_FRAME_RANGE，
    不再空循环后交付 0 帧成功文件。"""
    import src.features.edit.core.timeline_export as te

    monkeypatch.setattr(
        te, "preflight_audio_export",
        lambda config, sequence: ExportPreflightResult(ok=True))
    monkeypatch.setattr(
        te, "preflight_video_effects_export",
        lambda sequence: ExportPreflightResult(ok=True))
    monkeypatch.setattr(
        te, "preflight_caption_graphics_export",
        lambda sequence, project: ExportPreflightResult(ok=True))

    import src.features.edit.core.delivery_qc as dq
    monkeypatch.setattr(dq, "run_delivery_qc", lambda seq, proj: None)

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = SimpleNamespace(
        get_asset_by_id=lambda asset_id: None, compositions={})
    exporter._sequence = _make_stub_sequence(duration=100)
    exporter._frame_provider = None
    exporter._cancel_requested = False
    exporter._cancel_pending_before_start = False

    config = TimelineExportConfig(
        timeline_id="t1",
        range_type=ExportRangeType.CUSTOM,
        start_frame=1000,
        end_frame=-1,
        codec=ExportCodec.PRORES_422,
        output_path=str(tmp_path / "out.mov"),
        include_audio=False,
        burn_subtitles=False,
    )
    outcome = exporter._export_timeline_dispatch(config)
    assert isinstance(outcome, ExportOutcome)
    assert not outcome.success
    assert outcome.error_code == "EXPORT_EMPTY_FRAME_RANGE"
    assert exporter._is_exporting is False


# =====================================================================
# AUD-18401：direct TOPOS 异常路径汇合音频线程
# =====================================================================


class _AudioThreadStub:
    """存活线程替身：is_alive()=True，join 立即返回并记录调用。"""

    def __init__(self) -> None:
        self.joined = False

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def test_aud_18401_direct_topos_exception_joins_audio_thread(monkeypatch):
    import src.shared.export.topos_encoder as topos_encoder

    class _FailingEncoder:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open(self) -> None:
            raise RuntimeError("synthetic encoder open failure")

        def abort(self) -> None:
            pass

    monkeypatch.setattr(topos_encoder, "ToposVideoEncoder", _FailingEncoder)

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._export_telemetry = None
    exporter._gpu_trail = None
    exporter._open_clips = {}
    exporter._export_rgb8_sources = {}
    exporter._export_high_depth_sources = {}
    exporter._comp_frame_renderer = None
    audio_thread = _AudioThreadStub()
    exporter._audio_render_thread = audio_thread
    exporter._audio_master_pcm = np.zeros((2, 8), dtype=np.float32)
    exporter._audio_render_error = RuntimeError("stale")

    plan = SimpleNamespace(
        source_pixel_format="yuv422p10le",
        width=64, height=48, target_width=64, target_height=48,
        frames=4, asset_path="x.topos",
        decoder_resize=False, requires_planar_conversion=False,
    )
    config = TimelineExportConfig(output_path="out.topos")
    outcome = exporter._export_direct_topos(plan, config, SimpleNamespace())

    assert not outcome.success
    assert outcome.error_code == "EXPORT_FAILED"
    # 音频线程已汇合并清引用；共享音频状态已清（不泄漏到下一次导出）。
    assert audio_thread.joined is True
    assert exporter._audio_render_thread is None
    assert exporter._audio_master_pcm is None
    assert exporter._audio_render_error is None


# =====================================================================
# AUD-18402：manifest 后台写线程使用一次性状态快照
# =====================================================================


def test_aud_18402_manifest_async_uses_entry_snapshot(tmp_path, monkeypatch):
    import src.features.edit.core.timeline_export as te
    import src.features.edit.core.export_manifest as em

    captured: list[_ManifestSnapshot] = []
    original_sequence = _make_stub_sequence(duration=10)
    original_sequence.name = "original"

    def fake_build(project, sequence, config, **kwargs):
        captured.append(
            _ManifestSnapshot(
                project=project,
                sequence=sequence,
                qc_report=kwargs.get("qc_report"),
                preflight_result=kwargs.get("preflight_result"),
                loudness_report=kwargs.get("loudness_report"),
                effect_export_skips=kwargs.get("effect_export_skips") or (),
            ))
        return {"ok": True}

    monkeypatch.setattr(em, "build_export_manifest", fake_build)
    monkeypatch.setattr(em, "manifest_path_for", lambda path: "m.json")
    monkeypatch.setattr(em, "write_manifest_file", lambda path, manifest: None)

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = SimpleNamespace()
    exporter._sequence = original_sequence
    exporter._export_telemetry = None
    exporter._last_qc_report = "qc-original"
    exporter._last_preflight_result = "preflight-original"
    exporter._last_loudness_report = None
    exporter._effect_export_skips = ["fx-original"]
    exporter._manifest_lock = threading.Lock()
    exporter._delivery_finalized = threading.Event()
    exporter._delivery_finalized.clear()
    exporter._last_export_manifest = None

    config = TimelineExportConfig(output_path=str(tmp_path / "out.mov"))
    exporter._write_success_manifest_async(config)

    # cheap 写已同步完成，使用入口快照。
    assert len(captured) == 1
    assert captured[0].sequence is original_sequence
    assert captured[0].qc_report == "qc-original"
    assert captured[0].effect_export_skips == ("fx-original",)

    # 子导出循环改写全部共享状态（individual-clips 场景）。
    mutated_sequence = _make_stub_sequence(duration=5)
    mutated_sequence.name = "mutated"
    exporter._sequence = mutated_sequence
    exporter._last_qc_report = "qc-mutated"
    exporter._last_preflight_result = "preflight-mutated"
    exporter._effect_export_skips = ["fx-mutated"]

    exporter._delivery_finalized.set()
    assert te.wait_for_manifest_writers(timeout=10.0) is True

    assert len(captured) == 2  # 后台全量写只发生一次
    background = captured[1]
    assert background.sequence is original_sequence
    assert background.sequence is not mutated_sequence
    assert background.qc_report == "qc-original"
    assert background.preflight_result == "preflight-original"
    assert background.effect_export_skips == ("fx-original",)


# =====================================================================
# AUD-18403：CPU 转场侧帧补齐完整处理链（主循环同序）
# =====================================================================


class _TransitionStub:
    def __init__(self, transition_type: str = "wipe_left") -> None:
        self.transition_type = transition_type
        self.blend_factor = 0.5
        self.transition = SimpleNamespace(params={})


def test_aud_18403_transition_side_frame_runs_full_chain():
    exporter = TimelineExporter.__new__(TimelineExporter)
    order: list[str] = []
    frame = np.full((8, 12, 3), 128, dtype=np.uint8)

    exporter._apply_video_denoise_export = (
        lambda f, layer: (order.append("denoise"), f)[1])
    exporter._apply_color_grade = (
        lambda f, layer: (order.append("grade"), f)[1])
    exporter._apply_clip_effects_cpu = (
        lambda f, layer, idx: (order.append("effects"), f)[1])
    exporter._resize_frame = (
        lambda f, w, h: (order.append("resize"), f)[1])
    exporter._apply_crop_cpu = (
        lambda f, layer: (order.append("crop_matte"), f)[1])
    exporter._get_sequence_resolution = lambda: (12, 8)

    layer = SimpleNamespace(
        clip=SimpleNamespace(speed=None),
        crop=(0.0, 0.0, 0.0, 0.0, 0.0),
        transform=(0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
    )
    result = exporter._prepare_transition_side_frame(
        layer, frame, _TransitionStub(), 3, 12, 8)

    # 与主循环同序：denoise → grade → effects → resize → 视口 crop。
    assert order == ["denoise", "grade", "effects", "resize", "crop_matte"]
    assert result.shape == frame.shape
    assert result.dtype == np.uint8


def test_aud_18403_transition_side_frame_applies_crop():
    from src.features.edit.core.crop_cpu import crop_is_identity

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._apply_video_denoise_export = lambda f, layer: f
    exporter._apply_color_grade = lambda f, layer: f
    exporter._apply_clip_effects_cpu = lambda f, layer, idx: f
    exporter._resize_frame = lambda f, w, h: f
    exporter._apply_crop_cpu = lambda f, layer: None  # 哨兵：必须被调用
    exporter._get_sequence_resolution = lambda: (12, 8)

    layer = SimpleNamespace(
        clip=SimpleNamespace(speed=None),
        crop=(2.0, 2.0, 0.0, 0.0, 0.0),
        transform=(0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
    )
    assert not crop_is_identity(layer.crop)
    # 哨兵 _apply_crop_cpu 返回 None → 链条确实经过视口 crop 阶段。
    assert exporter._prepare_transition_side_frame(
        layer, np.zeros((8, 12, 3), np.uint8), _TransitionStub(), 0, 12, 8,
    ) is None


# =====================================================================
# AUD-18406：native VT bridge 拒绝非整数 fps
# =====================================================================


def test_aud_18406_bridge_rejects_non_integer_fps(monkeypatch, tmp_path):
    import src.features.edit.core.timeline_export as te
    import src.shared.gpu.metal.context as metal_context
    import src.shared.media.macos_vt.gpu_export_bridge as bridge_mod
    from src.shared.media.metadata import Rational

    monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
    monkeypatch.setattr(
        te, "can_use_native_gpu_bridge",
        lambda *args, **kwargs: SimpleNamespace(
            eligible=True, required_output_surface="nv12"))
    # 阻止真实 Metal/VT 资源创建（integer-fps 用例会走到 bridge 构造）。
    monkeypatch.setattr(
        metal_context, "MetalComputeContext",
        lambda: SimpleNamespace())
    monkeypatch.setattr(
        bridge_mod, "VideoToolboxGpuExportBridge",
        lambda *args, **kwargs: SimpleNamespace(handles_timeline_video=False))

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._gpu_encode_surface_provider = None
    exporter._sequence = _make_stub_sequence()
    exporter._project = None
    exporter._export_telemetry = None
    exporter._runtime_capability_snapshot = None
    exporter._active_export_config = None
    exporter._bridge_selection = {"selected": False}

    config = SimpleNamespace(
        fps=Rational(24000, 1001), codec="h264",
        width=1920, height=1080,
    )
    exporter._maybe_create_default_native_gpu_bridge(
        str(tmp_path / "out.mp4"), config)

    assert exporter._gpu_encode_surface_provider is None
    assert exporter._bridge_selection["selected"] is False
    assert exporter._bridge_selection["reason_code"] == "non_integer_fps"

    # 整数 fps 不被该门拒绝：继续协商并被（stub）bridge 接管。
    exporter._bridge_selection = {"selected": False}
    integer_config = SimpleNamespace(
        fps=Rational(24, 1), codec="h264", width=1920, height=1080)
    exporter._maybe_create_default_native_gpu_bridge(
        str(tmp_path / "out.mov"), integer_config)
    assert exporter._bridge_selection.get("reason_code") != "non_integer_fps"
    exporter._gpu_encode_surface_provider = None


# =====================================================================
# AUD-18407：yuv422p10 门逐帧活跃字幕判据
# =====================================================================


def _gate_exporter(active_subtitle_frames: set[int] | None):
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._export_has_alpha = False
    exporter._encoder = SimpleNamespace(_config=SimpleNamespace(
        codec="topos", pix_fmt="yuv422p10le"))
    exporter._export_color_metadata = SimpleNamespace(
        primaries="bt709", transfer="bt709", matrix="bt709", range="limited")

    def get_active(frame_idx):
        if active_subtitle_frames is None:
            raise AttributeError("broken sequence")
        return (object(),) if frame_idx in active_subtitle_frames else ()

    exporter._sequence = SimpleNamespace(
        format_settings={}, get_active_subtitle_clips=get_active)
    return exporter


def test_aud_18407_yuv422p10_gate_per_frame_subtitles():
    exporter = _gate_exporter({3})
    config = SimpleNamespace(burn_subtitles=True)
    # 默认 burn_subtitles=True 且当前帧无活跃字幕 → 允许（此前一刀切拒绝）。
    assert exporter._gpu_yuv422p10_output_allowed(
        config, 192, 108, frame_idx=0)
    # 有活跃字幕帧 → 拒绝（BGR 烧录路径）。
    assert not exporter._gpu_yuv422p10_output_allowed(
        config, 192, 108, frame_idx=3)
    # 查询异常 → fail-closed。
    broken = _gate_exporter(None)
    assert not broken._gpu_yuv422p10_output_allowed(
        config, 192, 108, frame_idx=0)
    # 几何契约维持既有拒绝。
    assert not exporter._gpu_yuv422p10_output_allowed(config, 191, 108)


# =====================================================================
# AUD-18408：静态图层帧缓存
# =====================================================================


def test_aud_18408_image_frame_cached_and_copy_safe(monkeypatch, tmp_path):
    import cv2

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._export_has_alpha = False

    image_path = str(tmp_path / "plate.png")
    cv2.imwrite(image_path, np.full((6, 8, 3), 90, dtype=np.uint8))

    calls: list[str] = []
    real_imread = cv2.imread

    def counting_imread(path, flags):
        calls.append(str(path))
        return real_imread(path, flags)

    monkeypatch.setattr(cv2, "imread", counting_imread)

    first = exporter._get_image_frame(image_path)
    second = exporter._get_image_frame(image_path)
    assert first is not None and second is not None
    assert len(calls) == 1  # 第二次命中缓存，无磁盘解码
    # 返回副本：调用方原地写不污染缓存。
    first[:] = 0
    assert int(second.max()) == 90
    assert int(exporter._get_image_frame(image_path).max()) == 90

    # alpha 模式变化 → 键不同 → 重新解码。
    exporter._export_has_alpha = True
    cv2.imwrite(image_path, np.full((6, 8, 4), 60, dtype=np.uint8))
    rgba = exporter._get_image_frame(image_path)
    assert rgba is not None and rgba.shape[2] == 4
    assert len(calls) == 2

    # _cleanup_clips 释放缓存 → 下一次访问重新解码。
    exporter._open_clips = {}
    exporter._export_rgb8_sources = {}
    exporter._export_high_depth_sources = {}
    exporter._comp_frame_renderer = None
    exporter._export_has_alpha = False
    exporter._cleanup_clips()
    assert exporter._get_image_frame(image_path) is not None
    assert len(calls) == 3


def test_aud_18408_image_frame_cache_is_bounded(monkeypatch, tmp_path):
    import cv2

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._export_has_alpha = False
    calls: list[str] = []
    monkeypatch.setattr(
        cv2, "imread",
        lambda path, flags: (calls.append(str(path)),
                             np.full((4, 4, 3), 7, np.uint8))[1])

    paths = [str(tmp_path / f"img_{i}.png") for i in range(10)]
    for path in paths:
        assert exporter._get_image_frame(path) is not None
    assert len(calls) == 10
    assert len(exporter._image_frame_cache) == exporter._IMAGE_FRAME_CACHE_MAX
    # 最旧的条目被逐出 → 再次访问重新解码；最新条目命中缓存。
    exporter._get_image_frame(paths[0])
    assert len(calls) == 11
    exporter._get_image_frame(paths[-1])
    assert len(calls) == 11


# =====================================================================
# AUD-18409：效果/调色运行期失败进入交付审计 / fail-fast
# =====================================================================


def test_aud_18409_effect_runtime_failure_recorded_in_skips(monkeypatch):
    import src.features.edit.core.timeline_export as te

    def failing_handler(frame, params):
        raise ValueError("synthetic effect failure")

    monkeypatch.setitem(
        te._CPU_EFFECT_HANDLERS, "wd12_fake_fx", failing_handler)

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._effect_export_skips = []
    effect = SimpleNamespace(
        effect_type="wd12_fake_fx", order=0, is_enabled=True, params={})
    layer = SimpleNamespace(
        clip=SimpleNamespace(effects=(effect,)), clip_id="c1")

    frame = np.full((4, 4, 3), 100, dtype=np.uint8)
    result = exporter._apply_clip_effects_cpu(frame, layer, 0)
    assert result is frame  # 帧保持未处理（既有语义）
    assert any(
        entry.startswith("wd12_fake_fx (runtime_failure:")
        for entry in exporter._effect_export_skips
    ), exporter._effect_export_skips


def test_aud_18409_color_grade_failure_fails_fast(monkeypatch):
    import src.features.color.core.the_decompiler as decompiler

    def raising_apply(cls_self, rgb, params):
        raise RuntimeError("synthetic grade failure")

    monkeypatch.setattr(
        decompiler.GradingMath, "apply_grading", raising_apply)

    exporter = TimelineExporter.__new__(TimelineExporter)
    # 强制走通用 float 路径（绕过 u8 LUT 快路径）。
    exporter._get_clip_color_grade = lambda layer: {
        "grading_params": {"saturation": 0.5},
        "grade_keyframes": None,
    }
    exporter._get_decomposed_grade_fastpath = lambda params: None
    layer = SimpleNamespace(clip_id="c9")
    frame = np.full((4, 4, 3), 100, dtype=np.uint8)
    with pytest.raises(ValueError, match="c9"):
        exporter._apply_color_grade(frame, layer)


# =====================================================================
# AUD-18501 / AUD-18502：GPU 导出渲染器
# =====================================================================


def _gpu_renderer_stub():
    from src.features.edit.core.timeline_gpu_export_renderer import (
        TimelineGpuExportRenderer,
    )
    renderer = TimelineGpuExportRenderer.__new__(TimelineGpuExportRenderer)
    renderer.gl_errors = 0
    renderer._warned_render_error = False
    return renderer


def test_aud_18501_effect_chain_failure_warns_once_and_counts(caplog):
    import logging as _logging

    renderer = _gpu_renderer_stub()
    with caplog.at_level(_logging.DEBUG):
        renderer._note_effect_chain_failure("stage_a", RuntimeError("boom"))
        renderer._note_effect_chain_failure("stage_b", RuntimeError("boom2"))
    assert renderer.gl_errors == 2
    warnings = [r for r in caplog.records
                if r.levelno == _logging.WARNING
                and "stage_a" in r.getMessage()]
    assert len(warnings) == 1  # 只告警一次
    assert any("stage_b" in r.getMessage() for r in caplog.records
               if r.levelno == _logging.DEBUG)


def test_aud_18501_deep_u16_bridge_failure_observable():
    renderer = _gpu_renderer_stub()
    renderer._effect_runner = SimpleNamespace()
    renderer._prog_u16_to_f2 = object()
    renderer._prog_f2_to_u16 = object()
    renderer._ensure_f2_pair = lambda width, height: True
    renderer._ctx = SimpleNamespace(TRIANGLE_STRIP=5)
    renderer._fbo_f2 = (SimpleNamespace(use=lambda: None),
                        SimpleNamespace(use=lambda: None))

    class _BrokenTex:
        @staticmethod
        def use(unit):
            raise RuntimeError("texture use failed")

    renderer._vao = {"u16_to_f2": SimpleNamespace(
        render=lambda mode: (_ for _ in ()).throw(
            RuntimeError("unreachable")))}
    result = renderer._apply_effect_chain_deep_u16(
        _BrokenTex, (("spec", {}),), 8, 8, out_slot=1)
    assert result is None
    assert renderer.gl_errors == 1


def test_aud_18502_off_thread_release_does_not_latch(caplog):
    import logging as _logging

    from src.features.edit.core.timeline_gpu_export_renderer import (
        TimelineGpuExportRenderer,
    )
    renderer = TimelineGpuExportRenderer.__new__(TimelineGpuExportRenderer)
    renderer._released = False
    renderer._ctx = object()  # 非 None：存在待释放资源
    renderer._owner_thread = threading.get_ident() ^ 0x7FFFFFFF
    renderer.frames_rendered = 0
    with caplog.at_level(_logging.WARNING):
        renderer.release()
    # 跨线程分支：告警但不闩死——创建线程稍后仍可合法释放。
    assert renderer._released is False
    assert renderer._ctx is not None
    assert any("off the creating thread" in r.getMessage()
               for r in caplog.records)

    # 创建线程重试 → 真正释放并闩死。
    released: list[str] = []

    class _CtxStub:
        def release(self) -> None:
            released.append("ctx")

    renderer._owner_thread = threading.get_ident()
    renderer._ctx = _CtxStub()
    renderer._vao = {}
    for attr in (
            "_tex_y", "_tex_u", "_tex_v", "_tex_packed",
            "_tex_y_u8", "_tex_u_u8", "_tex_v_u8",
            "_fbo_out", "_tex_out", "_fbo_y_out", "_tex_y_out",
            "_fbo_uv_out", "_tex_uv_out", "_fbo_out_u16", "_tex_out_u16",
            "_fbo_wipe", "_tex_wipe", "_tex_mask",
            "_tex_f2", "_fbo_f2"):
        setattr(renderer, attr, None)
    renderer._tex_mid = (None, None)
    renderer._fbo_mid = (None, None)
    renderer._tex_mid_u16 = (None, None)
    renderer._fbo_mid_u16 = (None, None)
    renderer._tex_acc = (None, None)
    renderer._fbo_acc = (None, None)
    renderer._tex_side = (None, None)
    renderer._fbo_side = (None, None)
    for prog_attr in (
            "_prog_yuv", "_prog_grade", "_prog_scale", "_prog_scale_rgb",
            "_prog_quant_rgb", "_prog_yuv420_luma", "_prog_yuv420_chroma",
            "_prog_yuv420_direct_luma", "_prog_yuv420_direct_chroma",
            "_prog_yuv_u16", "_prog_grade_u16", "_prog_scale_u16",
            "_prog_scale_u16_from_f1", "_prog_blend_f1", "_prog_blend_u16",
            "_prog_quant_u8", "_prog_quant_u16", "_prog_wipe",
            "_prog_blend_f4", "_prog_dip", "_prog_u16_to_f2",
            "_prog_f2_to_u16"):
        setattr(renderer, prog_attr, None)
    renderer._quad = SimpleNamespace(release=lambda: released.append("quad"))
    renderer._effect_runner = None
    renderer.release()
    assert released == ["quad", "ctx"]
    assert renderer._released is True
    assert renderer._ctx is None
    # 幂等：第二次调用 no-op。
    renderer.release()
    assert released == ["quad", "ctx"]


# =====================================================================
# AUD-18508：VFR reverse 回退 CFR 精确公式
# =====================================================================


def test_aud_18508_vfr_reverse_falls_back_to_reverse_order():
    pts_index = tuple(float(i) * 0.033 for i in range(30))  # 名义 30fps VFR
    base = dict(
        timeline_fps_num=30, timeline_fps_den=1,
        clip_timeline_start=0,
        clip_timeline_duration=10,
        source_start_frame=0,
        source_duration_frames=30,
        source_fps_num=30, source_fps_den=1,
        speed=Fraction(1, 1),
        source_pts_index=pts_index,
    )
    forward = [
        map_frame(FrameMapRequest(
            **{**base, "reverse": False, "timeline_frame": f})).source_frame
        for f in range(10)]
    reverse = [
        map_frame(FrameMapRequest(
            **{**base, "reverse": True, "timeline_frame": f})).source_frame
        for f in range(10)]
    assert forward == sorted(forward)  # 正向仍走 PTS bisect（递增）
    # 反向：回退 CFR 精确公式——帧序递减（此前按正向取帧）。
    assert reverse == sorted(reverse, reverse=True), reverse
    assert reverse[0] == 29 and reverse[-1] == 20

    # map_vfr_frame 显式承担同一回退语义（与 CFR 公式完全一致）。
    vfr_reverse = map_vfr_frame(FrameMapRequest(
        **{**base, "reverse": True, "timeline_frame": 0}))
    cfr_reverse = map_vfr_frame(FrameMapRequest(
        **{**base, "reverse": True, "timeline_frame": 0,
           "source_pts_index": None}))
    assert vfr_reverse is not None and cfr_reverse is not None
    assert vfr_reverse == cfr_reverse


def test_aud_18508_vfr_forward_mapping_unchanged():
    # 不规则 PTS：0.0, 0.05, 0.11, 0.2 —— 目标 0.1s 的最近帧是 0.11（帧 2）。
    request = FrameMapRequest(
        timeline_frame=1,
        timeline_fps_num=10, timeline_fps_den=1,
        clip_timeline_start=0, clip_timeline_duration=5,
        source_start_frame=0, source_duration_frames=4,
        source_fps_num=10, source_fps_den=1,
        source_pts_index=(0.0, 0.05, 0.11, 0.2),
    )
    result = map_frame(request)
    assert result is not None and result.source_frame == 2


# =====================================================================
# AUD-18601：Metal 8-bit 工作链对 >8-bit 源 fail-closed
# =====================================================================


def test_aud_18601_stack_source_bit_depth():
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource,
    )
    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )

    deep = GpuYuvSource(
        planes=(np.zeros((4, 8), np.uint16),
                np.zeros((2, 4), np.uint16),
                np.zeros((2, 4), np.uint16)),
        luma_bits=10, chroma_x_div=2, chroma_y_div=1,
        full_range=False, kr=0.2126, kb=0.0722)
    shallow = GpuYuvSource(
        planes=(np.zeros((4, 8), np.uint8),
                np.zeros((2, 4), np.uint8),
                np.zeros((2, 4), np.uint8)),
        luma_bits=8, chroma_x_div=2, chroma_y_div=1,
        full_range=False, kr=0.2126, kb=0.0722)
    assert TimelineMetalExportRenderer._stack_source_bit_depth(deep) == 10
    assert TimelineMetalExportRenderer._stack_source_bit_depth(shallow) == 8
    assert TimelineMetalExportRenderer._stack_source_bit_depth(
        np.zeros((4, 8, 3), np.uint8)) == 8
    assert TimelineMetalExportRenderer._stack_source_bit_depth(None) == 8


def test_aud_18601_layer_stack_fails_closed_for_high_depth_source():
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource,
    )
    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )

    deep = GpuYuvSource(
        planes=(np.zeros((48, 64), np.uint16),
                np.zeros((48, 32), np.uint16),
                np.zeros((48, 32), np.uint16)),
        luma_bits=10, chroma_x_div=2, chroma_y_div=1,
        full_range=False, kr=0.2126, kb=0.0722)
    renderer = TimelineMetalExportRenderer()
    try:
        # 门在 _ensure_ready 之前：无需 Metal 设备即得到确定性 reason。
        assert renderer.render_layer_stack(
            [(deep, None, 1.0, 0)], 64, 48,
            output_yuv422p10=True) is None
        assert renderer.last_fallback_reason == \
            "metal_8bit_chain_high_depth_source"
        assert renderer.render_layer_stack(
            [(deep, None, 1.0, 0)], 64, 48, output_u16=True) is None
        assert renderer.last_fallback_reason == \
            "metal_8bit_chain_high_depth_source"
        # 8-bit 输出（u8 读回）不触发该门。
        renderer.last_fallback_reason = None
        renderer.render_layer_stack([(deep, None, 1.0, 0)], 64, 48)
        assert renderer.last_fallback_reason != \
            "metal_8bit_chain_high_depth_source"
    finally:
        renderer.release()


# =====================================================================
# AUD-18602：stack 路径 GPU trail 记录 CPU 上传字节
# =====================================================================


class _TrailStub:
    def __init__(self) -> None:
        self.samples: list[dict] = []

    def record_stage_sample(self, **kwargs) -> None:
        self.samples.append(kwargs)


class _CtxStub:
    last_gpu_time_ms = 1.25


def test_aud_18602_upload_bytes_accumulated_and_recorded():
    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )
    renderer = TimelineMetalExportRenderer.__new__(
        TimelineMetalExportRenderer)
    renderer._render_lock = threading.RLock()
    renderer._buffers = {}

    written: list[int] = []

    class _ContextStub:
        last_gpu_time_ms = 0.5

        @staticmethod
        def create_buffer(size):
            return SimpleNamespace(length=lambda: int(size))

        @staticmethod
        def write_buffer(buffer, data):
            written.append(int(data.nbytes))

    renderer._context = _ContextStub()
    renderer._frame_cpu_upload_bytes = 0
    renderer._gpu_trail = _TrailStub()

    renderer._upload_array("params", np.zeros(12, dtype=np.float32))
    renderer._upload_array("source", np.zeros((4, 8), dtype=np.uint8))
    assert renderer._frame_cpu_upload_bytes == 12 * 4 + 32
    assert written == [48, 32]

    # 完成记录未显式传值 → 使用累计值（不再记 0）。
    renderer._record_render_completion(
        _CtxStub(), 0, yuv_output=True, readback=False)
    render_sample = next(s for s in renderer._gpu_trail.samples
                         if s.get("name") == "render")
    assert render_sample["cpu_upload_bytes"] == 80
    yuv_sample = next(s for s in renderer._gpu_trail.samples
                      if s.get("name") == "yuv_conversion")
    assert yuv_sample["cpu_download_bytes"] == 0

    # 显式传值仍优先（_render 的精确和保持不变）。
    renderer._record_render_completion(
        _CtxStub(), 0, yuv_output=True, cpu_upload_bytes=7, readback=False)
    explicit = [s for s in renderer._gpu_trail.samples
                if s.get("name") == "render"][-1]
    assert explicit["cpu_upload_bytes"] == 7


# =====================================================================
# AUD-18700：O6 ROI 计划——分支可达 + 语义修正
# =====================================================================


class _LayerStub:
    def __init__(self, clip_id, track_index, transform=None) -> None:
        self.clip_id = clip_id
        self.track_index = track_index
        self.opacity = 1.0
        # 8 元组：tx, ty, sx, sy, rot, pivot_mode, pivot_x, pivot_y
        self.transform = transform or (
            0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
        self.crop = (0.0, 0.0, 0.0, 0.0, 0.0)
        self.is_in_transition = False
        self.composite_id = None
        self.asset_id = ""


def _video_facts(layer):
    from src.features.edit.core.timeline_occlusion import (
        LayerOcclusionFacts,
    )
    return LayerOcclusionFacts(
        asset_kind="video",
        source_alpha_known_opaque=True,
        blend_mode="normal",
        has_effects=False,
    )


CANVAS = (1920, 1080)


def test_aud_18700_partial_band_cover_shrinks_visible_region():
    from src.features.edit.core.timeline_occlusion import (
        compute_visible_region_plan,
    )
    # 上层横贯全宽、贴上边（0..540 高度带）→ 下层可见区域 = 下方横带。
    upper = _LayerStub(
        "v2", 1,
        transform=(0.0, -270.0, 1.0, 0.5, 0.0, "center", 0.0, 0.0))
    lower = _LayerStub("v1", 0)
    plan = compute_visible_region_plan(
        (lower, upper), (), 0, CANVAS, facts_of=_video_facts)
    region = plan.region_for("v1")
    assert region is not None
    assert region.region is not None, "单矩形带状覆盖必须产出收缩区域"
    assert region.region == (0.0, 540.0, 1920.0, 540.0)
    assert region.complete_cover is False
    # 覆盖层自身仍整画布可见。
    assert plan.region_for("v2").region is None


def test_aud_18700_interior_partial_cover_stays_full_canvas():
    from src.features.edit.core.timeline_occlusion import (
        compute_visible_region_plan,
    )
    # 居中 0.5 缩放（悬浮内部矩形）：余集 L 形，保守整画布。
    upper = _LayerStub(
        "v2", 1,
        transform=(0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0))
    lower = _LayerStub("v1", 0)
    plan = compute_visible_region_plan(
        (lower, upper), (), 0, CANVAS, facts_of=_video_facts)
    region = plan.region_for("v1")
    assert region.region is None and region.complete_cover is False


def test_aud_18700_multiple_partial_rects_conservative():
    from src.features.edit.core.timeline_occlusion import (
        compute_visible_region_plan,
    )
    mid = _LayerStub(
        "v2", 1,
        transform=(0.0, -270.0, 1.0, 0.5, 0.0, "center", 0.0, 0.0))
    top = _LayerStub(
        "v3", 2,
        transform=(0.0, 270.0, 1.0, 0.5, 0.0, "center", 0.0, 0.0))
    lower = _LayerStub("v1", 0)
    plan = compute_visible_region_plan(
        (lower, mid, top), (), 0, CANVAS, facts_of=_video_facts)
    region = plan.region_for("v1")
    # 两个部分覆盖矩形：并集不求交 → 保守整画布（不反转、不崩溃）。
    assert region.region is None and region.complete_cover is False


def test_aud_18700_full_cover_still_completes():
    from src.features.edit.core.timeline_occlusion import (
        compute_visible_region_plan,
    )
    upper = _LayerStub("v2", 1)  # identity transform → full_frame
    lower = _LayerStub("v1", 0)
    plan = compute_visible_region_plan(
        (lower, upper), (), 0, CANVAS, facts_of=_video_facts)
    region = plan.region_for("v1")
    assert region.complete_cover is True and region.region is None


def test_aud_18700_visible_region_minus_rect_unit():
    from src.features.edit.core.timeline_occlusion import (
        _visible_region_minus_rect,
    )
    # 横贯全宽贴顶 → 下方横带。
    assert _visible_region_minus_rect(1920, 1080, (0, 0, 1920, 540)) == \
        (0.0, 540.0, 1920.0, 540.0)
    # 横贯全宽贴底 → 上方横带。
    assert _visible_region_minus_rect(1920, 1080, (0, 540, 1920, 540)) == \
        (0.0, 0.0, 1920.0, 540.0)
    # 竖贯全高贴左 → 右侧竖带。
    assert _visible_region_minus_rect(1920, 1080, (0, 0, 960, 1080)) == \
        (960.0, 0.0, 960.0, 1080.0)
    # 横贯全宽但垂直悬浮（上下各露一块）→ 保守 None。
    assert _visible_region_minus_rect(1920, 1080, (0, 270, 1920, 540)) is None
    # 内部矩形 → 保守 None。
    assert _visible_region_minus_rect(
        1920, 1080, (480, 270, 960, 540)) is None
    # 完全覆盖整画布 → 空集（零高度带；调用方映射为 complete_cover）。
    assert _visible_region_minus_rect(
        1920, 1080, (-2, -2, 1924, 1084)) == (0.0, 1080.0, 1920.0, 0.0)
    assert _visible_region_minus_rect(1920, 1080, None) is None


# =====================================================================
# AUD-18322：>8-bit 源转换目标 dtype
# =====================================================================


def test_aud_18322_source_rgb_output_dtype():
    from src.features.edit.core.timeline_clip_frame_provider import (
        TimelineClipFrameProvider,
    )
    assert TimelineClipFrameProvider._source_rgb_output_dtype(8) == "f1"
    assert TimelineClipFrameProvider._source_rgb_output_dtype(10) == "f2"
    assert TimelineClipFrameProvider._source_rgb_output_dtype(12) == "f2"
    assert TimelineClipFrameProvider._source_rgb_output_dtype(16) == "f2"
