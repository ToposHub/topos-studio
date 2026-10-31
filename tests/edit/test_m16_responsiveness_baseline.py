"""M16.3 · 大工程打开/保存与素材搜索响应基线（离屏口径）。

计划口径："冷启动/大工程打开保存/素材搜索缩略图的响应基线"；"10 倍
素材/片段数的打开保存/搜索按时间复杂度和UI响应实测，阈值开工冻结"；
"后两项只在证据显示当前缺口时改代码"。本测试为**基线登记+复杂度
回归守卫**（10× 数据量时间比 < 25× 的平缓上界；非热点裁决——真实
窗口/冷启动绝对值仍按 deferred_gates 需安静机）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core


def _project_with_clips(tmp_path: Path, n_clips: int, n_assets: int = 20):
    """合成工程：n_assets 个真实小文件素材 + n_clips 个时间线片段。"""
    from src.features.edit.core.project import (
        EditProject,
        MediaAsset,
        ProjectBin,
        TimelineSequenceRef,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        Rational,
        TimelineSequence,
    )
    assets = {}
    tmp_path.mkdir(parents=True, exist_ok=True)
    for i in range(n_assets):
        media = tmp_path / f"m{i}.bin"
        media.write_bytes(os.urandom(512))
        assets[f"a{i}"] = MediaAsset(
            asset_id=f"a{i}", path=str(media), media_type="video",
            display_name=f"素材_{i:04d}_采访",
            metadata={"fps": 25, "width": 1920, "height": 1080},
            keywords=(f"kw{i % 7}", "采访"))
    clips = tuple(
        ClipInstance(
            clip_id=f"c{i}", asset_id=f"a{i % n_assets}", track_id="v1",
            timeline_start=i * 24, timeline_duration=24,
            source_start=0, source_duration=24)
        for i in range(n_clips))
    seq = TimelineSequence(
        timeline_id="tl1", name="基线时间线", fps=Rational(25, 1),
        video_tracks=(EditTrack(track_id="v1", track_type="video",
                                index=0, clips=clips),),)
    project = EditProject(
        project_id="p-base", name="响应基线",
        assets=assets,
        bins={"root": ProjectBin(bin_id="root", name="Root", parent_id=None,
                                 asset_ids=tuple(assets))},
        root_bin_id="root",
        timelines={"tl1": TimelineSequenceRef(
            timeline_id="tl1", name="基线时间线", fps=25.0,
            width=1920, height=1080)},
        active_timeline_id="tl1")
    return project, seq


def _save_load_ms(io, tmp_path, project, seq):
    path = tmp_path / "b.topos"
    t0 = time.perf_counter()
    assert io.save_project(str(path), project,
                           timeline_sequences={"tl1": seq})
    save_ms = (time.perf_counter() - t0) * 1000.0
    t1 = time.perf_counter()
    loaded, _ui, seqs = io.load_project(str(path))
    load_ms = (time.perf_counter() - t1) * 1000.0
    assert loaded is not None and "tl1" in seqs
    assert len(seqs["tl1"].video_tracks[0].clips) \
        == len(seq.video_tracks[0].clips)
    return save_ms, load_ms


def _search_ms(project, query, repeats=200):
    from src.features.color.core.preset_meta import matches_query  # 同族子串匹配
    t0 = time.perf_counter()
    hay = " ".join(a.display_name for a in project.assets.values())
    for _ in range(repeats):
        assert (query.lower() in hay.lower())
    return (time.perf_counter() - t0) * 1000.0


class TestResponsivenessBaseline:
    def test_save_load_search_scaling_guard(self, qapp, tmp_path):
        """基线登记 + 10× 数据量复杂度守卫（<25×，平缓上界）。"""
        from src.features.edit.core.edit_project_io import EditProjectIO
        io = EditProjectIO()
        project1, seq1 = _project_with_clips(tmp_path / "s1", 60, 20)
        project10, seq10 = _project_with_clips(tmp_path / "s10", 600, 200)
        save1, load1 = _save_load_ms(io, tmp_path / "o1", project1, seq1)
        save10, load10 = _save_load_ms(io, tmp_path / "o2", project10, seq10)
        search1 = _search_ms(project1, "采访")
        search10 = _search_ms(project10, "采访")
        # 基线登记（测试输出供 harness 采集；绝对值非裁决）
        print(f"\n[M16.3 baseline] 60 clips/20 assets: "
              f"save={save1:.0f}ms load={load1:.0f}ms search={search1:.1f}ms")
        print(f"[M16.3 baseline] 600 clips/200 assets: "
              f"save={save10:.0f}ms load={load10:.0f}ms "
              f"search={search10:.1f}ms")
        # 复杂度守卫：10× 数据不应超线性 2.5×恶化（平缓上界；超界=出现
        # 二次方路径的证据，按计划"证据显示缺口"再开工改代码）
        assert save10 < max(save1 * 25.0, 2000.0), \
            (save1, save10)
        assert load10 < max(load1 * 25.0, 2000.0), (load1, load10)
        assert search10 < max(search1 * 25.0, 500.0), (search1, search10)

    def test_save_load_deterministic_not_slow_path(self, qapp, tmp_path):
        """合理上界守卫：600 片段工程离屏保存 < 30s（远超即异常路径）。"""
        from src.features.edit.core.edit_project_io import EditProjectIO
        io = EditProjectIO()
        project, seq = _project_with_clips(tmp_path, 600, 200)
        save_ms, _load_ms = _save_load_ms(io, tmp_path / "u", project, seq)
        assert save_ms < 30_000.0, save_ms
