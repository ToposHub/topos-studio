"""KF-01：共享轨道描述与参数编解码（keyframe_tracks）回归。

覆盖计划验收条件：
- 每个登记字段有真实往返（decode→encode→decode 恒等）；
- 每个无能力/非法值有被拒反例（显式原因码）；
- 身份稳定：显示名/改名/重排/同形状实例不重绑轨道；
- legacy 三插值模式在新键型模型下帧值语义等价；
- 工具覆盖清单与注册表同步（生成物强制一致）。
"""

import math

import pytest

from src.features.color.core import grade_keyframes as gk
from src.features.color.core import keyframe_tracks as kt
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.layer import MaskShape


# ---------------------------------------------------------------------------
# 单一权威：grade_keyframes 再导出与图命令白名单同源
# ---------------------------------------------------------------------------

def test_registries_are_single_sourced():
    assert gk.GRADE_KEYFRAME_PARAMS is kt.GRADE_KEYFRAME_PARAMS
    assert gk.MASK_KEYFRAME_FIELDS is kt.MASK_KEYFRAME_FIELDS
    assert gk.QUALIFIER_KEYFRAME_FIELDS is kt.QUALIFIER_KEYFRAME_FIELDS
    assert gk.CIRCULAR_QUALIFIER_FIELDS is kt.CIRCULAR_QUALIFIER_FIELDS
    assert gk.INTERPOLATION_MODES == kt.INTERPOLATION_MODES


def test_graph_command_whitelist_matches_registry():
    service_whitelist = ColorGraphService()._keyframe_track_whitelist()
    assert service_whitelist == kt.node_command_track_whitelist()
    assert "exposure" in service_whitelist
    assert "temperature" in service_whitelist  # 兼容双名仍可写
    assert kt.qualifier_track_name("hue_center") in service_whitelist
    # 可枚举白名单不含 mask.*（per-shape UUID 不可枚举）——经解析谓词准入
    assert not any(
        name.startswith("mask.") for name in service_whitelist)
    shape_id = "11111111-2222-3333-4444-555555555555"
    assert kt.track_name_is_writable(
        kt.mask_track_name(shape_id, "center"))
    assert kt.track_name_is_writable("exposure")
    assert not kt.track_name_is_writable("sizing/input")
    assert not kt.track_name_is_writable("not_a_param")


def test_parse_track_name_round_trip_all_domains():
    for spec in kt.GRADE_TRACK_FIELDS:
        address = kt.parse_track_name(spec.name)
        assert address is not None and address.domain == "grade"
        assert address.field == spec.name
        assert address.track_name == spec.name
    shape_id = "11111111-2222-3333-4444-555555555555"
    for spec in kt.WINDOW_TRACK_FIELDS:
        name = kt.mask_track_name(shape_id, spec.name)
        address = kt.parse_track_name(name)
        assert address is not None and address.domain == "window"
        assert (address.owner_id, address.field) == (shape_id, spec.name)
        assert address.track_name == name
    for spec in kt.QUALIFIER_TRACK_FIELDS:
        name = kt.qualifier_track_name(spec.name)
        address = kt.parse_track_name(name)
        assert address is not None and address.domain == "qualifier"
        assert address.track_name == name


def test_parse_track_name_rejects_unregistered():
    assert kt.parse_track_name("not_a_param") is None
    assert kt.parse_track_name("qualifier.not_a_field") is None
    assert kt.parse_track_name("mask.s1.not_a_field") is None
    assert kt.parse_track_name("mask.no_field_dot") is None
    # 离散/点集字段未登记为可动画
    assert kt.parse_track_name("mask.s1.visible") is None
    assert kt.parse_track_name("mask.s1.points") is None
    assert kt.parse_track_name("mask.s1.grad_points") is None
    # sizing / fx 轨道名尚未开放
    assert kt.parse_track_name("sizing/input") is None
    assert kt.parse_track_name("fx.inst1.gain") is None


