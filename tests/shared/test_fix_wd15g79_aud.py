"""修复验证：批 wd15-g79（AUD-21601 / AUD-31901 / AUD-33700 / AUD-33701 / AUD-33703）。

覆盖：
- AUD-21601：src/products 与 src/shared/feature_flags 双轨解析收敛为单一入口，
  两模块对同一环境必得同一结论（含 library 可禁用 / 大小写语义收敛）。
- AUD-31901：app_restart 在冻结分发（PyInstaller/py2app）形态下以可执行文件
  本体重启，而非以内嵌解释器"执行"应用。
- AUD-33700：is_enabled 对未注册但形似 AI/自动化能力的 id 失败关闭（M0 冻结），
  普通未注册 UI id 保持宽松。
- AUD-33701：产品模式预设不得静默覆盖 config 中显式设置的 AI 能力开关。
- AUD-33703：_resolve_in_dir 校验可执行权限，不可执行 bin/ffmpeg 不再遮蔽
  PATH 上的有效二进制。
"""

from __future__ import annotations

import os
import stat
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.shared import ffmpeg_locator
from src.shared.app_restart import _build_restart_command, restart_application
from src.shared.feature_flags import (
    AI_CAPABILITY_FLAGS,
    FeatureFlags,
    load_feature_flags,
)

# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

_PRODUCT_FEATURES = (
    "library",
    "color",
    "edit",
    "composite",
    "deliver",
    "teaching_animation",
)


