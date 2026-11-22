"""Production audio routing, PCM and mux with a known-position pulse."""

import wave
from dataclasses import replace

import av
import numpy as np

from src.features.composite.core.composition_pcm import prepare_audio_drop, render_composition_pcm
from src.features.composite.core.graph_preflight import MODE_EXPORT, run_preflight_data
from src.features.composite.core.layer_model import BOTTOM_UP_LAYER_STACK_VERSION, Layer, LayerStack
from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.core.media_drop_payload import MEDIA_TYPE_AUDIO, MediaDropPayload
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.render.composite_render_output import CompositeRenderOutput
from src.features.edit.core.audio_playback import AudioDecoder
from src.features.edit.core.comp_clip_pcm import CompClipPCMSource
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_export import (
    TimelineExportConfig, TimelineExporter, expand_sequence_audio_for_render,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def test_audio_layer_roundtrip_range_and_video_mux(tmp_path, monkeypatch):
    sound = tmp_path / "pulse.wav"
    signal = np.zeros(8000, dtype="<i2")
    signal[2200] = 20000
    with wave.open(str(sound), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(signal.tobytes())

    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    graph.duration_frames = 4
    ctl = LayerStackController(graph, layer_stack_version=BOTTOM_UP_LAYER_STACK_VERSION)
    ctl.add_media_payload(prepare_audio_drop(MediaDropPayload("audio-id", str(sound), MEDIA_TYPE_AUDIO)))
    saved = NodeGraph.from_dict(graph.to_dict()).to_dict()
    assert not saved["connections"]
    assert saved["layer_stacks"]["main"]["generated_merge_node_ids"] == []
    assert run_preflight_data(saved, mode=MODE_EXPORT).ok
    assert ctl.active_layer_ids_for_output() == list(saved["layers"])
    samples = render_composition_pcm(saved, 1, 2)
    assert samples.state == "ok"
    assert samples.samples.shape == (2, 4000)
    assert np.argmax(samples.samples[0]) == 200

    render = CompositeRenderOutput()
    monkeypatch.setattr(render, "_render_one_frame", lambda **_kwargs: np.zeros((32, 32, 3), np.float32))
    movie = tmp_path / "pulse.mov"
    result = render.render_video(saved, str(movie), 32, 32, 24, 1, 2, parallel_frames=1)
    assert result.success, result.error
    assert result.audio_state == "ok"
    with av.open(str(movie)) as exported:
        assert len(exported.streams.video) == len(exported.streams.audio) == 1
        decoded = np.concatenate([frame.to_ndarray().reshape(-1, 2)
                                  for frame in exported.decode(audio=0)])
        assert decoded.shape == (4000, 2)
        assert np.argmax(decoded[:, 0]) == 200

    complete_movie = movie.read_bytes()
    progress = []
    cancelled = render.render_video(
        saved, str(movie), 32, 32, 24, 1, 2, parallel_frames=1,
        progress_cb=lambda written, total: progress.append((written, total)),
        is_cancelled=lambda: bool(progress),
    )
    assert cancelled.error == "EXPORT_CANCELLED"
    assert movie.read_bytes() == complete_movie
    assert not list(tmp_path.glob(".topos-render-*"))

    parent, _, asset_id, graph_id = EditProject.create("Audio").create_empty_comp_clip(
        name="Sound comp", fps=24, duration_frames=4)
    parent = replace(parent, compositions={graph_id: saved})
    clip = ClipInstance(asset_id=asset_id, timeline_start=10, timeline_duration=2,
                        source_start=1, source_duration=2, track_id="picture")
    seq = TimelineSequence(video_tracks=(EditTrack(track_id="picture", clips=(clip,)),))
    expanded = expand_sequence_audio_for_render(seq, None, parent)
    assert len(expanded.audio_tracks) == 1
    assert expanded.audio_tracks[0].clips[0].source_start == 1
    explicitly_owned = replace(seq, audio_tracks=expanded.audio_tracks)
    assert expand_sequence_audio_for_render(explicitly_owned, None, parent) is explicitly_owned
    decoder = AudioDecoder()
    decoder.open_pcm(CompClipPCMSource(saved, parent, 48000), 24.0, source_start=1)
    assert np.argmax(decoder.decode_samples_for_frame(0, 24.0, 2000)[0]) == 200
    decoder.close()
    delivered = TimelineExporter(parent, seq, None)._render_audio_master(
        10, 12, TimelineExportConfig(), encoder=object())
    np.testing.assert_allclose(delivered, samples.samples, atol=1e-6)

    from src.features.edit.core.nested_sequence import create_nested_sequence_asset

    nested = create_nested_sequence_asset(seq)
    parent = parent.with_media_asset_added(nested)
    outer_clip = replace(clip, asset_id=nested.asset_id, timeline_start=20, source_start=10)
    outer = TimelineSequence(video_tracks=(EditTrack(clips=(outer_clip,)),))
    outer_mix = TimelineExporter(parent, outer, None, sequence_resolver={seq.timeline_id: seq}.get)
    nested_pcm = outer_mix._render_audio_master(20, 22, TimelineExportConfig(), encoder=object())
    np.testing.assert_allclose(nested_pcm, samples.samples, atol=1e-6)


def test_audio_export_error_preserves_existing_destination(tmp_path):
    movie = tmp_path / "existing.mov"
    movie.write_bytes(b"existing content")
    graph = {
        "layers": {"voice": {"audio": {"asset_id": "offline"},
                             "source_node_id": "source"}},
        "nodes": [{"id": "source", "parameters": {"file_path": str(tmp_path / "offline.wav")}}],
    }
    result = CompositeRenderOutput().render_video(graph, str(movie), 32, 32, 24, 0, 1)
    assert result.audio_state == "missing_media"
    assert not result.success
    assert movie.read_bytes() == b"existing content"


def test_layer_extensions_remain_after_legacy_stack_resave():
    layer = Layer.from_dict({"layer_id": "a", "root_node_id": "source",
                             "vendor_extension": {"mode": ["future"]}})
    stack = LayerStack.from_dict({"stack_id": "main", "layer_ids": ["a"], "extra": [42]})
    layer.name = "Edited"
    assert layer.to_dict()["vendor_extension"] == {"mode": ["future"]}
    assert stack.to_dict()["extra"] == [42]
