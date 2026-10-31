"""wd10-g60 修复批次的回归测试（REMEDIATION_PLAN 批 D，AUD-17700~18110）。

覆盖本批 16 个条目的根因修复：

- render_cache_worker.py：AUD-17700（初始化异常仍发 failed 信号 +
  _parse_fps 对 None/畸形 fps 的容错）
- scene_cut/apply_ops.py：AUD-17801（apply_splits 按 timeline_frame
  去重，保留 confidence 最高者并计入 warnings）
- scene_cut/cache.py：AUD-17810（load_analysis 捕获 ValueError，
  维持 "invalid → None" 契约）
- scene_cut/detector.py：AUD-17901（CancelledError 不再误报 ERROR）、
  AUD-17902（失败按契约抛 RuntimeError）、AUD-17903（cancel() 转发到
  进行中检测的 token）、AUD-17904（流式管线剥离 frame_data）
- scene_cut/filters.py：AUD-17910（自适应阈值逐帧滚动，当前帧不参与
  自身阈值）
- scene_cut/frame_sampler.py：AUD-17920（seek 保留起始帧，标签不错位）、
  AUD-17921（连续转换失败熔断 + 失败计数）
- scene_cut/models.py：AUD-18010（to_hash 覆盖全部设置字段）、
  AUD-18011（派生分析保留 proxy_fingerprint）、AUD-18012
  （timecode_to_frames 委托 interchange DF 实现）、AUD-18013
  （首场景起始秒按真实时间初始化）
- scene_cut/subclips.py：AUD-18100（dataclasses.replace 保留 revision）
- scene_cut/timeline_mapping.py：AUD-18110（CFR 反向映射补齐 fps 换算）

全部无 GL / 无真实媒体驱动（桩采样器 + 桩容器 + 纯数据断言）。
"""

from __future__ import annotations

import dataclasses
import os
import threading
import uuid
from fractions import Fraction
from typing import Any, Iterator

import numpy as np
import pytest

