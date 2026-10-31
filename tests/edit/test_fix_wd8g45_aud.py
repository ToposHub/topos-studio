# 状态(2026-09-29 wd8-g45): 修复批次 D（AUD-12602..12607/12617..12619/12701..
# 12703/12707/12708/12800/12901）的回归测试。
"""Remediation batch wd8-g45 — regression tests.

覆盖 REMEDIATION_PLAN.md 批 D 的 16 条 Major 条目：

- AUD-12602 FCPXML import 二次解析/整数属性无防护
- AUD-12603 FCPXML marker/transition/字幕导入缺失 + 多 sequence 静默丢弃
- AUD-12604 悬空 ref 的 asset-clip 伪装 PRESERVED
- AUD-12605 frameDuration 解析错误被静默吞掉
- AUD-12606 不可信 XML 无实体膨胀防护
- AUD-12607 导出 transition/marker 不符合 FCPXML schema
- AUD-12617 OTIO 导入 timeline_start/rate 错误
- AUD-12618 OTIO 导入音频轨静默丢失
- AUD-12619 OTIO 导出 target_url 用内部 uuid 伪造
- AUD-12701 inbox 已 claim 条目挤占 per-run 容量
- AUD-12702 指标收集器无锁
- AUD-12703 PARTIAL_SUCCESS 永远统计为 succeeded
- AUD-12707 PAUSED_FOR_RESUME 无法 fail()
- AUD-12708 orchestrator 四张映射无界增长
- AUD-12800 取消/审批竞态异常逃逸 execute()
- AUD-12901 scheduler shutdown 丢弃在途 work / submit 抛 RuntimeError
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.adapters import fcpxml_adapter as fcpxml_mod
from src.features.edit.adapters.fcpxml_adapter import FCPXMLAdapter
from src.features.edit.adapters.interchange_types import InterchangeEntryCategory
from src.features.edit.adapters.otio_adapter import (
    _rate_to_rational,
    _rescale_to_frames,
    _timeline_rate,
)
from src.features.edit.ai_editing.agent_inbox import AgentInbox, InboxItemKind
from src.features.edit.ai_editing.agent_metrics import AgentMetricsCollector
from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
    AgentTask,
)
from src.features.edit.ai_editing.agent_orchestrator import (
    _MAX_RETAINED_TERMINAL_RUNS,
    AgentOrchestrator,
    RunNotTransitionableError,
)
from src.features.edit.ai_editing.agent_task_executor import AgentTaskExecutor
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineMarker,
    TimelineSequence,
    TransitionInstance,
)
from src.shared.media.metadata import Rational


def _cat(report: Any, category: InterchangeEntryCategory, element: str = "") -> list[Any]:
    return [
        e for e in (report.entries if report else ())
        if e.category == category and (not element or e.element == element)
    ]


def _write(tmp_path: Any, name: str, content: str) -> Any:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ================================================================
# AUD-12602 / 12603 / 12604 / 12605 / 12606 — FCPXML import
# ================================================================


def _fcpxml(spine_body: str, *, extra_resources: str = "", format_attrs: str = "",
            second_sequence: str = "") -> str:
    if not format_attrs:
        format_attrs = 'frameDuration="1/24s" width="1920" height="1080"'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="1920x1080" {format_attrs}/>
    <asset id="r2" name="clip_a" hasVideo="1" start="0s" duration="7200s">
      <media-rep kind="original" src="file:///home/clip_a.mp4"/>
    </asset>
    <asset id="r3" name="clip_b" hasVideo="1" start="0s" duration="3600s"/>
    {extra_resources}
  </resources>
  <library>
    <event name="E">
      <project name="P">
        <sequence id="seq1" name="Main">
          <spine>
            {spine_body}
          </spine>
        </sequence>
        {second_sequence}
      </project>
    </event>
  </library>
</fcpxml>
"""


