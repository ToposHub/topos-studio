"""Contract tests for the deferred external YUV GPU pass."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.shared.media.gpu_frame import GpuFramePlane
from src.shared.video.external_yuv_renderer import ExternalYuvToRgbPass


def _plane(component: str, texture_id: int, target: str = "2d",
           internal_format: str = "") -> GpuFramePlane:
    return GpuFramePlane(
        component=component,
        api="opengl",
        texture_id=texture_id,
        target=target,
        width=64,
        height=64,
        internal_format=internal_format,
    )


def test_external_yuv_pass_rejects_non_opengl_planes_before_gl_setup():
    ctx = MagicMock()
    render_pass = ExternalYuvToRgbPass(ctx)
    metal_plane = GpuFramePlane(
        component="Y",
        api="metal",
        texture_id=101,
        target="2d",
        width=64,
        height=64,
        resource=SimpleNamespace(native_handle=object()),
    )

    assert not render_pass.render(
        (metal_plane, _plane("UV", 102)),
        SimpleNamespace(color_range="limited", color_matrix="bt709"),
        MagicMock(),
        64,
        64,
    )
    ctx.program.assert_not_called()


def test_external_yuv_pass_rejects_mixed_texture_target_families():
    ctx = MagicMock()
    render_pass = ExternalYuvToRgbPass(ctx)

    assert not render_pass.render(
        (_plane("Y", 101, "2d"), _plane("UV", 102, "rectangle")),
        SimpleNamespace(color_range="limited", color_matrix="bt709"),
        MagicMock(),
        64,
        64,
    )
    ctx.program.assert_not_called()


def _patched_gl_calls():
    """Patch every raw GL call the render path makes on the draw path."""
    from contextlib import ExitStack

    class _Stack:
        def __enter__(self):
            self._stack = ExitStack()
            for name in (
                "glActiveTexture",
                "glBindTexture",
                "glUseProgram",
                "glUniform1i",
                "glGetUniformLocation",
            ):
                self._stack.enter_context(
                    patch(f"OpenGL.GL.{name}")
                )
            return self._stack

        def __exit__(self, *exc):
            self._stack.__exit__(*exc)

    return _Stack()


def test_external_yuv_pass_maps_color_metadata():
    assert ExternalYuvToRgbPass._matrix_code("smpte170m") == 0
    assert ExternalYuvToRgbPass._matrix_code("bt709") == 1
    assert ExternalYuvToRgbPass._matrix_code("bt2020nc") == 2


def _rendered_2d_program(render_pass: ExternalYuvToRgbPass, ctx) -> MagicMock:
    """Run one successful mocked draw and return the 2D program used."""
    with _patched_gl_calls():
        ok = render_pass.render(
            (
                _plane("Y", 101, "2d", "GL_R8"),
                _plane("UV", 102, "2d", "GL_RG8"),
            ),
            SimpleNamespace(color_range="limited", color_matrix="bt709"),
            MagicMock(),
            64,
            64,
        )
    assert ok is True
    return ctx.program.return_value


def test_external_yuv_pass_assigns_sampler_units():
    """Both samplers must be pinned to their texture units.

    Samplers default to unit 0: without the explicit assignment the UV plane
    sampled the Y texture and the pass "succeeded" with wrong chroma. The
    assignment must use raw glUniform1i because moderngl never writes
    sampler2DRect uniform values to GL (see the Edit renderer's YUV path).
    """
    ctx = MagicMock()
    render_pass = ExternalYuvToRgbPass(ctx)
    assert render_pass.initialize() is True

    with (
        patch("OpenGL.GL.glActiveTexture"),
        patch("OpenGL.GL.glBindTexture"),
        patch("OpenGL.GL.glUseProgram"),
        patch("OpenGL.GL.glUniform1i") as uniform1i,
        patch("OpenGL.GL.glGetUniformLocation") as get_location,
    ):
        get_location.side_effect = (
            lambda glo, name: {"u_y_plane": 11, "u_uv_plane": 22}.get(name)
        )
        ok = render_pass.render(
            (
                _plane("Y", 101, "2d", "GL_R8"),
                _plane("UV", 102, "2d", "GL_RG8"),
            ),
            SimpleNamespace(color_range="limited", color_matrix="bt709"),
            MagicMock(),
            64,
            64,
        )
    assert ok is True
    assigned = {call.args for call in uniform1i.call_args_list}
    assert (11, 0) in assigned  # u_y_plane → unit 0
    assert (22, 1) in assigned  # u_uv_plane → unit 1


def test_external_yuv_pass_rect_path_also_assigns_sampler_units():
    """The sampler2DRect program needs the same unit assignment."""
    ctx = MagicMock()
    render_pass = ExternalYuvToRgbPass(ctx)
    assert render_pass.initialize() is True

    with (
        patch("OpenGL.GL.glActiveTexture"),
        patch("OpenGL.GL.glBindTexture"),
        patch("OpenGL.GL.glUseProgram"),
        patch("OpenGL.GL.glUniform1i") as uniform1i,
        patch("OpenGL.GL.glGetUniformLocation") as get_location,
    ):
        get_location.side_effect = (
            lambda glo, name: {"u_y_plane": 11, "u_uv_plane": 22}.get(name)
        )
        ok = render_pass.render(
            (
                _plane("Y", 101, "rectangle", "GL_R8"),
                _plane("UV", 102, "rectangle", "GL_RG8"),
            ),
            SimpleNamespace(color_range="limited", color_matrix="bt709"),
            MagicMock(),
            64,
            64,
        )
    assert ok is True
    assigned = {call.args for call in uniform1i.call_args_list}
    assert (11, 0) in assigned
    assert (22, 1) in assigned


def test_external_yuv_pass_la_mode_reads_uv_plane_format():
    """The luminance-alpha legacy profile is a property of the UV plane."""
    ctx = MagicMock()
    render_pass = ExternalYuvToRgbPass(ctx)
    assert render_pass.initialize() is True

    with _patched_gl_calls():
        ok = render_pass.render(
            (
                _plane("Y", 101, "2d", "GL_LUMINANCE"),
                _plane("UV", 102, "2d", "GL_LUMINANCE_ALPHA"),
            ),
            SimpleNamespace(color_range="limited", color_matrix="bt709"),
            MagicMock(),
            64,
            64,
        )
    assert ok is True
    program = ctx.program.return_value
    assigned = {
        call.args[:2]
        for call in program.__setitem__.call_args_list
    }
    assert ("u_uv_component_mode", 1) in assigned