from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
from src.features.edit.core.render_cache_worker import RenderCacheWorker
from src.features.edit.core.scene_cut.apply_ops import apply_splits
from src.features.edit.core.scene_cut.cache import SceneCutCache
from src.features.edit.core.scene_cut.detector import (
    DetectionPhase,
    SceneCutDetector,
)
from src.features.edit.core.scene_cut.filters import (
    FrameMetricsEntry,
    RollingStats,
    SceneCutFilterPipeline,
)
from src.features.edit.core.scene_cut.frame_sampler import (
    CancelledError,
    CancelToken,
    MAX_CONSECUTIVE_CONVERT_FAILURES,
    SampledFrame,
    VideoFrameSampler,
)
from src.features.edit.core.scene_cut.models import (
    FrameTimePoint,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
    SceneCutMetrics,
    SceneCutSettings,
    SourceFrameTimeIndex,
    generate_scene_subclips,
    timecode_to_frames,
    _frames_to_timecode,
)
from src.features.edit.core.scene_cut.subclips import add_subclips_to_bin
from src.features.edit.core.scene_cut.timeline_mapping import (
    ClipMappingConfig,
    ClipSourceMapper,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


# ================================================================
# 桩：帧采样器 / PyAV 帧
# ================================================================


class _StubSampler:
    """detector.VideoFrameSampler 的桩：可注入任意行为。"""

    def __init__(
        self,
        source_path: str,
        settings: SceneCutSettings,
        cancel_token: CancelToken | None = None,
        frames: list[SampledFrame] | None = None,
        error: Exception | None = None,
        wait_for_cancel: bool = False,
    ) -> None:
        self._settings = settings
        self._cancel_token = cancel_token or CancelToken()
        self._frames = frames or []
        self._error = error
        self._wait_for_cancel = wait_for_cancel
        # detector 读取的元数据属性
        self.original_width = 1920
        self.original_height = 1080
        self.target_width = settings.sample_width
        self.target_height = settings.max_sample_height
        self.total_frames = max(1, len(self._frames))
        self.fps = 24.0
        self.total_convert_failures = 0

    def open(self) -> None:
        return None

    def sample_frames(self) -> Iterator[Any]:
        if self._wait_for_cancel:
            while not self._cancel_token.is_cancelled():
                threading.Event().wait(0.005)
            self._cancel_token.check_and_raise()
        if self._error is not None:
            raise self._error
        yield from self._frames


def _make_sampled_frame(source_frame: int, frame_data: np.ndarray) -> SampledFrame:
    """构造真实 SampledFrame（dataclasses.replace 兼容）。"""
    return SampledFrame(
        time_point=FrameTimePoint(
            source_frame=source_frame,
            pts=source_frame,
            seconds=source_frame / 24.0,
        ),
        frame_data=frame_data,
        width=frame_data.shape[1] if frame_data.ndim == 3 else 0,
        height=frame_data.shape[0] if frame_data.ndim == 3 else 0,
        original_width=1920,
        original_height=1080,
    )


class _RecordingPipeline:
    """SceneCutFilterPipeline 桩：记录收到的 entries。"""

    instances: list["_RecordingPipeline"] = []

    def __init__(self, settings: SceneCutSettings) -> None:
        self.settings = settings
        self.entries: list[FrameMetricsEntry] = []
        self.diagnostics: dict[str, Any] = {"total_candidates": 0}
        _RecordingPipeline.instances.append(self)

    def process(
        self,
        entries: list[FrameMetricsEntry],
        progress_callback: Any = None,
    ) -> list[SceneCutCandidate]:
        self.entries = list(entries)
        return []


def _media_file(tmp_path: Any) -> str:
    path = os.path.join(str(tmp_path), "media.mp4")
    with open(path, "wb") as f:
        f.write(b"stub")
    return path


def _frames(count: int) -> list[SampledFrame]:
    rng = np.random.default_rng(42)
    return [
        _make_sampled_frame(i, rng.integers(0, 255, (144, 256, 3), dtype=np.uint8))
        for i in range(count)
    ]


# ================================================================
# AUD-17700 — render_cache_worker 初始化失败仍发 failed
# ================================================================


class TestAud17700:
    def test_parse_fps_none_returns_default(self) -> None:
        assert RenderCacheWorker._parse_fps(None) == (24, 1)  # type: ignore[arg-type]
        assert RenderCacheWorker._parse_fps("not-a-fps") == (24, 1)
        assert RenderCacheWorker._parse_fps("24000/1001") == (24000, 1001)
        assert RenderCacheWorker._parse_fps("24") == (24, 1)

    @pytest.mark.usefixtures("qapp")
    def test_init_failure_emits_failed_signal(self, tmp_path: Any) -> None:
        # dirname(output_path) 是一个已存在的普通文件 → makedirs 抛
        # NotADirectoryError（try 块之前的初始化代码路径，AUD-17700）。
        blocker = os.path.join(str(tmp_path), "blocker")
        with open(blocker, "wb") as f:
            f.write(b"x")
        manifest = type(
            "M",
            (),
            {
                "cache_id": "cache-1",
                "start_frame": 0,
                "duration_frames": 10,
                "timeline_fps": None,  # 同时覆盖 _parse_fps(None)
                "path": os.path.join(blocker, "cache.mov"),
                "width": 64,
                "height": 64,
                "codec": "prores",
                "render_kind": "comp_clip",
                "composite_id": "g1",
                "scene_id": "",
            },
        )()
        worker = RenderCacheWorker(manifest, {}, {})
        failures: list[tuple[str, str]] = []
        worker.failed.connect(lambda cid, msg: failures.append((cid, msg)))
        worker.run()  # 直接在测试线程执行 run()
        assert len(failures) == 1
        assert failures[0][0] == "cache-1"
        assert "Render cache init failed" in failures[0][1]


# ================================================================
# AUD-17801 — apply_splits 去重相同 timeline_frame
# ================================================================


def _split_timeline() -> tuple[TimelineSequence, str]:
    clip = ClipInstance(
        clip_id=str(uuid.uuid4()),
        asset_id=str(uuid.uuid4()),
        track_id="v1",
        timeline_start=0,
        timeline_duration=1000,
        source_start=0,
        source_duration=1000,
    )
    track = EditTrack(
        track_id="v1",
        track_type="video",
        name="V1",
        clips=(clip,),
        transitions=(),
        is_locked=False,
        is_muted=False,
    )
    timeline = TimelineSequence(
        timeline_id=str(uuid.uuid4()),
        name="T",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(track,),
        audio_tracks=(),
        markers=(),
    )
    return timeline, clip.clip_id


def _cut_candidate(
    source_frame: int, timeline_frame: int, confidence: float
) -> SceneCutCandidate:
    metrics = SceneCutMetrics(
        luma_delta=0.6,
        hist_delta=0.5,
        edge_delta=0.4,
        color_delta=0.3,
        black_score=0.0,
        flash_score=0.0,
        motion_score=0.0,
        combined_score=0.8,
    )
    return SceneCutCandidate(
        source_frame=source_frame,
        pts=source_frame,
        seconds=source_frame / 24.0,
        timeline_frame=timeline_frame,
        score=0.8,
        confidence=confidence,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=metrics,
    )


class TestAud17801:
    def test_duplicate_timeline_frames_deduplicated(self) -> None:
        timeline, clip_id = _split_timeline()
        candidates = (
            _cut_candidate(100, 200, 0.60),
            _cut_candidate(101, 200, 0.90),  # 同一 timeline_frame，更高置信度
            _cut_candidate(300, 400, 0.70),
        )
        result = apply_splits(
            candidates=candidates,
            timeline=timeline,
            target_clip_id=clip_id,
            target_track_id="v1",
        )
        assert result.split_count == 2
        assert sorted(result.split_positions) == [200, 400]
        assert any("duplicate split position" in w for w in result.warnings)
        # 命令数与去重后位置一致（不再有第二条同帧 SplitClipCommand）
        assert len(result.commands) == result.split_count

    def test_no_duplicate_no_warning(self) -> None:
        timeline, clip_id = _split_timeline()
        candidates = (
            _cut_candidate(100, 200, 0.60),
            _cut_candidate(300, 400, 0.70),
        )
        result = apply_splits(
            candidates=candidates,
            timeline=timeline,
            target_clip_id=clip_id,
            target_track_id="v1",
        )
        assert result.split_count == 2
        assert not any("duplicate" in w for w in result.warnings)


# ================================================================
# AUD-17810 — load_analysis 捕获 ValueError
# ================================================================


class TestAud17810:
    def test_unknown_enum_kind_returns_none(self, tmp_path: Any) -> None:
        cache = SceneCutCache(project_cache_dir=str(tmp_path))
        data = {
            "analysis_id": "a1",
            "asset_id": "asset-1",
            "source_path": "/m.mp4",
            "detector_version": "1.0.0",
            "settings": SceneCutSettings().to_dict(),
            "candidates": [
                {
                    "source_frame": 10,
                    "pts": 10,
                    "seconds": 0.4,
                    "score": 0.8,
                    "confidence": 0.9,
                    "kind": "bogus_kind",  # 未知枚举 → ValueError
                    "decision": "accepted",
                }
            ],
        }
        import json

        path = cache.analysis_cache.get_analysis_path("bad")
        path.write_text(json.dumps(data), encoding="utf-8")
        assert cache.load_analysis("bad") is None

    def test_out_of_range_settings_returns_none(self, tmp_path: Any) -> None:
        cache = SceneCutCache(project_cache_dir=str(tmp_path))
        data = {
            "analysis_id": "a1",
            "asset_id": "asset-1",
            "source_path": "/m.mp4",
            "detector_version": "1.0.0",
            # sensitivity 越界 → __post_init__ ValueError
            "settings": dict(SceneCutSettings().to_dict(), sensitivity=9.9),
            "candidates": [],
        }
        import json

        path = cache.analysis_cache.get_analysis_path("bad2")
        path.write_text(json.dumps(data), encoding="utf-8")
        assert cache.load_analysis("bad2") is None


# ================================================================
# AUD-17901 / 17902 / 17903 / 17904 — detector
# ================================================================


class TestAud17901:
    def test_cancelled_error_not_reported_as_error(self, tmp_path: Any, monkeypatch) -> None:
        media = _media_file(tmp_path)
        monkeypatch.setattr(
            "src.features.edit.core.scene_cut.detector.VideoFrameSampler",
            lambda *a, **k: _StubSampler(media, SceneCutSettings(), error=CancelledError("stop")),
        )
        detector = SceneCutDetector(media, "asset-1")
        phases: list[DetectionPhase] = []

        def on_progress(progress: Any) -> None:
            phases.append(progress.phase)

        with pytest.raises(CancelledError):
            detector.detect(progress_callback=on_progress)
        assert DetectionPhase.CANCELLED in phases
        assert DetectionPhase.ERROR not in phases


class TestAud17902:
    def test_failure_raises_runtime_error(self, tmp_path: Any, monkeypatch) -> None:
        media = _media_file(tmp_path)
        monkeypatch.setattr(
            "src.features.edit.core.scene_cut.detector.VideoFrameSampler",
            lambda *a, **k: _StubSampler(
                media, SceneCutSettings(), error=RuntimeError("decoder exploded")
            ),
        )
        detector = SceneCutDetector(media, "asset-1")
        with pytest.raises(RuntimeError, match="Scene cut detection failed"):
            detector.detect()


class TestAud17903:
    def test_cancel_stops_in_flight_detection(self, tmp_path: Any, monkeypatch) -> None:
        media = _media_file(tmp_path)
        holder: dict[str, Any] = {}

        def make_sampler(*a: Any, **k: Any) -> _StubSampler:
            sampler = _StubSampler(
                media, SceneCutSettings(), cancel_token=k.get("cancel_token"),
                wait_for_cancel=True,
            )
            holder["token"] = sampler._cancel_token
            return sampler

        monkeypatch.setattr(
            "src.features.edit.core.scene_cut.detector.VideoFrameSampler", make_sampler
        )
        detector = SceneCutDetector(media, "asset-1")
        outcome: dict[str, BaseException | None] = {"exc": None}

        def run_detect() -> None:
            try:
                detector.detect()
            except BaseException as e:  # noqa: BLE001 — 记录线程内异常
                outcome["exc"] = e

        thread = threading.Thread(target=run_detect)
        thread.start()
        # 等待 detect() 真正进入采样等待
        for _ in range(200):
            if "token" in holder:
                break
            threading.Event().wait(0.01)
        detector.cancel()  # 公共取消 API 必须能取消进行中的检测
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert isinstance(outcome["exc"], CancelledError)


class TestAud17904:
    def test_metric_entries_strip_pixel_payload(self, tmp_path: Any, monkeypatch) -> None:
        media = _media_file(tmp_path)
        frames = _frames(12)
        monkeypatch.setattr(
            "src.features.edit.core.scene_cut.detector.VideoFrameSampler",
            lambda *a, **k: _StubSampler(media, SceneCutSettings(), frames=frames),
        )
        _RecordingPipeline.instances.clear()
        monkeypatch.setattr(
            "src.features.edit.core.scene_cut.detector.SceneCutFilterPipeline",
            _RecordingPipeline,
        )
        detector = SceneCutDetector(media, "asset-1")
        analysis = detector.detect()
        assert len(_RecordingPipeline.instances) == 1
        entries = _RecordingPipeline.instances[0].entries
        assert len(entries) == 11  # 12 帧 → 11 个差分 entry
        # 像素载荷被剥离（0 字节），时间元数据完整保留
        assert all(e.frame.frame_data.nbytes == 0 for e in entries)
        assert [e.frame.source_frame for e in entries] == list(range(1, 12))
        assert [e.metrics is not None for e in entries] == [True] * 11
        assert len(analysis.candidates) == 0  # 桩管线无候选


# ================================================================
# AUD-17910 — 逐帧滚动自适应阈值
# ================================================================


def _entry(i: int, score: float) -> FrameMetricsEntry:
    stub = _make_sampled_frame(i, np.empty((0, 0, 3), dtype=np.uint8))
    metrics = SceneCutMetrics(
        luma_delta=min(score, 1.0),
        hist_delta=score,
        edge_delta=score,
        color_delta=score,
        black_score=0.0,
        flash_score=0.0,
        motion_score=0.0,
        combined_score=score,
    )
    return FrameMetricsEntry(frame=stub, metrics=metrics, frame_index=i)


class TestAud17910:
    def test_early_spike_detected_despite_hot_tail(self) -> None:
        # 60 帧：前 30 帧低分（idx5=0.65 尖峰），后 30 帧高分 0.9。
        # 旧行为：单一阈值取自"片尾 30 帧"窗口 → clamp 后 0.7，0.65 尖峰被漏检。
        # 新行为：第 5 帧的阈值只由第 0..4 帧窗口决定（≈0.1）→ 检出。
        entries = [_entry(i, 0.1) for i in range(60)]
        entries[5] = _entry(5, 0.65)
        for i in range(30, 60):
            entries[i] = _entry(i, 0.9)

        # 文档化旧行为的阈值（尾窗口 median=0.9 → clamp 0.7）
        legacy = RollingStats(window_size=30)
        for e in entries:
            legacy.update(e.metrics.combined_score)
        assert legacy.get_threshold(0.55) == pytest.approx(0.7)
        assert 0.65 < legacy.get_threshold(0.55)  # 旧行为下尖峰低于阈值

        pipeline = SceneCutFilterPipeline(
            SceneCutSettings(adaptive_threshold=True, suppress_flash=False)
        )
        candidates = pipeline.process(entries)
        assert any(c.source_frame == 5 for c in candidates)
        assert pipeline.diagnostics["adaptive_threshold_used"] is True

    def test_current_frame_not_in_own_threshold(self) -> None:
        # 单一尖峰帧：其阈值只来自前序帧（全 0.1）→ 命中；
        # 若当前帧参与自身阈值（旧第一遍全量统计），首轮窗口<5 仍走固定
        # 阈值 0.425，0.65 同样命中——因此另用"阈值必须随帧滚动"断言：
        # diagnostics 记录的是最后一帧的滚动阈值（0.9 主导的尾窗口）。
        entries = [_entry(i, 0.1) for i in range(40)]
        entries[5] = _entry(5, 0.65)
        for i in range(20, 40):
            entries[i] = _entry(i, 0.9)
        pipeline = SceneCutFilterPipeline(
            SceneCutSettings(adaptive_threshold=True, suppress_flash=False)
        )
        pipeline.process(entries)
        last_threshold = pipeline.diagnostics["threshold_value"]
        # 尾窗口被 0.9 主导 → 最终阈值接近 0.7（clamp 上限），
        # 与开头低分窗口的 ≈0.1 阈值不同 → 阈值确实逐帧滚动。
        assert last_threshold > 0.5


# ================================================================
# AUD-17920 / AUD-17921 — frame_sampler
# ================================================================


class _FakeSmall:
    def __init__(self, fail: bool, width: int, height: int) -> None:
        self._fail = fail
        self._width = width
        self._height = height

    def reformat(self, format: str | None = None, **_: Any) -> "_FakeSmall":
        if self._fail:
            raise ValueError("reformat failed")
        return self

    def to_ndarray(self) -> np.ndarray:
        return np.zeros((self._height, self._width, 3), dtype=np.uint8)


class _FakeAvFrame:
    def __init__(self, pts: int, fail_convert: bool = False) -> None:
        self.pts = pts
        self.duration = 1
        self.is_keyframe = pts == 0
        self._fail = fail_convert

    def reformat(self, width: int | None = None, height: int | None = None,
                 format: str | None = None, **_: Any) -> _FakeSmall:
        assert width is not None and height is not None
        return _FakeSmall(self._fail, width, height)


class _FakeContainer:
    def __init__(self, frame_count: int, fail_convert: bool = False) -> None:
        self._frames = [_FakeAvFrame(i, fail_convert) for i in range(frame_count)]
        self.seek_calls: list[int] = []

    def seek(self, pts: int, stream: Any = None, any_frame: bool = False) -> None:
        self.seek_calls.append(pts)

    def decode(self, stream: Any) -> Iterator[_FakeAvFrame]:
        return iter(list(self._frames))


def _stubbed_sampler(
    settings: SceneCutSettings, frame_count: int = 20
) -> tuple[VideoFrameSampler, _FakeContainer]:
    sampler = VideoFrameSampler.__new__(VideoFrameSampler)
    sampler._source_path = "/stub.mp4"
    sampler._settings = settings
    sampler._cancel_token = CancelToken()
    sampler._container = _FakeContainer(frame_count)
    sampler._video_stream = object()
    sampler._is_open = True
    sampler._current_frame = 0
    sampler._frame_iterator = None
    sampler._held_frame = None
    sampler._fps = 24.0
    sampler._time_base = 1.0 / 24.0  # pts=帧号 → seconds=帧号/24（CFR 一致）
    sampler._total_frames = frame_count
    sampler._original_width = 1920
    sampler._original_height = 1080
    sampler._target_width = 256
    sampler._target_height = 144
    sampler._convert_stats_lock = threading.Lock()
    sampler._consecutive_convert_failures = 0
    sampler._total_convert_failures = 0
    return sampler, sampler._container


class TestAud17920:
    def test_seek_holds_start_frame_and_labels_align(self) -> None:
        settings = SceneCutSettings(start_source_frame=10, end_source_frame=15)
        sampler, container = _stubbed_sampler(settings)
        frames = list(sampler.sample_frames())
        assert [f.source_frame for f in frames] == [10, 11, 12, 13, 14]
        assert frames[0].seconds == pytest.approx(10.0 / 24.0)
        # seek 生效（不从头解码静默补偿）
        assert container.seek_calls  # 时间基 1.0 → target_pts=0 也会调用 seek

    def test_build_time_index_start_frame_preserved(self) -> None:
        settings = SceneCutSettings(start_source_frame=10, end_source_frame=15)
        sampler, _ = _stubbed_sampler(settings)
        points = sampler.build_time_index()
        assert [p.source_frame for p in points] == [10, 11, 12, 13, 14]
        assert points[0].seconds == pytest.approx(10.0 / 24.0)


class TestAud17921:
    def test_occasional_failure_falls_back_to_black(self) -> None:
        sampler, _ = _stubbed_sampler(SceneCutSettings())
        result = sampler._convert_frame_data(_FakeAvFrame(0, fail_convert=True))
        assert result.shape == (144, 256, 3)
        assert not result.any()
        assert sampler.total_convert_failures == 1

    def test_consecutive_failures_trip_breaker(self) -> None:
        sampler, _ = _stubbed_sampler(SceneCutSettings())
        for _ in range(MAX_CONSECUTIVE_CONVERT_FAILURES - 1):
            sampler._convert_frame_data(_FakeAvFrame(0, fail_convert=True))
        with pytest.raises(RuntimeError, match="consecutive"):
            sampler._convert_frame_data(_FakeAvFrame(0, fail_convert=True))
        assert (
            sampler.total_convert_failures == MAX_CONSECUTIVE_CONVERT_FAILURES
        )

    def test_success_resets_consecutive_counter(self) -> None:
        sampler, _ = _stubbed_sampler(SceneCutSettings())
        for _ in range(MAX_CONSECUTIVE_CONVERT_FAILURES - 1):
            sampler._convert_frame_data(_FakeAvFrame(0, fail_convert=True))
        sampler._convert_frame_data(_FakeAvFrame(0, fail_convert=False))  # 成功复位
        # 复位后再失败一次不应熔断
        result = sampler._convert_frame_data(_FakeAvFrame(0, fail_convert=True))
        assert result.shape == (144, 256, 3)


# ================================================================
# AUD-18010 — to_hash 覆盖全部设置字段
# ================================================================


class TestAud18010:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("rolling_window_size", 60),
            ("black_score_threshold", 0.9),
            ("motion_score_threshold", 0.8),
            ("flash_score_threshold", 0.6),
            ("transition_frame_threshold", 20),
        ],
    )
    def test_suppression_thresholds_change_hash(self, field: str, value: Any) -> None:
        base = SceneCutSettings()
        changed = dataclasses.replace(base, **{field: value})
        assert changed.to_hash() != base.to_hash()

    def test_hash_covers_every_dataclass_field(self) -> None:
        # 防字段再次遗漏的守护断言：任何字段的可分辨变化都必须改变哈希。
        # 覆盖值需通过 __post_init__ 的范围校验。
        base = SceneCutSettings()
        valid_alt: dict[str, Any] = {
            "sensitivity": 0.4242,
            "min_scene_frames": 9,
            "sample_width": 320,
            "max_sample_height": 200,
            "use_proxy": True,
            "detect_transitions": True,
            "suppress_flash": True,
            "suppress_fast_motion": True,
            "suppress_black_fades": True,
            "adaptive_threshold": True,
            "rolling_window_size": 60,
            "black_score_threshold": 0.9,
            "motion_score_threshold": 0.8,
            "flash_score_threshold": 0.6,
            "transition_frame_threshold": 20,
            "start_source_frame": 5,
            "end_source_frame": 12345,
        }
        field_names = {f.name for f in dataclasses.fields(SceneCutSettings)}
        assert field_names == set(valid_alt.keys()), "测试覆盖表与字段集不同步"
        for name, value in valid_alt.items():
            current = getattr(base, name)
            if isinstance(current, bool):
                new = not current
            else:
                new = value
            changed = dataclasses.replace(base, **{name: new})
            assert changed.to_hash() != base.to_hash(), (
                f"to_hash() does not cover field {name}"
            )

    def test_equal_settings_equal_hash(self) -> None:
        assert SceneCutSettings().to_hash() == SceneCutSettings().to_hash()


