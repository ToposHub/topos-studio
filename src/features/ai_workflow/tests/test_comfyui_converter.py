"""P7.2 测试：C4 核心节点转换器（plan §1.4.2 / §1.4.2 golden workflow tests）。

> 将少数官方稳定节点映射为 Topos 原生高层节点，例如 Text-to-Image、Image-to-Video、
> Upscale。显式 converter + golden workflow tests。

验证 :class:`ComfyToToposConverter` 的核心契约（plan §1.4.2 C4 / §4.3）：

- 把稳定的 ComfyUI workflow 模式识别为 Topos 高层节点（text_to_image / upscale）。
- 转换是**显式 + 可验证**的：每个识别模式有 golden fixture + 等价断言。
- 无法识别的 workflow → 不强行转换（保留为 opaque Comfy 子图，C5 不承诺）。
- 转换产出的 Topos 节点 spec_id 受控（topos.generation.* 等）。
- 转换不执行任何 Comfy 代码（只读模式匹配）。

红线（plan §1.4.2 C4/C5 / §4.4 / §0）：

- **C5 不承诺**：未知 custom node workflow 不强行转 Topos 节点（保留为 opaque 子图）。
- 转换出的 Topos 节点 ``spec_id`` 必须在 :class:`NodeSpecRegistry` 已注册。
- 转换不执行 custom node 代码（只读模式匹配）。
- 转换出的节点参数来自 ComfyUI workflow 的字面量输入（不是 link 引用）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from src.features.ai_workflow.adapters.comfyui_converter import (
    ComfyToToposConverter,
    ConversionVerdict,
    GOLDEN_FIXTURES,
)
from src.features.ai_workflow.adapters.comfyui_importer import ComfyWorkflowImporter
from src.features.ai_workflow.adapters.comfyui_models import ComfyWorkflowSnapshot
from src.features.ai_workflow.adapters.generation_node_specs import (
    register_generation_node_specs,
)
from src.features.ai_workflow.core.node_specs import NodeSpecRegistry


# ---------------------------------------------------------------------- #
# Golden fixtures
# ---------------------------------------------------------------------- #


def _sdxl_t2i_workflow() -> dict:
    """官方稳定 SDXL text-to-image 模式（KSampler + CheckpointLoaderSimple + CLIPTextEncode +
    EmptyLatentImage + VAEDecode + SaveImage）。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 12345, "cfg": 7.0, "steps": 30,
            "model": ["4", 0], "positive": ["6", 0],
            "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": "sd_xl_base_1.0.safetensors",
        }},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
    }


def _upscale_workflow() -> dict:
    """官方稳定 upscale 模式（UpscaleImageLoader + ImageUpscaleWithModel + SaveImage）。"""
    return {
        "10": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
        "11": {"class_type": "UpscaleModelLoader", "inputs": {
            "model_name": "RealESRGAN_x4plus.pth",
        }},
        "12": {"class_type": "ImageUpscaleWithModel", "inputs": {
            "upscale_model": ["11", 0], "image": ["10", 0],
        }},
        "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0]}},
    }


def _imported(raw: dict) -> ComfyWorkflowSnapshot:
    return ComfyWorkflowImporter().import_from_dict("wf", raw)


def _generation_registry() -> NodeSpecRegistry:
    """注册好生成节点 spec 的 registry（fixture 与需要 registry 本体的测试共用）。"""
    reg = NodeSpecRegistry()
    register_generation_node_specs(reg)
    return reg


@pytest.fixture()
def converter() -> ComfyToToposConverter:
    """注册好生成节点 spec 的转换器（AUD-23511：集中 10+ 处构造样板）。

    ``register_generation_node_specs`` 签名或 converter 构造变化只需改这里。
    """
    return ComfyToToposConverter(registry=_generation_registry())


def _install_execution_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """使"转换不执行 custom node"红线可证伪（AUD-23510）。返回哨兵调用记录。

    恶意节点 payload 只是 dict 字面量，无论实现如何都不会"执行"；不设哨兵时
    红线不可证伪。策略分两类：

    - ``eval`` / ``exec`` / ``os.system``：被触达立即抛 AssertionError（记录 +
      熔断，绝不真正执行 payload）；
    - ``__import__``：只记录**新加载**模块、不阻断——pytest ``MonkeyPatch``
      自身会对已加载 stdlib（如 ``inspect``）做懒 import（框架噪音），且失败
      渲染也会懒 import，直接抛会把整个 session 打成 INTERNALERROR。
    """
    import builtins
    import inspect as _inspect  # noqa: F401  预热：MonkeyPatch.setattr 内部懒 import
    import os
    import sys

    calls: list[str] = []
    real_import = builtins.__import__
    loaded_at_install = frozenset(sys.modules)

    def _refuse(name: str) -> Callable[..., None]:
        def _boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError(
                f"红线被破坏：converter 执行了 {name}（只读模式匹配，不得执行）"
            )
        return _boom

    def _spy_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in loaded_at_install and name not in sys.modules:
            calls.append(f"__import__({name})")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "eval", _refuse("eval"))
    monkeypatch.setattr(builtins, "exec", _refuse("exec"))
    monkeypatch.setattr(builtins, "__import__", _spy_import)
    monkeypatch.setattr(os, "system", _refuse("os.system"))
    return calls


