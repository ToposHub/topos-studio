"""
ROI (Region of Interest) Tests - Validate ROI computation and propagation.

Tests cover:
1. Node ROI interface methods (compute_output_bbox, compute_input_roi)
2. Specialized node ROI calculations (Blur, Transform, Crop, Merge, Reformat)
3. ROI propagation in RenderScheduler
4. Cache key ROI inclusion

These are pure logic tests - no GPU required.
"""

import pytest
from typing import Dict, Optional

from src.features.composite.core.format import BBox, ImageFormat
from src.features.composite.nodes.base_node import Node, Port, TextureResult
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.crop_node import CropNode
from src.features.composite.nodes.reformat_node import ReformatNode
from src.features.composite.nodes.blend_node import MergeNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.invert_node import InvertNode


# ── BBox Helper Tests ──

def test_bbox_expansion():
    """Test BBox.expanded() method."""
    bbox = BBox(x=100, y=100, width=200, height=200)
    expanded = bbox.expanded(10)

    assert expanded.x == 90
    assert expanded.y == 90
    assert expanded.width == 220
    assert expanded.height == 220


def test_bbox_intersection():
    """Test BBox intersection calculation."""
    bbox1 = BBox(x=0, y=0, width=100, height=100)
    bbox2 = BBox(x=50, y=50, width=100, height=100)

    intersection = bbox1.intersection(bbox2)
    assert intersection.x == 50
    assert intersection.y == 50
    assert intersection.width == 50
    assert intersection.height == 50


def test_bbox_union():
    """Test BBox union calculation."""
    bbox1 = BBox(x=0, y=0, width=100, height=100)
    bbox2 = BBox(x=50, y=50, width=100, height=100)

    union = bbox1.union(bbox2)
    assert union.x == 0
    assert union.y == 0
    assert union.width == 150
    assert union.height == 150


def test_bbox_from_corners():
    """Test BBox.from_corners() method."""
    corners = [(100.5, 50.2), (200.7, 50.2), (100.5, 150.8), (200.7, 150.8)]
    bbox = BBox.from_corners(corners)

    # Uses floor for min, ceil for max
    assert bbox.x == 100
    assert bbox.y == 50
    assert bbox.width == 101  # 201 - 100
    assert bbox.height == 101  # 151 - 50


def test_bbox_transformed_corners():
    """Test BBox corner transformation."""
    bbox = BBox(x=0, y=0, width=100, height=100)

    # Identity transform (no change)
    matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # m00, m10, m01, m11, m02, m12
    corners = bbox.transformed_corners(matrix)
    assert corners[0] == (0, 0)
    assert corners[1] == (100, 0)
    assert corners[2] == (0, 100)
    assert corners[3] == (100, 100)

    # Translation transform
    matrix = (1.0, 0.0, 0.0, 1.0, 50.0, 25.0)
    corners = bbox.transformed_corners(matrix)
    assert corners[0] == (50, 25)


# ── Node ROI Interface Tests ──

def test_node_default_roi_interface():
    """Test default ROI methods on base node (Grade node as example)."""
    node = GradeNode()
    params = {"lift": 0.0, "gain": 1.0}

    # Default compute_output_bbox: pass through first input bbox
    input_bboxes = {"image": BBox(x=10, y=10, width=100, height=100)}
    output_bbox = node.compute_output_bbox(input_bboxes, params)
    assert output_bbox == input_bboxes["image"]

    # Default compute_input_roi: output ROI = input ROI
    output_roi = BBox(x=20, y=20, width=50, height=50)
    input_rois = node.compute_input_roi(output_roi, params)
    assert input_rois["image"] == output_roi

    # Default requires_roi_expansion: 0
    assert node.requires_roi_expansion(params) == 0


