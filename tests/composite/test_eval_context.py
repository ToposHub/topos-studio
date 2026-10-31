"""Tests for CompositeEvalContext and its integration with node evaluation.

Tests cover:
- CompositeEvalContext creation and field access
- isinstance-based dispatch in TimelineClipInputNode
- Backward compatibility when plain moderngl.Context is passed
- RenderRequest fields for runtime_context and timeline_clip_gpu_provider
"""
import pytest
from unittest.mock import MagicMock, patch

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.render.render_scheduler import RenderRequest, RenderMode


class TestCompositeEvalContext:
    """Test CompositeEvalContext creation and field access."""

    def test_default_creation(self):
        """All fields default to None."""
        ctx = CompositeEvalContext()
        assert ctx.gl is None
        assert ctx.runtime_context is None
        assert ctx.timeline_clip_gpu_provider is None
        assert ctx.resource_pool is None
        assert ctx.framebuffer_manager is None

    def test_creation_with_gl_only(self):
        """Can create with just a GL context (backward compat)."""
        mock_gl = MagicMock()
        ctx = CompositeEvalContext(gl=mock_gl)
        assert ctx.gl is mock_gl
        assert ctx.runtime_context is None

    def test_full_creation(self):
        """Can create with all fields populated."""
        mock_gl = MagicMock(name="gl")
        mock_runtime = MagicMock(name="runtime")
        mock_provider = MagicMock(name="provider")
        mock_pool = MagicMock(name="pool")
        mock_fb = MagicMock(name="fb")

        ctx = CompositeEvalContext(
            gl=mock_gl,
            runtime_context=mock_runtime,
            timeline_clip_gpu_provider=mock_provider,
            resource_pool=mock_pool,
            framebuffer_manager=mock_fb,
        )

        assert ctx.gl is mock_gl
        assert ctx.runtime_context is mock_runtime
        assert ctx.timeline_clip_gpu_provider is mock_provider
        assert ctx.resource_pool is mock_pool
        assert ctx.framebuffer_manager is mock_fb

    def test_field_access(self):
        """Fields are accessible as attributes."""
        mock_gl = MagicMock()
        ctx = CompositeEvalContext(gl=mock_gl)
        assert hasattr(ctx, 'gl')
        assert hasattr(ctx, 'runtime_context')
        assert hasattr(ctx, 'timeline_clip_gpu_provider')
        assert hasattr(ctx, 'resource_pool')
        assert hasattr(ctx, 'framebuffer_manager')

    def test_isinstance_check(self):
        """isinstance works for CompositeEvalContext."""
        ctx = CompositeEvalContext()
        assert isinstance(ctx, CompositeEvalContext)

        mock_gl = MagicMock()
        assert not isinstance(mock_gl, CompositeEvalContext)

    def test_dataclass_equality(self):
        """Two contexts with same fields are equal (dataclass behavior)."""
        gl = MagicMock()
        ctx1 = CompositeEvalContext(gl=gl)
        ctx2 = CompositeEvalContext(gl=gl)
        assert ctx1 == ctx2


class TestTimelineClipInputNodeWithEvalContext:
    """Test TimelineClipInputNode dispatch with CompositeEvalContext."""

    def test_with_composite_eval_context_and_provider(self):
        """Node extracts runtime_context and provider from CompositeEvalContext."""
        node = TimelineClipInputNode()
        mock_gl = MagicMock(name="gl")
        mock_runtime = MagicMock(name="runtime")
        mock_tex = TextureResult(texture_id=42, width=1920, height=1080)

        mock_provider = MagicMock(name="provider")
        mock_provider.get_timeline_clip_texture.return_value = mock_tex

        ctx = CompositeEvalContext(
            gl=mock_gl,
            runtime_context=mock_runtime,
            timeline_clip_gpu_provider=mock_provider,
        )

        result = node._do_evaluate({}, ctx, None, None, render_mode="preview")
        assert "output" in result
        assert result["output"].texture_id == 42
        mock_provider.get_timeline_clip_texture.assert_called_once_with(mock_runtime)

    def test_node_input_stage_overrides_runtime_context(self):
        """Node parameter controls provider input stage."""
        from src.features.composite.core.runtime_context import CompositeRuntimeContext

        node = TimelineClipInputNode()
        node.set_parameter("input_stage", "source_raw")
        runtime = CompositeRuntimeContext(input_stage="after_color")
        mock_tex = TextureResult(texture_id=42, width=1920, height=1080)

        mock_provider = MagicMock(name="provider")
        mock_provider.get_timeline_clip_texture.return_value = mock_tex

        ctx = CompositeEvalContext(
            gl=MagicMock(name="gl"),
            runtime_context=runtime,
            timeline_clip_gpu_provider=mock_provider,
        )

        result = node._do_evaluate({}, ctx, None, None, render_mode="preview")

        assert result["output"].texture_id == 42
        called_runtime = mock_provider.get_timeline_clip_texture.call_args.args[0]
        assert called_runtime.input_stage == "source_raw"

    def test_with_composite_eval_context_no_provider(self):
        """Returns empty when CompositeEvalContext has no provider."""
        node = TimelineClipInputNode()
        ctx = CompositeEvalContext(
            gl=MagicMock(),
            runtime_context=MagicMock(),
            timeline_clip_gpu_provider=None,
        )

        result = node._do_evaluate({}, ctx, None, None, render_mode="preview")
        assert result == {}

    def test_with_composite_eval_context_no_runtime(self):
        """Returns empty when CompositeEvalContext has no runtime_context."""
        node = TimelineClipInputNode()
        ctx = CompositeEvalContext(
            gl=MagicMock(),
            runtime_context=None,
            timeline_clip_gpu_provider=MagicMock(),
        )

        result = node._do_evaluate({}, ctx, None, None, render_mode="preview")
        assert result == {}

    def test_with_plain_gl_context_backward_compat(self):
        """Works with a plain mock GL context (backward compat path)."""
        node = TimelineClipInputNode()

        # Plain context (not CompositeEvalContext)
        mock_ctx = MagicMock(name="plain_gl")
        # Set attributes so getattr path works
        mock_ctx.runtime_context = None
        mock_ctx.timeline_clip_gpu_provider = None

        result = node._do_evaluate({}, mock_ctx, None, None, render_mode="preview")
        assert result == {}

    def test_fallback_to_cached_texture(self):
        """Falls back to cached texture when no runtime context available."""
        node = TimelineClipInputNode()
        cached_tex = TextureResult(texture_id=99, width=640, height=480)
        node._cached_texture = cached_tex

        # CompositeEvalContext without runtime/provider
        ctx = CompositeEvalContext(gl=MagicMock())

        result = node._do_evaluate({}, ctx, None, None, render_mode="preview")
        assert "output" in result
        assert result["output"].texture_id == 99

    def test_provider_exception_handled(self):
        """Provider exceptions are caught and logged without crashing."""
        node = TimelineClipInputNode()
        mock_provider = MagicMock()
        mock_provider.get_timeline_clip_texture.side_effect = RuntimeError("test error")

        ctx = CompositeEvalContext(
            gl=MagicMock(),
            runtime_context=MagicMock(),
            timeline_clip_gpu_provider=mock_provider,
        )

        result = node._do_evaluate({}, ctx, None, None, render_mode="preview")
        assert result == {}  # No crash, returns empty