# ---------------------------------------------------------------------------
# 稳定身份：显示名/改名/重排/同形状实例不重绑
# ---------------------------------------------------------------------------

def test_window_track_identity_is_shape_uuid_not_display_name():
    shape_a = MaskShape(MaskShape.CIRCLE)
    shape_b = MaskShape(MaskShape.CIRCLE)
    # 相同几何的两个实例：uuid 不同 → 轨道名不同（互不重绑）
    assert shape_a.id != shape_b.id
    assert (kt.mask_track_name(shape_a.id, "center")
            != kt.mask_track_name(shape_b.id, "center"))
    # 显示名（name）不参与轨道身份：改名前后轨道名不变
    before = kt.mask_track_name(shape_a.id, "center")
    shape_a.name = "我的窗口"
    assert kt.mask_track_name(shape_a.id, "center") == before
    # 重排（列表顺序）不影响解析——轨道名只含 shape_id 与字段
    address = kt.parse_track_name(before)
    assert address is not None and address.owner_id == shape_a.id


def test_node_rename_and_reorder_do_not_affect_track_identity():
    # 轨道名不含节点标题/序号：同一字段名在任何节点上解析结果一致
    address = kt.parse_track_name("exposure")
    assert address == kt.TrackAddress("grade", "exposure")
    assert address.track_name == "exposure"


# ---------------------------------------------------------------------------
# 键型与动态属性：legacy 等价 + 统一权重函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,kind,start,end", [
    ("hold", kt.KEY_KIND_STATIC, 1.0, 1.0),
    ("linear", kt.KEY_KIND_DYNAMIC, 1.0, 1.0),
    ("ease", kt.KEY_KIND_DYNAMIC, 0.0, 0.0),
])
def test_legacy_mode_to_dynamics_round_trip(mode, kind, start, end):
    assert kt.legacy_mode_to_dynamics(mode) == (kind, start, end)
    assert kt.dynamics_to_legacy_mode(kind, start, end) == mode


def test_segment_weight_matches_legacy_interp_factor():
    for u in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert kt.segment_weight(u, 1.0, 1.0) == pytest.approx(u)
        assert kt.segment_weight(u, 0.0, 0.0) == pytest.approx(
            gk._interp_factor(u, "ease"))
        # 静态左键：右侧保持（等价 legacy hold 的 0 权重）
        assert kt.segment_progress(
            u, kt.KEY_KIND_STATIC, 1.0, 1.0) == pytest.approx(
            gk._interp_factor(u, "hold"))


def test_segment_weight_is_monotone_on_valid_domain():
    for start in (0.0, 0.25, 0.5, 1.0, 1.5):
        for end in (0.0, 0.25, 0.5, 1.0, 1.5):
            previous = -1.0
            for step in range(21):
                weight = kt.segment_weight(step / 20.0, start, end)
                assert weight >= previous - 1e-9
                previous = weight


def test_segment_weight_non_default_pending_kf00_semantics():
    # 非 (1,1)/(0,0) 档位：PENDING-KF00——Hermite 兼容语义，
    # 端点约束 w(0)=0、w(1)=1 必须成立
    for start, end in ((0.5, 0.5), (0.25, 0.75), (1.0, 0.0), (0.0, 1.0)):
        assert kt.segment_weight(0.0, start, end) == pytest.approx(0.0)
        assert kt.segment_weight(1.0, start, end) == pytest.approx(1.0)


def test_legacy_keyframe_judgement():
    legacy = kt.decode_keyframe(
        {"frame": 3, "value": 1.5, "interpolation": "linear"})
    assert kt.keyframe_uses_legacy_interpolation(legacy)
    eased = kt.decode_keyframe(
        {"frame": 3, "value": 1.5, "interpolation": "ease"})
    assert kt.keyframe_uses_legacy_interpolation(eased)
    custom = kt.make_keyframe(3, 1.5, kind="dynamic", start=0.5, end=0.5)
    assert not kt.keyframe_uses_legacy_interpolation(custom)