def test_invert_node_roi():
    """Test Invert node ROI (pixel-wise operation, no expansion)."""
    node = InvertNode()
    params = {}

    input_bbox = BBox(x=0, y=0, width=200, height=150)
    input_bboxes = {"image": input_bbox}

    # Output bbox = input bbox
    output_bbox = node.compute_output_bbox(input_bboxes, params)
    assert output_bbox == input_bbox

    # Input ROI = output ROI (no expansion)
    output_roi = BBox(x=50, y=50, width=100, height=50)
    input_rois = node.compute_input_roi(output_roi, params)
    assert input_rois["image"] == output_roi


# ── Blur Node ROI Tests ──

def test_blur_node_output_bbox():
    """Test Blur node output bbox under the fixed-canvas model.

    Blur renders into a texture of the input size without expanding the
    canvas, so the bbox stays aligned with the input bbox (the feather at
    the edges lives inside the canvas as semi-transparent pixels).
    """
    node = BlurNode()
    params = {"radius": 10.0}

    input_bbox = BBox(x=100, y=100, width=200, height=200)
    input_bboxes = {"image": input_bbox}

    output_bbox = node.compute_output_bbox(input_bboxes, params)

    # Fixed canvas: output bbox equals input bbox
    assert output_bbox == input_bbox


def test_blur_node_input_roi():
    """Test Blur node input ROI requires expansion."""
    node = BlurNode()
    params = {"radius": 5.0}

    output_roi = BBox(x=50, y=50, width=100, height=100)
    input_rois = node.compute_input_roi(output_roi, params)

    # Input ROI should be expanded by radius to include blur kernel sampling area
    expected = output_roi.expanded(5)
    assert input_rois["image"] == expected


def test_blur_node_roi_expansion():
    """Test Blur node reports correct expansion margin."""
    node = BlurNode()
    params = {"radius": 15.0}

    expansion = node.requires_roi_expansion(params)
    assert expansion == 15


# ── Transform Node ROI Tests ──

def test_transform_node_identity():
    """Test Transform node identity transform (no change)."""
    node = TransformNode()
    params = {
        "translate_x": 0.0,
        "translate_y": 0.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "filter": "linear",
    }

    input_bbox = BBox(x=0, y=0, width=100, height=100)
    input_bboxes = {"image": input_bbox}

    # Output bbox should be same as input (identity transform)
    output_bbox = node.compute_output_bbox(input_bboxes, params)
    assert output_bbox.x == input_bbox.x
    assert output_bbox.y == input_bbox.y
    assert output_bbox.width == input_bbox.width
    assert output_bbox.height == input_bbox.height


def test_transform_node_translation():
    """Test Transform node translation under the "bbox follows content" model.

    Translation moves the bbox by (tx, ty) in the matrix's Y-down pixel
    space (positive translate_y moves content *down* on screen, matching the
    UI). In BBox's Y-up space that shows up as the y coordinate decreasing.
    So translate (+50, +25) shifts (0,0,100,100) to (50,-25,100,100): right
    edge +50, bottom edge -25 (= 25 px lower on screen).
    """
    node = TransformNode()
    params = {
        "translate_x": 50.0,
        "translate_y": 25.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "filter": "linear",
    }

    input_bbox = BBox(x=0, y=0, width=100, height=100)
    input_bboxes = {"image": input_bbox}

    output_bbox = node.compute_output_bbox(input_bboxes, params)

    # X shifts +50; Y shifts -25 in Y-up space (content moves down on screen).
    assert output_bbox == BBox(x=50, y=-25, width=100, height=100)


def test_transform_node_scale():
    """Test Transform node scale under the "bbox follows content" model.

    Scaling about the image center keeps the center anchored while corners
    move outward symmetrically, so the bbox grows around its center. A
    100x100 bbox at (50,50) scaled by 2 about its own center grows to
    200x200 still anchored at (50,50).
    """
    node = TransformNode()
    params = {
        "translate_x": 0.0,
        "translate_y": 0.0,
        "scale_x": 2.0,
        "scale_y": 2.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "filter": "linear",
    }

    input_bbox = BBox(x=50, y=50, width=100, height=100)
    input_bboxes = {"image": input_bbox}

    output_bbox = node.compute_output_bbox(input_bboxes, params)

    # 2x scale about the bbox center grows it symmetrically to 200x200.
    assert output_bbox == BBox(x=50, y=50, width=200, height=200)


