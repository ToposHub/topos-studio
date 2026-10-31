"""Coverage contract for the small Metal-native timeline effect subset."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import TimelineExporter


def _layer(*effects):
    return SimpleNamespace(clip=SimpleNamespace(effects=tuple(effects)))


@pytest.mark.parametrize(
    ("effect_type", "params", "kind"),
    (
        ("blur", {"blur_type": "box", "radius_x": 5.0,
                   "radius_y": 3.0, "mix": 0.75}, 1.0),
        ("blur", {"blur_type": "gaussian", "radius_x": 5.0,
                   "radius_y": 5.0, "mix": 1.0}, 2.0),
        ("blur", {"blur_type": "gaussian", "radius_x": 6.0,
                   "radius_y": 6.0, "mix": 1.0}, 2.0),
        ("blur", {"blur_type": "gaussian", "radius_x": 16.0,
                   "radius_y": 16.0, "mix": 1.0}, 2.0),
        ("blur", {"blur_type": "gaussian", "radius_x": 5.0,
                   "radius_y": 5.0, "edge_policy": "wrap"}, 2.0),
        ("blur", {"blur_type": "bilateral", "radius": 5.0,
                   "edge_preserve": 0.4, "mix": 0.75}, 9.0),
        ("blur", {"blur_type": "motion", "radius": 8.0,
                   "angle": 37.0, "shutter": 1.25,
                   "quality": "high", "edge_policy": "wrap"}, 10.0),
        ("blur", {"blur_type": "bokeh", "radius": 8.0,
                   "aperture_blades": "6", "anamorphic": 0.4,
                   "highlight_gain": 1.2, "quality": "normal"}, 11.0),
        ("sharpen", {"amount": 1.0, "radius": 1.0,
                     "threshold": 0.2, "channels": "rgb"}, 3.0),
        ("sharpen", {"amount": 1.0, "radius": 16.0}, 3.0),
        ("glow", {"threshold": 0.7, "radius": 5.0,
                  "intensity": 0.5, "mix": 0.8,
                  "tint_r": 0.8, "tint_g": 1.2,
                  "tint_b": 0.5, "quality": "fast"}, 4.0),
        ("glow", {"threshold": 0.7, "radius": 16.0,
                  "intensity": 0.5, "mix": 1.0}, 4.0),
        ("erode_dilate", {"operation": "erode", "size": 5,
                           "iterations": 2, "channels": "all",
                           "edge_mode": "extend"}, 5.0),
        ("erode_dilate", {"operation": "dilate", "size": 33,
                           "iterations": 1, "channels": "all",
                           "edge_mode": "extend"}, 5.0),
        ("erode_dilate", {"operation": "erode", "size": 50,
                           "iterations": 10, "channels": "all",
                           "edge_mode": "reflect"}, 5.0),
        ("edge_detect", {"method": "sobel", "threshold": 0.2,
                          "direction": "vertical", "output_mode": "gradient",
                          "output_strength": 1.5, "invert": True,
                          "kernel_size": 3, "preblur": 0.0,
                          "channel": "luminance"}, 6.0),
        ("edge_detect", {"method": "sobel", "kernel_size": 5,
                          "channel": "luminance"}, 6.0),
        ("edge_detect", {"method": "canny", "kernel_size": 7,
                          "channel": "r"}, 6.0),
        ("edge_detect", {"method": "laplacian", "kernel_size": 5,
                          "channel": "g"}, 6.0),
        ("edge_detect", {"method": "sobel", "preblur": 1.3,
                          "kernel_size": 3, "channel": "b"}, 6.0),
        ("edge_detect", {"method": "canny", "threshold": 0.2,
                          "threshold_high": 0.4,
                          "direction": "vertical", "output_mode": "edges",
                          "output_strength": 1.5, "invert": True,
                          "kernel_size": 3, "preblur": 0.0,
                          "channel": "luminance"}, 6.0),
        ("edge_detect", {"method": "prewitt", "kernel_size": 3,
                          "channel": "b"}, 6.0),
        ("edge_detect", {"method": "laplacian", "kernel_size": 3,
                          "output_mode": "gradient", "channel": "g"}, 6.0),
        ("edge_detect", {"method": "sobel", "channel": "r",
                          "kernel_size": 3, "preblur": 0.0}, 6.0),
        ("edge_detect", {"method": "sobel", "channel": "a",
                          "kernel_size": 3, "preblur": 0.0}, 6.0),
        ("denoise", {"strength": 0.75, "radius": 4,
                     "luminance_only": True, "chroma_only": False}, 7.0),
        ("defocus", {"mode": "separable", "focal_x": 0.4,
                     "focal_y": 0.6, "focal_size": 0.25,
                     "max_radius": 24.0, "edge_policy": "mirror",
                     "process_alpha": False}, 8.0),
    ),
)
def test_default_metal_effects_have_native_parameters(effect_type, params, kind):
    exporter = object.__new__(TimelineExporter)
    supported, uniforms = exporter._gpu_metal_effect_uniforms(
        _layer(EffectInstance(effect_type=effect_type, params=params)), 0)

    assert supported is True
    assert uniforms is not None
    assert uniforms["kind"] == kind
    if effect_type == "sharpen":
        assert uniforms["threshold"] == pytest.approx(params.get("threshold", 0.0))
        assert uniforms["channels"] == {
            "all": 0.0, "rgb": 1.0, "luminance": 2.0,
        }[params.get("channels", "all")]
    if effect_type == "glow":
        sample_count = {"fast": 8.0, "normal": 16.0, "high": 32.0}.get(
            params.get("quality", "normal"))
        assert uniforms["half_x"] == sample_count
        assert uniforms["tint_g"] == pytest.approx(params.get("tint_g", 0.85))
    if effect_type == "blur":
        if params.get("blur_type") == "bilateral":
            assert uniforms["half_x"] == uniforms["half_y"] == 5.0
            assert uniforms["range_sigma"] == pytest.approx(48.0 / 255.0)
            assert uniforms["kind"] == 9.0
        elif params.get("blur_type") == "motion":
            assert uniforms["kind"] == 10.0
            assert uniforms["half_x"] == 8.0
            assert uniforms["half_y"] == pytest.approx(1.25)
            assert uniforms["sigma_x"] == pytest.approx(
                math.radians(37.0))
            assert uniforms["sample_count"] == 32.0
            assert uniforms["edge_policy"] == 1.0
        elif params.get("blur_type") == "bokeh":
            assert uniforms["kind"] == 11.0
            assert uniforms["half_x"] == 8.0
            assert uniforms["aperture_blades"] == 6.0
            assert uniforms["anamorphic"] == pytest.approx(0.4)
            assert uniforms["highlight_gain"] == pytest.approx(1.2)
            assert uniforms["sample_count"] == 16.0
        else:
            assert uniforms["edge_policy"] == {
                "clamp": 0.0, "wrap": 1.0, "mirror": 2.0,
            }.get(params.get("edge_policy", "clamp"))
    if effect_type == "erode_dilate":
        expected_half = {
            5: 4.0, 33: 16.0, 50: 250.0,
        }[params.get("size")]
        assert uniforms["half_x"] == expected_half
        assert uniforms["half_y"] == expected_half
        assert uniforms["operation"] == float(
            params.get("operation") == "dilate")
    if effect_type == "edge_detect":
        assert uniforms["half_x"] == float(params.get("kernel_size", 3))
        channel = params.get("channel", "luminance")
        if channel in {"a", "r", "g", "b"}:
            assert uniforms["channel"] == {
                "a": 1.0, "r": 2.0, "g": 3.0, "b": 4.0,
            }[channel]
        assert uniforms["direction"] == float(
            ("both", "horizontal", "vertical").index(
                params.get("direction", "both")))
        assert uniforms["output_mode"] == float(
            ("edges", "gradient", "direction").index(
                params.get("output_mode", "edges")))
        assert uniforms["invert"] == float(bool(params.get("invert", False)))
        assert uniforms["method"] == {
            "sobel": 0.0, "canny": 1.0,
            "prewitt": 2.0, "laplacian": 3.0,
        }[params.get("method", "sobel")]
        assert uniforms["preblur"] == pytest.approx(params.get("preblur", 0.0))
    if effect_type == "denoise":
        assert uniforms["half_x"] == uniforms["half_y"] == 4.0
        assert uniforms["luminance_only"] == 1.0
        assert uniforms["chroma_only"] == 0.0
    if effect_type == "defocus":
        assert uniforms["half_x"] == 15.0
        assert uniforms["half_y"] == 0.0
        assert uniforms["focal_x"] == pytest.approx(0.4)
        assert uniforms["focal_y"] == pytest.approx(0.6)
        assert uniforms["focal_size"] == pytest.approx(0.25)
        assert uniforms["max_radius"] == pytest.approx(24.0)
        assert uniforms["edge_policy"] == 2.0


@pytest.mark.parametrize(
    "effect",
    (
        EffectInstance(effect_type="blur", params={
            "blur_type": "gaussian", "radius_x": 24.0,
            "radius_y": 24.0,
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "gaussian", "radius_x": 17.0,
            "radius_y": 3.0,
        }),
        EffectInstance(effect_type="sharpen", params={
            "amount": 1.0, "radius": 17.0,
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "gaussian", "radius_x": float("inf"),
            "radius_y": 2.0,
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "bilateral", "radius": float("inf"),
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "motion", "radius": 8.0,
            "motion_source": "velocity",
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "motion", "radius": 8.0, "shutter": 2.1,
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "bokeh", "radius": 8.0,
            "aperture_blades": "7",
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "bokeh", "radius": 8.0,
            "quality": "ultra",
        }),
        EffectInstance(effect_type="blur", params={
            "blur_type": "gaussian", "radius_x": 2.0,
            "radius_y": 2.0, "edge_policy": "unknown",
        }),
        EffectInstance(effect_type="sharpen", params={
            "amount": 1.0, "radius": float("nan"),
        }),
        EffectInstance(effect_type="glow", params={"quality": "ultra"}),
        EffectInstance(effect_type="glow", params={"radius": float("inf")}),
        EffectInstance(effect_type="glow", params={"intensity": float("nan")}),
        EffectInstance(effect_type="erode_dilate", params={
            "operation": "erode", "size": 5, "iterations": 1,
            "channels": "all", "edge_mode": "unknown",
        }),
        EffectInstance(effect_type="erode_dilate", params={
            "operation": "dilate", "size": 5, "iterations": 2,
            "channels": "unknown", "edge_mode": "extend",
        }),
        EffectInstance(effect_type="erode_dilate", params={
            "operation": "dilate", "size": 3.5, "iterations": 1,
            "channels": "all", "edge_mode": "extend",
        }),
        EffectInstance(effect_type="erode_dilate", params={
            "operation": "invalid", "size": 3, "iterations": 1,
            "channels": "all", "edge_mode": "extend",
        }),
        EffectInstance(effect_type="edge_detect", params={"kernel_size": 9}),
        EffectInstance(effect_type="edge_detect", params={"preblur": 10.1}),
        EffectInstance(effect_type="edge_detect", params={"preblur": -0.1}),
        EffectInstance(effect_type="edge_detect", params={
            "threshold": float("nan"),
        }),
        EffectInstance(effect_type="denoise", params={"strength": 1.1}),
        EffectInstance(effect_type="denoise", params={"radius": 10.5}),
        EffectInstance(effect_type="denoise", params={"radius": 11}),
        EffectInstance(effect_type="denoise", params={"strength": float("nan")}),
        EffectInstance(effect_type="defocus", params={"mode": "invalid"}),
        EffectInstance(effect_type="defocus", params={"max_radius": 101.0}),
        EffectInstance(effect_type="defocus", params={"focal_x": float("nan")}),
    ),
)
def test_metal_effects_outside_native_subset_are_declined(effect):
    exporter = object.__new__(TimelineExporter)

    supported, uniforms = exporter._gpu_metal_effect_uniforms(_layer(effect), 0)

    assert supported is False
    assert uniforms is None


@pytest.mark.parametrize(
    "params",
    (
        {"operation": "erode", "size": 3, "iterations": 1,
         "channels": "alpha", "edge_mode": "extend"},
        {"operation": "dilate", "size": 1, "iterations": 10,
         "channels": "all", "edge_mode": "extend"},
    ),
)
def test_opaque_timeline_morphology_noops_are_native(params):
    exporter = object.__new__(TimelineExporter)
    supported, uniforms = exporter._gpu_metal_effect_uniforms(
        _layer(EffectInstance(effect_type="erode_dilate", params=params)), 0)

    assert supported is True
    assert uniforms is None


@pytest.mark.parametrize(("edge_mode", "edge_policy"), (
    ("extend", 0.0), ("wrap", 1.0), ("reflect", 2.0),
))
def test_metal_morphology_supports_all_edge_modes(edge_mode, edge_policy):
    exporter = object.__new__(TimelineExporter)
    supported, uniforms = exporter._gpu_metal_effect_uniforms(
        _layer(EffectInstance(effect_type="erode_dilate", params={
            "operation": "dilate", "size": 5, "iterations": 1,
            "channels": "all", "edge_mode": edge_mode,
        })),
        0,
    )
    assert supported is True
    assert uniforms is not None
    assert uniforms["edge_policy"] == edge_policy


def test_metal_defocus_mipmap_mode_has_native_parameters():
    exporter = object.__new__(TimelineExporter)
    supported, uniforms = exporter._gpu_metal_effect_uniforms(
        _layer(EffectInstance(effect_type="defocus", params={
            "mode": "mipmap", "focal_x": 0.5, "focal_y": 0.5,
            "focal_size": 0.2, "max_radius": 32.0,
        })),
        0,
    )
    assert supported is True
    assert uniforms is not None
    assert uniforms["kind"] == 8.0
    assert uniforms["mode"] == 1.0


def test_metal_denoise_negligible_strength_is_native_identity():
    exporter = object.__new__(TimelineExporter)
    supported, uniforms = exporter._gpu_metal_effect_uniforms(
        _layer(EffectInstance(effect_type="denoise", params={
            "strength": 0.0005, "radius": 3,
        })), 0)
    assert supported is True
    assert uniforms is None


def test_metal_effect_chain_with_multiple_effects_is_declined():
    exporter = object.__new__(TimelineExporter)

    supported, uniforms = exporter._gpu_metal_effect_uniforms(
        _layer(
            EffectInstance(effect_type="blur", params={"radius": 3.0}),
            EffectInstance(effect_type="sharpen", params={"amount": 1.0}),
        ),
        0,
    )

    assert supported is False
    assert uniforms is None


def test_metal_effect_chain_resolves_all_supported_effects_in_timeline_order():
    exporter = object.__new__(TimelineExporter)
    glow = EffectInstance(
        effect_type="glow",
        params={"threshold": 0.7, "radius": 4.0, "intensity": 0.5},
        order=2,
    )
    blur = EffectInstance(
        effect_type="blur",
        params={"blur_type": "gaussian", "radius_x": 3.0,
                "radius_y": 3.0},
        order=0,
    )
    sharpen = EffectInstance(
        effect_type="sharpen", params={"amount": 0.8, "radius": 1.0},
        order=1,
    )

    supported, chain = exporter._gpu_metal_effect_chain_uniforms(
        _layer(glow, sharpen, blur), 11)

    assert supported is True
    assert chain is not None
    assert [item["kind"] for item in chain] == [2.0, 3.0, 4.0]


def test_metal_effect_chain_rejects_whole_chain_if_one_effect_is_unsupported():
    exporter = object.__new__(TimelineExporter)
    supported, chain = exporter._gpu_metal_effect_chain_uniforms(
        _layer(
            EffectInstance(effect_type="blur", params={"radius": 3.0}),
            EffectInstance(effect_type="defocus", params={
                "mode": "invalid", "max_radius": 8.0,
            }),
        ),
        0,
    )

    assert supported is False
    assert chain is None
