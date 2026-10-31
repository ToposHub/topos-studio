"""BENCH-0004 回归测试：codec 体积帽 / QP 地板预算门禁脚本。

被测对象：``tools/check_codec_budget.py``（读
``bench_out/real_codec_bitrate_20260907/result.json``，按 98% 体积帽与
QP63 地板预算逐档判定，违规 exit 1）。

覆盖：
1. 真实 bench 数据 → 6 项体积帽违规 + 3 项 QP 地板超支违规（与
   docs/benchreports/2026-09-28/codec.md §1.1 表值一致）；
2. 合规合成数据 → 通过（防"永远红灯"的门禁假阳性）；
3. 地板钉死判定语义（qp_min==qp_max==地板才算钉死；仅触及不算）；
4. bpp 复算完整性自检、缺档检测、--exclude 跳过；
5. CLI exit code（真实数据 1 / 合成数据 0）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from check_codec_budget import (  # noqa: E402
    DEFAULT_RESULT,
    check_result,
    recompute_bpp,
)

#: 真实数据（M，bench_out/real_codec_bitrate_20260907/result.json）期望值。
REAL_SIZE_RATIOS: dict[str, float] = {
    "proxy": 3.79,
    "lt": 1.46,
    "standard": 1.52,
    "hq": 1.29,
    "pro444": 1.03,
    "extreme": 1.36,
}
REAL_FLOOR_OVERSPEND: dict[str, float] = {
    "proxy": 1.53,
    "pro444": 1.40,
    "extreme": 1.67,
}


def _load_real() -> dict[str, Any]:
    return json.loads(DEFAULT_RESULT.read_text())


def _compliant_payload() -> dict[str, Any]:
    """构造全部档位合规的合成数据（体积 < 0.98 帽 + qp 未钉死）。"""
    payload = _load_real()
    for tier in payload["tiers"]:
        tier["prores_mov"]["bytes"] = 10_000_000
        tier["topos_mov"]["bytes"] = 9_000_000
        tier["topos_mov"]["bpp"] = recompute_bpp(
            9_000_000, payload["width"], payload["height"], payload["frames"])
        tier["topos_qp_min"] = 40
        tier["topos_qp_max"] = 44
    return payload


class TestRecomputeBpp:
    def test_matches_real_json_stored_bpp(self) -> None:
        # proxy 档：23107368 B / 1920x1080x96 → 0.928633（result.json 存档值）
        got = recompute_bpp(23_107_368, 1920, 1080, 96)
        assert abs(got - 0.928633) < 1e-6

    def test_invalid_geometry_raises(self) -> None:
        with pytest.raises(ValueError):
            recompute_bpp(100, 0, 1080, 96)


class TestRealDataGate:
    def test_real_data_fails_with_expected_violations(self) -> None:
        violations, rows, _notes = check_result(_load_real())
        caps = sorted(v for v in violations if v.startswith("cap: "))
        floors = sorted(v for v in violations if v.startswith("qp-floor: "))
        assert len(caps) == 6, "六档体积帽全部超限（BENCH-0004 §1.1）"
        assert len(floors) == 3, "三档 QP63 地板超支（BENCH-0004 §1.1）"
        assert not [v for v in violations if v.startswith(("tier-missing", "schema"))]

    def test_real_size_ratios_match_bench_report(self) -> None:
        _violations, rows, _notes = check_result(_load_real())
        by_tier = {r["tier"]: r for r in rows}
        for tier_id, expected in REAL_SIZE_RATIOS.items():
            assert by_tier[tier_id]["size_ratio"] == pytest.approx(
                expected, abs=0.005), f"{tier_id} 体积比与 codec.md §1.1 不一致"

    def test_real_floor_overspend_match_bench_report(self) -> None:
        _violations, rows, _notes = check_result(_load_real())
        by_tier = {r["tier"]: r for r in rows}
        for tier_id, expected in REAL_FLOOR_OVERSPEND.items():
            assert by_tier[tier_id]["floor_pinned"] is True
            assert by_tier[tier_id]["overspend"] == pytest.approx(
                expected, abs=0.005)
        # 未钉死档不产生超支判定
        for tier_id in ("lt", "standard", "hq"):
            assert by_tier[tier_id]["floor_pinned"] is False
            assert by_tier[tier_id]["overspend"] is None


class TestGateSemantics:
    def test_compliant_payload_passes(self) -> None:
        violations, rows, _notes = check_result(_compliant_payload())
        assert violations == []
        assert all(r["status"] == "PASS" for r in rows)

    def test_pinned_floor_without_overspend_passes_floor_check(self) -> None:
        payload = _compliant_payload()
        # 钉死在地板但 bpp 低于目标（假想：地板档恰好可再降码率）
        payload["tiers"][0]["topos_qp_min"] = 63
        payload["tiers"][0]["topos_qp_max"] = 63
        payload["tiers"][0]["topos_target_bpp"] = 5.0  # 目标高于实际
        violations, _rows, _notes = check_result(payload)
        assert not [v for v in violations if v.startswith("qp-floor:")]

    def test_overspend_within_tolerance_passes(self) -> None:
        payload = _load_real()
        # 超支 1.53/1.40 的两档在容差 1.6 下不再违规；extreme 1.67 仍违规。
        violations, _rows, _notes = check_result(payload, floor_tolerance=1.6)
        floors = sorted(v for v in violations if v.startswith("qp-floor: "))
        assert len(floors) == 1 and floors[0].startswith("qp-floor: extreme")

    def test_missing_tier_flagged(self) -> None:
        payload = _compliant_payload()
        payload["tiers"] = [t for t in payload["tiers"] if t["id"] != "hq"]
        violations, _rows, _notes = check_result(payload)
        assert any(v.startswith("tier-missing: 档位 hq") for v in violations)

    def test_tampered_bpp_detected(self) -> None:
        payload = _compliant_payload()
        payload["tiers"][0]["topos_mov"]["bpp"] = 0.01  # 与 bytes 复算矛盾
        violations, _rows, _notes = check_result(payload)
        assert any(v.startswith("bpp-arithmetic:") for v in violations)

    def test_exclude_skips_tier(self) -> None:
        payload = _compliant_payload()
        # 把 proxy 改回真实超限值，再排除它
        payload["tiers"][0]["topos_mov"]["bytes"] = 23_107_368
        violations, rows, _notes = check_result(payload, exclude=("proxy",))
        assert not [v for v in violations if "proxy" in v]
        by_tier = {r["tier"]: r for r in rows}
        assert by_tier["proxy"]["status"] == "SKIP"

    def test_cap_ratio_respected(self) -> None:
        payload = _compliant_payload()  # 比例 0.9
        violations, _rows, _notes = check_result(payload, cap_ratio=0.85)
        assert len([v for v in violations if v.startswith("cap: ")]) == 6


class TestCliExitCode:
    def test_real_data_exits_1(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO / "tools" / "check_codec_budget.py")],
            capture_output=True, text=True, cwd=REPO)
        assert proc.returncode == 1
        assert "验收线判定：不满足" in proc.stdout
        assert "qp-floor: proxy" in proc.stdout

    def test_compliant_payload_exits_0(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(_compliant_payload()))
        proc = subprocess.run(
            [sys.executable, str(REPO / "tools" / "check_codec_budget.py"),
             "--result", str(result_path)],
            capture_output=True, text=True, cwd=REPO)
        assert proc.returncode == 0
        assert "验收线判定：满足" in proc.stdout