def test_aud_12602_invalid_width_falls_back_with_report(tmp_path):
    """外部文件 width="1920.0" → 不抛异常，回退默认值并记录诊断/条目。"""
    p = _write(tmp_path, "dirty.fcpxml", _fcpxml(
        '<asset-clip name="c0" ref="r2" offset="0s" duration="48s" tcStart="0s"/>',
        format_attrs='width="1920.0" height="1080"',
    ))
    result = FCPXMLAdapter().import_to_canonical(p)
    assert len(result.sequences) == 1
    assert result.sequences[0].width == 1920
    assert any(d.get("severity") == "warning" and "width" in d.get("message", "")
               for d in result.report.diagnostics)
    assert _cat(result.report, InterchangeEntryCategory.CONVERTED, "format")


def test_aud_12602_second_parse_failure_returns_error_report(tmp_path, monkeypatch):
    """inspect 与 import 之间文件被替换（TOCTOU）→ 降级为错误报告，不抛异常。"""
    p = _write(tmp_path, "t.fcpxml", _fcpxml(
        '<asset-clip name="c0" ref="r2" offset="0s" duration="48s" tcStart="0s"/>',
    ))
    real = fcpxml_mod._parse_fcpxml
    calls = {"n": 0}

    def flaky(source: Any) -> ET.Element:
        calls["n"] += 1
        if calls["n"] >= 2:  # inspect 成功后、import 二次解析时文件已损坏
            raise ET.ParseError("file changed mid-import")
        return real(source)

    monkeypatch.setattr(fcpxml_mod, "_parse_fcpxml", flaky)
    result = FCPXMLAdapter().import_to_canonical(p)
    assert result.sequences == ()
    assert result.report is not None and result.report.has_errors
    assert any("parse failed during import" in d.get("message", "")
               for d in result.report.diagnostics)


def test_aud_12605_invalid_frame_duration_reports_fallback(tmp_path):
    """非法 frameDuration（0 分母）→ 回退 24fps 且有诊断 + CONVERTED 条目。"""
    p = _write(tmp_path, "fps.fcpxml", _fcpxml(
        '<asset-clip name="c0" ref="r2" offset="0s" duration="48s" tcStart="0s"/>',
        format_attrs='frameDuration="100/0s"',
    ))
    result = FCPXMLAdapter().import_to_canonical(p)
    assert result.sequences[0].fps == Rational(24, 1)
    assert any("frameDuration" in d.get("message", "")
               for d in result.report.diagnostics)
    assert _cat(result.report, InterchangeEntryCategory.CONVERTED, "format")


def test_aud_12606_rejects_dtd_and_entity_declarations(tmp_path):
    """含 DOCTYPE/ENTITY 的外部文件被拒绝（实体膨胀防护），不进入解析。"""
    p = _write(tmp_path, "evil.fcpxml", """<?xml version="1.0"?>
<!DOCTYPE fcpxml [<!ENTITY xxe "billion-laughs">]>
<fcpxml version="1.9"><resources/></fcpxml>""")
    adapter = FCPXMLAdapter()
    inspection = adapter.inspect(p)
    assert inspection.supported is False
    assert inspection.report is not None and inspection.report.has_errors
    result = adapter.import_to_canonical(p)
    assert result.sequences == ()


def test_aud_12606_rejects_oversized_file(tmp_path, monkeypatch):
    p = _write(tmp_path, "big.fcpxml", _fcpxml(
        '<asset-clip name="c0" ref="r2" offset="0s" duration="48s" tcStart="0s"/>',
    ))
    monkeypatch.setattr(fcpxml_mod, "_MAX_FCPXML_BYTES", 8)
    inspection = FCPXMLAdapter().inspect(p)
    assert inspection.supported is False
    assert inspection.report is not None and inspection.report.has_errors


