"""Stable runtime diagnostic records for the composite render boundary."""

from src.features.composite.core.runtime_diagnostics import (
    DiagnosticBus,
    RuntimeDiagnostic,
    records_from_scene_diagnostics,
)


def test_runtime_diagnostic_to_dict_is_json_safe():
    record = RuntimeDiagnostic(
        code="asset.missing",
        severity="error",
        message="文件不存在",
        source="model_import",
        node_id="model-1",
        frame=12,
        suggestion="重新链接素材",
    )
    assert record.to_dict() == {
        "code": "asset.missing",
        "severity": "error",
        "message": "文件不存在",
        "source": "model_import",
        "node_id": "model-1",
        "frame": 12,
        "suggestion": "重新链接素材",
    }


def test_diagnostic_bus_is_bounded_and_returns_snapshot():
    bus = DiagnosticBus(max_records=2)
    bus.publish(RuntimeDiagnostic("a", "warning", "A"))
    bus.publish(RuntimeDiagnostic("b", "warning", "B"))
    bus.publish(RuntimeDiagnostic("c", "warning", "C"))
    assert [item.code for item in bus.snapshot()] == ["b", "c"]
    bus.clear()
    assert bus.snapshot() == ()


def test_scene_diagnostics_normalize_and_dedupe_runtime_records():
    records = records_from_scene_diagnostics({
        "model_node_id": "model-1",
        "model_error": "asset.missing: 文件不存在",
        "model_import_diagnostics": ["asset.missing: 文件不存在"],
        "model_diagnostics": ["skinning.weights_not_normalized"],
        "model_import_probe": {
            "can_import": False,
            "reason_code": "asset.missing",
            "reason": "文件不存在",
        },
        "model_import_resource_estimate": {"over_budget": True},
    }, frame=24)
    assert [record.code for record in records] == [
        "asset.missing", "skinning.weights_not_normalized",
        "resource.budget_exceeded",
    ]
    assert all(record.node_id == "model-1" for record in records)
    assert all(record.frame == 24 for record in records)
