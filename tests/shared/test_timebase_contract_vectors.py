"""DESK-05A/08A · 跨端时间精度向量合同测试。

三层锁定（见 docs/adr/0005 §5）：
1. 入库向量文件与生成器再产物逐字节一致（文件新鲜度）；
2. 向量期望值由本测试内**另一套独立 oracle** 重算复核（生成器不失真）；
3. 桌面生产实现逐点对拍全部向量（map_frame / TimelineSampleClock /
   split_sample_partition / subframe_decompose / offset_to_reference_rate）。

移动端（Dart）实现方应实现同一向量集的等价 runner——全部向量绿即
时间语义与桌面对齐。此文件是双端共同的验收入口，不要在本测试里
放宽任何向量（发现生产实现与冻结规则不一致 → 修生产实现）。
"""

from __future__ import annotations

from fractions import Fraction

import pytest

pytestmark = pytest.mark.edit_core

from src.shared.contracts import timebase as tb
from src.features.edit.core.sample_time import (
    TimelineSampleClock,
    split_sample_partition,
    subframe_decompose,
    compose_timeline_seconds,
    offset_to_reference_rate,
)
from src.features.edit.core.timeline_frame_mapper import (
    FrameMapRequest,
    map_frame,
)


# ------------------------------------------------------------------
# 独立复核 oracle（不复用 tb 生成器内部函数：第三道独立计算）
# ------------------------------------------------------------------

def _recheck_cfr(vector: dict) -> int:
    defaults = tb.parse_frac  # noqa: F841 — 可读性
    merged = dict(tb.build_vectors()["cfr_defaults"])
    merged.update({k: v for k, v in vector.items() if k in merged})
    tl = Fraction(*map(int, merged["timeline_fps"].split("/")))
    src = Fraction(*map(int, merged["source_fps"].split("/")))
    speed = Fraction(*map(int, merged["speed"].split("/")))
    phase = Fraction(*map(int, merged["source_frame_phase"].split("/")))
    local = vector["timeline_frame"] - merged["clip_timeline_start"]
    t = Fraction(local) / tl
    src_frac = t * speed * src + phase
    if merged["reverse"]:
        total = Fraction(
            merged["source_start_frame"] + merged["source_duration_frames"] - 1
        ) - src_frac
    else:
        total = src_frac + merged["source_start_frame"]
    rounded = (total + Fraction(1, 2)).__floor__() if total >= 0 else \
        -((-total + Fraction(1, 2)).__floor__())
    return max(
        merged["source_start_frame"],
        min(rounded,
            merged["source_start_frame"] + merged["source_duration_frames"] - 1),
    )


class TestVectorFileFreshness:
    def test_committed_file_matches_generator(self) -> None:
        """入库 JSON 必须是生成器当前产物（防手改漂移）。"""
        committed = tb.vectors_path().read_text(encoding="utf-8")
        regenerated = tb.build_vectors()
        import json

        assert json.loads(committed) == regenerated


class TestIndependentOracleRecheck:
    """生成器 oracle 与本文件 oracle 是两套独立实现，逐点互拍。"""

    def test_cfr_vectors_match_local_oracle(self) -> None:
        vectors = tb.load_vectors_file()["cfr"]
        assert len(vectors) >= 80
        for vector in vectors:
            assert _recheck_cfr(vector) == vector["expected_source_frame"], \
                f"vector {vector['id']} 期望值与独立 oracle 不一致"

    def test_split_pairs_are_continuous(self) -> None:
        """分割父子向量逐帧一致（跨帧率 + 有理相位）。"""
        vectors = {v["id"]: v for v in tb.load_vectors_file()["cfr"]}
        for k in range(7, 13):
            parent = vectors[f"split_parent_k{k}"]
            child = vectors[f"split_child_k{k}"]
            assert parent["expected_source_frame"] == \
                child["expected_source_frame"]


class TestProductionCfrAgainstVectors:
    @pytest.mark.parametrize("vector", tb.load_vectors_file()["cfr"])
    def test_map_frame(self, vector: dict) -> None:
        merged = dict(tb.load_vectors_file()["cfr_defaults"])
        merged.update({k: v for k, v in vector.items() if k in merged})
        src_num, src_den = map(int, merged["source_fps"].split("/"))
        tl_num, tl_den = map(int, merged["timeline_fps"].split("/"))
        request = FrameMapRequest(
            timeline_frame=vector["timeline_frame"],
            timeline_fps_num=tl_num,
            timeline_fps_den=tl_den,
            clip_timeline_start=merged["clip_timeline_start"],
            clip_timeline_duration=merged["clip_timeline_duration"],
            source_start_frame=merged["source_start_frame"],
            source_duration_frames=merged["source_duration_frames"],
            source_fps_num=src_num,
            source_fps_den=src_den,
            speed=Fraction(*map(int, merged["speed"].split("/"))),
            reverse=merged["reverse"],
            source_frame_phase=Fraction(
                *map(int, merged["source_frame_phase"].split("/"))),
        )
        result = map_frame(request)
        assert result is not None, vector["id"]
        assert result.source_frame == vector["expected_source_frame"], (
            f"{vector['id']}: 生产 map_frame={result.source_frame}，"
            f"向量期望={vector['expected_source_frame']}"
        )


