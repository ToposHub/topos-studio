"""Delivery Matrix（P1-COLOR-25）测试。

覆盖：
- 操作检测：primary/texture/curves/hsl/mixer/soft_clip/lut/warper/effects/
  qualifier/windows/ai_matte/plugins/baked，不可见层忽略
- 三种交付目标的能力分类（video=CPU 管线丢弃高级操作、lut=可导出项烘焙、
  still=GPU 全保留）
- Layer 对象输入（走 to_dict）
- ColorStackData v2 快照 / 旧平铺键回退 / trackings 补 tracking 行
- sequence_color_stacks 收集去重
- 文本与 preflight issue 输出（稳定 code）
- 无损报告 confirm 直通
"""
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest

from src.features.color.core.delivery_matrix import (
    DELIVERY_BAKED, DELIVERY_DROPPED, DELIVERY_FULL, TARGET_LUT,
    TARGET_STILL, TARGET_VIDEO,
    build_delivery_matrix_report, delivery_preflight_issues,
    detect_operations, format_report_text, report_from_color_stacks,
    sequence_color_stacks, sequence_video_clip_ids,
)
from src.features.color.ui.delivery_matrix_dialog import DeliveryMatrixDialog


def _layer(**over):
    layer = {
        "visible": True,
        "grading_params": {},
        "curves_data": None, "hsl_curve_data": None,
        "mixer_params": {}, "soft_clip_params": {"enabled": False},
        "active_lut_path": None, "warper_params": None,
        "effects_params": {"enabled": False},
        "qualifier_params": {"enabled": False},
        "mask_shapes": [], "plugins": [],
    }
    layer.update(over)
    return layer


# ---------------------------------------------------------------------------
# 操作检测
# ---------------------------------------------------------------------------

def test_detect_no_operations():
    assert detect_operations([]) == set()
    assert detect_operations([_layer()]) == set()


def test_detect_primary_and_texture_split():
    layer = _layer(grading_params={"exposure": 10.0})
    ops = detect_operations([layer])
    assert ops == {"primary"}
    layer = _layer(grading_params={"exposure": 10.0, "sharpness": 20.0})
    assert detect_operations([layer]) == {"primary", "texture"}
    # 只有纹理参数：不算 primary
    layer = _layer(grading_params={"mid_detail": 5.0})
    assert detect_operations([layer]) == {"texture"}
    # 默认值不算
    layer = _layer(grading_params={"lift": (0.0, 0.0, 0.0), "contrast": 0.0})
    assert detect_operations([layer]) == set()


def test_detect_advanced_operations():
    assert detect_operations([_layer(curves_data={"master": [(0, 0), (1, 1)]})]) == {"curves"}
    assert detect_operations([_layer(hsl_curve_data={"red": {}})]) == {"hsl_curves"}
    assert detect_operations([_layer(mixer_params={"red": [0.1, 0.0, 0.0]})]) == {"mixer"}
    assert detect_operations([_layer(mixer_params={"red": [0.0, 0.0, 0.0]})]) == set()
    assert detect_operations([_layer(soft_clip_params={"enabled": True})]) == {"soft_clip"}
    assert detect_operations([_layer(active_lut_path="/x.cube")]) == {"lut"}
    assert detect_operations([_layer(warper_params=[[0, 0]])]) == {"warper"}
    assert detect_operations([_layer(effects_params={"enabled": True})]) == {"effects"}
    assert detect_operations([_layer(qualifier_params={"enabled": True})]) == {"qualifier"}
    assert detect_operations([_layer(mask_shapes=[{"id": "m"}])]) == {"windows"}
    # The default enable flag alone does not mean a matte was added.
    assert detect_operations([_layer(ai_matte_enabled=True)]) == set()
    assert detect_operations([_layer(
        ai_matte_enabled=True, ai_matte="mask")]) == {"ai_matte"}
    import numpy as np
    assert detect_operations([_layer(
        ai_matte_enabled=True, ai_matte=np.ones((2, 2)))]) == {"ai_matte"}
    assert detect_operations([_layer(ai_matte_file="mask.png")]) == {"ai_matte"}
    assert detect_operations([_layer(
        ai_matte_enabled=False, ai_matte="mask")]) == set()
    assert detect_operations([_layer(
        plugins=[{"instance_id": "i", "plugin_id": "p", "enabled": True}])]) == {"plugins"}
    assert detect_operations([_layer(
        plugins=[{"instance_id": "i", "plugin_id": "p", "enabled": False}])]) == set()
    assert detect_operations([_layer(is_baked=True)]) == {"baked"}