# ---------------------------------------------------------------------------
# 编解码：真实字段往返 + 非法值反例
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("track_name,value", [
    ("exposure", 1.5),
    ("exposure", 2),  # int 亦合法（归一 float）
    ("lift", (0.1, 0.2, 0.3)),
    ("lift", [0.1, 0.2, 0.3]),
    ("temperature", -0.75),  # 兼容双名往返
    (kt.qualifier_track_name("hue_center"), 0.9),
    (kt.qualifier_track_name("lum_soft_high"), 0.25),
])
def test_decode_encode_round_trip_real_fields(track_name, value):
    payload = {"frame": 12, "value": value, "interpolation": "linear"}
    decoded = kt.decode_keyframe(payload)
    assert decoded.frame == 12
    assert decoded.kind == kt.KEY_KIND_DYNAMIC
    encoded = kt.encode_keyframe(decoded)
    expected = dict(payload)
    if isinstance(expected["value"], tuple):
        expected["value"] = list(expected["value"])  # 序列化归一 list
    assert encoded == expected  # canonical-minimal：旧载荷逐字节不变
    assert kt.decode_keyframe(encoded) == decoded


def test_mask_track_round_trip_with_value_shape():
    shape_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    vec2_name = kt.mask_track_name(shape_id, "center")
    payload = {"frame": 5, "value": [0.5, 0.5], "interpolation": "ease"}
    decoded = kt.decode_keyframe(payload)
    assert kt.validate_value_shape(vec2_name, decoded.value) is None
    assert kt.encode_keyframe(decoded) == payload
    scalar_name = kt.mask_track_name(shape_id, "feather")
    assert kt.validate_value_shape(scalar_name, 12.0) is None


def test_explicit_dynamics_round_trip():
    payload = {"frame": 8, "value": 0.4, "interpolation": "linear",
               "kind": "dynamic", "start": 0.25, "end": 0.75}
    decoded = kt.decode_keyframe(payload)
    assert (decoded.kind, decoded.start, decoded.end) == (
        kt.KEY_KIND_DYNAMIC, 0.25, 0.75)
    # 非等价档位必须显式落盘，否则信息丢失
    encoded = kt.encode_keyframe(decoded)
    assert encoded["kind"] == "dynamic"
    assert encoded["start"] == 0.25 and encoded["end"] == 0.75
    assert kt.decode_keyframe(encoded) == decoded


def test_kind_is_authoritative_on_conflict():
    decoded = kt.decode_keyframe(
        {"frame": 1, "value": 2.0, "interpolation": "linear",
         "kind": "static"})
    assert decoded.kind == kt.KEY_KIND_STATIC
    assert decoded.interpolation == "hold"  # 键型归一插值模式


def test_legacy_ease_decode_carries_zero_weights():
    decoded = kt.decode_keyframe(
        {"frame": 2, "value": 1.0, "interpolation": "ease",
         "kind": "dynamic"})
    assert (decoded.start, decoded.end) == (0.0, 0.0)
    assert decoded.interpolation == "ease"


@pytest.mark.parametrize("payload,reason", [
    ({"frame": 0}, "missing_value"),
    ({"frame": 0, "value": True}, "bad_value_type"),
    ({"frame": 0, "value": "x"}, "bad_value_type"),
    ({"frame": 0, "value": {}}, "bad_value_type"),
    ({"frame": 0, "value": []}, "empty_value_sequence"),
    ({"frame": 0, "value": float("nan")}, "non_finite_value"),
    ({"frame": -1, "value": 1.0}, "negative_frame"),
    ({"frame": 0, "value": 1.0, "interpolation": "bezier"}, "bad_interpolation"),
    ({"frame": 0, "value": 1.0, "kind": "bezier"}, "bad_key_kind"),
    ({"frame": 0, "value": 1.0, "kind": "dynamic", "start": -0.5},
     "dynamic_weight_out_of_range"),
    ({"frame": 0, "value": 1.0, "kind": "dynamic", "end": 99.0},
     "dynamic_weight_out_of_range"),
])
def test_decode_rejects_illegal_values(payload, reason):
    with pytest.raises(ValueError, match=reason):
        kt.decode_keyframe(payload)


