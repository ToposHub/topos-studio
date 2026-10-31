"""
F7 AnimationState 接入 Model3D 测试。

覆盖：
- 新参数注册（anim_play_mode/clip_name/time_offset/start_frame/end_frame/fps）
- _sample_animation_worlds 的播放模式映射（loop/pingpong/once/clamp）
- 时间偏移 / 起止帧裁剪
- clip 名选择（anim_clip_name 优先于索引）
- 无动画时的降级（空 dict）

纯逻辑测试（mock _model_root.animations）。
"""

import numpy as np
import pytest

from src.features.composite.nodes.model3d_node import Model3DNode
from src.features.composite.render3d.scene_graph import (
    AnimationChannel,
    AnimationClip,
    SceneNode,
    SceneGraphRoot,
)


def _make_clip(name: str = "walk", duration: float = 2.0) -> AnimationClip:
    """构建测试 clip：1 channel，root 节点沿 X 平移。"""
    n_frames = max(2, int(duration * 10))
    times = np.linspace(0.0, duration, n_frames, dtype=np.float32)
    # translation.x 线性增长 0 → 1（便于从采样结果反推 anim_time）
    translations = np.zeros((n_frames, 3), dtype=np.float32)
    translations[:, 0] = np.linspace(0.0, 1.0, n_frames)
    rotations = np.tile([0, 0, 0, 1], (n_frames, 1)).astype(np.float32)
    scales = np.ones((n_frames, 3), dtype=np.float32)
    ch = AnimationChannel(
        node_name="root",
        times=times,
        translations=translations,
        rotations=rotations,
        scales=scales,
    )
    return AnimationClip(name=name, duration=duration, channels=[ch])


def _make_model_root(clips):
    """构建含动画的 SceneGraphRoot（单 root 节点）。"""
    root = SceneNode(name="root")
    return SceneGraphRoot(root=root, animations=clips)


@pytest.fixture
def node_with_anim():
    """Model3D 带 2 个 clip 的 fixture。"""
    n = Model3DNode("anim_test")
    clips = [_make_clip("walk", 2.0), _make_clip("run", 1.0)]
    n._model_root = _make_model_root(clips)
    return n


# ──────────────────────────────────────────────────────────────────────
# 参数注册
# ──────────────────────────────────────────────────────────────────────


class TestF7Parameters:
    def test_new_params_in_defaults(self):
        n = Model3DNode("t")
        assert n.parameters["anim_play_mode"] == "loop"
        assert n.parameters["anim_clip_name"] == ""
        assert n.parameters["anim_time_offset"] == 0.0
        assert n.parameters["anim_start_frame"] == 0.0
        assert n.parameters["anim_end_frame"] == 0.0
        assert n.parameters["anim_fps"] == 24.0

    def test_new_params_in_param_defs(self):
        names = [pd.name for pd in Model3DNode.PARAM_DEFS]
        assert "anim_play_mode" in names
        assert "anim_clip_name" in names
        assert "anim_time_offset" in names
        assert "anim_start_frame" in names
        assert "anim_end_frame" in names
        assert "anim_fps" in names

    def test_play_mode_options(self):
        pd = next(p for p in Model3DNode.PARAM_DEFS if p.name == "anim_play_mode")
        assert "loop" in pd.labels
        assert "pingpong" in pd.labels
        assert "once" in pd.labels
        assert "clamp" in pd.labels

    def test_existing_params_preserved(self):
        """旧参数仍存在（向后兼容）。"""
        n = Model3DNode("t")
        assert "anim_enabled" in n.parameters
        assert "anim_time" in n.parameters
        assert "anim_speed" in n.parameters
        assert "anim_clip" in n.parameters


# ──────────────────────────────────────────────────────────────────────
# _sample_animation_worlds 基础
# ──────────────────────────────────────────────────────────────────────