def test_detect_invisible_layer_ignored():
    layer = _layer(visible=False, grading_params={"exposure": 50.0},
                  qualifier_params={"enabled": True})
    assert detect_operations([layer]) == set()


def test_detect_across_layers_union():
    a = _layer(grading_params={"exposure": 5.0})
    b = _layer(curves_data={"master": []})
    assert detect_operations([a, b]) == {"primary", "curves"}


# ---------------------------------------------------------------------------
# 报告与能力分类
# ---------------------------------------------------------------------------

def test_report_video_drops_advanced_keeps_primary():
    layers = [_layer(
        grading_params={"exposure": 10.0},
        curves_data={"master": []},
        qualifier_params={"enabled": True},
        mask_shapes=[{"id": "m"}],
        plugins=[{"instance_id": "i", "plugin_id": "p", "enabled": True}],
    )]
    report = build_delivery_matrix_report(layers, TARGET_VIDEO)
    by_op = {r.operation: r.delivery for r in report.rows}
    assert by_op["primary"] == DELIVERY_FULL
    assert by_op["curves"] == DELIVERY_DROPPED
    # M06.2/M05.2：基础三带 CPU 导出像素级应用（video=full）；
    # 后处理另列行（本 layer 无后处理参数 → 不出现该行）
    assert by_op["qualifier"] == DELIVERY_FULL
    assert "qualifier_postprocess" not in by_op
    assert by_op["windows"] == DELIVERY_DROPPED
    assert by_op["plugins"] == DELIVERY_DROPPED
    assert report.has_loss and report.has_dropped
    assert len(report.kept_rows) == 2


def test_report_lut_bakes_exportable_drops_spatial():
    layers = [_layer(
        grading_params={"exposure": 10.0, "sharpness": 5.0},
        curves_data={"master": []},
        soft_clip_params={"enabled": True},
        warper_params=[[0]],
        mask_shapes=[{"id": "m"}],
    )]
    report = build_delivery_matrix_report(layers, TARGET_LUT)
    by_op = {r.operation: r.delivery for r in report.rows}
    assert by_op["primary"] == DELIVERY_BAKED
    assert by_op["curves"] == DELIVERY_BAKED
    assert by_op["soft_clip"] == DELIVERY_BAKED
    assert by_op["texture"] == DELIVERY_DROPPED
    assert by_op["warper"] == DELIVERY_DROPPED
    assert by_op["windows"] == DELIVERY_DROPPED


def test_report_still_keeps_everything():
    layers = [_layer(
        grading_params={"exposure": 10.0},
        curves_data={"master": []},
        qualifier_params={"enabled": True},
        mask_shapes=[{"id": "m"}],
    )]
    report = build_delivery_matrix_report(layers, TARGET_STILL)
    assert report.rows and all(r.delivery == DELIVERY_FULL for r in report.rows)
    assert not report.has_loss


def test_report_rejects_unknown_target():
    with pytest.raises(ValueError):
        build_delivery_matrix_report([], "print")


def test_report_empty_layers():
    report = build_delivery_matrix_report([], TARGET_VIDEO)
    assert report.rows == ()
    assert not report.has_loss


def test_report_from_layer_objects():
    from src.features.color.core.layer import Layer

    layer = Layer("L")
    layer.grading_params["exposure"] = 12.0
    layer.qualifier_params["enabled"] = True
    report = build_delivery_matrix_report([layer], TARGET_VIDEO)
    by_op = {r.operation: r.delivery for r in report.rows}
    assert by_op["primary"] == DELIVERY_FULL
    # 基础带在 CPU 导出应用（M06.2）
    assert by_op["qualifier"] == DELIVERY_FULL
    # 带后处理参数 → 独立行 dropped
    layer.qualifier_params["blur"] = 3.0
    report2 = build_delivery_matrix_report([layer], TARGET_VIDEO)
    by_op2 = {r.operation: r.delivery for r in report2.rows}
    assert by_op2["qualifier"] == DELIVERY_FULL
    assert by_op2["qualifier_postprocess"] == DELIVERY_DROPPED


