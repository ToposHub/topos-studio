"""T01 · snapshot codec 契约测试（序列化恒等/hash 双轨/结构化错误）。"""

from __future__ import annotations

import json

import pytest

from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.codec import (
    SnapshotParseResult,
    compute_content_hash,
    compute_payload_hash,
    parse_snapshot,
    serialize_snapshot,
    verify_content_hash,
    verify_payload_hash,
)
from src.features.edit.insights.dto import ClipEntry


pytestmark = [pytest.mark.edit_core]


@pytest.fixture()
def snapshot():
    st = syn.build_synthetic_state()
    return capture_readonly_state(
        st.state, capture_id="cap-codec-0001", captured_at=2000.25
    )


def _rehash(text: str) -> str:
    """篡改后独立重算合法 payload hash（把字段/时间基校验从 hash 校验隔离）。

    直接按 codec 文档的规范形态（排序键、ensure_ascii=False、UTF-8、
    sha256）在测试里独立计算——被篡改的载荷无法先 parse 再借道
    compute_payload_hash，也不应借道（那会循环依赖被测代码）。
    """
    import hashlib

    data = json.loads(text)
    stored_content = data.pop("content_hash")
    data.pop("payload_hash")
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    data["payload_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    data["content_hash"] = stored_content
    return json.dumps(data)


def test_roundtrip_identity(snapshot):
    text = serialize_snapshot(snapshot)
    result = parse_snapshot(text)
    assert isinstance(result, SnapshotParseResult)
    assert result.ok, result.error
    assert result.snapshot == snapshot  # dataclass 全字段恒等


def test_serialize_is_deterministic(snapshot):
    assert serialize_snapshot(snapshot) == serialize_snapshot(snapshot)


def test_payload_hash_mismatch_detected(snapshot):
    text = serialize_snapshot(snapshot)
    data = json.loads(text)
    data["project"]["name"] = "Tampered"
    tampered = json.dumps(data)
    result = parse_snapshot(tampered)
    assert not result.ok
    assert result.error.code == "HASH_MISMATCH"


def test_unsupported_schema_rejected(snapshot):
    text = serialize_snapshot(snapshot)
    data = json.loads(text)
    data["schema_version"] = "2"
    tampered = json.dumps(data)
    result = parse_snapshot(tampered)
    assert not result.ok
    assert result.error.code == "UNSUPPORTED_SCHEMA"


def test_unknown_field_rejected(snapshot):
    text = serialize_snapshot(snapshot)
    data = json.loads(text)
    data["future_field"] = 1
    result = parse_snapshot(_rehash(json.dumps(data)))
    assert not result.ok
    assert result.error.code == "UNKNOWN_FIELD"


def test_invalid_timebase_rejected_even_with_valid_hash(snapshot):
    text = serialize_snapshot(snapshot)
    data = json.loads(text)
    for seq in data["sequences"]:
        if seq["sequence_id"] == syn.SEQ_MAIN:
            for track in seq["tracks"]:
                for clip in track["clips"]:
                    if clip["clip_id"] == syn.CLIP_A1:
                        clip["timeline_end"] = -5  # end < start 且为负
    result = parse_snapshot(_rehash(json.dumps(data)))
    assert not result.ok
    assert result.error.code == "INVALID_TIMEBASE"


def test_missing_field_rejected(snapshot):
    text = serialize_snapshot(snapshot)
    data = json.loads(text)
    del data["captured_at"]
    result = parse_snapshot(json.dumps(data))
    assert not result.ok
    assert result.error.code == "MISSING_FIELD"


def test_invalid_json_structured_error():
    result = parse_snapshot("{not json")
    assert not result.ok
    assert result.error.code == "INVALID_JSON"


def test_content_hash_ignores_capture_metadata_and_revisions():
    """同内容、不同 capture_id/时间/revision → content_hash 相同。"""
    base = syn.build_synthetic_state()
    bumped = syn.build_bumped_state(base)

    snap_a = capture_readonly_state(
        base.state, capture_id="cap-a", captured_at=1000.0
    )
    snap_b = capture_readonly_state(
        bumped.state, capture_id="cap-b", captured_at=2000.0
    )
    assert compute_content_hash(snap_a) == compute_content_hash(snap_b)
    assert compute_payload_hash(snap_a) != compute_payload_hash(snap_b)
    assert verify_content_hash(snap_a) and verify_content_hash(snap_b)


def test_content_hash_detects_real_content_change():
    """真实编辑（clip 时长变化）→ content_hash 变化。"""
    from dataclasses import replace as dc_replace

    base = syn.build_synthetic_state()
    snap_a = capture_readonly_state(base.state, capture_id="cap-a", captured_at=1.0)

    # 内容变化：S1 的 CUE_ZH 文本改掉（其余全同，revision 也保持）
    changed_main = dc_replace(
        base.main,
        subtitle_tracks=(
            dc_replace(
                base.main.subtitle_tracks[0],
                subtitle_clips=(
                    type(base.main.subtitle_tracks[0].subtitle_clips[0])(
                        clip_id=syn.CUE_ZH,
                        track_id=syn.TRACK_SUB1,
                        timeline_start=10,
                        timeline_duration=60,
                        text="改过的台词",
                    ),
                    base.main.subtitle_tracks[0].subtitle_clips[1],
                ),
            ),
        ),
    )
    sequences = dict(base.sequences)
    sequences[syn.SEQ_MAIN] = changed_main
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    snap_b = capture_readonly_state(
        ProjectSequences(project=base.project, sequences=sequences),
        capture_id="cap-b",
        captured_at=2.0,
    )
    assert compute_content_hash(snap_a) != compute_content_hash(snap_b)


def test_clip_entry_durations_derive_from_half_open():
    c = ClipEntry(
        clip_id="c", sequence_id="s", track_id="t", track_type="video",
        asset_id="a", timeline_start=5, timeline_end=125,
        source_start=0, source_end=120, is_enabled=True,
        source_mapping="identity",
    )
    assert c.timeline_duration == 120
    assert c.source_duration == 120
