"""Which Edit preview frames the Metal composite may render (plan task 2.6).

The plan is pure data: it decides per frame whether *every* layer can be
reproduced on Metal and never touches the task.  A refusal keeps the whole
frame on the GL path; there is no partly Metal frame.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.features.edit.core.clip_params import ClipBlendMode, CropParams
from src.features.edit.core.edit_gpu_preview_renderer import (
    EditRenderTask,
    _transform_to_ndc_mat3_bytes,
)
from src.features.edit.core.metal_edit_compositor import (
    MAX_COMPOSITE_LAYERS,
    MetalCompositePlan,
    MetalLayerPlan,
    PlanRefusal,
    build_metal_composite_plan,
    gl_crop_params,
    inverse_quad_map,
    layer_params,
)
from src.features.edit.core.timeline_render_plan import TransitionBlendInfo
from src.shared.media.frame import DecodedFrame, PlaneInfo
from src.shared.media.metadata import Rational, VideoMetadata

OUTPUT = (960, 540)            # a proxy-sized preview canvas
CANVAS = (1920, 1080)          # the sequence resolution transforms are in


def _source(**overrides) -> SimpleNamespace:
    fields = dict(pixel_buffer=object(), pixel_format="nv12", bit_depth=8, width=1920,
                  height=1080, color_matrix="bt709", color_range="limited",
                  color_primaries="bt709", color_transfer="bt709")
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _nv12(source=_source, **track_info) -> dict:
    """An NV12 layer; ``source`` may be ``None`` or another frame."""
    return {"frame_type": "vt_pixel_buffer",
            "vt_pixel_buffer": _source() if source is _source else source,
            "track_info": dict(track_info)}


def _solid(rgb=(0.2, 0.4, 0.9), **track_info) -> dict:
    info = {"is_solid": True, "solid_color": rgb}
    info.update(track_info)
    return {"frame_type": "solid", "track_info": info}


def _planar(depth: int = 10, *, matrix: str = "bt709", range_tag: str = "limited",
            transfer: str = "bt709") -> dict:
    dtype = np.uint8 if depth == 8 else np.uint16
    pixel_format = "yuv422p" if depth == 8 else f"yuv422p{depth}le"
    shapes = ((20, 32), (20, 16), (20, 16))
    planes = tuple(np.zeros(shape, dtype=dtype) for shape in shapes)
    infos = tuple(PlaneInfo.create(index, width, height, name, dtype, depth)
                  for index, ((height, width), name) in enumerate(zip(shapes, "YUV")))
    metadata = VideoMetadata(
        32, 20, Rational(25, 1), 400, "dnxhd", "HQX", pixel_format, depth,
        color_matrix=matrix, color_range=range_tag, color_transfer=transfer,
        chroma_siting=0,
    )
    return {"frame_type": "planar", "track_info": {},
            "decoded_frame": DecodedFrame(2, 2, 0.08, planes=planes, metadata=metadata,
                                          plane_infos=infos, pixel_format=pixel_format)}


def _task(*layers, transition_jobs: tuple = ()) -> EditRenderTask:
    return EditRenderTask(7, 3, list(layers), output_fbo_size=OUTPUT,
                          coordinate_size=CANVAS, transition_jobs=transition_jobs)


def _dip(factor: float, color=(1.0, 0.0, 0.0)) -> TransitionBlendInfo:
    return TransitionBlendInfo(
        transition=SimpleNamespace(transition_type="dip_to_color"), left_clip=None,
        right_clip=None, blend_factor=factor, left_source_frame=0, right_source_frame=0,
        blend_color=color,
    )


def _layer(kind: str = "nv12", **fields) -> MetalLayerPlan:
    defaults = dict(source=None, solid_color=(0.0, 0.0, 0.0), transform=None, crop=None,
                    opacity=1.0, blend_mode=0, lut=None)
    defaults.update(fields)
    return MetalLayerPlan(kind=kind, **defaults)


def _plan(task: EditRenderTask) -> MetalCompositePlan:
    plan = build_metal_composite_plan(task)
    assert isinstance(plan, MetalCompositePlan), plan
    return plan


def _refusal(task: EditRenderTask) -> str:
    plan = build_metal_composite_plan(task)
    assert isinstance(plan, PlanRefusal), plan
    return plan.reason


class TestLayers:
    def test_several_clips_are_one_plan_in_task_order(self) -> None:
        plan = _plan(_task(_nv12(), _nv12(), _nv12()))
        assert [layer.kind for layer in plan.layers] == ["nv12"] * 3
        assert plan.is_single_untouched_clip is False
        assert _plan(_task(_nv12())).is_single_untouched_clip is True

    def test_the_task_is_left_untouched(self) -> None:
        task = _task(_nv12(opacity=0.5), _solid())
        before = [dict(layer["track_info"]) for layer in task.layers]
        sources = [layer.get("vt_pixel_buffer") for layer in task.layers]
        _plan(task)
        assert [dict(layer["track_info"]) for layer in task.layers] == before
        assert [layer.get("vt_pixel_buffer") for layer in task.layers] == sources

    @pytest.mark.parametrize("opacity", [0.0, 0.37, 1.0, 1.25])
    def test_opacity_is_taken_as_given(self, opacity: float) -> None:
        # The GL blend does not clamp u_opacity; neither does the plan.
        assert _plan(_task(_nv12(opacity=opacity))).layers[0].opacity == opacity

    @pytest.mark.parametrize("mode", list(ClipBlendMode))
    def test_every_blend_mode_keeps_its_code(self, mode: ClipBlendMode) -> None:
        assert _plan(_task(_nv12(), _nv12(blend_mode=mode))).layers[1].blend_mode == mode.code

    def test_blend_codes_follow_the_gl_lookup(self) -> None:
        # _blend_code_of reads an enum's ``code``; anything else is normal there.
        for blend_mode in (42, object(), "multiply"):
            assert _plan(_task(_nv12(), _nv12(blend_mode=blend_mode))).layers[1].blend_mode == 0

    @pytest.mark.parametrize("transform", [
        (5.0, -3.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),       # move
        (0.0, 0.0, 0.5, 1.5, 0.0, "center", 0.0, 0.0),         # scale
        (10.0, 20.0, 0.8, 0.8, 33.0, "center", 0.0, 0.0),      # rotate
        (0.0, 0.0, 1.2, 1.2, 15.0, "custom", 300.0, 200.0),    # custom pivot
    ])
    def test_affine_transforms_are_planned(self, transform: tuple) -> None:
        assert _plan(_task(_nv12(transform=transform))).layers[0].transform == transform

    def test_an_identity_transform_adds_no_pass(self) -> None:
        identity = (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
        within_tolerance = (5e-5, 0.0, 1.00001, 1.0, 0.0, "center", 0.0, 0.0)
        for transform in (None, identity, within_tolerance):
            assert _plan(_task(_nv12(transform=transform))).layers[0].transform is None

    @pytest.mark.parametrize(("transform", "reason"), [
        ((0.0, 0.0, 0.0, 1.0, 0.0, "center", 0.0, 0.0), "transform_degenerate"),
        ((0.0, 0.0, 1.0, 1.0, 0.0, "top_left", 0.0, 0.0),
         "transform_pivot_unsupported:top_left"),
        ((0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0), "transform_unsupported"),
        ((float("nan"), 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0), "transform_not_finite"),
        ("not a transform", "transform_unsupported"),
    ])
    def test_unrenderable_transforms_refuse_the_frame(self, transform, reason: str) -> None:
        assert _refusal(_task(_nv12(), _nv12(transform=transform))) == reason

    def test_a_crop_is_the_one_the_gl_pass_applies(self) -> None:
        plan = _plan(_task(_nv12(crop=(0.1, 0.0, 0.05, 0.2, 12.0))))
        crop = plan.layers[0].crop
        assert isinstance(crop, CropParams)
        assert (crop.left, crop.right, crop.top, crop.bottom, crop.feather) == (
            0.1, 0.0, 0.05, 0.2, 12.0)

    @pytest.mark.parametrize("crop", [None, (0.0, 0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)])
    def test_a_crop_that_removes_nothing_adds_no_pass(self, crop) -> None:
        assert _plan(_task(_nv12(crop=crop))).layers[0].crop is None

    def test_a_feather_only_crop_is_a_pass_like_in_gl(self) -> None:
        # crop_render.crop_is_identity (the GL pass) looks at the feather too.
        assert gl_crop_params((0.0, 0.0, 0.0, 0.0, 0.5)) is not None
        assert _plan(_task(_nv12(crop=(0.0, 0.0, 0.0, 0.0, 0.5)))).layers[0].crop is not None

    def test_an_unbuildable_crop_is_skipped_like_in_gl(self) -> None:
        # _apply_crop returns the source when CropParams() rejects the tuple.
        for crop in ((0.9, 0.9, 0.9, 0.9, 0.0), (0.1, 0.2), (0.1, 0.1, 0.1, 0.1, -3.0)):
            assert _plan(_task(_nv12(crop=crop))).layers[0].crop is None, crop

    def test_a_solid_keeps_the_colour_the_gl_track_fbo_is_cleared_with(self) -> None:
        plan = _plan(_task(_solid((0.1, 0.2, 0.3)), _nv12()))
        assert plan.layers[0].kind == "solid" and plan.layers[0].source is None
        assert plan.layers[0].solid_color == (0.1, 0.2, 0.3)
        generator = _solid((0.9, 0.9, 0.9),
                           generator_params={"mode": "solid", "color_r": 0.0, "color_g": 0.5})
        assert _plan(_task(generator)).layers[0].solid_color == (0.0, 0.5, 0.8)

    @pytest.mark.parametrize("mode", ["checker", "gradient", "radial"])
    def test_generator_patterns_stay_on_gl(self, mode: str) -> None:
        layer = _solid(generator_params={"mode": mode})
        assert _refusal(_task(layer)) == f"generator_unsupported:{mode}"

    @pytest.mark.parametrize(("layer", "reason"), [
        (_nv12(source=None), "source_unsupported"),
        (_nv12(opacity="half"), "opacity_not_numeric"),
        ({"frame_type": "planar", "decoded_frame": object(), "track_info": {}},
         "source_unsupported:planar"),
        ({"frame_type": "comp_gpu_handle", "gpu_handle": object(), "track_info": {}},
         "source_unsupported:comp_gpu_handle"),
        ({"frame_type": "solid", "track_info": {"solid_color": (1.0, 1.0, 1.0)}},
         "source_unsupported:solid"),          # no is_solid: not a solid layer
        (["not", "a", "dict"], "layer_not_a_dict"),
        (_nv12(clip_effects=(object(),)), "clip_effects"),
    ])
    def test_unrenderable_layers_refuse_the_whole_frame(self, layer, reason: str) -> None:
        assert _refusal(_task(_nv12(), layer)) == reason

    @pytest.mark.parametrize("overrides", [
        {"pixel_format": "p010"}, {"bit_depth": 10}, {"color_matrix": "bt2020"},
        {"color_range": "full"}, {"color_primaries": "bt2020"}, {"width": 0},
        {"pixel_buffer": None},
    ])
    def test_sources_outside_the_kernel_contract_stay_on_gl(self, overrides: dict) -> None:
        assert _refusal(_task(_nv12(_source(**overrides)))) == "source_unsupported"

    @pytest.mark.parametrize(("depth", "matrix", "matrix_code", "range_tag", "range_code"), [
        (8, "smpte170m", 6, "full", 1), (10, "bt709", 1, "limited", 0),
        (12, "bt2020nc", 9, "tv", 0), (16, "bt470bg", 5, "pc", 1),
    ])
    def test_planar_contract_keeps_planes_depth_and_edit_gl_sampling(
        self, depth: int, matrix: str, matrix_code: int, range_tag: str, range_code: int,
    ) -> None:
        layer = _planar(depth, matrix=matrix, range_tag=range_tag)
        plan = _plan(_task(_nv12(), layer))
        converted = plan.layers[1].source
        original = layer["decoded_frame"]
        assert plan.layers[1].kind == "planar"
        assert converted.bit_depth == depth and converted.subsampling == "422"
        assert (converted.matrix, converted.color_range, converted.chroma_siting) == (matrix_code, range_code, 1)
        assert (converted.y is original.planes[0] and converted.u is original.planes[1]
                and converted.v is original.planes[2])

    def test_unsupported_planar_hdr_and_upload_saturation_are_whole_frame_fallbacks(self) -> None:
        assert _refusal(_task(_nv12(), _planar(transfer="smpte2084"))) == "planar_color_contract_unsupported"
        assert _refusal(_task(*(_planar() for _ in range(5)))) == "too_many_planar_layers:5"


class TestGrades:
    def test_no_grade_is_fine(self) -> None:
        assert _plan(_task(_nv12())).layers[0].lut is None

    @pytest.mark.parametrize("grade", [
        {"color_graph": object()},
        {"color_graph": None, "grading_params": {"exposure": 1.0}},
        {"color_graph": "not-a-graph"},
    ])
    def test_anything_but_a_serial_lut_graph_stays_on_gl(self, grade: dict) -> None:
        assert _refusal(_task(_nv12(preview_grade=grade))) == "grade_unsupported"


class TestFrame:
    def test_no_layers_refuse(self) -> None:
        assert _refusal(_task()) == "no_layers"

    def test_deep_stacks_refuse(self) -> None:
        stack = [_nv12() for _ in range(MAX_COMPOSITE_LAYERS + 1)]
        assert _refusal(_task(*stack)) == f"too_many_layers:{MAX_COMPOSITE_LAYERS + 1}"
        del stack[-1]
        assert isinstance(build_metal_composite_plan(_task(*stack)), MetalCompositePlan)

    def test_the_limit_is_a_parameter(self) -> None:
        plan = build_metal_composite_plan(_task(_nv12(), _nv12()), max_layers=1)
        assert plan == PlanRefusal("too_many_layers:2")

    def test_adjustment_clips_stay_on_gl(self) -> None:
        task = _task(_nv12())
        task.adjustment_layers = [(object(), (), None)]
        assert _refusal(task) == "adjustment_layers"

    def test_a_dip_to_color_is_planned_with_its_colour_clamped(self) -> None:
        dip = _dip(0.25, (1.4, -0.2, 0.5))
        plan = _plan(_task(_nv12(), _nv12(), transition_jobs=((dip, None),)))
        # Two stages: the colour peaks half way, so the factor is blend * 2.
        assert plan.dips == ((0.5, (1.0, 0.0, 0.5)),)
        late = _plan(_task(_nv12(), transition_jobs=((_dip(0.8), None),))).dips
        assert late[0][0] == pytest.approx(0.4) and late[0][1] == (1.0, 0.0, 0.0)

    def test_a_dip_that_contributes_nothing_adds_no_pass(self) -> None:
        plan = _plan(_task(_nv12(), transition_jobs=((_dip(0.0), None),)))
        assert plan.dips == ()

    def test_dips_keep_their_order(self) -> None:
        jobs = ((_dip(0.2, (1.0, 0.0, 0.0)), None), (_dip(0.4, (0.0, 1.0, 0.0)), None))
        plan = _plan(_task(_nv12(), transition_jobs=jobs))
        assert plan.dips == ((0.4, (1.0, 0.0, 0.0)), (0.8, (0.0, 1.0, 0.0)))

    @pytest.mark.parametrize("transition_type", ["wipe", "dissolve", "fade", "circle"])
    def test_any_other_transition_keeps_the_frame_on_gl(self, transition_type: str) -> None:
        trans = SimpleNamespace(transition_type=transition_type)
        assert _refusal(_task(_nv12(), _nv12(), transition_jobs=((trans, object()),))) == (
            f"transition_unsupported:{transition_type}")

    def test_a_dip_with_a_mask_spec_is_not_the_plain_colour_pass(self) -> None:
        assert _refusal(_task(_nv12(), transition_jobs=((_dip(0.3), object()),))) == (
            "transition_unsupported:dip_to_color")


class TestQuadMap:
    TRANSFORMS = [
        (5.0, -3.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
        (10.0, 20.0, 0.8, 0.65, 33.0, "center", 0.0, 0.0),
        (0.0, 0.0, 1.3, 0.7, -120.0, "custom", 300.0, 200.0),
    ]
    SIZES = [((1920, 1080), (1920, 1080)), (OUTPUT, CANVAS)]

    @pytest.mark.parametrize("size", SIZES, ids=["native", "proxy"])
    @pytest.mark.parametrize("transform", TRANSFORMS, ids=["move", "shrink+rotate", "pivot"])
    def test_the_inverse_undoes_the_gl_quad_matrix(self, transform: tuple, size: tuple) -> None:
        output, canvas = size
        matrix = np.frombuffer(_transform_to_ndc_mat3_bytes(
            transform, *output, track_fbo_y_flipped=True,
            coordinate_width=canvas[0], coordinate_height=canvas[1]), np.float32,
        ).reshape(3, 3).T.astype(np.float64)        # the bytes are column-major
        row0, row1 = (np.asarray(row, np.float64)
                      for row in inverse_quad_map(transform, output, canvas))
        for x, y in np.random.default_rng(1).uniform(-1.0, 1.0, size=(64, 2)):
            quad = (matrix @ np.array([x, y, 1.0]))[:2]
            back = np.array([row0[:2] @ quad + row0[2], row1[:2] @ quad + row1[2]])
            np.testing.assert_allclose(back, [x, y], atol=2e-5)

    @pytest.mark.parametrize("size", SIZES, ids=["native", "proxy"])
    def test_moving_a_clip_down_reads_the_picture_above(self, size: tuple) -> None:
        # Image space is Y-down: +100 px on screen samples 100 px higher up.
        output, canvas = size
        row0, row1 = inverse_quad_map((0.0, 100.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
                                      output, canvas)
        source_v = (row1[2]) * 0.5 + 0.5              # at the screen centre (ndc 0, 0)
        assert source_v == pytest.approx(0.5 - 100.0 / canvas[1], abs=1e-6)

    def test_a_collapsed_quad_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="singular"):
            inverse_quad_map((0.0, 0.0, 1e-12, 1.0, 0.0, "center", 0.0, 0.0), (64, 64), (64, 64))

    def test_an_untouched_layer_reads_its_own_pixels(self) -> None:
        # Without a transform the quad is the whole picture: the identity map.
        # (All-zero rows would read one texel for every pixel of the frame.)
        identity = layer_params(_layer(), (64, 64), (64, 64))
        assert identity["row0"][0].tolist() == [1.0, 0.0, 0.0, 0.0]
        assert identity["row1"][0].tolist() == [0.0, 1.0, 0.0, 0.0]
        assert identity["flags"][0].tolist() == [0, 0, 0, 0]

    def test_layer_params_mark_which_tests_the_kernel_runs(self) -> None:
        moved = layer_params(_layer(transform=(1.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)),
                             (64, 64), (64, 64))
        assert moved["flags"][0].tolist() == [1, 0, 0, 0]
        cropped = layer_params(_layer(crop=CropParams(0.25, 0.0, 0.0, 0.0, 0.0)),
                               (64, 64), (64, 64))
        assert cropped["flags"][0].tolist() == [0, 1, 0, 0]
        # The kept rect is in image texcoords: a quarter off the left edge.
        assert cropped["crop_rect"][0].tolist() == [0.25, 0.0, 1.0, 1.0]

    def test_the_struct_is_five_sixteen_byte_rows(self) -> None:
        assert layer_params(_layer(), (64, 64), (64, 64)).dtype.itemsize == 80
