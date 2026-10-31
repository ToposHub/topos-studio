"""
AsyncRenderer Lane 合并测试 (P2-3)

## 测试目标

验证 RenderLane 枚举和 lane-based 命令合并逻辑：
1. RENDER_FRAME 根据 render_reason 分配到不同 lane
2. RENDER_VIDEO_FRAME proxy/playback 分配到 video_playback lane
3. viewport_refresh 最后执行确保最新 viewport 状态
4. critical 命令不被丢弃
5. lane 合并统计正确

## 测试覆盖

- RenderLane 枚举定义
- _get_lane_for_command() 返回正确 lane
- _drain_pending_slots() 执行顺序正确
- submit() lane 合并逻辑
- critical 命令不被丢弃

"""

from __future__ import annotations

import pytest
import threading
import queue
from typing import Optional, Any, Dict, Tuple, List
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

# 导入核心模块
from src.features.color.core.async_renderer import AsyncRenderer, RenderCommand, RenderLane


# =============================================================================
# Test RenderLane Enum
# =============================================================================

class TestRenderLaneEnum:
    """P2-3: 测试 RenderLane 枚举定义"""

    def test_render_lane_enum_exists(self):
        """RenderLane 枚举应存在"""
        assert RenderLane is not None

    def test_render_lane_has_video_playback(self):
        """RenderLane 应有 VIDEO_PLAYBACK"""
        assert hasattr(RenderLane, 'VIDEO_PLAYBACK')
        assert RenderLane.VIDEO_PLAYBACK.value == "video_playback"

    def test_render_lane_has_viewport_refresh(self):
        """RenderLane 应有 VIEWPORT_REFRESH"""
        assert hasattr(RenderLane, 'VIEWPORT_REFRESH')
        assert RenderLane.VIEWPORT_REFRESH.value == "viewport_refresh"

    def test_render_lane_has_grading_refresh(self):
        """RenderLane 应有 GRADING_REFRESH"""
        assert hasattr(RenderLane, 'GRADING_REFRESH')
        assert RenderLane.GRADING_REFRESH.value == "grading_refresh"

    def test_render_lane_has_aux_tex(self):
        """RenderLane 应有 AUX_TEX"""
        assert hasattr(RenderLane, 'AUX_TEX')
        assert RenderLane.AUX_TEX.value == "aux_tex"

    def test_render_lane_has_color_output(self):
        """RenderLane 应有 COLOR_OUTPUT"""
        assert hasattr(RenderLane, 'COLOR_OUTPUT')
        assert RenderLane.COLOR_OUTPUT.value == "color_output"

    def test_render_lane_has_scope_snapshot(self):
        """RenderLane 应有 SCOPE_SNAPSHOT"""
        assert hasattr(RenderLane, 'SCOPE_SNAPSHOT')
        assert RenderLane.SCOPE_SNAPSHOT.value == "scope_snapshot"

    def test_render_lane_count(self):
        """RenderLane 应有 7 个 lane (GPU_SCOPE added for native scope pipeline)"""
        assert len(RenderLane) == 7

    def test_render_lane_has_gpu_scope(self):
        """RenderLane 应有 GPU_SCOPE (native GPU scope, lowest priority)"""
        assert hasattr(RenderLane, 'GPU_SCOPE')
        assert RenderLane.GPU_SCOPE.value == "gpu_scope"


# =============================================================================
# Test _get_lane_for_command
# =============================================================================

