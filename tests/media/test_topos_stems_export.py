"""M-B8 导出侧：stems 角色轨多母带渲染 + 容器追加音轨（ADR-C054 §4）。

验收（plan §3 M-B8 任务 4 + e2e）：
- 编码器：`add_audio_track_stream` / `write_audio_track_samples` 全链路——
  主混音轨 0（v1.1 reserved 槽）+ 逐角色追加轨（s16 单声道 Dialogue +
  f32 立体声 Music）→ 容器 3 音轨逐位 roundtrip + 轨名（©nam/hdlr →
  ffprobe handler_name）；
- 导出器：双角色（对白+音乐）时间线 → `_render_audio_master(
  only_track_ids=…)` 按角色分组渲染独立母带（同解码/混音/Bus/Master
  链）→ **master == Σ(stems) 对拍** + 逐轨写入容器逐位一致；
- fail-fast：stems 先于主混音声明、空轨名、越界写入、首帧后声明；
- 降级：不支持多轨的编码器 → 单轨 + 警告（all-or-nothing）；空角色组
  声明前剔除（容器 v1.6"声明即须有样本"）；
- 单轨路径回归：不声明 stems 时容器仅 1 音轨（既有 M-A6 行为）。

库未构建时整体 skip（构建方式见 native/topos_codec/README.md）。
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TC_AUDIO_CODEC_LPCM,
    TC_AUDIO_CODEC_MP4A,
    TC_AUDIO_FMT_FLOAT32,
    TC_AUDIO_FMT_INT,
    TC_AUDIO_LAYOUT_MONO,
    TC_AUDIO_LAYOUT_STEREO,
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    ToposCodecError,
)

_W, _H = 64, 48
_SR = 48000
_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


@pytest.fixture(scope="module")
def codec() -> ToposCodec:
    import os

    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is None and not any(
        p.is_file() for p in topos_binding.candidate_library_paths()
    ):
        pytest.skip("libtopos_codec 未构建；先运行 "
                    "bash native/topos_codec/scripts/build_release_repo.sh")
    status, lib_path, lib = topos_binding.first_resolved_library(explicit)
    if status == topos_binding.LIB_RESOLUTION_NOT_FOUND:
        pytest.skip("libtopos_codec 未构建；先运行 "
                    "bash native/topos_codec/scripts/build_release_repo.sh")
    if lib is None:
        pytest.fail(f"libtopos_codec 不可加载（{status}）：{lib_path}")
    for sym in ("tc_mux_add_audio_track", "tc_movie_audio_track_count"):
        if not hasattr(lib, sym):
            pytest.fail(
                f"libtopos_codec 缺少多音轨符号 {sym}——请重建：bash "
                f"native/topos_codec/scripts/build_release_repo.sh")
    return ToposCodec(lib)


class _Rate:
    """Rational 替身（encoder 侧以 num/den getattr 读取）。"""

    def __init__(self, num: int, den: int) -> None:
        self.num, self.den = num, den


class _Cfg:
    width, height = _W, _H
    profile = 'standard'
    crf = 44
    entropy_mode = 'rans2'
    has_alpha = False
    alpha_premultiplied = False
    pix_fmt = 'yuv422p10le'

    def __init__(self, fps: "_Rate") -> None:
        self.fps = fps


def _synth_frame_planes(n: int) -> list:
    """确定性 10bit 4:2:2 平面（encoder 直喂路径）。"""
    cw = (_W + 1) // 2
    rng = np.random.default_rng(11 + n)
    y = ((np.indices((_H, _W))[1] * 5 % 900 + 60).astype(np.uint16)
         + rng.integers(0, 8, (_H, _W)).astype(np.uint16))
    u = np.full((_H, cw), 512, np.uint16)
    v = np.full((_H, cw), 512, np.uint16)
    return [np.ascontiguousarray(y), u, v]


def _stem_tone(freq: float, amp: float, n: int, channels: int = 2,
               phase: float = 0.0) -> np.ndarray:
    """确定性正弦母带 (channels, n) float32（对拍基准）。"""
    t = np.arange(n, dtype=np.float64) / _SR
    sig = (amp * np.sin(2.0 * np.pi * freq * t + phase)).astype(np.float32)
    return np.stack([sig] * channels)


def _s16_be_bytes(pcm: np.ndarray) -> bytes:
    """(ch, n) float32 → 容器 'twos' 大端 s16 交错字节（编码器同口径）。"""
    x = np.clip(pcm.T, -1.0, 1.0)
    q = np.rint(x * 32767.0).astype('<i2')
    return q.view('<u2').astype('>u2').tobytes()


def _f32_be_bytes(pcm: np.ndarray) -> bytes:
    """(ch, n) float32 → 容器大端 f32 交错字节（编码器同口径）。"""
    return pcm.T.astype(np.float32).astype('>f4').tobytes()


def _encode_stems_movie(codec: ToposCodec, path: Path,
                        master: np.ndarray, dialogue: np.ndarray,
                        music: np.ndarray, n_frames: int = 2) -> None:
    """主混音轨 0 + Dialogue(s16 mono) + Music(f32 stereo) 三轨容器。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.frame import DecodedFrame

    enc = ToposVideoEncoder(str(path), _Cfg(_Rate(25, 1)))
    enc.open()
    enc.add_audio_stream(codec="pcm_f32le", sample_rate=_SR, channels=2)
    idx_dialogue = enc.add_audio_track_stream(
        "Dialogue", codec="pcm_s16le", sample_rate=_SR, channels=1)
    idx_music = enc.add_audio_track_stream(
        "Music", codec="pcm_f32le", sample_rate=_SR, channels=2)
    assert (idx_dialogue, idx_music) == (1, 2)
    for k in range(n_frames):
        enc.encode_frame(DecodedFrame(
            frame_index=k, pts=k, time_seconds=k / 25.0,
            data=None, planes=_synth_frame_planes(k),
            pixel_format='yuv422p10le', width=_W, height=_H))
    enc.write_audio_samples(master, _SR)
    enc.write_audio_track_samples(idx_dialogue, dialogue, _SR)
    enc.write_audio_track_samples(idx_music, music, _SR)
    enc.close()