def test_transform_node_input_roi():
    """Test Transform node input ROI under the "bbox follows content" model.

    Since output and input no longer share a coordinate system, the output
    ROI is inverse-transformed back into input space (with the same Y-up/
    Y-down handling as the forward transform). For a pure translation of
    (100, 50) with pivot=center of a 200x200 input, an output ROI of
    (100,50,50,50) maps back to roughly (0,100,50,50), padded by 1px for
    the linear filter.
    """
    node = TransformNode()
    params = {
        "translate_x": 100.0,
        "translate_y": 50.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "filter": "linear",
    }

    output_roi = BBox(x=100, y=50, width=50, height=50)
    input_bboxes = {"image": BBox(x=0, y=0, width=200, height=200)}

    input_rois = node.compute_input_roi(output_roi, params, input_bboxes)

    # Inverse-transformed + 1px filter padding.
    assert input_rois["image"] == BBox(x=-1, y=99, width=52, height=52)


def test_transform_bbox_expansion_reaches_merge_canvas():
    """Regression: a small foreground moved by Transform must be able to
    reach outside its original frame on a larger merge canvas.

    Setup mirrors the reported bug: a 1500x800 foreground is translated so it
    should appear near the right edge of a 1920x1080 merge canvas. Under the
    old fixed-canvas policy the transform's output bbox stayed at
    (0,0,1500,800), capping where merge could place the content. Under the
    bbox-follows-content policy the output bbox extends to cover the moved
    position, so merge's union bbox can reach the full 1920x1080 canvas.
    """
    transform = TransformNode()
    merge = MergeNode()

    # Foreground translated +420,+140 (still inside 1920x1080 horizontally).
    transform_params = {
        "translate_x": 420.0,
        "translate_y": 140.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "filter": "linear",
    }
    fg_in_bbox = BBox(x=0, y=0, width=1500, height=800)
    fg_out_bbox = transform.compute_output_bbox({"image": fg_in_bbox}, transform_params)

    # Output bbox follows the moved content. Right edge reaches 1920 (= the
    # canvas width), so the foreground can be placed at the right side of the
    # merge canvas instead of being clipped to its original 1500px frame.
    assert fg_out_bbox.right == 1920

    # Background is a full 1920x1080 canvas.
    bg_bbox = BBox(x=0, y=0, width=1920, height=1080)

    # Merge with default bbox_policy=union: the union covers the full canvas,
    # so the moved foreground can land anywhere inside it.
    merge_params = {"bbox_policy": "union"}
    merge_out_bbox = merge.compute_output_bbox(
        {"A": fg_out_bbox, "B": bg_bbox}, merge_params
    )
    # Union of fg (420,-140,1500,800) and bg (0,0,1920,1080) → contains bg.
    assert merge_out_bbox == BBox(x=0, y=-140, width=1920, height=1220)


def test_transform_node_roi_expansion():
    """Test Transform node reports filter footprint."""
    node = TransformNode()

    # Linear filter needs 1 pixel extra
    params_linear = {"filter": "linear"}
    assert node.requires_roi_expansion(params_linear) == 1

    # Nearest filter needs 0 pixels extra
    params_nearest = {"filter": "nearest"}
    assert node.requires_roi_expansion(params_nearest) == 0


# ── Crop Node ROI Tests ──

def test_crop_node_output_bbox():
    """Test Crop node output bbox is intersection of crop region and input."""
    node = CropNode()
    params = {
        "x": 50,
        "y": 50,
        "width": 100,
        "height": 100,
        "reformat": True,
    }

    # Input bbox partially overlaps with crop region
    input_bbox = BBox(x=0, y=0, width=200, height=200)
    input_bboxes = {"image": input_bbox}

    output_bbox = node.compute_output_bbox(input_bboxes, params)

    # Output bbox should be intersection of crop region and input bbox
    crop_bbox = BBox(x=50, y=50, width=100, height=100)
    expected = crop_bbox.intersection(input_bbox)
    assert output_bbox.x == expected.x
    assert output_bbox.y == expected.y


