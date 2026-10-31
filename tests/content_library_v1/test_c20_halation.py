"""C20 lib1_halation 测试：F06 multiscale_halation 共享核再导出（单一
事实源，GPU 单 pass 等价口径 = evaluate_degraded_single_scale）+ 语义
冒烟（暗场恒等/高光红晕）+ manifest 契约。算法语义已由 test_f06 钉住。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.references import halation_ref as ref
from content_staging.library_v1.references import multiscale_halation_ref as f06
from content_staging.library_v1.nodes.halation.lib1_halation_node import (
    EFFECT_KEY, Lib1HalationNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "halation" / "component_manifest.json")


class TestC20SingleSource:
    def test_reexport_is_f06_degraded_entry(self) -> None:
        """禁双份漂移：evaluate = F06 的单尺度降级入口，DEFAULTS 同对象。"""
        assert ref.evaluate is f06.evaluate_degraded_single_scale
        assert ref.DEFAULTS is f06.DEFAULTS
        assert ref.SOURCE_REF == "multiscale_halation_ref"
        assert ref.SOURCE_ENTRY == "evaluate_degraded_single_scale"


class TestC20Semantics:
    def test_dark_field_identity(self) -> None:
        # 无高光 → 提取场 ≈ 0 → 加性输出恒等（0.05 远低于膝点）
        img = np.full((6, 6, 3), 0.05, dtype=np.float32)
        np.testing.assert_allclose(ref.evaluate(img), img, atol=1e-6)

    def test_highlight_spreads_red_halo(self) -> None:
        # 单白点 4.0：邻像素 R 通道被抬升（红重着色），远处像素不动
        img = np.zeros((8, 8, 3), dtype=np.float32)
        img[4, 4] = 4.0
        out = ref.evaluate(img)
        assert float(out[4, 5, 0]) > 0.2          # 红晕扩散（手算 0.224）
        assert float(out[4, 5, 2]) < float(out[4, 5, 0])  # 蓝通道弱于红
        assert float(out[0, 0, 0]) == pytest.approx(0.0, abs=1e-6)

    def test_matches_direct_f06_call(self) -> None:
        # C20 口径 = F06 的单尺度降级入口（真多尺度版留 evaluate_multiscale
        # 备查，两者有意不同——README/manifest known_limits 声明）
        rng = np.random.RandomState(31)
        img = rng.rand(6, 8, 3).astype(np.float32) * 2.0
        np.testing.assert_array_equal(
            ref.evaluate(img), f06.evaluate_degraded_single_scale(img))


class TestC20NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1HalationNode()
        assert node.node_type == EFFECT_KEY == "lib1_halation"


class TestC20Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C20"
        node = Lib1HalationNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        assert [p.name for p in manifest.params] == list(ref.DEFAULTS.keys())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
