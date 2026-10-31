from src.shared.control_surfaces.tangent.settings import (
    CONTROL_SURFACE_AUTO,
    CONTROL_SURFACE_ELEMENT,
    CONTROL_SURFACE_RIPPLE,
    DEFAULT_TANGENT_SENSITIVITY,
    tangent_sensitivity_from_display,
    tangent_sensitivity_to_display,
    TANGENT_SENSITIVITY_MAX,
    normalize_control_surface,
)
from src.shared.config.config_manager import ConfigManager


def test_control_surface_values_are_normalized() -> None:
    assert normalize_control_surface(CONTROL_SURFACE_AUTO) == CONTROL_SURFACE_AUTO
    assert normalize_control_surface(CONTROL_SURFACE_RIPPLE) == CONTROL_SURFACE_RIPPLE
    assert normalize_control_surface(CONTROL_SURFACE_ELEMENT) == CONTROL_SURFACE_ELEMENT
    assert normalize_control_surface("Element-Tk") == CONTROL_SURFACE_AUTO
    assert normalize_control_surface(None) == CONTROL_SURFACE_AUTO


def test_trackerball_defaults_use_internal_1000_percent_scale() -> None:
    assert DEFAULT_TANGENT_SENSITIVITY["tangent.lift_hue_sensitivity"] == 1000
    assert DEFAULT_TANGENT_SENSITIVITY["tangent.lift_saturation_sensitivity"] == 1000
    assert DEFAULT_TANGENT_SENSITIVITY["tangent.lift_brightness_sensitivity"] == 100
    assert TANGENT_SENSITIVITY_MAX >= 1000


def test_tangent_sensitivity_ui_scale_maps_100_to_internal_1000() -> None:
    assert tangent_sensitivity_to_display(1000) == 100.0
    assert tangent_sensitivity_from_display(100) == 1000


def test_old_tangent_defaults_are_migrated_but_custom_values_are_kept(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"tangent.lift_hue_sensitivity": 300, '
        '"tangent.trackerball_smoothing": 45, '
        '"tangent.lift_saturation_sensitivity": 275}',
        encoding="utf-8",
    )

    manager = ConfigManager(config_path)

    assert manager.get("tangent.lift_hue_sensitivity") == 1000
    assert manager.get("tangent.trackerball_smoothing") == 0
    assert manager.get("tangent.lift_saturation_sensitivity") == 275
    assert manager.get("tangent.settings_version") == 5


def test_version_two_trackerball_defaults_are_migrated(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"tangent.settings_version": 2, '
        '"tangent.lift_hue_sensitivity": 600, '
        '"tangent.lift_saturation_sensitivity": 600, '
        '"tangent.trackerball_smoothing": 0}',
        encoding="utf-8",
    )

    manager = ConfigManager(config_path)

    assert manager.get("tangent.lift_hue_sensitivity") == 1000
    assert manager.get("tangent.lift_saturation_sensitivity") == 1000
    assert manager.get("tangent.trackerball_smoothing") == 0
    assert manager.get("tangent.settings_version") == 5


def test_version_three_trackerball_defaults_are_migrated(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"tangent.settings_version": 3, '
        '"tangent.lift_hue_sensitivity": 2400, '
        '"tangent.lift_saturation_sensitivity": 2400}',
        encoding="utf-8",
    )

    manager = ConfigManager(config_path)

    assert manager.get("tangent.lift_hue_sensitivity") == 1000
    assert manager.get("tangent.lift_saturation_sensitivity") == 1000
    assert manager.get("tangent.settings_version") == 5


def test_legacy_wheel_angle_is_migrated_once_but_new_calibration_is_kept(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"tangent.settings_version": 4, '
                    '"tangent.color_wheel_rotation_degrees": 75.0, '
                    '"tangent.lift_hue_sensitivity": 300}', encoding="utf-8")
    manager = ConfigManager(path)
    assert manager.get("tangent.color_wheel_rotation_degrees") == 120.0
    assert manager.get("tangent.lift_hue_sensitivity") == 300
    assert ConfigManager(path).get("tangent.color_wheel_rotation_degrees") == 120.0
    manager.set("tangent.color_wheel_rotation_degrees", 75.0)
    assert ConfigManager(path).get("tangent.color_wheel_rotation_degrees") == 75.0


def test_custom_wheel_angle_is_not_migrated(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"tangent.settings_version": 4, '
                    '"tangent.color_wheel_rotation_degrees": 90.0}', encoding="utf-8")
    assert ConfigManager(path).get("tangent.color_wheel_rotation_degrees") == 90.0