def test_aud_12604_dangling_ref_requires_user_action(tmp_path):
    """悬空 ref：clip 保留 placement 供重连，但报告 REQUIRES_USER_ACTION + relink。"""
    p = _write(tmp_path, "dangling.fcpxml", _fcpxml(
        '<asset-clip name="c0" ref="r404" offset="0s" duration="48s" tcStart="0s"/>'
        '<asset-clip name="noRef" offset="48s" duration="24s" tcStart="0s"/>',
    ))
    result = FCPXMLAdapter().import_to_canonical(p)
    seq = result.sequences[0]
    clips = seq.video_tracks[0].clips
    # 无 ref 的 clip 被丢弃（DROPPED），悬空 ref 的 clip 保留
    assert [c.asset_id for c in clips] == ["r404"]
    assert _cat(result.report, InterchangeEntryCategory.REQUIRES_USER_ACTION, "asset-clip")
    assert _cat(result.report, InterchangeEntryCategory.DROPPED, "asset-clip")
    assert any(r.external_ref == "r404" for r in result.relink_requests)
    # 伪装成 PRESERVED 的条目只应属于真实存在的 asset（悬空 ref 不再计入）
    preserved = _cat(result.report, InterchangeEntryCategory.PRESERVED, "asset-clip")
    assert all("r404" not in e.description for e in preserved)


def test_aud_12603_unsupported_elements_and_extra_sequences_reported(tmp_path):
    """未知 spine 子元素与多余 sequence 均有 DROPPED 条目（不静默丢弃）。"""
    p = _write(tmp_path, "extra.fcpxml", _fcpxml(
        '<asset-clip name="c0" ref="r2" offset="0s" duration="48s" tcStart="0s"/>'
        '<audition name="alt"/>'
        '<multicamClip name="mc"/>',
        second_sequence='<sequence id="seq2" name="Second"><spine>'
                        '<asset-clip name="x" ref="r2" offset="0s" duration="24s"/>'
                        "</spine></sequence>",
    ))
    result = FCPXMLAdapter().import_to_canonical(p)
    assert len(result.sequences) == 1
    report = result.report
    assert _cat(report, InterchangeEntryCategory.DROPPED, "audition")
    assert _cat(report, InterchangeEntryCategory.DROPPED, "multicamClip")
    seq_dropped = _cat(report, InterchangeEntryCategory.DROPPED, "sequence")
    assert seq_dropped and "Second" in seq_dropped[0].description


def test_aud_12603_imports_markers_title_and_cross_dissolve(tmp_path):
    """marker/基础字幕/可精确定位的 cross dissolve 被导入而非静默跳过。"""
    spine = (
        '<asset-clip name="c0" ref="r2" offset="0s" duration="1200s" tcStart="0s">'
        '<marker start="25s" duration="0s" value="beat"/>'
        "</asset-clip>"
        '<transition name="Cross Dissolve" offset="1200s" duration="24s" ref="r9"/>'
        '<asset-clip name="c1" ref="r3" offset="1200s" duration="600s" tcStart="0s"/>'
        '<title name="t1" offset="0s" duration="2s" lane="1">'
        "<text><text-style ref=\"ts1\">你好字幕</text-style></text></title>"
    )
    p = _write(tmp_path, "rich.fcpxml", _fcpxml(
        spine,
        extra_resources='<effect id="r9" name="Cross Dissolve" '
                        'uid=".../Effects/Builtins/Dissolve.bundle"/>',
    ))
    result = FCPXMLAdapter().import_to_canonical(p)
    seq = result.sequences[0]
    # marker：挂在 clip 上，帧 = clip 起点 + 相对 start（24fps）
    assert len(seq.markers) == 1
    marker = seq.markers[0]
    assert marker.name == "beat" and marker.frame == 600
    assert marker.clip_id == seq.video_tracks[0].clips[0].clip_id
    # cross dissolve：双邻 clip 可精确定位 → TransitionInstance
    transitions = seq.video_tracks[0].transitions
    assert len(transitions) == 1 and transitions[0].transition_type == "cross_dissolve"
    v_clips = seq.video_tracks[0].clips
    assert transitions[0].left_clip_id == v_clips[0].clip_id
    assert transitions[0].right_clip_id == v_clips[1].clip_id
    # 基础字幕：文本 + 时长（2s@24fps = 48 帧）
    assert seq.subtitle_tracks and len(seq.subtitle_tracks[0].subtitle_clips) == 1
    sub = seq.subtitle_tracks[0].subtitle_clips[0]
    assert sub.text == "你好字幕" and sub.timeline_duration == 48
    assert _cat(result.report, InterchangeEntryCategory.PRESERVED, "marker")
    assert _cat(result.report, InterchangeEntryCategory.CONVERTED, "title")


