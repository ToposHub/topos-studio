"""B6: 用户自定义导出预设持久化测试。

覆盖：
- 存/读/改名/删除往返（Presets/User/export/*.json）
- 名称查重（save 与 rename 双向）
- 坏 JSON 容错跳过
- 队列 job 与预设快照解耦（build_job_from_preset 深拷贝语义）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.deliver.core import custom_export_presets as cep  # noqa: E402
from src.features.deliver.core.video_export_job import (  # noqa: E402
    AudioConfig,
    SubtitleConfig,
    SubtitleMode,
    VideoConfig,
)
from src.features.deliver.core.video_export_presets import (  # noqa: E402
    build_job_from_preset,
)


@pytest.fixture(autouse=True)
def preset_store(tmp_path: Path, monkeypatch) -> Path:
    """把 Presets 根目录重定向到临时目录（避免污染真实用户预设）。"""
    monkeypatch.chdir(tmp_path)
    return Path(cep.user_export_presets_dir())


def _video_cfg(**overrides) -> VideoConfig:
    fields = dict(
        container="mp4",
        codec="h264",
        profile="high",
        resolution=(1920, 1080),
        fps=None,
        quality_mode="vbr",
        quality_value=12_000_000,
    )
    fields.update(overrides)
    return VideoConfig(**fields)


class TestUserPresetCrud:
    def test_save_and_list_roundtrip(self) -> None:
        saved = cep.save_user_preset(
            "My Film Master",
            _video_cfg(container="mov", codec="prores_422"),
            AudioConfig(codec="pcm_s24le", bitrate=2304),
            SubtitleConfig(mode=SubtitleMode.OFF),
        )
        listed = cep.list_user_presets()
        assert [p.preset_id for p in listed] == [saved.preset_id]
        preset = listed[0]
        assert preset.fallback_title == "My Film Master"
        assert preset.video_config.codec == "prores_422"
        assert preset.video_config.container == "mov"
        assert preset.audio_config.codec == "pcm_s24le"
        assert preset.preset_id.startswith("user_")

    def test_persisted_json_marks_custom(self, preset_store: Path) -> None:
        cep.save_user_preset("Custom X", _video_cfg())
        files = list(preset_store.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["is_custom"] is True
        assert data["name"] == "Custom X"
        assert data["created_at"] > 0

    def test_duplicate_name_rejected(self) -> None:
        cep.save_user_preset("Dup", _video_cfg())
        with pytest.raises(ValueError, match="duplicate"):
            cep.save_user_preset("Dup", _video_cfg())

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            cep.save_user_preset("   ", _video_cfg())

    def test_rename_keeps_id_and_updates_title(self, preset_store: Path) -> None:
        saved = cep.save_user_preset("Old Name", _video_cfg())
        renamed = cep.rename_user_preset(saved.preset_id, "New Name")
        assert renamed is not None
        assert renamed.preset_id == saved.preset_id
        assert renamed.fallback_title == "New Name"
        data = json.loads(
            (preset_store / f"{saved.preset_id}.json").read_text(encoding="utf-8")
        )
        assert data["name"] == "New Name"

    def test_rename_duplicate_rejected(self) -> None:
        cep.save_user_preset("A", _video_cfg())
        saved_b = cep.save_user_preset("B", _video_cfg())
        with pytest.raises(ValueError, match="duplicate"):
            cep.rename_user_preset(saved_b.preset_id, "A")

    def test_delete_removes_file(self, preset_store: Path) -> None:
        saved = cep.save_user_preset("Doomed", _video_cfg())
        path = preset_store / f"{saved.preset_id}.json"
        assert path.exists()
        assert cep.delete_user_preset(saved.preset_id) is True
        assert not path.exists()
        assert cep.list_user_presets() == []

    def test_bad_json_skipped(self, preset_store: Path) -> None:
        cep.save_user_preset("Good", _video_cfg())
        (preset_store / "broken.json").write_text("{not json", encoding="utf-8")
        (preset_store / "nonobject.json").write_text("[1, 2]", encoding="utf-8")
        listed = cep.list_user_presets()
        assert [p.fallback_title for p in listed] == ["Good"]

    def test_no_tmp_file_left_after_save(self, preset_store: Path) -> None:
        cep.save_user_preset("Atomic", _video_cfg())
        assert not list(preset_store.glob("*.tmp"))


class TestJobSnapshotDecoupling:
    def test_job_configs_are_copies(self) -> None:
        """改预设后已建 job 的参数不变（快照语义，B6 第 3 步）。"""
        preset = cep.save_user_preset("Snap", _video_cfg(quality_value=1_000))
        job = build_job_from_preset(
            preset, project_name="p", timeline_name="t", output_dir="/tmp"
        )
        # 冻结 dataclass：模拟"预设后续修改"= 换一个新预设对象同 id
        revised = cep.rename_user_preset(preset.preset_id, "Snap2")
        assert revised is not None
        # job 快照与预设对象解耦
        assert job.video_config is not preset.video_config
        assert job.video_config.quality_value == 1_000
        assert job.preset_id == preset.preset_id
