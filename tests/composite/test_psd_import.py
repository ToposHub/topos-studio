"""B07: PSD/PSB transparent decode, manifest and layered import planning.

Hand-crafted PSD fixtures (see ``psd_fixture.py``) exercise real decode:
half-transparent 8×8 patches, negative offsets, fully-outside layers and
duplicate display names.  Import planning keeps only fidelity-safe plain
pixel layers and records merge reasons; canvas clipping matches the plan's
hand-computed patch example.
"""

import io
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pytest

psd_tools = pytest.importorskip("psd_tools")

from psd_fixture import sample_layer_document

from src.features.composite.core.psd_import import (
    SUPPORT_MATRIX, clip_psd_patch, import_cache_key, plan_psd_import,
    probe_psd_manifest, read_psd_layer_rgba,
)
from src.features.composite.core.psd_import import (
    PsdDocumentManifest, PsdLayerEntry,
)

pytestmark = pytest.mark.composite_core


@pytest.fixture
def document():
    return sample_layer_document()


@pytest.fixture
def manifest(document, tmp_path):
    path = tmp_path / "sample.psd"
    path.write_bytes(document)
    return probe_psd_manifest(str(path), "asset-psd", "rev-1")


# ── Canvas patch clipping (plan example, hand-computed) ─────────────────────


def test_clip_psd_patch_hand_computed():
    canvas = (16, 16)
    # Fully inside: identity placement.
    assert clip_psd_patch((4, 4, 12, 12), canvas) == (
        (4, 4), (slice(0, 8), slice(0, 8)))
    # Negative offset: visible part is the 4×4 bottom-right of the patch.
    assert clip_psd_patch((-4, -4, 4, 4), canvas) == (
        (0, 0), (slice(4, 8), slice(4, 8)))
    # Fully outside on either axis → None.
    assert clip_psd_patch((-32, -32, -28, -28), canvas) is None
    assert clip_psd_patch((16, 0, 20, 8), canvas) is None
    # Overhang past the right/bottom edge.
    assert clip_psd_patch((12, 12, 20, 20), canvas) == (
        (12, 12), (slice(0, 4), slice(0, 4)))
    # Zero-area patch → None.
    assert clip_psd_patch((4, 4, 4, 8), canvas) is None


# ── Manifest probing (real decode) ──────────────────────────────────────────


def test_manifest_probes_order_offsets_and_alpha(manifest):
    assert (manifest.width, manifest.height) == (16, 16)
    assert manifest.bit_depth == 8
    names = [entry.name for entry in manifest.layers]
    assert names == ["Back", "Half", "NegOff", "Outside", "Dup", "Dup"]
    by_name = {entry.name: entry for entry in manifest.layers}
    assert by_name["Back"].bbox == (0, 0, 16, 16)
    assert by_name["Half"].bbox == (4, 4, 12, 12)
    assert by_name["NegOff"].bbox == (-4, -4, 4, 4)
    assert by_name["Outside"].bbox == (-32, -32, -28, -28)


def test_half_transparent_patch_pixels_are_exact(manifest, tmp_path):
    path = tmp_path / "sample.psd"
    path.write_bytes(sample_layer_document())
    half = next(e for e in manifest.layers if e.name == "Half")
    pixels = read_psd_layer_rgba(str(path), half)
    assert pixels.shape == (8, 8, 4)
    # Hand-computed: uniform 50 % alpha green, straight (not premultiplied).
    assert pixels[0, 0, 0] == pytest.approx(0.0)
    assert pixels[4, 4, 1] == pytest.approx(1.0)
    assert pixels[4, 4, 3] == pytest.approx(128 / 255, abs=1 / 255)


def test_duplicate_names_resolve_without_guessing(manifest):
    duplicates = manifest.entries_named("Dup")
    assert len(duplicates) == 2
    selectors = {entry.selector for entry in duplicates}
    assert len(selectors) == 2  # distinct fingerprints, same display name
    assert duplicates[0].fingerprint != duplicates[1].fingerprint


def test_layer_ids_fall_back_to_structure_fingerprints(manifest):
    # The hand-crafted fixture carries no lyid records; every entry must
    # still have a stable identity (never the display name).
    for entry in manifest.layers:
        assert entry.selector[1]
        assert entry.selector[1] != entry.name


# ── Import planning policy ──────────────────────────────────────────────────


def _entry(**overrides):
    base = dict(layer_id=7, name="L", structure_path=(),
                bbox=(0, 0, 8, 8), visible=True, opacity=255,
                blend_mode="normal")
    base.update(overrides)
    return PsdLayerEntry(**base)


def test_plan_imports_only_plain_pixel_layers():
    manifest = PsdDocumentManifest(
        asset_id="a", revision="r", width=16, height=16,
        color_mode="RGB", bit_depth=8, has_merged_image=True,
        layers=(
            _entry(name="pixel"),
            _entry(name="group", is_group=True, group_passthrough=True),
            _entry(name="adjust", adjustment=True),
            _entry(name="smart", smart_object=True),
            _entry(name="clipped", clipped=True),
            _entry(name="styled", has_effects=True),
        ))
    planned = plan_psd_import(manifest)
    decisions = {p.entry.name: (p.importable, p.merge_reason)
                 for p in planned}
    assert decisions["pixel"] == (True, "")
    assert decisions["group"] == (False, "group_passthrough")
    assert decisions["adjust"] == (False, "adjustment")
    assert decisions["smart"] == (False, "smart_object")
    assert decisions["clipped"] == (False, "clipping")
    assert decisions["styled"] == (False, "effects")


def test_support_matrix_documents_capability():
    assert SUPPORT_MATRIX["psd"] == "supported"
    assert SUPPORT_MATRIX["psb"] == "supported"
    assert SUPPORT_MATRIX["32bit"] == "unsupported"
    assert SUPPORT_MATRIX["layer_styles"] == "merged"


def test_import_cache_key_separates_revision_selector_workspace_precision():
    base = import_cache_key("rev1", ("id", "7"), "ws", "f32")
    assert base != import_cache_key("rev2", ("id", "7"), "ws", "f32")
    assert base != import_cache_key("rev1", ("fp", "abc"), "ws", "f32")
    assert base != import_cache_key("rev1", ("id", "7"), "ws2", "f32")
    assert base != import_cache_key("rev1", ("id", "7"), "ws", "f16")


def test_real_document_plans_all_plain_layers_importable(manifest):
    planned = plan_psd_import(manifest)
    assert all(p.importable for p in planned)
    assert all(not p.merge_reason for p in planned)
    # Canvas offsets come from the PSD bbox (negative offsets preserved).
    neg = next(p for p in planned if p.entry.name == "NegOff")
    assert neg.canvas_offset == (-4, -4)
