"""T-P1-60：节点契约五分类与 93 节点可解释状态契约测试。

任务卡：93 节点不得再用同一 image-input harness 一概判断；每节点必须有
明确结果状态（passed_nonflat / passed_flat_by_contract /
blocked_missing_resource / blocked_missing_model / unsupported_backend /
failed）——``None`` 和全黑不能笼统叫通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.composite.core.node_capabilities import (  # noqa: E402
    _NODE_CAPABILITY_TABLE,
    get_node_contract_class,
)
from src.features.composite.core.node_engine import NODE_REGISTRY  # noqa: E402


class TestContractClassification:
    def test_all_registered_nodes_have_contract_class(self) -> None:
        missing = [nt for nt in NODE_REGISTRY
                   if get_node_contract_class(nt) is None]
        assert not missing, f"未分类节点: {missing}"

    def test_class_distribution_covers_five_classes(self) -> None:
        classes = {get_node_contract_class(nt) for nt in NODE_REGISTRY}
        assert classes == {
            "image_filter", "source", "generator", "ai_model",
            "3d_file_output",
        }

    @pytest.mark.parametrize("node_type,expected", [
        ("blur", "image_filter"),
        ("merge", "image_filter"),
        ("video_frame", "source"),
        ("image_input", "source"),
        ("constant", "generator"),
        ("text", "generator"),
        ("ai_generate", "ai_model"),
        ("ai_inpaint", "ai_model"),
        ("scene3d", "3d_file_output"),
        ("file_output", "3d_file_output"),
        ("output", "3d_file_output"),
    ])
    def test_probes(self, node_type: str, expected: str) -> None:
        assert get_node_contract_class(node_type) == expected

    def test_capability_table_covers_registry(self) -> None:
        """能力表应覆盖 registry 的全部 93 key（5 alias 合并后一致）。"""
        uncovered = [nt for nt in NODE_REGISTRY
                     if nt not in _NODE_CAPABILITY_TABLE]
        # 未覆盖项必须经 get_node_capability 的类属性合并路径可解析
        for node_type in uncovered:
            from src.features.composite.core.node_capabilities import (
                get_node_capability,
            )

            assert get_node_capability(node_type) is not None, node_type


class TestMatrixStatusClassifier:
    def _classifier(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib

        mod = importlib.import_module("bench_composite_nodes_gpu_coverage")
        return mod.classify_node_result

    def test_image_filter_flat_is_failed(self) -> None:
        classify = self._classifier()
        info = {"ok": True, "flat": True}
        assert classify("blur", info, None) == "failed"

    def test_generator_flat_is_by_contract(self) -> None:
        classify = self._classifier()
        assert classify("constant", {"ok": True, "flat": True}, None) == \
            "passed_flat_by_contract"

    def test_ai_render_none_is_blocked_missing_model(self) -> None:
        classify = self._classifier()
        assert classify("ai_generate", {"ok": False,
                                        "error": "render returned None"},
                        None) == "blocked_missing_model"

    def test_nonflat_is_pass(self) -> None:
        classify = self._classifier()
        assert classify("blur", {"ok": True, "std": 12.0}, None) == \
            "passed_nonflat"

    def test_resource_error_is_blocked(self) -> None:
        classify = self._classifier()
        assert classify("video_frame", {},
                        "FileNotFoundError: media missing") == \
            "blocked_missing_resource"

    def test_other_error_is_failed(self) -> None:
        classify = self._classifier()
        assert classify("blur", {}, "ValueError: bad param") == "failed"
