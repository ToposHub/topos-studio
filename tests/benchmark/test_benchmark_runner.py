"""P0-08: benchmark runner 统计与协议单元测试（不跑真实解码）。"""

import pytest

from tests.benchmark.benchmark_runner import (
    ProtocolConfig,
    SCENARIO_MEDIA,
    _percentile,
    _stats,
    resolve_scenario_media,
)


class TestStats:
    def test_empty(self):
        assert _stats([])["n"] == 0

    def test_percentiles(self):
        values = [float(i) for i in range(1, 101)]  # 1..100
        s = _stats(values)
        assert s["n"] == 100
        assert s["p50_ms"] == 50.5
        assert s["p95_ms"] == pytest.approx(95.05)
        assert s["max_ms"] == 100.0
        assert s["mean_ms"] == pytest.approx(50.5)

    def test_single_value(self):
        s = _stats([5.0])
        assert s["p50_ms"] == 5.0
        assert s["p95_ms"] == 5.0
        assert s["max_ms"] == 5.0


class TestProtocolConfig:
    def test_defaults_match_plan(self):
        cfg = ProtocolConfig()
        assert cfg.warmup_frames > 0
        assert cfg.steady_frames >= 100
        assert cfg.seek_samples >= 30
        assert cfg.longrun_frames > cfg.steady_frames

    def test_quick_smaller(self):
        full = ProtocolConfig()
        quick = ProtocolConfig.quick()
        assert (quick.steady_frames < full.steady_frames
                and quick.longrun_frames < full.longrun_frames)


class TestScenarioResolution:
    def test_known_scenarios_resolve(self):
        files = resolve_scenario_media(["B-01", "B-02", "B-03", "B-04"])
        assert len(files) >= 4
        # 去重
        assert len({str(f) for f in files}) == len(files)

    def test_all_scenario_media_under_test_videos(self):
        for sid, rels in SCENARIO_MEDIA.items():
            for rel in rels:
                assert rel.startswith("tests/test_videos/"), (sid, rel)