def _clear_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除可能影响 load_feature_flags 的全部环境输入，保证用例确定性。"""
    monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
    monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
    for name in _PRODUCT_FEATURES + tuple(AI_CAPABILITY_FLAGS):
        monkeypatch.delenv(f"TOPOS_FEATURE_{name.upper()}", raising=False)


# ---------------------------------------------------------------------------
# AUD-21601：products ↔ feature_flags 双轨解析收敛
# ---------------------------------------------------------------------------


class TestAud21601ConvergedParsing:
    """get_enabled_features() 委托 load_feature_flags()：两轨必得同一结论。"""

    @pytest.mark.parametrize("mode", ["studio", "color", "edit", "composite"])
    def test_short_mode_agrees_with_runtime_flags(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        from src.products import get_enabled_features

        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", mode)

        flags = load_feature_flags(None)
        products_set = get_enabled_features()
        for name in _PRODUCT_FEATURES:
            assert (name in products_set) == flags.is_enabled(name), (
                f"mode={mode}: products 与 feature_flags 对 {name} 结论分叉"
            )

    @pytest.mark.parametrize("mode", ["topos-studio", "topos-color", "topos-edit", "topos-composite"])
    def test_full_product_name_agrees_with_runtime_flags(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        from src.products import get_enabled_features

        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", mode)

        flags = load_feature_flags(None)
        products_set = get_enabled_features()
        for name in _PRODUCT_FEATURES:
            assert (name in products_set) == flags.is_enabled(name)

    def test_library_now_disableable_like_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧 products 轨把 library 钉死为 ALWAYS_ENABLED，与运行时门控分叉；
        收敛后 library 与 load_feature_flags 一致，可被 TOPOS_DISABLE_FEATURES
        禁用（color 仍恒启用）。"""
        from src.products import ALWAYS_ENABLED, get_enabled_features

        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "library,color")

        products_set = get_enabled_features()
        assert "library" not in products_set
        assert "color" in products_set  # 运行时语义：color 不可禁用
        # ALWAYS_ENABLED 常量仅描述预设表（resolved_enabled），不再参与
        # 环境变量解析。
        assert "library" in ALWAYS_ENABLED

    def test_disable_features_case_semantics_converged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧 products 轨对 TOPOS_DISABLE_FEATURES 逐项 lower()，feature_flags
        不做小写化——收敛后以 feature_flags 语义为准（大写名不生效）。"""
        from src.products import get_enabled_features

        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "EDIT")

        products_set = get_enabled_features()
        flags = load_feature_flags(None)
        assert "edit" in products_set
        assert flags.edit is True
        assert products_set == get_enabled_features()

    def test_preset_projection_color_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.products import get_enabled_features

        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "topos-color")

        assert get_enabled_features() == frozenset(
            {"library", "color", "deliver"}
        )


# ---------------------------------------------------------------------------
# AUD-31901：冻结分发形态的重启命令
# ---------------------------------------------------------------------------


class TestAud31901FrozenRestart:
    def _stub_qt(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        """以无 GUI 依赖的桩替换 PyQt6.QtWidgets（restart 末尾会取 instance）。"""
        fake_app = MagicMock()
        qt_stub = types.SimpleNamespace(QApplication=MagicMock(instance=lambda: fake_app))
        monkeypatch.setitem(sys.modules, "PyQt6.QtWidgets", qt_stub)
        return fake_app

    def test_frozen_restarts_executable_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(
            sys, "executable", "/opt/Topos Color.app/Contents/MacOS/Topos Color"
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["/opt/Topos Color.app/Contents/MacOS/Topos Color", "--project", "x.topos"],
        )

        args, work_dir = _build_restart_command()

        # 冻结形态：直接重启可执行文件本体，绝不把 sys.argv[0] 再作为
        # 脚本参数传给内嵌解释器。W01：产品侧经 os.path 规范化，win32
        # 下 "/opt/..." 被解析为当前盘绝对路径（C:\opt\...）——期望值
        # 用同一规范化函数构造，不写死 POSIX 字面量。
        assert args == [
            os.path.abspath(
                "/opt/Topos Color.app/Contents/MacOS/Topos Color"),
            "--project",
            "x.topos",
        ]
        assert work_dir == os.path.abspath(
            "/opt/Topos Color.app/Contents/MacOS")

    def test_source_mode_keeps_interpreter_plus_script(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
        monkeypatch.setattr(sys, "argv", ["src/main.py", "--debug"])
        cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            args, work_dir = _build_restart_command()
            # 期望值须在同一 cwd 下计算（macOS 的 /tmp 解析为 /private/tmp）。
            script = os.path.abspath("src/main.py")
        finally:
            os.chdir(cwd)

        assert args == ["/usr/bin/python3", script, "--debug"]
        assert work_dir == os.path.dirname(script)

    def test_restart_application_uses_frozen_command_and_quits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared import app_restart

        fake_app = self._stub_qt(monkeypatch)
        popen = MagicMock()
        monkeypatch.setattr(app_restart.subprocess, "Popen", popen)
        # AUD-31903 修复删除了固定 0.3s 错峰延时，模块不再导入 time
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", "/opt/topos/Topos.exe")
        monkeypatch.setattr(sys, "argv", ["/opt/topos/Topos.exe"])

        restart_application()

        (pos_args, kwargs) = popen.call_args
        assert list(pos_args[0]) == [os.path.abspath("/opt/topos/Topos.exe")]
        assert kwargs["cwd"] == os.path.abspath("/opt/topos")
        fake_app.quit.assert_called_once()


# ---------------------------------------------------------------------------
# AUD-33700：is_enabled 对未知能力 id 失败关闭（M0 冻结）
# ---------------------------------------------------------------------------


class TestAud33700FailClosedForCapabilityIds:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "media_analysys",   # 拼写错误（证据中的实例）
            "ai_assistants",    # 复数形
            "cloud_analyse",    # 英式拼写
            "mcp_tools",
            "generation_v2",
            "agent_mode",
            "auto_run",
        ],
    )
    def test_capability_flavored_unknown_id_fails_closed(self, bad_id: str) -> None:
        flags = FeatureFlags()
        assert flags.is_enabled(bad_id) is False
        assert flags.is_available(bad_id) is False  # 别名继承同一语义

    def test_capability_flavored_unknown_id_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="src.shared.feature_flags"):
            FeatureFlags().is_enabled("media_analysys")
        assert any(
            "fail-closed" in rec.message and "media_analysys" in rec.message
            for rec in caplog.records
        )

    def test_plain_unknown_id_stays_permissive(self) -> None:
        """普通未注册 UI id（如 host_help_sources 探测的页面首段前缀）
        保持宽松 True——既有锁定行为（unknown_feature → True）不受影响。"""
        flags = FeatureFlags()
        assert flags.is_enabled("unknown_feature") is True
        assert flags.is_enabled("workspace") is True
        assert flags.is_enabled("app") is True

    def test_registered_ai_flag_returns_real_value(self) -> None:
        assert FeatureFlags().is_enabled("generation") is False
        assert FeatureFlags(generation=True).is_enabled("generation") is True

    def test_stems_cover_all_ai_and_commercial_flags(self) -> None:
        from src.shared.feature_flags import (
            COMMERCIAL_AUTOMATION_FLAGS,
            _CAPABILITY_ID_STEMS,
        )

        for name in AI_CAPABILITY_FLAGS + COMMERCIAL_AUTOMATION_FLAGS:
            assert name.split("_", 1)[0] in _CAPABILITY_ID_STEMS


# ---------------------------------------------------------------------------
# AUD-33701：预设不覆盖 config 显式设置的 AI 能力开关
# ---------------------------------------------------------------------------


class TestAud33701PresetKeepsConfiguredAiFlags:
    def test_config_enabled_ai_flag_survives_preset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        config = {"features": {"ai_assistant": True}}

        flags = load_feature_flags(config)
        assert flags.ai_assistant is True  # 模块契约：显式 config 条目可开启
        assert flags.media_analysis is False  # 未显式设置的仍按预设 False

    def test_config_explicit_false_kept_when_preset_also_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        config = {"features": {"generation": False}}

        flags = load_feature_flags(config)
        assert flags.generation is False

    def test_preset_still_applies_when_config_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
        config = {"features": {"edit": True}}

        flags = load_feature_flags(config)
        assert flags.ai_assistant is False

    def test_preset_still_overrides_config_for_regular_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """常规产品模块 flag（edit 等）保持既有的 preset > config 优先级。"""
        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
        config = {"features": {"edit": True}}

        flags = load_feature_flags(config)
        assert flags.edit is False

    def test_env_var_still_highest_priority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_flag_env(monkeypatch)
        monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")

        monkeypatch.setenv("TOPOS_FEATURE_AI_ASSISTANT", "false")
        config_on = {"features": {"ai_assistant": True}}
        assert load_feature_flags(config_on).ai_assistant is False

        monkeypatch.setenv("TOPOS_FEATURE_AI_ASSISTANT", "true")
        config_off = {"features": {"ai_assistant": False}}
        assert load_feature_flags(config_off).ai_assistant is True


# ---------------------------------------------------------------------------
# AUD-33703：可执行权限检查
# ---------------------------------------------------------------------------


def _make_binary(directory: Path, exe: str, executable: bool) -> Path:
    path = directory / exe
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    mode = stat.S_IRUSR | stat.S_IWUSR
    if executable:
        mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    path.chmod(mode)
    return path


class TestAud33703ExecutablePermissionCheck:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ffmpeg_locator._ENV_HOME, raising=False)
        ffmpeg_locator.clear_cache()
        yield
        ffmpeg_locator.clear_cache()

    def test_non_executable_candidate_does_not_shadow_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        bundled = tmp_path / "bin"
        bundled.mkdir()
        _make_binary(bundled, exe, executable=False)
        monkeypatch.setenv(ffmpeg_locator._ENV_HOME, str(bundled))
        monkeypatch.setattr(
            ffmpeg_locator.shutil, "which", lambda name: f"/usr/local/bin/{name}"
        )

        if sys.platform == "win32":
            # W01：Windows 无执行位语义（os.access(X_OK) 对存在文件恒
            # 真），bundled 候选直接命中并遮蔽 PATH——与 POSIX 的执行位
            # 守卫语义不同，锁定各自的正确行为而非移植断言。
            assert ffmpeg_locator.find_ffmpeg() == str(bundled / exe)
        else:
            assert ffmpeg_locator.find_ffmpeg() == f"/usr/local/bin/{exe}"

    def test_executable_candidate_wins_over_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        bundled = tmp_path / "bin"
        bundled.mkdir()
        good = _make_binary(bundled, exe, executable=True)
        monkeypatch.setenv(ffmpeg_locator._ENV_HOME, str(bundled))
        monkeypatch.setattr(
            ffmpeg_locator.shutil, "which", lambda name: f"/usr/local/bin/{name}"
        )

        assert ffmpeg_locator.find_ffmpeg() == str(good)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="X_OK 检查仅在非 Windows 平台生效"
    )
    def test_windows_skips_xok_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows 无执行位概念：sys.platform == win32 时跳过 os.access。"""
        bundled = tmp_path / "bin"
        bundled.mkdir()
        plain = bundled / "ffmpeg.exe"
        plain.write_bytes(b"")
        plain.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 无执行位

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ffmpeg_locator.shutil, "which", lambda _name: None)
        monkeypatch.setenv(ffmpeg_locator._ENV_HOME, str(bundled))

        assert ffmpeg_locator.find_ffmpeg() == str(plain)

    def test_resolve_in_dir_missing_file_returns_none(
        self, tmp_path: Path
    ) -> None:
        assert ffmpeg_locator._resolve_in_dir(tmp_path, "ffmpeg") is None
