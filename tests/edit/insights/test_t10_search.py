"""T10 · 跨序列联合搜索测试（对照 expected.py 独立预期）。"""

from __future__ import annotations

import pytest

from tests.edit.insights import expected as exp
from tests.edit.insights import scale_fixture
from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.search import (
    ENTITY_ASSET,
    ENTITY_CLIP,
    ENTITY_CUE,
    SearchQuery,
    export_saved_queries,
    load_saved_queries,
    parse_query_string,
    unified_search,
)


pytestmark = [pytest.mark.edit_core]


@pytest.fixture(scope="module")
def snap():
    st = syn.build_synthetic_state()
    return capture_readonly_state(st.state, capture_id="cap-t10", captured_at=0.0)


def _hit_keys(result):
    return {(h.sequence_id, h.entity_id) for h in result.hits}


def test_chinese_duplicate_text_two_independent_hits(snap):
    """跨序列重复字幕："你好，世界" 在 S1/S2 各一条，独立保留。"""
    result = unified_search(snap, SearchQuery(text="你好"))
    assert _hit_keys(result) == exp.SEARCH_NIHAO
    by_seq = {h.sequence_id: h for h in result.hits}
    assert by_seq[syn.SEQ_MAIN].entity_id == syn.CUE_ZH
    assert by_seq[syn.SEQ_NTSC].entity_id == syn.CUE_SHARED
    # 身份三件套齐全（导航用）
    for hit in result.hits:
        seq_id, entity_id, track_id = hit.navigate_identity
        assert seq_id and entity_id and track_id


def test_same_asset_multiple_usages_independent_clip_hits(snap):
    """同一素材多次使用：3 处 clip 命中各自独立、区间不同。"""
    result = unified_search(snap, SearchQuery(text="CamA", entity_types=(ENTITY_CLIP,)))
    assert _hit_keys(result) == exp.SEARCH_CAMA_CLIPS
    intervals = {(h.timeline_start, h.timeline_end) for h in result.hits}
    assert intervals == {(0, 120), (120, 200), (0, 100)}  # 手算区间


def test_asset_hit_reports_pool_identity(snap):
    result = unified_search(snap, SearchQuery(text="CamA", entity_types=(ENTITY_ASSET,)))
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert (hit.sequence_id, hit.entity_id) == exp.SEARCH_CAMA_ASSET
    assert hit.matched_field == "display_name"


def test_case_insensitive_default(snap):
    result = unified_search(snap, SearchQuery(text="cama"))
    assert any(h.entity_type == ENTITY_ASSET for h in result.hits)
    strict = unified_search(snap, SearchQuery(text="cama", case_sensitive=True))
    assert not strict.hits  # 原文是 CamA，大小写敏感不命中


def test_multiline_quotes_chinese_text(snap):
    result = unified_search(snap, SearchQuery(text='with "quotes"'))
    assert _hit_keys(result) == exp.SEARCH_QUOTE
    hit = result.hits[0]
    assert "\n" in hit.matched_text      # 原文保真
    assert "⏎" in hit.entity_label       # 显示用单行化
    # 标点/unicode
    assert unified_search(snap, SearchQuery(text="，逗号")).hits
    assert unified_search(snap, SearchQuery(text="Second line")).hits


def test_marker_name_and_comment_searchable(snap):
    by_name = unified_search(snap, SearchQuery(text="转折点"))
    assert {(h.sequence_id, h.entity_id) for h in by_name.hits} == {(syn.SEQ_MAIN, syn.MARK_TURN)}
    by_comment = unified_search(snap, SearchQuery(text="剧情反转"))
    assert {h.entity_id for h in by_comment.hits} == {syn.MARK_TURN}


def test_asset_log_fields_searchable(snap):
    """keywords/rating 之外的日志字段（comment）可搜；rating 不可搜（数值）。"""
    st = syn.build_synthetic_state()
    assert unified_search(snap, SearchQuery(text="interview")).hits  # keywords
    assert not unified_search(snap, SearchQuery(text="4")).hits or all(
        h.entity_type != ENTITY_ASSET or h.matched_field != "rating" for h in unified_search(snap, SearchQuery(text="4")).hits
    )


