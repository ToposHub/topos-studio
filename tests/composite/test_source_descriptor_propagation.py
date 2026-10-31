"""Tests for source-color-metadata propagation into the composite IDT.

The IDT hook (``BaseEvaluatorMixin._apply_idt_to_source_outputs``) reads
``color_primaries`` from a source texture's ``frame_metadata`` to pick the
gamut for the decode. ``TimelineClipFrameProvider`` writes that metadata as a
plain dict ``{"color_primaries": ...}``, but the IDT previously used
``getattr(meta, "color_primaries")`` — which is always ``None`` for a dict —
so BT.2020 SDR / Display P3 SDR sources silently fell back to BT.709 primaries
in the decode. This is the P1 "dict vs getattr" regression guard.

The helper ``_metadata_get`` (module-level in base_evaluator.py) reads either
a dict or an attribute-bearing object, fixing the silent primaries drop.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.evaluators.base_evaluator import (
    BaseEvaluatorMixin, _metadata_get,
)
from src.features.composite.nodes.base_node import TextureResult

pytestmark = pytest.mark.composite_core


class _IDTCapturingEvaluator(BaseEvaluatorMixin):
    """Minimal evaluator stand-in that records the primaries handed to _convert_to_linear.

    _apply_idt_to_source_outputs calls self._convert_to_linear(value,
    transfer=..., primaries=...). We override it to capture the call instead
    of running the real GPU shader (no GL context in unit tests).
    """

    def __init__(self):
        self.captured = []  # list of (transfer, primaries)

    def _convert_to_linear(self, tex, transfer="srgb", primaries="bt709", **kw):
        self.captured.append((transfer, primaries))
        # Return the input unchanged; the test only inspects the capture.
        return tex


def _tex(color_space: str, frame_metadata=None) -> TextureResult:
    """A valid source texture tagged with the given space + metadata."""
    return TextureResult(
        texture_id=1, width=2, height=2,
        color_space=color_space,
        frame_metadata=frame_metadata,
    )


class TestMetadataGetHelper:
    """_metadata_get must read both dict and object metadata shapes."""

    def test_reads_dict(self):
        assert _metadata_get({"color_primaries": "bt2020"}, "color_primaries") == "bt2020"

    def test_reads_object_attribute(self):
        meta = SimpleNamespace(color_primaries="smpte432")
        assert _metadata_get(meta, "color_primaries") == "smpte432"

    def test_none_meta_returns_default(self):
        assert _metadata_get(None, "color_primaries") is None
        assert _metadata_get(None, "color_primaries", "fallback") == "fallback"

    def test_missing_key_returns_default(self):
        assert _metadata_get({}, "color_primaries") is None
        assert _metadata_get({"other": 1}, "color_primaries", "bt709") == "bt709"
        assert _metadata_get(SimpleNamespace(), "color_primaries", "bt709") == "bt709"


class TestIDTPrimariesFromDictMetadata:
    """The IDT must honour color_primaries stored in a dict frame_metadata.

    Regression: TimelineClipFrameProvider writes frame_metadata as a dict, but
    the IDT used getattr() — so BT.2020/P3 SDR sources decoded with the wrong
    (BT.709) gamut.
    """

    def test_bt2020_sdr_dict_metadata_picks_bt2020_primaries(self):
        """BT.2020-gamut Rec.709-transfer video must decode in BT.2020 gamut.

        This is the exact P1 scenario: transfer is bt709 (SDR), so the HDR
        branch (which forces bt2020) does NOT fire — the primaries must come
        from frame_metadata. Without _metadata_get this returned bt709.
        """
        evaluator = _IDTCapturingEvaluator()
        tex = _tex("bt709", frame_metadata={"color_primaries": "bt2020"})

        evaluator._apply_idt_to_source_outputs({"result": tex})

        assert evaluator.captured, "IDT did not invoke _convert_to_linear"
        transfer, primaries = evaluator.captured[-1]
        assert transfer == "bt709"
        assert primaries == "bt2020", (
            "BT.2020 SDR source must decode with bt2020 primaries; the dict-"
            "metadata getattr bug silently dropped it to bt709."
        )

    def test_display_p3_sdr_dict_metadata_picks_smpte432(self):
        """Display P3 + sRGB transfer must decode with smpte432 primaries."""
        evaluator = _IDTCapturingEvaluator()
        tex = _tex("srgb", frame_metadata={"color_primaries": "smpte432"})

        evaluator._apply_idt_to_source_outputs({"result": tex})

        transfer, primaries = evaluator.captured[-1]
        assert transfer == "srgb"
        assert primaries == "smpte432"

    def test_object_style_metadata_still_works(self):
        """Backward compat: attribute-bearing metadata (legacy source nodes)
        must still propagate primaries after the _metadata_get switch."""
        evaluator = _IDTCapturingEvaluator()
        meta = SimpleNamespace(color_primaries="bt2020")
        tex = _tex("bt709", frame_metadata=meta)

        evaluator._apply_idt_to_source_outputs({"result": tex})

        _, primaries = evaluator.captured[-1]
        assert primaries == "bt2020"

    def test_no_metadata_falls_back_to_bt709(self):
        """No frame_metadata at all keeps the SDR default (BT.709)."""
        evaluator = _IDTCapturingEvaluator()
        tex = _tex("bt709", frame_metadata=None)

        evaluator._apply_idt_to_source_outputs({"result": tex})

        _, primaries = evaluator.captured[-1]
        assert primaries == "bt709"

    def test_bt709_primaries_in_dict_stays_bt709(self):
        """Explicit bt709 primaries in metadata → bt709 (no spurious upgrade)."""
        evaluator = _IDTCapturingEvaluator()
        tex = _tex("bt709", frame_metadata={"color_primaries": "bt709"})

        evaluator._apply_idt_to_source_outputs({"result": tex})

        _, primaries = evaluator.captured[-1]
        assert primaries == "bt709"