def test_encoder_stems_container_roundtrip(codec, tmp_path) -> None:
    """三轨容器 roundtrip：逐轨字节级对拍 + 轨信息（格式/声道/轨名）。"""
    master = _stem_tone(330.0, 0.25, 4096)
    dialogue = _stem_tone(440.0, 0.20, 4096, channels=1)
    music = _stem_tone(220.0, 0.10, 4096)

    path = tmp_path / "stems.mov"
    _encode_stems_movie(codec, path, master, dialogue, music)

    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        assert mv.audio_track_count() == 3

        t0 = mv.audio_info_at(0)
        assert (t0.codec, t0.channel_count, t0.sample_format) == \
            (TC_AUDIO_CODEC_LPCM, 2, TC_AUDIO_FMT_FLOAT32)
        assert t0.name == ""                       # 轨 0 无轨名（兼容视图）
        assert t0.sample_count == master.shape[1]

        t1 = mv.audio_info_at(1)
        assert (t1.codec, t1.channel_count, t1.channel_layout) == \
            (TC_AUDIO_CODEC_LPCM, 1, TC_AUDIO_LAYOUT_MONO)
        assert t1.name == "Dialogue"
        assert t1.sample_count == dialogue.shape[1]

        t2 = mv.audio_info_at(2)
        assert (t2.codec, t2.channel_count, t2.channel_layout) == \
            (TC_AUDIO_CODEC_LPCM, 2, TC_AUDIO_LAYOUT_STEREO)
        assert t2.name == "Music"

        # 逐位对拍（f32 直通零量化；s16 与编码器同口径量化）
        assert mv.read_audio_samples_at(0) == _f32_be_bytes(master)
        assert mv.read_audio_samples_at(1) == _s16_be_bytes(dialogue)
        assert mv.read_audio_samples_at(2) == _f32_be_bytes(music)

        # 旧单轨 API = 轨 0 视图（v1.1 兼容语义）
        assert mv.audio_info().name == ""