class TestGetLaneForCommand:
    """P2-3: 测试 _get_lane_for_command 方法"""

    def _call_get_lane(self, cmd_type: str, data: Any) -> Optional[RenderLane]:
        """调用真实的 _get_lane_for_command 方法"""
        # 使用真实的 AsyncRenderer 方法
        return AsyncRenderer._get_lane_for_command(None, cmd_type, data)

    def test_render_video_frame_proxy_playback_to_video_playback_lane(self):
        """RENDER_VIDEO_FRAME proxy+playback 应分配到 VIDEO_PLAYBACK lane"""
        data = {
            'quality': 'proxy',
            'reason': 'playback',
            'drop_if_stale': True,
        }
        lane = self._call_get_lane(RenderCommand.RENDER_VIDEO_FRAME, data)
        assert lane == RenderLane.VIDEO_PLAYBACK

    def test_render_video_frame_master_not_mergeable(self):
        """RENDER_VIDEO_FRAME master 应不被合并"""
        data = {
            'quality': 'master',
            'reason': 'master',
            'drop_if_stale': False,
        }
        lane = self._call_get_lane(RenderCommand.RENDER_VIDEO_FRAME, data)
        assert lane is None


    def test_render_video_frame_seek_not_mergeable(self):
        """RENDER_VIDEO_FRAME seek 应不被合并"""
        data = {
            'quality': 'proxy',
            'reason': 'seek',
            'drop_if_stale': False,
        }
        lane = self._call_get_lane(RenderCommand.RENDER_VIDEO_FRAME, data)
        assert lane is None

    def test_render_frame_viewport_refresh_to_viewport_lane(self):
        """RENDER_FRAME viewport_refresh 应分配到 VIEWPORT_REFRESH lane"""
        data = {'render_reason': 'viewport_refresh'}
        lane = self._call_get_lane(RenderCommand.RENDER_FRAME, data)
        assert lane == RenderLane.VIEWPORT_REFRESH

    def test_render_frame_grading_to_grading_lane(self):
        """RENDER_FRAME grading 应分配到 GRADING_REFRESH lane"""
        data = {'render_reason': 'grading'}
        lane = self._call_get_lane(RenderCommand.RENDER_FRAME, data)
        assert lane == RenderLane.GRADING_REFRESH

    def test_render_frame_full_not_mergeable(self):
        """RENDER_FRAME full 应不被合并"""
        data = {'render_reason': 'full'}
        lane = self._call_get_lane(RenderCommand.RENDER_FRAME, data)
        assert lane is None

    def test_full_resolution_export_is_critical_and_not_mergeable(self):
        """Full-res readback commands must never be coalesced or dropped."""
        lane = self._call_get_lane(
            RenderCommand.RENDER_FULL_RESOLUTION,
            {'render_request_id': 'render-00000001'},
        )
        assert lane is None

    def test_render_frame_playback_not_mergeable(self):
        """RENDER_FRAME playback 应不被合并"""
        data = {'render_reason': 'playback'}
        lane = self._call_get_lane(RenderCommand.RENDER_FRAME, data)
        assert lane is None

    def test_update_aux_tex_to_aux_tex_lane(self):
        """UPDATE_AUX_TEX 按 (layer_id, type) 细分 AUX 子槽（单槽会丢更新）"""
        lane = self._call_get_lane(RenderCommand.UPDATE_AUX_TEX, {})
        assert isinstance(lane, tuple)
        assert lane[0] == RenderLane.AUX_TEX
        lane_a = self._call_get_lane(
            RenderCommand.UPDATE_AUX_TEX,
            {'layer_id': 'L1', 'type': 'curves'})
        lane_b = self._call_get_lane(
            RenderCommand.UPDATE_AUX_TEX,
            {'layer_id': 'L1', 'type': 'lut3d'})
        lane_c = self._call_get_lane(
            RenderCommand.UPDATE_AUX_TEX,
            {'layer_id': 'L2', 'type': 'curves'})
        # 不同资源不同槽位：互相不会覆盖
        assert len({lane_a, lane_b, lane_c}) == 3

    def test_render_color_output_to_color_output_lane(self):
        """RENDER_COLOR_OUTPUT 应分配到 COLOR_OUTPUT lane"""
        lane = self._call_get_lane(RenderCommand.RENDER_COLOR_OUTPUT, {})
        assert lane == RenderLane.COLOR_OUTPUT

    def test_upload_image_not_mergeable(self):
        """UPLOAD_IMAGE 应不被合并"""
        lane = self._call_get_lane(RenderCommand.UPLOAD_IMAGE, {})
        assert lane is None

    def test_bake_layer_not_mergeable(self):
        """BAKE_LAYER 应不被合并"""
        lane = self._call_get_lane(RenderCommand.BAKE_LAYER, {})
        assert lane is None

    def test_remove_layer_not_mergeable(self):
        """REMOVE_LAYER 应不被合并"""
        lane = self._call_get_lane(RenderCommand.REMOVE_LAYER, {})
        assert lane is None

    def test_upload_video_frame_not_mergeable(self):
        """UPLOAD_VIDEO_FRAME 应不被合并"""
        lane = self._call_get_lane(RenderCommand.UPLOAD_VIDEO_FRAME, {})
        assert lane is None