# ================================================================
# AUD-18011 — 派生分析保留 proxy_fingerprint
# ================================================================


def _analysis_with_proxy(proxy_fp: str | None) -> SceneCutAnalysis:
    metrics = SceneCutMetrics(
        luma_delta=0.5, hist_delta=0.5, edge_delta=0.4, color_delta=0.3,
        black_score=0.0, flash_score=0.0, motion_score=0.0, combined_score=0.7,
    )
    return SceneCutAnalysis(
        analysis_id="a1",
        asset_id="asset-1",
        source_path="/m.mp4",
        candidates=(
            SceneCutCandidate(
                source_frame=100, pts=100, seconds=100 / 24.0,
                score=0.7, confidence=0.9, kind=SceneCutKind.HARD_CUT,
                decision=SceneCutDecision.PENDING, metrics=metrics,
            ),
        ),
        proxy_fingerprint=proxy_fp,
    )


class TestAud18011:
    @pytest.mark.parametrize("proxy_fp", ["deadbeef12345678", None])
    def test_with_updated_candidate_preserves_proxy(self, proxy_fp: str | None) -> None:
        analysis = _analysis_with_proxy(proxy_fp)
        derived = analysis.with_updated_candidate(100, SceneCutDecision.REJECTED)
        assert derived.proxy_fingerprint == proxy_fp
        assert derived.to_cache_key() == analysis.to_cache_key()

    @pytest.mark.parametrize("proxy_fp", ["deadbeef12345678", None])
    def test_with_all_decisions_preserves_proxy(self, proxy_fp: str | None) -> None:
        analysis = _analysis_with_proxy(proxy_fp)
        derived = analysis.with_all_decisions({100: SceneCutDecision.ACCEPTED})
        assert derived.proxy_fingerprint == proxy_fp
        assert derived.to_cache_key() == analysis.to_cache_key()