class TestSampleAnimationWorlds:
    def test_disabled_returns_empty(self, node_with_anim):
        result = node_with_anim._sample_animation_worlds(False, 10.0, 1.0)
        assert result == {}

    def test_no_model_root_returns_empty(self):
        n = Model3DNode("t")
        n._model_root = None
        assert n._sample_animation_worlds(True, 10.0, 1.0) == {}

    def test_no_clips_returns_empty(self):
        n = Model3DNode("t")
        n._model_root = _make_model_root([])
        assert n._sample_animation_worlds(True, 10.0, 1.0) == {}

    def test_reuses_sampler_cache_for_same_frame(self, node_with_anim):
        """Model3D 的实际采样入口接入 AnimationSamplerCache。"""
        first = node_with_anim._sample_animation_worlds(True, 12.0, 1.0)
        second = node_with_anim._sample_animation_worlds(True, 12.0, 1.0)

        cache = node_with_anim._animation_sampler_cache
        assert cache.misses == 1
        assert cache.hits == 1
        assert np.allclose(first["root"], second["root"])

    def test_basic_sampling(self, node_with_anim):
        """frame=12, fps=24 → 0.5s → translation.x≈0.25（0→1 线性，duration=2）。"""
        result = node_with_anim._sample_animation_worlds(True, 12.0, 1.0)
        assert "root" in result
        # translation.x 在 0.5s 时约 0.25
        assert 0.2 < result["root"][0, 3] < 0.3


# ──────────────────────────────────────────────────────────────────────
# 播放模式
# ──────────────────────────────────────────────────────────────────────


class TestPlayModes:
    def test_loop_wraps(self, node_with_anim):
        """loop：frame=72 (3s), duration=2 → 回到 1s → x≈0.5。"""
        node_with_anim.parameters["anim_play_mode"] = "loop"
        result = node_with_anim._sample_animation_worlds(True, 72.0, 1.0)
        x = result["root"][0, 3]
        assert 0.4 < x < 0.6  # 1s → 0.5

    def test_clamp_at_end(self, node_with_anim):
        """clamp：frame=72 (3s) > duration(2s) → 钳到 2s。

        注：sample_animation 内部会 % duration 循环，2s % 2s = 0。
        但 remap 保证不会超过 duration（避免 loop 跳回开头之外的值）。
        这里验证 clamp 在超 duration 时不再 loop 到中间值。
        """
        node_with_anim.parameters["anim_play_mode"] = "clamp"
        # frame=60 → 2.5s, clamp 到 2.0s → sample_animation 循环后 = 0.0s
        # 对比 loop: frame=60 → 2.5s → 2.5%2=0.5s
        result_clamp = node_with_anim._sample_animation_worlds(True, 60.0, 1.0)
        node_with_anim.parameters["anim_play_mode"] = "loop"
        result_loop = node_with_anim._sample_animation_worlds(True, 60.0, 1.0)
        # clamp 钳到 2.0 → 循环后 0.0 → x≈0；loop 在 0.5s → x≈0.25
        x_clamp = result_clamp["root"][0, 3]
        x_loop = result_loop["root"][0, 3]
        assert x_clamp < 0.1  # 0s → x≈0
        assert 0.2 < x_loop < 0.3  # 0.5s → x≈0.25

    def test_once_stops_at_end(self, node_with_anim):
        """once：超 duration 后停在末尾（与 clamp 相同行为）。"""
        node_with_anim.parameters["anim_play_mode"] = "once"
        result = node_with_anim._sample_animation_worlds(True, 60.0, 1.0)
        x = result["root"][0, 3]
        # once 钳到 2.0 → sample 循环后 0.0 → x≈0
        assert x < 0.1

    def test_pingpong_reflects(self, node_with_anim):
        """pingpong：frame=72 (3s), duration=2 → 反弹到 1s → x≈0.5。"""
        node_with_anim.parameters["anim_play_mode"] = "pingpong"
        result = node_with_anim._sample_animation_worlds(True, 72.0, 1.0)
        x = result["root"][0, 3]
        assert 0.4 < x < 0.6