def test_crop_node_input_roi():
    """Test Crop node input ROI maps output ROI to input region."""
    node = CropNode()
    params = {
        "x": 50,
        "y": 50,
        "width": 100,
        "height": 100,
        "reformat": True,
    }

    # Output ROI is within crop region
    output_roi = BBox(x=60, y=60, width=50, height=50)
    input_rois = node.compute_input_roi(output_roi, params)

    # Input ROI should be same region (crop just reads that region)
    assert input_rois["image"].x == 60
    assert input_rois["image"].y == 60
    assert input_rois["image"].width == 50
    assert input_rois["image"].height == 50


def test_crop_node_roi_outside_region():
    """Test Crop node ROI when output ROI is outside crop region."""
    node = CropNode()
    params = {
        "x": 0,
        "y": 0,
        "width": 100,
        "height": 100,
        "reformat": True,
    }

    # Output ROI is outside crop region
    output_roi = BBox(x=200, y=200, width=50, height=50)
    input_rois = node.compute_input_roi(output_roi, params)

    # Input ROI should be empty (no intersection)
    assert input_rois["image"].is_empty


# ── Merge Node ROI Tests ──

def test_merge_node_output_bbox_union():
    """Test Merge node output bbox with union policy."""
    node = MergeNode()
    params = {
        "blend_mode": "over",
        "opacity": 1.0,
        "format_policy": "from_b",
        "bbox_policy": "union",
    }

    bbox_a = BBox(x=0, y=0, width=100, height=100)
    bbox_b = BBox(x=50, y=50, width=100, height=100)
    input_bboxes = {"A": bbox_a, "B": bbox_b}

    output_bbox = node.compute_output_bbox(input_bboxes, params)

    # Union bbox should contain both
    expected = bbox_a.union(bbox_b)
    assert output_bbox.x == expected.x
    assert output_bbox.y == expected.y
    assert output_bbox.width == expected.width
    assert output_bbox.height == expected.height


def test_merge_node_output_bbox_intersection():
    """Test Merge node output bbox with intersection policy."""
    node = MergeNode()
    params = {
        "blend_mode": "over",
        "opacity": 1.0,
        "format_policy": "from_b",
        "bbox_policy": "intersection",
    }

    bbox_a = BBox(x=0, y=0, width=100, height=100)
    bbox_b = BBox(x=50, y=50, width=100, height=100)
    input_bboxes = {"A": bbox_a, "B": bbox_b}

    output_bbox = node.compute_output_bbox(input_bboxes, params)

    # Intersection bbox
    expected = bbox_a.intersection(bbox_b)
    assert output_bbox.x == expected.x
    assert output_bbox.y == expected.y


def test_merge_node_input_roi():
    """Test Merge node input ROI for both inputs."""
    node = MergeNode()
    params = {
        "blend_mode": "over",
        "opacity": 1.0,
        "format_policy": "from_b",
        "bbox_policy": "union",
    }

    output_roi = BBox(x=50, y=50, width=100, height=100)
    input_rois = node.compute_input_roi(output_roi, params)

    # Both A and B need the same ROI (per-pixel blend)
    assert input_rois["A"] == output_roi
    assert input_rois["B"] == output_roi
    # Mask also needs same ROI (if present)
    assert input_rois["mask"] == output_roi


# ── Reformat Node ROI Tests ──

def test_reformat_node_output_bbox_fit():
    """Test Reformat node output bbox with fit mode."""
    node = ReformatNode()
    params = {
        "format_mode": "preset",
        "preset": "HD 1080",  # 1920x1080
        "resize_mode": "fit",
        "filter": "linear",
    }

    input_bbox = BBox(x=0, y=0, width=1000, height=500)
    input_bboxes = {"image": input_bbox}

    output_bbox = node.compute_output_bbox(input_bboxes, params)

    # Fit mode: bbox scales to fit within target
    # Scale factor = min(1920/1000, 1080/500) = min(1.92, 2.16) = 1.92
    assert output_bbox is not None


