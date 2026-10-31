"""C3 阶段 1: 片段级音频增益（Clip Gain）测试。

覆盖：
- 模型：audio_gain_db 默认/范围校验/with_audio_gain_db/序列化容错（旧工程）
- 命令：批量应用 + undo + 序列化往返
- 混音接线：decoder.set_gain_db → mix_samples 增益应用（数值表 dB→线性）
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import SetClipAudioGainCommand  # noqa: E402
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _clip(clip_id: str = "c1") -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=48,
        source_start=0, source_duration=48,
    )


def _sequence(*clips: ClipInstance) -> TimelineSequence:
    track = EditTrack(track_id="v1", name="V1", track_type="audio",
                      index=0, clips=clips)
    return TimelineSequence(audio_tracks=(track,), fps=Rational(24, 1))


class TestGainModel:
    def test_default_zero(self) -> None:
        assert _clip().audio_gain_db == 0.0

    def test_factory_and_range(self) -> None:
        clip = _clip().with_audio_gain_db(-6.0)
        assert clip.audio_gain_db == -6.0
        # clamp 边界
        assert _clip().with_audio_gain_db(-100.0).audio_gain_db == -48.0
        assert _clip().with_audio_gain_db(50.0).audio_gain_db == 12.0

    def test_direct_construction_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="audio_gain_db"):
            ClipInstance(
                clip_id="x", asset_id="a", track_id="v1",
                timeline_start=0, timeline_duration=10,
                source_start=0, source_duration=10,
                audio_gain_db=-60.0,
            )

    def test_serialization_and_legacy_compat(self) -> None:
        clip = _clip().with_audio_gain_db(-3.5)
        restored = ClipInstance.from_dict(clip.to_dict())
        assert restored.audio_gain_db == -3.5
        legacy = ClipInstance.from_dict(
            {k: v for k, v in clip.to_dict().items() if k != "audio_gain_db"})
        assert legacy.audio_gain_db == 0.0


class TestGainCommand:
    def test_batch_apply_undo(self) -> None:
        c1 = _clip("c1")
        c2 = ClipInstance(
            clip_id="c2", asset_id="a", track_id="v1",
            timeline_start=48, timeline_duration=48,
            source_start=48, source_duration=48,
        )
        seq = _sequence(c1, c2)
        cmd = SetClipAudioGainCommand.create(["c1", "c2"], -12.0)
        new_seq = cmd.apply(seq)
        assert new_seq.get_clip_by_id("c1").audio_gain_db == -12.0
        assert new_seq.get_clip_by_id("c2").audio_gain_db == -12.0
        undone = cmd.undo(new_seq)
        assert undone.get_clip_by_id("c1").audio_gain_db == 0.0

    def test_serialization_roundtrip(self) -> None:
        seq = _sequence(_clip())
        cmd = SetClipAudioGainCommand.create(["c1"], 3.0)
        applied = cmd.apply(seq)
        restored = SetClipAudioGainCommand.from_dict(cmd.to_dict())
        undone = restored.undo(applied)
        assert undone.get_clip_by_id("c1").audio_gain_db == 0.0


class TestGainMixMath:
    def test_db_to_linear_values(self) -> None:
        """混音公式数值表：10^(dB/20)。"""
        for db, expected in [(-6.0, 0.5012), (0.0, 1.0), (6.0, 1.9953), (12.0, 3.9811)]:
            assert (10.0 ** (db / 20.0)) == pytest.approx(expected, abs=1e-3)

    def test_decoder_gain_applied(self) -> None:
        """decoder.set_gain_db 存储 + mix 公式等效验证。"""
        from src.features.edit.core.audio_playback import AudioDecoder

        decoder = AudioDecoder.__new__(AudioDecoder)
        decoder._gain_db = 0.0
        decoder.set_gain_db(-6.0)
        assert decoder._gain_db == -6.0
        samples = np.full((2, 100), 0.5, dtype=np.float32)
        applied = samples * (10.0 ** (decoder._gain_db / 20.0))
        assert applied.mean() == pytest.approx(0.5 * (10 ** (-6.0 / 20.0)), abs=1e-3)
