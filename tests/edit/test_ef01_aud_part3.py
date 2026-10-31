"""EF-01 part3 修复批次回归测试（AUD-17813..19208，src/features/edit/core）。

逐条覆盖本子运行 19 个 Suggestion 级审计发现的修复，全部无 GL 上下文、
无真实媒体、无模态对话框。每条含正例；多数同时含"回退即失败"的
回退敏感性断言（见各测试内注释）。

覆盖清单：
- AUD-17813 scene_cut/cache.py       get_scene_cut_cache 惰性单例
- AUD-17911 scene_cut/filters.py     process() 重置 diagnostics
- AUD-17913 scene_cut/filters.py     motion/black 帧窗口裁剪 + 计数器门控
- AUD-18015 scene_cut/models.py      combined_score 文档公式对齐实现
- AUD-18210 serialization.py         _dict_to_asset 对显式 JSON null 防御
- AUD-18215 sidechain_ducker.py      逐样本循环向量化（位级一致）
- AUD-18323 timeline_clip_frame_provider.py  GL 线程身份一次性固化
- AUD-18325 timeline_clip_frame_provider.py  clear_debug_cache 跨线程停靠
- AUD-18326 timeline_clip_frame_provider.py  默认色彩标签记忆化
- AUD-18328 timeline_clip_frame_provider.py  异步解码世代号停止协议
- AUD-18411 timeline_export.py       取消标志 OR 合并（不覆盖）
- AUD-18511 timeline_frame_mapper.py source_time_seconds 语义文档
- AUD-18605 timeline_metal_export_renderer.py 单效果 planar 源物化
- AUD-18809 timeline_ops.py          模型 ValueError 转译 TimelineOperationError
- AUD-18816 timeline_ops.py          insert 波移单次原子重建
- AUD-18905 timeline_preview_renderer.py  _composite_layers try/finally
- AUD-18906 timeline_preview_renderer.py  _mg_static_frames 淘汰防空表
- AUD-18909 timeline_preview_renderer.py  comp GL context/surface 销毁
- AUD-19208 transition_mask.py       多边形 mask 网格/结果缓存
"""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from src.features.edit.core.scene_cut import cache as scene_cut_cache_module
from src.features.edit.core.scene_cut.cache import get_scene_cut_cache
from src.features.edit.core.scene_cut.filters import (
    DetectionState,
    FrameMetricsEntry,
    SceneCutFilterPipeline,
)
from src.features.edit.core.scene_cut.frame_sampler import SampledFrame
from src.features.edit.core.scene_cut.models import (
    FrameTimePoint,
    SceneCutKind,
    SceneCutMetrics,
    SceneCutSettings,
)
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.sidechain_ducker import (
    DuckerParams,
    SideChainDucker,
)
from src.features.edit.core.timeline_clip_frame_provider import (
    TimelineClipFrameProvider,
)
from src.features.edit.core.timeline_frame_mapper import FrameMapResult, map_frame
from src.features.edit.core.timeline_ops import (
    TimelineOperationError,
    _translate_model_error,
    add_transition,
    insert_clip_at_playhead,
    move_clip,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.transition_mask import (
    _coord_grid_cache,
    _polygon_mask,
    _polygon_mask_cache,
    compute_transition_mask,
)
from src.shared.media.metadata import Rational


# ================================================================
# 共享构造 helper
# ================================================================

def _make_sampled_frame(source_frame: int) -> SampledFrame:
    return SampledFrame(
        time_point=FrameTimePoint(
            source_frame=source_frame,
            pts=source_frame,
            seconds=source_frame / 24.0,
        ),
        frame_data=np.empty((0, 0, 3), dtype=np.uint8),
        width=0,
        height=0,
        original_width=1920,
        original_height=1080,
    )


def _entry(
    i: int,
    score: float,
    *,
    motion: float = 0.0,
    black: float = 0.0,
    flash: float = 0.0,
) -> FrameMetricsEntry:
    metrics = SceneCutMetrics(
        luma_delta=min(score, 1.0),
        hist_delta=score,
        edge_delta=score,
        color_delta=score,
        black_score=black,
        flash_score=flash,
        motion_score=motion,
        combined_score=score,
    )
    return FrameMetricsEntry(frame=_make_sampled_frame(i), metrics=metrics, frame_index=i)


def _make_clip(
    clip_id: str,
    track_id: str,
    start: int,
    duration: int,
) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset1",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        speed=1.0,
    )


def _make_track(track_id: str, clips: tuple[ClipInstance, ...]) -> EditTrack:
    ordered = tuple(sorted(clips, key=lambda c: c.timeline_start))
    return EditTrack(track_id=track_id, name=track_id, track_type="video", clips=ordered)


def _make_sequence(*tracks: EditTrack) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq1",
        name="Test Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=tuple(tracks),
    )


# ================================================================
# AUD-17813 — get_scene_cut_cache 惰性单例
# ================================================================

