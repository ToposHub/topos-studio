"""DESK-04B/04C · 包内容能力分级（E/P/B/X）与模块覆写合并策略测试。

04B 验收：不能安全编辑的内容只读/锁定；不能带缺效果静默导出。
04C 验收：空模块显式清理与未提供模块保留都有测试（None=保留 /
空容器=清除 / 新值=覆写，三态契约统一钉住）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import zipfile

import pytest

from tests.project.package_golden import save_golden_package

from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.package_capability import (
    GRADE_E,
    GRADE_P,
    GRADE_X,
    grade_package_capabilities,
)
from src.shared.project.topos_project_io import ToposProjectIO

pytestmark = pytest.mark.edit_core


def _golden_sequences(io_package: str):
    io = EditProjectIO()
    loaded = io.load_project(io_package)
    assert loaded is not None
    project, _ui, sequences = loaded
    return io, project, sequences


def _online_assets(project, tmp_path):
    """把素材路径指到真实临时文件（排除"离线素材"维度的 X 干扰）。"""
    online = {}
    for asset_id, asset in project.assets.items():
        real = os.path.join(str(tmp_path), f"{asset_id}.bin")
        with open(real, "wb") as fh:
            fh.write(b"x" * 16)
        online[asset_id] = dataclasses.replace(
            asset, path=real, is_offline=False)
    return online


class TestGrades:
    def test_clean_golden_is_e(self, tmp_path) -> None:
        package = save_golden_package(str(tmp_path), "clean.topos")
        io, project, sequences = _golden_sequences(package)
        report = grade_package_capabilities(
            full_data={"manifest": {}, "version_status": {}},
            timeline_sequences=sequences.values(),
            assets=_online_assets(project, tmp_path),
        )
        assert report.grade == GRADE_E
        assert report.export_allowed
        for op in ("timeline_edit", "clip_trim", "subtitle_edit",
                   "color_edit", "effect_param_edit", "export"):
            ok, _ = report.operation_allowed(op)
            assert ok, op

    def test_unknown_enabled_effect_is_x_and_blocks_export(
        self, tmp_path,
    ) -> None:
        package = save_golden_package(str(tmp_path), "x.topos")
        io, project, sequences = _golden_sequences(package)

        seq = sequences["golden-timeline-main"]
        clip = seq.video_tracks[0].clips[0]
        future_effect = EffectInstance(
            effect_id="fx-future", effect_type="mobile_ai_relight_v9")
        edited_clip = dataclasses.replace(clip, effects=(future_effect,))
        edited_seq = seq.with_clip_updated(edited_clip)

        report = grade_package_capabilities(
            full_data={"manifest": {}, "version_status": {}},
            timeline_sequences=[edited_seq],
            assets=project.assets,
        )
        assert report.grade == GRADE_X
        assert not report.export_allowed
        assert report.locked_object_refs == {clip.clip_id}
        ok, reason = report.operation_allowed("export")
        assert not ok and "mobile_ai_relight_v9" in reason
        # 结构性编辑不受 X 级影响（只锁导出与该对象）
        assert report.operation_allowed("timeline_edit")[0]
        assert report.operation_allowed("effect_param_edit")[0]  # 对象级由调用方判

    def test_unknown_disabled_effect_is_p(self, tmp_path) -> None:
        package = save_golden_package(str(tmp_path), "p.topos")
        io, project, sequences = _golden_sequences(package)
        seq = sequences["golden-timeline-main"]
        clip = seq.video_tracks[0].clips[0]
        disabled = EffectInstance(
            effect_id="fx-off", effect_type="future_dof_v9", is_enabled=False)
        edited = dataclasses.replace(clip, effects=(disabled,))

        report = grade_package_capabilities(
            full_data={"manifest": {}, "version_status": {}},
            timeline_sequences=[seq.with_clip_updated(edited)],
            assets=_online_assets(project, tmp_path),
        )
        assert report.grade == GRADE_P
        assert report.export_allowed  # 不参与渲染，可导出（带告警）
        assert report.locked_object_refs == {clip.clip_id}

    def test_missing_required_feature_is_x(self, tmp_path) -> None:
        report = grade_package_capabilities(
            full_data={"manifest": {"required_features": ["ai_relight"]},
                       "version_status": {}},
            timeline_sequences=None, assets={},
            product_features={"media_pool", "color"},
        )
        assert report.grade == GRADE_X
        assert not report.export_allowed

    def test_future_package_version_is_b(self) -> None:
        report = grade_package_capabilities(
            full_data={"version_status": {
                "package": {"status": "future", "file_version": 9,
                            "current_version": 3}}},
            timeline_sequences=None, assets={},
        )
        assert report.grade == "B"
        assert report.export_allowed

    def test_offline_asset_without_resource_is_x(self, tmp_path) -> None:
        package = save_golden_package(str(tmp_path), "offline.topos")
        io, project, sequences = _golden_sequences(package)
        # 黄金素材路径本就不存在（/golden/media/...）→ X（渲染必需素材不可用）
        report = grade_package_capabilities(
            full_data={"manifest": {}, "version_status": {}},
            timeline_sequences=sequences.values(),
            assets=project.assets,
        )
        assert report.grade == GRADE_X
        assert any(f.object_kind == "source_media"
                   for f in report.export_blockers)

    def test_production_load_reports_grade(self, tmp_path) -> None:
        package = save_golden_package(str(tmp_path), "prod.topos")
        io = EditProjectIO()
        assert io.load_project(package) is not None
        assert io.get_last_capability_report() is not None
        assert io.get_last_capability_report().grade == GRADE_X  # 离线黄金素材


class TestModuleOverwriteSemantics:
    """04C：None=保留 / 空容器=清除 / 新值=覆写 三态契约。"""

    def _read(self, path, name):
        with zipfile.ZipFile(path) as zf:
            return json.loads(zf.read(name))

    def test_none_keeps_empty_clears_new_overwrites(self, tmp_path) -> None:
        from tests.project.package_golden import golden_save_kwargs

        base = save_golden_package(str(tmp_path), "base.topos")
        # 基线包含 bins（有 root/sub 两个）
        assert "golden-bin-root" in self._read(base, "edit/bins.json")

        # ① 未提供（None）→ 保留：save 不传 full_bins_data
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        kwargs.pop("full_bins_data")
        assert ToposProjectIO.save(project, base, **kwargs)
        assert "golden-bin-root" in self._read(base, "edit/bins.json")

        # ② 空容器 → 显式清除
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        kwargs["full_bins_data"] = {}
        assert ToposProjectIO.save(project, base, **kwargs)
        assert self._read(base, "edit/bins.json") == {}

        # ③ 新值 → 覆写
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        kwargs["full_bins_data"] = {
            "new-bin": {"bin_id": "new-bin", "name": "Fresh",
                        "parent_id": None, "asset_ids": [],
                        "timeline_ids": []}}
        assert ToposProjectIO.save(project, base, **kwargs)
        assert set(self._read(base, "edit/bins.json")) == {"new-bin"}

    def test_unknown_module_and_manifest_keys_survive_all_three(
        self, tmp_path,
    ) -> None:
        """三态覆写不影响未知模块/未知键（Phase B + 04A 合并恒保留）。"""
        from tests.project.package_golden import golden_save_kwargs

        path = save_golden_package(str(tmp_path), "mix.topos")
        with zipfile.ZipFile(path) as zf:
            contents = {i.filename: zf.read(i.filename)
                        for i in zf.infolist() if not i.is_dir()}
        contents["future_module/data.json"] = json.dumps(
            {"v": 9, "list": [1, 2]}).encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw in contents.items():
                zf.writestr(name, raw)

        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        kwargs["full_bins_data"] = {}  # 同时显式清空一个已知模块
        assert ToposProjectIO.save(project, path, **kwargs)

        assert json.loads(self._read_raw(path, "future_module/data.json")) == \
            {"v": 9, "list": [1, 2]}
        assert self._read(path, "edit/bins.json") == {}  # 清空生效
        # manifest 未知键仍在（D0 既有行为在此复验）
        manifest = self._read(path, "manifest.json")
        assert manifest["project_id"] == "golden-desk01a-0001"

    def _read_raw(self, path, name):
        with zipfile.ZipFile(path) as zf:
            return zf.read(name)