def test_full_resolution_request_cancellation_is_scoped_and_reversible():
    """Cancelling one export must not set the renderer-wide stop event."""
    renderer = AsyncRenderer.__new__(AsyncRenderer)

    assert renderer.register_full_resolution("render-test") is True
    assert renderer.cancel_full_resolution("render-test") is True
    assert renderer._full_resolution_cancelled("render-test") is True
    assert renderer._cancel_requested() is False

    renderer.forget_full_resolution("render-test")
    assert renderer._full_resolution_cancelled("render-test") is False
    assert renderer.cancel_full_resolution("render-test") is False


def test_full_resolution_pbo_enqueue_tracks_pending_dimensions():
    """PBO submissions retain dimensions until the render thread polls them."""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    renderer._full_res_pbo_disabled = False
    renderer._full_res_pbo_readback = Mock()
    renderer._full_res_pbo_readback.enqueue.return_value = True
    renderer._full_res_pending_readbacks = {}
    renderer.ctx = Mock()

    assert renderer._enqueue_full_resolution_readback(
        "render-pbo", Mock(), 1920, 1080
    ) is True
    assert renderer._full_res_pending_readbacks == {
        "render-pbo": (1920, 1080)
    }
    renderer._full_res_pbo_readback.enqueue.assert_called_once()


def test_full_resolution_pbo_resize_waits_only_for_different_dimensions():
    """Same-size batches stay async; a size change drains the prior DMA."""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    renderer._full_res_pending_readbacks = {"render-pbo": (1920, 1080)}
    renderer.ctx = Mock()
    renderer._poll_full_resolution_readbacks = Mock(
        side_effect=lambda: renderer._full_res_pending_readbacks.clear()
    )
    renderer._full_res_pbo_readback = Mock()
    renderer._full_res_pbo_disabled = False

    renderer._synchronize_full_res_pbo_for_resize(1920, 1080)
    renderer.ctx.finish.assert_not_called()

    renderer._synchronize_full_res_pbo_for_resize(1280, 720)
    renderer.ctx.finish.assert_called_once_with()
    renderer._poll_full_resolution_readbacks.assert_called_once_with()


def test_full_resolution_pbo_shutdown_drains_before_releasing_resources():
    """Renderer shutdown gives in-flight readbacks one final render-thread poll."""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    renderer._full_res_pending_readbacks = {"render-pbo": (1920, 1080)}
    renderer.ctx = Mock()
    renderer._full_res_pbo_readback = Mock()
    renderer._poll_full_resolution_readbacks = Mock(
        side_effect=lambda: renderer._full_res_pending_readbacks.clear()
    )

    renderer._drain_full_resolution_readbacks_for_shutdown()

    renderer.ctx.finish.assert_called_once_with()
    renderer._poll_full_resolution_readbacks.assert_called_once_with()
    assert renderer._full_res_pending_readbacks == {}


# =============================================================================
# Test _drain_pending_slots Order
# =============================================================================

