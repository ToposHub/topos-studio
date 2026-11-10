"""SR04：应用边界解析受管 LUT 引用（layer_controller 层验收）。

gallery still 的 grade 携带 ``topos-lut://`` 受管引用（导入重写产物）；
应用（Apply Grade／分类应用）在 :meth:`_prepare_preset_layers` 边界把
受管引用换算为**当前库根**的本机路径——库目录迁移后引用仍解析；
用户自己的绝对引用原样保留（不猜）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.color.core.gallery_resource_refs import (
    managed_lut_reference,
)


def _make_controller(tmp_path: Path, monkeypatch):
    from src.app.controllers.layer_controller import LayerOpsMixin

    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    ctrl.status_bar = MagicMock()
    ctrl.engine = MagicMock()
    return ctrl


def _layer(lut_ref: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        active_lut_path=lut_ref, ai_matte=None, ai_matte_enabled=True,
        is_baked=False, baked_file_name=None, baked_texture_id=None,
        plugins=[], mask_shapes=[],
    )


class TestApplyBoundaryResolution:
    def test_managed_ref_resolved_to_current_library_root(
            self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        lut = tmp_path / "Presets" / "LUTs" / "ab12cd34_look.cube"
        lut.parent.mkdir(parents=True, exist_ok=True)
        lut.write_bytes(b"LUT")
        topos = tmp_path / "Presets" / "User" / "Look.topos"
        prepared = ctrl._prepare_preset_layers(
            [_layer(managed_lut_reference("ab12cd34_look.cube"))],
            topos_path=str(topos))
        assert prepared[0].active_lut_path == str(lut)

    def test_managed_ref_missing_resolves_to_expected_path(
            self, tmp_path, monkeypatch):
        """缺失受管引用换算到期望路径——存在性交给既有缺失处理。"""
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = tmp_path / "Presets" / "User" / "Look.topos"
        prepared = ctrl._prepare_preset_layers(
            [_layer(managed_lut_reference("deadbeef_gone.cube"))],
            topos_path=str(topos))
        assert prepared[0].active_lut_path.endswith(
            "Presets/LUTs/deadbeef_gone.cube")

    def test_user_absolute_ref_untouched(self, tmp_path, monkeypatch):
        ctrl = _make_controller(tmp_path, monkeypatch)
        topos = tmp_path / "Presets" / "User" / "Look.topos"
        prepared = ctrl._prepare_preset_layers(
            [_layer("/Users/me/my-luts/own.cube")], topos_path=str(topos))
        assert prepared[0].active_lut_path == "/Users/me/my-luts/own.cube"

    def test_no_topos_path_keeps_refs_verbatim(self, tmp_path, monkeypatch):
        """兼容入口（无来源路径）：不解析也不吞引用。"""
        ctrl = _make_controller(tmp_path, monkeypatch)
        ref = managed_lut_reference("ab12cd34_look.cube")
        prepared = ctrl._prepare_preset_layers([_layer(ref)])
        assert prepared[0].active_lut_path == ref