def test_empty_or_whitespace_no_hits(snap):
    for text in ("", "   ", "\t"):
        result = unified_search(snap, SearchQuery(text=text))
        assert result.hits == ()
        assert result.searched_sequences  # 搜索范围仍如实报告
        assert syn.SEQ_UNLOADED in result.excluded_unloaded


def test_unloaded_reported_not_fake_zero(snap):
    """未加载序列出现在 excluded_unloaded，不参与 searched 也不算零结果。"""
    result = unified_search(snap, SearchQuery(text="任意词"))
    assert syn.SEQ_UNLOADED in result.excluded_unloaded
    assert syn.SEQ_UNLOADED not in result.searched_sequences
    assert set(result.searched_sequences) == {syn.SEQ_MAIN, syn.SEQ_NTSC}


def test_sequence_and_duration_filters(snap):
    only_ntsc = unified_search(snap, SearchQuery(text="你好", sequence_ids=(syn.SEQ_NTSC,)))
    assert {h.sequence_id for h in only_ntsc.hits} == {syn.SEQ_NTSC}

    # 时长过滤（手算：CamA clip = 120/80/100，CamB = 120/60）：
    # ≥100 → A1(120)、B1(120)、NTSC1(100)
    result = unified_search(
        snap, SearchQuery(text="Cam", entity_types=(ENTITY_CLIP,), min_duration_frames=100)
    )
    assert {h.entity_id for h in result.hits} == {syn.CLIP_A1, syn.CLIP_B1, syn.CLIP_NTSC1}
    # ≤80 → A2(80)、B2(60)
    result_max = unified_search(
        snap, SearchQuery(text="Cam", entity_types=(ENTITY_CLIP,), max_duration_frames=80)
    )
    assert {h.entity_id for h in result_max.hits} == {syn.CLIP_A2, syn.CLIP_B2}


def test_query_syntax_restricted_prefixes(snap):
    parsed = parse_query_string("type:cue 你好")
    assert parsed.text == "你好"
    assert parsed.entity_types == (ENTITY_CUE,)
    result = unified_search(snap, parsed)
    assert all(h.entity_type == ENTITY_CUE for h in result.hits)

    parsed_tl = parse_query_string("timeline:ntsc 你好")
    assert parsed_tl.text == "你好"
    result_tl = unified_search(snap, parsed_tl)
    assert {h.sequence_id for h in result_tl.hits} == {syn.SEQ_NTSC}

    # 未知前缀不吞并：完整保留为文本（整串作字面子串，不额外拆词）
    parsed_foo = parse_query_string("foo:bar CamA")
    assert parsed_foo.text == "foo:bar CamA"
    assert parsed_foo.entity_types == ()
    # 单独的 CamA 仍正常命中（文本保留不影响显式查询）
    assert unified_search(snap, SearchQuery(text="CamA")).hits

    # type 前缀只影响实体过滤，不影响文本
    assert parse_query_string("type:marker").text == ""


def test_truncation_flag(snap):
    result = unified_search(snap, SearchQuery(text="你好"), limit=1)
    assert result.truncated is True
    assert len(result.hits) == 1


def test_saved_queries_roundtrip(tmp_path):
    queries = [
        SearchQuery(text="你好", entity_types=(ENTITY_CUE,)),
        parse_query_string("timeline:ntsc type:clip CamA"),
    ]
    path = str(tmp_path / "saved.json")
    export_saved_queries(path, queries, name="测试收藏")
    loaded = load_saved_queries(path)
    assert loaded == queries


def test_saved_queries_reject_unknown_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema": "other/9", "queries": []}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_saved_queries(str(path))


def test_scale_10k_expected_count():
    """10k 规模：cue 文本 common-word-{i%10}，搜 common-word-3 → 手算 10 条。"""
    sc = scale_fixture.build_scale_state(10_000)
    snapshot = capture_readonly_state(sc.state, capture_id="cap-t10-scale")
    result = unified_search(snapshot, SearchQuery(text="common-word-3"))
    assert len(result.hits) == 10  # 10000//100 = 100 条 cue，i%10==3 → 10 条
    assert result.truncated is False