class TestDrainPendingSlotsOrder:
    """P2-3: 测试 _drain_pending_slots 执行顺序"""

    def setup_method(self):
        """每个测试方法前创建模拟 renderer"""
        self._pending_slots = {lane: None for lane in RenderLane}
        self._pending_aux_keys = []
        self._pending_lock = threading.Lock()

    def _call_drain(self) -> List[Tuple[str, Any]]:
        """调用真实的 _drain_pending_slots 方法"""
        # 创建模拟 renderer 并设置属性
        renderer = Mock()
        renderer._pending_slots = self._pending_slots
        renderer._pending_aux_keys = self._pending_aux_keys
        renderer._pending_lock = self._pending_lock
        return AsyncRenderer._drain_pending_slots(renderer)

    def test_drain_order_viewport_last(self):
        """viewport_refresh 应最后执行"""
        # 设置所有 lane 都有命令（AUX 用 per-resource 子槽键）
        aux_key = (RenderLane.AUX_TEX, 'L1', 'curves')
        self._pending_slots[aux_key] = ('aux_cmd', {'data': 'aux'})
        self._pending_aux_keys.append(aux_key)
        self._pending_slots[RenderLane.GRADING_REFRESH] = ('grading_cmd', {'data': 'grading'})
        self._pending_slots[RenderLane.VIDEO_PLAYBACK] = ('video_cmd', {'data': 'video'})
        self._pending_slots[RenderLane.VIEWPORT_REFRESH] = ('viewport_cmd', {'data': 'viewport'})
        self._pending_slots[RenderLane.COLOR_OUTPUT] = ('color_cmd', {'data': 'color'})

        commands = self._call_drain()

        # 验证顺序
        assert len(commands) == 5
        assert commands[0][0] == 'aux_cmd'  # AUX_TEX first
        assert commands[1][0] == 'grading_cmd'  # GRADING_REFRESH second
        assert commands[2][0] == 'video_cmd'  # VIDEO_PLAYBACK third
        assert commands[3][0] == 'viewport_cmd'  # VIEWPORT_REFRESH fourth (before color_output)
        assert commands[4][0] == 'color_cmd'  # COLOR_OUTPUT last

    def test_drain_keeps_distinct_aux_resources(self):
        """不同资源的 AUX 更新必须都保留（回归：单槽合并丢更新）"""
        key1 = (RenderLane.AUX_TEX, 'L1', 'curves')
        key2 = (RenderLane.AUX_TEX, 'L1', 'lut3d')
        key3 = (RenderLane.AUX_TEX, 'L2', 'mask')
        for key, tag in ((key1, 'c'), (key2, 'l'), (key3, 'm')):
            self._pending_slots[key] = (f'aux_{tag}', {})
            self._pending_aux_keys.append(key)
        # 同一资源重复提交：后到替换先到（合并语义保留）
        self._pending_slots[key1] = ('aux_c2', {})

        commands = self._call_drain()

        tags = [cmd[0] for cmd in commands]
        assert tags == ['aux_c2', 'aux_l', 'aux_m']
        # drain 后子槽清空，可再次占用
        assert self._pending_slots.get(key1) is None
        assert self._pending_aux_keys == []

    def test_drain_order_video_before_viewport(self):
        """video_playback 应在 viewport_refresh 之前执行"""
        # 只设置 video 和 viewport
        self._pending_slots[RenderLane.VIDEO_PLAYBACK] = ('video_cmd', {'frame': 1})
        self._pending_slots[RenderLane.VIEWPORT_REFRESH] = ('viewport_cmd', {'zoom': 2.0})

        commands = self._call_drain()

        assert len(commands) == 2
        assert commands[0][0] == 'video_cmd'  # video first
        assert commands[1][0] == 'viewport_cmd'  # viewport second (last)

    def test_drain_resets_slots_to_none(self):
        """drain 后所有 slot 应重置为 None"""
        self._pending_slots[RenderLane.VIDEO_PLAYBACK] = ('video_cmd', {})
        self._pending_slots[RenderLane.VIEWPORT_REFRESH] = ('viewport_cmd', {})

        self._call_drain()

        for lane in RenderLane:
            assert self._pending_slots[lane] is None

    def test_drain_empty_slots_returns_empty_list(self):
        """所有 slot 为空时应返回空列表"""
        commands = self._call_drain()
        assert commands == []

    def test_discard_pending_preview_refreshes_keeps_video_frame(self):
        """Playback start discards UI redraws but retains the latest video frame."""
        renderer = Mock()
        renderer._pending_slots = self._pending_slots
        renderer._pending_lock = self._pending_lock
        self._pending_slots[RenderLane.GRADING_REFRESH] = ('grading', {})
        self._pending_slots[RenderLane.VIEWPORT_REFRESH] = ('viewport', {})
        self._pending_slots[RenderLane.VIDEO_PLAYBACK] = ('video', {'frame': 42})

        AsyncRenderer.discard_pending_preview_refreshes(renderer)

        assert self._pending_slots[RenderLane.GRADING_REFRESH] is None
        assert self._pending_slots[RenderLane.VIEWPORT_REFRESH] is None
        assert self._pending_slots[RenderLane.VIDEO_PLAYBACK] == ('video', {'frame': 42})


# =============================================================================
# Test Lane Merge Logic via submit()
# =============================================================================