def test_reformat_node_input_roi():
    """Test Reformat node input ROI with scaling."""
    node = ReformatNode()
    params = {
        "format_mode": "custom",
        "width": 2000,
        "height": 2000,
        "resize_mode": "fit",
        "filter": "linear",
    }

    # Output ROI at target resolution
    output_roi = BBox(x=0, y=0, width=1000, height=1000)
    input_bboxes = {"image": BBox(x=0, y=0, width=1000, height=1000)}

    input_rois = node.compute_input_roi(output_roi, params, input_bboxes)

    # Input ROI should be inverse scaled
    # If output is scaled up, input ROI is smaller
    assert input_rois["image"].width > 0


def test_reformat_node_roi_expansion():
    """Test Reformat node reports filter footprint."""
    node = ReformatNode()

    params_linear = {"filter": "linear"}
    assert node.requires_roi_expansion(params_linear) == 1

    params_nearest = {"filter": "nearest"}
    assert node.requires_roi_expansion(params_nearest) == 0


# ── Cache Key ROI Tests ──

def test_cache_key_includes_roi():
    """Test that cache key includes ROI information."""
    from src.features.composite.render.cache_manager import CacheManager
    from src.features.composite.core.graph_snapshot import NodeSnapshot

    # Create mock objects
    class MockResourcePool:
        pass

    cache_mgr = CacheManager(MockResourcePool(), max_entries=10)

    # Create node snapshot
    ns = NodeSnapshot(
        node_id="test_node",
        node_type="blur",
        parameters={"radius": 5.0},
        param_revision=1,
        input_ports=[],
        output_ports=[],
        disabled=False,
    )

    inputs = {}  # No inputs for this test

    # Without ROI
    key_no_roi = cache_mgr.compute_key(ns, inputs, render_mode="preview")

    # With ROI
    roi = BBox(x=100, y=100, width=200, height=200)
    key_with_roi = cache_mgr.compute_key(ns, inputs, render_mode="preview", roi=roi)

    # Keys should be different
    assert key_no_roi != key_with_roi
    assert "roi100,100,200,200" in key_with_roi


def test_different_rois_different_keys():
    """Test that different ROIs produce different cache keys."""
    from src.features.composite.render.cache_manager import CacheManager
    from src.features.composite.core.graph_snapshot import NodeSnapshot

    class MockResourcePool:
        pass

    cache_mgr = CacheManager(MockResourcePool(), max_entries=10)

    ns = NodeSnapshot(
        node_id="test_node",
        node_type="blur",
        parameters={"radius": 5.0},
        param_revision=1,
        input_ports=[],
        output_ports=[],
        disabled=False,
    )

    inputs = {}

    roi1 = BBox(x=0, y=0, width=100, height=100)
    roi2 = BBox(x=50, y=50, width=100, height=100)

    key1 = cache_mgr.compute_key(ns, inputs, render_mode="preview", roi=roi1)
    key2 = cache_mgr.compute_key(ns, inputs, render_mode="preview", roi=roi2)

    # Different ROIs should produce different keys
    assert key1 != key2


# ── RenderScheduler ROI Tests ──

def test_scheduler_plan_without_roi():
    """Test RenderScheduler.plan() without ROI."""
    from src.features.composite.render.render_scheduler import RenderScheduler
    from src.features.composite.core.graph_snapshot import GraphSnapshot

    scheduler = RenderScheduler()

    # Create minimal snapshot
    snapshot = GraphSnapshot(
        generation=1,
        nodes={},
        connections=[],
        eval_order=[],
        topology_revision=1,
        color_revision=1,
        color_texture_id=0,
        color_width=1920,
        color_height=1080,
    )

    plan = scheduler.plan(snapshot, render_mode="preview")

    # Plan should not have ROI enabled
    assert plan.roi_enabled == False
    assert plan.viewer_roi is None


