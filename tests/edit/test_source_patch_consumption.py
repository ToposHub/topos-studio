"""F2.1 · Source Patching 渲染消费片——预览选道/导出选道/插入四门。

三条消费路径共用 ``SourcePatchSpec.select_channels``（模型级单点）：
- 预览：AudioMixer.mix_samples 经 decoder._source_channel_map 选道；
- 成片导出：TimelineExporter._render_audio_master 交付适配前选道；
- Stem：create_clip_decode_fn 的 decode 回调统一选道（保调/retime/直读）。

插入/覆盖/追加/拖放四个入口按 patch 的 video/audio_enabled 决定成对落轨
（audio off = video-only；video off = audio-only 重定向；双关拒绝）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.source_patch import SourcePatchSpec, patch_for_asset
from src.features.edit.core.stem_export import (
    StemSpec,
    StemType,
    create_clip_decode_fn,
)
from src.features.edit.core.timeline_export import (
    TimelineExporter,
    TimelineExportConfig,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

SR = 48000
FPS = 25
#: 6 声道各给一个可分辨的 DC 电平（ch_i = 0.05*(i+1)）
DC_AMPS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]


# ── 模型级：select_channels ─────────────────────────────────────────────


class TestSelectChannels:
    def _pcm(self) -> np.ndarray:
        return np.stack([
            np.full(64, amp, dtype=np.float32) for amp in DC_AMPS
        ])

    def test_selects_rows_ordered_by_target(self) -> None:
        out = SourcePatchSpec(
            audio_channel_map=((3, 0), (0, 1)),
        ).select_channels(self._pcm())
        assert out.shape == (2, 64)
        # 目标通道 0 ← 源 3；目标通道 1 ← 源 0
        assert float(np.mean(out[0])) == pytest.approx(DC_AMPS[3], abs=1e-6)
        assert float(np.mean(out[1])) == pytest.approx(DC_AMPS[0], abs=1e-6)

    def test_identity_without_map(self) -> None:
        pcm = self._pcm()
        assert SourcePatchSpec().select_channels(pcm) is pcm

    def test_identity_when_map_exceeds_decoded_channels(self) -> None:
        """映射基线 6ch、实际解码 2ch（布局漂移）——退直通不崩。"""
        stereo = np.zeros((2, 8), dtype=np.float32)
        out = SourcePatchSpec(
            audio_channel_map=((5, 0),),
        ).select_channels(stereo)
        assert out is stereo


# ── 预览：mix_samples 消费 decoder._source_channel_map ──────────────────


class _MapDecoder:
    """每声道独立 DC 电平的假 decoder（getattr 守卫的属性全默认）。"""

    def __init__(self, amps: list[float], channel_map=None) -> None:
        self._amps = amps
        self._source_channel_map = channel_map

    def decode_samples_for_frame(self, frame, fps, num_samples):
        return np.stack([
            np.full(num_samples, amp, dtype=np.float32)
            for amp in self._amps
        ])

    def close(self) -> None:
        pass


class TestMixerAppliesChannelMap:
    def test_center_channel_only_reaches_output(self) -> None:
        """5.1 源单选 LFE（源声道 3）——单声道输出只剩该声道电平。"""
        mixer = AudioMixer()
        decoder = _MapDecoder(DC_AMPS, channel_map=((3, 0),))
        mixer.add_track("t1", decoder, AudioTrackState(track_id="t1"))
        mixed, _ = mixer.mix_samples(0, FPS, 64, output_channels=1)
        assert mixed.shape[0] == 1
        assert float(np.mean(mixed[0])) == pytest.approx(DC_AMPS[3], abs=1e-4)

    def test_channel_reorder_reaches_output(self) -> None:
        """立体声源 L/R 对调（(1,0),(0,1)）——双声道直出验证重排。"""
        mixer = AudioMixer()
        decoder = _MapDecoder([0.10, 0.20], channel_map=((1, 0), (0, 1)))
        mixer.add_track("t1", decoder, AudioTrackState(track_id="t1"))
        mixed, _ = mixer.mix_samples(0, FPS, 64, output_channels=2)
        assert float(np.mean(mixed[0])) == pytest.approx(0.20, abs=1e-4)  # L ← 源 R
        assert float(np.mean(mixed[1])) == pytest.approx(0.10, abs=1e-4)  # R ← 源 L

    def test_no_map_keeps_standard_downmix(self) -> None:
        """无映射 = 现状行为：6→2 走 BS.775 标准下混（非单选直出）。"""
        mixer = AudioMixer()
        mixer.add_track(
            "t1", _MapDecoder(DC_AMPS), AudioTrackState(track_id="t1"))
        mixed, _ = mixer.mix_samples(0, FPS, 64, output_channels=2)
        # 标准下混是各声道加权和，不等于纯 ch3 电平
        assert float(np.mean(mixed[0])) != pytest.approx(DC_AMPS[3], abs=1e-3)


# ── 导出/Stem：真实 WAV + 生产 decode 回调 ─────────────────────────────


def _write_wav(path: Path, pcm: np.ndarray, sample_rate: int = SR) -> Path:
    import av

    channels = pcm.shape[0]
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")
    container = av.open(str(path), mode="w", format="wav")
    stream = container.add_stream("pcm_s16le", rate=sample_rate)
    stream.layout = layout
    block = sample_rate // 5
    for start in range(0, pcm.shape[1], block):
        chunk = np.clip(pcm[:, start:start + block], -1.0, 1.0)
        frame = av.AudioFrame.from_ndarray(
            chunk.astype(np.float32), format="fltp", layout=layout)
        frame.sample_rate = sample_rate
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return path


def _dc_wav(path: Path) -> Path:
    pcm = np.stack([
        np.full(SR, amp, dtype=np.float32) for amp in DC_AMPS
    ])
    return _write_wav(path, pcm)


def _audio_timeline(asset_id: str, duration: int = 48) -> TimelineSequence:
    clip = ClipInstance(
        "clip-1", asset_id, "a0", 0, duration, 0, duration, speed=1.0)
    track = EditTrack("a0", "A1", "audio", 0, clips=(clip,), volume=1.0)
    return TimelineSequence(
        timeline_id="seq-a", name="A", width=64, height=48,
        fps=Rational(FPS, 1), audio_tracks=(track,),
    )


def _project(wav: Path, spec: SourcePatchSpec | None) -> EditProject:
    asset = MediaAsset("asset-1", str(wav), "audio", "DC6", {})
    project = EditProject("p", "P", assets={"asset-1": asset})
    if spec is not None:
        project = project.with_source_patch_override("asset-1", spec)
    return project


class TestExportHonoursChannelMap:
    def test_master_render_selects_channel(self, tmp_path) -> None:
        """成片 _render_audio_master：映射生效时只剩源声道 3 的电平。"""
        wav = _dc_wav(tmp_path / "dc6.wav")
        config = TimelineExportConfig(
            output_path=str(tmp_path / "out.mov"), audio_channels=1)

        baseline = TimelineExporter(
            _project(wav, None), _audio_timeline("asset-1"), Mock(),
        )._render_audio_master(0, 24, config, encoder=Mock())
        patched = TimelineExporter(
            _project(wav, SourcePatchSpec(audio_channel_map=((3, 0),))),
            _audio_timeline("asset-1"), Mock(),
        )._render_audio_master(0, 24, config, encoder=Mock())

        assert baseline is not None and patched is not None
        # 单声道输出：基线 = 6 声道下混（加权电平）；映射后 = 纯 ch3
        assert float(np.mean(patched[0])) == pytest.approx(DC_AMPS[3], abs=2e-2)
        assert float(np.mean(baseline[0])) != pytest.approx(DC_AMPS[3], abs=2e-2)

    def test_stem_decode_callback_selects_channel(self, tmp_path) -> None:
        """Stem 生产 decode 回调（直读路径）按映射选道。"""
        wav = _dc_wav(tmp_path / "dc6.wav")
        seq = _audio_timeline("asset-1")

        decode, close = create_clip_decode_fn(
            _project(wav, None), float(FPS), SR)
        try:
            full = decode(
                seq.audio_tracks[0], seq.audio_tracks[0].clips[0], 0,
                float(FPS), 480)
            assert full is not None and full.shape[0] == 6
        finally:
            close()

        decode, close = create_clip_decode_fn(
            _project(wav, SourcePatchSpec(audio_channel_map=((3, 0),))),
            float(FPS), SR)
        try:
            sel = decode(
                seq.audio_tracks[0], seq.audio_tracks[0].clips[0], 0,
                float(FPS), 480)
            assert sel is not None
            assert sel.shape[0] == 1
            assert float(np.mean(sel[0])) == pytest.approx(DC_AMPS[3], abs=2e-2)
        finally:
            close()


# ── 插入四门：patch 并入 has_audio 判定 ────────────────────────────────


def _av_asset(asset_id: str = "av1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path="/tmp/av.mp4", media_type="video",
        display_name="av",
        metadata={"audio_info": {"channels": 2, "channel_layout": "stereo"}},
    )


def _av_sequence() -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-gate", name="Gate", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


class TestInsertGatesHonourPatch:
    def _view(self, qapp, qtbot, spec: SourcePatchSpec | None):
        from src.features.edit.ui.timeline_view import TimelineView

        project = EditProject("p", "P", assets={"av1": _av_asset()})
        if spec is not None:
            project = project.with_source_patch_override("av1", spec)
        view = TimelineView()
        view.load_timeline(_av_sequence())
        view.set_project(project)
        return view

    def _clips(self, view) -> tuple[list, list]:
        return (
            list(view._timeline.video_tracks[0].clips),
            list(view._timeline.audio_tracks[0].clips),
        )

    def test_default_patches_linked_pair(self, qapp, qtbot) -> None:
        view = self._view(qapp, qtbot, None)
        assert view.insert_clip("av1", 0, 23) is True
        video_clips, audio_clips = self._clips(view)
        assert len(video_clips) == 1 and len(audio_clips) == 1
        assert video_clips[0].linked_clip_id == audio_clips[0].clip_id

    def test_audio_disabled_inserts_video_only(self, qapp, qtbot) -> None:
        """关音频的有声源不再强制成对落轨（核心 Source Patching 语义）。"""
        view = self._view(
            qapp, qtbot, SourcePatchSpec(audio_enabled=False))
        assert view.insert_clip("av1", 0, 23) is True
        video_clips, audio_clips = self._clips(view)
        assert len(video_clips) == 1 and audio_clips == []
        assert video_clips[0].linked_clip_id is None

    def test_video_disabled_inserts_audio_only(self, qapp, qtbot) -> None:
        view = self._view(qapp, qtbot, SourcePatchSpec(video_enabled=False))
        assert view.insert_clip("av1", 0, 23) is True
        video_clips, audio_clips = self._clips(view)
        assert video_clips == [] and len(audio_clips) == 1

    def test_both_disabled_rejected(self, qapp, qtbot) -> None:
        view = self._view(
            qapp, qtbot,
            SourcePatchSpec(video_enabled=False, audio_enabled=False))
        assert view.insert_clip("av1", 0, 23) is False
        video_clips, audio_clips = self._clips(view)
        assert video_clips == [] and audio_clips == []

    def test_overwrite_audio_disabled_single_track(self, qapp, qtbot) -> None:
        view = self._view(
            qapp, qtbot, SourcePatchSpec(audio_enabled=False))
        assert view.overwrite_clip("av1", 0, 23) is True
        video_clips, audio_clips = self._clips(view)
        assert len(video_clips) == 1 and audio_clips == []

    def test_append_video_disabled_lands_on_audio(self, qapp, qtbot) -> None:
        view = self._view(qapp, qtbot, SourcePatchSpec(video_enabled=False))
        assert view.append_clip("av1", 0, 23) is True
        video_clips, audio_clips = self._clips(view)
        assert video_clips == [] and len(audio_clips) == 1


# ── R9/F2.1：跨序列粘贴轨道重映射 ───────────────────────────────────────


def _pair_sequence(prefix: str, with_audio: bool = True) -> TimelineSequence:
    """含一对链接 V/A 片段的序列（轨道 id 带 prefix，模拟不同序列）。"""
    v_id, a_id = f"{prefix}-v", f"{prefix}-a"
    video_clip = ClipInstance(
        "c-v", "av1", v_id, 0, 24, 0, 24, linked_clip_id="c-a")
    audio_clip = ClipInstance(
        "c-a", "av1", a_id, 0, 24, 0, 24, linked_clip_id="c-v") \
        if with_audio else None
    return TimelineSequence(
        timeline_id=f"seq-{prefix}", name=prefix, width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack(v_id, "V1", "video", 0, clips=(video_clip,)),),
        audio_tracks=(
            (EditTrack(a_id, "A1", "audio", 0, clips=(audio_clip,)),)
            if with_audio else ()),
    )


class TestCrossSequencePasteRemap:
    def _select_pair(self, view, seq) -> None:
        sel = view._selection.with_clip_selected(
            "c-v", next(t.track_id for t in seq.video_tracks),
            seq.timeline_id, replace=True)
        sel = sel.with_clip_selected(
            "c-a", next(t.track_id for t in seq.audio_tracks),
            seq.timeline_id, replace=False)
        view.set_selection(sel)

    def test_cross_sequence_paste_remaps_by_position(self, qapp, qtbot) -> None:
        """不同 track_id 的序列间粘贴：按（类型,序号）落对应轨，链接保持。"""
        from src.features.edit.ui.timeline_view import TimelineView

        seq_a = _pair_sequence("a")
        seq_b = _pair_sequence("b")
        view = TimelineView()
        view.load_timeline(seq_a)
        self._select_pair(view, seq_a)
        view._copy_selected_clips()
        assert view._clipboard is not None
        entry_types = sorted(
            e["track_type"] for e in view._clipboard.clips)
        assert entry_types == ["audio", "video"]

        view.load_timeline(seq_b)
        view._paste_clips_at_frame(10)

        video_clips = list(view._timeline.video_tracks[0].clips)
        audio_clips = list(view._timeline.audio_tracks[0].clips)
        # 原有一对 + 粘贴一对；粘贴点与原片段（0–24）重叠右移到 24
        assert len(video_clips) == 2 and len(audio_clips) == 2
        pasted_v = next(c for c in video_clips if c.clip_id != "c-v")
        pasted_a = next(c for c in audio_clips if c.clip_id != "c-a")
        assert pasted_v.timeline_start == 24 and pasted_a.timeline_start == 24
        assert pasted_v.linked_clip_id == pasted_a.clip_id
        assert pasted_a.linked_clip_id == pasted_v.clip_id

    def test_missing_track_type_skips_only_that_clip(self, qapp, qtbot) -> None:
        """目标序列无音频轨：只跳过音频条，视频条照常粘贴（非整批丢失）。"""
        from src.features.edit.ui.timeline_view import TimelineView

        seq_a = _pair_sequence("a")
        seq_b = _pair_sequence("b", with_audio=False)
        view = TimelineView()
        view.load_timeline(seq_a)
        self._select_pair(view, seq_a)
        view._copy_selected_clips()
        view.load_timeline(seq_b)
        view._paste_clips_at_frame(5)

        video_clips = list(view._timeline.video_tracks[0].clips)
        assert len(video_clips) == 2  # 原 1 + 粘贴 1（右移避让原片段）
        assert not hasattr(view._timeline, "audio_tracks") or \
            not view._timeline.audio_tracks