def test_decode_rejects_non_finite_weights():
    with pytest.raises(ValueError, match="dynamic_weight_out_of_range"):
        kt.decode_keyframe({"frame": 0, "value": 1.0, "kind": "dynamic",
                            "start": float("nan"), "end": 1.0})


@pytest.mark.parametrize("track_name,value,reason", [
    ("exposure", (1.0, 2.0), "bad_value_arity"),
    ("lift", (1.0, 2.0), "bad_value_arity"),
    ("lift", 1.0, "bad_value_arity"),
    (kt.qualifier_track_name("lum_low"), (1.0, 2.0), "bad_value_arity"),
    ("exposure", True, "bad_value_type"),
    ("exposure", None, "bad_value_type"),
    ("exposure", (1.0, float("inf"), 2.0), "non_finite_value"),
])
def test_validate_value_shape_rejections(track_name, value, reason):
    assert kt.validate_value_shape(track_name, value) == reason


def test_validate_value_shape_unknown_track_forward_compat():
    # 未登记轨道不校验分量数（向前兼容，既有 AUD-0520 语义）
    assert kt.validate_value_shape("future_param", 1.0) is None
    assert kt.expected_value_size("future_param") is None


def test_expected_value_size_all_domains():
    assert kt.expected_value_size("exposure") == 1
    assert kt.expected_value_size("gain") == 3
    shape_id = "s"
    assert kt.expected_value_size(kt.mask_track_name(shape_id, "size")) == 2
    assert kt.expected_value_size(
        kt.mask_track_name(shape_id, "rotation")) == 1
    assert kt.expected_value_size(
        kt.qualifier_track_name("hue_center")) == 1


# ---------------------------------------------------------------------------
# 工具覆盖清单：生成物与注册表同步
# ---------------------------------------------------------------------------

def test_coverage_manifest_covers_all_registered_fields():
    content = kt.render_coverage_markdown()
    for spec in (kt.GRADE_TRACK_FIELDS + kt.WINDOW_TRACK_FIELDS
                 + kt.QUALIFIER_TRACK_FIELDS):
        assert f"| {spec.name} |" in content
    # 未支持域必须给出可见原因
    for domain in ("Sizing", "Open FX"):
        assert domain in content
    assert "PENDING-KF00" in content
    # window 全能力开放（写入谓词 + 形状存在性校验；KF-03 后续完成项）
    window_row = next(line for line in content.splitlines()
                      if line.startswith("| Window"))
    assert "✗" not in window_row


def test_coverage_manifest_file_in_sync():
    doc_path = ("docs/plans/2026-11-06-color-keyframes/"
                "TOOL_COVERAGE.md")
    with open(doc_path, encoding="utf-8") as handle:
        checked_in = handle.read()
    assert checked_in == kt.render_coverage_markdown()


def test_capability_rows_shapes():
    rows = kt.capability_rows()
    by_domain = {}
    for row in rows:
        by_domain.setdefault(row["domain"], []).append(row)
    assert set(by_domain) == {"grade", "window", "qualifier", "sizing", "fx"}
    # grade/qualifier 当前全能力可用
    for domain in ("grade", "qualifier"):
        summary = by_domain[domain][0]
        assert all(summary[column] for column in kt.CAPABILITY_COLUMNS)
    # window 全能力可用（write 谓词准入由批命令目标校验执行）
    window_summary = by_domain["window"][0]
    assert all(window_summary[column] for column in kt.CAPABILITY_COLUMNS)
    assert window_summary["write_domain"] == "graph+layer"
