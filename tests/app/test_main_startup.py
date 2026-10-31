"""src/main.py 启动引导专测（P5-T1）。

不启动真实 Qt 事件循环：MainWindow/后续装配经 sys.modules stub 替换，
QApplication 经 monkeypatch 替换。覆盖：CLI 参数解析、--project 校验、
--product 环境变量、--test-startup 退出路径、早期致命错退出路径、
RTLD_LOCAL 符号隔离、_orderly_shutdown 行为、--help 退出码。
"""

from __future__ import annotations

import os
import sys
import types
from unittest import mock

import pytest

# conftest 已建会话级 QApplication；src.main 顶层的 Qt/WebEngine 导入在其后安全。
import src.main as main_mod


@pytest.fixture()
def shutdown_recorder(monkeypatch):
    """把 _orderly_shutdown 换成记录器，避免 os._exit 杀掉测试进程。

    真实 _orderly_shutdown 经 os._exit 终止、永不返回；记录后抛
    SystemExit(code) 保持同一语义（SystemExit 不被 main 的 except Exception
    捕获），使 main() 在该点之后的代码不可达——与生产行为一致。
    """
    calls: list[int] = []
    def _record(app, code):
        calls.append(code)
        raise SystemExit(code)
    monkeypatch.setattr(main_mod, "_orderly_shutdown", _record)
    return calls


@pytest.fixture()
def qt_stub(monkeypatch):
    """替换 QApplication/MainWindow，隔离真实装配与事件循环。"""
    app_instance = mock.MagicMock(name="QApplication")
    app_instance.exec.return_value = 0
    monkeypatch.setattr(main_mod, "QApplication", mock.MagicMock(return_value=app_instance))

    window = mock.MagicMock(name="MainWindow")
    fake_mw_mod = types.ModuleType("src.app.main_window")
    fake_mw_mod.MainWindow = mock.MagicMock(return_value=window)
    monkeypatch.setitem(sys.modules, "src.app.main_window", fake_mw_mod)
    return {"app": app_instance, "window": window}


def _run_main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["topos", *argv])
    with pytest.raises(SystemExit) as ei:
        main_mod.main()
    return ei.value.code


# ---------------------------------------------------------------------------
# 模块导入副作用（环境变量 / dlopen 隔离）
# ---------------------------------------------------------------------------


def test_import_sets_opencv_openexr_env():
    # OpenEXR 支持必须在 cv2 导入前开启；作为模块导入副作用存在。
    assert os.environ.get("OPENCV_IO_ENABLE_OPENEXR") == "1"


def test_import_defaults_local_config_path(monkeypatch):
    # 非打包运行必须把 TOPOS_CONFIG_PATH 落在项目内，避免共享全局配置。
    # 以 runpy 重执行模块体来验证导入时副作用（run_name 非 __main__ 不触发 main()）。
    import runpy
    monkeypatch.delenv("TOPOS_CONFIG_PATH", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    runpy.run_module("src.main", run_name="src.main_envcheck", alter_sys=False)
    assert os.environ.get("TOPOS_CONFIG_PATH") == str(main_mod.PROJECT_ROOT / ".topos" / "config.json")


def test_dlopen_isolation_idempotent_and_local():
    # darwin 上重复调用应幂等，且结果保持 RTLD_LOCAL；非 darwin 为 no-op。
    before = sys.getdlopenflags()
    main_mod._isolate_macos_native_extension_symbols()
    after = sys.getdlopenflags()
    if sys.platform == "darwin":
        assert after & os.RTLD_LOCAL
        main_mod._isolate_macos_native_extension_symbols()
        assert sys.getdlopenflags() == after
    else:
        assert after == before


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------


def test_help_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["topos", "--help"])
    with pytest.raises(SystemExit) as ei:
        main_mod.main()
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--product" in out and "--test-startup" in out


def test_unknown_args_are_ignored(monkeypatch, qt_stub, shutdown_recorder):
    rc = _run_main(monkeypatch, "--unknown-flag", "value")
    # 正常路径 exec 返回 0 → 有序关停退出码 0
    assert shutdown_recorder == [0]
    assert rc == 0


def test_product_arg_sets_env_once(monkeypatch, qt_stub, shutdown_recorder):
    monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
    _run_main(monkeypatch, "--product", "edit")
    assert os.environ["TOPOS_PRODUCT_MODE"] == "edit"
    assert os.environ["QSG_RHI_BACKEND"] == "opengl"
    monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)


def test_product_arg_does_not_override_existing_env(monkeypatch, qt_stub, shutdown_recorder, caplog):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "color")
    _run_main(monkeypatch, "--product", "edit")
    assert os.environ["TOPOS_PRODUCT_MODE"] == "color"


def test_invalid_project_path_logs_error(monkeypatch, qt_stub, shutdown_recorder, caplog, tmp_path):
    bad = str(tmp_path / "missing.topos")
    with caplog.at_level("ERROR", logger="src.main"):
        _run_main(monkeypatch, "--project", bad)
    assert any("not an existing .topos file" in r.message for r in caplog.records)
    assert shutdown_recorder == [0]


# ---------------------------------------------------------------------------
# 退出路径
# ---------------------------------------------------------------------------


def test_test_startup_exits_zero(monkeypatch, qt_stub, shutdown_recorder):
    _run_main(monkeypatch, "--test-startup")
    assert shutdown_recorder == [0]
    qt_stub["window"].show.assert_not_called()


def test_fatal_startup_error_exits_one(monkeypatch, shutdown_recorder, caplog):
    # QApplication 仍真实可用（异常发生在装配阶段之前/之后均应收敛到退出码 1）
    app_instance = mock.MagicMock(name="QApplication")
    monkeypatch.setattr(main_mod, "QApplication", mock.MagicMock(return_value=app_instance))
    boom = types.ModuleType("src.app.main_window")
    boom.MainWindow = mock.MagicMock(side_effect=RuntimeError("boom-during-assembly"))
    monkeypatch.setitem(sys.modules, "src.app.main_window", boom)
    monkeypatch.setattr(sys, "argv", ["topos"])
    with caplog.at_level("ERROR", logger="src.main"), pytest.raises(SystemExit) as ei:
        main_mod.main()
    assert shutdown_recorder == [1]
    assert ei.value.code == 1
    assert any("FATAL ERROR" in r.message for r in caplog.records)


def test_orderly_shutdown_exits_with_code_and_flushes(monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    main_mod._orderly_shutdown(None, 7)
    assert exits == [7]


def test_orderly_shutdown_tolerates_worker_errors(monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    # file_explorer 不可导入时必须吞掉并仍走到 os._exit
    monkeypatch.setitem(sys.modules, "file_explorer.main", None)  # 触发导入失败
    main_mod._orderly_shutdown(None, 3)
    assert exits == [3]
