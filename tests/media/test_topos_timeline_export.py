"""R2：真实时间线 Alpha/HDR 导出链（TimelineExporter → ToposVideoEncoder → ToposMediaSource）。

2026-08-30 审计 §4 R2 门禁：真实 TimelineExporter 合成产物经 ToposMediaSource
解码后验证 Alpha/颜色/HDR 数值——不得用 passthrough 测试替代，不得只断言
文件存在或登记点。

覆盖：
- P0-1：时间线导出 Alpha 不被压平（合成缓冲保留 RGBA、_extract_frame_data
  保留第 4 通道、_encode_frame 分派 bgra*）；
- straight-alpha 合成契约：半透明区颜色不被 premultiply 压暗（无镶边）；
- 多层 Composite over 语义；
- HDR（BT.2020+PQ）元数据 roundtrip + 高光信号不被额外裁剪；
- 取消路径资源释放（无输出/临时残留、clip 会话关闭）；
- 字幕烧录 4ch 分支；奇数宽高；编码器浮点信号域契约。

C 库未构建时素材用例 skip（构建见 native/topos_codec/README.md）。
"""
from __future__ import annotations

import os
import types
from typing import Callable, Dict, List, Optional

import numpy as np
import pytest

pytestmark = pytest.mark.media_core

from src.shared.codec import topos_binding
from src.shared.media.frame import DecodedFrame

W, H, N = 64, 40, 3


def _lib_available() -> bool:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


@pytest.fixture(scope="module")
def require_lib():
    if not _lib_available():
        pytest.skip("libtopos_codec 未构建；先运行 bash native/topos_codec/run_tests.sh")


