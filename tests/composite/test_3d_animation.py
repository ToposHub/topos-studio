"""
F7 动画系统测试。

覆盖：
- remap_frame_to_anim_time：frame → anim time（loop/pingpong/once/clamp + speed/offset）
- AnimationState：播放状态机（update/reset/signature/finished）
- AnimationSamplerCache：采样缓存（hit/miss/LRU/统计）
- select_clip / clip_names

纯 numpy 逻辑测试，无 GPU 依赖。
"""

import numpy as np
import pytest

from src.features.composite.render3d.animation import (
    PLAY_MODE_CLAMP,
    PLAY_MODE_LOOP,
    PLAY_MODE_ONCE,
    PLAY_MODE_PINGPONG,
    AnimationSamplerCache,
    AnimationState,
    clip_names,
    remap_frame_to_anim_time,
    select_clip,
)
from src.features.composite.render3d.scene_graph import (
    AnimationChannel,
    AnimationClip,
)


# ──────────────────────────────────────────────────────────────────────
# remap_frame_to_anim_time
# ──────────────────────────────────────────────────────────────────────


class TestRemapFrameToAnimTime:
    def test_basic_frame_to_time(self):
        """frame=12, fps=24, duration=10s → 0.5s。"""
        t = remap_frame_to_anim_time(12, 24, 10.0)
        assert abs(t - 0.5) < 1e-6

    def test_zero_duration_returns_zero(self):
        t = remap_frame_to_anim_time(100, 24, 0.0)
        assert t == 0.0

    def test_zero_fps_returns_zero(self):
        t = remap_frame_to_anim_time(100, 0, 10.0)
        assert t == 0.0

    def test_loop_wraps_around(self):
        """duration=2s, frame=72 (3s), loop → 1.0s。"""
        t = remap_frame_to_anim_time(72, 24, 2.0, play_mode=PLAY_MODE_LOOP)
        assert abs(t - 1.0) < 1e-6

    def test_speed_2x(self):
        """speed=2.0 → 时间走两倍快。"""
        t = remap_frame_to_anim_time(24, 24, 10.0, speed=2.0)
        assert abs(t - 2.0) < 1e-6

    def test_speed_half(self):
        t = remap_frame_to_anim_time(48, 24, 10.0, speed=0.5)
        assert abs(t - 1.0) < 1e-6

    def test_time_offset(self):
        t = remap_frame_to_anim_time(0, 24, 10.0, time_offset=1.5)
        assert abs(t - 1.5) < 1e-6

    def test_start_frame(self):
        """frame < start_frame → 0（未开始）。"""
        t = remap_frame_to_anim_time(5, 24, 10.0, start_frame=10)
        assert t == 0.0
        # frame=start_frame → 0
        t2 = remap_frame_to_anim_time(10, 24, 10.0, start_frame=10)
        assert abs(t2) < 1e-6
        # frame=start_frame+24 → 1s
        t3 = remap_frame_to_anim_time(34, 24, 10.0, start_frame=10)
        assert abs(t3 - 1.0) < 1e-6

    def test_end_frame_clamps(self):
        t = remap_frame_to_anim_time(200, 24, 10.0, end_frame=48)
        # frame 被钳到 48 → 2s
        assert abs(t - 2.0) < 1e-6

    def test_once_mode_stops_at_end(self):
        """once 模式：超 duration 后停在 duration。"""
        # frame=300 (12.5s), duration=2s, once → 2.0
        t = remap_frame_to_anim_time(300, 24, 2.0, play_mode=PLAY_MODE_ONCE)
        assert abs(t - 2.0) < 1e-6

    def test_once_mode_clamps_negative(self):
        """once 模式：负时间偏移应钳到 0（回归：之前不钳下界）。"""
        t = remap_frame_to_anim_time(0, 24, 2.0, play_mode=PLAY_MODE_ONCE,
                                     time_offset=-1.0)
        assert t == 0.0

    def test_clamp_mode(self):
        t = remap_frame_to_anim_time(300, 24, 2.0, play_mode=PLAY_MODE_CLAMP)
        assert abs(t - 2.0) < 1e-6
        # 负偏移 → 0
        t2 = remap_frame_to_anim_time(0, 24, 2.0, play_mode=PLAY_MODE_CLAMP,
                                      time_offset=-1.0)
        assert t2 == 0.0

    def test_pingpong_mode(self):
        """pingpong：duration=2s，3s 时 → 反弹回 1s。"""
        # raw=3s, period=4s, t=3s > 2 → 4-3=1
        t = remap_frame_to_anim_time(72, 24, 2.0, play_mode=PLAY_MODE_PINGPONG)
        assert abs(t - 1.0) < 1e-6
        # raw=1s → 正常 1s（未到反弹点）
        t2 = remap_frame_to_anim_time(24, 24, 2.0, play_mode=PLAY_MODE_PINGPONG)
        assert abs(t2 - 1.0) < 1e-6
        # raw=4s → 0（完整往返）
        t3 = remap_frame_to_anim_time(96, 24, 2.0, play_mode=PLAY_MODE_PINGPONG)
        assert abs(t3) < 1e-6


