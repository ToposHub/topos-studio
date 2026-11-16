"""B01 契约层测试：字段解析、单位、身份与错误语义。

这些测试不启动 Flask、不访问磁盘：契约层必须能在任何环境独立验证。
"""
from __future__ import annotations

import pytest

from src.features.files.contracts import (
    Availability,
    Capability,
    Entry,
    FilesError,
    FilesErrorCode,
    JobFailure,
    JobResult,
    Location,
    LocationKind,
    ProbeStatus,
    QueryRequest,
    UserMetadata,
    format_bytes,
    format_duration,
    identity_key,
    normalize_path,
    parse_entry,
    parse_fps,
    parse_query_result,
    split_tags,
)


def test_missing_optional_fields_stay_unknown_not_faked():
    entry = parse_entry({"name": "a.png", "path": "/media/a.png", "type": "image"})
    assert entry.is_dir is False
    assert entry.technical.duration_seconds is None
    assert entry.technical.duration_label == "—"
    assert entry.technical.probe_status == ProbeStatus.UNKNOWN
    assert entry.size_label == "—"
    assert entry.availability == Availability.UNKNOWN


def test_legacy_cached_info_is_only_a_fallback():
    entry = parse_entry({
        "name": "b.mov", "path": "/media/b.mov", "type": "video",
        "res": "1920x1080 · ProRes 422 · 25.00fps · 120 Mbps", "duration": 12.5,
    })
    assert entry.technical.resolution == "1920×1080"
    assert entry.technical.fps == pytest.approx(25.0)
    assert entry.technical.duration_seconds == pytest.approx(12.5)
    assert entry.technical.duration_label == "0:12"  # 整秒截断，不制造多一秒


def test_structured_fields_win_over_cached_text():
    entry = parse_entry({
        "name": "c.mov", "path": "/media/c.mov", "type": "video",
        "res": "1280x720 · H.264 · 30.00fps",
        "width": 3840, "height": 2160, "codec": "ProRes 4444", "fps": "30000/1001",
        "duration": 2.0,
    })
    assert entry.technical.resolution == "3840×2160"
    assert entry.technical.codec == "ProRes 4444"
    assert entry.technical.fps == pytest.approx(30000 / 1001)


def test_fps_fraction_and_invalid_values():
    assert parse_fps("30000/1001") == pytest.approx(29.970, rel=1e-3)
    assert parse_fps("0/1") is None
    assert parse_fps("abc") is None
    assert parse_fps(None) is None


def test_tags_split_dedupe_and_order():
    assert split_tags(" a, b,a ,,") == ("a", "b")
    assert split_tags(["雪景", "雪景", " 夜景 "]) == ("雪景", "夜景")
    assert split_tags("") == ()


def test_rating_color_flag_are_clamped_not_trusted():
    user = UserMetadata.from_legacy({"rating": "9", "color": -3, "flag": 7})
    assert user.rating == 5
    assert user.color == 0
    assert user.flag == 2


def test_identity_key_is_nfc_and_casefold_but_path_is_preserved():
    nfd = "/media/Cafe\u0301/Be\u0301ta.mov"
    entry = parse_entry({"name": "Be\u0301ta.mov", "path": nfd, "type": "video"})
    assert entry.identity == identity_key("/media/Café/Béta.mov")
    assert entry.path == normalize_path(nfd)
    assert identity_key("/media/A/") == identity_key("/media/a")
    assert "Be\u0301ta.mov" not in entry.name  # NFC 归一后的展示名


def test_locked_directory_maps_to_locked_error():
    request = QueryRequest("req-1", "/media/locked/")
    with pytest.raises(FilesError) as excinfo:
        parse_query_result({"locked": True, "hint": "解锁后重试", "files": []}, request)
    assert excinfo.value.code == FilesErrorCode.LOCKED
    assert excinfo.value.target.endswith("/locked")
    assert excinfo.value.retriable is False


def test_missing_path_maps_to_not_found():
    with pytest.raises(FilesError) as excinfo:
        parse_query_result({"files": [], "error": "Path not found: /x"}, QueryRequest("r", "/x"))
    assert excinfo.value.code == FilesErrorCode.NOT_FOUND
    assert excinfo.value.retriable is False


def test_invalid_payload_is_rejected_loudly():
    with pytest.raises(FilesError) as excinfo:
        parse_query_result([1, 2], QueryRequest("r", "/media"))
    assert excinfo.value.code == FilesErrorCode.INVALID_PAYLOAD
    with pytest.raises(FilesError) as excinfo:
        parse_entry({"name": "a", "type": "image"})
    assert excinfo.value.code == FilesErrorCode.INVALID_PAYLOAD


def test_query_result_total_and_truncation_are_explicit():
    payload = {
        "files": [{"name": "a", "path": "/m/a", "type": "image"} for _ in range(3)],
        "total": 4200, "truncated": True,
    }
    result = parse_query_result(payload, QueryRequest("r", "/m", generation=7))
    assert result.count == 3
    assert result.total == 4200
    assert result.truncated is True
    assert result.generation == 7
    assert result.request_id == "r"


def test_virtual_and_offline_locations_cannot_receive_files():
    search = Location(LocationKind.SEARCH, "q:tag", "标签检索", None)
    assert search.can_accept_files() is False
    offline = Location.directory("/Volumes/EXT", online=False)
    assert offline.can_accept_files() is False
    locked = Location.directory("/Volumes/EXT", locked=True)
    assert locked.can_accept_files() is False
    normal = Location.directory("/Users/creator/Movies")
    assert normal.can_accept_files() is True
    assert normal.display_name == "Movies"


def test_job_result_reports_partial_success_without_claiming_ok():
    result = JobResult(
        "job-1", successes=("/m/a.mov",),
        failures=(JobFailure("/m/b.mov", FilesError(FilesErrorCode.UNSUPPORTED, "不支持", False, "/m/b.mov")),),
    )
    assert result.ok is False
    assert result.summary() == "成功 1 · 失败 1"
    payload = result.to_dict()
    assert payload["failures"][0]["error"]["code"] == FilesErrorCode.UNSUPPORTED


def test_format_helpers_do_not_invent_values():
    assert format_duration(None) == "—"
    assert format_duration(0) == "—"
    assert format_duration(3671) == "1:01:11"
    assert format_bytes(None) == "—"
    assert format_bytes(1536) == "1.5 KB"


def test_capability_round_trip():
    capability = Capability("metadata.patch", False, "服务未响应", 2)
    assert capability.to_dict() == {
        "key": "metadata.patch", "available": False, "reason": "服务未响应", "version": 2,
    }


def test_entry_legacy_round_trip_keeps_old_keys():
    entry = Entry(
        path="/m/a.mov", name="a.mov", kind="video", size_bytes=2048, mtime_label="2026-11-16",
        technical=parse_entry({"name": "a", "path": "/m/a.mov", "type": "video",
                               "duration": 65.0, "width": 1920, "height": 1080}).technical,
        user=UserMetadata(rating=3, tags=("采访",)),
    )
    legacy = entry.to_legacy_dict()
    assert legacy["type"] == "video"
    assert legacy["duration"] == pytest.approx(65.0)
    assert legacy["tags"] == "采访"
    assert legacy["rating"] == 3
    assert legacy["size"] == "2.0 KB"
