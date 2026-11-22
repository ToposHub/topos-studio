"""One-hot speakers through the normal live, movie and Stem paths."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
import wave

import numpy as np
import pytest

from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.audio_playback import AudioDecoder, AudioMixer, AudioTrackState
from src.features.edit.core.channel_layout import ChannelLayout
from src.features.edit.core.commands import SetMixLayoutCommand
from src.features.edit.core.delivery_readback import measure_delivery_wav
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.stem_export import (
    StemSpec, StemType, create_clip_decode_fn, render_stem_pcm, write_stem_wav,
)
from src.features.edit.core.timeline_export import TimelineExportConfig, TimelineExporter
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, EffectChainParams, Send, TimelineSequence, TrackEffectInstance,
)


@pytest.mark.parametrize("name,channels", [("mono", 1), ("5.1", 6), ("7.1", 8)])
def test_native_bed_live_movie_and_wav_readback(tmp_path: Path, name: str, channels: int) -> None:
    pcm = np.zeros((channels, 4000), np.float32)
    pcm[np.arange(channels), np.arange(channels) * 37 + 13] = 0.25
    source = tmp_path / "bed.wav"
    assert write_stem_wav(str(source), pcm, sample_rate=48000, bit_depth=24)
    clip = ClipInstance("c", "bed", "bed", 0, 2, 0, 2)
    sequence = TimelineSequence(
        master_audio_layout=name, master_volume=0.5,
        audio_tracks=(EditTrack("bed", "Bed", "audio", 0, clips=(clip,), audio_layout=name,
                                volume=0.5, sends=(Send(target_bus_id="fx", level=0.5),)),
                      EditTrack("fx", "Aux", "audio", 1, is_bus=True,
                                bus_layout=name, volume=0.5)),
    )
    project = EditProject("p", "P", assets={
        "bed": MediaAsset("bed", str(source), "audio", "Bed", {}),
    })
    expected = pcm * 0.3125

    decoder = AudioDecoder()
    assert decoder.open(source, 24.0)
    mixer = AudioMixer()
    mixer.add_track("bed", decoder, AudioTrackState("bed", volume=0.5))
    mixer.register_bus_track("fx", AudioTrackState("fx", volume=0.5))
    mixer.set_track_routing("bed", None, [("fx", 0.5)], is_bus=False)
    mixer.set_track_routing("fx", None, [], is_bus=True)
    mixer.set_bus_layouts({"fx": name})
    mixer.set_programme_layouts({"bed": name}, name)
    mixer.set_master_volume(0.5)
    try:
        first, _ = mixer.mix_samples(0, 24.0, 2000, mixer.programme_channels(2))
        second, _ = mixer.mix_samples(1, 24.0, 2000, mixer.programme_channels(2))
    finally:
        decoder.close()
    np.testing.assert_allclose(np.concatenate((first, second), axis=1), expected, atol=2e-6)
    movie = TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 2, TimelineExportConfig(output_path=str(tmp_path / "bed.mov"),
                                   audio_channel_layout=name), encoder=Mock())
    np.testing.assert_allclose(movie, expected, atol=2e-6)

    decode, close = create_clip_decode_fn(project, 24.0, 48000)
    try:
        stem = render_stem_pcm(sequence, StemSpec(StemType.MASTER, "Master"), 48000, channels,
                               decode_clip_fn=decode)
    finally:
        close()
    np.testing.assert_allclose(stem, expected, atol=2e-6)
    delivery = tmp_path / "master.wav"
    assert write_stem_wav(str(delivery), stem, sample_rate=48000, bit_depth=24)
    result = measure_delivery_wav(delivery)
    assert result.status == "complete", result.error
    assert result.channel_order == ChannelLayout.from_channel_count(channels).channel_labels
    assert result.sample_count == expected.shape[1]
    with wave.open(str(delivery), "rb") as reader:
        packed = np.frombuffer(reader.readframes(4000), dtype=np.uint8).reshape(4000, channels, 3)
        audible = np.any(packed != 0, axis=2)
        assert np.array_equal(audible.T, expected != 0)


def test_layout_apply_preserves_identity_and_rejects_stereo_fx() -> None:
    track = EditTrack("a", "A", "audio", 0)
    sequence = TimelineSequence(audio_tracks=(track,))
    master, _ = SetMixLayoutCommand.create(None, "7.1").apply(sequence)
    native, applied = SetMixLayoutCommand.create("a", "7.1").apply(master)
    reopened = TimelineSequence.from_dict(native.to_dict())
    assert reopened.with_master_volume(0.25).master_audio_layout == "7.1"
    assert reopened.audio_tracks[0].with_pan(-0.5).audio_layout == "7.1"
    assert build_audio_graph_snapshot(reopened).stable_hash != build_audio_graph_snapshot(master).stable_hash
    assert applied.undo(reopened).audio_tracks[0].audio_layout == ""
    stereo_fx = EffectChainParams(effects=(TrackEffectInstance("imager", {}),))
    stereo = replace(sequence, audio_tracks=(replace(track, effect_chain=stereo_fx),))
    with pytest.raises(ValueError, match="imager cannot process 7.1"):
        SetMixLayoutCommand.create("a", "7.1").apply(stereo)


def test_planar_aac_71_decodes_and_resamples_without_ghost_plane(tmp_path: Path) -> None:
    import subprocess
    import sys
    import av

    encoded = tmp_path / "bed.m4a"
    with av.open(str(encoded), "w") as container:
        stream = container.add_stream("aac", rate=48000)
        stream.layout = "7.1"
        pcm = np.zeros((4096, 8), np.float32)
        pcm[:, 6] = 0.25 * np.sin(np.arange(4096) * (2 * np.pi * 1500 / 48000))
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="flt", layout="7.1")
        frame.sample_rate = 48000
        for packet in (*stream.encode(frame), *stream.encode(None)):
            container.mux(packet)
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from pathlib import Path
import numpy as np
from src.features.edit.core.audio_playback import AudioDecoder
decoder = AudioDecoder()
assert decoder.open(Path(sys.argv[1]), 24, output_sample_rate=44100)
try:
    pcm = decoder.decode_samples_for_frame(0, 24, 1800)
    assert pcm.shape[0] == 8 and pcm.shape[1] > 1700, pcm.shape
    assert np.max(np.abs(pcm[6])) > 0.1
    assert np.max(np.abs(np.delete(pcm, 6, 0))) < 0.01
finally:
    decoder.close()
""", str(encoded)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_side_surround_source_keeps_side_speakers_in_71(tmp_path: Path) -> None:
    import av

    path = tmp_path / "side.wav"
    source = np.zeros((6, 2000), np.float32)
    source[4, 37] = 0.25
    source[5, 59] = 0.125
    with av.open(str(path), "w") as container:
        stream = container.add_stream("pcm_s24le", rate=48000)
        stream.layout = "5.1(side)"
        frame = av.AudioFrame.from_ndarray(source, format="fltp", layout="5.1(side)")
        frame.sample_rate = 48000
        for packet in (*stream.encode(frame), *stream.encode(None)):
            container.mux(packet)
    clip = ClipInstance("c", "asset", "bed", 0, 1, 0, 1)
    track = EditTrack("bed", "Bed", "audio", 0, clips=(clip,), audio_layout="7.1")
    sequence = TimelineSequence(master_audio_layout="7.1", audio_tracks=(track,))
    project = EditProject("p", "P", assets={
        "asset": MediaAsset("asset", str(path), "audio", "Side", {}),
    })
    expected = np.zeros((8, 2000), np.float32)
    expected[6:] = source[4:]
    movie = TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 1, TimelineExportConfig(output_path=str(tmp_path / "movie.mov"),
                                   audio_channel_layout="7.1"), encoder=Mock())
    np.testing.assert_allclose(movie, expected, atol=2e-6)
    decode, close = create_clip_decode_fn(project, 24.0, 48000, True)
    try:
        # A pre-master track print must retain the track's side channels
        # even if the programme Master is configured for mono.
        stem = render_stem_pcm(
            replace(sequence, master_audio_layout="mono"),
            StemSpec(StemType.TRACK, "Bed", track_ids=("bed",), include_master=False),
            48000, 8, decode_clip_fn=decode)
    finally:
        close()
    np.testing.assert_allclose(stem, expected, atol=2e-6)


