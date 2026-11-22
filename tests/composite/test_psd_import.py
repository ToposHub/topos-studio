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

from psd_fixture import build_psd, sample_layer_document

from src.features.composite.core.psd_import import (
    SUPPORT_MATRIX, clip_psd_patch, import_cache_key, plan_psd_import,
    probe_psd_manifest, read_psd_document_rgba, read_psd_layer_rgba,
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
    assert manifest.color_mode == "RGB"
    names = [entry.name for entry in manifest.layers]
    assert names == ["Dup", "Dup", "Outside", "NegOff", "Half", "Back"]
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


def test_ambiguous_same_name_and_bounds_are_not_relinked_to_first_match(tmp_path):
    path = tmp_path / "ambiguous.psd"
    layer = dict(name="Same", left=0, top=0, right=2, bottom=2, opacity=0)
    path.write_bytes(build_psd(2, 2, [
        {**layer, "fill": (255, 0, 0, 255)}, {**layer, "fill": (0, 0, 255, 255)},
    ]))
    first, second = probe_psd_manifest(str(path), "asset", "rev").layers
    assert first.opacity == second.opacity == 0
    assert first.selector == second.selector
    with pytest.raises(LookupError, match="ambiguous"):
        read_psd_layer_rgba(str(path), first)


@pytest.mark.parametrize("version", [1, 2], ids=["PSD", "PSB"])
def test_native_16bit_rgb_alpha_and_missing_preview_are_really_read(tmp_path, version):
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import Compression, Resource
    from psd_tools.psd.image_resources import ImageResource, VersionInfo

    path = tmp_path / ("native.psd" if version == 1 else "native.psb")
    doc = psd_tools.PSDImage.new("RGB", (2, 2), depth=16)
    doc._record.header.version = version
    pixels = np.array([[[1000, 2000, 3000, 33333]]], dtype=np.uint16)
    PixelLayer.fromarray(pixels, doc, "Native precision", compression=Compression.RAW)
    doc.image_resources[Resource.VERSION_INFO] = ImageResource(
        key=Resource.VERSION_INFO, data=VersionInfo(has_composite=False))
    doc.save(path)
    manifest = probe_psd_manifest(str(path), "asset", "rev")
    assert manifest.bit_depth == 16
    assert manifest.color_mode == "RGB"
    assert not manifest.has_merged_image
    np.testing.assert_allclose(
        read_psd_layer_rgba(str(path), manifest.layers[0]),
        pixels.astype(np.float32) / 65535.0,
    )
    flat, depth = read_psd_document_rgba(str(path))
    assert depth == 16
    assert flat.shape == (2, 2, 4)
    np.testing.assert_allclose(flat[0, 0], pixels[0, 0].astype(np.float32) / 65535.0,
                               atol=6 / 65535)
    assert flat[1, 1, 3] == 0.0


def test_flatten_uses_native_merged_rgba_preview(tmp_path):
    path = tmp_path / "merged.psd"
    doc = psd_tools.PSDImage.new("RGBA", (1, 1), color=(1.0, 0.5, 0.5, 0.5), depth=16)
    doc.save(path)
    flat, depth = read_psd_document_rgba(str(path))
    assert depth == 16
    np.testing.assert_allclose(flat[0, 0], [1.0, 0.0, 0.0, 32768 / 65535.0],
                               atol=3.0 / 65535)


def test_flatten_transforms_rgb_icc_without_quantizing_16bit_alpha(tmp_path):
    from PIL import ImageCms
    from psd_tools.constants import Resource
    from psd_tools.psd.image_resources import ImageResource

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    for depth in (8, 16):
        doc = psd_tools.PSDImage.new("RGBA", (1, 1), color=(1.0, 0.5, 0.5, 0.5), depth=depth)
        doc.image_resources[Resource.ICC_PROFILE] = ImageResource(key=Resource.ICC_PROFILE, data=profile)
        path = tmp_path / f"color-{depth}.psd"
        doc.save(path)
        try:
            result, actual_depth = read_psd_document_rgba(str(path))
        except RuntimeError as exc:
            pytest.skip(str(exc))
        assert actual_depth == depth
        assert result[0, 0, 0] == pytest.approx(1.0, abs=1 / 255)
        assert result[0, 0, 3] == pytest.approx((1 << (depth - 1)) / ((1 << depth) - 1))


def test_group_offset_and_fidelity_plan_keep_group_contents_together(tmp_path):
    from psd_tools.api.layers import Group, PixelLayer
    from psd_tools.constants import BlendMode

    path = tmp_path / "group.psd"
    doc = psd_tools.PSDImage.new("RGB", (8, 8))
    group = Group.new(doc, name="Named Group")
    group.blend_mode = BlendMode.PASS_THROUGH
    group.opacity = 80
    PixelLayer.fromarray(np.array([[[255, 0, 0, 128]]], dtype=np.uint8),
                         group, name="Pixel child", left=3, top=4)
    doc.save(path)
    manifest = probe_psd_manifest(str(path), "asset", "rev")
    group, child = manifest.layers
    assert group.bbox == child.bbox == (3, 4, 4, 5)
    assert group.group_passthrough
    assert group.opacity == 80
    assert [entry.merge_reason for entry in plan_psd_import(manifest)] == [
        "group_passthrough", "group_contents",
    ]
    np.testing.assert_allclose(
        read_psd_layer_rgba(str(path), group), [[[1.0, 0.0, 0.0, 128 / 255]]])


def test_clip_base_stays_with_the_clip_and_preserves_the_top_layer(tmp_path):
    from psd_tools.api.layers import PixelLayer

    path = tmp_path / "clipped.psd"
    doc = psd_tools.PSDImage.new("RGB", (8, 8))
    patch = np.full((2, 2, 4), 255, dtype=np.uint8)
    PixelLayer.fromarray(patch, doc, name="Back untouched")
    PixelLayer.fromarray(patch, doc, name="Base")
    clip = PixelLayer.fromarray(patch, doc, name="Clip")
    clip.clipping = True
    PixelLayer.fromarray(patch, doc, name="Top untouched")
    doc.save(path)
    manifest = probe_psd_manifest(str(path), "asset", "rev")
    assert [(item.entry.name, item.merge_reason) for item in plan_psd_import(manifest)] == [
        ("Top untouched", ""), ("Clip", "clipping"), ("Base", "clipping_base"),
        ("Back untouched", ""),
    ]
    from src.features.composite.core.psd_import import plan_psd_import_units, read_psd_canvas_layer_rgba

    units = plan_psd_import_units(manifest, plan_psd_import(manifest))
    assert [[item.entry.name for item in unit] for unit in units] == [
        ["Top untouched"], ["Clip", "Base"], ["Back untouched"],
    ]
    merged, depth = read_psd_canvas_layer_rgba(str(path), {
        "version": 1, "merged_selectors": [list(item.entry.selector) for item in units[1]],
    })
    assert depth == 8
    np.testing.assert_array_equal(merged[:2, :2], np.ones((2, 2, 4), dtype=np.float32))
    assert not merged[2:, :, 3].any()


# ── Import planning policy ──────────────────────────────────────────────────


def _entry(**overrides):
    base = dict(layer_id=None, name="L", structure_path=(),
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
    assert SUPPORT_MATRIX["layer_styles"] == "editable_rgb_normal_subset"


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
    neg = next(p for p in planned if p.entry.name == "NegOff")
    assert neg.canvas_offset == (-4, -4)


def test_psd_layer_decode_uses_same_selector_after_relink(tmp_path):
    from src.features.composite.core.image_loader import decode_image_file
    from src.features.composite.core.psd_import import make_psd_layer_reference

    path = tmp_path / "layers.psd"
    path.write_bytes(sample_layer_document())
    manifest = probe_psd_manifest(str(path), "psd-asset")
    layer = next(entry for entry in manifest.layers if entry.name == "NegOff")
    reference = make_psd_layer_reference(layer, manifest)
    moved = tmp_path / "moved.psd"
    path.rename(moved)
    decoded = decode_image_file(str(moved), psd_layer=reference)
    assert decoded is not None
    assert decoded.data.shape == (16, 16, 4)
    raw = read_psd_layer_rgba(str(moved), layer)
    np.testing.assert_allclose(decoded.data[:4, :4], raw[4:, 4:])
    assert not decoded.data[4:, :, 3].any()
    assert not decoded.data[:, 4:, 3].any()
    with pytest.raises(LookupError, match="missing"):
        decode_image_file(str(moved), psd_layer={**reference, "selector": ["id", "99"]})


def test_psd_color_overlay_round_trips_into_editable_graph(tmp_path):
    from PIL import Image
    from psd_tools.api.layers import PixelLayer
    from psd_tools.constants import Tag
    from psd_tools.psd.descriptor import Bool, Descriptor, Double
    from psd_tools.terminology import Key, Klass
    from src.features.composite.core.layer_model import BOTTOM_UP_LAYER_STACK_VERSION
    from src.features.composite.core.layer_stack_controller import LayerStackController
    from src.features.composite.core.media_drop_payload import MediaDropPayload
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.output_node import OutputNode

    path = tmp_path / "overlay.psd"
    document = psd_tools.PSDImage.new("RGBA", (5, 5))
    layer = PixelLayer.frompil(Image.new("RGBA", (3, 3), (255, 0, 0, 128)),
                              document, left=1, top=1)
    color = Descriptor(classID=Klass.RGBColor, items={
        Key.Red: Double(0), Key.Green: Double(255), Key.Blue: Double(0),
    })
    overlay = Descriptor(classID=Klass.SolidFill, items={
        b"present": Bool(True), Key.Enabled: Bool(True),
        Key.Color: color, Key.Opacity: Double(50),
    })
    layer.tagged_blocks.set_data(Tag.OBJECT_BASED_EFFECTS_LAYER_INFO, classID=b"null", items={
        b"masterFXSwitch": Bool(True), Klass.SolidFill: overlay,
    })
    document.save(path)
    manifest = probe_psd_manifest(str(path), "psd-asset")
    assert plan_psd_import(manifest)[0].importable
    graph = NodeGraph()
    output = OutputNode("output")
    output.set_parameter("width", 5)
    output.set_parameter("height", 5)
    graph.add_node(output)
    controller = LayerStackController(graph, layer_stack_version=BOTTOM_UP_LAYER_STACK_VERSION)
    assert controller.stage_psd_layers(MediaDropPayload("psd-asset", str(path)), manifest, 0) == 1
    reopened = NodeGraph.from_dict(graph.to_dict())
    style = next(node for node in reopened.nodes.values() if node.node_type == "layer_style")
    assert style.parameters["overlay_opacity"] == 0.5
    assert style.parameters["overlay_g"] == 1.0
    assert style.parameters["psd_srgb"] is True
    assert any(node.node_type == "merge" and node.parameters.get("psd_srgb")
               for node in reopened.nodes.values())
    styles = style.parameters["psd_styles"]
    assert styles[0]["kind"] == "color_overlay" and styles[0]["source_description"]
    style.set_parameter("overlay_opacity", 0.65)
    edited = NodeGraph.from_dict(reopened.to_dict()).get_node(style.id)
    assert edited.parameters["overlay_opacity"] == pytest.approx(0.65)
    assert edited.parameters["psd_styles"] == styles