# ================================================================
# AUD-12607 — FCPXML export schema
# ================================================================


def _sequence_for_export() -> TimelineSequence:
    clip_a = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=120,
        source_start=0, source_duration=120,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    clip_b = ClipInstance(
        clip_id="c2", asset_id="a2", track_id="v1",
        timeline_start=120, timeline_duration=60,
        source_start=240, source_duration=60,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    trans = TransitionInstance(
        transition_id="tr1", transition_type="cross_dissolve", track_id="v1",
        left_clip_id="c1", right_clip_id="c2", center_frame=120, duration=12,
    )
    v1 = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=(clip_a, clip_b), transitions=(trans,),
    )
    return TimelineSequence(
        timeline_id="t1", name="Schema Test", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(v1,),
        markers=(
            TimelineMarker(marker_id="m1", frame=60, name="beat"),
            TimelineMarker(marker_id="m2", frame=9999, name="orphan"),
        ),
    )


def test_aud_12607_export_transition_and_marker_schema(tmp_path):
    """transition 带 offset/ref（effect 资源）；marker 挂在 asset-clip 上。"""
    out = tmp_path / "schema.fcpxml"
    adapter = FCPXMLAdapter()
    result = adapter.export_from_canonical(
        _sequence_for_export(), options={"output_path": str(out)},
    )
    root = ET.parse(str(out)).getroot()

    # transition：offset + duration + ref（引用 resources 中的 effect）
    transitions = root.findall(".//transition")
    assert len(transitions) == 1
    t_el = transitions[0]
    assert t_el.get("offset") and t_el.get("duration")
    effect_ref = t_el.get("ref")
    assert effect_ref
    effects = root.findall(".//resources/effect")
    assert any(e.get("id") == effect_ref for e in effects)
    # transition 不再统一追加在所有 clip 之后：位于两个 clip 之间
    spine_children = [c.tag for c in root.find(".//spine")]
    assert spine_children == ["asset-clip", "transition", "asset-clip"]

    # marker 归属到 asset-clip（帧 60 落在 c1 [0,120)），不再挂 spine
    assert root.findall(".//spine/marker") == []
    clip_markers = root.findall(".//asset-clip/marker")
    assert len(clip_markers) == 1
    assert clip_markers[0].get("value") == "beat"

    # 无法归属的 marker（帧 9999）显式 DROPPED
    dropped = _cat(result.report, InterchangeEntryCategory.DROPPED, "marker")
    assert dropped and "m2" in dropped[0].description
    assert _cat(result.report, InterchangeEntryCategory.PRESERVED, "transition")


# ================================================================
# AUD-12617 / 12618 / 12619 — OTIO adapter
# ================================================================


def test_aud_12617_rate_helpers():
    """帧率/帧值换算：NTSC 用精确有理数，跨率换算不丢失倍数。"""
    assert _rate_to_rational(29.97) == Rational(30000, 1001)
    assert _rate_to_rational(23.976) == Rational(24000, 1001)
    assert _rate_to_rational(25.0) == Rational(25, 1)
    assert _rate_to_rational(0) == Rational(24, 1)
    # 100 帧 @30fps → 序列 24fps 应为 125 帧（旧实现直接取 value 会得到 100）
    assert _rescale_to_frames(100, 30.0, 24.0) == 125
    assert _rescale_to_frames(100, 24.0, 24.0) == 100
    assert _rescale_to_frames(50, 0.0, 25.0) == 50  # 源率未知 → 视为序列率
    assert _rescale_to_frames(10, 25.0, 0.0) == 0  # 目标率非法 → 0


