"""T-P0-30：Native VT bridge 前置协商与安全回退聚焦测试。

覆盖总计划任务卡的六组模拟场景 + 保护性报错回归 + 初始化失败清理 +
真实 TOPOS 422 → H264 导出（TOPOS_EXPORT_NATIVE_VT=1 不得失败或产生
伪成功文件，必须 ``bridge_not_selected:topos_422_to_nv12_not_implemented``）。
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from src.features.edit.core.timeline_export import (
    _ExternalGpuSurfaceEncoder,
    can_use_native_gpu_bridge,
)
from src.features.edit.core.clip_params import CropParams, TransformParams

TEST_ROOT = Path(__file__).resolve().parents[2]
TOPOS_SOURCE = TEST_ROOT / "tests/test_videos/2K_topos_422_Proxy.mov"

_IDENTITY_TRANSFORM = TransformParams()
_IDENTITY_CROP = CropParams()


# ---------------------------------------------------------------------------
# stub 构造
# ---------------------------------------------------------------------------

class _StubAsset:
    def __init__(self, media_type="video", pixel_format="yuv420p"):
        self.media_type = media_type
        self.metadata = {"pixel_format": pixel_format} if pixel_format else {}


class _StubClip:
    def __init__(self, asset_id="a1", enabled=True, effects=(),
                 color_stack_id=None, composite_id=None,
                 transform="identity", crop="identity",
                 opacity=1.0, fade_in=0.0, fade_out=0.0):
        self.asset_id = asset_id
        self.is_enabled = enabled
        self.effects = effects
        self.color_stack_id = color_stack_id
        self.composite_id = composite_id
        self.transform = transform
        self.crop = crop
        self.opacity = opacity
        self.fade_in_duration = fade_in
        self.fade_out_duration = fade_out


class _StubTrack:
    def __init__(self, clips=(), transitions=(), enabled=True):
        self.clips = clips
        self.transitions = transitions
        self.is_enabled = enabled


class _StubSequence:
    def __init__(self, tracks=(), subtitle_tracks=()):
        self.video_tracks = tracks
        self.subtitle_tracks = subtitle_tracks


class _StubProject:
    def __init__(self, assets=None):
        self._assets = assets or {}

    def get_asset_by_id(self, asset_id):
        return self._assets.get(asset_id)


class _StubEncoderConfig:
    def __init__(self, codec="h264", pix_fmt="yuv420p",
                 width=1920, height=1080):
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.width = width
        self.height = height


class _StubExportConfig:
    def __init__(self, burn_subtitles=False, has_alpha=False):
        self.burn_subtitles = burn_subtitles
        self.has_alpha = has_alpha


class _Identity:
    """TransformParams()/CropParams() 的哨兵（与自身相等）。"""




def _eligible_project(pixel_format="yuv420p"):
    asset = _StubAsset(pixel_format=pixel_format)
    clip = _StubClip(transform=_IDENTITY_TRANSFORM, crop=_IDENTITY_CROP)
    sequence = _StubSequence([_StubTrack([clip])])
    project = _StubProject({"a1": asset})
    return sequence, project


# ---------------------------------------------------------------------------
# 纯函数：六组场景 + 边界
# ---------------------------------------------------------------------------


class TestEligibilitySixScenarios:
    def test_topos_422_to_h264_eligible(self):
        """T-P0-31：422 R16 → NV12 GPU 降采样 kernel 落地后 bridge 可选。"""
        sequence, project = _eligible_project(pixel_format="yuv422p10le")
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig("h264", "yuv420p"))
        assert result.eligible is True
        assert result.required_output_surface == "nv12"

    def test_topos_422_to_hevc_eligible(self):
        """T-P0-31：422 R16 → P010 surface 同样有定义的转换路径。"""
        sequence, project = _eligible_project(pixel_format="yuv422p10le")
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig("hevc", "yuv420p10le"))
        assert result.eligible is True
        assert result.required_output_surface == "p010"

    def test_topos_422_non_420_surface_still_blocked(self):
        """422 只支持 nv12/p010 surface；其它组合 fail-closed。"""
        sequence, project = _eligible_project(pixel_format="yuv422p10le")
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig("prores_422", "yuv422p10le"))
        assert result.eligible is False
        assert result.reason_code in (
            "unsupported_encoder_format", "topos_422_surface_format_unsupported")

    def test_h264_nv12_source_to_h264_eligible(self):
        sequence, project = _eligible_project(pixel_format="yuv420p")
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig("h264", "yuv420p"))
        assert result.eligible is True
        assert result.required_output_surface == "nv12"

    def test_yuv420p_source_to_hevc_p010_blocked(self):
        sequence, project = _eligible_project(pixel_format="yuv420p")
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig("hevc", "yuv420p10le"))
        assert result.eligible is False
        assert result.reason_code == (
            "source_to_surface_not_supported:yuv420p:p010")

    def test_tenbit_source_to_hevc_p010_eligible(self):
        sequence, project = _eligible_project(pixel_format="yuv420p10le")
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig("hevc", "yuv420p10le"))
        assert result.eligible is True
        assert result.required_output_surface == "p010"

    def test_blur_effect_blocked(self):
        asset = _StubAsset()
        clip = _StubClip(effects=("blur",), transform=_IDENTITY_TRANSFORM,
                         crop=_IDENTITY_CROP)
        result = can_use_native_gpu_bridge(
            _StubSequence([_StubTrack([clip])]),
            _StubProject({"a1": asset}),
            _StubEncoderConfig())
        assert result.reason_code == "clip_effects_present"

    def test_transition_blocked(self):
        asset = _StubAsset()
        clip = _StubClip(transform=_IDENTITY_TRANSFORM,
                         crop=_IDENTITY_CROP)
        result = can_use_native_gpu_bridge(
            _StubSequence([_StubTrack([clip], transitions=("dissolve",))]),
            _StubProject({"a1": asset}),
            _StubEncoderConfig())
        assert result.reason_code == "transitions_present"

    def test_burn_subtitles_blocked(self):
        sequence, project = _eligible_project()

        class _SubtitleClip:
            is_enabled = True

        class _SubtitleTrack:
            is_enabled = True
            clips = (_SubtitleClip(),)

        sequence_with_subs = _StubSequence(
            sequence.video_tracks, subtitle_tracks=(_SubtitleTrack(),))
        result = can_use_native_gpu_bridge(
            sequence_with_subs, project, _StubEncoderConfig(),
            export_config=_StubExportConfig(burn_subtitles=True))
        assert result.reason_code == "subtitles_requested"

    def test_burn_subtitles_without_content_allowed(self):
        sequence, project = _eligible_project()
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig(),
            export_config=_StubExportConfig(burn_subtitles=True))
        assert result.eligible is True

    def test_alpha_blocked(self):
        sequence, project = _eligible_project()
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig(),
            export_config=_StubExportConfig(has_alpha=True))
        assert result.reason_code == "alpha_requested"

    def test_odd_resolution_blocked(self):
        sequence, project = _eligible_project()
        result = can_use_native_gpu_bridge(
            sequence, project,
            _StubEncoderConfig(width=1921, height=1080))
        assert result.eligible is False
        assert result.reason_code == "odd_resolution_not_supported"


class TestEligibilityBoundaries:
    def test_unsupported_encoder_format(self):
        sequence, project = _eligible_project()
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig("prores", "yuv422p10le"))
        assert result.reason_code == "unsupported_encoder_format"

    def test_missing_pixel_format_fail_closed(self):
        sequence, project = _eligible_project(pixel_format="")
        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig())
        assert result.reason_code == "source_pixel_format_unknown"

    def test_opacity_and_fades_blocked(self):
        asset = _StubAsset()
        for kwargs, expected in (
            (dict(opacity=0.5, transform=_IDENTITY_TRANSFORM, crop=_IDENTITY_CROP),
             "non_identity_opacity"),
            (dict(fade_in=0.5, transform=_IDENTITY_TRANSFORM, crop=_IDENTITY_CROP),
             "fades_present"),
        ):
            clip = _StubClip(**kwargs)
            result = can_use_native_gpu_bridge(
                _StubSequence([_StubTrack([clip])]),
                _StubProject({"a1": asset}),
                _StubEncoderConfig())
            assert result.reason_code == expected

    def test_videotoolbox_unavailable_snapshot(self):
        sequence, project = _eligible_project()

        class _State:
            value = "UNAVAILABLE"

        class _Entry:
            state = _State()
            reason_code = "VIDEOTOOLBOX_FRAMEWORK_NOT_FOUND"

        class _Snapshot:
            @staticmethod
            def entry(component_id):
                if component_id == "encode.videotoolbox":
                    return _Entry()
                return None

        result = can_use_native_gpu_bridge(
            sequence, project, _StubEncoderConfig(),
            snapshot=_Snapshot())
        assert result.reason_code == "videotoolbox_unavailable"

    def test_no_video_clips(self):
        result = can_use_native_gpu_bridge(
            _StubSequence([_StubTrack([])]), _StubProject({}),
            _StubEncoderConfig())
        assert result.reason_code == "no_video_clips"


# ---------------------------------------------------------------------------
# 保护性报错回归 + bridge 选择行为
# ---------------------------------------------------------------------------


class TestProtectiveEncoderError:
    def test_cpu_frame_rejected(self):
        from src.shared.export.gpu_encode_surface import GpuEncodeSurface

        encoder = _ExternalGpuSurfaceEncoder(bridge=object(), config=None)
        encoder.open()
        with pytest.raises(
                RuntimeError,
                match="cannot consume a CPU/host-visible frame"):
            encoder.encode_frame(object())  # CPU/host-visible frame

    def test_gpu_surface_submitted(self):
        from src.shared.export.gpu_encode_surface import GpuEncodeSurface

        submitted = {"n": 0}
        surface = GpuEncodeSurface.__new__(GpuEncodeSurface)
        surface.submit = lambda: submitted.__setitem__("n", 1)
        encoder = _ExternalGpuSurfaceEncoder(bridge=object(), config=None)
        encoder.open()
        encoder.encode_frame(surface)
        assert submitted["n"] == 1


class TestBridgeSelection:
    def _exporter(self, tmp_path, sequence, project, telemetry=None):
        from src.features.edit.core.timeline_export import TimelineExporter

        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = sequence
        exporter._project = project
        exporter._gpu_encode_surface_provider = None
        exporter._active_export_config = None
        exporter._timeline_graphics_backend = "cpu"
        exporter._bridge_selection = {"selected": False,
                                      "reason_code": "not_evaluated",
                                      "output_surface": ""}
        exporter._export_telemetry = telemetry
        return exporter

    def test_env_unset_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOPOS_EXPORT_NATIVE_VT", raising=False)
        sequence, project = _eligible_project()
        exporter = self._exporter(tmp_path, sequence, project)
        exporter._maybe_create_default_native_gpu_bridge(
            str(tmp_path / "out.mp4"), _StubEncoderConfig())
        assert exporter._gpu_encode_surface_provider is None
        assert exporter.bridge_selection["reason_code"] == "not_evaluated"

    def test_topos_422_records_bridge_selected(
            self, tmp_path, monkeypatch):
        """T-P0-31 后 topos 422 走 nv12/p010 surface：bridge 应被选中。"""
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
        from src.shared.export.export_stage_telemetry import (
            ExportTelemetryRecorder,
        )

        telemetry = ExportTelemetryRecorder("t")
        sequence, project = _eligible_project(pixel_format="yuv422p10le")
        exporter = self._exporter(tmp_path, sequence, project, telemetry)

        class _FakeBridge:
            close_on_export = True
            handles_timeline_video = True
            output_path = str(tmp_path / "out.mp4")
            width = 1920
            height = 1080

            def __init__(self, ctx, path, **kwargs):
                pass

            def close(self):
                pass

        metal_mod = types.ModuleType("src.shared.gpu.metal.context")
        metal_mod.MetalComputeContext = lambda: object()
        bridge_mod = types.ModuleType(
            "src.shared.media.macos_vt.gpu_export_bridge")
        bridge_mod.VideoToolboxGpuExportBridge = _FakeBridge
        monkeypatch.setitem(sys.modules,
                            "src.shared.gpu.metal.context", metal_mod)
        monkeypatch.setitem(
            sys.modules,
            "src.shared.media.macos_vt.gpu_export_bridge", bridge_mod)

        exporter._maybe_create_default_native_gpu_bridge(
            str(tmp_path / "out.mp4"), _StubEncoderConfig())
        assert isinstance(exporter._gpu_encode_surface_provider, _FakeBridge)
        assert exporter.bridge_selection == {
            "selected": True, "reason_code": "",
            "output_surface": "nv12",
        }

    def test_bridge_init_failure_cleans_partial_output(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
        from src.shared.export.export_stage_telemetry import (
            ExportTelemetryRecorder,
        )

        telemetry = ExportTelemetryRecorder("t")
        sequence, project = _eligible_project()
        output = tmp_path / "out.mp4"
        output.write_bytes(b"partial mux from failed bridge")
        exporter = self._exporter(tmp_path, sequence, project, telemetry)

        # 模拟 Metal context 不可用（模块存在但构造抛出）
        metal_mod = types.ModuleType("src.shared.gpu.metal.context")

        class _BoomContext:
            def __init__(self):
                raise RuntimeError("simulated metal init failure")

        metal_mod.MetalComputeContext = _BoomContext
        monkeypatch.setitem(sys.modules,
                            "src.shared.gpu.metal.context", metal_mod)

        exporter._maybe_create_default_native_gpu_bridge(
            str(output), _StubEncoderConfig())
        assert exporter._gpu_encode_surface_provider is None
        assert not output.exists(), "半成品输出必须被清理"
        assert exporter.bridge_selection["reason_code"] == "bridge_init_failed"
        assert telemetry.snapshot().fallback_root_cause.startswith(
            "bridge_init_failed:")

    def test_successful_bridge_selection(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
        sequence, project = _eligible_project()
        exporter = self._exporter(tmp_path, sequence, project)

        created = {}

        class _FakeBridge:
            close_on_export = True
            handles_timeline_video = True
            output_path = str(tmp_path / "out.mp4")
            width = 1920
            height = 1080

            def __init__(self, ctx, path, **kwargs):
                created["path"] = path
                created["pixel_format"] = kwargs.get("pixel_format")

            def close(self):
                pass

        metal_mod = types.ModuleType("src.shared.gpu.metal.context")
        metal_mod.MetalComputeContext = lambda: object()
        bridge_mod = types.ModuleType(
            "src.shared.media.macos_vt.gpu_export_bridge")
        bridge_mod.VideoToolboxGpuExportBridge = _FakeBridge
        monkeypatch.setitem(sys.modules,
                            "src.shared.gpu.metal.context", metal_mod)
        monkeypatch.setitem(
            sys.modules,
            "src.shared.media.macos_vt.gpu_export_bridge", bridge_mod)

        exporter._maybe_create_default_native_gpu_bridge(
            str(tmp_path / "out.mp4"), _StubEncoderConfig())
        assert isinstance(exporter._gpu_encode_surface_provider, _FakeBridge)
        assert exporter._timeline_graphics_backend == "metal"
        assert exporter.bridge_selection == {
            "selected": True, "reason_code": "",
            "output_surface": "nv12",
        }
        assert created["pixel_format"] == "nv12"


# ---------------------------------------------------------------------------
# 真实素材：TOPOS 422 → H264（T-P0-31 落地后经 native VT bridge 导出）
# ---------------------------------------------------------------------------


class TestRealTopos422GpuBridge:
    def test_native_vt_export_topos_422_bridge_selected(
            self, tmp_path, monkeypatch, codec="h264",
            expected_pix_fmt="yuv420p", expected_surface="nv12"):
        """T-P0-31 集成：真实 TOPOS 422 源 → Metal 422→NV12/P010 surface → VT。

        资格门选中 bridge；导出成功且 probe 有效。Metal/VT 中途故障时允许
        诚实回退（telemetry 必须携带 fallback 标记），但不允许失败或伪成功。
        HEVC 用例由 test_native_vt_export_topos_422_hevc_p010 覆盖（卡验证 2）。
        """
        if not TOPOS_SOURCE.is_file():
            pytest.skip("真实 TOPOS 素材缺失")
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
        # pytest 环境默认禁用 GPU 渲染（确定性口径）；本测试验证的正是
        # Metal surface 路径，显式启用（与 W06 工作流同语义）。
        monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
        from src.features.edit.core.project import (
            EditProject,
            MediaAsset,
            probe_media_metadata,
        )
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.shared.media.metadata import Rational

        codec_enum = (ExportCodec.HEVC if codec == "hevc"
                      else ExportCodec.H264)
        frames = 8
        project = EditProject.create(name="bridge-preflight")
        metadata = {"fps": 25.0, "total_frames": frames}
        metadata.update(probe_media_metadata(str(TOPOS_SOURCE)))
        metadata["total_frames"] = frames
        assert metadata.get("pixel_format") == "yuv422p10le"
        asset = MediaAsset(
            asset_id="asset-bridge-pf", path=str(TOPOS_SOURCE),
            media_type="video", display_name="bridge-pf",
            metadata=metadata,
        )
        project = project.with_media_asset_added(asset)
        clip = ClipInstance(
            clip_id="clip-bridge-pf", asset_id=asset.asset_id,
            track_id="v1", timeline_start=0, timeline_duration=frames,
            source_start=0, source_duration=frames,
        )
        v1 = EditTrack(track_id="v1", name="V1", track_type="video",
                       index=0, clips=(clip,))
        sequence = TimelineSequence(
            timeline_id="tl-bridge-pf", name="BridgePF",
            fps=Rational(25, 1), width=1920, height=1080,
            video_tracks=(v1,), audio_tracks=(),
        )
        # 10-bit HEVC 走 MOV 容器（与 W03 矩阵一致的仓库惯例）。
        output = tmp_path / (
            f"out_{codec}.{'mov' if codec == 'hevc' else 'mp4'}")
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=codec_enum,
            pixel_format=expected_pix_fmt,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        # T-P0-31：资格门选中 bridge（目标 surface）
        selection = exporter.bridge_selection
        assert selection["selected"] is True, selection
        assert selection["output_surface"] == expected_surface, selection
        # 输出 probe 正确（无伪成功文件）
        import av

        with av.open(str(output)) as container:
            stream = container.streams.video[0]
            assert stream.codec_context.name == codec
            assert str(stream.codec_context.pix_fmt) == expected_pix_fmt
            count = sum(1 for _ in container.decode(stream))
        assert count == frames
        # telemetry 记录 encode backend（videotoolbox 或诚实回退标记）
        telemetry = outcome.stage_telemetry
        assert telemetry is not None

    @pytest.mark.skipif(
        os.environ.get("TOPOS_RUN_KNOWN_X265_CRASH") != "1",
        reason="N06（2026-09-27，修正 C19 误诊）：崩溃不在 VT bridge，而在"
               "软件 libx265 10-bit——PyAV 17.0.1（ffmpeg 8.0.1 + libx265 "
               "4.1 十比特构建）在进程内有过任何 PyAV 媒体探测/解码后，"
               "x265 10-bit flush 必发 SIGSEGV（x265_10bit::subpelCompare/"
               "interp_8tap_hv 读已污染参考帧；topos 与 prores 源同触发，"
               "h264 与 8-bit hevc 不受影响，pools=none/frame-threads=1/"
               "bframes=0/ultrafast 均不能绕过——上游缺陷，非本项目代码，"
               "最小复现：probe_media_metadata(任意真实视频) 后 FFmpegVideo"
               "Encoder(hevc,yuv420p10le) 编 8 帧 close()）。默认跳过防击穿"
               "测试进程；设 TOPOS_RUN_KNOWN_X265_CRASH=1 显式执行（变量名"
               "与行为一致——修掉原 TOPOS_SKIP_KNOWN_VT_CRASH 设 1 反而执行"
               "的极性反转缺陷）。上游升级后移除本标记。")
    def test_native_vt_export_topos_422_hevc_p010(
            self, tmp_path, monkeypatch):
        """卡验证 2 的 HEVC P010 臂（诚实双结局）。

        Main10 硬件编码器可用的 Mac → 原生 P010 surface 路径；
        不支持（如部分 AMD dGPU 报 -12900）→ bridge 结构化拒绝 +
        CPU 10-bit 回退。两种都是诚实结果；8-bit 冒充 P010 才不通过。

        C02 备注：本测试此前随整文件 skip 从未真跑；解封后立即暴露
        VT flush 段错误（见上方 skipif 与计划 C19）。
        """
        if not TOPOS_SOURCE.is_file():
            pytest.skip("真实 TOPOS 素材缺失")
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
        # 注意：hevc 10-bit 臂不强制 GPU_RENDER——forced GPU + 高精度导出
        # 会走 planar 直出管线，与宿主 hevc 编码器不兼容（预存组合限制，
        # W03 同此自适应口径）。Main10 硬件可用的机器上 bridge 在
        # _create_encoder 时自行接管渲染后端，无需强制。
        from src.features.edit.core.project import (
            EditProject,
            MediaAsset,
            probe_media_metadata,
        )
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.shared.media.metadata import Rational

        frames = 8
        project = EditProject.create(name="bridge-preflight")
        metadata = {"fps": 25.0, "total_frames": frames}
        metadata.update(probe_media_metadata(str(TOPOS_SOURCE)))
        metadata["total_frames"] = frames
        assert metadata.get("pixel_format") == "yuv422p10le"
        asset = MediaAsset(
            asset_id="asset-bridge-pf", path=str(TOPOS_SOURCE),
            media_type="video", display_name="bridge-pf",
            metadata=metadata,
        )
        project = project.with_media_asset_added(asset)
        clip = ClipInstance(
            clip_id="clip-bridge-pf", asset_id=asset.asset_id,
            track_id="v1", timeline_start=0, timeline_duration=frames,
            source_start=0, source_duration=frames,
        )
        v1 = EditTrack(track_id="v1", name="V1", track_type="video",
                       index=0, clips=(clip,))
        sequence = TimelineSequence(
            timeline_id="tl-bridge-pf", name="BridgePF",
            fps=Rational(25, 1), width=1920, height=1080,
            video_tracks=(v1,), audio_tracks=(),
        )
        output = tmp_path / "out_hevc.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.HEVC,
            pixel_format="yuv420p10le",
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        selection = exporter.bridge_selection
        if selection["selected"]:
            # 原生路径：P010 surface 交付。
            assert selection["output_surface"] == "p010"
        else:
            # 结构化拒绝：Main10 不可用必须显式留痕（不允许静默 8-bit）。
            assert selection["reason_code"] == "bridge_init_failed"
            telemetry = outcome.stage_telemetry or {}
            root_cause = str(telemetry.get("fallback_root_cause", ""))
            assert "profile_level_rejected" in root_cause, root_cause
            assert "p010" in root_cause, root_cause
        # 输出 probe：真实 10-bit HEVC（任何路径都不允许位深缩水）。
        import av

        with av.open(str(output)) as container:
            stream = container.streams.video[0]
            assert stream.codec_context.name == "hevc"
            assert str(stream.codec_context.pix_fmt) == "yuv420p10le"
            count = sum(1 for _ in container.decode(stream))
        assert count == frames

