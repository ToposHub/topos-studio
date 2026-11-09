"""I02（复查计划）：时间域/范围校验/换源状态恢复接入真实 Source widget。

对照复查证据：``open_asset`` 此前取 ``asset.fps or 24.0``、换源一律重置
frame/In/Out、``_frame_to_timecode`` 不消费起始 TC/VFR、In/Out setter 无
统一校验。本组用例钉死接入后的行为：

- Rational fps（23.976 → 24000/1001）与显式 num/den 覆盖；
- 起始 TC 偏移的 TC 显示、29.97 DF 的 ";" 输出；
- In/Out 校验（负值/越界/反置拒绝零写入，单帧范围合法），无素材拒绝；
- A→B→A 恢复播放头与 In/Out；换版（帧数变化）作废不回填假位置；
- 被污染的存储区间经时间域再校验丢弃；
- VFR 索引异步注入不阻塞、未开素材时 no-op。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.source_viewer import SourceViewerWidget
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_ui


def _asset(asset_id: str = "a", total: int = 240, fps: float = 24.0,
           **extra: Any) -> MediaAsset:
    metadata = {"fps": fps, "total_frames": total, "width": 640,
                "height": 480}
    metadata.update(extra)
    return MediaAsset(
        asset_id=asset_id, path=f"/tmp/i02_{asset_id}_{total}.mov",
        media_type="video", display_name=f"{asset_id}.mov",
        metadata=metadata)


def _widget(qapp: Any) -> SourceViewerWidget:
    widget = SourceViewerWidget()
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget._load_video_frame = lambda *_a, **_k: None  # type: ignore[method-assign]
    return widget


# ── 帧率权威 ───────────────────────────────────────────────────────


def test_i02_rational_fps_derived_from_float(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(fps=23.976))
    assert widget.source_time_domain is not None
    assert widget.source_time_domain.fps == Rational(24000, 1001)
    assert abs(widget.fps - 23.976) < 0.001  # 浮点近似仅作显示/时钟


def test_i02_explicit_rational_metadata_wins(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(fps=25.0, fps_rational_num=24000,
                             fps_rational_den=1001))
    assert widget.source_time_domain.fps == Rational(24000, 1001)


def test_importer_rational_keys_take_precedence_over_legacy_aliases(qapp: Any) -> None:
    widget = _widget(qapp)
    widget.open_asset(_asset(
        fps=25.0, fps_num=24000, fps_den=1001,
        fps_rational_num=30, fps_rational_den=1))
    assert widget.source_time_domain.fps == Rational(24000, 1001)


def test_importer_timecode_key_is_used_before_legacy_alias(qapp: Any) -> None:
    widget = _widget(qapp)
    widget.open_asset(_asset(
        timecode="01:00:00:00", start_timecode="02:00:00:00"))
    assert widget._frame_to_timecode(0) == "01:00:00:00"


# ── 时间码显示（起始 TC / DF） ─────────────────────────────────────


def test_i02_start_timecode_offsets_display(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(start_timecode="01:00:00:00"))
    assert widget._frame_to_timecode(0) == "01:00:00:00"
    assert widget._frame_to_timecode(12) == "01:00:00:12"


def test_i02_drop_frame_separator_honored(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(fps=29.97, start_timecode="01:00:00;00"))
    tc = widget._frame_to_timecode(0)
    assert ";" in tc and tc.startswith("01:00:00")
    # DF 起始后第 1800 帧落在 01:00:59;29 → 下一帧是 01:01:00;02（跳帧）
    assert widget.source_time_domain.tc_type.name == "DROP_FRAME"


# ── In/Out 校验（零写入 + 回因） ───────────────────────────────────


def test_i02_set_in_point_rejects_negative_and_past_end(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(total=100))
    rejected: list[str] = []
    ranges: list[tuple[Any, Any]] = []
    widget.range_rejected.connect(rejected.append)
    widget.range_changed.connect(lambda i, o: ranges.append((i, o)))

    widget.set_in_point(-5)
    widget.set_in_point(100)  # total=100 → 合法帧 0..99
    assert rejected == ["negative", "past_end"]
    assert ranges == []          # 零写入：不发任何状态信号
    assert widget._in_point is None


def test_i02_set_out_point_rejects_reversed_range(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(total=100))
    rejected: list[str] = []
    widget.range_rejected.connect(rejected.append)
    widget.set_in_point(10)
    widget.set_out_point(5)      # Out < In：反置拒绝

    assert rejected == ["in_after_out"]
    assert widget._in_point == 10
    assert widget._out_point is None


def test_i02_single_frame_range_is_valid(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(total=100))
    rejected: list[str] = []
    widget.range_rejected.connect(rejected.append)

    widget.set_in_point(7)
    widget.set_out_point(7)      # 单帧区间（长度 1）合法

    assert rejected == []
    assert (widget._in_point, widget._out_point) == (7, 7)


def test_i02_no_asset_rejects_mark_points(qapp):
    widget = _widget(qapp)
    rejected: list[str] = []
    widget.range_rejected.connect(rejected.append)
    widget.set_in_point(5)
    assert rejected == ["no_source_asset"]
    assert widget._in_point is None


# ── 换源状态：A→B→A / 换版作废 / 污染区间 ─────────────────────────


def test_i02_asset_roundtrip_restores_frame_and_marks(qapp):
    widget = _widget(qapp)
    asset_a, asset_b = _asset("a", total=240), _asset("b", total=120)
    widget.open_asset(asset_a)
    widget.seek(42)
    widget.set_in_point(10)
    widget.set_out_point(49)
    ranges: list[tuple[Any, Any]] = []
    widget.range_changed.connect(lambda i, o: ranges.append((i, o)))
    ranges.clear()

    widget.open_asset(asset_b)   # 离开 A（保存）
    assert widget._current_frame == 0
    assert widget._in_point is None

    widget.open_asset(asset_a)   # 回到 A（恢复）
    assert widget._current_frame == 42
    assert (widget._in_point, widget._out_point) == (10, 49)
    assert (10, 49) in ranges    # 恢复的范围随状态信号广播（按钮同步）


@pytest.mark.parametrize("endpoint", ["in", "out"])
def test_asset_roundtrip_restores_single_mark(qapp: Any, endpoint: str) -> None:
    widget = _widget(qapp)
    asset_a = _asset("a", total=100)
    widget.open_asset(asset_a)
    setter = widget.set_in_point if endpoint == "in" else widget.set_out_point
    setter(10)
    widget.open_asset(_asset("b"))
    widget.open_asset(asset_a)
    assert (widget.in_point, widget.out_point) == (
        (10, None) if endpoint == "in" else (None, 10))


@pytest.mark.parametrize("endpoint", ["in", "out"])
@pytest.mark.parametrize("value", [-1, 100])
def test_invalid_stored_single_mark_is_discarded(
        qapp: Any, endpoint: str, value: int) -> None:
    widget = _widget(qapp)
    asset_a = _asset("a", total=100)
    widget.open_asset(asset_a)
    domain = widget.source_time_domain
    widget.open_asset(_asset("b"))
    widget._monitor_state_store.save(
        asset_a.asset_id, domain.media_fingerprint, 100, frame=5,
        in_point=value if endpoint == "in" else None,
        out_point=value if endpoint == "out" else None)
    widget.open_asset(asset_a)
    assert widget.current_frame == 5
    assert widget.in_point is None and widget.out_point is None


@pytest.mark.parametrize("total,expected", [
    (1, "00:00:00:01"), (240, "00:00:10:00"),
])
def test_duration_counts_all_frames_without_start_timecode_offset(
        qapp: Any, total: int, expected: str) -> None:
    widget = _widget(qapp)
    widget.open_asset(_asset(total=total, timecode="01:00:00:00"))
    assert widget.toolbar.timecode_label.text() == "01:00:00:00"
    assert widget.toolbar.duration_label.text() == expected


@pytest.mark.parametrize("release", ["close_source", "release_for_project_close"])
def test_source_release_clears_time_domain_and_ignores_late_index(
        qapp: Any, release: str) -> None:
    widget = _widget(qapp)
    widget.open_asset(_asset("a"))
    widget.set_source_time_index(object())
    getattr(widget, release)()
    assert widget.current_asset is None
    assert widget.source_time_domain is None
    widget.set_source_time_index(object())
    assert widget.source_time_domain is None
    widget.open_asset(_asset("b"))
    assert widget.source_time_domain.asset_id == "b"
    assert widget.source_time_domain.time_index is None


def test_i02_media_version_change_invalidates_state(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset("a", total=240))
    widget.seek(88)
    widget.set_in_point(3)

    # 同 asset_id 换版（帧数变化）：指纹/帧数校验失败 → 全新状态
    widget.open_asset(_asset("a", total=120))

    assert widget._current_frame == 0
    assert widget._in_point is None


def test_i02_poisoned_stored_range_not_restored(qapp):
    widget = _widget(qapp)
    asset = _asset("a", total=100)
    widget.open_asset(asset)
    domain = widget.source_time_domain
    assert domain is not None
    widget.open_asset(_asset("b", total=60))   # 正常保存 a 后离开
    # 直接污染存储：越界区间（合法帧 0..99）
    widget._monitor_state_store.save(
        asset.asset_id, domain.media_fingerprint, 100,
        frame=5, in_point=50, out_point=400)
    widget.open_asset(asset)

    assert widget._current_frame == 5          # 播放头仍恢复
    assert widget._in_point is None            # 越界区间经时间域再校验丢弃
    assert widget._out_point is None


def test_i02_time_index_adoption(qapp):
    widget = _widget(qapp)
    widget.set_source_time_index(object())     # 未开素材：no-op
    assert widget.source_time_domain is None

    widget.open_asset(_asset("a"))
    sentinel = object()
    widget.set_source_time_index(sentinel)
    assert widget.source_time_domain.time_index is sentinel