def test_aud_12617_timeline_rate_fallback():
    class _FakeRt:
        def __init__(self, value: float, rate: float):
            self.value, self.rate = value, rate

    class _FakeTimeline:
        global_start_time = _FakeRt(0, 25.0)

        def duration(self):
            return _FakeRt(100, 25.0)

    assert _timeline_rate(_FakeTimeline()) == 25.0

    class _NoRate:
        global_start_time = None

        def duration(self):
            raise TypeError("no range")

    assert _timeline_rate(_NoRate()) == 24.0


def _otio():
    pytest.importorskip("opentimelineio")
    import opentimelineio as otio
    return otio


def test_aud_12617_12618_otio_import_placement_and_audio_tracks(tmp_path):
    """多 clip 按轨道时序放置（不堆叠）+ 音频轨保留 + 非 24fps 正确换算。"""
    otio = _otio()
    from src.features.edit.adapters.otio_adapter import OTIOAdapter

    def _ref(url: str) -> Any:
        return otio.schema.ExternalReference(
            target_url=url,
            available_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 30),
                otio.opentime.RationalTime(1000, 30),
            ),
        )

    def _clip(name: str, url: str, start: int, dur: int) -> Any:
        c = otio.schema.Clip(name=name, media_reference=_ref(url))
        c.source_range = otio.opentime.TimeRange(
            otio.opentime.RationalTime(start, 30),
            otio.opentime.RationalTime(dur, 30),
        )
        return c

    timeline = otio.schema.Timeline("multi", global_start_time=otio.opentime.RationalTime(0, 30))
    v = otio.schema.Track(kind=otio.schema.TrackKind.Video)
    v.append(_clip("v1a", "file:///tmp/a.mp4", 0, 100))
    v.append(_clip("v1b", "file:///tmp/b.mp4", 10, 100))
    a = otio.schema.Track(kind=otio.schema.TrackKind.Audio)
    a.append(_clip("a1", "file:///tmp/a.wav", 0, 100))
    timeline.tracks.append(v)
    timeline.tracks.append(a)
    src = tmp_path / "multi.otio"
    otio.adapters.write_to_file(timeline, str(src))

    result = OTIOAdapter().import_to_canonical(src)
    assert len(result.sequences) == 1
    seq = result.sequences[0]
    # 序列帧率来自 timeline（30fps），非硬编码 24
    assert seq.fps == Rational(30, 1)
    video_clips = seq.video_tracks[0].clips
    # AUD-12617：第二个 clip 从帧 100 开始（旧实现取自身 duration → 与第一个堆叠）
    assert video_clips[0].timeline_start == 0
    assert video_clips[1].timeline_start == 100
    assert video_clips[1].source_start == 10
    # AUD-12618：音频轨不再丢失
    assert seq.audio_tracks and len(seq.audio_tracks[0].clips) == 1