def test_scheduler_plan_with_roi():
    """Test RenderScheduler.plan() with ROI for preview mode."""
    from src.features.composite.render.render_scheduler import RenderScheduler, RenderMode
    from src.features.composite.core.graph_snapshot import GraphSnapshot

    scheduler = RenderScheduler()

    snapshot = GraphSnapshot(
        generation=1,
        nodes={},
        connections=[],
        eval_order=[],
        topology_revision=1,
        color_revision=1,
        color_texture_id=0,
        color_width=1920,
        color_height=1080,
    )

    viewer_roi = BBox(x=100, y=100, width=500, height=500)
    plan = scheduler.plan(snapshot, render_mode=RenderMode.PREVIEW, viewer_roi=viewer_roi)

    # Plan should have ROI enabled for preview mode
    assert plan.roi_enabled == True
    assert plan.viewer_roi == viewer_roi


def test_scheduler_roi_disabled_for_export():
    """Test that ROI is disabled for export mode."""
    from src.features.composite.render.render_scheduler import RenderScheduler, RenderMode
    from src.features.composite.core.graph_snapshot import GraphSnapshot

    scheduler = RenderScheduler()

    snapshot = GraphSnapshot(
        generation=1,
        nodes={},
        connections=[],
        eval_order=[],
        topology_revision=1,
        color_revision=1,
        color_texture_id=0,
        color_width=1920,
        color_height=1080,
    )

    viewer_roi = BBox(x=100, y=100, width=500, height=500)
    plan = scheduler.plan(snapshot, render_mode=RenderMode.EXPORT, viewer_roi=viewer_roi)

    # ROI should be disabled for export mode
    assert plan.roi_enabled == False


# ── ROI Propagation Tests ──

def test_roi_propagation_single_node():
    """Test ROI propagation through a simple chain."""
    from src.features.composite.render.render_scheduler import RenderScheduler, RenderMode, NodePlan
    from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot

    scheduler = RenderScheduler()

    # Create a simple node chain: Output -> Blur
    output_ns = NodeSnapshot(
        node_id="output1",
        node_type="output",
        parameters={"resolution_mode": "auto"},
        param_revision=1,
        input_ports=[("image", "image", "image")],
        output_ports=[("display", "output", "image")],
        disabled=False,
    )

    blur_ns = NodeSnapshot(
        node_id="blur1",
        node_type="blur",
        parameters={"radius": 10.0},
        param_revision=1,
        input_ports=[("image", "input", "image")],
        output_ports=[("result", "output", "image")],
        disabled=False,
    )

    snapshot = GraphSnapshot(
        generation=1,
        nodes={"output1": output_ns, "blur1": blur_ns},
        connections=[],  # Empty for simplicity
        eval_order=["blur1", "output1"],
        topology_revision=1,
        color_revision=1,
        color_texture_id=0,
        color_width=1920,
        color_height=1080,
    )

    viewer_roi = BBox(x=500, y=500, width=200, height=200)
    plan = scheduler.plan(snapshot, render_mode=RenderMode.PREVIEW, viewer_roi=viewer_roi)

    # Note: Without connections, ROI propagation won't work fully,
    # but the plan structure should be correct
    assert plan.roi_enabled == True
    assert plan.viewer_roi == viewer_roi


# ── Integration Tests ──

def test_full_roi_chain():
    """Test ROI propagation through Blur -> Grade -> Output chain."""
    # This tests the expected behavior of ROI propagation:
    # 1. Output needs viewer_roi
    # 2. Grade needs same ROI (pixel-wise)
    # 3. Blur needs ROI expanded by radius

    # Setup
    blur = BlurNode()
    blur_params = {"radius": 5.0}

    grade = GradeNode()
    grade_params = {"lift": 0.0, "gain": 1.0}

    # ROI chain calculation (reverse)
    viewer_roi = BBox(x=100, y=100, width=200, height=200)

    # Grade: input_roi = output_roi (no expansion)
    grade_input_rois = grade.compute_input_roi(viewer_roi, grade_params)
    grade_roi = grade_input_rois["image"]

    # Blur: input_roi = output_roi expanded by radius
    blur_input_rois = blur.compute_input_roi(grade_roi, blur_params)
    blur_roi = blur_input_rois["image"]

    # Expected: blur_roi should be viewer_roi expanded by blur radius
    expected = viewer_roi.expanded(5)
    assert blur_roi.x == expected.x
    assert blur_roi.y == expected.y
    assert blur_roi.width == expected.width
    assert blur_roi.height == expected.height


