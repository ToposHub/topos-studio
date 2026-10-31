"""
真实素材多轮回归：时间线多轨遮挡裁剪（计划 §6.2 解码/合成回归 + §7 基准组）。

素材来自 tests/test_videos（真实 FFmpeg/Topos 解码，无 mock、无 GUI）。
每个场景以"裁剪开"与"裁剪关"各渲染同一批帧：

- B1 两轨，上轨全屏不透明：裁剪后下轨解码请求为 0（§7-B1）；
- B2 两轨，上轨缩小 50%：下轨仍被请求、不被误裁（§7-B2）；
- B3 四轨，最上层全屏不透明：下面三轨零解码请求（§7-B3）；
- B4 同轨 cross_dissolve 转场：转场涉及层全部保留（§3.2）；
- B5 上轨 Alpha 元数据（ProRes 4444 语义）：fail-closed 保留下层。

一致性验收（§6.2-4）：同帧开/关裁剪输出逐像素一致（合成语义未变，
仅省去被遮挡层的解码与合成；浮点往返经 rint 量化可证无损）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core import timeline_occlusion as oc
from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportSource,
    TimelineExportConfig,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
    TransitionInstance,
)

REPO = Path(__file__).resolve().parents[2]
TEST_VIDEOS = REPO / "tests" / "test_videos"

LOWER_FILE = "2K_h264.mp4"            # yuv420p 8bit
UPPER_FILE = "2K_prores422HQ.mov"     # yuv422p10le
UPPER_ALPHA_FILE = "2K_prores4444.mov"  # ProRes 4444 语义（按 yuva 元数据模拟）

SEQ_W, SEQ_H, FPS = 1920, 1080, 25
FRAMES = (0, 4, 12)
CLIP_DURATION = 24

pytestmark = pytest.mark.skipif(
    not (TEST_VIDEOS / LOWER_FILE).is_file()
    or not (TEST_VIDEOS / UPPER_FILE).is_file(),
    reason="真实测试素材缺失",
)


def _make_asset(asset_id: str, filename: str, pixel_format: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=str(TEST_VIDEOS / filename),
        media_type="video",
        display_name=asset_id,
        metadata={
            "codec": "prores" if filename.endswith(".mov") else "h264",
            "pixel_format": pixel_format,
            "width": SEQ_W,
            "height": SEQ_H,
            "fps_num": 25,
            "fps_den": 1,
            "fps": 25.0,
            "total_frames": 300,
        },
    )


def _project_with(assets: list) -> EditProject:
    project = EditProject.create(name="occlusion-real-media")
    for asset in assets:
        project = project.with_media_asset_added(asset)
    return project


def _clip(clip_id: str, asset_id: str, track_id: str, **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=kw.pop("timeline_start", 0),
        timeline_duration=CLIP_DURATION,
        source_start=kw.pop("source_start", 0),
        source_duration=CLIP_DURATION,
        **kw,
    )


def _sequence(tracks: list) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="occlusion-seq",
        name="OcclusionRealMedia",
        fps=Rational(25, 1),
        width=SEQ_W,
        height=SEQ_H,
        video_tracks=tuple(tracks),
    )


def _export_config() -> TimelineExportConfig:
    return TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="occlusion-seq",
        output_path="",  # 仅渲染帧，不落编码文件
        codec=ExportCodec.H264,
        pixel_format="yuv420p",
        resolution=(SEQ_W, SEQ_H),
        fps=25.0,
        include_audio=False,
        burn_subtitles=False,
    )


def _render_frames(project, seq, frames, cull: bool):
    """渲染指定帧；返回 (帧数组列表, 层取帧请求计数列表)。

    计数包装在 ``_get_layer_frame`` 上——这是"实际请求解码"的精确口径：
    被裁掉的层根本不会走到这里。
    """
    oc._ENV_CACHE["TOPOS_TIMELINE_OCCLUSION_CULL"] = cull
    exporter = create_timeline_exporter(project, seq)
    # _render_frame_impl 直接调用（跳过编码器）；渲染计划按
    # export_timeline 的初始化语义手工注入。
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    exporter._render_plan = TimelineRenderPlan(seq)
    calls: list = []
    real_get = exporter._get_layer_frame

    def counting_get(layer):
        calls.append(layer.clip_id)
        return real_get(layer)

    exporter._get_layer_frame = counting_get
    config = _export_config()
    frames_out = []
    for frame_idx in frames:
        rendered = exporter._render_frame_impl(frame_idx, config)
        assert rendered is not None, f"frame {frame_idx} rendered None"
        frames_out.append(np.asarray(rendered))
    return frames_out, calls


@pytest.fixture(autouse=True)
def _restore_env():
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()
    yield
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()


class TestRealMediaB1FullOcclusion:
    """B1：两轨，上轨全屏不透明。"""

    def _build(self):
        lower = _make_asset("lower", LOWER_FILE, "yuv420p")
        upper = _make_asset("upper", UPPER_FILE, "yuv422p10le")
        project = _project_with([lower, upper])
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(_clip("c_lower", "lower", "v1"),))
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                       clips=(_clip("c_upper", "upper", "v2"),))
        return project, _sequence([v1, v2])

    @pytest.mark.parametrize("round_idx", range(3))
    def test_lower_not_decoded_and_pixels_identical(self, round_idx):
        project, seq = self._build()
        frames_on, calls_on = _render_frames(
            project, seq, FRAMES, cull=True)
        frames_off, calls_off = _render_frames(
            project, seq, FRAMES, cull=False)

        # 下轨零取帧请求；关裁剪时每帧都请求下轨+上轨。
        assert calls_on == ["c_upper"] * len(FRAMES)
        assert calls_off == (
            ["c_lower", "c_upper"] * len(FRAMES))

        # §6.2-4：同帧开/关裁剪输出逐像素一致。
        for idx, (on, off) in enumerate(zip(frames_on, frames_off)):
            assert on.shape == off.shape
            assert np.array_equal(on, off), (
                f"round {round_idx} frame {FRAMES[idx]}: "
                f"max_diff={np.abs(on.astype(int) - off.astype(int)).max()}")

        # telemetry 记录了裁剪证据。
        snap = oc.get_occlusion_telemetry().snapshot()
        assert snap["modes"].get("export", 0) >= len(FRAMES)
        assert snap["culled_layers"] >= len(FRAMES)


class TestRealMediaB2ScaledUpper:
    """B2：两轨，上轨缩小 50%（露出下层，不得误裁）。"""

    def _build(self):
        lower = _make_asset("lower", LOWER_FILE, "yuv420p")
        upper = _make_asset("upper", UPPER_FILE, "yuv422p10le")
        project = _project_with([lower, upper])
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(_clip("c_lower", "lower", "v1"),))
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                       clips=(_clip(
                           "c_upper", "upper", "v2",
                           transform=TransformParams(scale_x=0.5, scale_y=0.5),
                       ),))
        return project, _sequence([v1, v2])

    @pytest.mark.parametrize("round_idx", range(3))
    def test_lower_still_requested_and_pixels_identical(self, round_idx):
        project, seq = self._build()
        frames_on, calls_on = _render_frames(
            project, seq, FRAMES, cull=True)
        frames_off, calls_off = _render_frames(
            project, seq, FRAMES, cull=False)

        # 缩小上层不构成全画布遮挡：两层都必须请求。
        assert calls_on == ["c_lower", "c_upper"] * len(FRAMES)
        assert calls_off == calls_on

        for idx, (on, off) in enumerate(zip(frames_on, frames_off)):
            assert np.array_equal(on, off), (
                f"round {round_idx} frame {FRAMES[idx]}: "
                f"max_diff={np.abs(on.astype(int) - off.astype(int)).max()}")


class TestRealMediaB3FourTrack:
    """B3：四轨，最上层全屏不透明——下面三轨全部零解码。"""

    @pytest.mark.parametrize("round_idx", range(2))
    def test_three_lower_tracks_not_decoded(self, round_idx):
        lower = _make_asset("lower", LOWER_FILE, "yuv420p")
        upper = _make_asset("upper", UPPER_FILE, "yuv422p10le")
        project = _project_with([lower, upper])
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(_clip("c_l1", "lower", "v1"),))
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                       clips=(_clip("c_l2", "lower", "v2"),))
        v3 = EditTrack(track_id="v3", name="V3", track_type="video", index=2,
                       clips=(_clip("c_l3", "lower", "v3"),))
        v4 = EditTrack(track_id="v4", name="V4", track_type="video", index=3,
                       clips=(_clip("c_top", "upper", "v4"),))
        seq = _sequence([v1, v2, v3, v4])

        frames_on, calls_on = _render_frames(project, seq, FRAMES, cull=True)
        frames_off, calls_off = _render_frames(
            project, seq, FRAMES, cull=False)

        # 只保留最上层及其上方（无）→ 每帧只有 1 次取帧请求。
        assert calls_on == ["c_top"] * len(FRAMES)
        # 关闭裁剪：每帧 4 层全部请求（渲染顺序从下到上）。
        assert calls_off == ["c_l1", "c_l2", "c_l3", "c_top"] * len(FRAMES)

        for idx, (on, off) in enumerate(zip(frames_on, frames_off)):
            assert np.array_equal(on, off), (
                f"round {round_idx} frame {FRAMES[idx]}: "
                f"max_diff={np.abs(on.astype(int) - off.astype(int)).max()}")


class TestRealMediaB4Transition:
    """B4：同轨 cross_dissolve——转场帧全部层保留。"""

    def test_transition_frames_keep_all_and_identical(self):
        asset = _make_asset("src", UPPER_FILE, "yuv422p10le")
        project = _project_with([asset])
        clip_a = _clip("c_a", "src", "v1", timeline_start=0)
        clip_b = _clip(
            "c_b", "src", "v1", timeline_start=CLIP_DURATION,
            source_start=CLIP_DURATION,
        )
        transition = TransitionInstance(
            transition_id="t1",
            transition_type="cross_dissolve",
            track_id="v1",
            left_clip_id="c_a",
            right_clip_id="c_b",
            center_frame=CLIP_DURATION,
            duration=8,
            alignment="centered",
        )
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(clip_a, clip_b), transitions=(transition,))
        seq = _sequence([v1])

        # 转场中心附近的帧（两层都可见）。
        transition_frames = (CLIP_DURATION - 2, CLIP_DURATION, CLIP_DURATION + 2)
        frames_on, calls_on = _render_frames(
            project, seq, transition_frames, cull=True)
        frames_off, calls_off = _render_frames(
            project, seq, transition_frames, cull=False)

        # 转场帧 fail-closed：每帧两个 clip 层都被请求。
        assert len(calls_on) == 2 * len(transition_frames)
        assert calls_off == calls_on

        for idx, (on, off) in enumerate(zip(frames_on, frames_off)):
            assert np.array_equal(on, off), (
                f"frame {transition_frames[idx]}: "
                f"max_diff={np.abs(on.astype(int) - off.astype(int)).max()}")


class TestRealMediaB5AlphaMetadata:
    """B5：上轨为带 Alpha 元数据的素材——fail-closed 保留下层。"""

    def test_alpha_upper_keeps_lower(self):
        lower = _make_asset("lower", LOWER_FILE, "yuv420p")
        upper = _make_asset("upper_alpha", UPPER_ALPHA_FILE, "yuva444p10le")
        project = _project_with([lower, upper])
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(_clip("c_lower", "lower", "v1"),))
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                       clips=(_clip("c_upper", "upper_alpha", "v2"),))
        seq = _sequence([v1, v2])

        frames_on, calls_on = _render_frames(project, seq, FRAMES, cull=True)
        frames_off, calls_off = _render_frames(
            project, seq, FRAMES, cull=False)

        # 未知/存在 Alpha → 不裁。
        assert calls_on == ["c_lower", "c_upper"] * len(FRAMES)
        assert calls_off == calls_on
        for on, off in zip(frames_on, frames_off):
            assert np.array_equal(on, off)


class TestRealMediaCancelCleanup:
    """§6.2-5：取消路径不因裁剪结果泄漏 decoder session。"""

    def test_cancel_releases_sessions_and_culled_lower_never_opens(self, tmp_path):
        project, seq = TestRealMediaB1FullOcclusion()._build()
        opened_assets_seen: list = []
        cancel_at = {"frame": None}

        def progress(current, total, fraction):
            opened_assets_seen.append(set(exporter._open_clips.keys()))
            if current >= 2 and cancel_at["frame"] is None:
                cancel_at["frame"] = current
                exporter.cancel_export()

        exporter = create_timeline_exporter(project, seq, progress_callback=progress)
        cfg = _export_config()
        cfg = type(cfg)(**{**{f.name: getattr(cfg, f.name)
                              for f in cfg.__dataclass_fields__.values()},
                           "output_path": str(tmp_path / "cancel.mov")})
        outcome = exporter.export_timeline(cfg)

        # 取消确实发生（第 2 帧后），结果归型为用户取消。
        assert cancel_at["frame"] is not None
        assert bool(getattr(outcome, "cancelled", False)) is True
        # 裁剪生效期间：被裁下轨从未打开过解码会话（每帧只有上轨）。
        assert opened_assets_seen, "progress callback 未被调用"
        for opened in opened_assets_seen:
            # _open_clips 的键是解码会话键（clip 级）：只允许上轨出现。
            assert "c_lower" not in opened
            assert opened <= {"c_upper"}
        # 取消收尾：全部解码会话释放，无泄漏。
        assert dict(exporter._open_clips) == {}


class TestRealMediaPreviewExportParity:
    """§6.3-1：CPU preview vs CPU export 同帧 PSNR（真实解码，无 mock）。"""

    @staticmethod
    def _psnr(a: np.ndarray, b: np.ndarray) -> float:
        diff = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
        mse = float(np.mean(diff * diff))
        if mse <= 0.0:
            return float("inf")
        return 10.0 * float(np.log10(255.0 * 255.0 / mse))

    def test_culled_frame_preview_matches_export(self):
        from src.features.edit.core.timeline_preview_renderer import (
            AssetResolver,
            PreviewRenderWorker,
        )
        from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
        from src.shared.video import get_frame_provider_service

        project, seq = TestRealMediaB1FullOcclusion()._build()
        frame_idx = 3

        # CPU 导出帧（裁剪开启，走 _render_frame_impl）
        oc._ENV_CACHE["TOPOS_TIMELINE_OCCLUSION_CULL"] = True
        exporter = create_timeline_exporter(project, seq)
        exporter._render_plan = TimelineRenderPlan(seq)
        export_frame = np.asarray(
            exporter._render_frame_impl(frame_idx, _export_config()))

        # CPU 预览帧：真实 worker（GPU 关闭 → CPU 合成），同一裁剪入口
        worker = PreviewRenderWorker(
            frame_provider=get_frame_provider_service(),
            asset_resolver=AssetResolver(project),
            video_renderer=None,
            timeline_sequence=seq,
            viewport_size=(SEQ_W, SEQ_H),
            gpu_decode_manager=None,
            gpu_renderer=None,
        )
        try:
            worker._active_decode_target_size = (SEQ_W, SEQ_H)
            plan = TimelineRenderPlan(seq).get_frame_plan(frame_idx)
            pruned = worker._prune_occluded_layers_for_preview(
                plan.layers, plan.transitions, frame_idx)
            preview_frame = worker._composite_layers(
                pruned, plan.transitions, frame_idx)
        finally:
            worker.stop()

        assert preview_frame is not None
        assert export_frame.shape == preview_frame.shape
        psnr = self._psnr(export_frame, preview_frame)
        # 两侧不同解码源（直连预览源 vs 导出会话）允许转换级差异；
        # 40 dB 远高于人眼可辨阈值，足以证明裁剪语义一致。
        assert psnr >= 40.0, f"preview/export PSNR too low: {psnr:.2f} dB"


class TestRealMediaRepeatExportDeterminism:
    """B6（§6.3-4）：同工程经同一 TimelineExporter 漏斗重复导出——
    单文件与批量两个入口都汇入 export_timeline（代码审计见实施文档），
    此处证明该漏斗的裁剪决策与输出画面确定一致。"""

    def test_two_exports_same_decision_and_identical_output(self, tmp_path):
        import av

        project, seq = TestRealMediaB1FullOcclusion()._build()
        paths = [str(tmp_path / f"repeat_{i}.mov") for i in range(2)]
        snapshots = []
        for path in paths:
            oc.get_occlusion_telemetry().reset()
            exporter = create_timeline_exporter(project, seq)
            cfg = _export_config()
            cfg = type(cfg)(**{**{f.name: getattr(cfg, f.name)
                                  for f in cfg.__dataclass_fields__.values()},
                               "output_path": path})
            outcome = exporter.export_timeline(cfg)
            assert bool(outcome), f"export failed: {getattr(outcome, 'cause', '')}"
            snap = oc.get_occlusion_telemetry().snapshot()
            snapshots.append({
                "frames": snap["frames"],
                "culled_layers": snap["culled_layers"],
                "reasons": snap["reasons"],
            })

        # 裁剪决策逐帧一致（帧数/被裁层数/原因分布）。
        assert snapshots[0] == snapshots[1]
        assert snapshots[0]["culled_layers"] > 0

        # 输出画面一致：解码两版首帧做像素对拍。
        first_frames = []
        for path in paths:
            with av.open(path) as container:
                for frame in container.decode(video=0):
                    arr = frame.to_ndarray(format="bgr24")
                    first_frames.append(arr)
                    break
        assert first_frames[0].shape == first_frames[1].shape
        diff = np.asarray(first_frames[0], dtype=np.int16) - np.asarray(
            first_frames[1], dtype=np.int16)
        assert int(np.abs(diff).max()) <= 1, (
            f"repeat export pixels differ: max={int(np.abs(diff).max())}")