# ================================================================
# AUD-18012 — timecode_to_frames 的 DF / 小数帧率
# ================================================================


class TestAud18012:
    def test_df_2997_first_displayable_at_minute_one(self) -> None:
        # DF 下 00:01:00:00 / 00:01:00:01 不可显示（被丢弃），
        # 00:01:00:02 = 1800 帧（1 分钟内丢了 2 帧；旧实现 = 60*29 = 1740）
        assert timecode_to_frames("00:01:00:02", 30000.0 / 1001.0) == 1800

    def test_df_2997_one_hour(self) -> None:
        # 1 小时 DF = 107892 帧（旧实现 = 3600*29 = 104400，偏 3492 帧）
        assert timecode_to_frames("01:00:00:00", 30000.0 / 1001.0) == 107892

    def test_df_roundtrip_with_frames_to_timecode(self) -> None:
        fps = 30000.0 / 1001.0
        for frames in (1798, 1799, 1800, 107891):
            tc = _frames_to_timecode(frames, fps)
            assert ";" in tc  # DF 分隔符
            assert timecode_to_frames(tc, fps) == frames

    def test_df_non_displayable_code_raises(self) -> None:
        with pytest.raises(ValueError):
            timecode_to_frames("00:01:00:00", 30000.0 / 1001.0)

    def test_fractional_ndf_rounds_not_truncates(self) -> None:
        # 29.97 NDF：名义 30fps（旧实现 int() 截断为 29）
        assert timecode_to_frames("00:00:01:00", 29.97) == 30

    def test_out_of_range_fields_raise(self) -> None:
        with pytest.raises(ValueError):
            timecode_to_frames("00:00:01:31", 30.0)  # ff >= fps
        with pytest.raises(ValueError):
            timecode_to_frames("00:61:00:00", 24.0)  # mm >= 60