# ---------------------------------------------------------------------- #
# ConversionVerdict
# ---------------------------------------------------------------------- #


class TestConversionVerdict:
    def test_convert_recognizes_sdxl_t2i_as_text_to_image(self, converter: ComfyToToposConverter) -> None:
        snap = _imported(_sdxl_t2i_workflow())
        outcome = converter.convert(snap)
        assert outcome.verdict == ConversionVerdict.CONVERTED
        assert len(outcome.findings) == 1
        f = outcome.findings[0]
        assert f.target_spec_id == "topos.generation.text_to_image"

    def test_convert_recognizes_upscale_workflow(self, converter: ComfyToToposConverter) -> None:
        snap = _imported(_upscale_workflow())
        outcome = converter.convert(snap)
        assert outcome.verdict == ConversionVerdict.CONVERTED
        assert outcome.findings[0].target_spec_id == "topos.generation.upscale"

    def test_unknown_workflow_not_converted(self, converter: ComfyToToposConverter) -> None:
        # C5 不承诺：未知模式 → verdict=OPAQUE（保留为 Comfy 子图）
        snap = _imported({
            "1": {"class_type": "SomeRandomNode", "inputs": {"x": 1}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        })
        outcome = converter.convert(snap)
        assert outcome.verdict == ConversionVerdict.OPAQUE
        assert len(outcome.findings) == 0
        # 应有说明（不强行转换）
        assert any("opaque" in w.lower() or "not converted" in w.lower()
                   for w in outcome.warnings)

    def test_partial_workflow_not_converted(self, converter: ComfyToToposConverter) -> None:
        # 部分 SDXL（缺 SaveImage）→ 不应识别为 text_to_image
        raw = _sdxl_t2i_workflow()
        del raw["9"]  # 缺输出节点
        snap = _imported(raw)
        outcome = converter.convert(snap)
        # 不完整的 SDXL → OPAQUE（不强行转）
        assert outcome.verdict == ConversionVerdict.OPAQUE


# ---------------------------------------------------------------------- #
# ConversionFinding —— 转换出的 Topos 节点参数
# ---------------------------------------------------------------------- #


class TestConversionFindingParams:
    def test_t2i_finding_extracts_params_from_workflow(self, converter: ComfyToToposConverter) -> None:
        snap = _imported(_sdxl_t2i_workflow())
        outcome = converter.convert(snap)
        f = outcome.findings[0]
        # 参数应从 ComfyUI 字面量输入提取
        assert f.params["width"] == 1024
        assert f.params["height"] == 1024
        assert f.params["seed"] == 12345
        assert f.params["steps"] == 30

    def test_t2i_finding_target_spec_is_registered(
        self, converter: ComfyToToposConverter,
    ) -> None:
        # 红线：转换出的 spec_id 必须在 registry 已注册
        # （converter 构造时即校验；这里对同构 registry 双重确认）
        reg = _generation_registry()
        snap = _imported(_sdxl_t2i_workflow())
        outcome = converter.convert(snap)
        assert len(outcome.findings) >= 1
        for f in outcome.findings:
            assert reg.get(f.target_spec_id) is not None, (
                f"converted target spec {f.target_spec_id!r} not registered"
            )

    def test_finding_carries_source_node_ids(self, converter: ComfyToToposConverter) -> None:
        snap = _imported(_sdxl_t2i_workflow())
        outcome = converter.convert(snap)
        f = outcome.findings[0]
        # 应记录源 Comfy 节点 ID（可追溯）
        assert len(f.source_node_ids) > 0
        assert "3" in f.source_node_ids  # KSampler

    def test_finding_to_dict_serializable(self, converter: ComfyToToposConverter) -> None:
        snap = _imported(_sdxl_t2i_workflow())
        outcome = converter.convert(snap)
        d = outcome.findings[0].to_dict()
        import json
        json.dumps(d)  # 可序列化
        assert "target_spec_id" in d
        assert "params" in d


# ---------------------------------------------------------------------- #
# Golden workflow fixtures —— plan §1.4.2 "golden workflow tests"
# ---------------------------------------------------------------------- #


class TestGoldenFixtures:
    def test_golden_fixtures_cover_core_patterns(self) -> None:
        # plan §1.4.2 列举的核心转换：Text-to-Image / Image-to-Video / Upscale
        target_ids = {g.expected_target_spec_id for g in GOLDEN_FIXTURES}
        assert "topos.generation.text_to_image" in target_ids
        assert "topos.generation.upscale" in target_ids

    def test_each_golden_fixture_has_name_and_description(self) -> None:
        for g in GOLDEN_FIXTURES:
            assert g.name, "golden fixture missing name"
            assert g.description, f"golden fixture {g.name!r} missing description"
            assert g.workflow_dict, f"golden fixture {g.name!r} missing workflow"
            assert g.expected_target_spec_id, (
                f"golden fixture {g.name!r} missing expected_target_spec_id"
            )

    def test_all_golden_fixtures_convert_correctly(self, converter: ComfyToToposConverter) -> None:
        # 红线：每个 golden fixture 必须被 converter 正确识别
        for g in GOLDEN_FIXTURES:
            snap = ComfyWorkflowImporter().import_from_dict(
                f"golden-{g.name}", g.workflow_dict,
            )
            outcome = converter.convert(snap)
            assert outcome.verdict == ConversionVerdict.CONVERTED, (
                f"golden fixture {g.name!r} was NOT converted (verdict={outcome.verdict}); "
                f"warnings={outcome.warnings}"
            )
            assert any(
                f.target_spec_id == g.expected_target_spec_id
                for f in outcome.findings
            ), (
                f"golden fixture {g.name!r} expected {g.expected_target_spec_id!r} "
                f"but got {[f.target_spec_id for f in outcome.findings]}"
            )

    def test_golden_fixture_with_tampered_pattern_not_converted(self, converter: ComfyToToposConverter) -> None:
        # 红线：改动 golden fixture 的关键 class_type → 不应再识别
        import copy
        # 取 SDXL T2I golden，把 KSampler 改成未知
        g = next(g for g in GOLDEN_FIXTURES
                 if g.expected_target_spec_id == "topos.generation.text_to_image")
        tampered = copy.deepcopy(g.workflow_dict)
        # 改 KSampler 节点的 class_type
        for nid, node in tampered.items():
            if node.get("class_type") == "KSampler":
                tampered[nid]["class_type"] = "NotAKSampler"
                break
        snap = ComfyWorkflowImporter().import_from_dict("tampered", tampered)
        outcome = converter.convert(snap)
        # 改动后不应识别为 text_to_image
        assert outcome.verdict == ConversionVerdict.OPAQUE


# ---------------------------------------------------------------------- #
# ConversionOutcome —— 整体结果
# ---------------------------------------------------------------------- #


class TestConversionOutcome:
    def test_outcome_to_dict_serializable(self, converter: ComfyToToposConverter) -> None:
        snap = _imported(_sdxl_t2i_workflow())
        outcome = converter.convert(snap)
        d = outcome.to_dict()
        import json
        json.dumps(d)
        assert "verdict" in d
        assert "findings" in d

    def test_outcome_records_workflow_hash(self, converter: ComfyToToposConverter) -> None:
        snap = _imported(_sdxl_t2i_workflow())
        outcome = converter.convert(snap)
        assert outcome.source_workflow_hash == snap.content_hash

    def test_converter_does_not_execute_custom_nodes(
        self, converter: ComfyToToposConverter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """红线：转换只读模式匹配，不执行任何 custom node 代码（AUD-23510）。"""
        raw = _sdxl_t2i_workflow()
        raw["999"] = {
            "class_type": "EvalNode",
            "inputs": {"code": "import os; os.system('rm -rf /')"},
        }
        snap = _imported(raw)
        # 哨兵使红线可证伪：恶意 payload 若被 eval/exec/system/import 触达即失败。
        # 哨兵只覆盖 convert() 调用窗口（红线主体）；断言前解除，避免 pytest
        # 失败渲染自身的懒 import 打成 INTERNALERROR。
        imports_seen = _install_execution_sentinels(monkeypatch)
        try:
            # 即使有恶意节点，转换也不执行（SDXL 模式仍识别）
            outcome = converter.convert(snap)
        finally:
            monkeypatch.undo()
        # 应识别为 text_to_image（恶意节点不影响模式匹配）
        assert outcome.verdict == ConversionVerdict.CONVERTED
        assert outcome.findings[0].target_spec_id == "topos.generation.text_to_image"
        # 恶意节点只作为元数据保留（可追溯记录在 source），未被翻译执行
        assert "999" in outcome.findings[0].source_node_ids
        assert not imports_seen, (
            f"红线被破坏：converter 触发了 import：{imports_seen}"
        )
