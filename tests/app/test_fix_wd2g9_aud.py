"""批 D（wd2-g9）修复回归测试：app 壳层（Major×15）。

覆盖 REMEDIATION_PLAN 批 D 条目：

* AUD-3500 —— closeEvent 的保存确认先于一切后台服务停机；取消关闭时
  任何服务不被 shutdown；确认通过后 mixin 内二次确认被 bypass（不重复
  弹窗），closeEvent 返回后恢复类级方法解析。
* AUD-3501 —— UpdateChecker（QThread）在关闭路径 quit()+wait()。
* AUD-3502 —— script_engine 后台脚本线程 stop + wait（先于 engine 停机）。
* AUD-3503 —— denoise/deblur 引擎 worker 经 cancel() 停止。
* AUD-3504 —— Library ↔ Files 双向目录同步：同路径重入不 emit、emit
  期间状态锁断环。
* GAP-027 —— 以上 MainWindow closeEvent / _shutdown_playback_pipeline /
  _config_bool 用例同时为 main_window.py 低覆盖区补测。
* AUD-3600 —— 未注册顶层菜单：持有 menu_bar 时自动补建（View/Timeline
  组不再被整组丢弃）；无 menu_bar 时 warning 而非 DEBUG 静默。
* AUD-3601 —— 中间子菜单标题经 i18n（menu.<segment> → panel.<segment>
  → 原文回退）。
* AUD-3701 —— post_register_hook 抛异常时模块保持"已注册"返回 True；
  register_module 拒绝时如实返回 False。
* AUD-3801 —— paint 热路径：评分走内存缓存（无逐项 SQLite 连接）、
  is_dir 走模型节点树（无 stat）、.topos 存在性按路径缓存、缩略图重绘
  节流合并。
* AUD-3802 —— Rating 排序 lessThan 查内存缓存，切排序时单连接批量刷新。
* AUD-3803 —— 重命名迁移 .topos 边车与 file_meta 行（NFC 键），删除
  询问边车并清理 DB 行（目录按前缀）。
* AUD-3804 —— 依赖缺失时 _load_url 重试有独立上限，错误页的
  loadFinished(True) 不再重置状态/绕过退避。
* AUD-3805 —— update_metadata 缺失字段重置 "-"，空 metadata 清空面板。
* AUD-40013 —— 内置示例目录定位 src/features/scripting/examples。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.app

# 重导入统一在模块顶部（collection 阶段、QApplication 创建之前）完成：
# 会话中途 import Qt/WebEngine 相关模块会与已存活的引擎线程在 GC 时
# 相撞（实测 Fatal Python error: Aborted），conftest 已保证
# QWebEngineWidgets 先于 QApplication 导入。
import src.app.module_loader as ml  # noqa: E402
import src.app.ui.library_panel as lp  # noqa: E402
from src.app.main_window import MainWindow, _config_bool  # noqa: E402
from src.app.menu import menu_builder as mb  # noqa: E402
from src.app.ui import script_panel  # noqa: E402
from src.app.ui.files_panel import FilesPanel  # noqa: E402
from src.app.ui.info_panel import InfoPanel  # noqa: E402
from src.shared.actions import ActionRegistry, MenuActionDescriptor  # noqa: E402
from src.app.controllers.file_controller import FileOpsMixin  # noqa: E402
from src.app.menu.menu_builder import MenuBuilder  # noqa: E402
from src.app.module_loader import FeatureModuleSpec  # noqa: E402
from src.shared.db.database import Database  # noqa: E402
from src.shared.i18n.i18n import I18N  # noqa: E402
import unicodedata  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# AUD-3500/3501/3502/3503 + GAP-027：MainWindow closeEvent 停机序列
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def main_window(qapp):
    """模块级复用的轻量 MainWindow（test_mode 不建面板）。

    teardown 走完整真实 close（含 FileOpsMixin 的引擎线程停机）再
    deleteLater——只 deleteLater 不停线程时，C++ 对象析构会与存活的
    AsyncRenderer 线程相撞（GC 期 Qt abort，实测复现）。
    """
    window = MainWindow(test_mode=True)
    yield window
    from PyQt6.QtTest import QTest
    try:
        _clean_main_window(window)
        window.close()  # 真实 closeEvent + mixin 引擎停机
    except Exception:
        pass
    try:
        window.deleteLater()
    except Exception:
        pass
    QTest.qWait(50)


def _clean_main_window(mw) -> None:
    """清除上一用例挂上的实例级替身，恢复类级方法解析。"""
    for key in (
        "_shutdown_playback_pipeline", "_confirm_save_before_close",
        "_update_checker", "script_engine", "denoise_panel", "deblur_panel",
    ):
        mw.__dict__.pop(key, None)


def _isolate_mixin_close(monkeypatch) -> list:
    """把 FileOpsMixin.closeEvent 换成轻量桩（记录调用、不碰真实引擎）。

    MainWindow.closeEvent 是被测对象；mixin 内的 engine worker 停机
    属于重型集成路径，重复触发会与存活线程在 GC 时相撞（Qt abort），
    由末尾的 test_close_accept_path_runs_real_mixin_cleanup 单独覆盖一次。
    """
    from PyQt6.QtWidgets import QMainWindow

    calls: list = []

    def _stub_close(self, event):
        calls.append(self)
        QMainWindow.closeEvent(self, event)

    monkeypatch.setattr(FileOpsMixin, "closeEvent", _stub_close)
    return calls


def _reset_shutdown_state(mw) -> None:
    """恢复可重入的停机状态，并挂上可断言的假服务。"""
    _clean_main_window(mw)
    mw._pipeline_shutdown_done = False
    mw.tangent_color_bridge = SimpleNamespace(shutdown=Mock(name="tangent.shutdown"))
    mw.tracking_service = SimpleNamespace(shutdown=Mock(name="tracking.shutdown"))
    mw.files_panel = SimpleNamespace(shutdown=Mock(name="files.shutdown"))
    mw._shutdown_playback_pipeline = Mock(name="_shutdown_playback_pipeline")


def _close_event():
    from PyQt6.QtGui import QCloseEvent

    return QCloseEvent()


class _FakeThreadHandle:
    def __init__(self) -> None:
        self.quit = Mock(name="thread.quit")
        self.wait = Mock(name="thread.wait", return_value=True)


class _FakeScriptEngine:
    def __init__(self, with_thread: bool = True) -> None:
        self.stop = Mock(name="script.stop")
        self._thread = _FakeThreadHandle() if with_thread else None


def test_close_cancel_aborts_before_any_shutdown(main_window):
    """AUD-3500：确认框取消 → 不停任何服务、事件被忽略、管线可复用。"""
    mw = main_window
    _reset_shutdown_state(mw)
    mw._confirm_save_before_close = Mock(return_value=False)

    event = _close_event()
    mw.closeEvent(event)

    assert event.isAccepted() is False
    mw.tangent_color_bridge.shutdown.assert_not_called()
    mw.tracking_service.shutdown.assert_not_called()
    mw.files_panel.shutdown.assert_not_called()
    mw._shutdown_playback_pipeline.assert_not_called()
    # 取消后管线未进入不可恢复的已停机状态
    assert mw._pipeline_shutdown_done is False


def test_close_confirm_runs_before_all_shutdowns(main_window, monkeypatch):
    """AUD-3500：保存确认必须先于 Tangent/tracking/files/回放管线停机。"""
    _isolate_mixin_close(monkeypatch)
    mw = main_window
    _reset_shutdown_state(mw)
    order: list[str] = []
    mw._confirm_save_before_close = lambda: order.append("confirm") or True
    mw.tangent_color_bridge.shutdown.side_effect = lambda: order.append("tangent")
    mw.tracking_service.shutdown.side_effect = lambda: order.append("tracking")
    mw.files_panel.shutdown.side_effect = lambda: order.append("files")
    mw._shutdown_playback_pipeline.side_effect = lambda: order.append("pipeline")

    event = _close_event()
    mw.closeEvent(event)

    assert order[0] == "confirm"
    assert set(order[1:]) == {"tangent", "tracking", "files", "pipeline"}
    assert order.index("confirm") < min(
        order.index(k) for k in ("tangent", "tracking", "files", "pipeline"))


def test_close_confirmed_shuts_services_and_accepts(main_window, monkeypatch):
    """AUD-3500：确认通过 → 全部服务按序停机，事件接受。"""
    _isolate_mixin_close(monkeypatch)
    mw = main_window
    _reset_shutdown_state(mw)
    mw._confirm_save_before_close = Mock(return_value=True)

    event = _close_event()
    mw.closeEvent(event)

    mw.tangent_color_bridge.shutdown.assert_called_once()
    mw.tracking_service.shutdown.assert_called_once()
    mw.files_panel.shutdown.assert_called_once()
    mw._shutdown_playback_pipeline.assert_called_once()
    assert event.isAccepted() is True
    # 事件处理后实例级 bypass 已清除（类级方法解析恢复）
    assert "_confirm_save_before_close" not in mw.__dict__


def test_close_confirmed_bypasses_second_prompt_in_mixin(
        main_window, monkeypatch):
    """AUD-3500：首轮确认后 FileOpsMixin 内的二次确认直接放行。

    "丢弃"分支不清理脏标记——若 mixin 再问一次用户会被问两遍；
    这里以桩替换 FileOpsMixin.closeEvent 复现其首行确认逻辑。
    """
    from PyQt6.QtWidgets import QMainWindow

    mw = main_window
    _reset_shutdown_state(mw)
    second_confirm_results: list[bool] = []

    def _fake_mixin_close(self, event):
        second_confirm_results.append(bool(self._confirm_save_before_close()))
        QMainWindow.closeEvent(self, event)

    monkeypatch.setattr(FileOpsMixin, "closeEvent", _fake_mixin_close)
    first_confirm = Mock(return_value=True)  # 模拟"丢弃/保存/无变更"
    mw._confirm_save_before_close = first_confirm

    event = _close_event()
    mw.closeEvent(event)

    first_confirm.assert_called_once()      # 用户只被问一次
    assert second_confirm_results == [True]  # mixin 二次确认被 bypass


def test_close_stops_update_checker_thread(main_window, monkeypatch):
    """AUD-3501：UpdateChecker QThread 在关闭路径 quit()+wait()。"""
    _isolate_mixin_close(monkeypatch)
    mw = main_window
    _reset_shutdown_state(mw)
    mw._confirm_save_before_close = Mock(return_value=True)
    checker = _FakeThreadHandle()
    mw._update_checker = checker

    mw.closeEvent(_close_event())

    checker.quit.assert_called_once()
    checker.wait.assert_called_once_with(10500)


def test_close_stops_script_engine_before_engine_workers(main_window, monkeypatch):
    """AUD-3502：script_engine.stop() + 脚本线程 wait（bounded）。"""
    _isolate_mixin_close(monkeypatch)
    mw = main_window
    _reset_shutdown_state(mw)
    mw._confirm_save_before_close = Mock(return_value=True)
    engine = _FakeScriptEngine(with_thread=True)
    mw.script_engine = engine

    mw.closeEvent(_close_event())

    engine.stop.assert_called_once()
    engine._thread.wait.assert_called_once_with(3000)


def test_close_script_engine_without_thread_handle_is_safe(main_window, monkeypatch):
    """AUD-3502：无 _thread 属性（引擎从未执行脚本）时不抛异常。"""
    _isolate_mixin_close(monkeypatch)
    mw = main_window
    _reset_shutdown_state(mw)
    mw._confirm_save_before_close = Mock(return_value=True)
    engine = _FakeScriptEngine(with_thread=False)
    mw.script_engine = engine

    event = _close_event()
    mw.closeEvent(event)

    engine.stop.assert_called_once()
    assert event.isAccepted() is True


def test_close_cancels_denoise_and_deblur_workers(main_window, monkeypatch):
    """AUD-3503：denoise/deblur 引擎 worker 经 cancel() 协作停止。"""
    _isolate_mixin_close(monkeypatch)
    mw = main_window
    _reset_shutdown_state(mw)
    mw._confirm_save_before_close = Mock(return_value=True)
    denoise_cancel = Mock(name="denoise.cancel")
    deblur_cancel = Mock(name="deblur.cancel")
    mw.denoise_panel = SimpleNamespace(engine=SimpleNamespace(cancel=denoise_cancel))
    mw.deblur_panel = SimpleNamespace(engine=SimpleNamespace(cancel=deblur_cancel))

    mw.closeEvent(_close_event())

    denoise_cancel.assert_called_once()
    deblur_cancel.assert_called_once()


def test_shutdown_playback_pipeline_idempotent(main_window):
    """GAP-027 补测：_pipeline_shutdown_done 幂等（重复调用零副作用）。"""
    mw = main_window
    _clean_main_window(mw)
    mw._pipeline_shutdown_done = False
    pause = Mock()
    mw.timeline_panel = SimpleNamespace(
        get_playback_controller=lambda: SimpleNamespace(is_playing=True, pause=pause))
    audio_shutdown = Mock()
    mw.edit_page = SimpleNamespace(shutdown_audio_playback=audio_shutdown)
    stop_playback = Mock()
    mw.video_controller = SimpleNamespace(stop_playback=stop_playback)
    preview_shutdown = Mock()
    mw.timeline_preview_renderer = SimpleNamespace(shutdown=preview_shutdown)

    mw._shutdown_playback_pipeline()
    mw._shutdown_playback_pipeline()  # 第二次必须整体跳过

    pause.assert_called_once()
    audio_shutdown.assert_called_once()
    stop_playback.assert_called_once()
    preview_shutdown.assert_called_once()
    assert mw._pipeline_shutdown_done is True
    # 渲染器停止后引用置空（第 4 步语义）
    assert mw.timeline_preview_renderer is None


def test_config_bool_distinguishes_none_and_false():
    """GAP-027 补测：_config_bool 显式 False 不落默认值。"""

    assert _config_bool(None, True) is True
    assert _config_bool(False, True) is False
    assert _config_bool(True, False) is True
    assert _config_bool(0, True) is False
    assert _config_bool("yes", False) is True


# Library's QFileSystemModel/host.db cache and the Files web retry runtime
# were retired. Equivalent production UI/service invariants now run in
# tests/ui/test_files_native_host.py and the Files domain/transaction suites.


# ═══════════════════════════════════════════════════════════════════════
# AUD-3805：InfoPanel 元数据残留
# ═══════════════════════════════════════════════════════════════════════


def test_info_panel_missing_fields_reset(qapp):

    panel = InfoPanel()
    try:
        panel.update_metadata({"Camera": "A7R4", "ISO": "800",
                               "Focal Length": "24mm"})
        assert panel.info_labels["camera"].text() == "A7R4"
        assert panel.info_labels["iso"].text() == "800"
        assert panel.info_labels["focal_length"].text() == "24mm"

        # 切到无这些字段的图 B：旧值不得残留
        panel.update_metadata({"Camera": "R5"})
        assert panel.info_labels["camera"].text() == "R5"
        assert panel.info_labels["iso"].text() == "-"
        assert panel.info_labels["focal_length"].text() == "-"
    finally:
        panel.deleteLater()


def test_info_panel_empty_metadata_clears_all(qapp):

    panel = InfoPanel()
    try:
        panel.update_metadata({"Lens": "35mm"})
        assert panel.info_labels["lens"].text() == "35mm"

        panel.update_metadata({})   # 空字典 = 清空面板
        assert panel.info_labels["lens"].text() == "-"
        assert all(label.text() == "-" for label in panel.info_labels.values())
    finally:
        panel.deleteLater()


def test_info_panel_snake_case_keys_accepted(qapp):

    panel = InfoPanel()
    try:
        panel.update_metadata({"iso": "1600"})  # rawpy snake_case 回退键
        assert panel.info_labels["iso"].text() == "1600"
    finally:
        panel.deleteLater()


# ═══════════════════════════════════════════════════════════════════════
# AUD-3600/3601：MenuBuilder 顶层菜单自动补建 / 子菜单 i18n
# ═══════════════════════════════════════════════════════════════════════


def _make_descriptor(action_id: str, menu_path: str):

    return MenuActionDescriptor(
        action_id=action_id,
        menu_path=menu_path,
        label=f"Label {action_id}",
        callback_name=f"_cb_{action_id.replace('.', '_')}",
    )


def _make_builder(qapp, with_menu_bar: bool = True):
    from PyQt6.QtWidgets import QMenuBar, QMenu

    registry = ActionRegistry()
    registry.register(_make_descriptor("edit.zoom_in", "View/Timeline"))
    registry.register(_make_descriptor("edit.zoom_out", "View/Timeline"))

    menu_bar = QMenuBar() if with_menu_bar else None
    builder = MenuBuilder(
        menu_bar=menu_bar,
        action_registry=registry,
        callback_resolver=lambda name: (lambda checked=False: None),
    )
    # 注册既有 File 菜单（模拟调用方只注册五个顶层菜单的现状）
    builder.register_existing_menu("File", QMenu("&File"))
    return builder, menu_bar


def test_unregistered_top_menu_auto_created(qapp):
    """AUD-3600：View/Timeline 组不再被整组丢弃——menu_bar 上自动补建。"""
    builder, menu_bar = _make_builder(qapp, with_menu_bar=True)
    try:
        actions = builder.build()

        assert set(actions) == {"edit.zoom_in", "edit.zoom_out"}
        top_names = [a.text() for a in menu_bar.actions()]
        assert any("View" in name for name in top_names), top_names
        # 子菜单与 action 真实挂载
        view_menu = builder._menus["View"]
        timeline = builder._menus["View/Timeline"]
        assert timeline is not None
        assert any(a.text() == "Label edit.zoom_in" for a in timeline.actions())
        assert view_menu is not None
    finally:
        if menu_bar is not None:
            menu_bar.deleteLater()


def test_unregistered_top_menu_without_menu_bar_warns(qapp, caplog):
    """AUD-3600：无 menu_bar（测试替身）时 warning 而非 DEBUG 静默跳过。"""
    import logging

    builder, _ = _make_builder(qapp, with_menu_bar=False)
    with caplog.at_level(logging.WARNING, logger="src.app.menu.menu_builder"):
        actions = builder.build()

    assert actions == {}
    assert any("no menu registered" in r.message for r in caplog.records)


def test_submenu_title_translated_via_fallback_chain(qapp, monkeypatch):
    """AUD-3601：menu.<seg> 未命中时回落 panel.<seg>（10 个 locale 均有）。"""

    monkeypatch.setattr(
        I18N, "tr_or_none",
        classmethod(lambda cls, key, **kw: {
            "panel.timeline": "时间线", "menu.image": "图像 (&I)",
        }.get(key)))

    assert mb.MenuBuilder._translated_segment("Timeline") == "时间线"
    assert mb.MenuBuilder._translated_segment("Image") == "图像 (&I)"
    assert mb.MenuBuilder._translated_segment("Nope") == "Nope"  # 原文回退


def test_submenu_title_uses_translated_text(qapp, monkeypatch):

    monkeypatch.setattr(
        I18N, "tr_or_none",
        classmethod(lambda cls, key, **kw: "时间线" if key == "panel.timeline" else None))
    builder, menu_bar = _make_builder(qapp, with_menu_bar=True)
    try:
        builder.build()
        assert builder._menus["View/Timeline"].title() == "时间线"
    finally:
        if menu_bar is not None:
            menu_bar.deleteLater()


# ═══════════════════════════════════════════════════════════════════════
# AUD-3701：module_loader 钩子异常语义
# ═══════════════════════════════════════════════════════════════════════


class _FakeRegistry:
    def __init__(self, accept: bool = True):
        self.accept = accept
        self.registered: list = []

    def register_module(self, module):
        if not self.accept:
            return False
        self.registered.append(module)
        return True


def _spec(hook: str = ""):

    return FeatureModuleSpec(
        feature_id="fakefeat",
        module_path="tests.app.test_fix_wd2g9_aud",
        class_name="_FakeFeatureModule",
        post_register_hook=hook,
    )


class _FakeFeatureModule:
    def __init__(self):
        self.hook_calls = 0

    def discover_and_register_plugins(self):
        self.hook_calls += 1
        return 7


class _RaisingHookModule(_FakeFeatureModule):
    def discover_and_register_plugins(self):
        raise RuntimeError("plugin discovery exploded")


def test_post_register_hook_failure_keeps_registered_true(monkeypatch):
    """AUD-3701：钩子抛异常 → 模块已注册，返回 (True, "") 而非误报失败。"""

    monkeypatch.setattr(ml, "_FEATURE_MODULES", ())  # 隔离表副作用
    monkeypatch.setattr(
        ml, "_import_feature_class",
        lambda spec: _RaisingHookModule)

    registry = _FakeRegistry(accept=True)
    ok, error = ml._register_spec(registry, _spec("discover_and_register_plugins"))

    assert ok is True
    assert error == ""
    assert len(registry.registered) == 1  # 模块确实已注册


def test_post_register_hook_success_returns_true(monkeypatch):

    monkeypatch.setattr(ml, "_FEATURE_MODULES", ())
    monkeypatch.setattr(ml, "_import_feature_class", lambda spec: _FakeFeatureModule)

    registry = _FakeRegistry(accept=True)
    ok, error = ml._register_spec(registry, _spec("discover_and_register_plugins"))

    assert ok is True
    assert error == ""
    assert registry.registered[0].hook_calls == 1


def test_register_module_rejected_reports_false(monkeypatch):
    """register_module 返回 False（不可用/重复）→ 如实上报失败。"""

    monkeypatch.setattr(ml, "_FEATURE_MODULES", ())
    monkeypatch.setattr(ml, "_import_feature_class", lambda spec: _FakeFeatureModule)

    registry = _FakeRegistry(accept=False)
    ok, error = ml._register_spec(registry, _spec())

    assert ok is False
    assert "rejected" in error


def test_import_error_reports_false(monkeypatch):

    def _boom(spec):
        raise ImportError("no such module")

    monkeypatch.setattr(ml, "_FEATURE_MODULES", ())
    monkeypatch.setattr(ml, "_import_feature_class", _boom)

    ok, error = ml._register_spec(_FakeRegistry(), _spec())
    assert ok is False
    assert "no such module" in error


# ═══════════════════════════════════════════════════════════════════════
# AUD-40013：内置示例目录定位
# ═══════════════════════════════════════════════════════════════════════


def test_discover_builtin_examples_finds_real_examples():

    examples = script_panel._discover_builtin_examples()

    assert examples, "内置示例目录为空——Scripts 菜单 Examples 将显示 (No examples)"
    assert "basic_grade.py" in examples
    # 内容确实是示例脚本（非空、含 topos API 调用痕迹）
    assert "topos" in examples["basic_grade.py"].lower()


def test_close_accept_path_runs_real_mixin_cleanup(main_window):
    """AUD-3500 集成路径：真实 FileOpsMixin.closeEvent 完整执行一次。

    单元用例均以 _isolate_mixin_close 隔离引擎停机；此处放开真实路径，
    验证确认通过后 MainWindow.closeEvent 与 mixin 的保存确认兼容
    （test_mode 分支），并确保引擎 worker 停机不抛异常。
    """
    mw = main_window
    _reset_shutdown_state(mw)
    confirm_calls: list[bool] = []

    def _confirm():
        confirm_calls.append(True)
        return True

    mw._confirm_save_before_close = _confirm
    event = _close_event()
    mw.closeEvent(event)

    assert event.isAccepted() is True
    assert confirm_calls == [True]      # 用户只被问一次（mixin 内 bypass）
    assert "_confirm_save_before_close" not in mw.__dict__


def test_discover_builtin_examples_dir_is_features_scripting():

    examples_dir = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(script_panel.__file__)),
        "..", "..", "features", "scripting", "examples"))
    assert os.path.isdir(examples_dir)
    # 旧缺陷路径（src/app/scripting/examples）必须不存在
    assert not os.path.isdir(os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(script_panel.__file__)),
        "..", "scripting", "examples")))