class TestLaneMergeLogic:
    """P2-3: 测试 lane 合并逻辑"""

    def setup_method(self):
        """每个测试方法前创建模拟 renderer"""
        self._pending_slots = {lane: None for lane in RenderLane}
        self._pending_lock = threading.Lock()
        self._drain_pending_signaled = False
        self._drain_pending_signal_lock = threading.Lock()
        self._coalesced_lane_commands = 0
        self._command_queue_put_calls = []

        # 模拟 command_queue.put
        def mock_put(item):
            self._command_queue_put_calls.append(item)

        self._mock_queue = Mock()
        self._mock_queue.put = mock_put

    def _call_submit(self, cmd_type: str, data: Any = None):
        """调用真实的 submit 方法"""
        renderer = Mock()
        renderer._pending_slots = self._pending_slots
        renderer._pending_lock = self._pending_lock
        renderer._drain_pending_signaled = self._drain_pending_signaled
        renderer._drain_pending_signal_lock = self._drain_pending_signal_lock
        renderer._coalesced_lane_commands = self._coalesced_lane_commands
        renderer.command_queue = self._mock_queue

        # 使用真实的 _get_lane_for_command 方法
        renderer._get_lane_for_command = AsyncRenderer._get_lane_for_command.__get__(renderer, AsyncRenderer)

        # 调用真实方法
        AsyncRenderer.submit(renderer, cmd_type, data)

        # 更新状态
        self._pending_slots = renderer._pending_slots
        self._coalesced_lane_commands = renderer._coalesced_lane_commands
        self._drain_pending_signaled = renderer._drain_pending_signaled

    def test_mergeable_command_updates_slot(self):
        """mergeable 命令应更新 slot"""
        data = {'render_reason': 'viewport_refresh'}

        self._call_submit(RenderCommand.RENDER_FRAME, data)

        # slot 应被更新
        assert self._pending_slots[RenderLane.VIEWPORT_REFRESH] == (RenderCommand.RENDER_FRAME, data)

    def test_mergeable_command_replaces_old(self):
        """新 mergeable 命令应替换旧命令"""
        data1 = {'render_reason': 'viewport_refresh', 'zoom': 1.0}
        data2 = {'render_reason': 'viewport_refresh', 'zoom': 2.0}

        self._call_submit(RenderCommand.RENDER_FRAME, data1)
        self._call_submit(RenderCommand.RENDER_FRAME, data2)

        # slot 应包含最新命令
        assert self._pending_slots[RenderLane.VIEWPORT_REFRESH] == (RenderCommand.RENDER_FRAME, data2)
        # coalesced count 应增加
        assert self._coalesced_lane_commands == 1

    def test_scope_snapshot_command_uses_its_merge_lane(self):
        """Scope snapshots are coalesced instead of accumulating readbacks."""
        data = {'max_dim': 512}

        self._call_submit(RenderCommand.RENDER_SCOPE, data)

        assert self._pending_slots[RenderLane.SCOPE_SNAPSHOT] == (
            RenderCommand.RENDER_SCOPE, data,
        )

    def test_critical_command_enqueued_directly(self):
        """critical 命令应直接进入队列"""
        data = {'path': '/test/image.jpg'}

        self._call_submit(RenderCommand.UPLOAD_IMAGE, data)

        # 命令应直接进入队列
        assert len(self._command_queue_put_calls) == 1
        assert self._command_queue_put_calls[0] == (RenderCommand.UPLOAD_IMAGE, data)

    def test_critical_command_not_in_slot(self):
        """critical 命令不应更新 slot"""
        data = {'path': '/test/image.jpg'}

        self._call_submit(RenderCommand.UPLOAD_IMAGE, data)

        # slot 应保持为空
        for lane in RenderLane:
            assert self._pending_slots[lane] is None

    def test_different_lanes_not_replace_each_other(self):
        """不同 lane 的命令不应互相替换"""
        video_data = {'quality': 'proxy', 'reason': 'playback', 'drop_if_stale': True}
        viewport_data = {'render_reason': 'viewport_refresh'}

        self._call_submit(RenderCommand.RENDER_VIDEO_FRAME, video_data)
        self._call_submit(RenderCommand.RENDER_FRAME, viewport_data)

        # 两个 slot 都应有命令
        assert self._pending_slots[RenderLane.VIDEO_PLAYBACK] == (RenderCommand.RENDER_VIDEO_FRAME, video_data)
        assert self._pending_slots[RenderLane.VIEWPORT_REFRESH] == (RenderCommand.RENDER_FRAME, viewport_data)


# =============================================================================
# Test get_lane_states Method
# =============================================================================

