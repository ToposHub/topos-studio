"""M19.4 · PQ/HLG 参考向量冻结回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

from src.shared.color.hdr_reference_vectors import (
    HLG_REFERENCE_VECTORS,
    PQ_PEAK_NITS,
    PQ_REFERENCE_VECTORS,
    PQ_REFERENCE_WHITE_NITS,
    hlg_oetf,
    hlg_oetf_inverse,
    pq_decode,
    pq_encode,
)


class TestPQVectors:
    def test_roundtrip_accuracy(self):
        for nits in (0.01, 1.0, 100.0, 203.0, 1000.0, 4000.0, 10000.0):
            assert pq_decode(pq_encode(nits)) == pytest.approx(
                nits, rel=1e-6), f"PQ roundtrip failed at {nits} nits"

    def test_frozen_anchors(self):
        # 峰值 = 码值 1.0 / 1023
        assert PQ_REFERENCE_VECTORS[10000.0] == (1.0, 1023)
        # 参考白 203nit 的 10bit code（业界通用 ~587）在冻结表内一致
        code10 = PQ_REFERENCE_VECTORS[PQ_REFERENCE_WHITE_NITS][1]
        assert 580 <= code10 <= 595
        # 单调性
        codes = [v[1] for v in PQ_REFERENCE_VECTORS.values()]
        assert codes == sorted(codes)

    def test_monotone_curve(self):
        prev = -1.0
        for nits in range(0, 10001, 500):
            c = pq_encode(float(nits))
            assert c >= prev
            prev = c


class TestHLGVectors:
    def test_roundtrip(self):
        for signal in (0.1, 0.3, 0.5, 0.75, 0.9):
            assert hlg_oetf(hlg_oetf_inverse(signal)) == pytest.approx(
                signal, abs=1e-6), f"HLG roundtrip failed at {signal}"

    def test_knee_point(self):
        # 0.5 信号是 HLG 拐点（两段公式交界）
        assert HLG_REFERENCE_VECTORS[0.5][1] == 512
        assert hlg_oetf(1.0 / 12.0) == pytest.approx(0.5, abs=1e-6)

    def test_anchors_monotone(self):
        linings = [v[0] for v in HLG_REFERENCE_VECTORS.values()]
        assert linings == sorted(linings)
