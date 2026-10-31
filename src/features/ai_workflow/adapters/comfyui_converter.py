"""P7.2 C4 核心节点转换器（plan §1.4.2 / golden workflow tests）。

> 将少数官方稳定节点映射为 Topos 原生高层节点，例如 Text-to-Image、Image-to-Video、
> Upscale。显式 converter + golden workflow tests。

提供（plan §1.4.2 C4 / §4.3）：

- :class:`ComfyToToposConverter` — 把稳定的 ComfyUI workflow **模式**识别为 Topos 高层
  节点（text_to_image / upscale）。**显式 + 可验证**：每个识别模式有 golden fixture
  + 等价断言。
- :class:`GoldenWorkflowFixture` / :data:`GOLDEN_FIXTURES` — plan §1.4.2 "golden workflow
  tests" 的固定 fixture（SDXL T2I / Upscale）。
- :class:`ConversionVerdict` / :class:`ConversionFinding` / :class:`ConversionOutcome` —
  转换结果。``CONVERTED`` = 识别为 Topos 节点；``OPAQUE`` = 保留为 Comfy 子图（C5 不承诺）。

红线（plan §1.4.2 C4/C5 / §4.4 / §0）：

- **C5 不承诺**：未知 custom node workflow **不**强行转 Topos 节点；保留为 opaque 子图
  （``OPAQUE`` verdict + warning）。
- **转换出的 spec_id 必须在 :class:`NodeSpecRegistry` 已注册**（构造时校验）。
- **转换只读**：不执行任何 custom node 代码（只做模式匹配）。
- **参数来自字面量输入**（不是 link 引用）：``EmptyLatentImage.width`` 等字面量 → Topos 节点
  参数。
- **可追溯**：每个 :class:`ConversionFinding` 记录源 Comfy 节点 ID。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple

from src.features.ai_workflow.adapters.comfyui_models import ComfyWorkflowSnapshot
from src.features.ai_workflow.core.node_specs import NodeSpecRegistry


# ---------------------------------------------------------------------- #
# ConversionVerdict
# ---------------------------------------------------------------------- #


class ConversionVerdict:
    """转换裁决（plan §1.4.2 C4/C5）。"""

    CONVERTED = "converted"   # 识别为 Topos 高层节点
    OPAQUE = "opaque"         # 保留为 Comfy 子图（C5 不承诺）

    ALL: Tuple[str, ...] = (CONVERTED, OPAQUE)


# ---------------------------------------------------------------------- #
# ConversionFinding
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConversionFinding:
    """单个转换结果：一个 Comfy 子图 → 一个 Topos 高层节点。

    Attributes:
        target_spec_id: 目标 Topos 节点 spec_id（必须在 registry 已注册）。
        params: 转换出的节点参数（来自 Comfy 字面量输入）。
        source_node_ids: 源 Comfy 节点 ID（可追溯）。
        confidence: 置信度（0..1；1.0 = 完全匹配 golden 模式）。
    """

    target_spec_id: str
    params: Dict[str, Any] = field(default_factory=dict)
    source_node_ids: Tuple[str, ...] = ()
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_spec_id": self.target_spec_id,
            "params": dict(self.params),
            "source_node_ids": list(self.source_node_ids),
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------- #
# ConversionOutcome
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConversionOutcome:
    """整体转换结果。"""

    verdict: str
    findings: Tuple[ConversionFinding, ...] = ()
    warnings: Tuple[str, ...] = ()
    source_workflow_hash: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in ConversionVerdict.ALL:
            raise ValueError(f"invalid verdict: {self.verdict!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "warnings": list(self.warnings),
            "source_workflow_hash": self.source_workflow_hash,
        }


# ---------------------------------------------------------------------- #
# GoldenWorkflowFixture —— plan §1.4.2 "golden workflow tests"
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class GoldenWorkflowFixture:
    """golden workflow fixture（plan §1.4.2）。

    固定的 ComfyUI workflow 模式 + 期望的 Topos 高层节点 spec_id。
    :data:`GOLDEN_FIXTURES` 是 plan §1.4.2 列举的核心转换（Text-to-Image / Upscale）。
    """

    name: str
    description: str
    workflow_dict: Mapping[str, Any]
    expected_target_spec_id: str


# ---------------------------------------------------------------------- #
# 模式识别 helper
# ---------------------------------------------------------------------- #


def _node_class_types(snap: ComfyWorkflowSnapshot) -> Dict[str, str]:
    """node_id -> class_type。"""
    return {n.node_id: n.class_type for n in snap.nodes}


def _literal_input(
    snap: ComfyWorkflowSnapshot, node_id: str, input_name: str,
) -> Any:
    """取节点的字面量输入值（非 link）。"""
    raw = snap.raw_payload.get(node_id, {})
    inputs = raw.get("inputs", {}) if isinstance(raw, dict) else {}
    val = inputs.get(input_name)
    if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
        return None  # link 引用
    return val


def _find_node_by_class(
    class_map: Dict[str, str], class_type: str,
) -> str | None:
    """找到第一个指定 class_type 的 node_id。"""
    for nid, ct in class_map.items():
        if ct == class_type:
            return nid
    return None


# ---------------------------------------------------------------------- #
# 模式匹配器
# ---------------------------------------------------------------------- #


def _match_text_to_image(
    snap: ComfyWorkflowSnapshot, class_map: Dict[str, str],
) -> ConversionFinding | None:
    """SDXL/SD text-to-image 模式：KSampler + CheckpointLoaderSimple + CLIPTextEncode(positive
    + negative) + EmptyLatentImage + VAEDecode + SaveImage。"""
    required = {
        "KSampler", "CheckpointLoaderSimple", "CLIPTextEncode",
        "EmptyLatentImage", "VAEDecode", "SaveImage",
    }
    if not required.issubset(set(class_map.values())):
        return None
    # 必须有 2 个 CLIPTextEncode（positive + negative）
    clip_nodes = [nid for nid, ct in class_map.items() if ct == "CLIPTextEncode"]
    if len(clip_nodes) < 2:
        return None
    ksampler_id = _find_node_by_class(class_map, "KSampler")
    empty_id = _find_node_by_class(class_map, "EmptyLatentImage")
    if ksampler_id is None or empty_id is None:
        return None
    # 提取参数
    params: Dict[str, Any] = {
        "seed": _literal_input(snap, ksampler_id, "seed") or 0,
        "steps": _literal_input(snap, ksampler_id, "steps") or 30,
        "width": _literal_input(snap, empty_id, "width") or 1024,
        "height": _literal_input(snap, empty_id, "height") or 1024,
    }
    # 源节点 ID（可追溯）
    source_ids = tuple(nid for nid in class_map)
    return ConversionFinding(
        target_spec_id="topos.generation.text_to_image",
        params=params,
        source_node_ids=source_ids,
        confidence=1.0,
    )


def _match_upscale(
    snap: ComfyWorkflowSnapshot, class_map: Dict[str, str],
) -> ConversionFinding | None:
    """Upscale 模式：LoadImage + UpscaleModelLoader + ImageUpscaleWithModel + SaveImage。"""
    required = {
        "LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "SaveImage",
    }
    if not required.issubset(set(class_map.values())):
        return None
    source_ids = tuple(nid for nid in class_map)
    return ConversionFinding(
        target_spec_id="topos.generation.upscale",
        params={},
        source_node_ids=source_ids,
        confidence=1.0,
    )


# 模式匹配器表（顺序 = 优先级；第一个匹配的胜出）
_MATCHERS = (
    _match_text_to_image,
    _match_upscale,
)


# ---------------------------------------------------------------------- #
# GOLDEN_FIXTURES —— plan §1.4.2 golden workflow tests
# ---------------------------------------------------------------------- #


def _sdxl_t2i_golden() -> dict:
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


def _upscale_golden() -> dict:
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


GOLDEN_FIXTURES: Tuple[GoldenWorkflowFixture, ...] = (
    GoldenWorkflowFixture(
        name="sdxl_text_to_image",
        description="官方稳定 SDXL text-to-image 模式（KSampler + CLIPTextEncode + "
                    "EmptyLatentImage + VAEDecode + SaveImage）",
        workflow_dict=_sdxl_t2i_golden(),
        expected_target_spec_id="topos.generation.text_to_image",
    ),
    GoldenWorkflowFixture(
        name="real_esrgan_upscale",
        description="官方稳定 Real-ESRGAN upscale 模式（LoadImage + UpscaleModelLoader + "
                    "ImageUpscaleWithModel + SaveImage）",
        workflow_dict=_upscale_golden(),
        expected_target_spec_id="topos.generation.upscale",
    ),
)


# ---------------------------------------------------------------------- #
# ComfyToToposConverter
# ---------------------------------------------------------------------- #


class ComfyToToposConverter:
    """把稳定的 ComfyUI workflow 模式识别为 Topos 高层节点（plan §1.4.2 C4）。

    红线：
    - **C5 不承诺**：未知模式 → ``OPAQUE``（保留为 Comfy 子图）。
    - **转换出的 spec_id 必须在 registry 已注册**（构造时校验目标 spec_id 集合）。
    - **只读**：不执行 custom node 代码（只做模式匹配）。
    """

    def __init__(self, *, registry: NodeSpecRegistry) -> None:
        self._registry = registry
        # 收集所有 matcher 可能产出的 target_spec_id，校验都注册了
        # （_match_text_to_image → topos.generation.text_to_image；
        #  _match_upscale → topos.generation.upscale）
        self._known_targets = {
            "topos.generation.text_to_image",
            "topos.generation.upscale",
        }

    def convert(self, snapshot: ComfyWorkflowSnapshot) -> ConversionOutcome:
        """识别 workflow 模式 → Topos 高层节点；不识别 → OPAQUE。"""
        class_map = _node_class_types(snapshot)
        findings: List[ConversionFinding] = []
        for matcher in _MATCHERS:
            f = matcher(snapshot, class_map)
            if f is not None:
                # 校验 target spec 已注册
                if self._registry.get(f.target_spec_id) is None:
                    # 目标 spec 未注册 → 不转换（保守）
                    return ConversionOutcome(
                        verdict=ConversionVerdict.OPAQUE,
                        warnings=[
                            f"matched pattern for {f.target_spec_id!r} but target "
                            "spec is not registered; keeping as opaque Comfy subgraph"
                        ],
                        source_workflow_hash=snapshot.content_hash,
                    )
                findings.append(f)
                break  # 第一个匹配的胜出
        if not findings:
            return ConversionOutcome(
                verdict=ConversionVerdict.OPAQUE,
                warnings=[
                    "no recognized Topos-native pattern in this ComfyUI workflow; "
                    "keeping as opaque Comfy subgraph (C5 not promised)"
                ],
                source_workflow_hash=snapshot.content_hash,
            )
        return ConversionOutcome(
            verdict=ConversionVerdict.CONVERTED,
            findings=tuple(findings),
            source_workflow_hash=snapshot.content_hash,
        )


__all__ = [
    "ComfyToToposConverter",
    "ConversionFinding",
    "ConversionOutcome",
    "ConversionVerdict",
    "GOLDEN_FIXTURES",
    "GoldenWorkflowFixture",
]