class TestGetLaneStates:
    """P2-3: 测试 get_lane_states 方法"""

    def setup_method(self):
        """每个测试方法前创建模拟 renderer"""
        self._pending_slots = {lane: None for lane in RenderLane}
        self._pending_lock = threading.Lock()

    def _call_get_lane_states(self) -> Dict[str, bool]:
        """调用真实的 get_lane_states 方法"""
        renderer = Mock()
        renderer._pending_slots = self._pending_slots
        renderer._pending_lock = self._pending_lock
        return AsyncRenderer.get_lane_states(renderer)

    def test_get_lane_states_returns_dict(self):
        """get_lane_states 应返回字典"""
        states = self._call_get_lane_states()
        assert isinstance(states, dict)

    def test_get_lane_states_all_false_when_empty(self):
        """所有 slot 为空时应返回全 False"""
        states = self._call_get_lane_states()
        for lane_name in ['video_playback', 'viewport_refresh', 'grading_refresh', 'aux_tex', 'color_output', 'scope_snapshot']:
            assert states[lane_name] is False

    def test_get_lane_states_true_when_has_command(self):
        """slot 有命令时应返回 True"""
        self._pending_slots[RenderLane.VIDEO_PLAYBACK] = ('cmd', {})
        self._pending_slots[RenderLane.VIEWPORT_REFRESH] = ('cmd', {})

        states = self._call_get_lane_states()

        assert states['video_playback'] is True
        assert states['viewport_refresh'] is True
        assert states['grading_refresh'] is False
        assert states['aux_tex'] is False
        assert states['color_output'] is False


# =============================================================================
# Test Critical Commands Never Discarded
# =============================================================================

class TestCriticalCommandsNeverDiscarded:
    """P2-3: 测试 critical 命令不被丢弃"""

    def _call_get_lane(self, cmd_type: str, data: Any) -> Optional[RenderLane]:
        """调用真实的 _get_lane_for_command 方法"""
        return AsyncRenderer._get_lane_for_command(None, cmd_type, data)

    def test_seek_command_not_mergeable(self):
        """seek 命令应不被合并"""
        data = {'reason': 'seek', 'drop_if_stale': False}
        lane = self._call_get_lane(RenderCommand.RENDER_VIDEO_FRAME, data)
        assert lane is None

    def test_master_command_not_mergeable(self):
        """master 命令应不被合并"""
        data = {'quality': 'master', 'reason': 'master', 'drop_if_stale': False}
        lane = self._call_get_lane(RenderCommand.RENDER_VIDEO_FRAME, data)
        assert lane is None

    def test_export_command_not_mergeable(self):
        """export 命令应不被合并"""
        # RENDER_FRAME with 'full' reason
        data = {'render_reason': 'full'}
        lane = self._call_get_lane(RenderCommand.RENDER_FRAME, data)
        assert lane is None

    def test_upload_image_not_mergeable(self):
        """image upload 命令应不被合并"""
        lane = self._call_get_lane(RenderCommand.UPLOAD_IMAGE, {})
        assert lane is None

    def test_bake_layer_not_mergeable(self):
        """bake 命令应不被合并"""
        lane = self._call_get_lane(RenderCommand.BAKE_LAYER, {})
        assert lane is None

    def test_upload_baked_texture_not_mergeable(self):
        """upload baked texture 命令应不被合并"""
        lane = self._call_get_lane(RenderCommand.UPLOAD_BAKED_TEXTURE, {})
        assert lane is None

    def test_clear_baked_texture_not_mergeable(self):
        """解冻必须按顺序释放对应的 baked texture，不能被合并丢弃。"""
        lane = self._call_get_lane(RenderCommand.CLEAR_BAKED_TEXTURE, {})
        assert lane is None

    def test_remove_layer_not_mergeable(self):
        """remove layer 命令应不被合并"""
        lane = self._call_get_lane(RenderCommand.REMOVE_LAYER, {})
        assert lane is None

    def test_upload_ai_plugin_result_not_mergeable(self):
        """upload AI plugin result 命令应不被合并"""
        lane = self._call_get_lane(RenderCommand.UPLOAD_AI_PLUGIN_RESULT, {})
        assert lane is None


# =============================================================================
# Integration Test with Real AsyncRenderer (Mocked OpenGL)
# =============================================================================

class TestRealAsyncRendererLaneIntegration:
    """真实 AsyncRenderer lane 集成测试"""

    def test_async_renderer_has_render_lane_import(self):
        """AsyncRenderer 应导入 RenderLane"""
        from src.features.color.core.async_renderer import AsyncRenderer, RenderLane
        assert RenderLane is not None


