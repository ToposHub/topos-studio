"""TAEP Phase 8 — OTIO adapter 骨架 + nested sequence 测试。

验收（TAEP §12 P8）：
- opentimelineio 未安装时，OTIO adapter graceful degrade（不阻塞主程序）。
- nested sequence flatten 策略正确展平引用其他 sequence 的 clip。
- InterchangeAdapter 协议一致（与 FCPXML 同接口）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.adapters.fcpxml_adapter import FCPXMLAdapter
from src.features.edit.adapters.interchange_types import InterchangeEntryCategory
from src.features.edit.adapters.otio_adapter import (
    NESTED_SEQUENCE_POLICY,
    OTIOAdapter,
    flatten_nested_sequence,
)
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


# ================================================================
# OTIO graceful degrade
# ================================================================


def test_otio_inspect_reports_unavailable_without_dep(tmp_path):
    """若无 opentimelineio，inspect 报告不可用（不抛异常）。"""
    p = tmp_path / "fake.otio"
    p.write_text("{}", encoding="utf-8")
    adapter = OTIOAdapter()
    inspection = adapter.inspect(p)
    assert inspection.format_id == "otio"
    # 无依赖时 supported=False（诊断含 warning）
    if not inspection.supported:
        assert inspection.report is not None
        assert any(d.get("severity") == "warning" for d in inspection.report.diagnostics)


def test_otio_import_degrades_without_dep(tmp_path):
    p = tmp_path / "fake.otio"
    p.write_text("{}", encoding="utf-8")
    result = OTIOAdapter().import_to_canonical(p)
    # 无依赖时返回空 sequences
    assert result.sequences == ()


def test_otio_adapter_same_protocol_as_fcpxml():
    """OTIO 与 FCPXML 实现同一个 InterchangeAdapter 协议。"""
    fcpxml = FCPXMLAdapter()
    otio = OTIOAdapter()
    # 都有 format_id / inspect / import_to_canonical / export_from_canonical
    assert hasattr(fcpxml, "format_id") and hasattr(otio, "format_id")
    assert callable(fcpxml.inspect) and callable(otio.inspect)
    assert fcpxml.format_id == "fcpxml"
    assert otio.format_id == "otio"


# ================================================================
# nested sequence flatten（不需 OTIO 依赖）
# ================================================================


def test_flatten_nested_sequence_resolves_references():
    """嵌套 sequence 引用的 clip 被展平到父时间线。"""
    # 子 sequence：2 个 clip 在 [0,100) 和 [100,100)
    child_clip_a = ClipInstance(
        clip_id="child_a", asset_id="real_a", track_id="v1",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    child_clip_b = ClipInstance(
        clip_id="child_b", asset_id="real_b", track_id="v1",
        timeline_start=100, timeline_duration=100, source_start=0, source_duration=100,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    child = TimelineSequence(
        timeline_id="child_seq", name="Child", fps=__import__("src.shared.media.metadata", fromlist=["Rational"]).Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                                clips=(child_clip_a, child_clip_b)),),
    )
    # 父 sequence：1 个 clip 引用 child（asset_id="child_seq"），位于 timeline_start=200
    parent_clip = ClipInstance(
        clip_id="nested_ref", asset_id="child_seq", track_id="v1",
        timeline_start=200, timeline_duration=200, source_start=0, source_duration=200,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    parent = TimelineSequence(
        timeline_id="parent_seq", name="Parent", fps=child.fps,
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                                clips=(parent_clip,)),),
    )

    flattened = flatten_nested_sequence(parent, {"child_seq": child})
    flat_clips = flattened.video_tracks[0].clips
    # 嵌套展开为 2 个 clip，偏移到 200+
    assert len(flat_clips) == 2
    assert flat_clips[0].timeline_start == 200  # 0 + offset 200
    assert flat_clips[1].timeline_start == 300  # 100 + offset 200
    # asset_id 是真实素材（不是 child_seq）
    assert all(c.asset_id in ("real_a", "real_b") for c in flat_clips)


def test_nested_sequence_policy_options():
    assert "flatten" in NESTED_SEQUENCE_POLICY
    assert "retain" in NESTED_SEQUENCE_POLICY
    assert "reference" in NESTED_SEQUENCE_POLICY
