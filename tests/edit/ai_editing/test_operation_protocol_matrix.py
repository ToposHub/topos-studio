"""T-P2-20: 全域操作 schema 矩阵测试.

固定三件事：
1. 字段完整性 —— 每个操作契约 12 个必备字段全部存在且非空（写操作
   必须声明 error_codes）；
2. 域覆盖 —— 卡点要求的 7 个域各有条目；
3. 与现实一致 —— apply 指向的入口真实存在：Edit 域 id 与
   capability manifest 的 operations 同源、Composite 域 id 与
   edit_service NODE_OPS 同源、权限值与 ToolPermission 词汇一致。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.operation_protocol import (
    DOMAINS, REQUIRED_FIELDS, OperationContract, get_operation,
    operation_registry, operations_by_domain, registry_to_json,
)
from src.features.edit.ai_editing.tool_facade import ToolPermission


def test_every_contract_has_all_required_fields():
    for op in operation_registry():
        data = op.to_dict()
        missing = [f for f in REQUIRED_FIELDS if f not in data]
        assert not missing, f"{op.operation_id} 缺字段 {missing}"
        # 关键叙事字段必须非空
        for f in ("operation_id", "domain", "permission", "scope",
                  "idempotency_key", "apply", "verify",
                  "undo_or_compensation"):
            assert str(data[f]).strip(), f"{op.operation_id}.{f} 为空"
        if op.permission in ("write", "file_write", "expensive"):
            assert op.error_codes, f"写操作 {op.operation_id} 未声明 error_codes"


def test_idempotency_keys_are_named_templates():
    for op in operation_registry():
        assert "{" in op.idempotency_key and "}" in op.idempotency_key, (
            f"{op.operation_id} 的 idempotency_key 不是命名模板: "
            f"{op.idempotency_key}")


def test_all_seven_domains_covered():
    for domain in DOMAINS:
        ops = operations_by_domain(domain)
        assert ops, f"域 {domain} 无操作条目"
        assert len(ops) >= 3, f"域 {domain} 覆盖不足（{len(ops)} 条）"


def test_permissions_use_toolpermission_vocabulary():
    valid = {p.value for p in ToolPermission}
    for op in operation_registry():
        assert op.permission in valid, (
            f"{op.operation_id} 权限 {op.permission!r} 不在 ToolPermission 词汇")


def test_edit_domain_covers_manifest_operations():
    """Edit 域条目与 capability manifest.operations（OperationKind 真实
    词汇）同源——manifest 里每个可编译操作都有对应契约。"""
    from src.features.edit.ai_editing.capability_manifest import build_manifest

    manifest_ops = set(build_manifest().operations)
    registry_ids = {o.operation_id for o in operations_by_domain("Edit")}
    for name in sorted(manifest_ops):
        assert f"edit.{name}" in registry_ids, (
            f"manifest 操作 {name} 缺 edit.{name} 契约")


def test_composite_domain_covers_node_ops():
    """Composite 域与 edit_service.NODE_OPS（M3.1 单一真相）同源。"""
    from src.features.composite import edit_service as composite_edit_service

    node_ops = set(composite_edit_service.NODE_OPS)
    registry_ids = {o.operation_id for o in operations_by_domain("Composite")}
    for name in sorted(node_ops):
        assert f"composite.{name}" in registry_ids, (
            f"NODE_OP {name} 缺 composite.{name} 契约")


def test_deliver_domain_references_real_tools():
    ids = {o.operation_id for o in operations_by_domain("Deliver")}
    assert {"deliver.request", "deliver.cancel", "deliver.status",
            "deliver.result_verify"} <= ids
    request = get_operation("deliver.request")
    assert "topos.request_export" in request.apply
    assert "cancel_job" in get_operation("deliver.cancel").apply


def test_irreversible_export_requires_scoped_authorization_note():
    """不可逆导出的 undo 路径必须写明特定授权+输出范围（卡步骤 4 语义）。"""
    request = get_operation("deliver.request")
    assert "不可逆" in request.undo_or_compensation
    assert "授权" in request.undo_or_compensation


def test_registry_json_is_machine_readable():
    payload = registry_to_json()
    assert payload["protocol"] == "topos.operation/1.0"
    assert payload["required_fields"] == list(REQUIRED_FIELDS)
    assert len(payload["operations"]) == len(operation_registry())
    json.dumps(payload)  # 审计消费同形状，必须可序列化


def test_get_operation_lookup():
    assert get_operation("edit.add_clip") is not None
    assert get_operation("no.such.op") is None


def test_contract_is_frozen():
    op = get_operation("edit.add_clip")
    with pytest.raises(Exception):
        op.operation_id = "tampered"  # type: ignore[misc]