# ──────────────────────────────────────────────────────────────────────
# AnimationState
# ──────────────────────────────────────────────────────────────────────


class TestAnimationState:
    def test_defaults(self):
        s = AnimationState()
        assert s.clip_name == ""
        assert s.playing is True
        assert s.speed == 1.0
        assert s.play_mode == PLAY_MODE_LOOP
        assert s.anim_time == 0.0
        assert s.finished is False

    def test_update_basic(self):
        s = AnimationState()
        t = s.update(frame=24, fps=24, clip_duration=10.0)
        assert abs(t - 1.0) < 1e-6
        assert abs(s.anim_time - 1.0) < 1e-6

    def test_update_paused_freezes_time(self):
        s = AnimationState(playing=False, anim_time=2.5)
        t = s.update(frame=100, fps=24, clip_duration=10.0)
        assert t == 2.5  # 冻结
        assert s.anim_time == 2.5

    def test_update_no_clip_zero_time(self):
        s = AnimationState()
        t = s.update(frame=100, fps=24, clip_duration=0.0)
        assert t == 0.0
        assert s.anim_time == 0.0

    def test_update_speed(self):
        s = AnimationState(speed=2.0)
        t = s.update(frame=24, fps=24, clip_duration=10.0)
        assert abs(t - 2.0) < 1e-6

    def test_once_finished_flag(self):
        s = AnimationState(play_mode=PLAY_MODE_ONCE)
        # frame=300 (12.5s) > duration 2s → finished
        s.update(frame=300, fps=24, clip_duration=2.0)
        assert s.finished is True

    def test_once_not_finished_during_play(self):
        s = AnimationState(play_mode=PLAY_MODE_ONCE)
        s.update(frame=24, fps=24, clip_duration=10.0)
        assert s.finished is False

    def test_loop_never_finished(self):
        s = AnimationState(play_mode=PLAY_MODE_LOOP)
        s.update(frame=1000, fps=24, clip_duration=2.0)
        assert s.finished is False

    def test_reset(self):
        s = AnimationState(anim_time=5.0, finished=True)
        s.reset()
        assert s.anim_time == 0.0
        assert s.finished is False

    def test_signature_stability(self):
        """相同参数 → 相同签名。"""
        s1 = AnimationState(clip_name="walk", speed=1.5, play_mode=PLAY_MODE_LOOP)
        s2 = AnimationState(clip_name="walk", speed=1.5, play_mode=PLAY_MODE_LOOP)
        assert s1.signature() == s2.signature()

    def test_signature_differs_on_param_change(self):
        s1 = AnimationState(clip_name="walk")
        s2 = AnimationState(clip_name="run")
        assert s1.signature() != s2.signature()

    def test_signature_ignores_anim_time(self):
        """anim_time 不进 signature（每帧变；它单独进 cache key）。"""
        s1 = AnimationState(anim_time=1.0)
        s2 = AnimationState(anim_time=2.0)
        assert s1.signature() == s2.signature()


# ──────────────────────────────────────────────────────────────────────
# AnimationSamplerCache
# ──────────────────────────────────────────────────────────────────────


def _make_test_clip(name: str = "test", duration: float = 1.0) -> AnimationClip:
    """构建测试 clip：1 个 channel，2 个关键帧（0s 和 1s 各平移）。"""
    times = np.array([0.0, 1.0], dtype=np.float32)
    translations = np.array([[0, 0, 0], [1, 0, 0]], dtype=np.float32)
    rotations = np.array([[0, 0, 0, 1], [0, 0, 0, 1]], dtype=np.float32)
    scales = np.array([[1, 1, 1], [1, 1, 1]], dtype=np.float32)
    ch = AnimationChannel(
        node_name="root",
        times=times,
        translations=translations,
        rotations=rotations,
        scales=scales,
    )
    return AnimationClip(name=name, duration=duration, channels=[ch])