def test_report_from_default_layer_does_not_report_empty_ai_matte():
    from src.features.color.core.layer import Layer

    layer = Layer("L")
    assert layer.ai_matte is None
    assert layer.ai_matte_enabled is True
    report = build_delivery_matrix_report([layer], TARGET_VIDEO)
    assert "ai_matte" not in {row.operation for row in report.rows}


# ---------------------------------------------------------------------------
# ColorStack 路径
# ---------------------------------------------------------------------------

def _stack(**kw):
    from src.shared.project.color_stack import ColorStackData

    defaults = dict(stack_id="s1")
    defaults.update(kw)
    return ColorStackData(**defaults)


def test_report_from_color_stacks_v2_snapshot():
    stack = _stack(layers=(_layer(grading_params={"temp": 20.0},
                                  curves_data={"m": []}),))
    report = report_from_color_stacks([stack], TARGET_VIDEO)
    by_op = {r.operation: r.delivery for r in report.rows}
    assert by_op["primary"] == DELIVERY_FULL
    assert by_op["curves"] == DELIVERY_DROPPED


def test_report_from_color_stacks_legacy_flat_keys():
    stack = _stack(
        grading_params={"exposure": 8.0},
        curves_data={"m": []},
        mixer_params={"red": [0.2, 0, 0]},
        lut_path="/a.cube",
    )
    report = report_from_color_stacks([stack], TARGET_VIDEO)
    by_op = {r.operation: r.delivery for r in report.rows}
    assert set(by_op) == {"primary", "curves", "mixer", "lut"}
    assert all(v == DELIVERY_DROPPED for k, v in by_op.items() if k != "primary")


def test_report_from_color_stacks_tracking():
    stack = _stack(trackings={"win1": {"frames": {"12": {"x": 0.1}}}})
    report = report_from_color_stacks([stack], TARGET_VIDEO)
    assert "tracking" in {r.operation for r in report.rows}
    # still 目标不提示跟踪丢失
    still = report_from_color_stacks([stack], TARGET_STILL)
    assert "tracking" not in {r.operation for r in still.rows}


@pytest.mark.parametrize(
    "trackings",
    [
        {},
        {"schema_version": 2, "bindings": []},
        {"schema_version": 2, "bindings": [{"binding_id": "b1"}]},
        {"win1": {"frames": {}}},
    ],
)
def test_report_from_color_stacks_ignores_empty_tracking_store_or_placeholder(trackings):
    stack = _stack(trackings=trackings)
    report = report_from_color_stacks([stack], TARGET_VIDEO)
    assert "tracking" not in {row.operation for row in report.rows}


def test_report_from_color_stacks_detects_tracking_binding():
    stack = _stack(trackings={
        "schema_version": 2,
        "bindings": [{
            "binding_id": "b1", "shape_id": "w1",
            "transforms": {"12": {"matrix": [[1.0, 0.0, 0.1]]}},
        }],
    })
    report = report_from_color_stacks([stack], TARGET_VIDEO)
    assert "tracking" in {row.operation for row in report.rows}


def test_report_from_color_stacks_ignores_tracking_for_deleted_window():
    stack = _stack(
        layers=(_layer(mask_shapes=[]),),
        trackings={
            "schema_version": 2,
            "bindings": [{
                "binding_id": "b1", "clip_id": "clip1",
                "shape_id": "deleted-window",
                "transforms": {"12": {"matrix": [1.0]}},
            }],
        },
    )

    report = report_from_color_stacks(
        [stack], TARGET_VIDEO, tracking_clip_ids={"clip1"})

    assert {row.operation for row in report.rows} == set()


