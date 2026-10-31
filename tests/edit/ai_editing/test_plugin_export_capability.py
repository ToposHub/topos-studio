"""T-P2-20/W06: 可交付插件能力契约测试.

插件头部 ``// @export_support opengl`` 声明 → catalog spec 携带
``export_opengl`` → ``resolve_execution_capability`` 判定可交付
（opengl_export 轴 + CPU 回退跳过质量约束）→ capability manifest、
plan validator、Deliver preflight 三处同一真相。未声明的插件维持
preview_only。

端到端像素级参与证明由 scripts/bench_w_matrix_replay.py 的 W06 承载
（GL 合成臂 + 帧哈希差）；本文件固定数据契约。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.edit_core

from src.features.shared.effects.effect_capability import (
    resolve_execution_capability,
)
from src.features.shared.effects.effect_catalog import get_effect_catalog


def _plugin_specs():
    catalog = get_effect_catalog()
    return [s for s in catalog.get_all_specs()
            if s.effect_type.startswith("plugin:")]


def _declared_export_plugin() -> str | None:
    for spec in _plugin_specs():
        if "export_opengl" in spec.plugin_capabilities:
            return spec.effect_type
    return None


def _preview_only_plugin() -> str | None:
    for spec in _plugin_specs():
        if "export_opengl" not in spec.plugin_capabilities:
            return spec.effect_type
    return None


def test_catalog_has_one_declared_and_one_undeclared_plugin():
    declared, undeclared = _declared_export_plugin(), _preview_only_plugin()
    assert declared, "catalog 无声明 export_support 的插件（warm_tint 缺失？）"
    assert undeclared, "catalog 无未声明导出的插件"


def test_declared_plugin_resolves_exportable_via_opengl():
    etype = _declared_export_plugin()
    cap = resolve_execution_capability(etype, catalog_known=True)
    assert cap.exportable and not cap.preview_only
    assert cap.opengl_export and not cap.metal_export and not cap.cpu_export
    assert ("plugin_opengl_export_only_cpu_fallback_skips_effect"
            in cap.quality_constraints), (
        "GL-only 插件必须携带 CPU 回退跳过质量约束（诚实条款）")
    code, _msg = cap.delivery_restriction()
    assert code == ""


def test_undeclared_plugin_stays_preview_only():
    etype = _preview_only_plugin()
    cap = resolve_execution_capability(etype, catalog_known=True)
    assert not cap.exportable and cap.preview_only
    code, _msg = cap.delivery_restriction()
    assert code == "plugin_preview_only"


def test_manifest_splits_plugins_by_declaration():
    from src.features.edit.ai_editing.capability_manifest import (
        build_manifest)
    manifest = build_manifest()
    declared, undeclared = [], []
    for e in manifest.effects:
        if not e.effect_type.startswith("plugin:"):
            continue
        (declared if e.exportable else undeclared).append(e)
    assert declared, "manifest 无可交付插件"
    assert undeclared, "manifest 无 preview_only 插件"
    for e in declared:
        assert e.delivery_restriction_code == ""
    for e in undeclared:
        assert e.delivery_restriction_code == "plugin_preview_only"


def test_plan_accepts_declared_plugin_as_delivery_step():
    """声明导出的插件不再被计划期 EFFECT_NOT_DELIVERABLE 拒绝。"""
    from src.features.edit.ai_editing.capability_manifest import (
        build_manifest, is_effect_known, get_effect_capability)
    etype = _declared_export_plugin()
    m = build_manifest()
    assert is_effect_known(m, etype)
    capability = get_effect_capability(m, etype)
    assert capability is not None and capability.exportable


def test_gl_export_chain_admits_declared_plugin_spec():
    from src.features.edit.core.timeline_export import (
        _spec_allows_opengl_export)
    catalog = get_effect_catalog()
    declared = _declared_export_plugin()
    undeclared = _preview_only_plugin()
    assert _spec_allows_opengl_export(catalog.get_spec(declared))
    assert not _spec_allows_opengl_export(catalog.get_spec(undeclared))