class _FakeFrameProvider:
    """导出用最小 FrameProvider：按 clip_id（=素材路径）返回预置帧。"""

    def __init__(
        self,
        frames_by_path: Dict[str, List[DecodedFrame]],
        on_request: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._frames = frames_by_path
        self._on_request = on_request
        self.opened: List[str] = []
        self.closed_clips: List[str] = []

    def open_clip(self, path: str, share_by_path: bool = False, **_kw) -> str:
        self.opened.append(str(path))
        return str(path)

    def request_frame(self, clip_id: str, frame: int, quality: str = "master"):
        if self._on_request is not None:
            self._on_request(int(frame))
        frames = self._frames[str(clip_id)]
        return frames[int(frame) % len(frames)]

    def close_clip(self, clip_id: str, **_kw) -> None:
        self.closed_clips.append(str(clip_id))


def _bgra_frame(
    i: int,
    color=(160, 80, 40),
    alpha: Optional[np.ndarray] = None,
    w: int = W,
    h: int = H,
) -> DecodedFrame:
    bgra = np.zeros((h, w, 4), np.uint8)
    bgra[..., 0] = color[0]
    bgra[..., 1] = color[1]
    bgra[..., 2] = color[2]
    if alpha is None:
        alpha = np.full((h, w), 255, np.uint8)
    bgra[..., 3] = alpha
    return DecodedFrame(
        frame_index=i, pts=i, time_seconds=i / 24.0,
        data=bgra, pixel_format='bgra', width=w, height=h,
    )


def _alpha_gradient(w: int = W, h: int = H) -> np.ndarray:
    return np.tile(
        (np.arange(w, dtype=np.uint32) * 255 // max(1, w - 1)).astype(np.uint8),
        (h, 1),
    )


def _run_export(
    tmp_path,
    layers: List[Dict],
    name: str,
    has_alpha: bool = True,
    codec=None,
    asset_md: Optional[dict] = None,
    on_request: Optional[Callable[[int], None]] = None,
    w: int = W,
    h: int = H,
    extra_cfg: Optional[dict] = None,
):
    """构造真实工程 + TimelineExporter 并跑完整 export_timeline。

    layers: [{path, frames, track_index}]，自下而上按 track_index 排序合成。
    """
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.shared.media.metadata import Rational

    project = EditProject.create("R2")
    tracks = []
    frames_by_path: Dict[str, List[DecodedFrame]] = {}
    for layer in layers:
        # Delivery QC 预检要求源文件存在（内容不校验——帧来自 fake provider）
        if not os.path.exists(layer["path"]):
            with open(layer["path"], "wb") as fh:
                fh.write(b"placeholder-source")
        md = {
            "width": w, "height": h, "fps": 24.0,
            "total_frames": len(layer["frames"]),
            "duration_seconds": len(layer["frames"]) / 24.0,
        }
        if asset_md:
            md.update(asset_md)
        project = project.import_asset(
            path=layer["path"], media_type="video", metadata=md,
        )
        asset_id = list(project.assets.keys())[-1]
        track_id = f"t{layer['track_index']}"
        clip = ClipInstance(
            clip_id=f"c{layer['track_index']}", asset_id=asset_id,
            track_id=track_id, timeline_start=0,
            timeline_duration=len(layer["frames"]),
            source_start=0, source_duration=len(layer["frames"]),
        )
        tracks.append(EditTrack(
            track_id=track_id, name=f"V{layer['track_index'] + 1}",
            track_type="video", index=layer["track_index"], clips=(clip,),
        ))
        frames_by_path[layer["path"]] = layer["frames"]

    sequence = TimelineSequence(
        name="R2", width=w, height=h, fps=Rational(24, 1),
        video_tracks=tuple(tracks),
    )
    provider = _FakeFrameProvider(frames_by_path, on_request=on_request)
    exporter = TimelineExporter(
        project=project, sequence=sequence, frame_provider=provider,
    )
    out = str(tmp_path / name)
    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id=sequence.timeline_id,
        range_type=ExportRangeType.ENTIRE,
        codec=codec if codec is not None else ExportCodec.TOPOS_HQ,
        output_path=out,
        has_alpha=has_alpha,
        include_audio=False,
        burn_subtitles=False,
        quality=6,
        quality_mode="crf",
        resolution=(w, h),
        **(extra_cfg or {}),
    )
    ok = exporter.export_timeline(cfg).success  # P0-05：布尔消费走 .success
    return ok, out, provider, exporter


def _open_decoded(path: str):
    from src.shared.media.factory import MediaSourceFactory

    src = MediaSourceFactory.create(path)
    src.open()
    return src


def _bt709_luma(bgr, max_val=255.0) -> float:
    r, g, b = (float(bgr[2]) / max_val,
               float(bgr[1]) / max_val,
               float(bgr[0]) / max_val)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _limited_y10(rgb01) -> int:
    y = (0.2126 * rgb01[0] + 0.7152 * rgb01[1] + 0.0722 * rgb01[2])
    return int(round(y * 876.0 + 64.0))


# =============================================================================
# 真实时间线 Alpha 导出（P0-1）
# =============================================================================


class TestTimelineToposAlphaExport:
    def test_timeline_topos_alpha_export_roundtrip(self, require_lib, tmp_path) -> None:
        """单层 BGRA alpha 梯度：解码回的 alpha 与颜色均数值正确（非全不透明）。"""
        alpha = _alpha_gradient()
        frames = [_bgra_frame(i, color=(40, 80, 160), alpha=alpha) for i in range(N)]
        ok, out, _provider, _exp = _run_export(
            tmp_path, [{"path": str(tmp_path / "src.mov"), "frames": frames,
                        "track_index": 0}],
            "alpha_rt.mov")

        assert ok is True, "export_timeline 应成功"
        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.has_alpha is True
            assert md.pixel_format == "topos_yuva422p10a12"
            assert md.total_frames == N
            f = s.read_frame(0)
            assert len(f.planes) == 4
            # x=10 处 a8=40 → a12 ≈ rint(40/255*4095)=642（修复前全不透明 4095）
            a10 = int(f.planes[3][H // 2, 10])
            assert abs(a10 - 642) <= 64, f"alpha={a10}"
            # straight 颜色：低 alpha 列不被 premultiply 压暗——Y 全列恒定
            y_expect = _limited_y10((160 / 255, 80 / 255, 40 / 255))
            for x in (5, 20, 40, 58):
                y_val = int(f.planes[0][H // 2, x])
                assert abs(y_val - y_expect) <= 15, f"Y@x{x}={y_val}≠{y_expect}"
        finally:
            s.close()

    def test_timeline_topos_premultiplied_edge_no_fringe(self, require_lib, tmp_path) -> None:
        """硬边缘（左全透明/右全不透明纯红）：右半颜色纯红无压暗镶边。"""
        w, h = W, H
        alpha = np.zeros((h, w), np.uint8)
        alpha[:, w // 2:] = 255
        frames = [_bgra_frame(i, color=(0, 0, 255), alpha=alpha) for i in range(N)]
        ok, out, _p, _e = _run_export(
            tmp_path, [{"path": str(tmp_path / "edge.mov"), "frames": frames,
                        "track_index": 0}],
            "edge.mov", w=w, h=h)

        assert ok is True
        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            y_red = _limited_y10((1.0, 0.0, 0.0))
            # 右半：alpha 满刻度、Y=纯红亮度（多列取样，边缘列也无暗镶边）
            for x in (w // 2 + 1, w // 2 + 4, w - 4):
                assert int(f.planes[3][h // 2, x]) >= 4095 - 64
                y_val = int(f.planes[0][h // 2, x])
                assert abs(y_val - y_red) <= 20, f"Y@x{x}={y_val}≠{y_red}"
            # 左半：alpha ≈ 0
            for x in (2, w // 4, w // 2 - 2):
                assert int(f.planes[3][h // 2, x]) <= 64
        finally:
            s.close()

    def test_timeline_topos_alpha_odd_dimensions(self, require_lib, tmp_path) -> None:
        """奇数宽高：4:2:2 奇宽路径下 alpha/帧数仍正确。"""
        w, h = 63, 41
        alpha = _alpha_gradient(w, h)
        frames = [_bgra_frame(i, color=(30, 60, 90), alpha=alpha, w=w, h=h)
                  for i in range(2)]
        ok, out, _p, _e = _run_export(
            tmp_path, [{"path": str(tmp_path / "odd.mov"), "frames": frames,
                        "track_index": 0}],
            "odd.mov", w=w, h=h)
        assert ok is True
        s = _open_decoded(out)
        try:
            md = s.probe()
            assert (md.width, md.height) == (w, h)
            assert md.has_alpha is True
            f = s.read_frame(0)
            assert len(f.planes) == 4
            a_mid = int(f.planes[3][h // 2, w // 2])
            expect = int(round((w // 2) * 255 / (w - 1) / 255 * 4095))
            assert abs(a_mid - expect) <= 64
        finally:
            s.close()

    def test_timeline_alpha_disabled_keeps_opaque(self, require_lib, tmp_path) -> None:
        """未勾选 Alpha：输出 3 平面（显式丢弃），帧数完整——既有行为回归钉。"""
        alpha = _alpha_gradient()
        frames = [_bgra_frame(i, color=(40, 80, 160), alpha=alpha) for i in range(N)]
        ok, out, _p, _e = _run_export(
            tmp_path, [{"path": str(tmp_path / "noa.mov"), "frames": frames,
                        "track_index": 0}],
            "no_alpha.mov", has_alpha=False)
        assert ok is True
        s = _open_decoded(out)
        try:
            assert s.probe().has_alpha is False
            assert len(s.read_frame(0).planes) == 3
        finally:
            s.close()


class TestTimelineComposite:
    def test_composite_to_topos_alpha_roundtrip(self, require_lib, tmp_path) -> None:
        """双层 Composite（Porter-Duff over）：不透明灰底 × 半透明红顶层。"""
        w, h = W, H
        bottom = [_bgra_frame(i, color=(128, 128, 128)) for i in range(2)]
        a_grad = _alpha_gradient(w, h)
        top = [_bgra_frame(i, color=(0, 0, 255), alpha=a_grad) for i in range(2)]
        ok, out, _p, _e = _run_export(
            tmp_path, [
                {"path": str(tmp_path / "bottom.mov"), "frames": bottom,
                 "track_index": 0},
                {"path": str(tmp_path / "top.mov"), "frames": top,
                 "track_index": 1},
            ],
            "composite.mov", w=w, h=h)
        assert ok is True

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            row = h // 2
            # x=0（a=0）：纯灰底；over 合成 alpha = a_top + a_bottom(1−a_top)
            # = 满刻度（底不透明）——复验 P2-01：原 `or True` 永真断言移除
            y_gray = _limited_y10((128 / 255, 128 / 255, 128 / 255))
            assert int(f.planes[3][row, 0]) >= 4095 - 64, \
                f"alpha@x0={int(f.planes[3][row, 0])}（不透明底 over 应满刻度）"
            y0 = int(f.planes[0][row, 0])
            assert abs(y0 - y_gray) <= 15, f"Y@x0={y0}≠{y_gray}"
            # x=w-1（a=255）：纯红
            y_red = _limited_y10((1.0, 0.0, 0.0))
            y1 = int(f.planes[0][row, w - 1])
            assert abs(y1 - y_red) <= 20, f"Y@x{w-1}={y1}≠{y_red}"
            assert int(f.planes[3][row, w - 1]) >= 4095 - 64
            # x=中（a≈128）：blend = red*a + gray*(1-a)
            a_f = 128 / 255
            blend = (255 * a_f + 128 * (1 - a_f),
                     128 * (1 - a_f),
                     128 * (1 - a_f))
            y_mid = _limited_y10(tuple(c / 255 for c in blend))
            ym = int(f.planes[0][row, w // 2])
            assert abs(ym - y_mid) <= 15, f"Y@mid={ym}≠{y_mid}"
            # 不透明底 × 半透明顶 → over 合成 alpha 恒满刻度（部分透明
            # 场景由单层 alpha 梯度用例覆盖）
            am = int(f.planes[3][row, w // 2])
            assert am >= 4095 - 64, f"A@mid={am}"
        finally:
            s.close()


# =============================================================================
# HDR（P1-2：显式色彩变换 / 高光不被静默裁剪）
# =============================================================================


class TestTimelineHDR:
    def test_timeline_topos_hdr_highlight_not_clipped(self, require_lib, tmp_path) -> None:
        """BT.2020+PQ 时间线：高光信号（240/255）完整存活 + 元数据 roundtrip。"""
        w, h = W, H
        bgra = np.zeros((h, w, 4), np.uint8)
        bgra[..., :3] = 30          # 暗部
        bgra[:, : w // 2, :3] = 240  # 左半高光（信号域值，非 nits）
        bgra[..., 3] = 255
        frames = [DecodedFrame(frame_index=i, pts=i, time_seconds=i / 24.0,
                               data=bgra.copy(), pixel_format='bgra',
                               width=w, height=h) for i in range(2)]
        ok, out, _p, _e = _run_export(
            tmp_path, [{"path": str(tmp_path / "hdr.mov"), "frames": frames,
                        "track_index": 0}],
            "hdr.mov", has_alpha=False,
            asset_md={"color_primaries": "bt2020", "color_transfer": "smpte2084",
                      "color_matrix": "bt2020nc", "color_range": "limited"},
            w=w, h=h)
        assert ok is True

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert str(md.color_transfer).lower() == "smpte2084"
            assert str(md.color_matrix).lower() in ("bt2020nc", "bt2020")
            assert str(md.color_primaries).lower() == "bt2020"
            f = s.read_frame(0)
            row = h // 2
            y_bright_expect = _limited_y10((240 / 255, 240 / 255, 240 / 255))
            y_dark_expect = _limited_y10((30 / 255, 30 / 255, 30 / 255))
            y_bright = int(f.planes[0][row, w // 4])
            y_dark = int(f.planes[0][row, w - w // 4])
            # 高光未被额外裁剪/压平：接近 240 信号值的期望亮度
            assert abs(y_bright - y_bright_expect) <= 25, \
                f"Y_bright={y_bright}≠{y_bright_expect}"
            assert abs(y_dark - y_dark_expect) <= 15, \
                f"Y_dark={y_dark}≠{y_dark_expect}"
            assert y_bright - y_dark > 600   # 对比度存活
        finally:
            s.close()

    def test_linear_to_signal_transfer_explicit(self) -> None:
        """comp clip 场景线性值 → 导出信号：显式 OETF，2.0 与 1.0 线性可区分。"""
        from src.features.edit.core.timeline_export import TimelineExporter

        linear = np.array([[[2.0, 1.0, 0.5],
                            [1.0, 0.5, 0.05]]], dtype=np.float32)
        signal = TimelineExporter._linear_bgr_to_export_signal(
            linear, transfer="bt709")
        assert signal is not None
        assert signal.shape == linear.shape
        # 单调：线性 2.0 > 1.0 > 0.5 > 0.05 的信号值严格递减（未在某处被
        # [0,1] 硬裁成相同值）
        vals = [float(signal[0, 0, 0]), float(signal[0, 0, 1]),
                float(signal[0, 1, 1]), float(signal[0, 1, 2])]
        assert vals == sorted(vals, reverse=True), vals
        assert vals[0] > vals[1], "线性 2.0 与 1.0 不得映射到同一信号值"
        assert all(0.0 <= v <= 1.0 for v in vals)


# =============================================================================
# 取消与资源释放
# =============================================================================


class TestTimelineCancel:
    def test_timeline_topos_cancel_releases_gpu_and_native_resources(
            self, require_lib, tmp_path) -> None:
        """中途取消：无输出/临时残留，clip 会话关闭，编码器置空。"""
        alpha = _alpha_gradient()
        frames = [_bgra_frame(i, color=(40, 80, 160), alpha=alpha)
                  for i in range(6)]
        holder = {}

        def on_request(frame: int) -> None:
            if frame == 2 and "exporter" in holder:
                holder["exporter"].cancel_export()

        layers = [{"path": str(tmp_path / "cancel_src.mov"), "frames": frames,
                   "track_index": 0}]
        # 复用 _run_export 的构造，但需要 exporter 引用 → 手动跑一次带钩子
        from src.features.edit.core.project import EditProject
        from src.features.edit.core.timeline_export import (
            ExportCodec, ExportRangeType, ExportSource,
            TimelineExportConfig, TimelineExporter,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, TimelineSequence,
        )
        from src.shared.media.metadata import Rational

        project = EditProject.create("R2-cancel")
        if not os.path.exists(layers[0]["path"]):
            with open(layers[0]["path"], "wb") as fh:
                fh.write(b"placeholder-source")
        project = project.import_asset(
            path=layers[0]["path"], media_type="video",
            metadata={"width": W, "height": H, "fps": 24.0,
                      "total_frames": len(frames),
                      "duration_seconds": len(frames) / 24.0},
        )
        asset_id = list(project.assets.keys())[-1]
        clip = ClipInstance(
            clip_id="c0", asset_id=asset_id, track_id="t0",
            timeline_start=0, timeline_duration=len(frames),
            source_start=0, source_duration=len(frames),
        )
        sequence = TimelineSequence(
            name="R2-cancel", width=W, height=H, fps=Rational(24, 1),
            video_tracks=(EditTrack(track_id="t0", name="V1",
                                    track_type="video", index=0, clips=(clip,)),),
        )
        provider = _FakeFrameProvider(
            {layers[0]["path"]: frames}, on_request=on_request)
        exporter = TimelineExporter(
            project=project, sequence=sequence, frame_provider=provider)
        holder["exporter"] = exporter
        out = str(tmp_path / "cancelled.mov")
        outcome = exporter.export_timeline(TimelineExportConfig(
            source=ExportSource.TIMELINE, timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE, codec=ExportCodec.TOPOS_HQ,
            output_path=out, has_alpha=True, include_audio=False,
            burn_subtitles=False, quality=6, quality_mode="crf",
            resolution=(W, H),
        ))

        # P0-05：取消 = cancelled 显式 True，不得报成功
        assert outcome.success is False, "取消的导出不得报成功"
        assert outcome.cancelled is True, "用户取消必须显式归型为 cancelled"
        assert not os.path.exists(out)
        assert not os.path.exists(out + ".topos-tmp")
        assert exporter._encoder is None
        assert exporter._is_exporting is False
        assert provider.closed_clips, "取消后 clip 会话应被关闭"


# =============================================================================
# 字幕烧录 4ch（straight-alpha over 不破坏帧 alpha）
# =============================================================================


class TestSubtitleBurnInAlpha:
    def test_burn_in_subtitles_keeps_frame_alpha(self, tmp_path) -> None:
        from src.features.edit.core.timeline_export import TimelineExporter

        exporter = TimelineExporter.__new__(TimelineExporter)
        w, h = 16, 8
        frame = np.zeros((h, w, 4), np.uint8)
        frame[..., 0] = 40
        frame[..., 1] = 80
        frame[..., 2] = 160
        frame[..., 3] = 128          # 半透明底
        frame[:, :8, 3] = 255        # 左半不透明

        seq = types.SimpleNamespace()
        seq.get_active_subtitle_clips = lambda _f: [types.SimpleNamespace(
            clip_id="s1", track_id="st1", style_override=None)]

        def _track_by_id(_tid):
            return None

        seq.get_track_by_id = _track_by_id
        seq.caption_track_graphic_styles = ()
        exporter._sequence = seq

        import unittest.mock as mock
        sub_rgba = np.zeros((h, w, 4), np.float32)
        sub_rgba[:, 4:12, 0] = 1.0   # 红色字幕块
        sub_rgba[:, 4:12, 3] = 1.0

        with mock.patch(
            "src.features.edit.core.subtitle_renderer."
            "render_subtitle_clip_to_rgba_cached",
            return_value=sub_rgba,
        ):
            out = exporter._burn_in_subtitles(frame, 0, w, h)

        assert out.shape == (h, w, 4), "4ch 输入必须保持 4ch（alpha 不丢）"
        # 字幕区（左半底不透明）：红 + 底不透明
        assert int(out[4, 6, 2]) >= 250 and int(out[4, 6, 3]) == 255
        # 字幕区内右半（底半透明）：字幕不透明地盖上去 → alpha 提到 255
        assert int(out[4, 10, 2]) >= 250 and int(out[4, 10, 3]) == 255
        # 非字幕区：底 alpha 原样（右半 128）
        assert int(out[4, 14, 3]) == 128
        assert int(out[4, 14, 2]) == 160   # 底色不被翻通道


# =============================================================================
# FFmpeg 编码路径四通道（R2 后首次可达——必须有端到端证据）
# =============================================================================


class TestFFmpegAlphaPath:
    def test_prores4444_timeline_alpha_roundtrip(self, tmp_path) -> None:
        """ProRes 4444 + Alpha：真实时间线导出 → PyAV 解码校验 alpha 通道。

        R2 之前 alpha 在合成层就被丢弃（输出恒不透明）；R2 后该路径可达，
        本用例锁定 rgba64le 重排/reformat 链路可用且 alpha 数值正确。
        """
        av = pytest.importorskip("av")
        try:
            # ProRes 4444 编码器可用性（prores_ks）
            codec = next(
                (c for c in av.codecs_available if 'prores' in c), None)
            if codec is None:
                pytest.skip("无 ProRes 编码器")
        except Exception:
            pytest.skip("PyAV 编码器枚举失败")

        from src.features.edit.core.timeline_export import ExportCodec

        alpha = _alpha_gradient()
        frames = [_bgra_frame(i, color=(40, 80, 160), alpha=alpha)
                  for i in range(2)]
        ok, out, _p, _e = _run_export(
            tmp_path, [{"path": str(tmp_path / "ff_src.mov"), "frames": frames,
                        "track_index": 0}],
            "ff_alpha.mov", codec=ExportCodec.PRORES_4444)
        assert ok is True, "ProRes 4444 + Alpha 导出应成功"

        container = av.open(out)
        try:
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                fmt_name = str(frame.format.name)
                assert 'yuva' in fmt_name, \
                    f"ProRes 4444 + Alpha 应产出 yuva* 格式，得到 {fmt_name}"
                assert len(frame.planes) == 4, \
                    f"ProRes 4444 Alpha 应产出 4 平面，得到 {len(frame.planes)}"
                depth = next((d for d in (16, 14, 12, 10, 8)
                              if f"p{d}" in fmt_name), 8)
                full = float((1 << depth) - 1)
                a_plane = np.frombuffer(
                    frame.planes[3], dtype=np.uint16
                ).reshape(frame.height, frame.width)
                # x=10 处 a8=40 → alpha 满刻度按位深换算
                a_val = float(a_plane[H // 2, 10])
                expect = 40 / 255.0 * full
                assert abs(a_val - expect) <= 4 * full / 255.0, \
                    f"A={a_val}≠{expect}(depth={depth})"
                break
        finally:
            container.close()


# =============================================================================
# 编码器浮点信号域契约（P1-2 防御性修复）
# =============================================================================


class TestEncoderFloatSignalDomain:
    def test_float_input_warns_and_quantizes(self, require_lib, tmp_path, caplog) -> None:
        """>1.0 浮点高光：显式警告（非静默裁剪）+ 结果有效。"""
        import logging as _logging

        from src.shared.export.topos_encoder import ToposVideoEncoder
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.metadata import Rational

        out = str(tmp_path / "float_signal.mov")
        cfg = FFmpegEncoderConfig(
            codec='topos', width=W, height=H, fps=Rational(24, 1),
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=6,
            has_alpha=True)
        enc = ToposVideoEncoder(out, cfg)
        enc.open()
        data = np.zeros((H, W, 4), np.float32)
        data[:, :W // 2, :3] = 2.0   # 左半白色高光 >1.0（Y 将触码值天花板）
        data[:, W // 2:, 0] = 0.5    # 右半 R=0.5（RGBA 通道 0 = R）
        data[..., 3] = 1.0
        with caplog.at_level(_logging.WARNING,
                             logger='src.shared.export.topos_encoder'):
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                data=data, pixel_format='rgba', width=W, height=H))
        enc.close()
        assert '信号域' in caplog.text or '[0,1]' in caplog.text

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            y_half = _limited_y10((0.5, 0.0, 0.0))
            y_right = int(f.planes[0][H // 2, W - 4])
            assert abs(y_right - y_half) <= 15, f"Y_right={y_right}≠{y_half}"
            y_left = int(f.planes[0][H // 2, 4])
            assert y_left >= 1000   # 白色 2.0 高光在码值上限（容器天花板）
        finally:
            s.close()


# =============================================================================
# O1（复验 2026-08-31）：数据语义链——P0-02 / P0-03 / P0-04 / P1-01
# =============================================================================


def _bare_exporter():
    """只调用 _extract_frame_data/_apply_crop_cpu 等纯方法的裸实例。"""
    from src.features.edit.core.timeline_export import TimelineExporter

    return object.__new__(TimelineExporter)


class TestO1DataSemantics:
    def test_timeline_rgba_to_bgra_keeps_alpha_last(self) -> None:
        """P0-02（最小复现 1）：RGBA [10,20,30,40] → BGRA [30,20,10,40]。

        此前 np.flip 整轴翻转得 [40,30,20,10]——Alpha 被翻到首通道，
        颜色与 Alpha 同时错误。
        """
        exp = _bare_exporter()

        class _F:
            data = np.array([[[10, 20, 30, 40]]], np.uint8)
            pixel_format = "rgba"
            planes = ()
            width, height = 1, 1

        out = exp._extract_frame_data(_F(), keep_alpha=True)
        assert out.shape == (1, 1, 4)
        assert out[0, 0].tolist() == [30, 20, 10, 40], out[0, 0].tolist()

    def test_alpha_crop_rgba8_and_rgba16(self) -> None:
        """P1-01：四通道 crop——不再 NameError；RGBA16 支持；straight 语义
        （仅缩放 alpha，羽化边缘 RGB 不被压暗）。"""
        w, h = 32, 24
        for dtype in (np.uint8, np.uint16):
            mx = 255 if dtype == np.uint8 else 65535
            frame = np.zeros((h, w, 4), dtype)
            frame[..., 0] = 30 * (mx // 255)
            frame[..., 1] = 20 * (mx // 255)
            frame[..., 2] = 10 * (mx // 255)
            frame[..., 3] = mx
            layer = types.SimpleNamespace(
                crop=(0.0, 0.5, 0.0, 0.0, 0.0))  # 裁掉右半（无羽化）
            out = exp_crop(frame, layer)
            assert out.dtype == dtype
            # 右半（裁剪区外）：alpha=0（此前 NameError / premultiply 语义下
            # RGB 也被压暗——straight 语义 RGB 保留）
            assert int(out[0, w - 2, 3]) == 0
            assert int(out[0, w - 2, 2]) == 10 * (mx // 255), \
                "straight 裁剪：裁剪区外 RGB 保留（由 alpha=0 屏蔽）"
            # 左半：alpha 满刻度不变
            assert int(out[0, 2, 3]) == mx

    def test_planar_alpha16_independent_depth(self) -> None:
        """P0-03（最小复现 3）：a8 满刻度 255 经 16-bit 转换应为 65535，
        不是 255/1023*65535≈16336（alpha 用颜色位深归一化的旧缺陷）。"""
        from src.shared.media.frame import PlaneInfo

        exp = _bare_exporter()
        w, h = 16, 8
        cw = w // 2
        planes = (np.full((h, w), 512, np.uint16),
                  np.full((h, cw), 512, np.uint16),
                  np.full((h, cw), 512, np.uint16),
                  np.full((h, w), 255, np.uint16))
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, h, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, h, 'V', np.uint16, 10),
                 PlaneInfo.create(3, w, h, 'A', np.uint16, 8))
        out = exp._convert_yuv_planes_to_bgr16(
            planes, "topos_yuva422p10a8", metadata=None,
            plane_infos=infos, keep_alpha=True)
        assert out is not None and out.shape[-1] == 4
        assert int(out[0, 0, 3]) == 65535, int(out[0, 0, 3])

    def test_bit_depth_parser_ignores_alpha_suffix(self) -> None:
        """P0-03：a12/a16 后缀不得污染颜色位深推断。"""
        exp = _bare_exporter()
        assert exp._bit_depth_for_pixel_format("topos_yuva422p10a16", None) == 10
        assert exp._bit_depth_for_pixel_format("topos_gbrap12a8", None) == 12
        assert exp._bit_depth_for_pixel_format("yuv422p10le", None) == 10


def exp_crop(frame, layer):
    """_apply_crop_cpu 的实例方法绑定捷径（方法只读 self 无状态）。"""
    from src.features.edit.core.timeline_export import TimelineExporter

    return TimelineExporter._apply_crop_cpu(
        object.__new__(TimelineExporter), frame, layer)


class TestO1ToposReexport:
    def test_topos_planar_reexport_preserves_alpha_and_color(
            self, require_lib, tmp_path) -> None:
        """P0-03（最小复现 4）：Topos planar 素材重导出不得损坏 Alpha/颜色。

        链路：BGRA 梯度 → Topos(a12) → ToposMediaSource planar 解码帧作为
        时间线源 → 再导出 Topos → 解码比对。修复前 FFmpeg 打不开 TPIC、
        逐帧重试后落入低深度 fallback（dtype 宽度右移 + 忽略 range/matrix），
        Alpha 样本 2056 → 128 级别的损坏仍返回"成功"。
        """
        # 第一遍：真实导出建立 planar Topos 源
        alpha = _alpha_gradient()
        frames = [_bgra_frame(i, color=(40, 80, 160), alpha=alpha) for i in range(2)]
        ok1, out1, _p, _e = _run_export(
            tmp_path, [{"path": str(tmp_path / "src1.mov"), "frames": frames,
                        "track_index": 0}],
            "pass1.mov")
        assert ok1 is True
        s1 = _open_decoded(out1)
        try:
            src_frames = [s1.read_frame(i) for i in range(2)]
        finally:
            s1.close()

        # 第二遍：planar Topos 帧直接作为时间线源（get_source_frame 的
        # topos 直通分支 → _finish_source_frame → planar→BGR 转换）
        fake_frames = []
        for f in src_frames:
            fake_frames.append(f)
        ok2, out2, _p2, _e2 = _run_export(
            tmp_path, [{"path": out1, "frames": fake_frames,
                        "track_index": 0}],
            "pass2.mov")
        assert ok2 is True, "Topos planar 源重导出应成功（不再经 FFmpeg）"

        s2 = _open_decoded(out2)
        try:
            f2 = s2.read_frame(0)
            assert len(f2.planes) == 4
            # alpha 梯度形状保持：x=10 处 a8=40 → a12≈642（与第一遍同值）
            a10 = int(f2.planes[3][H // 2, 10])
            assert abs(a10 - 642) <= 40, f"重导出 alpha 损坏：{a10}"
            # 颜色：Y 与第一遍一致（±2 LSB——8-bit 合成往返）
            f1 = src_frames[0]
            y1 = int(f1.planes[0][H // 2, 32])
            y2 = int(f2.planes[0][H // 2, 32])
            assert abs(y2 - y1) <= 6, f"重导出颜色漂移：{y2} vs {y1}"
            # alpha 曲线整体斜率保持（修复前 2056→128 的压缩不会只差 LSB）
            a_left = int(f2.planes[3][H // 2, 2])
            a_right = int(f2.planes[3][H // 2, W - 2])
            assert a_right - a_left >= 3800, (a_left, a_right)
        finally:
            s2.close()

    def test_timeline_premultiplied_source_unpremultiplies_before_composite(
            self, require_lib, tmp_path) -> None:
        """P0-04：Topos premultiplied 源进 CPU 合成前必须 unpremultiply。

        顶层 = premultiplied GBR 平面（50% 红：R=预乘半幅，A=128/255），
        底层 = 不透明灰(128)。straight 语义合成结果 ≈ blend(red, 0.502)；
        修复前预乘值被当 straight 合成，红色被系统性压暗（Y 差 ~47/876）。
        """
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = W, H
        gbr_planes = (
            np.zeros((h, w), np.uint16),                     # G
            np.zeros((h, w), np.uint16),                     # B
            np.full((h, w), 512, np.uint16),                 # R 预乘 ≈ 50%
            np.full((h, w), 128, np.uint16),                 # A（a8 满刻度=255）
        )
        infos = (PlaneInfo.create(0, w, h, 'G', np.uint16, 10),
                 PlaneInfo.create(1, w, h, 'B', np.uint16, 10),
                 PlaneInfo.create(2, w, h, 'R', np.uint16, 10),
                 PlaneInfo.create(3, w, h, 'A', np.uint16, 8))
        top = [DecodedFrame(
            frame_index=i, pts=i, time_seconds=i / 24.0,
            planes=gbr_planes, pixel_format="topos_gbrap10a8",
            width=w, height=h, plane_infos=infos,
            extra={"topos_alpha_premultiplied": True}) for i in range(2)]
        bottom = [_bgra_frame(i, color=(128, 128, 128)) for i in range(2)]

        ok, out, _p, _e = _run_export(
            tmp_path, [
                {"path": str(tmp_path / "gray.mov"), "frames": bottom,
                 "track_index": 0},
                {"path": str(tmp_path / "pm.mov"), "frames": top,
                 "track_index": 1},
            ],
            "premult_comp.mov", w=w, h=h)
        assert ok is True

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            row = h // 2
            x = w // 2
            a_f = 128 / 255
            # straight 合成：R = 1.0*a + 0.502*(1-a)；G/B = 0.502*(1-a)
            r_c = 1.0 * a_f + (128 / 255) * (1 - a_f)
            g_c = (128 / 255) * (1 - a_f)
            y_expect = _limited_y10((r_c, g_c, g_c))
            y_got = int(f.planes[0][row, x])
            assert abs(y_got - y_expect) <= 20, \
                f"premult 合成错误：Y={y_got}，straight 期望 {y_expect}"
            # 旧缺陷（预乘被当 straight）时 Y ≈ 330，偏差 >47 超出容差
            # over 不透明底 → alpha 满刻度
            assert int(f.planes[3][row, x]) >= 4095 - 64
        finally:
            s.close()


class TestO1HdrFloatLayer:
    def test_scene_linear_over_one_survives_pq_chain(
            self, require_lib, tmp_path) -> None:
        """P1-04：普通 float 层场景线性 >1.0 经真实 TimelineExporter→Topos
        →解码不被裁平（此前 _normalise_bgr_frame 无条件 clip 到 [0,1]，
        >1 高光进编码器前已丢失；只有 Comp Clip 专用路径有 OETF）。"""
        w, h = W, H
        bgra = np.zeros((h, w, 4), np.float32)
        bgra[:, : w // 2, :3] = 4.0     # 左半：场景线性 4.0（>1 高光）
        bgra[:, w // 2:, :3] = 0.05     # 右半：暗部
        bgra[..., 3] = 1.0
        frames = [DecodedFrame(frame_index=i, pts=i, time_seconds=i / 24.0,
                               data=bgra.copy(), pixel_format='bgra',
                               width=w, height=h) for i in range(2)]
        ok, out, _p, _e = _run_export(
            tmp_path, [{"path": str(tmp_path / "hdrl.mov"), "frames": frames,
                        "track_index": 0}],
            "hdrl.mov", has_alpha=False,
            asset_md={"color_primaries": "bt2020", "color_transfer": "smpte2084",
                      "color_matrix": "bt2020nc", "color_range": "limited"},
            w=w, h=h)
        assert ok is True

        from src.features.edit.core.timeline_export import TimelineExporter

        # 期望值必须用与导出一致的 primaries/range 计算（bt2020 与 bt709
        # 的 LINEAR_SCENE→PQ 映射不同）
        sig4 = TimelineExporter._linear_bgr_to_export_signal(
            np.full((1, 1, 3), 4.0, np.float32),
            transfer="smpte2084", primaries="bt2020", color_range="limited")
        sig1 = TimelineExporter._linear_bgr_to_export_signal(
            np.full((1, 1, 3), 1.0, np.float32),
            transfer="smpte2084", primaries="bt2020", color_range="limited")
        assert sig4 is not None and sig1 is not None
        y4 = _limited_y10((float(sig4[0, 0, 2]),
                           float(sig4[0, 0, 1]), float(sig4[0, 0, 0])))
        y1 = _limited_y10((float(sig1[0, 0, 2]),
                           float(sig1[0, 0, 1]), float(sig1[0, 0, 0])))
        assert y4 > y1 + 30, (y4, y1)  # OETF 单调：4.0 与 1.0 可区分

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            row = h // 2
            y_bright = int(f.planes[0][row, w // 4])
            y_dark = int(f.planes[0][row, w - w // 4])
            # 高光按 OETF 编码（若被 [0,1] 裁平则恰为 y1）
            assert abs(y_bright - y4) <= 30, \
                f"Y_bright={y_bright}，线性 4.0 期望 {y4}（裁平值 {y1}）"
            assert y_bright > y1 + 25, "线性 4.0 与 1.0 不得映射同一信号值"
            assert y_bright - y_dark > 120, (y_bright, y_dark)
        finally:
            s.close()


class TestR7ExportControlsPassthrough:
    """R7 交付面收口：TimelineExportConfig.pixel_format / alpha_budget_policy
    从导出配置到编码器的全链透传（UI 侧见
    tests/deliver/test_video_export_topos_controls.py）。"""

    def test_get_codec_settings_honors_pixel_format_override(self) -> None:
        """444 档显式 pix_fmt 覆盖 codec_map 默认；非 Topos codec 忽略覆盖。"""
        exp = _bare_exporter()
        from src.features.edit.core.timeline_export import (
            ExportCodec, TimelineExportConfig,
        )

        cfg = TimelineExportConfig(
            codec=ExportCodec.TOPOS_4444, pixel_format="gbrp12le")
        assert exp._get_codec_settings(cfg) == ("topos", "gbrp12le", "4444")

        cfg = TimelineExportConfig(
            codec=ExportCodec.TOPOS_HQ, pixel_format="yuv444p10le")
        assert exp._get_codec_settings(cfg) == ("topos", "yuv444p10le", "hq")

        # 非 Topos：覆盖不生效（保持 codec_map 值）
        cfg = TimelineExportConfig(
            codec=ExportCodec.PRORES_422, pixel_format="gbrp12le")
        assert exp._get_codec_settings(cfg)[1] == "yuv422p10le"

        # 未设置（默认空串）：档位默认
        cfg = TimelineExportConfig(codec=ExportCodec.TOPOS_4444XQ)
        assert exp._get_codec_settings(cfg) == ("topos", "yuv444p12le", "4444xq")

    def test_4444_gbr_export_produces_planar_rgb_source(
            self, require_lib, tmp_path) -> None:
        """E2E：Pro444 + gbrp10le → 解码侧 pf=2 → planar_rgb 输出模式。"""
        from src.features.edit.core.timeline_export import ExportCodec

        frames = [_bgra_frame(k, color=(150 + k, 90 + k, 40 + k))
                  for k in range(N)]
        src = str(tmp_path / "gbr_src.png")
        ok, out, _, _ = _run_export(
            tmp_path, [{"path": src, "frames": frames, "track_index": 0}],
            "r7_gbr.mov", has_alpha=False, codec=ExportCodec.TOPOS_4444,
            extra_cfg={"pixel_format": "gbrp10le"})
        assert ok
        from src.shared.media.topos_source import ToposMediaSource
        ts = ToposMediaSource(out)
        ts.open()
        assert ts.output_mode == "planar_rgb"
        ts.close()

    def test_alpha_budget_policy_error_rejects_overrun(
            self, require_lib, tmp_path) -> None:
        """policy=error：Alpha 噪声撑爆 proxy 档硬上限 → 结构化拒绝
        （RuntimeError 从编码器冒出，不产出文件）。"""
        rng = np.random.default_rng(7)
        frames = [
            _bgra_frame(k, color=(64, 64, 64),  # 平坦颜色 → 色彩载荷极小
                        alpha=rng.integers(0, 256, (H, W)).astype(np.uint8))
            for k in range(2)]
        src = str(tmp_path / "noise_src.png")
        from src.features.edit.core.timeline_export import ExportCodec
        # export_timeline 捕获编码器异常并返回失败（错误消息进日志）——
        # 结构化拒绝 = ok False + 不产出文件
        ok, out, _, _ = _run_export(
            tmp_path, [{"path": src, "frames": frames, "track_index": 0}],
            "r7_err.mov", has_alpha=True, codec=ExportCodec.TOPOS_PROXY,
            extra_cfg={"alpha_budget_policy": "error"})
        assert ok is False
        assert not os.path.exists(out)

    def test_alpha_budget_policy_continue_authorizes_delivery(
            self, require_lib, tmp_path) -> None:
        """policy=continue：同一噪声素材授权交付，tpcB 记 authorized 标志。"""
        rng = np.random.default_rng(7)
        frames = [
            _bgra_frame(k, color=(64, 64, 64),
                        alpha=rng.integers(0, 256, (H, W)).astype(np.uint8))
            for k in range(2)]
        src = str(tmp_path / "noise_src2.png")
        from src.features.edit.core.timeline_export import ExportCodec
        ok, out, _, _ = _run_export(
            tmp_path, [{"path": src, "frames": frames, "track_index": 0}],
            "r7_cont.mov", has_alpha=True, codec=ExportCodec.TOPOS_PROXY,
            extra_cfg={"alpha_budget_policy": "continue"})
        assert ok
        from src.shared.media.topos_source import ToposMediaSource
        ts = ToposMediaSource(out)
        ts.open()
        fr = ts.read_frame(0)
        ts.close()
        bud = (fr.extra or {}).get("topos_alpha_budget") or {}
        assert bud.get("flags", {}).get("authorized") is True