def test_report_from_color_stacks_ignores_tracking_for_other_clip():
    stack = _stack(trackings={
        "schema_version": 2,
        "bindings": [{
            "binding_id": "b1", "clip_id": "old-clip", "shape_id": "window1",
            "transforms": {"12": {"matrix": [1.0]}},
        }],
    })

    report = report_from_color_stacks(
        [stack], TARGET_VIDEO, tracking_clip_ids={"current-clip"})

    assert "tracking" not in {row.operation for row in report.rows}


def test_report_from_color_stacks_uses_legacy_stack_clip_owner():
    stack = _stack(clip_id="current-clip", trackings={
        "schema_version": 2,
        "bindings": [{
            "binding_id": "b1", "shape_id": "window1",
            "transforms": {"12": {"matrix": [1.0]}},
        }],
    })

    report = report_from_color_stacks(
        [stack], TARGET_VIDEO, tracking_clip_ids={"current-clip"})

    assert "tracking" in {row.operation for row in report.rows}


def test_sequence_color_stacks_collects_and_dedupes():
    clip_a = MagicMock(color_stack_id="s1")
    clip_b = MagicMock(color_stack_id="s1")       # 共享栈去重
    clip_c = MagicMock(color_stack_id="missing")  # 不存在的栈跳过
    clip_d = MagicMock(color_stack_id=None)       # 无栈跳过
    track = MagicMock(clips=[clip_a, clip_b, clip_c, clip_d])
    sequence = MagicMock(video_tracks=[track])
    project = MagicMock(color_stacks={"s1": _stack()})
    stacks = sequence_color_stacks(sequence, project)
    assert len(stacks) == 1


def test_sequence_video_clip_ids_collects_video_clip_ids():
    clip_a = SimpleNamespace(clip_id="c1")
    clip_b = SimpleNamespace(clip_id="c2")
    disabled_clip = SimpleNamespace(clip_id="c3", is_enabled=False)
    sequence = SimpleNamespace(video_tracks=[
        SimpleNamespace(clips=[clip_a, clip_b, clip_a, disabled_clip]),
        SimpleNamespace(clips=[SimpleNamespace(clip_id="c4")], is_visible=False),
    ])

    assert sequence_video_clip_ids(sequence) == {"c1", "c2"}


def test_timeline_report_ignores_orphan_tracking_from_referenced_stack():
    stack = _stack(
        layers=(_layer(mask_shapes=[]),),
        trackings={
            "schema_version": 2,
            "bindings": [{
                "binding_id": "b1", "clip_id": "removed-clip",
                "shape_id": "removed-window",
                "transforms": {"12": {"matrix": [1.0]}},
            }],
        },
    )
    clip = SimpleNamespace(
        clip_id="current-clip", color_stack_id=stack.stack_id, is_enabled=True)
    sequence = SimpleNamespace(video_tracks=[SimpleNamespace(
        clips=[clip], is_enabled=True, is_visible=True)])
    project = SimpleNamespace(color_stacks={stack.stack_id: stack})

    report = report_from_color_stacks(
        sequence_color_stacks(sequence, project), TARGET_VIDEO,
        tracking_clip_ids=sequence_video_clip_ids(sequence),
    )

    assert report.rows == ()


# ---------------------------------------------------------------------------
# 文本与 preflight 输出
# ---------------------------------------------------------------------------

def test_format_report_text_and_preflight_codes():
    layers = [_layer(grading_params={"exposure": 3.0}, curves_data={"m": []},
                     mask_shapes=[{"id": "m"}])]
    report = build_delivery_matrix_report(layers, TARGET_VIDEO)
    text = format_report_text(report)
    assert "RGB" in text and "丢失" in text and "完整保留" in text

    issues = delivery_preflight_issues(report)
    codes = [code for code, _ in issues]
    assert "color.delivery.dropped.curves" in codes
    assert "color.delivery.dropped.windows" in codes
    assert all(not c.endswith("primary") for c in codes)  # 完整保留不出 issue

    lut_report = build_delivery_matrix_report(layers, TARGET_LUT)
    lut_codes = [code for code, _ in delivery_preflight_issues(lut_report)]
    assert "color.delivery.baked.primary" in lut_codes


