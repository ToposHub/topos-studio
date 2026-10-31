"""P1-21 render-role and shadow-catcher contract tests.

These tests intentionally stay on the pure-data side of the renderer.  The
GPU smoke path is exercised by the existing Scene3D suites when a compatible
ModernGL context is available; these checks guarantee that role semantics do
not silently disappear before reaching that path.
"""

import numpy as np
import pytest

from src.features.composite.core.shaders import (
    _MESH_SHADOW_CATCHER_FRAGMENT,
    _MESH_SHADOW_DEPTH_FRAGMENT,
)
from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.render3d.aov import (
    ObjectRenderFlags,
    RENDER_ROLE_HOLDOUT,
    RENDER_ROLE_MATTE,
    RENDER_ROLE_SHADOW_CATCHER,
    render_flags_from_role,
)
from src.features.composite.render3d.scene_payload import (
    GeometryPayload,
    ModelPayload,
)

pytestmark = pytest.mark.composite_core


def test_holdout_is_depth_visible_but_not_beauty_visible():
    flags = render_flags_from_role(RENDER_ROLE_HOLDOUT)
    assert Scene3DNode._is_beauty_visible(flags) is False
    assert Scene3DNode._can_cast_shadow(flags) is True
    assert Scene3DNode._has_holdout(flags) is True


def test_shadow_catcher_is_not_a_caster_or_beauty_object():
    flags = render_flags_from_role(
        RENDER_ROLE_SHADOW_CATCHER, shadow_intensity=2.5,
        shadow_softness=0.75)
    assert Scene3DNode._is_beauty_visible(flags) is False
    assert Scene3DNode._can_cast_shadow(flags) is False
    assert flags.shadow_intensity == 2.5
    assert flags.shadow_softness == 0.75


def test_matte_role_is_not_beauty_or_shadow_caster():
    flags = render_flags_from_role(RENDER_ROLE_MATTE, matte_id=42)
    assert flags.matte_object is True
    assert flags.matte_id == 42
    assert Scene3DNode._is_beauty_visible(flags) is False
    assert Scene3DNode._can_cast_shadow(flags) is False


def test_role_changes_invalidate_scene_payload_signatures():
    base_geo = GeometryPayload(vao=None, model_matrix=np.eye(4, dtype=np.float32))
    holdout_geo = GeometryPayload(
        vao=None,
        model_matrix=np.eye(4, dtype=np.float32),
        render_flags=render_flags_from_role(RENDER_ROLE_HOLDOUT),
    )
    assert base_geo.signature() != holdout_geo.signature()

    base_model = ModelPayload(has_model=True)
    matte_model = ModelPayload(
        has_model=True,
        render_flags=render_flags_from_role(RENDER_ROLE_MATTE, matte_id=7),
    )
    assert base_model.signature() != matte_model.signature()


def test_shadow_shader_contract_uses_light_space_and_pcf():
    assert "u_light_view_proj" in _MESH_SHADOW_CATCHER_FRAGMENT
    assert "u_shadow_map" in _MESH_SHADOW_CATCHER_FRAGMENT
    assert "textureSize" in _MESH_SHADOW_CATCHER_FRAGMENT
    assert "for (int y = -2; y <= 2; ++y)" in _MESH_SHADOW_CATCHER_FRAGMENT
    assert "gl_FragCoord.z" in _MESH_SHADOW_DEPTH_FRAGMENT