def test_mono_inline_gain_targets_source_before_surround_routing(tmp_path: Path) -> None:
    from src.features.edit.core.audio_edit import make_step, new_audio_edit

    source = tmp_path / "dx.wav"
    pcm = np.zeros((1, 2000), np.float32)
    pcm[0, 800] = 0.25
    assert write_stem_wav(str(source), pcm)
    edit = new_audio_edit([make_step("range_gain", 0, 1600, (0,), gain_db=-6.0)])
    clip = ClipInstance("c", "dx", "dx", 0, 1, 0, 1, audio_edit=edit)
    track = EditTrack("dx", "Dialogue", "audio", 0, clips=(clip,), audio_layout="5.1")
    sequence = TimelineSequence(master_audio_layout="5.1", audio_tracks=(track,))
    project = EditProject("p", "P", assets={
        "dx": MediaAsset("dx", str(source), "audio", "Dialogue", {}),
    })
    expected = np.zeros((6, 2000), np.float32)
    expected[2, 800] = 0.25 * 10 ** (-6 / 20)
    movie = TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 1, TimelineExportConfig(output_path=str(tmp_path / "movie.mov"),
                                   audio_channel_layout="5.1"), encoder=Mock())
    np.testing.assert_allclose(movie, expected, atol=2e-6)
    decoder = AudioDecoder()
    assert decoder.open(source, 24.0)
    decoder._audio_edit = edit
    mixer = AudioMixer()
    mixer.set_programme_layouts({"dx": "5.1"}, "5.1")
    mixer.add_track("dx", decoder, AudioTrackState("dx"))
    try:
        live, levels = mixer.mix_samples(0, 24.0, 2000, 6)
    finally:
        decoder.close()
    np.testing.assert_allclose(live, expected, atol=2e-6)
    assert levels["-1"].speaker_peaks[2] == pytest.approx(expected[2, 800])