# ──────────────────────────────────────────────────────────────────────
# 时间偏移 / 起止帧 / 速度
# ──────────────────────────────────────────────────────────────────────


class TestTimeControls:
    def test_time_offset(self, node_with_anim):
        """offset=1.0s → frame=0 实际从 1s 开始 → x≈0.5。"""
        node_with_anim.parameters["anim_time_offset"] = 1.0
        result = node_with_anim._sample_animation_worlds(True, 0.0, 1.0)
        x = result["root"][0, 3]
        assert 0.4 < x < 0.6

    def test_speed_2x(self, node_with_anim):
        """speed=2 → frame=12 (0.5s) 实际走 1s → x≈0.5。"""
        result = node_with_anim._sample_animation_worlds(True, 12.0, 2.0)
        x = result["root"][0, 3]
        assert 0.4 < x < 0.6

    def test_start_frame(self, node_with_anim):
        """start_frame=24 → frame=24 才开始（=0s）→ x≈0。"""
        node_with_anim.parameters["anim_start_frame"] = 24.0
        result = node_with_anim._sample_animation_worlds(True, 24.0, 1.0)
        x = result["root"][0, 3]
        assert x < 0.1  # 刚开始

    def test_start_frame_before_returns_zero(self, node_with_anim):
        """frame < start_frame → 未开始 → time=0 → x≈0。"""
        node_with_anim.parameters["anim_start_frame"] = 48.0
        result = node_with_anim._sample_animation_worlds(True, 24.0, 1.0)
        x = result["root"][0, 3]
        assert x < 0.1

    def test_end_frame_clamps(self, node_with_anim):
        """end_frame=24 → frame=48 被钳到 24 (1s) → x≈0.5。"""
        node_with_anim.parameters["anim_end_frame"] = 24.0
        result = node_with_anim._sample_animation_worlds(True, 48.0, 1.0)
        x = result["root"][0, 3]
        assert 0.4 < x < 0.6

    def test_custom_fps(self, node_with_anim):
        """fps=30 → frame=30 → 1s → x≈0.5。"""
        node_with_anim.parameters["anim_fps"] = 30.0
        result = node_with_anim._sample_animation_worlds(True, 30.0, 1.0)
        x = result["root"][0, 3]
        assert 0.4 < x < 0.6


# ──────────────────────────────────────────────────────────────────────
# clip 名选择
# ──────────────────────────────────────────────────────────────────────


class TestClipSelection:
    def test_clip_name_overrides_index(self, node_with_anim):
        """anim_clip_name='run' 优先于 anim_clip 索引。

        run duration=1.0s, walk duration=2.0s。
        frame=23 → 0.958s:
          run: 0.958/1.0 → x≈0.958（接近末尾）
          walk: 0.958/2.0 → x≈0.479（中间）
        用 x 大小区分选了哪个 clip。
        """
        node_with_anim.parameters["anim_clip_name"] = "run"
        result = node_with_anim._sample_animation_worlds(True, 23.0, 1.0, anim_clip=0)
        x = result["root"][0, 3]
        assert x > 0.8  # run → 接近末尾

    def test_clip_index_when_name_empty(self, node_with_anim):
        """anim_clip_name 空 → 按 anim_clip 索引。"""
        node_with_anim.parameters["anim_clip_name"] = ""
        # anim_clip=1 → run (duration=1.0)；frame=23 → 接近末尾
        result = node_with_anim._sample_animation_worlds(True, 23.0, 1.0, anim_clip=1)
        x = result["root"][0, 3]
        assert x > 0.8  # run → 接近末尾

    def test_clip_name_not_found_falls_back(self, node_with_anim):
        """clip_name 找不到 → 回退到索引。"""
        node_with_anim.parameters["anim_clip_name"] = "nonexistent"
        # 回退到 anim_clip=0 (walk, duration=2)；frame=24 → 1s → x≈0.5
        result = node_with_anim._sample_animation_worlds(True, 24.0, 1.0, anim_clip=0)
        x = result["root"][0, 3]
        assert 0.4 < x < 0.6
