"""P0-06: 确定性基准工程构建器单元测试。"""

import pytest

from tests.benchmark.benchmark_projects import (
    BENCHMARK_MEDIA,
    PROJECT_BUILDERS,
    build_benchmark_project,
    build_media_pool_descriptor,
    describe_project,
    project_media_refs,
)


class TestBenchmarkProjects:
    @pytest.mark.parametrize("scenario", sorted(PROJECT_BUILDERS.keys()))
    def test_all_scenarios_build(self, scenario):
        seq = build_benchmark_project(scenario)
        assert seq.timeline_id
        assert seq.width > 0 and seq.height > 0
        assert seq.video_tracks

    def test_determinism(self):
        a = build_benchmark_project("E-02_dual_track_transitions")
        b = build_benchmark_project("E-02_dual_track_transitions")
        assert describe_project(a) == describe_project(b)
        clips_a = [c.clip_id for t in a.video_tracks for c in t.clips]
        clips_b = [c.clip_id for t in b.video_tracks for c in t.clips]
        assert clips_a == clips_b

    def test_e01_single_track(self):
        seq = build_benchmark_project("E-01_single_track")
        assert len(seq.video_tracks) == 1
        assert len(seq.video_tracks[0].clips) == 4

    def test_e02_has_transition(self):
        seq = build_benchmark_project("E-02_dual_track_transitions")
        assert len(seq.video_tracks) == 2
        transitions = [tr for t in seq.video_tracks for tr in t.transitions]
        assert len(transitions) == 1
        assert transitions[0].transition_type == "cross_dissolve"

    def test_e03_four_tracks_with_subtitles(self):
        seq = build_benchmark_project("E-03_four_track_titles")
        assert len(seq.video_tracks) == 4
        assert len(seq.subtitle_tracks) == 1
        assert len(seq.subtitle_tracks[0].subtitle_clips) == 4

    def test_e04_heavy_fx(self):
        seq = build_benchmark_project("E-04_heavy_fx")
        effect_counts = [len(c.effects) for t in seq.video_tracks for c in t.clips]
        assert max(effect_counts) >= 3

    def test_e05_long_project_duration(self):
        seq = build_benchmark_project("E-05_long_project_30min")
        desc = describe_project(seq)
        # 30min @25fps = 45000 帧
        assert desc["duration_frames"] == 30 * 60 * 25

    def test_media_refs_resolve_to_real_files(self):
        seq = build_benchmark_project("E-02_dual_track_transitions")
        refs = project_media_refs(seq)
        assert refs
        for rel in refs:
            assert rel.startswith("tests/test_videos/")

    def test_media_pool_descriptor(self):
        pool = build_media_pool_descriptor(size=100)
        assert pool["size"] == 100
        assert len(pool["assets"]) == 100
        ids = {a["asset_id"] for a in pool["assets"]}
        assert len(ids) == 100
        for entry in pool["assets"]:
            assert entry["media_key"] in BENCHMARK_MEDIA

    def test_unknown_scenario_raises(self):
        with pytest.raises(ValueError, match="available"):
            build_benchmark_project("E-99_nonexistent")

    def test_scenario_prefix_resolution(self):
        # "E-01" 前缀等价全名 E-01_single_track（docstring 示例用法）。
        seq = build_benchmark_project("E-01")
        assert seq.fps == 25

    def test_ambiguous_scenario_prefix_raises(self):
        with pytest.raises(ValueError, match="ambiguous"):
            build_benchmark_project("E-0")
