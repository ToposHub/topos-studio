import json
import os

from src.shared.config.config_manager import ConfigManager


def test_missing_config_is_created_from_defaults(tmp_path):
    config_path = tmp_path / "config.json"

    manager = ConfigManager(config_path=config_path)

    assert config_path.exists()
    assert manager.get("current_page") == "Color"
    assert manager.get("video_pipeline_mode") == "auto"


def test_config_manager_default_path_is_user_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPOS_CONFIG_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("platform.system", lambda: "Linux")

    path = ConfigManager.get_default_config_path()

    assert path == str(tmp_path / "xdg" / "topos" / "config.json")
    assert not path.endswith(os.path.join("topos-color02", "config.json"))


def test_config_manager_honors_a_project_local_config_path(monkeypatch, tmp_path):
    """Source launchers isolate layouts through TOPOS_CONFIG_PATH."""
    project_config = tmp_path / ".topos" / "config.json"
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(project_config))

    assert ConfigManager.get_default_config_path() == str(project_config)


def test_save_merges_dirty_keys_from_multiple_instances(tmp_path):
    config_path = tmp_path / "config.json"

    first = ConfigManager(config_path=config_path)
    second = ConfigManager(config_path=config_path)

    first.set("locale", "zh_CN")
    second.set("last_library_path", "/tmp/library")

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["locale"] == "zh_CN"
    assert saved["last_library_path"] == "/tmp/library"


def test_corrupted_config_is_backed_up_and_recreated(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{not valid json", encoding="utf-8")

    manager = ConfigManager(config_path=config_path)

    assert manager.get("current_page") == "Color"
    assert config_path.with_suffix(".json.corrupted").exists()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["current_page"] == "Color"


def test_removed_render_cache_prores_preference_migrates_to_topos(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "render_cache_codec": "prores_422_proxy",
            "render_cache_container": "mp4",
        }),
        encoding="utf-8",
    )

    manager = ConfigManager(config_path=config_path)

    assert manager.get("render_cache_codec") == "topos_proxy"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["render_cache_codec"] == "topos_proxy"
    assert "render_cache_container" not in saved