@pytest.mark.skipif(_FFPROBE is None, reason="ffprobe 不可用")
def test_ffprobe_stems_tracks_and_names(codec, tmp_path) -> None:
    """ffmpeg oracle：3 音轨/各声道数/轨名（hdlr → handler_name tag）。"""
    path = tmp_path / "stems_probe.mov"
    _encode_stems_movie(codec, path,
                        _stem_tone(330.0, 0.25, 2048),
                        _stem_tone(440.0, 0.20, 2048, channels=1),
                        _stem_tone(220.0, 0.10, 2048))
    out = subprocess.run(
        [_FFPROBE, "-v", "error", "-show_entries",
         "stream=index,codec_type,channels,channel_layout:stream_tags="
         "handler_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    import json
    streams = json.loads(out.stdout)["streams"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    assert len(audio) == 3
    assert sorted(s["channels"] for s in audio) == [1, 2, 2]
    names = {s.get("tags", {}).get("handler_name") for s in audio}
    assert {"Dialogue", "Music"} <= names


def test_encoder_stems_fail_fast(codec, tmp_path) -> None:
    """fail-fast：stems 先于主混音声明 / 空轨名 / 越界写入 / 首帧后声明。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder

    enc = ToposVideoEncoder(str(tmp_path / "ff.mov"), _Cfg(_Rate(25, 1)))
    enc.open()
    # 追加轨先于主混音轨声明 → 拒绝（轨 0 = 兼容视图必须先在）
    with pytest.raises(RuntimeError, match="add_audio_stream"):
        enc.add_audio_track_stream("Dialogue")
    enc.add_audio_stream(codec="pcm_f32le", sample_rate=_SR, channels=2)
    # 空轨名 → 拒绝（©nam 承载角色标签）
    with pytest.raises(RuntimeError, match="轨名"):
        enc.add_audio_track_stream("   ")
    idx = enc.add_audio_track_stream("Dialogue", codec="pcm_s16le",
                                     sample_rate=_SR, channels=1)
    # 越界写入
    with pytest.raises(RuntimeError, match="越界"):
        enc.write_audio_track_samples(idx + 1,
                                      _stem_tone(1.0, 0.1, 64), _SR)
    with pytest.raises(RuntimeError, match="越界"):
        enc.write_audio_track_samples(0, _stem_tone(1.0, 0.1, 64), _SR)
    # 声道数与声明不符 fail-fast
    with pytest.raises(RuntimeError, match="声道数"):
        enc.write_audio_track_samples(idx, _stem_tone(1.0, 0.1, 64, channels=2),
                                      _SR)
    enc.encode_frame(_mk_frame(0))
    # 首帧后声明 → 拒绝
    with pytest.raises(RuntimeError, match="首帧"):
        enc.add_audio_track_stream("Music")
    enc.abort()


def _mk_frame(k: int):
    from src.shared.media.frame import DecodedFrame

    return DecodedFrame(
        frame_index=k, pts=k, time_seconds=k / 25.0,
        data=None, planes=_synth_frame_planes(k % 4),
        pixel_format='yuv422p10le', width=_W, height=_H)


def test_encoder_single_track_regression(codec, tmp_path) -> None:
    """单轨路径回归：不声明 stems → 容器仅 1 音轨（M-A6 行为不变）。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder

    enc = ToposVideoEncoder(str(tmp_path / "single.mov"), _Cfg(_Rate(25, 1)))
    enc.open()
    enc.add_audio_stream(codec="pcm_f32le", sample_rate=_SR, channels=2)
    for k in range(2):
        enc.encode_frame(_mk_frame(k))
    enc.write_audio_samples(_stem_tone(330.0, 0.25, 1024), _SR)
    enc.close()

    with topos_binding.ToposMovieFile(codec, str(tmp_path / "single.mov")) as mv:
        assert mv.audio_track_count() == 1
        assert mv.audio_info_at(0).sample_count == 1024
        assert mv.read_audio_samples_at(0) == \
            _f32_be_bytes(_stem_tone(330.0, 0.25, 1024))


# ── 导出器级：双角色分组母带渲染 + 逐轨对拍 ─────────────────────────────

_SR_F = 48000.0


class _FakeAsset:
    """项目素材替身（export_source_path 只读 path）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.is_nested_sequence = False


class _FakeProject:
    def __init__(self, assets: Dict[str, _FakeAsset]) -> None:
        self.assets = assets


class _FakeDecoder:
    """AudioDecoder 替身：按素材路径给确定性音色（dialogue 440/music 220）。"""

    source_layout = None

    def __init__(self) -> None:
        self._amp = 0.0

    def open(self, path, timeline_fps=None, output_sample_rate=None) -> bool:
        p = str(path)
        if "dialogue" in p:
            self._amp = 0.2
        elif "music" in p:
            self._amp = 0.1
        else:
            self._amp = 0.0
        return True

    def decode_samples_for_frame(self, source_frame, fps, n):
        idx = (np.arange(n, dtype=np.float64)
               + float(source_frame) * 37.0)
        sig = (self._amp * ((idx % 97.0) / 97.0 - 0.5)).astype(np.float32)
        return np.stack([sig, sig])

    def close(self) -> None:
        pass


@pytest.fixture()
def fake_audio_decoder(monkeypatch):
    import src.features.edit.core.audio_playback as audio_playback

    monkeypatch.setattr(audio_playback, "AudioDecoder", _FakeDecoder)
    return _FakeDecoder


def _make_two_role_sequence():
    """双角色时间线：t_dialogue(dialogue) + t_music(music)，各 1 clip
    24 帧 @24fps（= 48000 样本）。"""
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )

    def _clip(track_id: str) -> ClipInstance:
        return ClipInstance(
            clip_id=f"{track_id}_clip",
            asset_id=f"{track_id}_asset",
            track_id=track_id,
            source_start=0,
            source_duration=24,
            timeline_start=0,
            timeline_duration=24,
        )

    t_dialogue = EditTrack(
        track_id="t_dialogue", name="Dialogue", track_type="audio",
        index=0, clips=(_clip("t_dialogue"),), stem_group="dialogue")
    t_music = EditTrack(
        track_id="t_music", name="Music", track_type="audio",
        index=1, clips=(_clip("t_music"),), stem_group="music")
    return TimelineSequence(
        audio_tracks=(t_dialogue, t_music), fps=24.0)


def _be_f32_frames(raw: bytes, channels: int) -> np.ndarray:
    """容器大端 f32 字节 → (channels, n) float32。"""
    arr = np.frombuffer(raw, dtype='>f4').reshape(-1, channels)
    return np.ascontiguousarray(arr.T, dtype=np.float32)


def test_exporter_dual_role_masters_match_container_tracks(
        codec, tmp_path, fake_audio_decoder) -> None:
    """e2e 对拍：双角色时间线 → 分组母带（only_track_ids）→ 容器逐轨
    逐位一致 + master == Σ(stems)（容差 1e-5）。"""
    from src.features.edit.core.timeline_export import (
        AudioStemExportTrack,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.shared.export.topos_encoder import ToposVideoEncoder

    project = _FakeProject({
        "t_dialogue_asset": _FakeAsset("/fake/dialogue.wav"),
        "t_music_asset": _FakeAsset("/fake/music.wav"),
    })
    sequence = _make_two_role_sequence()
    exporter = TimelineExporter(project, sequence, frame_provider=None)

    cfg = TimelineExportConfig(
        codec="topos", include_audio=True, timeline_id="tl",
        audio_codec="pcm_f32le", audio_sample_rate=_SR, audio_channels=2,
        audio_stems=(
            AudioStemExportTrack(stem_group="dialogue", name="Dialogue",
                                 codec="pcm_f32le", channels=2),
            AudioStemExportTrack(stem_group="music", name="Music",
                                 codec="pcm_f32le", channels=2),
        ),
        output_path=str(tmp_path / "dual.mov"),
    )
    assert cfg.validate() == []

    path = tmp_path / "dual.mov"
    enc = ToposVideoEncoder(str(path), _Cfg(_Rate(24, 1)))
    enc.open()
    enc.add_audio_stream(codec="pcm_f32le", sample_rate=_SR, channels=2)

    # 声明（exporter 域）→ 容器追加轨 1/2
    stems = exporter._declare_audio_stems(cfg, enc)
    assert [(s.stem_group, i) for s, i in stems] == [("dialogue", 1),
                                                     ("music", 2)]

    # 分组母带渲染（fake 解码器 = dialogue/music 独立音色）
    master = exporter._render_audio_master(0, 24, cfg, encoder=enc)
    assert master is not None and master.shape == (2, 48000)
    exporter._audio_stem_tracks = stems
    pcms = exporter._render_stem_masters(0, 24, cfg, encoder=enc)
    assert len(pcms) == 2 and all(p is not None for p in pcms)
    stem_dialogue, stem_music = pcms

    # 角色分离成立：两组母带不同、各自只含本组音色（幅度 0.2 / 0.1）
    assert not np.array_equal(stem_dialogue, stem_music)
    assert float(np.max(np.abs(stem_dialogue))) == pytest.approx(0.1, abs=0.01)
    assert float(np.max(np.abs(stem_music))) == pytest.approx(0.05, abs=0.01)
    # 对拍：master == Σ(stems)（同链混音，浮点和容差）
    assert np.allclose(master, stem_dialogue + stem_music, atol=1e-5)

    # 写入容器（轨 0 主混音 + 追加轨）→ close 定稿
    for k in range(2):
        enc.encode_frame(_mk_frame(k))
    enc.write_audio_samples(master, _SR)
    exporter._audio_stem_pcms = pcms
    exporter._write_stem_tracks(cfg, enc)
    enc.close()

    # 容器逐轨逐位对拍（f32 直通零量化）
    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        assert mv.audio_track_count() == 3
        assert mv.read_audio_samples_at(0) == _f32_be_bytes(master)
        assert mv.read_audio_samples_at(1) == _f32_be_bytes(stem_dialogue)
        assert mv.read_audio_samples_at(2) == _f32_be_bytes(stem_music)
        assert mv.audio_info_at(1).name == "Dialogue"
        assert mv.audio_info_at(2).name == "Music"


def test_exporter_stems_muted_group_not_declared(
        codec, tmp_path, fake_audio_decoder) -> None:
    """回归：角色轨全部静音时，该组声明前剔除（与渲染闸门同口径），
    导出成功定稿——而非"已声明但渲染为空"令 finish fail-fast 使
    整个导出（含视频）失败。"""
    from src.features.edit.core.timeline_export import (
        AudioStemExportTrack,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.shared.export.topos_encoder import ToposVideoEncoder

    project = _FakeProject({
        "t_dialogue_asset": _FakeAsset("/fake/dialogue.wav"),
        "t_music_asset": _FakeAsset("/fake/music.wav"),
    })
    import dataclasses

    sequence = _make_two_role_sequence()
    # 对白轨静音（常见操作：只交付音乐 stems；EditTrack 为 frozen dataclass）
    muted_dialogue = dataclasses.replace(sequence.audio_tracks[0],
                                         is_muted=True)
    sequence = dataclasses.replace(
        sequence, audio_tracks=(muted_dialogue, sequence.audio_tracks[1]))

    exporter = TimelineExporter(project, sequence, frame_provider=None)
    cfg = TimelineExportConfig(
        codec="topos", include_audio=True, timeline_id="tl",
        audio_codec="pcm_f32le", audio_sample_rate=_SR, audio_channels=2,
        audio_stems=(
            AudioStemExportTrack(stem_group="dialogue", name="Dialogue",
                                 codec="pcm_f32le", channels=2),
            AudioStemExportTrack(stem_group="music", name="Music",
                                 codec="pcm_f32le", channels=2),
        ),
        output_path=str(tmp_path / "muted.mov"),
    )
    assert cfg.validate() == []

    path = tmp_path / "muted.mov"
    enc = ToposVideoEncoder(str(path), _Cfg(_Rate(24, 1)))
    enc.open()
    enc.add_audio_stream(codec="pcm_f32le", sample_rate=_SR, channels=2)

    # 静音组不再声明（旧行为：声明 dialogue → 渲染 None → close 必败）
    stems = exporter._declare_audio_stems(cfg, enc)
    assert [(s.stem_group, i) for s, i in stems] == [("music", 1)]

    master = exporter._render_audio_master(0, 24, cfg, encoder=enc)
    assert master is not None
    exporter._audio_stem_tracks = stems
    pcms = exporter._render_stem_masters(0, 24, cfg, encoder=enc)
    assert len(pcms) == 1 and pcms[0] is not None

    for k in range(2):
        enc.encode_frame(_mk_frame(k))
    enc.write_audio_samples(master, _SR)
    exporter._audio_stem_pcms = pcms
    exporter._write_stem_tracks(cfg, enc)
    enc.close()  # 旧代码在此抛"已声明但未注入任何音频样本"

    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        assert mv.audio_track_count() == 2   # 主混音 + Music（无 Dialogue）
        assert mv.audio_info_at(1).name == "Music"


def test_exporter_stems_empty_group_skipped_and_degrade(
        codec, tmp_path, fake_audio_decoder) -> None:
    """空角色组声明前剔除（容器"声明即须有样本"）；不支持多轨的编码器
    整体降级单轨。"""
    from src.features.edit.core.timeline_export import (
        AudioStemExportTrack,
        TimelineExportConfig,
        TimelineExporter,
    )

    project = _FakeProject({
        "t_dialogue_asset": _FakeAsset("/fake/dialogue.wav"),
        "t_music_asset": _FakeAsset("/fake/music.wav"),
    })
    exporter = TimelineExporter(project, _make_two_role_sequence(),
                                frame_provider=None)
    cfg = TimelineExportConfig(
        include_audio=True, audio_codec="pcm_f32le",
        audio_sample_rate=_SR, audio_channels=2,
        audio_stems=(
            AudioStemExportTrack(stem_group="dialogue", name="Dialogue"),
            AudioStemExportTrack(stem_group="effects", name="Effects"),
        ),
        output_path=str(tmp_path / "x.mov"),
    )
    # effects 组不存在 → 声明剔剩 dialogue
    stems = exporter._declare_audio_stems(cfg)
    assert stems == []          # 编码器未建（无 add_audio_track_stream）→ 降级

    # 支持 stems 的编码器：只声明非空组
    from src.shared.export.topos_encoder import ToposVideoEncoder
    enc = ToposVideoEncoder(str(tmp_path / "y.mov"), _Cfg(_Rate(24, 1)))
    enc.open()
    enc.add_audio_stream(codec="pcm_f32le", sample_rate=_SR, channels=2)
    stems = exporter._declare_audio_stems(cfg, enc)
    assert [(s.stem_group, i) for s, i in stems] == [("dialogue", 1)]
    enc.abort()


def test_export_config_stem_validation() -> None:
    """配置层 fail-fast：空组名/重复组/非法声道数。"""
    from src.features.edit.core.timeline_export import (
        AudioStemExportTrack,
        TimelineExportConfig,
    )

    base = dict(codec="topos", output_path="/tmp/x.mov", timeline_id="tl")
    ok = TimelineExportConfig(
        **base, audio_stems=(AudioStemExportTrack(stem_group="dialogue"),))
    assert ok.validate() == []
    errs = TimelineExportConfig(
        **base, audio_stems=(
            AudioStemExportTrack(stem_group=""),
            AudioStemExportTrack(stem_group="music"),
            AudioStemExportTrack(stem_group="music"),
            AudioStemExportTrack(stem_group="fx", channels=3),
        )).validate()
    assert any("stem_group 不能为空" in e for e in errs)
    assert any("重复" in e for e in errs)
    assert any("声道数" in e for e in errs)


def test_deliver_job_stems_roundtrip() -> None:
    """交付作业快照：StemTrackConfig 序列化 + → 导出域声明转换。"""
    from src.features.deliver.core.video_export_job import (
        AudioConfig,
        StemTrackConfig,
    )

    cfg = AudioConfig(stems=(
        StemTrackConfig(stem_group="dialogue", name="Dialogue",
                        codec="pcm_s16le", channels=1),
        StemTrackConfig(stem_group="music", name="Music"),
    ))
    data = cfg.to_dict()
    assert data["stems"][0]["stem_group"] == "dialogue"
    back = AudioConfig.from_dict(data)
    assert back.stems == cfg.stems

    # 旧 schema（无 stems 字段）回读 = 空（兼容）
    legacy = AudioConfig.from_dict(
        {k: v for k, v in data.items() if k != "stems"})
    assert legacy.stems == ()

    # → 导出域 AudioStemExportTrack（to_timeline_export_config 内部映射）
    from src.features.deliver.core.video_export_job import (
        VideoExportJob,
        VideoConfig,
        NamingConfig,
        RenderMode,
        FilenameUse,
        SourceKind,
        RangeType,
    )
    job = VideoExportJob(
        timeline_id="tl",
        source_kind=SourceKind.TIMELINE,
        range_type=RangeType.ENTIRE,
        output_path="/tmp/x.mov",
        video_config=VideoConfig(),
        audio_config=cfg,
        naming_config=NamingConfig(
            render_mode=RenderMode.SINGLE_CLIP,
            filename_use=FilenameUse.TIMELINE),
    )
    tc = job.to_timeline_export_config()
    assert [(s.stem_group, s.codec, s.channels) for s in tc.audio_stems] == \
        [("dialogue", "pcm_s16le", 1), ("music", "pcm_s24le", 2)]
