"""P1-26 camera filmback and physically-controlled light contracts."""

import inspect
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_filmback_presets_and_gate_fit_are_deterministic():
    from src.features.composite.render3d.camera_model import (
        filmback_spec, fov_from_focal_and_filmback,
        focal_from_fov_and_filmback, sensor_height_for_gate_fit)

    full_frame = filmback_spec("full_frame")
    assert (full_frame.width_mm, full_frame.height_mm) == (36.0, 24.0)
    assert sensor_height_for_gate_fit(full_frame, 16.0 / 9.0, "vertical") == 24.0
    assert sensor_height_for_gate_fit(full_frame, 16.0 / 9.0, "horizontal") < 24.0
    focal = focal_from_fov_and_filmback(50.0, full_frame, 16.0 / 9.0, "vertical")
    assert fov_from_focal_and_filmback(focal, full_frame, 16.0 / 9.0, "vertical") == pytest.approx(50.0)


def test_camera_filmback_change_preserves_requested_match_mode():
    from src.features.composite.nodes.camera3d_node import Camera3DNode

    camera = Camera3DNode()
    original_fov = camera.parameters["cam_fov"]
    camera.set_parameter("cam_filmback", "full_frame")
    assert camera.parameters["cam_sensor_width"] == 36.0
    assert camera.parameters["cam_sensor_size"] == 24.0
    assert camera.parameters["cam_fov"] == original_fov
    assert camera.parameters["cam_focal_length"] < 38.6

    camera.set_parameter("cam_match_mode", "preserve_focal_length")
    old_focal = camera.parameters["cam_focal_length"]
    camera.set_parameter("cam_filmback", "super35")
    assert camera.parameters["cam_focal_length"] == old_focal
    assert camera.parameters["cam_fov"] != original_fov


def test_camera_units_are_carried_into_payload_contract():
    from src.features.composite.nodes.camera3d_node import Camera3DNode
    from src.features.composite.render3d.scene_payload import CameraPayload

    names = {item.name for item in Camera3DNode.PARAM_DEFS}
    assert {"cam_filmback", "cam_gate_fit", "cam_match_mode", "cam_unit"} <= names
    payload = CameraPayload(
        proj=np.eye(4, dtype=np.float32), view=np.eye(4, dtype=np.float32),
        eye=(0.0, 0.0, 5.0), target=(0.0, 0.0, 0.0),
        sensor_width=36.0, filmback="full_frame", unit_mode="cm",
        unit_scale=0.01)
    assert "full_frame" in payload.signature()
    assert 0.01 in payload.signature()


def test_ies_profile_reader_averages_horizontal_slices(tmp_path):
    from src.features.composite.render3d.ies_profile import load_ies_profile

    path = tmp_path / "test.ies"
    path.write_text(
        "IESNA:LM-63-1995\nTILT=NONE\n"
        "1 1000 1 4 2 1 1 1 1 1 1 1 1\n"
        "0 30 60 90\n0 90\n"
        "100 80 30 0 80 60 20 0\n",
        encoding="utf-8")
    profile = load_ies_profile(str(path))
    assert profile.vertical_angles == (0.0, 30.0, 60.0, 90.0)
    assert profile.samples(4) == pytest.approx((1.0, 0.7777778, 0.2777778, 0.0))


def test_light_controls_reach_payload_and_shader_contract():
    from src.features.composite.nodes.light3d_node import Light3DNode
    from src.features.composite.render3d.scene_payload import (
        LIGHT_DECAY_SHADER_TYPE_ID, LightPayload)
    from src.features.composite.core import shaders
    from src.features.composite.nodes import scene3d_node

    names = {item.name for item in Light3DNode.PARAM_DEFS}
    assert {"light_decay_mode", "light_shadow_quality", "light_shadow_enable",
            "light_unit", "ies_profile", "ies_enabled"} <= names
    assert LIGHT_DECAY_SHADER_TYPE_ID["inverse_square"] == 2
    payload = LightPayload(
        decay_mode="linear", shadow_quality="high", shadow_enabled=False,
        ies_enabled=True, ies_samples=(1.0, 0.8, 0.2, 0.0))
    assert "linear" in payload.signature()
    assert payload.shadow_enabled is False
    assert "u_light_decay_mode" in shaders._MESH_PBR_FRAGMENT
    assert "u_light_ies_profile" in shaders._MESH_PBR_FRAGMENT
    source = inspect.getsource(scene3d_node.Scene3DNode._do_evaluate)
    assert "_shadow_map_size" in source
    assert "shadow_quality" in source
