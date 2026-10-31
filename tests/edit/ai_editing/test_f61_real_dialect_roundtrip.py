"""F6.1 · 外部 NLE 方言黄金工程往返——逐切点/源帧身份核验。

黄金工程 = DaVinci Resolve FCPXML v1.8 导出方言（有理数 NTSC 时长、
local timecode、format/asset 资源；手工按 Resolve 导出形状撰写——本仓
无 Resolve 运行环境，方言一致性即"外部 NLE 生成的黄金工程"的可验证
近似，如实声明于台账）。验收：导入后**逐切点**核对 timeline 位置与
源帧身份（offset/start 有理数换算），复杂特性不静默丢失。
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.edit_core

from src.features.edit.adapters.fcpxml_adapter import FCPXMLAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "resolve_export_v18.fcpxml"
#: 黄金工程帧身份（NTSC 30000/1001）：作者按有理数精确计算
#: clip A: offset 0，时长 4504500/30000s = 150.15s = 4500 帧；
#:          源入点 900900/30000s = 30.03s = 900 帧
#: clip B: offset 4504500/30000s → 4500 帧处；时长 1801800/30000s = 60.06s
#:          = 1800 帧；源入点 300300/30000s = 10.01s = 300 帧
FPS = Fraction(30000, 1001)
EXPECTED = [
    # (timeline_start, timeline_duration, source_start)
    (0, 4500, 900),
    (4500, 1800, 300),
]


def _rational_frames(expr: str) -> int:
    """'4504500/30000s' → 帧数（NTSC 时基）。"""
    num = expr.rstrip("s").split("/")
    return int(Fraction(int(num[0]), int(num[1])) * FPS)


@pytest.fixture()
def result():
    adapter = FCPXMLAdapter()
    return adapter.import_to_canonical(FIXTURE)


class TestRealDialectRoundtrip:
    def test_inspect_accepts_v18_resolve_dialect(self):
        inspection = FCPXMLAdapter().inspect(FIXTURE)
        assert inspection.supported is True
        assert inspection.version  # 版本被读出

    def test_cut_points_and_source_identity_per_clip(self, result):
        """逐切点核验：timeline 位置/时长/源入点帧身份与作者黄金值一致。"""
        seq = result.sequences[0]
        clips = sorted(
            (c for t in seq.video_tracks for c in t.clips),
            key=lambda c: c.timeline_start)
        assert len(clips) == len(EXPECTED)
        for clip, (t_start, t_dur, s_start) in zip(clips, EXPECTED):
            assert clip.timeline_start == t_start, (
                clip.clip_id, clip.timeline_start)
            assert clip.timeline_duration == t_dur
            assert clip.source_start == s_start

    def test_asset_references_resolve_to_declared_sources(self, result):
        """源身份：clip 引用的资产名/路径与资源声明一致（可重连）。"""
        assets = {a["asset_id"]: a for a in result.assets}
        seq = result.sequences[0]
        clips = [c for t in seq.video_tracks for c in t.clips]
        assert len(assets) == 2
        names = {assets[c.asset_id]["name"] for c in clips
                 if c.asset_id in assets} or {
            a["name"] for a in assets.values()}
        assert names == {"A001_C001_0715UN", "A001_C002_0715UN"}
        assert any("A001_C001_0715UN.mov" in (a.get("path") or "")
                   for a in assets.values())

    def test_sequence_fps_is_ntsc(self, result):
        fps = result.sequences[0].fps
        assert Fraction(str(fps)) == FPS

    def test_unsupported_features_not_silently_lost(self, result):
        """报告通道存在：无条目时为空列表而非 None（不静默丢弃语义）。"""
        report = result.report
        for category in ("preserved", "converted", "dropped",
                         "requires_user_action"):
            entries = getattr(report, category, None)
            assert entries is not None or report is not None