# ── ROI Expansion Tests ──

def test_roi_expansion_empty_params():
    """测试空参数时 ROI 不应扩展。

    验证节点在默认参数或空参数时，_tile_padding() 返回 0。
    """
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.nodes.grade_node import GradeNode
    from src.features.composite.nodes.invert_node import InvertNode
    from src.features.composite.nodes.transform_node import TransformNode
    from src.features.composite.nodes.erode_dilate_node import ErodeDilateNode

    # Grade 节点：像素级操作，不需要 ROI 扩展
    grade = GradeNode()
    empty_params = {}  # 空参数
    default_params = {
        "lift_r": 0.0, "lift_g": 0.0, "lift_b": 0.0,
        "gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
        "gain_r": 1.0, "gain_g": 1.0, "gain_b": 1.0,
    }

    # Grade 没有 _tile_padding 方法，requires_roi_expansion 返回 0
    assert grade.requires_roi_expansion(empty_params) == 0, "Grade with empty params should have 0 expansion"
    assert grade.requires_roi_expansion(default_params) == 0, "Grade with default params should have 0 expansion"

    # Invert 节点：像素级操作，不需要 ROI 扩展
    invert = InvertNode()
    assert invert.requires_roi_expansion({}) == 0, "Invert should have 0 expansion"

    # Blur 节点：radius=0 时不需要扩展
    blur = BlurNode()
    zero_radius_params = {"radius": 0.0}
    assert blur.requires_roi_expansion(zero_radius_params) == 0, "Blur with radius=0 should have 0 expansion"
    assert blur._tile_padding(zero_radius_params) == 0, "Blur _tile_padding with radius=0 should return 0"

    # Transform 节点：nearest 滤镜不需要扩展
    transform = TransformNode()
    nearest_params = {"filter": "nearest"}
    assert transform.requires_roi_expansion(nearest_params) == 0, "Transform nearest filter should have 0 expansion"
    assert transform._tile_padding(nearest_params) == 0, "Transform _tile_padding nearest should return 0"

    # ErodeDilate 节点：size=1 时最小扩展
    erode_dilate = ErodeDilateNode()
    size_1_params = {"size": 1}
    # size // 2 + 1 = 1 // 2 + 1 = 0 + 1 = 1
    assert erode_dilate._tile_padding(size_1_params) == 1, "ErodeDilate size=1 should have padding=1"