class TestBakedLayerHelpers:
    def test_clear_baked_texture_releases_only_the_requested_texture(self):
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        kept = MagicMock()
        removed = MagicMock()
        renderer.baked_textures = {"keep": kept, "remove": removed}

        AsyncRenderer._do_clear_baked_texture(renderer, "remove")

        removed.release.assert_called_once_with()
        kept.release.assert_not_called()
        assert renderer.baked_textures == {"keep": kept}

    def test_baked_view_uses_preview_coordinate_space_for_full_resolution_bake(self):
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.pyramid_textures = [MagicMock(size=(1000, 500))]

        result = AsyncRenderer._baked_layer_coordinate_size(
            renderer, (4000, 2000), 0
        )

        assert result == (1000, 500)

    def test_baked_full_resolution_texture_uses_preview_tile_uvs(self):
        """A 4x bake must not cover only a quarter of the preview tiles."""
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.tiles = [SimpleNamespace(level=0, x=0, y=0, w=500, h=250)]
        renderer._render_tile_raw_uv = MagicMock()
        program = MagicMock()

        AsyncRenderer._render_tiles_to_current_fbo(
            renderer,
            MagicMock(size=(4000, 2000)),
            program,
            {"visible_rect": (0, 0, 1000, 500)},
            0,
            coordinate_size=(1000, 500),
        )

        renderer._render_tile_raw_uv.assert_called_once_with(
            program, -1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5
        )

    def test_async_renderer_pending_slots_uses_render_lane(self):
        """AsyncRenderer._pending_slots 应使用 RenderLane 作为 key"""
        # 验证类型注解（通过检查类定义）
        import inspect
        source = inspect.getsource(AsyncRenderer.__init__)

        # 检查源代码中使用了 RenderLane 枚举
        assert 'RenderLane.VIDEO_PLAYBACK' in source or 'RenderLane' in source

    def test_async_renderer_has_get_lane_states(self):
        """AsyncRenderer 应有 get_lane_states 方法"""
        assert hasattr(AsyncRenderer, 'get_lane_states')

    def test_async_renderer_has_get_renderer_stats(self):
        """AsyncRenderer 应有 get_renderer_stats 方法"""
        assert hasattr(AsyncRenderer, 'get_renderer_stats')

    def test_async_renderer_has_reset_renderer_stats(self):
        """AsyncRenderer 应有 reset_renderer_stats 方法"""
        assert hasattr(AsyncRenderer, 'reset_renderer_stats')


class TestQueuedGpuFrameCleanup:
    """Stopping the render thread must not strand zero-copy frame backings."""

    def test_cleanup_releases_lane_and_direct_queue_gpu_frames(self) -> None:
        lane_frame = object()
        direct_frame = object()
        command_queue: queue.Queue = queue.Queue()
        command_queue.put((
            RenderCommand.RENDER_VIDEO_GPU_FRAME,
            {'gpu_decoded_frame': direct_frame},
        ))
        command_queue.put(('discarded_command', {}))
        renderer = Mock()
        renderer._pending_lock = threading.Lock()
        renderer._pending_slots = {lane: None for lane in RenderLane}
        renderer._pending_slots[RenderLane.VIDEO_PLAYBACK] = (
            RenderCommand.RENDER_VIDEO_GPU_FRAME,
            {'gpu_decoded_frame': lane_frame},
        )
        renderer._drain_pending_signal_lock = threading.Lock()
        renderer._drain_pending_signaled = True
        renderer.command_queue = command_queue

        with (
            patch(
                'src.features.color.core.async_renderer.is_gpu_decoded_frame',
                side_effect=lambda frame: frame in (lane_frame, direct_frame),
            ),
            patch(
                'src.features.color.core.async_renderer.safe_release_gpu_frame',
            ) as release,
        ):
            AsyncRenderer._release_queued_gpu_frames(renderer)

        assert [call.args[0] for call in release.call_args_list] == [
            lane_frame,
            direct_frame,
        ]
        assert all(entry is None for entry in renderer._pending_slots.values())
        assert renderer._drain_pending_signaled is False
        command_queue.join()


# =============================================================================
# G-P0-11: Command Loop Exception Safety
# =============================================================================