# ================================================================
# AUD-18013 — 首场景起始秒按真实时间
# ================================================================


def _range_analysis(start: int, end: int, cut_frame: int | None) -> SceneCutAnalysis:
    candidates: tuple[SceneCutCandidate, ...] = ()
    if cut_frame is not None:
        candidates = (
            SceneCutCandidate(
                source_frame=cut_frame, pts=cut_frame, seconds=cut_frame / 24.0,
                score=0.8, confidence=0.9, kind=SceneCutKind.HARD_CUT,
                decision=SceneCutDecision.ACCEPTED,
            ),
        )
    return SceneCutAnalysis(
        analysis_id="a1",
        asset_id="asset-1",
        source_path="/m.mp4",
        settings=SceneCutSettings(start_source_frame=start, end_source_frame=end),
        candidates=candidates,
    )


class TestAud18013:
    def test_first_scene_start_seconds_uses_range_start(self) -> None:
        analysis = _range_analysis(100, 300, 150)
        specs = generate_scene_subclips(
            analysis, total_source_frames=200, total_duration_seconds=200 / 24.0,
            parent_display_name="V", fps=24.0,
        )
        assert specs[0].source_start == 100
        assert specs[0].start_seconds == pytest.approx(100 / 24.0)
        # 首场景时长不再吞并 0..起点 的时长
        assert specs[0].duration_seconds == pytest.approx(50 / 24.0)
        # 尾场景时长按绝对端点折算（不再出现 负数/错位）
        assert specs[1].duration_seconds == pytest.approx(150 / 24.0)

    def test_no_cuts_branch_uses_range_start(self) -> None:
        analysis = _range_analysis(100, 300, None)
        specs = generate_scene_subclips(
            analysis, total_source_frames=200, total_duration_seconds=200 / 24.0,
            parent_display_name="V", fps=24.0,
        )
        assert len(specs) == 1
        assert specs[0].start_seconds == pytest.approx(100 / 24.0)

    def test_time_index_used_when_available(self) -> None:
        # VFR 索引：帧 100 → 5.0s（非 fps 折算的 4.17s）
        index = SourceFrameTimeIndex(
            asset_id="asset-1",
            media_fingerprint="",
            points=(
                FrameTimePoint(source_frame=0, pts=0, seconds=0.0),
                FrameTimePoint(source_frame=100, pts=120000, seconds=5.0),
                FrameTimePoint(source_frame=150, pts=180000, seconds=7.5),
                FrameTimePoint(source_frame=300, pts=360000, seconds=15.0),
            ),
            average_fps=20.0,
        )
        analysis = SceneCutAnalysis(
            analysis_id="a1",
            asset_id="asset-1",
            source_path="/m.mp4",
            settings=SceneCutSettings(start_source_frame=100, end_source_frame=300),
            source_time_index=index,
            candidates=(
                SceneCutCandidate(
                    source_frame=150, pts=180000, seconds=7.5,
                    score=0.8, confidence=0.9, kind=SceneCutKind.HARD_CUT,
                    decision=SceneCutDecision.ACCEPTED,
                ),
            ),
        )
        specs = generate_scene_subclips(
            analysis, total_source_frames=200, total_duration_seconds=10.0,
            parent_display_name="V", fps=24.0,
        )
        assert specs[0].start_seconds == pytest.approx(5.0)
        assert specs[0].duration_seconds == pytest.approx(2.5)
        assert specs[1].start_seconds == pytest.approx(7.5)
        assert specs[1].duration_seconds == pytest.approx(7.5)

    def test_full_media_analysis_unchanged(self) -> None:
        # start=0 的全片分析：行为与修复前一致（0.0 起点）
        analysis = _range_analysis(0, 240, 100)
        specs = generate_scene_subclips(
            analysis, total_source_frames=240, total_duration_seconds=10.0,
            parent_display_name="V", fps=24.0,
        )
        assert specs[0].start_seconds == pytest.approx(0.0)
        assert specs[0].duration_seconds == pytest.approx(100 / 24.0)
        assert specs[1].duration_seconds == pytest.approx(140 / 24.0)