def test_aud_12619_otio_export_target_url_resolvable(tmp_path):
    """asset_paths 提供路径 → 真实 file URL；缺失 → MissingReference + 用户行动条目。"""
    otio = _otio()
    from src.features.edit.adapters.otio_adapter import OTIOAdapter

    media_file = tmp_path / "media.mp4"
    media_file.write_bytes(b"\0" * 16)
    clip_a = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=48,
        source_start=0, source_duration=48,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    clip_b = ClipInstance(
        clip_id="c2", asset_id="missing", track_id="v1",
        timeline_start=48, timeline_duration=24,
        source_start=0, source_duration=24,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    seq = TimelineSequence(
        timeline_id="t", name="OTIO Export", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video",
                                index=0, clips=(clip_a, clip_b)),),
    )
    out = tmp_path / "out.otio"
    result = OTIOAdapter().export_from_canonical(
        seq, options={"output_path": str(out), "asset_paths": {"a1": str(media_file)}},
    )
    timeline = otio.adapters.read_from_file(str(out))
    clips = list(timeline.each_clip())
    urls = [c.media_reference.target_url for c in clips]
    # 已知路径 → 真实可解析的 file URL（绝不再是内部 uuid 伪造）
    assert urls[0].startswith("file://") and urls[0].endswith("media.mp4")
    from pathlib import Path as _Path
    assert _Path(urls[0].replace("file://", "")).exists() or _Path(
        urls[0][len("file://localhost/"):] if "localhost" in urls[0] else urls[0][7:]
    ).exists()
    # 未知路径 → MissingReference + REQUIRES_USER_ACTION
    assert clips[1].media_reference.schema_name() == "MissingReference"
    requires = _cat(result.report, InterchangeEntryCategory.REQUIRES_USER_ACTION, "clip")
    assert requires and any("missing" in e.description for e in requires)


# ================================================================
# AUD-12701 — agent inbox capacity
# ================================================================


def test_aud_12701_claimed_items_do_not_consume_capacity():
    inbox = AgentInbox(max_items_per_run=2)
    for _ in range(5):
        inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "msg")
        inbox.claim("run-1")
    # 已 claim 的条目不再挤占容量：终身入队数可超上限，待处理数仍受限
    inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "p1")
    inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "p2")
    with pytest.raises(OverflowError):
        inbox.enqueue("run-1", InboxItemKind.FOLLOW_UP, "p3")
    assert inbox.has_pending("run-1")


# ================================================================
# AUD-12702 / 12703 — agent metrics
# ================================================================


def _start_run(orch: AgentOrchestrator, session: str) -> str:
    return orch.start_task(AgentTask(
        session_id=session, title="t", user_goal="g", kind="agent",
    ))