class TestAud17813SceneCutCacheSingleton:
    def test_same_dir_returns_same_instance(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(scene_cut_cache_module, "_SCENE_CUT_CACHE_INSTANCES", {})
        cache_dir = str(tmp_path / "proj_cache")
        first = get_scene_cut_cache(cache_dir)
        second = get_scene_cut_cache(cache_dir)
        assert first is second

    def test_none_dir_returns_shared_default(self, monkeypatch) -> None:
        monkeypatch.setattr(scene_cut_cache_module, "_SCENE_CUT_CACHE_INSTANCES", {})
        assert get_scene_cut_cache() is get_scene_cut_cache()

    def test_different_dirs_get_different_instances(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(scene_cut_cache_module, "_SCENE_CUT_CACHE_INSTANCES", {})
        a = get_scene_cut_cache(str(tmp_path / "a"))
        b = get_scene_cut_cache(str(tmp_path / "b"))
        assert a is not b
        assert a.cache_dir != b.cache_dir

    def test_relative_and_absolute_paths_share_instance(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """同目录的不同表示（尾斜杠等）归一到同一单例键。"""
        monkeypatch.setattr(scene_cut_cache_module, "_SCENE_CUT_CACHE_INSTANCES", {})
        cache_dir = tmp_path / "c"
        a = get_scene_cut_cache(str(cache_dir))
        b = get_scene_cut_cache(str(cache_dir) + "/")
        assert a is b


# ================================================================
# AUD-17911 — process() 重置 diagnostics
# ================================================================

class TestAud17911DiagnosticsReset:
    def test_second_process_does_not_accumulate(self) -> None:
        """同管线跑两遍，诊断计数必须一致（回退：第二遍翻倍）。"""
        settings = SceneCutSettings(adaptive_threshold=False)
        entries = [_entry(i, 0.9) for i in range(30)]
        pipeline = SceneCutFilterPipeline(settings)

        pipeline.process(list(entries))
        first_diag = pipeline.diagnostics
        assert first_diag["min_distance_filtered_count"] > 0

        pipeline.process(list(entries))
        second_diag = pipeline.diagnostics

        assert second_diag == first_diag

    def test_diagnostics_rebuilt_from_zero_keys(self) -> None:
        settings = SceneCutSettings(adaptive_threshold=False)
        entries = [_entry(i, 0.9) for i in range(30)]
        pipeline = SceneCutFilterPipeline(settings)

        pipeline.process(list(entries))
        first_diag = pipeline.diagnostics
        assert first_diag["total_candidates"] > 0

        pipeline.process(list(entries))
        # 第二遍计数与一遍 30 帧的全新管线完全一致（不叠加）
        fresh = SceneCutFilterPipeline(settings)
        fresh.process(list(entries))
        assert pipeline.diagnostics == fresh.diagnostics
        assert pipeline.diagnostics["total_candidates"] == first_diag["total_candidates"]


# ================================================================
# AUD-17913 — motion/black 帧窗口裁剪 + 累计计数器门控
# ================================================================

class TestAud17913BoundedFrameLists:
    def test_motion_window_bounded_seen_counts_all(self) -> None:
        """200 高运动帧：列表只留窗口，seen 计满（回退：len==200）。"""
        settings = SceneCutSettings(
            adaptive_threshold=False,
            motion_score_threshold=0.5,
        )
        entries = [_entry(i, 0.0, motion=0.9) for i in range(200)]
        pipeline = SceneCutFilterPipeline(settings)
        pipeline.process(entries)

        state = pipeline._state
        assert state.motion_frames_seen == 200
        assert len(state.motion_frames) <= DetectionState.MOTION_SCAN_RADIUS + 1
        # 窗口内保留的是最近的帧
        assert state.motion_frames[-1] == 199

    def test_black_window_bounded(self) -> None:
        settings = SceneCutSettings(
            adaptive_threshold=False,
            black_score_threshold=0.5,
        )
        entries = [_entry(i, 0.0, black=0.9) for i in range(200)]
        pipeline = SceneCutFilterPipeline(settings)
        pipeline.process(entries)

        state = pipeline._state
        assert state.black_frames_seen == 200
        assert len(state.black_frames) <= DetectionState.BLACK_SCAN_RADIUS + 1

    def test_fast_motion_classification_preserved(self) -> None:
        """连续高运动后的候选仍被分类为 FAST_MOTION（计数器门控语义）。"""
        settings = SceneCutSettings(
            adaptive_threshold=False,
            motion_score_threshold=0.5,
            min_scene_frames=8,
        )
        entries = [_entry(i, 0.9, motion=0.9) for i in range(20)]
        pipeline = SceneCutFilterPipeline(settings)
        candidates = pipeline.process(entries)

        by_frame = {c.source_frame: c for c in candidates}
        # 帧 0：seen=1，门控未过 → HARD_CUT
        assert by_frame[0].kind == SceneCutKind.HARD_CUT
        # 帧 8：seen=9 > 3 且 ±5 邻域内 ≥3 帧 → FAST_MOTION
        assert by_frame[8].kind == SceneCutKind.FAST_MOTION

    def test_register_cut_resets_seen_counters(self) -> None:
        state = DetectionState()
        state.note_motion_frame(0)
        state.note_motion_frame(1)
        state.note_black_frame(2)
        assert state.motion_frames_seen == 2
        assert state.black_frames_seen == 1

        state.register_cut(2, _entry(2, 0.9).metrics)
        assert state.motion_frames_seen == 0
        assert state.black_frames_seen == 0
        assert state.motion_frames == []
        assert state.black_frames == []


# ================================================================
# AUD-18015 — combined_score 文档公式对齐实现
# ================================================================

class TestAud18015CombinedScoreDoc:
    def test_docstring_matches_implementation(self) -> None:
        """文档必须是四项公式，不得再含 0.05*motion（回退：旧文案复活）。"""
        doc = SceneCutMetrics.__doc__ or ""
        assert "0.05*motion" not in doc
        assert "0.35*luma" in doc
        assert "0.30*hist" in doc
        assert "0.20*edge" in doc
        assert "0.10*color" in doc

    def test_implementation_weights_are_four_terms(self) -> None:
        from src.features.edit.core.scene_cut import metrics as metrics_mod

        total = (
            metrics_mod.WEIGHT_LUMA_DELTA
            + metrics_mod.WEIGHT_HIST_DELTA
            + metrics_mod.WEIGHT_EDGE_DELTA
            + metrics_mod.WEIGHT_COLOR_DELTA
        )
        assert abs(total - 0.95) < 1e-9
        assert not hasattr(metrics_mod, "WEIGHT_MOTION_DELTA")


# ================================================================
# AUD-18210 — _dict_to_asset 显式 null 防御
# ================================================================

class TestAud18210NullDeserialization:
    def test_explicit_null_fields_fall_back(self) -> None:
        asset = EditProjectSerializer._dict_to_asset({
            "asset_id": "a1",
            "path": "/tmp/x.mp4",
            "keywords": None,      # 旧代码：tuple(None) → TypeError
            "metadata": None,      # 旧代码：metadata=None → 下游 .get 崩溃
            "rating": None,        # 旧代码：int(None) → TypeError
        })
        assert asset.keywords == ()
        assert asset.metadata == {}
        assert asset.rating == 0

    def test_valid_fields_preserved(self) -> None:
        asset = EditProjectSerializer._dict_to_asset({
            "asset_id": "a2",
            "path": "/tmp/y.mov",
            "keywords": ["k1", "k2"],
            "metadata": {"fps": "24", "codec": "h264"},
            "rating": 4,
        })
        assert asset.keywords == ("k1", "k2")
        assert asset.metadata == {"fps": "24", "codec": "h264"}
        assert asset.rating == 4

    def test_wrong_typed_fields_fall_back(self) -> None:
        """类型不对（字符串/数字）同样兜底，不抛不污染。"""
        asset = EditProjectSerializer._dict_to_asset({
            "asset_id": "a3",
            "path": "/tmp/z.mov",
            "keywords": "not-a-list",
            "metadata": ["not", "a", "dict"],
            "rating": "high",
        })
        assert asset.keywords == ()
        assert asset.metadata == {}
        assert asset.rating == 0


# ================================================================
# AUD-18215 — sidechain ducker 向量化（位级一致）
# ================================================================

def _reference_duck_gains(
    sc_db: np.ndarray,
    threshold: float,
    target_duck: float,
    att_c: float,
    rel_c: float,
    hold_samples: int,
    gain0: float,
    hold0: float,
) -> tuple[np.ndarray, float, float]:
    """修复前的逐样本参考实现（审计证据原样）。"""
    n = len(sc_db)
    gains = np.empty(n, dtype=np.float64)
    gain_db = gain0
    hold_counter = hold0
    for i in range(n):
        if sc_db[i] > threshold:
            if gain_db > target_duck:
                gain_db = target_duck + (gain_db - target_duck) * att_c
            hold_counter = float(hold_samples)
        else:
            if hold_counter > 0:
                hold_counter -= 1.0
            else:
                if gain_db < 0.0:
                    gain_db = 0.0 + (gain_db - 0.0) * rel_c
        gains[i] = gain_db
    return gains, gain_db, hold_counter


class TestAud18215DuckerVectorized:
    def _process_with_reference(self, sidechain_level: np.ndarray) -> None:
        sr = 48000.0
        params = DuckerParams(
            enabled=True,
            threshold_db=-30.0,
            target_duck_db=-6.0,
            attack_ms=20.0,
            release_ms=500.0,
            hold_ms=100.0,
        )
        ducker = SideChainDucker(sr)
        ducker.set_params(params)

        n = len(sidechain_level)
        sidechain = np.tile(sidechain_level, (2, 1))
        music = (np.ones((2, n), dtype=np.float64) * 0.5)
        out = ducker.process(music, sidechain=sidechain)

        sc_peak = np.max(np.abs(sidechain.astype(np.float64)), axis=0)
        sc_db = 20.0 * np.log10(np.maximum(sc_peak, 1e-9))
        ref_gains, _, _ = _reference_duck_gains(
            sc_db,
            params.threshold_db,
            params.target_duck_db,
            ducker._attack_coeff,
            ducker._release_coeff,
            ducker._hold_samples,
            0.0,
            0.0,
        )
        ref_out = music * (10.0 ** (ref_gains / 20.0))[np.newaxis, :]
        # attack 段的"加 target 减 target"两次舍入无法闭式重放——
        # 向量化与逐样本实现差 ≤1 ulp（~1e-15 相对），远低于 int16
        # 量化步长与仓库 parity 容差（atol=1e-4）。release 段位级一致。
        np.testing.assert_allclose(out, ref_out, rtol=1e-12, atol=1e-12)

    def test_parity_alternating_speech(self) -> None:
        rng = np.random.default_rng(42)
        levels = rng.uniform(0.0, 0.1, 4096)
        # 构造稀疏"语音段"（> threshold）与静音段交替
        speech = np.zeros(4096)
        speech[100:900] = 0.5
        speech[1500:1520] = 0.5
        speech[3000:4000] = 0.2
        levels += speech
        self._process_with_reference(levels)

    def test_parity_start_mid_duck(self) -> None:
        """携带非零初始状态（mid-duck）+ hold 边界。"""
        ducker = SideChainDucker(48000.0)
        ducker.set_params(DuckerParams(hold_ms=100.0, release_ms=500.0))
        ducker._gain_db = -5.5
        ducker._hold_counter = 4800.0 + 0.0  # 恰好在 hold 边界
        music = np.full((1, 20000), 0.5)
        out = ducker.process(music, sidechain=np.zeros((1, 20000)))

        ref_gains, ref_end, _ = _reference_duck_gains(
            np.full(20000, -200.0),
            -30.0,
            -6.0,
            ducker._attack_coeff,
            ducker._release_coeff,
            ducker._hold_samples,
            -5.5,
            4800.0,
        )
        ref_out = music * (10.0 ** (ref_gains / 20.0))[np.newaxis, :]
        np.testing.assert_array_equal(out, ref_out)
        assert ducker._gain_db == ref_end

    def test_smooth_release_parity(self) -> None:
        ducker = SideChainDucker(48000.0)
        ducker.set_params(DuckerParams(release_ms=500.0))
        ducker._gain_db = -6.0
        music = np.full((1, 4096), 0.5)
        out = ducker._smooth_release_to_zero(music)

        gains = np.empty(4096, dtype=np.float64)
        gain_db = -6.0
        for i in range(4096):
            if gain_db < 0.0:
                gain_db = 0.0 + (gain_db - 0.0) * ducker._release_coeff
            gains[i] = gain_db
        ref_out = music * (10.0 ** (gains / 20.0))[np.newaxis, :]
        np.testing.assert_array_equal(out, ref_out)
        assert ducker._gain_db == gain_db

    def test_vectorized_path_actually_used(self) -> None:
        """回退敏感性：向量化标记（np.multiply.accumulate）必须存在，
        逐样本主循环必须消失。回退到旧循环实现即失败。"""
        import inspect

        import src.features.edit.core.sidechain_ducker as sd_mod

        source = inspect.getsource(sd_mod.SideChainDucker.process)
        assert "np.multiply.accumulate" in source
        assert "for i in range(n):" not in source
        release_source = inspect.getsource(sd_mod.SideChainDucker._smooth_release_to_zero)
        assert "np.multiply.accumulate" in release_source
        assert "for i in range(n):" not in release_source


# ================================================================
# AUD-18323 — GL 线程身份一次性固化
# ================================================================

def _fake_runtime_context() -> SimpleNamespace:
    return SimpleNamespace(
        graph_id="graph",
        timeline_frame=0,
        input_stage="after_color",
        width=64,
        height=64,
        timeline_id=None,
        clip_id=None,
        asset_id=None,
        source_frame=0,
    )


def _install_cache_sentinels(provider: TimelineClipFrameProvider) -> None:
    provider._source_texture_cache[("sentinel", 0)] = "tex"
    provider._source_cache_order.append(("sentinel", 0))
    provider._debug_placeholder_cache[(64, 64)] = "placeholder"
    provider._owned_textures[999] = "owned"
    provider._source_texture_retired.append(123)


class TestAud18323GlThreadPin:
    def test_thread_change_invalidates_and_repins(self, monkeypatch) -> None:
        provider = TimelineClipFrameProvider()
        fake_other_thread = threading.get_ident() + 987654321
        provider._gl_thread_id = fake_other_thread
        _install_cache_sentinels(provider)

        # 拦截两个取纹理路径，避免真实解码/上传
        monkeypatch.setattr(provider, "_try_color_output_texture", lambda ctx: None)
        monkeypatch.setattr(provider, "_try_source_frame_texture", lambda ctx: None)

        provider.get_timeline_clip_texture(_fake_runtime_context())

        # 线程身份重绑为当前线程（而非被无条件覆写而留下旧记账）
        assert provider._gl_thread_id == threading.get_ident()
        assert not provider._source_texture_cache
        assert not provider._source_cache_order
        assert not provider._debug_placeholder_cache
        assert not provider._owned_textures
        assert not provider._source_texture_retired
        assert provider._last_source_texture_id is None

    def test_first_entry_pins_thread(self, monkeypatch) -> None:
        provider = TimelineClipFrameProvider()
        monkeypatch.setattr(provider, "_try_color_output_texture", lambda ctx: None)
        monkeypatch.setattr(provider, "_try_source_frame_texture", lambda ctx: None)
        assert provider._gl_thread_id is None
        provider.get_timeline_clip_texture(_fake_runtime_context())
        assert provider._gl_thread_id == threading.get_ident()

    def test_same_thread_entry_keeps_caches(self, monkeypatch) -> None:
        provider = TimelineClipFrameProvider()
        provider._gl_thread_id = threading.get_ident()
        monkeypatch.setattr(provider, "_try_color_output_texture", lambda ctx: None)
        monkeypatch.setattr(provider, "_try_source_frame_texture", lambda ctx: None)
        _install_cache_sentinels(provider)
        provider._source_texture_retired.clear()  # 避免触发 retired 释放

        provider.get_timeline_clip_texture(_fake_runtime_context())

        assert provider._gl_thread_id == threading.get_ident()
        assert provider._source_texture_cache  # 同线程进入不作废缓存


# ================================================================
# AUD-18325 — clear_debug_cache 跨线程停靠
# ================================================================

class _FakeTexture:
    def __init__(self) -> None:
        self.released = 0
        self.texture_id = 1

    def release(self) -> None:
        self.released += 1


class TestAud18325ClearDebugCacheDocking:
    def test_off_thread_call_parks_and_drains(self) -> None:
        provider = TimelineClipFrameProvider()
        tex = _FakeTexture()
        provider._owned_textures[1] = tex
        provider._source_texture_cache[("a", 0)] = tex
        provider._debug_placeholder_cache[(8, 8)] = "p"
        fake_other_thread = threading.get_ident() + 192837465
        provider._gl_thread_id = fake_other_thread

        provider.clear_debug_cache()

        # 非 GL 线程：绝不释放（回退：立即释放 → 此断言失败）
        assert tex.released == 0
        assert provider._owned_textures
        assert provider._debug_cache_clear_pending is True

        # 渲染线程入口 drain → 完整释放
        provider._gl_thread_id = threading.get_ident()
        provider._drain_pending_source_cache_clear()
        assert tex.released == 1
        assert not provider._owned_textures
        assert not provider._source_texture_cache
        assert not provider._debug_placeholder_cache
        assert provider._debug_cache_clear_pending is False

    def test_unpinned_provider_releases_immediately(self) -> None:
        provider = TimelineClipFrameProvider()
        tex = _FakeTexture()
        provider._owned_textures[1] = tex
        provider.clear_debug_cache()
        assert tex.released == 1
        assert not provider._owned_textures

    def test_same_thread_call_releases_immediately(self) -> None:
        provider = TimelineClipFrameProvider()
        provider._gl_thread_id = threading.get_ident()
        tex = _FakeTexture()
        provider._owned_textures[7] = tex
        provider.clear_debug_cache()
        assert tex.released == 1
        assert provider._debug_cache_clear_pending is False


# ================================================================
# AUD-18326 — 默认色彩标签记忆化
# ================================================================

class TestAud18326DefaultTagsMemoized:
    def test_probe_failure_cached_once(self) -> None:
        provider = TimelineClipFrameProvider()

        class _ExplodingService:
            def __init__(self) -> None:
                self.calls = 0

            def get_clip_info(self, clip_id: str) -> Any:
                self.calls += 1
                raise RuntimeError("no metadata backend")

        service = _ExplodingService()
        tags1 = provider._detect_source_color_space(service, "clip1")
        tags2 = provider._detect_source_color_space(service, "clip1")

        assert tags1 == ("bt709", "bt709", "bt709", "limited")
        assert tags2 == tags1
        # 回退：早退不写缓存 → 两次调用各探测一次
        assert service.calls == 1
        assert provider._clip_color_tags["clip1"] == tags1

    def test_service_without_get_clip_info_cached(self) -> None:
        provider = TimelineClipFrameProvider()
        tags = provider._detect_source_color_space(object(), "clip2")
        assert tags == ("bt709", "bt709", "bt709", "limited")
        assert provider._clip_color_tags["clip2"] == tags


# ================================================================
# AUD-18328 — 异步解码世代号停止协议
# ================================================================

class TestAud18328AsyncDecodeGeneration:
    def test_stop_bumps_generation(self) -> None:
        provider = TimelineClipFrameProvider()
        gen0 = provider._async_decode_generation
        provider._stop_async_decode_thread()
        assert provider._async_decode_generation == gen0 + 1

    def test_stale_generation_worker_never_touches_state(self) -> None:
        """过期世代 worker 立即退出，不消费 targets、不写 pending。"""
        provider = TimelineClipFrameProvider()
        provider._async_decode_generation = 10
        provider._async_targets["/movie.mp4"] = ("clip1", 7, "bt709", "limited")
        decoded_sentinel = object()
        monkey_called = {"n": 0}

        def _fake_decode(service, vid_clip_id, frame, asset_path):
            monkey_called["n"] += 1
            return decoded_sentinel

        provider._decode_source_frame_via_service = _fake_decode  # type: ignore[method-assign]

        provider._async_decode_worker(generation=3)

        assert monkey_called["n"] == 0
        assert "/movie.mp4" in provider._async_targets
        assert not provider._pending_source_frames

    def test_current_generation_worker_writes_pending_then_exits(self) -> None:
        provider = TimelineClipFrameProvider()
        provider._async_targets["/movie.mp4"] = ("clip1", 3, "bt709", "limited")
        decoded_sentinel = object()
        provider._decode_source_frame_via_service = (  # type: ignore[method-assign]
            lambda service, vid, frame, path: decoded_sentinel
        )

        def _stop_after_first_frame() -> None:
            provider._async_decode_stop = True

        provider._frame_ready_callback = _stop_after_first_frame

        worker = threading.Thread(
            target=provider._async_decode_worker,
            args=(provider._async_decode_generation,),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=5.0)

        assert not worker.is_alive()
        assert provider._pending_source_frames[("/movie.mp4", 3)][0] is decoded_sentinel

    def test_stopped_worker_ignores_reset_stop_flag(self) -> None:
        """stop 复位后，旧世代 worker 仍必须退出（世代号语义）。"""
        provider = TimelineClipFrameProvider()
        old_generation = provider._async_decode_generation
        provider._stop_async_decode_thread()  # 世代 +1、stop=True
        assert provider._async_decode_generation == old_generation + 1

        # 模拟 _ensure_async_decode_thread 的 stop 复位（对新 worker 而言合法）
        provider._async_decode_stop = False

        done = threading.Event()

        def _stale_worker() -> None:
            provider._async_decode_worker(generation=old_generation)
            done.set()

        thread = threading.Thread(target=_stale_worker, daemon=True)
        thread.start()
        assert done.wait(timeout=2.0), "stale generation worker did not exit"


# ================================================================
# AUD-18411 — 取消标志 OR 合并
# ================================================================

class TestAud18411CancelMerge:
    def _source_code(self) -> str:
        path = (
            Path(__file__).resolve().parents[2]
            / "src" / "features" / "edit" / "core" / "timeline_export.py"
        )
        return path.read_text()

    def test_merge_not_overwrite_in_source(self) -> None:
        """回退敏感性：源码必须含 OR 合并，且不再直接覆写。"""
        source = self._source_code()
        assert "self._cancel_requested or self._cancel_pending_before_start" in source
        # 旧实现（直接赋值 pending）不得复活
        assert "self._cancel_requested = self._cancel_pending_before_start\n" not in source

    def test_stale_reset_precedes_exporting_flip(self) -> None:
        """陈旧标志清理必须发生在 _is_exporting 翻转之前（pending 通道完好）。"""
        source = self._source_code()
        reset_idx = source.find("self._cancel_requested = False")
        flip_idx = source.find("self._is_exporting = True")
        merge_idx = source.find(
            "self._cancel_requested or self._cancel_pending_before_start"
        )
        assert reset_idx != -1 and flip_idx != -1 and merge_idx != -1
        assert reset_idx < flip_idx < merge_idx

    def _simulate(
        self,
        stale: bool,
        pre_start_cancel: bool,
        race_after_flip: bool,
    ) -> bool:
        """按源码顺序复刻三行初始化 + cancel_export 语义，验证合同。

        Args:
            stale: 上一轮导出遗留 _cancel_requested=True 且无 pending。
            pre_start_cancel: worker 创建后、export_timeline 前的取消
                （_is_exporting 尚为 False → 走 pending 通道）。
            race_after_flip: 取消恰在 `_is_exporting = True` 之后、
                merge 之前到达（直接置 _cancel_requested）。
        """
        cancel_requested = stale
        pending_before_start = False
        is_exporting = False

        def cancel_export() -> None:
            nonlocal cancel_requested, pending_before_start
            cancel_requested = True
            if not is_exporting:
                pending_before_start = True

        if pre_start_cancel:
            cancel_export()
        # --- 源码初始化块（同顺序）---
        cancel_requested = False
        is_exporting = True
        if race_after_flip:
            cancel_export()
        cancel_requested = cancel_requested or pending_before_start
        pending_before_start = False
        return cancel_requested

    def test_race_cancel_survives(self) -> None:
        assert self._simulate(False, False, True) is True

    def test_pre_start_cancel_survives(self) -> None:
        assert self._simulate(False, True, False) is True

    def test_stale_cancel_does_not_leak_into_new_run(self) -> None:
        assert self._simulate(True, False, False) is False


# ================================================================
# AUD-18511 — source_time_seconds 语义文档
# ================================================================

class TestAud18511SourceTimeSemantics:
    def test_docstring_documents_clip_local_semantics(self) -> None:
        """回退敏感性：旧文案（"Time offset within the source media"）复活即失败。"""
        doc = FrameMapResult.__doc__ or ""
        assert "Time offset within the source media" not in doc
        assert "Clip-local" in doc or "clip-local" in doc
        assert "NOT flipped" in doc

    def test_forward_mapping_local_time(self) -> None:
        request = SimpleNamespace(
            timeline_frame=24,
            timeline_fps_num=24,
            timeline_fps_den=1,
            clip_timeline_start=0,
            clip_timeline_duration=100,
            source_start_frame=0,
            source_duration_frames=100,
            source_fps_num=24,
            source_fps_den=1,
            speed=Fraction(1, 1),
            reverse=False,
            source_pts_index=None,
        )
        result = map_frame(request)  # type: ignore[arg-type]
        assert result is not None
        assert abs(result.source_time_seconds - 1.0) < 1e-9

    def test_reverse_mapping_keeps_clip_local_time(self) -> None:
        """reverse 不翻转 source_time_seconds（文档钉死该语义）。"""
        request = SimpleNamespace(
            timeline_frame=24,
            timeline_fps_num=24,
            timeline_fps_den=1,
            clip_timeline_start=0,
            clip_timeline_duration=100,
            source_start_frame=0,
            source_duration_frames=100,
            source_fps_num=24,
            source_fps_den=1,
            speed=Fraction(1, 1),
            reverse=True,
            source_pts_index=None,
        )
        result = map_frame(request)  # type: ignore[arg-type]
        assert result is not None
        assert abs(result.source_time_seconds - 1.0) < 1e-9
        # 帧号翻转：1.0s → 源 100 帧素材的第 75 帧
        assert result.source_frame == 75

    def test_ignores_source_start_offset(self) -> None:
        """入点偏移不计入 source_time_seconds（clip-local）。"""
        request = SimpleNamespace(
            timeline_frame=0,
            timeline_fps_num=24,
            timeline_fps_den=1,
            clip_timeline_start=0,
            clip_timeline_duration=50,
            source_start_frame=480,
            source_duration_frames=100,
            source_fps_num=24,
            source_fps_den=1,
            speed=Fraction(1, 1),
            reverse=False,
            source_pts_index=None,
        )
        result = map_frame(request)  # type: ignore[arg-type]
        assert result is not None
        assert abs(result.source_time_seconds - 0.0) < 1e-9
        assert result.source_frame == 480


# ================================================================
# AUD-18605 — 单效果 + planar/packed 源物化条件
# ================================================================

class TestAud18605MaterializeCondition:
    def _renderer(self) -> Any:
        from src.features.edit.core.timeline_metal_export_renderer import (
            TimelineMetalExportRenderer,
        )
        return TimelineMetalExportRenderer

    def test_single_small_radius_planar_source_materializes(self) -> None:
        """AUD-18605 主修复：单效果 half>1 非 Metal 源 → 物化。"""
        cls = self._renderer()
        effect = {"kind": 2.0, "half_x": 2.0, "half_y": 2.0}
        assert cls._should_materialize_effect_chain((effect,), False) is True

    def test_single_tiny_radius_planar_source_fuses(self) -> None:
        cls = self._renderer()
        effect = {"kind": 2.0, "half_x": 1.0, "half_y": 1.0}
        assert cls._should_materialize_effect_chain((effect,), False) is False

    def test_no_effect_never_materializes(self) -> None:
        cls = self._renderer()
        assert cls._should_materialize_effect_chain((), False) is False

    def test_single_effect_metal_texture_materializes(self) -> None:
        cls = self._renderer()
        effect = {"kind": 2.0, "half_x": 1.0, "half_y": 1.0}
        assert cls._should_materialize_effect_chain((effect,), True) is True

    def test_special_kinds_materialize(self) -> None:
        cls = self._renderer()
        for kind in (6, 7, 8, 9, 10, 11):
            effect = {"kind": float(kind), "half_x": 0.0, "half_y": 0.0}
            assert cls._should_materialize_effect_chain((effect,), False) is True

    def test_multiple_effects_materialize(self) -> None:
        cls = self._renderer()
        effects = (
            {"kind": 2.0, "half_x": 1.0, "half_y": 1.0},
            {"kind": 2.0, "half_x": 1.0, "half_y": 1.0},
        )
        assert cls._should_materialize_effect_chain(effects, False) is True


# ================================================================
# AUD-18809 — 模型 ValueError 转译 TimelineOperationError
# ================================================================

class TestAud18809ExceptionContract:
    def test_move_clip_cross_track_overlap_translated(self) -> None:
        """跨轨移动撞重叠：必须 TimelineOperationError（回退：裸 ValueError）。"""
        track1 = _make_track("track1", (_make_clip("a1", "track1", 0, 100),))
        track2 = _make_track("track2", (_make_clip("b1", "track2", 50, 100),))
        sequence = _make_sequence(track1, track2)

        with pytest.raises(TimelineOperationError) as exc_info:
            move_clip(sequence, "a1", "track2", 100)
        # TimelineOperationError 是 ValueError 子类；断言"是子类类型本身"
        assert type(exc_info.value) is TimelineOperationError
        assert "b1" in str(exc_info.value)

    def test_add_transition_duration_excess_translated(self) -> None:
        track = _make_track(
            "track1",
            (_make_clip("a1", "track1", 0, 100), _make_clip("a2", "track1", 100, 100)),
        )
        sequence = _make_sequence(track)

        with pytest.raises(TimelineOperationError) as exc_info:
            add_transition(sequence, "a1", "a2", 1000)
        assert type(exc_info.value) is TimelineOperationError
        assert "Transition range" in str(exc_info.value) or "exceeds" in str(exc_info.value)

    def test_insert_overlap_translated(self, monkeypatch) -> None:
        """insert 的 with_clip_added ValueError 转译保持（AUD-18803 契约）。"""
        track = _make_track("track1", (_make_clip("a1", "track1", 0, 100),))
        sequence = _make_sequence(track)

        def _raising_with_clip_added(self_track: EditTrack, clip: ClipInstance) -> EditTrack:
            raise ValueError("Clip overlaps with a1")

        monkeypatch.setattr(EditTrack, "with_clip_added", _raising_with_clip_added)
        with pytest.raises(TimelineOperationError) as exc_info:
            insert_clip_at_playhead(sequence, "asset1", 0, 10, 200, "track1")
        assert type(exc_info.value) is TimelineOperationError
        assert "Insert failed" in str(exc_info.value)

    def test_translate_model_error_preserves_message(self) -> None:
        err = _translate_model_error(ValueError("Clip overlaps with x"), "Move clip a1")
        assert isinstance(err, TimelineOperationError)
        assert "Move clip a1" in str(err)
        assert "Clip overlaps with x" in str(err)

    def test_translation_chains_root_cause(self) -> None:
        """raise ... from exc 必须保留根因链（move_clip 路径）。"""
        track1 = _make_track("track1", (_make_clip("a1", "track1", 0, 100),))
        track2 = _make_track("track2", (_make_clip("b1", "track2", 50, 100),))
        sequence = _make_sequence(track1, track2)

        with pytest.raises(TimelineOperationError) as exc_info:
            move_clip(sequence, "a1", "track2", 100)
        assert exc_info.value.__cause__ is not None
        assert "b1" in str(exc_info.value.__cause__)


# ================================================================
# AUD-18816 — insert 波移单次原子重建
# ================================================================

class TestAud18816InsertAtomicRebuild:
    def test_insert_does_not_call_with_clip_updated(self, monkeypatch) -> None:
        """回退敏感性：逐片段 with_clip_updated 回退后计数 > 0。"""
        calls = {"n": 0}
        original = EditTrack.with_clip_updated

        def _spy(self_track: EditTrack, clip: ClipInstance) -> EditTrack:
            calls["n"] += 1
            return original(self_track, clip)

        monkeypatch.setattr(EditTrack, "with_clip_updated", _spy)

        # c0..c28: 0..2900（相邻），straddle 2900..3000 跨播放头 2950，
        # c30..c32: 3000..3300 在播放头之后。
        clips = [_make_clip(f"c{i}", "track1", i * 100, 100) for i in range(29)]
        clips.append(_make_clip("straddle", "track1", 2900, 100))
        clips.append(_make_clip("c30", "track1", 3000, 100))
        clips.append(_make_clip("c31", "track1", 3100, 100))
        clips.append(_make_clip("c32", "track1", 3200, 100))
        track = _make_track("track1", tuple(clips))
        sequence = _make_sequence(track)

        new_sequence = insert_clip_at_playhead(
            sequence, "asset1", 0, 46, 2950, "track1",
        )

        assert calls["n"] == 0

        new_track = new_sequence.get_track_by_id("track1")
        inserted = [
            c for c in new_track.clips if c.timeline_start == 2950
        ]
        assert len(inserted) == 1
        assert inserted[0].timeline_duration == 47  # int(47/1.0)
        # 跨播放头片段（2900..3000）被分割：左半 2900..2950 保留，
        # 右半（2950..3000，长 50）随波移右移 47 → 2997 起。
        split_right = [
            c for c in new_track.clips
            if c.timeline_start == 2997 and c.clip_id != inserted[0].clip_id
        ]
        assert len(split_right) == 1
        assert split_right[0].timeline_duration == 50
        # 后续片段整体右移 47
        shifted = {
            c.clip_id: c.timeline_start
            for c in new_track.clips
            if c.clip_id in ("c30", "c31", "c32")
        }
        assert shifted == {"c30": 3047, "c31": 3147, "c32": 3247}

    def test_insert_result_matches_reference_ripple(self) -> None:
        """与逐片段参考实现结果完全一致（顺序/时长/源范围）。"""
        clips = [_make_clip(f"c{i}", "track1", i * 100, 100) for i in range(6)]
        clips.append(_make_clip("mid", "track1", 600, 100))  # 600..700 跨 650
        clips.extend(
            _make_clip(f"c{i}", "track1", i * 100, 100) for i in range(7, 12)
        )
        track = _make_track("track1", tuple(clips))
        sequence = _make_sequence(track)
        playhead = 650

        new_sequence = insert_clip_at_playhead(
            sequence, "asset1", 0, 32, playhead, "track1",
        )
        new_track = new_sequence.get_track_by_id("track1")

        # 参考结果：分割 mid(600-700) → left(600-650)+right(650-700)；
        # right 与所有 >=650 的片段右移 33；新片段落在 650。
        starts = sorted(c.timeline_start for c in new_track.clips)
        assert starts == sorted(
            [0, 100, 200, 300, 400, 500, 600, 650, 683,
             733, 833, 933, 1033, 1133]
        )
        new_clip = next(c for c in new_track.clips if c.timeline_start == 650)
        assert new_clip.timeline_duration == 33
        assert new_clip.source_duration == 33


# ================================================================
# AUD-18905 — _composite_layers try/finally
# ================================================================

class TestAud18905CompositeFrameRestore:
    def _worker(self):
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

        return PreviewRenderWorker(
            frame_provider=None,
            asset_resolver=None,
            video_renderer=None,
            timeline_sequence=None,
            viewport_size=(64, 64),
        )

    def _layer(self) -> Any:
        from src.features.edit.core.timeline_render_plan import ClipLayerInfo

        return ClipLayerInfo(
            clip=_make_clip("c1", "track1", 0, 100),
            source_frame=0,
            track_index=0,
            track_name="V1",
            opacity=1.0,
            transform=(0, 0, 1, 1, 0, "center", 0, 0),
            crop=(0, 0, 0, 0, 0),
        )

    def test_exception_restores_missing_attribute(self, monkeypatch) -> None:
        worker = self._worker()
        try:
            layer = self._layer()

            def _boom(_layer: Any) -> None:
                raise RuntimeError("decode exploded")

            monkeypatch.setattr(worker, "_get_layer_frame", _boom)

            with pytest.raises(RuntimeError):
                worker._composite_layers((layer,), (), 5)

            # 回退：无 try/finally → 属性残留 == 5 → 此断言失败
            assert "_current_render_timeline_frame" not in worker.__dict__
        finally:
            worker.stop()

    def test_exception_restores_previous_value(self, monkeypatch) -> None:
        worker = self._worker()
        try:
            worker._current_render_timeline_frame = 42
            layer = self._layer()

            def _boom(_layer: Any) -> None:
                raise RuntimeError("x")

            monkeypatch.setattr(worker, "_get_layer_frame", _boom)
            with pytest.raises(RuntimeError):
                worker._composite_layers((layer,), (), 5)
            assert worker._current_render_timeline_frame == 42
        finally:
            worker.stop()

    def test_no_layers_returns_without_setting_attribute(self) -> None:
        worker = self._worker()
        try:
            assert worker._composite_layers((), (), 7) is None
            assert "_current_render_timeline_frame" not in worker.__dict__
        finally:
            worker.stop()


# ================================================================
# AUD-18906 — _mg_static_frames 淘汰防空表竞态
# ================================================================

class _RaceDict(dict):
    """len() 首次调用时清空自身但谎报满载——确定性复现 UI clear() 竞态。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.armed = True

    def __len__(self) -> int:
        if self.armed:
            self.armed = False
            dict.clear(self)
            return 1_000_000
        return dict.__len__(self)


class TestAud18906MgStaticFramesEviction:
    def _worker_and_patches(self, monkeypatch):
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

        fake_coordinator = SimpleNamespace(
            cache=SimpleNamespace(
                nearest_frame_for_scene=lambda scene_id, frame: (7, "rendered"),
            )
        )
        monkeypatch.setattr(
            "src.features.teaching_animation.scene_cache.get_shared_scene_cache_coordinator",
            lambda: fake_coordinator,
        )
        monkeypatch.setattr(
            "src.features.teaching_animation.host_adapters.reset_teaching_scene_frame_schedule",
            lambda scene_id: None,
        )
        monkeypatch.setattr(
            "src.features.teaching_animation.render.frame_adapter.FrameAdapter.from_rendered_frame",
            lambda rendered: "rgba",
        )

        worker = PreviewRenderWorker(
            frame_provider=None,
            asset_resolver=None,
            video_renderer=None,
            timeline_sequence=None,
            viewport_size=(64, 64),
        )
        monkeypatch.setattr(
            worker, "_convert_rgba_to_preview_frame",
            lambda rgba: np.full((4, 4, 3), 7, dtype=np.uint8),
        )
        return worker

    def _layer(self) -> Any:
        from src.features.edit.core.timeline_render_plan import ClipLayerInfo

        return ClipLayerInfo(
            clip=_make_clip("mg1", "track1", 0, 100),
            source_frame=3,
            track_index=0,
            track_name="MG",
            opacity=1.0,
            transform=(0, 0, 1, 1, 0, "center", 0, 0),
            crop=(0, 0, 0, 0, 0),
        )

    def test_eviction_survives_concurrent_clear(self, monkeypatch) -> None:
        """len 判定后表被清空 → 不得 StopIteration（回退：抛 StopIteration）。"""
        worker = self._worker_and_patches(monkeypatch)
        try:
            race_dict = _RaceDict()
            race_dict[("old", 0, (64, 64))] = "frame"
            worker._mg_static_frames = race_dict
            worker._mg_static_frames_max = 32

            frame = worker._get_motion_graphic_static_frame(self._layer(), "scene1")
            assert frame is not None
            # 竞态后表里是新写入的 memo 项
            assert len(race_dict) == 1
        finally:
            worker.stop()

    def test_normal_eviction_removes_oldest(self, monkeypatch) -> None:
        worker = self._worker_and_patches(monkeypatch)
        try:
            store = {}
            for i in range(32):
                store[((f"s{i}"), 7, (64, 64))] = f"frame{i}"
            worker._mg_static_frames = store
            worker._mg_static_frames_max = 32

            worker._get_motion_graphic_static_frame(self._layer(), "s-new")

            assert len(store) == 32
            assert (("s0"), 7, (64, 64)) not in store
            assert (("s-new"), 7, (64, 64)) in store
        finally:
            worker.stop()


# ================================================================
# AUD-18909 — comp GL context/surface 销毁
# ================================================================

class TestAud18909CompGlTeardown:
    def _stub_worker(self) -> SimpleNamespace:
        context = SimpleNamespace(done_current=lambda: None)
        surface = SimpleNamespace(destroyed=0)
        surface.destroy = lambda: setattr(surface, "destroyed", surface.destroyed + 1)
        return SimpleNamespace(
            _comp_gl_context=context,
            _comp_gl_surface=surface,
            _comp_gl_context_current=True,
            _comp_gl_ready=True,
            _comp_gpu_direct_enabled=True,
        )

    def test_teardown_destroys_and_clears(self) -> None:
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

        stub = self._stub_worker()
        surface = stub._comp_gl_surface
        PreviewRenderWorker._teardown_comp_gl_resources(stub)

        assert stub._comp_gl_context is None
        assert stub._comp_gl_surface is None
        assert surface.destroyed == 1
        assert stub._comp_gl_ready is False
        assert stub._comp_gpu_direct_enabled is False

    def test_teardown_idempotent(self) -> None:
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

        stub = self._stub_worker()
        PreviewRenderWorker._teardown_comp_gl_resources(stub)
        surface = SimpleNamespace(destroyed=0)
        surface.destroy = lambda: setattr(surface, "destroyed", surface.destroyed + 1)
        stub._comp_gl_surface = surface  # 模拟重复调用前重建（不应发生，但幂等保护）
        PreviewRenderWorker._teardown_comp_gl_resources(stub)
        assert surface.destroyed == 1
        assert stub._comp_gl_surface is None

    def test_teardown_skips_done_current_when_not_current(self) -> None:
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

        stub = self._stub_worker()
        stub._comp_gl_context_current = False
        done_calls = {"n": 0}
        context = SimpleNamespace(
            done_current=lambda: done_calls.update(n=done_calls["n"] + 1)
        )
        stub._comp_gl_context = context
        PreviewRenderWorker._teardown_comp_gl_resources(stub)
        assert done_calls["n"] == 0
        assert stub._comp_gl_context is None

    def test_worker_stop_wires_gl_teardown(self) -> None:
        """stop 的统一销毁钩子必须调用 GL teardown（源级接线守卫）。"""
        path = (
            Path(__file__).resolve().parents[2]
            / "src" / "features" / "edit" / "core" / "timeline_preview_renderer.py"
        )
        source = path.read_text()
        # _teardown_decode_resources 钩子 + shutdown 兜底
        assert source.count("self._teardown_comp_gl_resources()") >= 1
        assert source.count("._teardown_comp_gl_resources()") >= 1


# ================================================================
# AUD-19208 — 多边形 mask 网格/结果缓存
# ================================================================

class TestAud19208PolygonMaskCache:
    def test_second_call_served_from_cache(self, monkeypatch) -> None:
        _polygon_mask_cache.clear()
        _coord_grid_cache.clear()

        first = compute_transition_mask(64, 48, 0.5, "iris_heart")
        assert ("iris_heart", 64, 48, 0.5) in _polygon_mask_cache
        assert (64, 48) in _coord_grid_cache

        # 回退敏感性：缓存命中路径不得再进逐边循环。
        # 回退到旧实现（无缓存）时 _polygon_mask 会被再次调用 → 计数 > 0。
        calls = {"n": 0}
        original = _polygon_mask

        def _spy(x, y, vertices):
            calls["n"] += 1
            return original(x, y, vertices)

        monkeypatch.setattr(
            "src.features.edit.core.transition_mask._polygon_mask", _spy
        )
        second = compute_transition_mask(64, 48, 0.5, "iris_heart")
        assert calls["n"] == 0
        np.testing.assert_array_equal(first, second)

    def test_cached_result_bit_identical_to_fresh_compute(self) -> None:
        _polygon_mask_cache.clear()
        _coord_grid_cache.clear()

        width, height, progress, kind = 96, 64, 0.37, "iris_star"
        cached = compute_transition_mask(width, height, progress, kind)

        # 独立重算参考值
        yy, xx = np.mgrid[0:height, 0:width]
        x = (xx.astype(np.float32) + 0.5) / float(width) * 2.0 - 1.0
        y = (yy.astype(np.float32) + 0.5) / float(height) * 2.0 - 1.0
        x *= float(width) / float(height)
        from src.features.edit.core.transition_mask import _shape_vertices

        vertices = _shape_vertices(kind) * max(0.0, progress)
        reference = _polygon_mask(x, y, vertices).astype(np.float32)
        np.testing.assert_array_equal(cached, reference)

    def test_returned_array_mutation_does_not_poison_cache(self) -> None:
        _polygon_mask_cache.clear()
        first = compute_transition_mask(32, 32, 0.6, "iris_square")
        expected = first.copy()
        first[:] = 0.0  # 调用方破坏返回值
        second = compute_transition_mask(32, 32, 0.6, "iris_square")
        np.testing.assert_array_equal(second, expected)

    def test_different_progress_not_served_from_cache(self, monkeypatch) -> None:
        _polygon_mask_cache.clear()
        _coord_grid_cache.clear()
        compute_transition_mask(64, 48, 0.3, "iris_hexagon")
        calls = {"n": 0}
        original = _polygon_mask

        def _spy(x, y, vertices):
            calls["n"] += 1
            return original(x, y, vertices)

        monkeypatch.setattr(
            "src.features.edit.core.transition_mask._polygon_mask", _spy
        )
        compute_transition_mask(64, 48, 0.4, "iris_hexagon")
        assert calls["n"] == 1  # 不同 progress 必须重算

    def test_grid_cache_bounded(self) -> None:
        _coord_grid_cache.clear()
        for w in range(8, 40):
            compute_transition_mask(w, 16, 0.5, "iris_circle")
        assert len(_coord_grid_cache) <= 16