# ================================================================
# AUD-18100 — add_subclips_to_bin 保留 revision
# ================================================================


class TestAud18100:
    def test_revision_preserved(self) -> None:
        project = EditProject(
            project_id="p1", name="proj", revision=7,
        )
        # 确保目标 bin 存在
        root = ProjectBin(bin_id=project.root_bin_id or "root", name="Root", parent_id=None)
        project = dataclasses.replace(
            project, bins={root.bin_id: root}, root_bin_id=root.bin_id
        )
        asset = MediaAsset(
            asset_id="a-1",
            path="/m.mp4",
            media_type="video",
            display_name="sub.mp4",
            metadata={},
        )
        result = add_subclips_to_bin(project, (asset,), root.bin_id)
        assert result.revision == 7  # 旧实现静默重置为 0
        assert "a-1" in result.assets
        assert "a-1" in result.bins[root.bin_id].asset_ids

    def test_other_fields_preserved(self) -> None:
        project = EditProject(
            project_id="p1", name="proj", revision=3,
            active_timeline_id="tl-9", created_at=111.0, modified_at=222.0,
        )
        root = ProjectBin(bin_id=project.root_bin_id or "root", name="Root", parent_id=None)
        project = dataclasses.replace(
            project, bins={root.bin_id: root}, root_bin_id=root.bin_id
        )
        asset = MediaAsset(
            asset_id="a-2",
            path="/m2.mp4",
            media_type="video",
            display_name="sub2.mp4",
            metadata={},
        )
        result = add_subclips_to_bin(project, (asset,), root.bin_id)
        assert result.active_timeline_id == "tl-9"
        assert result.created_at == 111.0
        assert result.modified_at == 222.0