def test_aud_12702_collector_is_thread_safe():
    """worker 线程并发事件 + 查询线程迭代：无 RuntimeError、计数不丢。"""
    orch = AgentOrchestrator()
    collector = AgentMetricsCollector()
    collector.attach(orch)
    errors: list[str] = []

    def reader() -> None:
        for _ in range(400):
            try:
                collector.all_metrics()
                collector.summary()
                for m in collector.all_metrics():
                    assert m.run_id
            except RuntimeError as exc:  # pragma: no cover - 旧实现才可能触发
                errors.append(str(exc))

    def writer(session: str) -> None:
        for _ in range(20):
            run_id = _start_run(orch, session)
            for _ in range(15):
                orch.emit(run_id, AgentEventKind.RUN_TOOL_STARTED, payload={})
            orch.request_cancel(run_id)

    threads = [threading.Thread(target=reader)]
    threads += [threading.Thread(target=writer, args=(f"s{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    # 4 线程 × 20 run × 15 次工具调用，读-改-写零丢失
    assert collector.summary()["total_tool_calls"] == 4 * 20 * 15


def test_aud_12703_partial_success_counted():
    """finish_verification(partial=True) → 指标记 PARTIAL_SUCCESS（非 succeeded）。"""
    orch = AgentOrchestrator()
    collector = AgentMetricsCollector()
    collector.attach(orch)
    run_id = _start_run(orch, "partial")
    orch.mark_context_ready(run_id)
    orch.mark_plan_ready(run_id)
    orch.begin_verification(run_id)
    orch.finish_verification(run_id, success=True, partial=True)
    metrics = collector.get_metrics(run_id)
    assert metrics is not None
    assert metrics.outcome == AgentRunState.PARTIAL_SUCCESS
    outcomes = collector.summary()["outcomes"]
    assert outcomes.get(AgentRunState.PARTIAL_SUCCESS, 0) >= 1


# ================================================================
# AUD-12707 / 12708 — orchestrator
# ================================================================


def test_aud_12707_fail_from_paused_for_resume():
    orch = AgentOrchestrator()
    run_id = _start_run(orch, "s")
    orch.mark_context_ready(run_id)
    orch.mark_plan_ready(run_id)
    orch.begin_verification(run_id)
    orch.pause_for_resume(run_id)
    # 旧实现：PAUSED_FOR_RESUME 无 FAILED 出边 → ValueError
    run = orch.fail(run_id, reason="resume validation failed")
    assert run.state == AgentRunState.FAILED
    assert orch.get_run(run_id).state == AgentRunState.FAILED


def test_aud_12708_terminal_runs_auto_pruned():
    orch = AgentOrchestrator()
    run_ids = [_start_run(orch, f"s{i}") for i in range(_MAX_RETAINED_TERMINAL_RUNS + 8)]
    for rid in run_ids:
        orch.request_cancel(rid)
    # 终态转换自动淘汰：runs/events/checkpoints 均有界
    assert len(orch.list_runs()) <= _MAX_RETAINED_TERMINAL_RUNS
    evicted = run_ids[: len(run_ids) - _MAX_RETAINED_TERMINAL_RUNS]
    for rid in evicted:
        assert orch.get_run(rid) is None
        assert orch.get_events(rid) == ()
    # 保留的最近 run 仍可查
    for rid in run_ids[len(evicted):]:
        assert orch.get_run(rid) is not None
    # 手动收敛 + 非终态永不淘汰
    active_id = _start_run(orch, "active")
    before_terminal = len([r for r in orch.list_runs() if r.is_terminal])
    removed = orch.prune_terminal_runs(keep_last_n=4)
    assert removed == before_terminal - 4
    assert len([r for r in orch.list_runs() if r.is_terminal]) == 4
    assert orch.get_run(active_id) is not None
    assert orch.prune_terminal_runs(keep_last_n=0) == 4
    assert orch.get_run(active_id) is not None


# ================================================================
# AUD-12800 — executor cancel/approval race
# ================================================================


class _FakeLLM:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.last_usage = None

    def complete(self, messages: Any, **kw: Any) -> str:
        if self._responses:
            return self._responses.pop(0)
        return '{"achieved": true, "notes": "ok"}'


class _FakeFacade:
    def list_tools(self, **kw: Any) -> list[Any]:
        from src.features.edit.ai_editing.tool_facade import (
            ToolDefinition,
            ToolPermission,
        )
        return [
            ToolDefinition(
                name="topos.get_project_snapshot", permission=ToolPermission.READ,
                description="snapshot", input_schema={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="topos.apply_edit_plan", permission=ToolPermission.WRITE,
                description="apply", input_schema={"type": "object", "properties": {}},
            ),
        ]

    def invoke(self, tool_name: str, args: Any = None, *, approval: Any = None,
               **kw: Any) -> dict[str, Any]:
        return {"ok": True, "data": {"tool": tool_name}}


class _CancelDuringApprovalBridge:
    """模拟审批阻塞期间 UI 取消：返回过期的授权对象。"""

    def __init__(self, orch: AgentOrchestrator):
        self._orch = orch

    def request_approval(self, run_id: str, plan: Any) -> Any:
        self._orch.request_cancel(run_id)
        return "host-approved"  # 取消之后才返回的过期授权


def _executor(orch: AgentOrchestrator, responses: list[str], bridge: Any) -> AgentTaskExecutor:
    profile = MagicMock()
    profile.limits = MagicMock(max_context_tokens=200000)
    client = _FakeLLM(responses)
    return AgentTaskExecutor(
        orch,
        client_factory=lambda p: client,
        tool_facade=_FakeFacade(),
        profile=profile,
        approval_bridge=bridge,
    )


def _plan_json(tool: str) -> str:
    return json.dumps({
        "rationale": "r",
        "steps": [{"description": "step", "tool_name": tool, "tool_args": {}}],
    }, ensure_ascii=False)


def test_aud_12800_cancel_during_approval_returns_cancelled():
    """审批等待期间取消 → CANCELLED 结果，竞态异常不逃逸 execute()。"""
    orch = AgentOrchestrator()
    executor = _executor(orch, [_plan_json("topos.apply_edit_plan")],
                         _CancelDuringApprovalBridge(orch))
    result = executor.execute(
        session_id="s", user_goal="write",
        messages=[{"role": "user", "content": "write"}],
    )
    assert result.terminal_state == AgentRunState.CANCELLED
    assert orch.get_run(result.run_id).state == AgentRunState.CANCELLED


def test_aud_12800_transition_error_converges_to_result(monkeypatch):
    """竞态转换异常逃逸主循环时，顶层兜底返回 FAILED/实际状态的 AgentTaskResult。"""
    orch = AgentOrchestrator()

    def boom(run_id: str) -> Any:
        raise RunNotTransitionableError(
            "expected 'gathering_context' but run is 'cancelled'"
        )

    monkeypatch.setattr(orch, "mark_context_ready", boom)
    executor = _executor(orch, [_plan_json("topos.get_project_snapshot")], None)
    result = executor.execute(
        session_id="s", user_goal="read",
        messages=[{"role": "user", "content": "read"}],
    )
    assert result.terminal_state == AgentRunState.FAILED
    assert "状态竞态" in result.reply
    assert orch.get_run(result.run_id).state == AgentRunState.FAILED


# ================================================================
# AUD-12901 — analysis scheduler shutdown
# ================================================================


def test_aud_12901_shutdown_terminates_jobs_and_post_shutdown_submit_is_safe():
    from src.features.edit.ai_editing.analysis_scheduler import (
        AnalysisJobScheduler,
        ResourceLane,
    )
    from src.features.edit.ai_editing.jobs import JobHandle, JobKind, JobStatus

    release = threading.Event()
    running_count = {"n": 0}
    lock = threading.Lock()

    def slow_work() -> None:
        with lock:
            running_count["n"] += 1
        release.wait(timeout=10)

    def quick_work() -> None:
        return

    sched = AnalysisJobScheduler(asynchronous=True)
    job1 = JobHandle(kind=JobKind.ANALYSIS, label="slow1")
    job2 = JobHandle(kind=JobKind.ANALYSIS, label="slow2")
    job3 = JobHandle(kind=JobKind.ANALYSIS, label="pending")
    cancel_seen: list[str] = []
    sched.submit(ResourceLane.ANALYSIS_LOCAL, job1, work=slow_work)
    sched.submit(ResourceLane.ANALYSIS_LOCAL, job2, work=slow_work)
    # 容量（本地分析并发 2）已满 → job3 排队
    sched.submit(ResourceLane.ANALYSIS_LOCAL, job3, work=quick_work,
                 on_cancel=lambda: cancel_seen.append(job3.job_id))
    assert sched.pending_count() == 1

    sched.shutdown(wait=False)
    # 在途 job：被 cancel_futures 丢弃 → 统一 FAILED(SCHEDULER_SHUTDOWN)，不再永久 RUNNING
    assert job1.status is JobStatus.FAILED
    assert job1.error_code == "SCHEDULER_SHUTDOWN"
    assert job2.status is JobStatus.FAILED
    # pending job：QUEUED → CANCELLED，on_cancel 触发
    assert job3.status is JobStatus.CANCELLED
    assert cancel_seen == [job3.job_id]
    assert sched.is_idle()

    release.set()
    # shutdown 后 submit 不再抛 RuntimeError：executor 已置空 → 同步降级执行
    job4 = JobHandle(kind=JobKind.ANALYSIS, label="after")
    done = threading.Event()

    def work4() -> None:
        # work 契约：驱动 job 状态（与调度路径一致）
        job4.complete({})
        done.set()

    sched.submit(ResourceLane.ANALYSIS_LOCAL, job4, work=work4)
    assert done.wait(timeout=5)
    assert job4.status is JobStatus.SUCCEEDED
