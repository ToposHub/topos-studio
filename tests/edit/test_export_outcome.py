"""P0-05 / P1-01 回归：导出结果三态语义、取消即时性与 worker 信号映射。

- 注入编码器创建失败 → ExportOutcome.failed（cancelled=False），worker 报
  结构化错误而非 EXPORT_CANCELLED（此前一切 False 都被吞成取消）；
- 用户取消 → ExportOutcome.user_cancelled，worker 报 EXPORT_CANCELLED；
- P1-01：取消后立即停止——不再渲染/写入音频、无产物残留；
- worker 三态信号映射（finished / EXPORT_CANCELLED / 结构化错误）。

C 库未构建时用例 skip（取消/失败路径用 Topos 编码器 abort 语义验证）。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Dict, List

import numpy as np
import pytest

pytestmark = pytest.mark.media_core

from src.shared.codec import topos_binding
from src.shared.media.frame import DecodedFrame

W, H, N = 64, 40, 4


def _lib_available() -> bool:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


require_lib = pytest.mark.skipif(
    not _lib_available(), reason="libtopos_codec 未构建")


def _bgra_frame(i: int) -> DecodedFrame:
    bgra = np.zeros((H, W, 4), np.uint8)
    bgra[..., 0] = 40
    bgra[..., 1] = 80
    bgra[..., 2] = 160
    bgra[..., 3] = 255
    return DecodedFrame(
        frame_index=i, pts=i, time_seconds=i / 24.0,
        data=bgra, pixel_format='bgra', width=W, height=H,
    )


def _bgra_array(i: int) -> np.ndarray:
    """串行路径 _render_frame 的返回物（numpy BGRA，_encode_frame 直接消费）。"""
    return _bgra_frame(i).data


class _FakeFrameProvider:
    def __init__(self, frames: List[DecodedFrame]) -> None:
        self._frames = frames

    def open_clip(self, path: str, share_by_path: bool = False, **_kw) -> str:
        return str(path)

    def request_frame(self, clip_id: str, frame: int, quality: str = "master"):
        return self._frames[int(frame) % len(self._frames)]

    def close_clip(self, clip_id: str, **_kw) -> None:
        pass


def _make_exporter(tmp_path, monkeypatch) -> "object":
    """最小真实导出链：EditProject + 单视频轨 4 帧时间线 + fake provider。"""
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_export import TimelineExporter
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.shared.media.metadata import Rational

    src = str(tmp_path / "source_placeholder.mov")
    if not os.path.exists(src):
        with open(src, "wb") as fh:
            fh.write(b"placeholder-source")

    project = EditProject.create("P0-05")
    project = project.import_asset(
        path=src, media_type="video",
        metadata={"width": W, "height": H, "fps": 24.0,
                  "total_frames": N, "duration_seconds": N / 24.0},
    )
    asset_id = list(project.assets.keys())[-1]
    clip = ClipInstance(
        clip_id="c0", asset_id=asset_id, track_id="t0", timeline_start=0,
        timeline_duration=N, source_start=0, source_duration=N,
    )
    track = EditTrack(track_id="t0", name="V1", track_type="video",
                      index=0, clips=(clip,))
    sequence = TimelineSequence(
        name="P0-05", width=W, height=H, fps=Rational(24, 1),
        video_tracks=(track,),
    )
    exporter = TimelineExporter(
        project=project, sequence=sequence,
        frame_provider=_FakeFrameProvider([_bgra_frame(i) for i in range(N)]),
    )
    return exporter


def _export_config(tmp_path, name: str, include_audio: bool = False):
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
    )

    return TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="auto",
        range_type=ExportRangeType.ENTIRE,
        codec=ExportCodec.TOPOS_HQ,
        output_path=str(tmp_path / name),
        has_alpha=False,
        include_audio=include_audio,
        burn_subtitles=False,
        quality=6,
        quality_mode="crf",
        resolution=(W, H),
    )


@require_lib
def test_encode_failure_is_failed_not_cancelled(tmp_path, monkeypatch) -> None:
    """P0-05：注入编码器创建失败 → failed（含错误码/cause），非 cancelled。"""
    from src.features.edit.core.timeline_export import TimelineExporter

    exporter = _make_exporter(tmp_path, monkeypatch)
    out_path = str(tmp_path / "fail.mov")

    def _boom(output_path, encoder_config):
        raise RuntimeError("simulated encoder open failure")

    monkeypatch.setattr(exporter, "_create_encoder", _boom)
    outcome = exporter.export_timeline(_export_config(tmp_path, "fail.mov"))

    assert outcome.success is False
    assert outcome.cancelled is False, "失败不得被归型为取消"
    assert outcome.error_code == "EXPORT_FAILED"
    assert "RuntimeError" in outcome.cause and "simulated" in outcome.cause
    assert not os.path.exists(out_path), "失败不得留下伪成功产物"
    assert exporter._is_exporting is False


@require_lib
def test_user_cancel_reports_cancelled(tmp_path, monkeypatch) -> None:
    """P0-05：用户取消 → cancelled=True；无产物（abort 语义）。"""
    exporter = _make_exporter(tmp_path, monkeypatch)
    out_path = str(tmp_path / "cancel.mov")

    # 强制串行路径，_render_frame 第二帧置取消
    monkeypatch.setattr(exporter, "_pipeline_eligible",
                        lambda start, end, config: False)
    calls = {"n": 0}

    def _render(frame_idx, config):
        calls["n"] += 1
        if calls["n"] >= 2:
            exporter._cancel_requested = True
        return _bgra_array(frame_idx)

    monkeypatch.setattr(exporter, "_render_frame", _render)
    outcome = exporter.export_timeline(_export_config(tmp_path, "cancel.mov"))

    assert outcome.success is False
    assert outcome.cancelled is True
    assert calls["n"] == 2, "取消后不得继续渲染后续帧"
    assert not os.path.exists(out_path), "取消不得留下可被误识为成功的产物"


@require_lib
def test_cancel_skips_audio_render(tmp_path, monkeypatch) -> None:
    """P1-01/P0-EXPORT-AUDIO：取消后立即停止——音频渲染线程提前收尾、
    不写入编码器（写入只发生在未取消路径）。"""
    import src.features.edit.core.timeline_export as tl_mod

    exporter = _make_exporter(tmp_path, monkeypatch)

    # 伪造展开后的音频轨（存在音频工作项）
    fake_audio_seq = SimpleNamespace(
        audio_tracks=(SimpleNamespace(clips=(SimpleNamespace(),)),))
    monkeypatch.setattr(tl_mod, "expand_sequence_audio_for_render",
                        lambda *a, **k: fake_audio_seq)

    # 捕获创建的编码器并记录 write_audio_samples 调用
    created = {}
    orig_create = exporter._create_encoder

    def _capture_create(output_path, encoder_config):
        enc = orig_create(output_path, encoder_config)
        created["enc"] = enc
        writes = {"n": 0}

        orig_write = enc.write_audio_samples

        def _record_write(samples, rate):
            writes["n"] += 1
            return orig_write(samples, rate)

        enc.write_audio_samples = _record_write
        enc._test_audio_writes = writes
        return enc

    monkeypatch.setattr(exporter, "_create_encoder", _capture_create)

    audio_renders = {"n": 0}

    def _count_render(start_frame, end_frame, config, encoder=None):
        audio_renders["n"] += 1
        return None

    monkeypatch.setattr(exporter, "_render_audio_master", _count_render)
    monkeypatch.setattr(exporter, "_pipeline_eligible",
                        lambda start, end, config: False)
    calls = {"n": 0}

    def _render(frame_idx, config):
        calls["n"] += 1
        if calls["n"] >= 2:
            exporter._cancel_requested = True
        return _bgra_array(frame_idx)

    monkeypatch.setattr(exporter, "_render_frame", _render)
    outcome = exporter.export_timeline(
        _export_config(tmp_path, "cancel_audio.mov", include_audio=True))

    assert outcome.cancelled is True
    assert audio_renders["n"] <= 1, "并行渲染最多启动一次且循环内随取消退出"
    enc = created.get("enc")
    if enc is not None:
        assert enc._test_audio_writes["n"] == 0, "取消后不得写入音频"


@require_lib
def test_topos_audio_supported_runs_audio_render(tmp_path, monkeypatch) -> None:
    """v1.1 音频轨（plan M-A6）：Topos 容器已支持音频（supports_audio=True）
    → 请求音频时音频渲染照常执行、audio_omitted 不再置位。

    P1-02 的跳过门收敛到真不支持音频的容器（GIF / image sequence）。
    渲染以计数桩代替（不依赖真实音频素材），返回 None 时写入自然跳过。"""
    import src.features.edit.core.timeline_export as tl_mod

    exporter = _make_exporter(tmp_path, monkeypatch)
    fake_audio_seq = SimpleNamespace(
        audio_tracks=(SimpleNamespace(clips=(SimpleNamespace(),)),))
    monkeypatch.setattr(tl_mod, "expand_sequence_audio_for_render",
                        lambda *a, **k: fake_audio_seq)
    monkeypatch.setattr(exporter, "_pipeline_eligible",
                        lambda start, end, config: False)
    monkeypatch.setattr(exporter, "_render_frame",
                        lambda frame_idx, config: _bgra_array(frame_idx))

    audio_renders = {"n": 0}

    def _count_render(start_frame, end_frame, config, encoder=None):
        audio_renders["n"] += 1
        # 有效 PCM（声明音频后必须产出数据，否则编码器 close 按
        # 「声明音频但零样本」fail-fast——与其它 codec 的音频错误语义一致）
        import numpy as _np

        return _np.zeros((2, 4800), dtype=_np.float32)

    monkeypatch.setattr(exporter, "_render_audio_master", _count_render)
    outcome = exporter.export_timeline(
        _export_config(tmp_path, "audio_written.mov", include_audio=True))

    assert outcome.success is True
    assert outcome.cancelled is False
    assert audio_renders["n"] == 1, "v1.1：topos 请求音频必须执行音频渲染"
    assert outcome.audio_omitted is False, "容器已支持音频，不得标注省略"


def _run_worker_once(qapp, monkeypatch, outcome) -> "SimpleNamespace":
    """以 fake exporter 驱动 VideoExportWorker.run，捕获三态信号。"""
    from src.shared.export.video_export_worker import VideoExportWorker
    import src.features.edit.core.timeline_export as tl_mod

    worker = VideoExportWorker()
    captured = SimpleNamespace(finished=[], error=[])
    worker.finished.connect(lambda p: captured.finished.append(p))
    worker.error.connect(lambda m: captured.error.append(m))

    fake_exporter = SimpleNamespace(
        export_timeline=lambda config: outcome,
        cancel_export=lambda: None,
    )
    monkeypatch.setattr(tl_mod, "create_timeline_exporter",
                        lambda **kw: fake_exporter)
    config = SimpleNamespace(output_path="/tmp/unused.mov")
    worker.run(None, None, config)
    return captured


def test_worker_maps_failure_not_cancelled(qapp, monkeypatch) -> None:
    from src.features.edit.core.timeline_export import ExportOutcome

    captured = _run_worker_once(
        qapp, monkeypatch,
        ExportOutcome.failed("EXPORT_FAILED", "RuntimeError: boom"))
    assert captured.finished == []
    assert len(captured.error) == 1
    msg = captured.error[0]
    assert "EXPORT_CANCELLED" not in msg, "失败不得映射为取消"
    assert "EXPORT_FAILED" in msg and "RuntimeError: boom" in msg


def test_worker_maps_cancel(qapp, monkeypatch) -> None:
    from src.features.edit.core.timeline_export import ExportOutcome

    captured = _run_worker_once(
        qapp, monkeypatch, ExportOutcome.user_cancelled())
    assert captured.finished == []
    assert captured.error == ["EXPORT_CANCELLED"]


def test_worker_maps_success(qapp, monkeypatch) -> None:
    from src.features.edit.core.timeline_export import ExportOutcome

    captured = _run_worker_once(qapp, monkeypatch, ExportOutcome.ok())
    assert captured.finished == ["/tmp/unused.mov"]
    assert captured.error == []


# ---------------- F04（2026-09-27）：降噪预检查按 clip 区间判定 ----------------

@require_lib
def test_pipeline_eligible_checks_clips_not_per_frame(
        tmp_path, monkeypatch) -> None:
    """F04 反例：历史对 [start,end) 逐帧 get_frame_plan（90k 帧 = 90k 份
    计划构造，4096 缓存全淘汰）。修复后按重叠 clip 判定——
    get_frame_plan 调用次数必须为 0，且语义（无降噪 → True）保留。"""
    exporter = _make_exporter(tmp_path, monkeypatch)
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    exporter._render_plan = TimelineRenderPlan(exporter._sequence)
    assert exporter._render_plan is not None
    plan_calls = {"n": 0}
    real_get = exporter._render_plan.get_frame_plan

    def counting_get(frame):
        plan_calls["n"] += 1
        return real_get(frame)

    monkeypatch.setattr(exporter._render_plan,
                        "get_frame_plan", counting_get)

    assert exporter._pipeline_eligible(0, 4, _export_config(
        tmp_path, "e.mov")) is True
    assert plan_calls["n"] == 0, "预检查不得逐帧构造 FrameRenderPlan"


@require_lib
def test_pipeline_eligible_false_when_clip_has_denoise(
        tmp_path, monkeypatch) -> None:
    """语义保留：重叠 clip 启用降噪 → 回退串行（False）；区间外 clip
    启用降噪不影响。"""
    from types import SimpleNamespace

    exporter = _make_exporter(tmp_path, monkeypatch)
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    exporter._render_plan = TimelineRenderPlan(exporter._sequence)
    # 给时间线上的唯一 clip 挂降噪（color_stack 引用）
    stack_id = "stack-denoise"
    clip = exporter._sequence.video_tracks[0].clips[0]
    object.__setattr__(clip, "color_stack_id", stack_id)
    object.__setattr__(
        exporter._project, "color_stacks",
        {stack_id: SimpleNamespace(video_denoise={"radius": 2})})

    assert exporter._pipeline_eligible(0, 4, _export_config(
        tmp_path, "e2.mov")) is False

    # clip 移出导出区间 → 不再拦截
    object.__setattr__(clip, "timeline_start", 100)
    object.__setattr__(clip, "timeline_duration", 10)
    assert exporter._pipeline_eligible(0, 4, _export_config(
        tmp_path, "e3.mov")) is True


# ---------------- N08（2026-09-27 复验）：不可渲染轨道的降噪不阻断流水线 ----------------

@require_lib
def test_pipeline_eligible_ignores_denoise_on_unrenderable_tracks(
        tmp_path, monkeypatch) -> None:
    """N08：禁用/隐藏/静音轨道上的降噪 clip 不会进渲染，不应把可流水
    线的导出打回串行；可见启用轨道的降噪仍阻断。"""
    from types import SimpleNamespace

    exporter = _make_exporter(tmp_path, monkeypatch)
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    exporter._render_plan = TimelineRenderPlan(exporter._sequence)
    stack_id = "stack-denoise-n08"
    clip = exporter._sequence.video_tracks[0].clips[0]
    object.__setattr__(clip, "color_stack_id", stack_id)
    object.__setattr__(
        exporter._project, "color_stacks",
        {stack_id: SimpleNamespace(video_denoise={"radius": 2})})
    track = exporter._sequence.video_tracks[0]
    cfg = _export_config(tmp_path, "n08.mov")

    # 对照：可见/启用/非静音轨道的降噪 → 阻断
    assert exporter._pipeline_eligible(0, 4, cfg) is False

    # 逐一注入不可渲染态（is_enabled=False / is_visible=False / is_muted=True）
    for field, value in (("is_enabled", False), ("is_visible", False),
                         ("is_muted", True)):
        prev = getattr(track, field)
        object.__setattr__(track, field, value)
        assert exporter._pipeline_eligible(0, 4, cfg) is True, (
            f"{field}={value} 的降噪轨不渲染，不应阻断流水线")
        object.__setattr__(track, field, prev)

    # 恢复对照态复验
    assert exporter._pipeline_eligible(0, 4, cfg) is False


@require_lib
def test_pipeline_eligible_ignores_denoise_when_solo_elsewhere(
        tmp_path, monkeypatch) -> None:
    """N08：他轨 solo 时，非 solo 轨的降噪 clip 不渲染 → 不阻断。"""
    from types import SimpleNamespace

    exporter = _make_exporter(tmp_path, monkeypatch)
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack

    stack_id = "stack-denoise-solo"
    clip = exporter._sequence.video_tracks[0].clips[0]
    object.__setattr__(clip, "color_stack_id", stack_id)
    object.__setattr__(
        exporter._project, "color_stacks",
        {stack_id: SimpleNamespace(video_denoise={"radius": 2})})

    # 追加一条 solo 轨（含一个普通 clip），原降噪轨被 solo 压制
    asset_id = clip.asset_id
    solo_clip = ClipInstance(
        clip_id="c-solo", asset_id=asset_id, track_id="t-solo",
        timeline_start=0, timeline_duration=N, source_start=0,
        source_duration=N)
    solo_track = EditTrack(
        track_id="t-solo", name="VS", track_type="video", index=1,
        clips=(solo_clip,), is_solo=True)
    object.__setattr__(
        exporter._sequence, "video_tracks",
        (exporter._sequence.video_tracks[0], solo_track))
    exporter._render_plan = TimelineRenderPlan(exporter._sequence)

    cfg = _export_config(tmp_path, "n08solo.mov")
    assert exporter._pipeline_eligible(0, 4, cfg) is True, \
        "solo 压制下的降噪轨不应阻断流水线"


@require_lib
def test_pipeline_eligible_ignores_disabled_clip_denoise(
        tmp_path, monkeypatch) -> None:
    """N08：片段级禁用（is_enabled=False）的降噪 clip 不渲染 → 不阻断。"""
    from types import SimpleNamespace

    exporter = _make_exporter(tmp_path, monkeypatch)
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    exporter._render_plan = TimelineRenderPlan(exporter._sequence)
    stack_id = "stack-denoise-clipoff"
    clip = exporter._sequence.video_tracks[0].clips[0]
    object.__setattr__(clip, "color_stack_id", stack_id)
    object.__setattr__(
        exporter._project, "color_stacks",
        {stack_id: SimpleNamespace(video_denoise={"radius": 2})})

    cfg = _export_config(tmp_path, "n08clip.mov")
    assert exporter._pipeline_eligible(0, 4, cfg) is False  # 对照
    object.__setattr__(clip, "is_enabled", False)
    assert exporter._pipeline_eligible(0, 4, cfg) is True, \
        "禁用片段的降噪不应阻断流水线"


@require_lib
def test_pipeline_eligible_matches_render_plan_layer_filtering(
        tmp_path, monkeypatch) -> None:
    """N08 等价性：_pipeline_eligible 的判定与逐帧渲染计划的轨道过滤
    一致——降噪轨在渲染计划中出现 ⟺ 预检查返回 False。"""
    from types import SimpleNamespace

    exporter = _make_exporter(tmp_path, monkeypatch)
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    exporter._render_plan = TimelineRenderPlan(exporter._sequence)
    stack_id = "stack-denoise-equiv"
    clip = exporter._sequence.video_tracks[0].clips[0]
    object.__setattr__(clip, "color_stack_id", stack_id)
    object.__setattr__(
        exporter._project, "color_stacks",
        {stack_id: SimpleNamespace(video_denoise={"radius": 2})})
    cfg = _export_config(tmp_path, "n08eq.mov")

    for track_state, expect_layers in (
        ({"is_enabled": True, "is_visible": True, "is_muted": False}, True),
        ({"is_enabled": False, "is_visible": True, "is_muted": False}, False),
        ({"is_enabled": True, "is_visible": False, "is_muted": False}, False),
        ({"is_enabled": True, "is_visible": True, "is_muted": True}, False),
    ):
        track = exporter._sequence.video_tracks[0]
        for k, v in track_state.items():
            object.__setattr__(track, k, v)
        exporter._render_plan = TimelineRenderPlan(exporter._sequence)
        plan = exporter._render_plan.get_frame_plan(0)
        has_layer = any(
            getattr(l, "clip", None) is clip
            for l in getattr(plan, "layers", ()))
        eligible = exporter._pipeline_eligible(0, 4, cfg)
        # 降噪轨进渲染计划 ⟺ 存在降噪 clip 且被渲染 ⟺ 不允许流水线
        assert has_layer == (not eligible), (
            f"轨道态 {track_state}：渲染计划含降噪 clip={has_layer} 与 "
            f"预检查 eligible={eligible} 不一致")