# ================================================================
# AUD-18110 — CFR 反向映射的 fps 换算
# ================================================================


def _reverse_mapper() -> ClipSourceMapper:
    config = ClipMappingConfig(
        clip_id="c1",
        asset_id="a1",
        timeline_fps=Fraction(24, 1),
        source_fps=Fraction(24000, 1001),
        timeline_start=0,
        timeline_duration=2400,
        source_start=0,
        source_duration=2398,
        reverse=True,
    )
    return ClipSourceMapper(config)


class TestAud18110:
    def test_reverse_mapping_applies_fps_ratio(self) -> None:
        mapper = _reverse_mapper()
        result = mapper.source_to_timeline(0)
        # 源帧 0（反向最末）→ 相对 2397 源帧 ≈ 2397 × 24/23.976 ≈ 2399.4
        # 旧实现丢失 fps 比率 → 2397（差 >2 帧，突破 ≤1 帧承诺）
        assert result.timeline_frame == 2399
        assert result.error_frames < 0.5

    def test_reverse_roundtrip_within_one_frame(self) -> None:
        mapper = _reverse_mapper()
        for source_frame in (0, 1, 597, 1198, 2397):
            forward = mapper.source_to_timeline(source_frame)
            back = mapper.timeline_to_source(forward.timeline_frame)
            assert abs(back.source_frame - source_frame) <= 1, (
                f"roundtrip error {abs(back.source_frame - source_frame)} "
                f"frames at source {source_frame}"
            )

    def test_reverse_maps_end_to_timeline_zero(self) -> None:
        mapper = _reverse_mapper()
        result = mapper.source_to_timeline(2397)
        assert result.timeline_frame == 0