def test_confirm_no_loss_short_circuits():
    # 无损失：confirm 直接放行（不弹窗）
    report = build_delivery_matrix_report(
        [_layer(grading_params={"exposure": 3.0})], TARGET_VIDEO)
    assert not report.has_loss
    assert DeliveryMatrixDialog.confirm(None, report) is True


# ---------------------------------------------------------------------------
# P1-COLOR-13/15 复查：视频降噪显式报告 + 插件 export_support 措辞
# ---------------------------------------------------------------------------

class TestVideoDenoiseHonesty:
    def test_engine_denoise_on_adds_full_row_for_video(self):
        """P1-COLOR-13 收口：headless GL 桥接入导出后 video 目标完整保留。"""
        from src.features.color.core.delivery_matrix import (
            report_from_color_stacks)
        from src.shared.project.color_stack import ColorStackData

        stack = ColorStackData.create(asset_id="a1")
        report = report_from_color_stacks(
            [stack], "video", video_denoise_enabled=True)
        dn_rows = [r for r in report.rows if r.operation == "video_denoise"]
        assert len(dn_rows) == 1
        assert dn_rows[0].delivery == "full"
        assert not report.has_dropped

    def test_denoise_off_no_row(self):
        from src.features.color.core.delivery_matrix import (
            report_from_color_stacks)
        from src.shared.project.color_stack import ColorStackData

        stack = ColorStackData.create(asset_id="a1")
        report = report_from_color_stacks([stack], "video")
        assert not any(r.operation == "video_denoise" for r in report.rows)

    def test_denoise_row_feeds_preflight_issues(self):
        """LUT 交付无法表达时域/空域降噪 → preflight 仍显式报 dropped。"""
        from src.features.color.core.delivery_matrix import (
            delivery_preflight_issues, report_from_color_stacks)
        from src.shared.project.color_stack import ColorStackData

        stack = ColorStackData.create(asset_id="a1")
        lut_report = report_from_color_stacks(
            [stack], "lut", video_denoise_enabled=True)
        codes = [code for code, _ in delivery_preflight_issues(lut_report)]
        assert "color.delivery.dropped.video_denoise" in codes
        # video 目标经 GPU 桥完整保留，不再产生 dropped 提示
        video_report = report_from_color_stacks(
            [stack], "video", video_denoise_enabled=True)
        video_codes = [
            code for code, _ in delivery_preflight_issues(video_report)]
        assert "color.delivery.dropped.video_denoise" not in video_codes

    def test_layer_video_denoise_dict_detection(self):
        from src.features.color.core.delivery_matrix import detect_operations
        layer = {
            "visible": True,
            "grading_params": {},
            "video_denoise": {"spatial": {"enabled": True}},
        }
        assert "video_denoise" in detect_operations([layer])
        off = {"visible": True, "grading_params": {},
               "video_denoise": {"spatial": {"enabled": False}}}
        assert "video_denoise" not in detect_operations([off])

    def test_engine_video_denoise_enabled_helper(self):
        from types import SimpleNamespace
        from src.features.color.core.delivery_matrix import (
            engine_video_denoise_enabled)

        assert engine_video_denoise_enabled(None) is False
        assert engine_video_denoise_enabled(
            SimpleNamespace(video_denoise_params=None)) is False
        on = SimpleNamespace(video_denoise_params=SimpleNamespace(
            spatial=SimpleNamespace(enabled=False),
            temporal=SimpleNamespace(enabled=True)))
        assert engine_video_denoise_enabled(on) is True