class TestProductionVfrAgainstVectors:
    @pytest.mark.parametrize("vector", tb.load_vectors_file()["vfr"])
    def test_map_vfr_frame(self, vector: dict) -> None:
        tl_num, tl_den = map(int, vector["timeline_fps"].split("/"))
        pts = tuple(
            float(Fraction(*map(int, s.split("/"))))
            for s in vector["source_pts_seconds"]
        )
        request = FrameMapRequest(
            timeline_frame=vector["timeline_frame"],
            timeline_fps_num=tl_num,
            timeline_fps_den=tl_den,
            clip_timeline_start=vector["clip_timeline_start"],
            clip_timeline_duration=vector["clip_timeline_duration"],
            source_start_frame=0,
            source_duration_frames=len(pts),
            source_pts_index=pts,
            speed=Fraction(*map(int, vector["speed"].split("/"))),
        )
        result = map_frame(request)
        assert result is not None, vector["id"]
        assert result.source_frame == vector["expected_source_frame"], (
            f"{vector['id']}: 生产 VFR={result.source_frame}，"
            f"向量期望={vector['expected_source_frame']}"
        )

    def test_vfr_tie_resolves_earlier(self) -> None:
        """距离平局归较早条目（向量集中显式钉住）。"""
        vector = next(
            v for v in tb.load_vectors_file()["vfr"]
            if v["id"] == "vfr_tie_9_16_to_half")
        # target=9/16 距 1/2 与 5/8 各 1/16，中间无条目 → 较早者 idx 4
        assert vector["expected_source_frame"] == 4


class TestProductionAudioAgainstVectors:
    audio = tb.load_vectors_file()["audio"]

    @pytest.mark.parametrize("vector", audio["frame_start_samples"])
    def test_frame_start_sample(self, vector: dict) -> None:
        clock = TimelineSampleClock(
            Fraction(*map(int, vector["fps"].split("/"))),
            vector["sample_rate"])
        expected = Fraction(
            *map(int, vector["expected_start_sample"].split("/")))
        assert clock.frame_start_sample(vector["frame"]) == expected

    @pytest.mark.parametrize("vector", audio["block_bounds"])
    def test_block_bounds(self, vector: dict) -> None:
        clock = TimelineSampleClock(
            Fraction(*map(int, vector["fps"].split("/"))),
            vector["sample_rate"])
        bounds = clock.block_bounds(vector["start_frame"],
                                    vector["frame_count"])
        assert bounds == vector["expected_bounds"]
        # 契约：相邻边界差之和精确闭合（无孔无缝）
        assert bounds[-1] == sum(
            bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1))

    @pytest.mark.parametrize("vector", audio["split_partitions"])
    def test_split_partition(self, vector: dict) -> None:
        clock = TimelineSampleClock(
            Fraction(*map(int, vector["fps"].split("/"))),
            vector["sample_rate"])
        left, right = split_sample_partition(
            clock,
            vector["clip_timeline_start_frame"],
            vector["clip_timeline_duration_frames"],
            vector["split_timeline_frame"],
            source_sample_offset=vector["source_sample_offset"])
        assert list(left) == vector["expected_left"], vector["id"]
        assert list(right) == vector["expected_right"], vector["id"]
        # 契约：左右消费窗无缝无重叠（总样本守恒）
        assert left[0] + left[1] == right[0]
        assert left[1] + right[1] <= vector["clip_timeline_duration_frames"] * 48000

    @pytest.mark.parametrize("vector", audio["subframe_roundtrips"])
    def test_subframe_roundtrip(self, vector: dict) -> None:
        clock = TimelineSampleClock(
            Fraction(*map(int, vector["fps"].split("/"))),
            vector["sample_rate"])
        seconds = Fraction(*map(int, vector["seconds"].split("/")))
        frame, offset = subframe_decompose(clock, seconds)
        assert frame == vector["expected_frame"]
        assert offset == Fraction(
            *map(int, vector["expected_offset_samples"].split("/")))
        assert compose_timeline_seconds(clock, frame, offset) == seconds

    @pytest.mark.parametrize("vector", audio["rate_conversions"])
    def test_rate_conversion(self, vector: dict) -> None:
        offset = Fraction(*map(int, vector["offset_samples"].split("/")))
        whole, residual = offset_to_reference_rate(
            offset, vector["declared_rate"])
        assert whole == vector["expected_whole_samples"]
        assert residual == Fraction(
            *map(int, vector["expected_residual"].split("/")))
        assert 0 <= residual < 1


class TestVectorSetIdentity:
    def test_vector_set_and_digest_stable(self) -> None:
        """向量集摘要：双端核对同一份向量的锚点。"""
        vectors = tb.load_vectors_file()
        assert vectors["vector_set"] == tb.VECTOR_SET_ID
        # 摘要确定性：同一数据两次计算一致
        assert tb.vectors_digest(vectors) == tb.vectors_digest(
            tb.load_vectors_file())