def test_roi_expansion_specific_cases():
    """测试特定节点参数的 ROI 扩展验证。

    验证不同节点类型的 ROI 扩展逻辑：
    - blur_node: radius 参数 -> padding 应为 radius
    - erode_dilate_node: size 参数 -> padding 应为 size//2 + 1
    - transform_node: filter 参数 -> linear=1, nearest=0
    - 验证 TilePaddingComputer 正确计算链式 padding
    """
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.nodes.transform_node import TransformNode
    from src.features.composite.nodes.erode_dilate_node import ErodeDilateNode
    from src.features.composite.core.tile_padding_computer import TilePaddingComputer
    from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot, ConnectionSnapshot

    # Blur 节点：验证 radius -> padding 映射
    blur = BlurNode()

    # 测试不同 radius 值
    test_radii = [5.0, 10.0, 25.0, 50.0, 100.0]
    for radius in test_radii:
        params = {"radius": radius}
        expected_padding = int(radius)
        actual_padding = blur._tile_padding(params)
        assert actual_padding == expected_padding, \
            f"Blur radius={radius} should have padding={expected_padding}, got {actual_padding}"

    # 测试 radius_x/radius_y（取最大值）
    params_xy = {"radius_x": 10.0, "radius_y": 20.0}
    assert blur._tile_padding(params_xy) == 20, "Blur should use max(radius_x, radius_y)"

    params_xy2 = {"radius_x": 30.0, "radius_y": 5.0}
    assert blur._tile_padding(params_xy2) == 30, "Blur should use max(radius_x, radius_y)"

    # ErodeDilate 节点：验证 size -> padding 映射
    erode_dilate = ErodeDilateNode()

    # 测试不同 size 值：padding = size // 2 + 1
    test_sizes = [1, 3, 5, 7, 10, 20, 50]
    for size in test_sizes:
        params = {"size": size}
        expected_padding = size // 2 + 1
        actual_padding = erode_dilate._tile_padding(params)
        assert actual_padding == expected_padding, \
            f"ErodeDilate size={size} should have padding={expected_padding}, got {actual_padding}"

    # Transform 节点：验证 filter -> padding 映射
    transform = TransformNode()

    # Linear filter -> padding = 1
    linear_params = {"filter": "linear"}
    assert transform._tile_padding(linear_params) == 1, "Transform linear filter should have padding=1"

    # Nearest filter -> padding = 0
    nearest_params = {"filter": "nearest"}
    assert transform._tile_padding(nearest_params) == 0, "Transform nearest filter should have padding=0"

    # 验证 requires_roi_expansion 方法一致性
    assert transform.requires_roi_expansion(linear_params) == 1, "requires_roi_expansion should match _tile_padding"
    assert transform.requires_roi_expansion(nearest_params) == 0, "requires_roi_expansion should match _tile_padding"

    # 验证 TilePaddingComputer 链式计算
    # 创建测试图：color_input -> blur(radius=10) -> erode_dilate(size=5) -> output
    color_ns = NodeSnapshot(
        node_id="color1",
        node_type="color_input",
        parameters={},
        param_revision=1,
        input_ports=[],
        output_ports=[("result", "image")],
        disabled=False,
    )

    blur_ns = NodeSnapshot(
        node_id="blur1",
        node_type="blur",
        parameters={"radius": 10.0},
        param_revision=1,
        input_ports=[("image", "image")],
        output_ports=[("result", "image")],
        disabled=False,
    )

    erode_ns = NodeSnapshot(
        node_id="erode1",
        node_type="erode_dilate",
        parameters={"size": 5},
        param_revision=1,
        input_ports=[("image", "image")],
        output_ports=[("image", "image")],
        disabled=False,
    )

    output_ns = NodeSnapshot(
        node_id="output1",
        node_type="output",
        parameters={},
        param_revision=1,
        input_ports=[("image", "image")],
        output_ports=[],
        disabled=False,
    )

    connections = [
        ConnectionSnapshot("color1", "result", "blur1", "image"),
        ConnectionSnapshot("blur1", "result", "erode1", "image"),
        ConnectionSnapshot("erode1", "image", "output1", "image"),
    ]

    snapshot = GraphSnapshot(
        generation=1,
        nodes={"color1": color_ns, "blur1": blur_ns, "erode1": erode_ns, "output1": output_ns},
        connections=connections,
        eval_order=["color1", "blur1", "erode1", "output1"],
        color_texture_id=0,
        color_width=1920,
        color_height=1080,
        color_revision=0,
    )

    padding_info = TilePaddingComputer.compute(snapshot)

    # 预期总 padding：blur(10) + erode_dilate(5//2+1=3) = 13
    # 走图反向计算：source 需要所有下游节点的 padding 累加
    expected_total = 10 + 3  # blur(10) + erode_dilate(3)
    assert padding_info.total_padding == expected_total, \
        f"Chain padding should be {expected_total}, got {padding_info.total_padding}"

    # 验证单个节点 padding
    assert padding_info.per_node_padding.get("blur1", 0) >= 10, "Blur1 should have at least 10 padding"
    assert padding_info.per_node_padding.get("erode1", 0) >= 3, "Erode1 should have at least 3 padding"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])