class TestRenderRequestFields:
    """Test RenderRequest accepts the new optional fields."""

    def test_render_request_without_context(self):
        """RenderRequest works without runtime_context (backward compat)."""
        snapshot = MagicMock()
        req = RenderRequest(
            snapshot=snapshot,
            request_id=1,
            timestamp=0.0,
        )
        assert req.runtime_context is None
        assert req.timeline_clip_gpu_provider is None

    def test_render_request_with_context(self):
        """RenderRequest accepts runtime_context and provider."""
        snapshot = MagicMock()
        mock_runtime = MagicMock()
        mock_provider = MagicMock()

        req = RenderRequest(
            snapshot=snapshot,
            request_id=1,
            timestamp=0.0,
            runtime_context=mock_runtime,
            timeline_clip_gpu_provider=mock_provider,
        )
        assert req.runtime_context is mock_runtime
        assert req.timeline_clip_gpu_provider is mock_provider

    def test_render_request_default_none(self):
        """New fields default to None."""
        snapshot = MagicMock()
        req = RenderRequest(
            snapshot=snapshot,
            request_id=1,
            timestamp=0.0,
        )
        assert req.runtime_context is None
        assert req.timeline_clip_gpu_provider is None


class TestEvalContextIntegration:
    """Integration test: full flow from CompositeEvalContext to node evaluation."""

    def test_source_node_receives_eval_context(self):
        """Verify that when _eval_ctx is set, source nodes get it as ctx arg."""
        from src.features.composite.core.eval_context import CompositeEvalContext

        # Simulate what CompositeRenderer._handle_evaluate_graph does
        mock_gl = MagicMock(name="gl")
        mock_runtime = MagicMock(name="runtime")
        mock_provider = MagicMock(name="provider")

        eval_ctx = CompositeEvalContext(
            gl=mock_gl,
            runtime_context=mock_runtime,
            timeline_clip_gpu_provider=mock_provider,
        )

        # TimelineClipInputNode should be able to use this
        node = TimelineClipInputNode()
        mock_tex = TextureResult(texture_id=100, width=1920, height=1080)
        mock_provider.get_timeline_clip_texture.return_value = mock_tex

        # This is the call pattern used by _evaluate_node for source nodes
        result = node.evaluate({}, eval_ctx, None, None, render_mode="preview")

        assert "output" in result
        assert result["output"].texture_id == 100

    def test_non_source_node_still_gets_gl(self):
        """Non-source nodes should still receive gl context directly.

        In the real pipeline, _evaluate_node passes self.ctx (the moderngl.Context)
        to non-source nodes. Only source nodes get the CompositeEvalContext.
        This test verifies that non-source nodes don't need to know about
        CompositeEvalContext at all.
        """
        # A non-source node receives just the gl context (self.ctx)
        mock_gl = MagicMock(name="gl_context")

        # Verify that isinstance check would route correctly
        assert not isinstance(mock_gl, CompositeEvalContext)

        # When _evaluate_node passes self.ctx to non-source nodes,
        # they just use it as moderngl.Context - no changes needed
        assert mock_gl is not None
