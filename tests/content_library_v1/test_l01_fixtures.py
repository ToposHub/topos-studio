"""L01 夹具测试：再生确定性（hash 对拍 manifest）与数值抽查。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from content_staging.library_v1.fixtures import generate_fixtures as gf

_FIXTURES = Path(gf.__file__).resolve().parent


def _manifest_entries() -> dict:
    data = json.loads((_FIXTURES / "fixture_manifest.json").read_text(encoding="utf-8"))
    return {e["name"]: e for e in data["fixtures"]}


def test_fixture_set_complete_and_registered() -> None:
    entries = _manifest_entries()
    assert set(entries) == set(gf._BUILDERS)
    assert set(gf.check()) == set()  # 生成器/manifest/磁盘三方一致


def test_all_fixtures_regenerate_bit_exact() -> None:
    for name, builder in gf._BUILDERS.items():
        arr = builder()
        assert arr.dtype == np.float32
        assert gf.sha256_of(arr) == _manifest_entries()[name]["sha256"]


def test_hdr_probe_values_exact() -> None:
    probes = gf.hdr_probes()
    assert probes.shape == (9, 3)
    # §9.1 数值覆盖：-0.25/0/0.18/1/4.5/12 中性灰 + 高饱和/HDR 色
    #（夹具为 float32，期望值同以 float32 表达做精确比较）
    np.testing.assert_array_equal(probes[0], np.float32([-0.25] * 3))
    np.testing.assert_array_equal(probes[2], np.float32([0.18] * 3))
    np.testing.assert_array_equal(probes[5], np.float32([12.0] * 3))
    np.testing.assert_array_equal(probes[6], np.float32([1.0, 0.0, 0.0]))
    np.testing.assert_array_equal(probes[8], np.float32([12.0, 0.1, 4.5]))


def test_gray_ramp_endpoints_and_row_invariance() -> None:
    ramp = gf.gray_ramp_linear()
    assert ramp.shape == (32, 64, 3)
    assert ramp[0, 0, 0] == 0.0 and ramp[0, -1, 0] == 1.0
    np.testing.assert_array_equal(ramp[0], ramp[31])


def test_uv_grid_encoding_and_corners() -> None:
    uv = gf.uv_grid()
    assert uv.shape == (64, 64, 3)
    assert uv[32, 0, 0] == 0.0 and abs(float(uv[32, 63, 0]) - 1.0) < 1e-6
    # 四角 4×4 白色角标
    np.testing.assert_array_equal(uv[0:4, 0:4], 1.0)
    np.testing.assert_array_equal(uv[60:64, 60:64], 1.0)
    # 网格线落在 B 通道
    assert float(uv[0, 8, 2]) == 1.0


def test_alpha_fringe_straight_semantics() -> None:
    fringe = gf.alpha_fringe_hair()
    assert fringe.shape == (64, 64, 4)
    skin = np.float32([0.60, 0.50, 0.40])
    # 不透明区 RGB=肤色、A=1
    np.testing.assert_array_equal(fringe[10, 10], np.float32([0.60, 0.50, 0.40, 1.0]))
    # 彩边：alpha 渐隐而 RGB 保持（straight，不随 alpha 缩放）
    np.testing.assert_array_equal(fringe[10, 32, :3], skin)
    assert 0.0 < float(fringe[10, 32, 3]) < 1.0
    assert float(fringe[10, 37, 3]) < float(fringe[10, 32, 3])
    # 全透明区为 0（无 hidden 颜色；发丝最左可到 x=41，取 x=39 避开）
    np.testing.assert_array_equal(fringe[10, 39], np.float32(0.0))
    # 发丝存在部分 alpha 的暗色像素
    hair = fringe[(fringe[:, :, 3] > 0) & (fringe[:, :, 3] < 1)]
    assert hair.shape[0] > 64  # 6 列彩边 + 发丝像素
    assert np.all(hair[:, 3] > 0)


def test_short_seq_deterministic_motion() -> None:
    seq = gf.short_seq_8()
    assert seq.shape == (8, 32, 16, 3)
    dark, bright = np.float32(0.18), np.float32(0.72)
    # 首帧边缘在 x=2，末帧在 x=14；两侧恒为 0.18/0.72
    assert float(seq[0, 0, 1, 0]) == float(dark) and float(seq[0, 0, 2, 0]) == float(bright)
    assert float(seq[7, 0, 13, 0]) == float(dark) and float(seq[7, 0, 14, 0]) == float(bright)
    # 帧间确有变化（非静止）
    assert not np.array_equal(seq[0], seq[7])


def test_lattice_hash_deterministic_and_bounded() -> None:
    a = gf._lattice_hash(np.arange(100), np.arange(100), seed=5)
    b = gf._lattice_hash(np.arange(100), np.arange(100), seed=5)
    np.testing.assert_array_equal(a, b)
    assert np.all(a >= 0.0) and np.all(a < 1.0)
    c = gf._lattice_hash(np.arange(100), np.arange(100), seed=6)
    assert not np.array_equal(a, c)  # seed 有效


def test_rank_and_paper_distributions() -> None:
    rank = gf.rank_field_64()
    assert rank.shape == (64, 64)
    assert float(rank.min()) >= 0.0 and float(rank.max()) < 1.0
    # 均匀性粗检：4096 像素过半应在 ±10% 内
    q = int((rank < 0.5).sum())
    assert 1850 < q < 2250
    paper = gf.paper_tex_64()
    assert paper.shape == (64, 64)
    assert float(paper.min()) >= 0.0 and float(paper.max()) < 1.0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