class TestAnimationSamplerCache:
    def test_sample_returns_correct_value(self):
        cache = AnimationSamplerCache()
        clip = _make_test_clip()
        result = cache.sample(clip, 0.5)
        # 0.5s 在两帧中间 → translation.x = 0.5
        assert "root" in result
        np.testing.assert_array_almost_equal(result["root"][:3, 3], [0.5, 0, 0])

    def test_cache_hit_on_same_quantized_time(self):
        cache = AnimationSamplerCache(quantize_hz=120)
        clip = _make_test_clip()
        # 0.500 和 0.504 在 1/120≈0.0083 量化内 → 命中
        cache.sample(clip, 0.500)
        cache.sample(clip, 0.504)
        assert cache.hits == 1
        assert cache.misses == 1

    def test_cache_miss_on_different_time(self):
        cache = AnimationSamplerCache(quantize_hz=120)
        clip = _make_test_clip()
        cache.sample(clip, 0.0)
        cache.sample(clip, 0.5)
        assert cache.misses == 2
        assert cache.hits == 0

    def test_hit_rate(self):
        cache = AnimationSamplerCache()
        clip = _make_test_clip()
        cache.sample(clip, 0.5)
        cache.sample(clip, 0.5)
        cache.sample(clip, 0.5)
        # 1 miss + 2 hit → rate = 2/3
        assert abs(cache.hit_rate - 2.0 / 3.0) < 1e-6

    def test_lru_eviction(self):
        """超出 max_entries 淘汰最旧。"""
        cache = AnimationSamplerCache(quantize_hz=1000, max_entries=2)
        clip = _make_test_clip()
        cache.sample(clip, 0.0)
        cache.sample(clip, 0.1)
        assert cache.size == 2
        cache.sample(clip, 0.2)  # 触发淘汰
        assert cache.size == 2  # 不超过 max

    def test_lru_not_fifo(self):
        """LRU：刚访问的 entry 不应被淘汰（回归：之前是 FIFO）。

        max=2：插入 t0/t1，再访问 t0（hit），再插 t2 →
        应淘汰 t1（最久未访问），保留 t0（刚访问过）。
        """
        cache = AnimationSamplerCache(quantize_hz=1000, max_entries=2)
        clip = _make_test_clip()
        cache.sample(clip, 0.0)  # miss, insert t0
        cache.sample(clip, 0.1)  # miss, insert t1
        cache.sample(clip, 0.0)  # hit t0 → LRU 移到末尾
        cache.sample(clip, 0.2)  # miss, insert t2 → 淘汰 t1（不是 t0）
        assert cache.misses == 3  # t0, t1, t2 = 3 miss
        assert cache.hits == 1    # t0 hit
        # t0 应仍在缓存（再访问应 hit）
        cache.sample(clip, 0.0)
        assert cache.hits == 2    # t0 仍在 → hit

    def test_clear(self):
        cache = AnimationSamplerCache()
        clip = _make_test_clip()
        cache.sample(clip, 0.5)
        cache.sample(clip, 0.5)
        cache.clear()
        assert cache.size == 0
        assert cache.hits == 0
        assert cache.misses == 0

    def test_returns_copy_not_reference(self):
        """缓存返回的是拷贝，修改不影响缓存内部。"""
        cache = AnimationSamplerCache()
        clip = _make_test_clip()
        r1 = cache.sample(clip, 0.5)
        r1["root"][0, 3] = 999.0  # 篡改返回值
        r2 = cache.sample(clip, 0.5)  # 应命中缓存
        assert r2["root"][0, 3] != 999.0  # 缓存未被污染

    def test_different_clips_separate_cache(self):
        cache = AnimationSamplerCache()
        c1 = _make_test_clip(name="walk")
        c2 = _make_test_clip(name="run")
        cache.sample(c1, 0.5)
        cache.sample(c2, 0.5)
        assert cache.size == 2
        assert cache.misses == 2

    def test_same_named_clip_objects_do_not_alias(self):
        """两个资产/片段同名时，缓存不能复用错误的采样结果。"""
        cache = AnimationSamplerCache()
        c1 = _make_test_clip(name="walk")
        c2 = _make_test_clip(name="walk")
        c2.channels[0].translations[:, 0] *= 2.0

        first = cache.sample(c1, 0.5)
        second = cache.sample(c2, 0.5)

        assert cache.misses == 2
        assert first["root"][0, 3] != second["root"][0, 3]


# ──────────────────────────────────────────────────────────────────────
# select_clip / clip_names
# ──────────────────────────────────────────────────────────────────────


class TestSelectClip:
    def test_empty_list(self):
        assert select_clip([], "walk") is None
        assert select_clip([], "") is None

    def test_by_name(self):
        clips = [AnimationClip(name="walk", duration=1.0),
                 AnimationClip(name="run", duration=0.5)]
        assert select_clip(clips, "run").name == "run"

    def test_empty_name_returns_first(self):
        clips = [AnimationClip(name="walk", duration=1.0),
                 AnimationClip(name="run", duration=0.5)]
        assert select_clip(clips, "").name == "walk"

    def test_name_not_found(self):
        clips = [AnimationClip(name="walk", duration=1.0)]
        assert select_clip(clips, "missing") is None


class TestClipNames:
    def test_empty(self):
        assert clip_names([]) == []

    def test_names(self):
        clips = [AnimationClip(name="walk", duration=1.0),
                 AnimationClip(name="run", duration=0.5)]
        assert clip_names(clips) == ["walk", "run"]