class TestPluginExportSupportDetail:
    def test_declared_exportable_plugin_adds_detail(self, monkeypatch):
        from src.features.color.core import delivery_matrix as dm
        import src.features.color.core.plugin_manager as pm_mod

        layer = {
            "visible": True,
            "grading_params": {},
            "plugins": [{"plugin_id": "p1", "enabled": True}],
        }
        manager = SimpleNamespace(get_plugin=lambda pid: {
            "capabilities": {"export_support": "full"}})
        monkeypatch.setattr(pm_mod, "get_plugin_manager", lambda: manager)

        report = dm.build_delivery_matrix_report([layer], "video")
        plugins_row = next(r for r in report.rows if r.operation == "plugins")
        assert plugins_row.delivery == "dropped"  # 管线现实：仍丢失
        assert plugins_row.detail_key == (
            "delivery.detail_plugins_export_support")
        text = dm.format_report_text(report)
        assert "DCTL" in text or "dctl" in text

    def test_all_none_declaration_no_detail(self, monkeypatch):
        from src.features.color.core import delivery_matrix as dm
        import src.features.color.core.plugin_manager as pm_mod

        layer = {
            "visible": True,
            "grading_params": {},
            "plugins": [{"plugin_id": "p1", "enabled": True}],
        }
        manager = SimpleNamespace(get_plugin=lambda pid: {
            "capabilities": {"export_support": "none"}})
        monkeypatch.setattr(pm_mod, "get_plugin_manager", lambda: manager)

        report = dm.build_delivery_matrix_report([layer], "video")
        plugins_row = next(r for r in report.rows if r.operation == "plugins")
        assert plugins_row.detail == ""


class TestStackLevelVideoDenoiseDetection:
    """P1-COLOR-13: 栈级 per-clip 降噪参数自动进交付报告。"""

    def test_active_stack_denoise_adds_row_without_flag(self):
        from src.features.color.core.delivery_matrix import (
            report_from_color_stacks)
        from src.shared.project.color_stack import ColorStackData

        stack = ColorStackData.create(asset_id="a1")
        stack = stack.with_video_denoise(
            {"spatial": {"enabled": True}, "temporal": {"enabled": False}})
        report = report_from_color_stacks([stack], "video")
        dn = [r for r in report.rows if r.operation == "video_denoise"]
        assert len(dn) == 1 and dn[0].delivery == "full"

    def test_inactive_stack_denoise_no_row(self):
        from src.features.color.core.delivery_matrix import (
            report_from_color_stacks)
        from src.shared.project.color_stack import ColorStackData

        stack = ColorStackData.create(asset_id="a1")
        stack = stack.with_video_denoise(
            {"spatial": {"enabled": False}, "temporal": {"enabled": False}})
        report = report_from_color_stacks([stack], "video")
        assert not any(
            r.operation == "video_denoise" for r in report.rows)


# ================================================================
# M05.2 · 逐后端能力矩阵
# ================================================================

class TestConsumerCapabilityMatrix:
    def test_matrix_covers_all_delivery_ops(self):
        from src.features.color.core.delivery_matrix import _OPERATIONS
        """能力矩阵 op 集合 ⊇ _OPERATIONS 行集合（除 baked 标记行）——
        声明漂移（新增 op 忘记声明能力）被测试拦截。"""
        from src.features.color.core.delivery_matrix import (
            CONSUMER_CAPABILITY_MATRIX,
        )
        declared = set(_OPERATIONS.keys()) - {"baked"}
        matrix_ops = set(CONSUMER_CAPABILITY_MATRIX.keys())
        missing = declared - matrix_ops - {"chain_segments"}
        # chain_segments_basic/advanced 是矩阵自有键；其余 _OPERATIONS
        # 行必须有矩阵声明
        assert not missing, f"未声明能力的 op：{missing}"

    def test_video_target_consistent_with_matrix(self):
        from src.features.color.core.delivery_matrix import _OPERATIONS
        """video 导出主路径（CPU）的 _OPERATIONS 声明与能力矩阵一致。"""
        from src.features.color.core.delivery_matrix import (
            CONSUMER_CAPABILITY_MATRIX,
        )
        for op, row in _OPERATIONS.items():
            if op == "baked":
                continue
            cap = CONSUMER_CAPABILITY_MATRIX.get(op, {}).get(
                "video_export_cpu")
            expected = row[TARGET_VIDEO]
            assert cap == expected, (
                f"{op}: _OPERATIONS video={expected} 但能力矩阵 "
                f"video_export_cpu={cap}")

    def test_capability_for_unknown_fails_closed(self):
        from src.features.color.core.delivery_matrix import capability_for
        assert capability_for("nonexistent_op", "video_export_cpu") == "dropped"
        assert capability_for("primary", "unknown_consumer") == "dropped"
        assert capability_for("primary", "video_export_cpu") == "full"
        assert capability_for("lut", "lut_export") == "baked"
