"""
EditTrack mutation API 字段丢失回归测试（AM-04 阶段 0.2）。

验证 with_name / with_color / with_output_bus 三个 mutation API 在修改单字段时，
**完整保留 effect_chain**（含 ≥2 个 FX 的链）。

缺陷背景（AM-04）：这三个方法手工构造新 EditTrack 时漏传 effect_chain，
导致重命名、换色、改输出 Bus 会把已配置的轨道 FX 链清空。

验收标准（计划表 0.2）：
- 含 FX 链的轨道执行 rename/color/output bus 后，effect_chain.to_dict() 完全相同
- undo/redo 后链序列和参数逐字段相等
- save/load 序列化往返后相同
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import (
    EditTrack,
    EffectChainParams,
    StripEQParams,
    TrackEffectInstance,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_fx_chain() -> EffectChainParams:
    """构造含 2 个 FX 的效果链（strip_eq + limiter），满足"≥2 FX"验收要求。"""
    eq_params = StripEQParams.default("3-band")
    return EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="strip_eq",
            params=eq_params.to_dict(),
            enabled=True,
            order=0,
            panel="eq",
        ),
        TrackEffectInstance(
            effect_type="limiter",
            params={"enabled": True, "threshold": -1.5, "release": 100.0},
            enabled=True,
            order=1,
            panel="dynamics",
        ),
    ))


def _track_with_chain() -> EditTrack:
    """构造一个带 2-FX 链的音频轨。"""
    return EditTrack(
        track_id="t1", name="A1", track_type="audio", index=0,
        effect_chain=_make_fx_chain(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 核心回归：三个 mutation API 保留 effect_chain
# ─────────────────────────────────────────────────────────────────────────────

class TestWithNamePreservesEffectChain:
    def test_with_name_keeps_chain(self):
        """with_name 后 effect_chain 不丢失。"""
        t = _track_with_chain()
        before = t.effect_chain.to_dict()

        t2 = t.with_name("Renamed")

        assert t2.name == "Renamed"
        assert t2.effect_chain is not None
        assert t2.effect_chain.to_dict() == before

    def test_with_name_chain_field_equal_per_entry(self):
        """逐字段比对链条目（AM-04 验收：逐字段相等）。"""
        t = _track_with_chain()
        t2 = t.with_name("Renamed")

        assert t2.effect_chain is not None
        assert len(t2.effect_chain.effects) == 2
        for orig, new in zip(t.effect_chain.effects, t2.effect_chain.effects):
            assert orig.effect_type == new.effect_type
            assert orig.params == new.params
            assert orig.enabled == new.enabled
            assert orig.order == new.order
            assert orig.instance_id == new.instance_id


class TestWithColorPreservesEffectChain:
    def test_with_color_keeps_chain(self):
        t = _track_with_chain()
        before = t.effect_chain.to_dict()

        t2 = t.with_color("#ff5722")

        assert t2.color == "#ff5722"
        assert t2.effect_chain is not None
        assert t2.effect_chain.to_dict() == before

    def test_with_color_none_clear_keeps_chain(self):
        """with_color(None) 清除颜色但保留 FX 链（None 语义测试）。"""
        t = _track_with_chain().with_color("#abcdef")
        assert t.color == "#abcdef"

        t2 = t.with_color(None)

        assert t2.color is None  # 颜色已清除
        assert t2.effect_chain is not None
        assert len(t2.effect_chain.effects) == 2


class TestWithOutputBusPreservesEffectChain:
    def test_with_output_bus_keeps_chain(self):
        t = _track_with_chain()
        before = t.effect_chain.to_dict()

        t2 = t.with_output_bus("bus1")

        assert t2.output_bus_id == "bus1"
        assert t2.effect_chain is not None
        assert t2.effect_chain.to_dict() == before

    def test_with_output_bus_none_to_master_keeps_chain(self):
        """with_output_bus(None) 输出到 Master 但保留 FX 链（None 语义测试）。"""
        t = _track_with_chain().with_output_bus("bus1")
        assert t.output_bus_id == "bus1"

        t2 = t.with_output_bus(None)

        assert t2.output_bus_id is None  # 回到 Master
        assert t2.effect_chain is not None
        assert len(t2.effect_chain.effects) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 不可变性 + 序列化往返
# ─────────────────────────────────────────────────────────────────────────────

class TestImmutabilityAndSerialization:
    def test_original_track_unchanged(self):
        """mutation 后原轨 effect_chain 不变（不可变性）。"""
        t = _track_with_chain()
        original_dict = t.effect_chain.to_dict()

        _ = t.with_name("X")
        _ = t.with_color("#000")
        _ = t.with_output_bus("bus1")

        assert t.effect_chain.to_dict() == original_dict

    def test_serialize_roundtrip_preserves_chain_after_mutation(self):
        """mutation → to_dict → from_dict 往返后 effect_chain 相同。"""
        t = _track_with_chain().with_name("Renamed").with_color("#112233")

        seq_dict = t.to_dict()
        restored = EditTrack.from_dict(seq_dict)

        assert restored.effect_chain is not None
        assert restored.effect_chain.to_dict() == t.effect_chain.to_dict()
        assert restored.name == "Renamed"
        assert restored.color == "#112233"

    def test_mutation_chain_bytes_stable(self):
        """链内容（含 instance_id）在 mutation 前后逐字节稳定。"""
        t = _track_with_chain()
        ids_before = [e.instance_id for e in t.effect_chain.effects]

        for mut in (t.with_name("a"), t.with_color("#fff"), t.with_output_bus("b")):
            ids = [e.instance_id for e in mut.effect_chain.effects]
            assert ids == ids_before


# ─────────────────────────────────────────────────────────────────────────────
# 全字段保留（防止未来新增字段时再次漏传）
# ─────────────────────────────────────────────────────────────────────────────

class TestAllFieldsPreserved:
    """三个 mutation API 必须保留除目标字段外的所有字段。

    这是对 AM-04 的扩展保护：未来新增 EditTrack 字段时，若 mutation 方法漏传，
    此处会失败。当前显式枚举所有公共字段。
    """

    def _full_track(self) -> EditTrack:
        return EditTrack(
            track_id="t1", name="A1", track_type="audio", index=2,
            volume=0.8, pan=0.5, color="#aabbcc",
            is_enabled=True, is_locked=True, is_visible=False,
            is_muted=True, is_solo=True,
            effect_chain=_make_fx_chain(),
        )

    def test_with_name_preserves_all(self):
        t = self._full_track()
        t2 = t.with_name("New")
        for attr in ("track_id", "track_type", "index", "volume", "pan", "color",
                     "is_enabled", "is_locked", "is_visible", "is_muted", "is_solo"):
            assert getattr(t2, attr) == getattr(t, attr), f"{attr} changed"
        assert t2.effect_chain == t.effect_chain

    def test_with_color_preserves_all(self):
        t = self._full_track()
        t2 = t.with_color("#000000")
        for attr in ("track_id", "name", "track_type", "index", "volume", "pan",
                     "is_enabled", "is_locked", "is_visible", "is_muted", "is_solo"):
            assert getattr(t2, attr) == getattr(t, attr), f"{attr} changed"
        assert t2.effect_chain == t.effect_chain

    def test_with_output_bus_preserves_all(self):
        t = self._full_track()
        t2 = t.with_output_bus("bus9")
        for attr in ("track_id", "name", "track_type", "index", "volume", "pan", "color",
                     "is_enabled", "is_locked", "is_visible", "is_muted", "is_solo"):
            assert getattr(t2, attr) == getattr(t, attr), f"{attr} changed"
        assert t2.effect_chain == t.effect_chain
