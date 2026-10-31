"""Real-GL regression tests for the Scene3D motion-vector AOV."""

import numpy as np
import pytest

from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.render3d.aov import AOVPass
from src.features.composite.render3d.math_utils import (
    look_at, perspective, translate, vec3,
)
from src.features.composite.render3d.primitives import get_primitive
from src.features.composite.render3d.scene_payload import GeometryPayload


pytestmark = pytest.mark.composite_core


@pytest.fixture()
def motion_scene():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:
        pytest.skip(f"Cannot create standalone GL context: {exc}")

    node = Scene3DNode()
    texture = ctx.texture((64, 64), 4, dtype="f2")
    depth_rb = ctx.depth_renderbuffer((64, 64))
    fbo = ctx.framebuffer(color_attachments=[texture],
                          depth_attachment=depth_rb)
    node._aov_textures[AOVPass.MOTION_VECTOR.value] = texture
    node._aov_fbos[AOVPass.MOTION_VECTOR.value] = fbo
    node._aov_depth_rbs[AOVPass.MOTION_VECTOR.value] = depth_rb

    try:
        yield ctx, node
    finally:
        node.cleanup()
        ctx.release()


def _motion_pixels(fbo) -> np.ndarray:
    raw = fbo.read(components=4, dtype="f4")
    return np.frombuffer(raw, dtype=np.float32).reshape(64, 64, 4)


def _geo_payload(x: float) -> GeometryPayload:
    return GeometryPayload(
        vao=None,
        mesh=get_primitive("cube"),
        model_matrix=translate(x, 0.0, -4.0),
        index_count=36,
        variant="plain",
    )


def test_motion_vector_is_zero_on_first_frame_and_nonzero_on_next(motion_scene):
    ctx, node = motion_scene
    projection = perspective(50.0, 1.0, 0.1, 100.0)
    view = look_at(vec3(0.0, 0.0, 0.0), vec3(0.0, 0.0, -4.0),
                   vec3(0.0, 1.0, 0.0))
    model = np.eye(4, dtype=np.float32)

    node._render_motion_pass(
        ctx, 64, 64, projection, view, model,
        frame_number=0, scene_key=("geo",), import_ok=False,
        instance_enabled=False, instance_count=1,
        geo_payloads_src=[_geo_payload(0.0)],
    )
    first = _motion_pixels(node._aov_fbos[AOVPass.MOTION_VECTOR.value])
    assert np.max(np.abs(first[:, :, :2])) < 1e-6

    node._render_motion_pass(
        ctx, 64, 64, projection, view, model,
        frame_number=1, scene_key=("geo",), import_ok=False,
        instance_enabled=False, instance_count=1,
        geo_payloads_src=[_geo_payload(0.25)],
    )
    second = _motion_pixels(node._aov_fbos[AOVPass.MOTION_VECTOR.value])
    assert np.max(np.abs(second[:, :, :2])) > 1e-4


def test_geometry_aov_renders_instanced_geo_payload(motion_scene):
    ctx, node = motion_scene
    normal_texture = ctx.texture((64, 64), 4, dtype="f2")
    normal_depth = ctx.depth_renderbuffer((64, 64))
    normal_fbo = ctx.framebuffer(
        color_attachments=[normal_texture], depth_attachment=normal_depth)
    node._aov_textures[AOVPass.NORMAL.value] = normal_texture
    node._aov_fbos[AOVPass.NORMAL.value] = normal_fbo
    node._aov_depth_rbs[AOVPass.NORMAL.value] = normal_depth
    instance_vbo = ctx.buffer(np.asarray(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32).tobytes())

    try:
        node._render_aov_pass(
            ctx, AOVPass.NORMAL.value, 0, 64, 64,
            perspective(50.0, 1.0, 0.1, 100.0),
            look_at(vec3(0.0, 0.0, 0.0), vec3(0.0, 0.0, -4.0),
                    vec3(0.0, 1.0, 0.0)),
            np.eye(4, dtype=np.float32),
            import_ok=False, instance_enabled=False, instance_count=1,
            anim_enabled=False, anim_time=0.0, anim_speed=1.0,
            geo_payloads_src=[GeometryPayload(
                vao=None,
                mesh=get_primitive("cube"),
                model_matrix=translate(-0.5, 0.0, -4.0),
                index_count=36,
                variant="instanced",
                instance_vbo=instance_vbo,
                instance_enabled=True,
                instance_count=2,
            )],
        )
        pixels = _motion_pixels(normal_fbo)
        assert float(np.max(pixels[:, :, 3])) > 0.9
    finally:
        instance_vbo.release()
