"""M14.3 · 工程 sample-rate 字段迁移回归。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project_sample_rate import (
    DEFAULT_PROJECT_SAMPLE_RATE,
    migrate_project_sample_rate,
    project_sample_rate,
)


class TestMigrate:
    def test_missing_key_gets_default_idempotent(self):
        out, migrated = migrate_project_sample_rate({"name": "p"})
        assert out["project_sample_rate"] == 48000
        assert migrated is False  # 缺键补默认不算迁移（幂等）
        out2, m2 = migrate_project_sample_rate(out)
        assert m2 is False and out2 == out

    def test_invalid_value_reset(self):
        out, migrated = migrate_project_sample_rate(
            {"project_sample_rate": -1})
        assert out["project_sample_rate"] == 48000
        assert migrated is True

    def test_valid_value_preserved(self):
        out, migrated = migrate_project_sample_rate(
            {"project_sample_rate": 44100})
        assert out["project_sample_rate"] == 44100
        assert migrated is False

    def test_unknown_keys_kept(self):
        out, _ = migrate_project_sample_rate({"future_key": {"x": 1}})
        assert out["future_key"] == {"x": 1}


class TestRuntimeAccessor:
    def test_missing_attr_default(self):
        assert project_sample_rate(NS()) == 48000

    def test_valid_attr(self):
        assert project_sample_rate(NS(project_sample_rate=44100)) == 44100

    def test_invalid_attr_default(self):
        assert project_sample_rate(NS(project_sample_rate="bad")) == 48000


# ================================================================
# M14.3b · 字段接 EditProject：JSON 序列化 + 工程包转换器往返
# ================================================================

class TestFieldWiringRoundTrip:
    @staticmethod
    def _project(rate: int = 0):
        from src.features.edit.core.project import EditProject
        return EditProject(
            project_id="p-sr",
            name="SampleRate",
            assets={},
            bins={},
            timelines={},
            project_sample_rate=rate,
        )

    def test_json_roundtrip_preserves_44100(self):
        from src.features.edit.core.serialization import EditProjectSerializer
        restored = EditProjectSerializer.from_json(
            EditProjectSerializer.to_json(self._project(44100)))
        assert restored.project_sample_rate == 44100
        assert project_sample_rate(restored) == 44100

    def test_old_json_without_key_stays_unset(self):
        """旧工程 JSON 无键 → 字段 0（未设置）→ 访问器解析 48000。"""
        import json as _json
        from src.features.edit.core.serialization import EditProjectSerializer
        payload = _json.loads(
            EditProjectSerializer.to_json(self._project(44100)))
        del payload["project"]["project_sample_rate"]
        restored = EditProjectSerializer.from_json(_json.dumps(payload))
        assert restored.project_sample_rate == 0
        assert project_sample_rate(restored) == DEFAULT_PROJECT_SAMPLE_RATE

    def test_package_converter_roundtrip_44100(self):
        from src.features.edit.core.edit_project_io import (
            convert_edit_project_to_topos_project,
            convert_topos_project_to_edit_project,
        )
        topos = convert_edit_project_to_topos_project(
            self._project(44100), force_project_mode="edit_project")
        assert topos.settings.get("project_sample_rate") == 44100
        back = convert_topos_project_to_edit_project(topos, full_data={})
        assert back.project_sample_rate == 44100
        assert project_sample_rate(back) == 44100

    def test_package_converter_old_project_unset(self):
        from src.features.edit.core.edit_project_io import (
            convert_edit_project_to_topos_project,
            convert_topos_project_to_edit_project,
        )
        topos = convert_edit_project_to_topos_project(
            self._project(0), force_project_mode="edit_project")
        assert topos.settings.get("project_sample_rate") == 0
        back = convert_topos_project_to_edit_project(topos, full_data={})
        assert back.project_sample_rate == 0
        assert project_sample_rate(back) == DEFAULT_PROJECT_SAMPLE_RATE
