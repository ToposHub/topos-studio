"""wd2-g10 批 D 修复回归（app 对话框域，Major × 14）。

对应 REMEDIATION_PLAN 条目：AUD-03901/03903/03904/03906/03907
（preferences_dialog）、AUD-40001/40002（project_manager_dialog）、
AUD-40015（script_panel）、AUD-4100/4101（shortcut_dialog）、
AUD-4110（welcome_dialog）。AUD-03902/AUD-03905 已由此前的 B3 系列
修复覆盖（见 tests/app/test_preferences_dialog_coverage.py 的
BUG-B3-02/03/04 翻转用例），本文件不重复。

证据报告：docs/audit/2026-09-28/batches/batch-039-app-ui.md、
batch-040-app-ui.md、batch-041-app-ui.md。
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QBuffer, QRunnable, Qt
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QMenu, QMessageBox, QWidget

pytestmark = pytest.mark.app

import src.app.ui.preferences_dialog as preferences_dialog_module
import src.app.ui.project_manager_dialog as project_manager_module
import src.app.ui.script_panel as script_panel_module
from src.app.ui.preferences_dialog import PreferencesDialog
from src.app.ui.script_panel import ScriptPanel
from src.app.ui.shortcut_dialog import ShortcutDialog
from src.app.ui.welcome_dialog import WelcomeDialog
from src.shared.config.config_manager import ConfigManager
from src.shared.ui.shortcut_manager import ShortcutManager


# ── 测试替身 ────────────────────────────────────────────────────


class _RecordingConfig:
    """ConfigManager 替身：仅内存读写，记录 set()/save_config() 调用。"""

    def __init__(self, initial: Optional[dict] = None) -> None:
        self.DEFAULT_CONFIG = ConfigManager.DEFAULT_CONFIG
        self.config: dict = {**self.DEFAULT_CONFIG, **(initial or {})}
        self.set_calls: list[tuple[str, Any, bool]] = []
        self.save_count = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.set_calls.append((key, value, auto_save))
        self.config[key] = value

    def save_config(self) -> None:
        self.save_count += 1


class _FakeFeModule:
    """file_explorer.main 替身：内存 config + 可控 plugin_manager。"""

    def __init__(self, disabled: Optional[list[str]] = None,
                 plugins: Optional[list[dict]] = None) -> None:
        self.conf: dict = {"disabled_plugins": list(disabled or [])}
        self.saved: Optional[dict] = None
        self.plugin_manager = SimpleNamespace(plugins=list(plugins or []))

    def load_config(self) -> dict:
        return dict(self.conf)

    def save_config(self, conf: dict) -> None:
        self.saved = dict(conf)
        self.conf = dict(conf)


class _FakeModulePage:
    """ModularPreferencePage 最小实现 + 可选失败注入与 rollback 钩子。"""

    def __init__(self, page_id: str, *, load_error: Optional[BaseException] = None,
                 apply_error: Optional[BaseException] = None,
                 restore_error: Optional[BaseException] = None) -> None:
        self.page_id = page_id
        self._load_error = load_error
        self._apply_error = apply_error
        self._restore_error = restore_error
        self.loads: list[dict] = []
        self.applies = 0
        self.default_restores: list[dict] = []
        self.rollbacks = 0

    def create_widget(self, parent: Optional[QWidget] = None) -> QWidget:
        from PyQt6.QtWidgets import QLabel
        return QLabel(f"fake page: {self.page_id}")

    def load(self, config: dict) -> None:
        if self._load_error is not None:
            raise self._load_error
        self.loads.append(config)

    def apply(self, config_manager: Any) -> None:
        if self._apply_error is not None:
            raise self._apply_error
        self.applies += 1

    def restore_defaults(self, defaults: dict) -> None:
        if self._restore_error is not None:
            raise self._restore_error
        self.default_restores.append(defaults)

    def rollback(self) -> None:
        self.rollbacks += 1


class _BareModulePage:
    """旧协议模块页：只有 create_widget/load/apply/restore_defaults，
    不实现可选的 rollback() 钩子——Cancel 时必须被安全跳过。"""

    def __init__(self, page_id: str) -> None:
        self.page_id = page_id
        self.applies = 0

    def create_widget(self, parent: Optional[QWidget] = None) -> QWidget:
        from PyQt6.QtWidgets import QLabel
        return QLabel(f"bare page: {self.page_id}")

    def load(self, config: dict) -> None:
        return None

    def apply(self, config_manager: Any) -> None:
        self.applies += 1

    def restore_defaults(self, defaults: dict) -> None:
        return None


class _ShortcutConfig:
    """ShortcutManager 所需的最小 ConfigManager 替身。"""

    def __init__(self, data: Optional[dict] = None) -> None:
        self.data: dict = dict(data or {})
        self.saved_keys: list[str] = []

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.data[key] = value
        self.saved_keys.append(key)

    def save_config(self) -> None:
        return None


class _DictConfig:
    """面向 welcome/project manager 的最小只读 config。"""

    def __init__(self, data: Optional[dict] = None) -> None:
        self.data: dict = dict(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.data[key] = value


# ── 工具函数 ────────────────────────────────────────────────────


def _set_combo_by_data(combo, data: Any) -> None:
    for i in range(combo.count()):
        if combo.itemData(i) == data:
            combo.setCurrentIndex(i)
            return
    raise AssertionError(f"combo has no item with data {data!r}")


def _confirm_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preferences_dialog_module.QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    captured: list[tuple] = []
    monkeypatch.setattr(
        preferences_dialog_module.QMessageBox, "warning",
        lambda *args, **kwargs: captured.append(args) or QMessageBox.StandardButton.Ok,
    )
    return captured


# ── PreferencesDialog fixtures ─────────────────────────────────


@pytest.fixture
def make_dialog(qapp, monkeypatch, tmp_path) -> Iterator[
        Callable[..., tuple[PreferencesDialog, _RecordingConfig]]]:
    """PreferencesDialog 工厂：hermetic 缓存目录 + 切断真实 FE 插件桥。"""
    import src.shared.io.file_io as file_io
    monkeypatch.setattr(file_io.FileIO, "CACHE_DIR", str(tmp_path / "images_cache"))
    monkeypatch.setattr(file_io.FileIO, "BAKED_CACHE_DIR", str(tmp_path / "baked_layers"))

    created: list[PreferencesDialog] = []

    def _make(initial: Optional[dict] = None,
              module_pages: Optional[list[Any]] = None) -> tuple[PreferencesDialog, _RecordingConfig]:
        cfg = _RecordingConfig(initial)
        dlg = PreferencesDialog(cfg, module_pages=list(module_pages or []))
        # 默认切断真实 file_explorer 桥；需要 FE 行为的用例在测试体内重设。
        dlg._fe_plugin_bridge = lambda: None  # type: ignore[method-assign]
        created.append(dlg)
        return dlg, cfg

    yield _make
    for dlg in created:
        dlg.deleteLater()


# ═══════════════════════════ AUD-03901 ═══════════════════════════


class TestCancelPreviewRollbackSignalLock:

    def test_cancel_rollback_emits_each_preview_key_once(self, make_dialog) -> None:
        """[AUD-03901] Cancel 回滚时每个外观项只发射一次预览信号。

        旧实现 _restore_preview_snapshot 不加 _preview_blocked 状态锁：
        程序化 setCurrentIndex/setValue 逐项触发一次，末尾
        _emit_batch_preview 再发一次（双重发射）。
        """
        dlg, _ = make_dialog({
            "ui_scale": 100, "panel_opacity": 90, "accent_color": "#ff8800",
        })
        events: list[tuple[str, Any]] = []
        dlg.appearance_preview_changed.connect(
            lambda key, value: events.append((key, value)))
        # 用户实时预览（合法发射）
        _set_combo_by_data(dlg._widgets["ui_scale"], 140)
        assert ("ui_scale", 140) in events
        events.clear()
        dlg._on_cancel()
        # 回滚批次恰好 3 条（每个 previewable key 一次），无逐项重复
        assert len(events) == 3
        assert dict(events) == {
            "ui_scale": 100, "panel_opacity": 90, "accent_color": "#ff8800",
        }
        assert events.count(("ui_scale", 100)) == 1


# ═══════════════════════════ AUD-03903 ═══════════════════════════


class TestCancelRollbackCompleteness:

    @staticmethod
    def _fe_with_two_plugins() -> _FakeFeModule:
        return _FakeFeModule(
            disabled=["plugin.alpha"],
            plugins=[
                {"name": "plugin.alpha", "info": {"version": "1.0", "description": "A"}},
                {"name": "plugin.beta", "info": {"version": "2.0", "description": "B"}},
            ],
        )

    @staticmethod
    def _uncheck(dlg: PreferencesDialog, name: str) -> None:
        for i in range(dlg._plugin_list.count()):
            item = dlg._plugin_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                item.setCheckState(Qt.CheckState.Unchecked)

    def test_cancel_after_apply_restores_fe_config(self, make_dialog) -> None:
        """[AUD-03903] Apply 直写的 FE config 必须随 Cancel 一并还原。"""
        dlg, _ = make_dialog()
        fe = self._fe_with_two_plugins()
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        dlg._refresh_plugins()
        self._uncheck(dlg, "plugin.beta")   # alpha 本就处于停用（未勾选）态

        dlg._on_apply()
        assert fe.conf["disabled_plugins"] == ["plugin.alpha", "plugin.beta"]

        dlg._on_cancel()
        assert fe.conf["disabled_plugins"] == ["plugin.alpha"]
        assert fe.saved["disabled_plugins"] == ["plugin.alpha"]

    def test_cancel_without_apply_never_touches_fe_config(self, make_dialog) -> None:
        """[AUD-03903] 未 Apply 直接 Cancel 不得触碰 FE config。"""
        dlg, _ = make_dialog()
        fe = self._fe_with_two_plugins()
        dlg._fe_plugin_bridge = lambda: fe  # type: ignore[method-assign]
        dlg._refresh_plugins()
        self._uncheck(dlg, "plugin.alpha")

        dlg._on_cancel()
        assert fe.conf["disabled_plugins"] == ["plugin.alpha"]
        assert fe.saved is None
        assert dlg._fe_config_snapshot is None

    def test_cancel_after_apply_calls_module_page_rollback(self, make_dialog) -> None:
        """[AUD-03903] 模块页副作用经可选 rollback() 钩子在 Cancel 时撤销。"""
        page = _FakeModulePage("color.processing")
        bare = _BareModulePage("deliver.export")
        dlg, _ = make_dialog(module_pages=[page, bare])

        dlg._on_apply()
        assert page.rollbacks == 0
        dlg._on_cancel()
        assert page.rollbacks == 1
        assert bare.applies == 1  # bare 页正常参与 apply，rollback 被安全跳过


# ═══════════════════════════ AUD-03904 ═══════════════════════════


class TestCancelConfigSnapshotRestore:

    def test_cancel_restores_config_via_public_set_api(self, make_dialog) -> None:
        """[AUD-03904] 回滚走 set()/save_config() 公开路径，不整体替换 config。"""
        dlg, cfg = make_dialog({"panel_opacity": 90})
        identity_before = id(cfg.config)
        dlg._widgets["panel_opacity"].setValue(100)
        dlg._on_apply()
        assert cfg.config["panel_opacity"] == 100

        dlg._on_cancel()
        assert cfg.config["panel_opacity"] == 90
        # 不再裸替换 config 属性：dict 对象身份不变（无撕裂窗口）
        assert id(cfg.config) == identity_before
        # 恢复值经由公开 set(auto_save=False) 写入
        assert ("panel_opacity", 90, False) in cfg.set_calls
        assert cfg.save_count == 2  # Apply 落盘一次 + Cancel 回滚一次

    def test_cancel_preserves_foreign_keys_added_while_open(self, make_dialog) -> None:
        """[AUD-03904] 对话框打开期间其他组件写入的键不被 Cancel 抹掉。

        旧的整体替换实现会把这些键连带回滚（数据丢失）。
        """
        dlg, cfg = make_dialog()
        dlg._on_apply()
        cfg.config["recent_projects"] = ["/tmp/x.topos"]
        dlg._on_cancel()
        assert cfg.config["recent_projects"] == ["/tmp/x.topos"]


# ═══════════════════════════ AUD-03906 ═══════════════════════════


class TestModulePageDelegationErrors:

    def test_module_page_apply_failure_warns_user(
            self, make_dialog, monkeypatch, caplog) -> None:
        """[AUD-03906] 模块页 apply 抛错必须记录堆栈并弹出警示。"""
        warnings = _capture_warnings(monkeypatch)
        page = _FakeModulePage("color.processing", apply_error=RuntimeError("boom"))
        dlg, cfg = make_dialog(module_pages=[page])

        with caplog.at_level("ERROR", logger="src.app.ui.preferences_dialog"):
            dlg._on_apply()

        assert page.applies == 0
        assert any("apply failed" in record.getMessage() for record in caplog.records)
        assert len(warnings) == 1
        assert "color.processing" in str(warnings[0])
        assert cfg.save_count == 1  # 宿主设置仍落盘

    def test_module_page_restore_defaults_failure_warns_user(
            self, make_dialog, monkeypatch, caplog) -> None:
        """[AUD-03906] restore_defaults 失败不得伪装成"已恢复默认"。"""
        _confirm_yes(monkeypatch)
        warnings = _capture_warnings(monkeypatch)
        page = _FakeModulePage("deliver.export", restore_error=ValueError("bad"))
        dlg, _ = make_dialog(module_pages=[page])

        with caplog.at_level("ERROR", logger="src.app.ui.preferences_dialog"):
            dlg._on_restore_defaults()

        assert any(
            "restore_defaults failed" in record.getMessage() for record in caplog.records)
        assert len(warnings) == 1
        assert "deliver.export" in str(warnings[0])

    def test_module_page_load_failure_is_logged_not_swallowed(
            self, monkeypatch, tmp_path, caplog, qapp) -> None:
        """[AUD-03906] load 失败不再 except-pass 静默吞掉。"""
        import src.shared.io.file_io as file_io
        monkeypatch.setattr(file_io.FileIO, "CACHE_DIR", str(tmp_path / "c"))
        monkeypatch.setattr(file_io.FileIO, "BAKED_CACHE_DIR", str(tmp_path / "b"))
        page = _FakeModulePage("edit.ai_editing", load_error=TypeError("nope"))

        with caplog.at_level("ERROR", logger="src.app.ui.preferences_dialog"):
            dlg = PreferencesDialog(_RecordingConfig(), module_pages=[page])
            dlg._fe_plugin_bridge = lambda: None  # type: ignore[method-assign]
        dlg.deleteLater()

        assert any("load failed" in record.getMessage() for record in caplog.records)

    def test_module_page_success_path_never_warns(self, make_dialog, monkeypatch) -> None:
        warnings = _capture_warnings(monkeypatch)
        page = _FakeModulePage("composite.settings")
        dlg, _ = make_dialog(module_pages=[page])
        dlg._on_apply()
        assert page.applies == 1
        assert warnings == []


# ═══════════════════════════ AUD-03907 ═══════════════════════════


class TestCorruptNumericConfigLoading:

    def test_corrupt_numeric_values_load_with_fallbacks(self, make_dialog) -> None:
        """[AUD-03907] 损坏 config 数值不再使对话框构造崩溃。"""
        dlg, _ = make_dialog({
            "auto_save_interval": 7.9,                    # float → int 截断
            "max_recent_files": [1, 2],                   # list → 默认 10
            "panel_opacity": "100",                       # 可解析 str → 100
            "zoom_speed": "fast",                         # 不可解析 str → 默认 100
            "render_cache_auto_threshold": "6x",          # ValueError → 默认 6
            "default_timeline_width": None,               # None → 默认 1920
            "undo_history_limit": True,                   # bool 视为损坏 → 默认 50
        })
        w = dlg._widgets
        assert w["auto_save_interval"].intValue() == 7
        assert w["max_recent_files"].intValue() == 10
        assert w["panel_opacity"].value() == 100
        assert w["zoom_speed"].value() == 100
        assert w["render_cache_auto_threshold"].value() == 6
        assert w["default_timeline_width"].value() == 1920
        assert w["undo_history_limit"].intValue() == 50

    def test_coerce_int_direct(self) -> None:
        coerce = PreferencesDialog._coerce_int
        assert coerce(5, 9) == 5
        assert coerce(5.7, 9) == 5
        assert coerce("42", 9) == 42
        assert coerce(" 42 ", 9) == 42
        assert coerce("6x", 9) == 9
        assert coerce(None, 9) == 9
        assert coerce([1], 9) == 9
        assert coerce({"v": 1}, 9) == 9
        assert coerce(True, 9) == 9


# ═══════════════════════════ AUD-40001 ═══════════════════════════


def _png_bytes() -> bytes:
    pixmap = QPixmap(16, 9)
    pixmap.fill(QColor("#3050a0"))
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.ReadWrite)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data().data())


def _meta(path: str, name: Any = "Project", modified_at: Any = 1.0,
          file_modified_at: Any = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        path=path, name=name, project_mode="video", created_at=1.0,
        modified_at=modified_at, file_modified_at=file_modified_at,
        preview_path=None,
    )


class _SilentSignal:
    def connect(self, _slot: Any) -> None:
        return None


class _SilentSignals:
    def __init__(self) -> None:
        self.done = _SilentSignal()


class _SilentScanWorker(QRunnable):
    """不触发 done 信号的扫描替身，避免线程池竞态。"""

    def __init__(self) -> None:
        super().__init__()
        self.signals = _SilentSignals()

    def run(self) -> None:
        return None


@pytest.fixture
def make_pm_dialog(qapp, monkeypatch) -> Iterator[Callable[[], Any]]:
    """ProjectManagerDialog 工厂：静默扫描替身 + 假 FE/config。"""
    monkeypatch.setattr(
        project_manager_module, "_ProjectScanWorker",
        lambda config: _SilentScanWorker(),
    )
    created: list[Any] = []

    def _make() -> Any:
        dlg = project_manager_module.ProjectManagerDialog(_DictConfig({}))
        created.append(dlg)
        return dlg

    yield _make
    for dlg in created:
        dlg.deleteLater()


class TestCoverThumbnailCache:

    def test_cover_extracted_once_across_filter_rebuilds(
            self, make_pm_dialog, monkeypatch) -> None:
        """[AUD-40001] 搜索/排序触发的整表重建复用已解码封面。"""
        calls: list[str] = []

        def fake_extract(path: str) -> bytes:
            calls.append(path)
            return _png_bytes()

        monkeypatch.setattr(
            project_manager_module, "extract_cover_thumbnail", fake_extract)
        dlg = make_pm_dialog()
        dlg._all_projects = [_meta("/lib/p1.topos"), _meta("/lib/p2.topos")]

        dlg._apply_filter("")
        assert sorted(calls) == ["/lib/p1.topos", "/lib/p2.topos"]
        calls.clear()

        # 模拟搜索每个按键 + 排序变更——全部命中缓存，零次重开 zip
        dlg._apply_filter("pro")
        dlg.sort_box.setCurrentIndex(1)   # sort by name（触发一次重建）
        dlg._apply_filter("pro")
        dlg._apply_filter("")
        assert calls == []

    def test_refresh_invalidates_cover_cache(self, make_pm_dialog, monkeypatch) -> None:
        """[AUD-40001] 刷新（重新扫描）时封面缓存失效。"""
        monkeypatch.setattr(
            project_manager_module, "extract_cover_thumbnail",
            lambda path: _png_bytes())
        dlg = make_pm_dialog()
        dlg._all_projects = [_meta("/lib/p1.topos")]
        dlg._apply_filter("")
        assert len(dlg._cover_pixmap_cache) == 1

        # 静默扫描替身不回调 done，手工复位 inflight 后再触发刷新
        dlg._scan_inflight = False
        dlg._refresh_project_list()
        assert dlg._cover_pixmap_cache == {}


# ═══════════════════════════ AUD-40002 ═══════════════════════════


class TestMalformedProjectMeta:

    def test_filter_and_search_survive_non_str_names(
            self, make_pm_dialog) -> None:
        """[AUD-40002] 畸形 project.json（name 非字符串）不再令列表构建崩溃。"""
        dlg = make_pm_dialog()
        dlg._all_projects = [
            _meta("/lib/bad1.topos", name=12345,
                  modified_at="not-a-number", file_modified_at=None),
            _meta("/lib/bad2.topos", name=["nested", "list"]),
            _meta("/lib/ok.topos", name="Fine Project"),
        ]
        # 名称排序（旧实现 meta.name.casefold() 抛 AttributeError）
        dlg.sort_box.setCurrentIndex(1)
        dlg._apply_filter("")
        assert dlg.list_widget.count() == 3

        # 搜索（旧实现 join(meta.name) 抛 TypeError）
        dlg._apply_filter("123")
        assert dlg.list_widget.count() == 1
        item = dlg.list_widget.item(0)
        assert item.data(
            project_manager_module._ProjectCardDelegate.PROJECT_NAME_ROLE) == "12345"

    def test_recent_sort_survives_mixed_mtimes(self, make_pm_dialog) -> None:
        """[AUD-40002] modified_at 非数值时排序键收敛为 float。"""
        dlg = make_pm_dialog()
        dlg._all_projects = [
            _meta("/lib/a.topos", modified_at="soon", file_modified_at=None),
            _meta("/lib/b.topos", modified_at=2.0, file_modified_at=None),
            _meta("/lib/c.topos", modified_at=1.0, file_modified_at=None),
        ]
        dlg.sort_box.setCurrentIndex(0)  # recently modified
        dlg._apply_filter("")             # 不抛 TypeError 即通过
        assert dlg.list_widget.count() == 3

    def test_safe_meta_helpers(self) -> None:
        safe_name = project_manager_module.ProjectManagerDialog._safe_meta_name
        safe_mtime = project_manager_module.ProjectManagerDialog._safe_meta_mtime
        assert safe_name(_meta("/p", name="ok")) == "ok"
        assert safe_name(_meta("/p", name=123)) == "123"
        assert safe_name(_meta("/p", name=None)) == ""
        assert safe_mtime(_meta("/p", modified_at="x", file_modified_at=None)) == 0.0
        assert safe_mtime(_meta("/p", modified_at=3, file_modified_at=9)) == 3.0
        assert safe_mtime(_meta("/p", modified_at=True, file_modified_at=4)) == 4.0


# ═══════════════════════════ AUD-40015 ═══════════════════════════


@pytest.fixture
def make_script_panel(qapp, monkeypatch) -> Iterator[Callable[[], ScriptPanel]]:
    """ScriptPanel 工厂：隔离示例/用户脚本发现与 QSettings 近期文件。"""
    monkeypatch.setattr(
        script_panel_module, "_discover_builtin_examples",
        lambda: {"example_one.py": "print(1)"})
    monkeypatch.setattr(
        script_panel_module, "_discover_user_scripts",
        lambda: {"my_script.py": "print(2)"})
    monkeypatch.setattr(script_panel_module.ScriptPanel, "_load_recent_files", lambda self: None)
    created: list[ScriptPanel] = []

    def _make() -> ScriptPanel:
        panel = ScriptPanel()
        panel._recent_files = ["/tmp/old.py"]
        created.append(panel)
        return panel

    yield _make
    for panel in created:
        panel.deleteLater()


class TestScriptsMenuReuse:

    def test_menu_object_reused_no_accumulation(self, make_script_panel) -> None:
        """[AUD-40015] 多次刷新复用同一 QMenu，不再无限累积。"""
        panel = make_script_panel()
        menu = panel.btn_scripts.menu()
        assert menu is not None
        baseline_menus = {
            m for m in panel.findChildren(QMenu)}  # noqa: C416 - 身份集合
        assert menu in baseline_menus

        for _ in range(5):
            panel._update_scripts_menu()

        # 按钮 menu 身份不变；面板上没有新增任何 QMenu（旧实现 1→6 递增）
        assert panel.btn_scripts.menu() is menu
        assert {m for m in panel.findChildren(QMenu)} == baseline_menus

    def test_rebuild_refreshes_content_in_place(self, make_script_panel, monkeypatch) -> None:
        """[AUD-40015/40016] clear+refill 后同一菜单立即呈现新内容。"""
        panel = make_script_panel()
        menu = panel.btn_scripts.menu()
        assert any(action.text() == "My Script" for action in menu.actions())

        monkeypatch.setattr(
            script_panel_module, "_discover_user_scripts",
            lambda: {"fresh_script.py": "print(3)"})
        panel._update_scripts_menu()

        assert any(action.text() == "Fresh Script" for action in menu.actions())
        assert not any(action.text() == "My Script" for action in menu.actions())
        # action 文案不随重建累积（同一文案只出现一次）
        texts = [action.text() for action in menu.actions() if action.text()]
        assert len(texts) == len(set(texts))


# ═══════════════════════════ AUD-4100 / AUD-4101 ════════════════


def _make_shortcut_manager(
        custom: Optional[dict[str, str]] = None,
        actions: int = 3) -> tuple[ShortcutManager, _ShortcutConfig]:
    cfg = _ShortcutConfig({"custom_shortcuts": dict(custom or {})})
    manager = ShortcutManager(cfg)
    for i in range(actions):
        manager.register(
            f"test.action{i}", f"Action {i}", "Test", f"Ctrl+{i + 1}")
    return manager, cfg


@pytest.fixture
def make_shortcut_dialog(qapp) -> Iterator[Callable[..., ShortcutDialog]]:
    created: list[ShortcutDialog] = []

    def _make(manager: ShortcutManager) -> ShortcutDialog:
        dialog = ShortcutDialog(manager)
        created.append(dialog)
        return dialog

    yield _make
    for dialog in created:
        if not sip.isdeleted(dialog):
            dialog.deleteLater()


class TestShortcutDialogCancelSemantics:

    def test_cancel_reverts_session_edits(self, make_shortcut_dialog) -> None:
        """[AUD-4100] Cancel 撤销本次会话的全部快捷键改动。"""
        manager, cfg = _make_shortcut_manager()
        dialog = make_shortcut_dialog(manager)

        manager.set_shortcut("test.action0", "Ctrl+Shift+X")
        manager.reset_all()
        assert manager._custom_shortcuts == {}

        dialog.reject()
        assert manager._custom_shortcuts == {}
        assert cfg.data["custom_shortcuts"] == {}
        assert manager.get_shortcut("test.action0") == "Ctrl+1"  # 回默认

    def test_cancel_restores_entry_custom_map(self, make_shortcut_dialog) -> None:
        """[AUD-4100] Cancel 恢复进入对话框时的自定义映射（含被删项）。"""
        manager, cfg = _make_shortcut_manager(custom={"test.action1": "Ctrl+9"})
        dialog = make_shortcut_dialog(manager)

        manager.set_shortcut("test.action2", "Ctrl+8")   # 新增自定义
        manager.remove_shortcut("test.action1")          # 删除既有自定义
        dialog.reject()

        assert manager._custom_shortcuts == {"test.action1": "Ctrl+9"}
        assert cfg.data["custom_shortcuts"] == {"test.action1": "Ctrl+9"}
        assert manager.get_shortcut("test.action2") == "Ctrl+3"

    def test_accept_keeps_session_edits(self, make_shortcut_dialog) -> None:
        """[AUD-4100] OK（accept）保留全部改动。"""
        manager, cfg = _make_shortcut_manager(custom={"test.action1": "Ctrl+9"})
        dialog = make_shortcut_dialog(manager)

        manager.set_shortcut("test.action2", "Ctrl+8")
        dialog.accept()

        assert manager._custom_shortcuts == {
            "test.action1": "Ctrl+9", "test.action2": "Ctrl+8"}
        assert cfg.data["custom_shortcuts"] == manager._custom_shortcuts

    def test_cancel_without_changes_skips_disk_writes(self, make_shortcut_dialog) -> None:
        """[AUD-4100] 无改动时 Cancel 不产生多余落盘。"""
        manager, cfg = _make_shortcut_manager(custom={"test.action1": "Ctrl+9"})
        dialog = make_shortcut_dialog(manager)
        writes_before = list(cfg.saved_keys)
        dialog.reject()
        assert cfg.saved_keys == writes_before


class TestShortcutDialogDeleteOnClose:

    def test_dialog_attributes_set(self, make_shortcut_dialog) -> None:
        manager, _ = _make_shortcut_manager()
        dialog = make_shortcut_dialog(manager)
        assert dialog.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def test_dialog_destroyed_after_close(self, qapp, monkeypatch) -> None:
        """[AUD-4101] 关闭（accept/reject done 路径）后 C++ 对象销毁。"""
        from PyQt6.QtCore import QCoreApplication, QEvent
        manager, _ = _make_shortcut_manager()
        dialog = ShortcutDialog(manager)
        dialog.reject()
        qapp.processEvents()
        # deleteLater 的 DeferredDelete 事件需显式冲刷（真实事件循环下一轮
        # 也会处理；测试里直接 sendPostedEvents 等价）
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert sip.isdeleted(dialog)


# ═══════════════════════════ AUD-4110 ═══════════════════════════


class TestWelcomeRecentListValidation:

    def test_str_list_rejects_scalar_and_keeps_strings(self) -> None:
        str_list = WelcomeDialog._str_list
        assert str_list(None) == []
        assert str_list("/a/single/path") == []       # str 不按字符迭代
        assert str_list({"a": 1}) == []
        assert str_list([3, "ok", "", None, "good"]) == ["ok", "good"]
        assert str_list([f"p{i}" for i in range(80)], limit=50) == [
            f"p{i}" for i in range(50)]

    def test_recent_files_with_non_str_entries_constructs(self, qapp) -> None:
        """[AUD-4110] 损坏的 recent_files 不再令欢迎页构造崩溃。"""
        cfg = _DictConfig({
            "recent_files": ["/a/b.png", 42, {"x": 1}, "", "/c/d.png"],
            "recent_projects": [7, "/p/proj.topos"],
            "show_welcome_on_startup": True,
        })
        dialog = WelcomeDialog(cfg)
        try:
            assert dialog.recent_list.count() == 2
            paths = [
                dialog.recent_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(dialog.recent_list.count())
            ]
            assert paths == ["/a/b.png", "/c/d.png"]
        finally:
            dialog.deleteLater()
            qapp.processEvents()
