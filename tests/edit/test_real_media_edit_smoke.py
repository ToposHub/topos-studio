"""P0-08：真实素材端到端 smoke（headless）。

用 tests/test_videos 的真实视频矩阵驱动本轮 P0 修复的核心链路：

1. 真实探测（h264/aac）→ 媒体轨/时间线
2. 命令链（P0-04 跨轨移动到已有轨道 + 撤销/重做 + ripple）跑在真实素材上
3. 真实音频解码 → 共享 AudioGraphRenderer（P0-01 离线路径）混音
4. 真实解码 PCM → BWF take 落盘（P0-06 bext）→ 读回验证

不依赖 Qt 窗口/GPU（P0-08 完整分进程 GUI soak 另行建设）。
素材缺失时 skip。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = [pytest.mark.edit_core, pytest.mark.real_media, pytest.mark.smoke]

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    CommandStack,
    MoveClipToTrackCommand,
    SplitClipCommand,
    RippleDeleteCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

VIDEO = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"

pytestmark += [pytest.mark.skipif(not VIDEO.exists(), reason=f"real media missing: {VIDEO}")]


def _probe(path: Path):
    import av
    container = av.open(str(path))
    video = next((s for s in container.streams if s.type == "video"), None)
    audio = next((s for s in container.streams if s.type == "audio"), None)
    info = {
        "fps": float(video.average_rate) if video else 24.0,
        "duration": float(video.duration * video.time_base) if video and video.duration else 0.0,
        "width": video.codec_context.width if video else 0,
        "height": video.codec_context.height if video else 0,
        "has_audio": audio is not None,
        "sample_rate": int(audio.codec_context.sample_rate) if audio else 48000,
        "channels": int(audio.codec_context.channels) if audio else 2,
    }
    container.close()
    return info


def _decode_audio_block(path: Path, sample_rate: int, n_samples: int) -> np.ndarray:
    """解码真实音轨的前 n_samples 样本，返回 (channels, n)。"""
    import av
    container = av.open(str(path))
    collected: list[np.ndarray] = []
    got = 0
    resampler = None
    for frame in container.decode(audio=0):
        arr = frame.to_ndarray()  # (channels, n) for planar formats
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if resampler is None:
            from src.features.edit.core.channel_layout import ChannelLayout
            layout = ChannelLayout.from_channel_count(arr.shape[0]).av_layout_name
            resampler = av.AudioResampler(
                format="fltp", layout=layout, rate=sample_rate)
        out = resampler.resample(frame)
        for of in (out if isinstance(out, list) else [out]):
            oa = of.to_ndarray()
            if oa.ndim == 1:
                oa = oa.reshape(1, -1)
            collected.append(oa.astype(np.float32))
            got += oa.shape[-1]
        if got >= n_samples:
            break
    container.close()
    pcm = np.concatenate(collected, axis=1)[:, :n_samples]
    return np.ascontiguousarray(pcm)


class TestRealMediaEditSmoke:
    def test_probe(self):
        info = _probe(VIDEO)
        assert info["width"] == 2048 or info["height"] >= 1000  # 2K 级
        assert info["duration"] > 10
        assert info["has_audio"]

    def test_command_chain_on_real_timeline(self):
        """P0-04：真实素材时间线上的完整命令链（含跨轨移动/撤销/重做）。"""
        info = _probe(VIDEO)
        fps = round(info["fps"])
        clip_frames = min(int(info["duration"] * 0.8) * fps, 24 * fps)

        def mk(clip_id, track_id, start):
            return ClipInstance(
                clip_id=clip_id, asset_id="asset_real", track_id=track_id,
                timeline_start=start, timeline_duration=clip_frames,
                source_start=0, source_duration=clip_frames,
                speed=SpeedParams(speed=1.0),
            )

        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(mk("c1", "v1", 0),))
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                       clips=(mk("c2", "v2", 0),))
        a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0,
                       clips=(mk("a1c", "a1", 0),))
        seq = TimelineSequence(timeline_id="real", name="Real",
                               video_tracks=(v1, v2), audio_tracks=(a1,))

        stack = CommandStack()

        # Split at 1/3
        cmd_split = SplitClipCommand.create("v1", "c1", clip_frames // 3)
        seq = stack.execute(cmd_split, seq)
        assert seq.get_track_by_id("v1").clips[0].timeline_duration == clip_frames // 3

        # P0-04: 跨轨移动到已有轨道（真实时长规模），落点避开 v2 上的 c2
        landing = clip_frames + 24  # c2 占 [0, clip_frames)
        cmd_move = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1", clip_id="c1",
            target_track_id="v2", new_start=landing, old_start=0,
        )
        seq = stack.execute(cmd_move, seq)
        moved = seq.get_track_by_id("v2").get_clip_by_id("c1")
        assert moved is not None and moved.timeline_start == landing

        # Undo → 回到 v1 原位
        seq = stack.undo(seq)
        assert seq.get_track_by_id("v1").get_clip_by_id("c1") is not None
        # Redo → 又到 v2
        seq = stack.redo(seq)
        assert seq.get_track_by_id("v2").get_clip_by_id("c1") is not None

        # Ripple delete 音频轨片段 + undo（真实时长规模下后段平移正确）
        import uuid as _uuid
        cmd_ripple = RippleDeleteCommand(
            command_id=str(_uuid.uuid4()), label="Ripple Delete",
            track_id="a1", clip_id="a1c",
        )
        seq = stack.execute(cmd_ripple, seq)
        assert len(seq.get_track_by_id("a1").clips) == 0
        seq = stack.undo(seq)
        assert len(seq.get_track_by_id("a1").clips) == 1

    def test_shared_renderer_with_real_audio(self):
        """P0-01：真实解码 PCM 经共享 AudioGraphRenderer 混音非静音。"""
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer

        info = _probe(VIDEO)
        sr = 48000
        n = sr // 4  # 0.25s
        pcm = _decode_audio_block(VIDEO, sr, n)
        assert pcm.shape[-1] == n
        peak = float(np.max(np.abs(pcm)))
        assert peak > 1e-4, "真实音轨解码结果不应为静音"

        track = EditTrack(track_id="t1", name="A1", track_type="audio", index=0,
                          volume=0.9)
        seq = TimelineSequence(timeline_id="r", name="R", audio_tracks=(track,))
        renderer = AudioGraphRenderer(float(sr))
        renderer.configure_from_sequence(seq)
        processed = renderer.render_track_block("t1", pcm, 0)
        assert processed is not None
        out = renderer.apply_master(processed)
        assert out.shape == pcm.shape
        assert float(np.max(np.abs(out))) > 1e-4

    def test_bwf_take_from_real_audio(self, tmp_path):
        """P0-06/P0-07：真实解码 PCM → BWF take 落盘 → bext 读回。"""
        from src.features.edit.core.bwf import read_bext_chunk
        from src.features.edit.core.recording_manager import (
            TakeInfo, write_take_wav,
        )

        sr = 48000
        pcm = _decode_audio_block(VIDEO, sr, sr)  # 1s
        take = TakeInfo(scene_name="RealScene", talent_name="RealTalent", take_number=1)
        take.time_reference_samples = sr * 120
        path = str(tmp_path / "real_take.wav")
        assert write_take_wav(path, pcm, sr, 24, take)

        bext = read_bext_chunk(path)
        assert bext is not None
        assert bext.time_reference_samples == sr * 120
        assert "RealScene" in bext.description

        # PyAV 可完整解码回等长 PCM
        import av
        c = av.open(path)
        total = sum(f.samples for f in c.decode(audio=0))
        c.close()
        assert total == pcm.shape[1]
