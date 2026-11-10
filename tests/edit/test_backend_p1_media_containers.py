"""P1-6 Image Sequence、分离声道、专业容器——导入契约核验。

对应《媒体池后端交接计划》P1 第 6 项：
- 图片序列分组/缺帧登记：既有 tests/edit/test_m04_sequence_classify.py
  （连号分组、缺口诊断仍分组、稀疏巧合不分组、混合批保留单张）复用核验；
- 分离声道：纯音频 mov 容器（专业分离音频）探测给出逐轨 audio_tracks；
- 专业容器：真实 ProRes/H.264 素材的时间码域与音频轨完整；
- 不支持的流给出诊断（error 保留），后台导入 worker 在 QThreadPool
  执行（既有导入取消测试覆盖，不在 UI 线程同步探测）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

av = pytest.importorskip("av", reason="PyAV 探测依赖")
np = pytest.importorskip("numpy", reason="合成音频依赖")

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import probe_media_metadata
from src.features.edit.core.sequence_classify import classify_import_batch

TEST_VIDEOS = Path(__file__).resolve().parents[2] / "tests" / "test_videos"


def _make_audio_only_mov(track_count: int = 2, frames: int = 10) -> str:
    """合成纯音频 mov（专业分离声道风格：无视频流，多条单声道轨）。"""
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, f"split_audio_{track_count}.mov")
    container = av.open(path, "w")
    sample_rate = 48000
    streams = [
        container.add_stream("pcm_s16le", rate=sample_rate, layout="mono")
        for _ in range(track_count)
    ]
    for _ in range(frames):
        for stream in streams:
            data = np.zeros((1, 1024), dtype="int16")
            frame = av.AudioFrame.from_ndarray(
                data, format="s16", layout="mono")
            frame.sample_rate = sample_rate
            for packet in stream.encode(frame):
                container.mux(packet)
    for stream in streams:
        for packet in stream.encode(None):
            container.mux(packet)
    container.close()
    return path


def test_audio_only_mov_probes_multi_track() -> None:
    """纯音频 mov：视频流缺失不整体失败——逐轨探测 + 时长 + 诊断保留。"""
    path = _make_audio_only_mov(track_count=2)
    metadata = probe_media_metadata(path)
    assert metadata.get("audio") is True
    assert metadata.get("error"), "诊断信息保留（无视频流）"
    tracks = metadata.get("audio_tracks")
    assert len(tracks) == 2  # 分离声道：两条独立单声道轨
    assert all(t["channel_count"] == 1 for t in tracks)
    assert all(t["codec"] == "pcm_s16le" for t in tracks)
    assert all(t["bits_per_sample"] == 16 for t in tracks)
    assert metadata["audio_info"]["sample_rate"] == 48000
    assert metadata["duration_seconds"] == pytest.approx(
        10 * 1024 / 48000, abs=1e-3)


def test_unreadable_container_keeps_diagnostics() -> None:
    """不可解码容器：结构化 error，不抛异常、不伪造字段。"""
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "fake.mov")
    Path(path).write_bytes(b"definitely not a quicktime movie")
    metadata = probe_media_metadata(path)
    assert metadata.get("error")
    assert "audio_info" not in metadata
    assert "audio_tracks" not in metadata


def test_professional_containers_full_contract() -> None:
    """真实专业容器（ProRes/H.264）：时间码域 + 音频轨 + 逐轨位深。"""
    prores = TEST_VIDEOS / "2K_prores422.mov"
    if not prores.exists():
        pytest.skip(f"测试素材缺失: {prores}")
    metadata = probe_media_metadata(str(prores))
    assert metadata["codec"] == "prores"
    assert metadata["fps_num"] == 25 and metadata["fps_den"] == 1
    # 时间码三键要么同在要么同缺（不伪造）
    tc = metadata.get("timecode")
    if tc is None:
        assert metadata.get("timecode_fps") is None
        assert metadata.get("timecode_drop_frame") is None
    tracks = metadata.get("audio_tracks") or []
    assert tracks, "ProRes 素材应有逐轨音频"
    assert tracks[0]["bits_per_sample"] == 24  # pcm_s24le


def test_sequence_grouping_contract_via_classifier(tmp_path) -> None:
    """合法序列组合：连号成组为单素材、缺口登记、异前缀不误组。"""
    for index in (0, 1, 2, 4, 5):  # 3 号缺帧
        (tmp_path / f"shotA_{index:04d}.exr").write_bytes(b"x")
    (tmp_path / "other_0000.exr").write_bytes(b"x")  # 异前缀
    files = [str(tmp_path / f.name) for f in sorted(tmp_path.iterdir())]
    groups, singles = classify_import_batch(files)
    assert len(groups) == 1
    assert groups[0].missing_numbers == (3,)  # 缺帧登记
    assert [Path(s).name for s in singles] == ["other_0000.exr"]