class TestCommandLoopExceptionSafety:
    """G-P0-11: 命令循环异常安全。

    验证 run() 的任务循环在单条命令 handler 抛错时：
    1. command_queue.task_done() 仍然被调用（否则 command_queue.join() 永久阻塞）；
    2. 队列中后续命令继续被处理（不被一条坏命令静默丢弃）。

    本测试在真实线程中跑 run()，但把 GL 初始化路径全部 patch 成 no-op，
    只验证命令调度的异常隔离契约，不依赖真实 OpenGL。
    """

    def _make_loop_runner(self):
        """构造一个跳过 GL 初始化、可直接跑 run() 的 AsyncRenderer。"""
        from PyQt6.QtCore import QThread
        import queue as _queue
        import threading

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        QThread.__init__(renderer)
        renderer.command_queue = _queue.Queue()
        renderer.is_running = True
        renderer.shared_pyqt_context = MagicMock()
        renderer.shared_pyqt_context.makeCurrent.return_value = True
        renderer.surface = MagicMock()
        renderer._renderer_stats = {'processed_frames': 0}
        renderer._drain_pending_signal_lock = threading.Lock()
        renderer._drain_pending_signaled = False
        # _cleanup() 在 run() 退出时被调用；提供它需要的空容器，避免 AttributeError。
        renderer.ctx = None
        renderer.matte_cache = {}
        renderer._matte_viewport_textures = {}
        renderer.layer_textures = {}
        return renderer

    def _run_with_mocked_gl(self, renderer):
        """用 mocked GL 前缀启动 run()，返回所在线程。"""
        import threading
        from unittest.mock import patch
        import moderngl  # noqa: F401  (确保 import 路径可被 patch)

        with patch('src.features.color.core.async_renderer.moderngl') as mg, \
                patch.object(AsyncRenderer, '_load_utility_shaders'), \
                patch.object(AsyncRenderer, '_create_identity_lut'), \
                patch.object(AsyncRenderer, '_create_defaults'), \
                patch.object(AsyncRenderer, '_run_yuv_gpu_self_test'), \
                patch('src.features.color.core.async_renderer.FramebufferManager'), \
                patch('src.shared.video.video_texture_pool.VideoTexturePool'), \
                patch('src.shared.video.yuv_gpu_upload.YUVGPUUploader'), \
                patch.object(AsyncRenderer, '_cleanup'):
            # 让 run() 里的 self.ctx = moderngl.create_context() 得到 None，
            # 这样 _cleanup() 的 `if self.ctx:` 早退，无需补全所有清理容器。
            mg.create_context.return_value = None
            t = threading.Thread(target=renderer.run)
            t.start()
            return t

    def test_failing_command_does_not_block_join(self):
        """一条抛错的命令不应让 command_queue.join() 永久挂起。"""
        import time

        renderer = self._make_loop_runner()

        def failing_handle(cmd_type, data):
            if cmd_type == 'FAIL':
                raise RuntimeError('simulated handler failure')

        renderer._handle_command = failing_handle
        t = self._run_with_mocked_gl(renderer)
        try:
            renderer.command_queue.put(('FAIL', None))
            # 给 worker 时间处理；join 必须能返回（task_done 已在 finally 中调用）。
            renderer.command_queue.join()
        finally:
            renderer.is_running = False
            renderer.command_queue.put(('__wake__', None))
            t.join(timeout=2.0)
        assert not t.is_alive()

    def test_failing_command_does_not_drop_subsequent_commands(self):
        """一条抛错的命令不应让后续队列命令被丢弃。"""
        import time

        renderer = self._make_loop_runner()
        handled = []

        def handle(cmd_type, data):
            handled.append(cmd_type)
            if cmd_type == 'FAIL':
                raise RuntimeError('simulated handler failure')

        renderer._handle_command = handle
        t = self._run_with_mocked_gl(renderer)
        try:
            renderer.command_queue.put(('FAIL', None))
            renderer.command_queue.put(('OK1', None))
            renderer.command_queue.put(('OK2', None))
            renderer.command_queue.join()
            # 失败命令之后的命令必须都被处理。
            assert 'OK1' in handled
            assert 'OK2' in handled
        finally:
            renderer.is_running = False
            renderer.command_queue.put(('__wake__', None))
            t.join(timeout=2.0)
        assert not t.is_alive()


def test_async_renderer_stop_event_is_cooperative():
    """Long render handlers can observe stop without killing the thread."""
    import threading

    renderer = AsyncRenderer.__new__(AsyncRenderer)
    renderer._stop_event = threading.Event()
    assert renderer._cancel_requested() is False
    renderer._stop_event.set()
    assert renderer._cancel_requested() is True


# =============================================================================
# EntryPoint
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
