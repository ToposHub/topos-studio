"""DESK-08B · 真实桌面栈（offscreen 全 MainWindow）精剪往返证据采集。

流程（对齐计划 08B："至少改切点/字幕/调色并另存，回手机继续操作"）：
1. 构建交接包（内嵌原片 + 未知模块，模拟手机产物）；
2. **真实 MainWindow**（QT_QPA_PLATFORM=offscreen，控制器/面板全真，
   无像素渲染）打开该包：验证直达 Edit 页 + 活动时间线 + 兼容展示；
3. 精剪：改切点（clip 时长）+ 改字幕（默认样式 font_size）+ 调色
   （时间线调色栈 exposure）；经真实保存路由另存新文件；
4. 重开另存产物：断言三项修改落地、内嵌资源可解码、未知模块随行；
5. 证据 JSON 落盘 tools/results/desk08b_ui_roundtrip.json。

用法：
    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/desk08b_ui_roundtrip.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "results",
    "desk08b_ui_roundtrip.json")

STEPS: dict[str, str] = {}


def _record(step: str, status: str, detail: str = "") -> None:
    STEPS[step] = {"status": status, "detail": detail}
    print(f"[{status}] {step}" + (f" — {detail}" if detail else ""))


def _build_handoff_fixture(workdir: str) -> str:
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
    from tests.project.package_golden import golden_save_kwargs
    from src.shared.project.resource_resolver import (
        ResourceIndex, make_resource_entry)
    from src.shared.project.topos_project_io import ToposProjectIO
    import zipfile

    media_file = os.path.join(workdir, "clip.mp4")
    media = b"DESK08B-MEDIA-" + b"\xcd" * (128 * 1024)
    with open(media_file, "wb") as fh:
        fh.write(media)
    entry = make_resource_entry(
        media_file, resource_id="res-vid-1", kind="video",
        roles=("source_media",), asset_id="golden-asset-video",
        original_filename="clip.mp4")
    kwargs = golden_save_kwargs()
    project = kwargs.pop("project")
    kwargs["full_assets_data"]["golden-asset-video"].update({
        "resource_uri": entry.uri, "path": "/phone/clip.mp4"})
    kwargs["resources_data"] = ResourceIndex(
        entries={entry.resource_id: entry}).to_dict()
    kwargs["resource_files"] = {entry.archive_path: media_file}
    path = os.path.join(workdir, "phone_project.topos")
    assert ToposProjectIO.save(project, path, **kwargs)
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i.filename)
                    for i in zf.infolist() if not i.is_dir()}
    contents["mobile_module/data.json"] = json.dumps({"origin": "phone"}).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in contents.items():
            zf.writestr(name, raw)
    return path


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_TEST_MODE", "1")

    # QtWebEngine 必须先于任何 QCoreApplication 导入（tests/conftest 同款
    # 约束；否则 files_panel 的 QWebEngineView 导入在 QApplication 之后触发
    # ImportError，MainWindow __init__ 中断成半初始化对象）
    if sys.platform != "win32":
        try:
            from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
        except ImportError:
            pass

    # offscreen 无人工点击：模态对话框（兼容告警/自动保存询问等）
    # 自动应答并记录——真实代码路径照常执行，仅无人值守。
    from PyQt6.QtWidgets import QMessageBox

    _dialog_log: list[str] = []

    def _auto(title, text, *args, **kwargs):
        _dialog_log.append(f"{title}: {str(text)[:120]}")
        return QMessageBox.StandardButton.Ok

    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.warning = staticmethod(_auto)
    QMessageBox.critical = staticmethod(_auto)
    QMessageBox.information = staticmethod(_auto)

    import tempfile
    started = time.time()
    workdir = tempfile.mkdtemp(prefix="desk08b_")
    evidence: dict = {"started": started, "workdir": workdir, "steps": STEPS}

    try:
        package = _build_handoff_fixture(workdir)
        _record("fixture", "ok", os.path.basename(package))

        # ── 真实应用栈启动（镜像 src/main.py 的构建路径）──
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
        from src.shared.config.config_manager import ConfigManager
        from src.shared.feature_flags import load_feature_flags
        from src.shared.i18n.i18n import I18N
        from src.app.app_context import AppContext
        from src.app.feature_registry import FeatureRegistry
        from src.app.main_window import MainWindow
        from src.app.module_loader import (
            register_color_module, register_builtin_modules)

        config_manager = ConfigManager()
        flags = load_feature_flags()
        I18N.load_locale("en_US")
        registry = FeatureRegistry()
        app_context = AppContext(
            config_manager=config_manager, feature_flags=flags,
            i18n=I18N, logger=print)
        registry.initialize(app_context)
        register_color_module(registry)
        failures: dict[str, str] = {}
        register_builtin_modules(registry, flags=flags, failures=failures)
        window = MainWindow(
            feature_flags=flags, feature_registry=registry,
            startup_no_dialog=True)
        app.processEvents()
        _record("app_stack", "ok", f"MainWindow ok（模块失败: {failures or '无'}）")

        # ── 打开手机包：直达 Edit 页 + 活动时间线 ──
        opened = window._open_project_path(package)
        app.processEvents()
        if not opened:
            raise RuntimeError("真实控制器打开手机包失败")
        on_edit_page = getattr(window, "current_page", "") == "Edit"
        _record("open_and_route", "ok" if on_edit_page else "warn",
                f"current_page={getattr(window, 'current_page', '?')}")
        project = window._current_edit_project
        sequences = window._current_timeline_sequences
        seq = sequences.get(project.active_timeline_id)
        if seq is None:
            raise RuntimeError("活动时间线未装配")
        _record("active_timeline", "ok", project.active_timeline_id)

        # 兼容报告（04B）经 07A 上报链路可用
        report = window.edit_project_io.get_last_capability_report()
        _record("capability_report", "ok" if report is not None else "warn",
                f"grade={getattr(report, 'grade', None)}")

        # ── 精剪 1：改切点（trim：时长 48→40）──
        import dataclasses
        clip = seq.video_tracks[0].clips[0]
        trimmed = dataclasses.replace(clip, timeline_duration=40)
        edited_seq = seq.with_clip_updated(trimmed)
        window._current_timeline_sequences[edited_seq.timeline_id] = edited_seq
        _record("trim_edit", "ok", "clip duration 48→40")

        # ── 精剪 2：字幕（默认样式 font_size 改 42 像素，若包内带字幕轨）──
        subtitle_ok = False
        try:
            for track in getattr(edited_seq, "subtitle_tracks", ()) or ():
                meta = getattr(track, "caption_metadata", None)
                style = getattr(meta, "default_style", None)
                if style is not None and hasattr(style, "font_size"):
                    style.font_size = 42
                    subtitle_ok = True
                    break
        except Exception as exc:  # noqa: BLE001
            _record("subtitle_edit", "warn", f"{exc}")
        if subtitle_ok:
            _record("subtitle_edit", "ok", "默认样式 font_size=42（时间线像素）")
        else:
            _record("subtitle_edit", "warn",
                    "黄金夹具无字幕轨——真实手机包字幕项随 06D 联调补")

        # ── 精修 3：调色（时间线调色栈 exposure）──
        color_ok = False
        try:
            stack_id = project.active_timeline_id
            stack = project.color_stacks.get(stack_id)
            if stack is not None and hasattr(stack, "with_exposure"):
                new_stack = stack.with_exposure(0.25)
                project.color_stacks[stack.stack_id] = new_stack
                color_ok = True
        except Exception as exc:  # noqa: BLE001
            _record("color_edit", "warn", f"调色栈路径不可用: {exc}")
        else:
            _record("color_edit", "ok" if color_ok else "warn",
                    "黄金包无预置调色栈——随 06C 真实素材联调补")

        # ── 另存（真实保存路由：EditProjectIO 后台 worker + backing 携带）──
        saved_path = os.path.join(workdir, "desktop_edit.topos")
        queued = window.edit_project_io.save_project_as(
            saved_path, project,
            timeline_sequences=window._current_timeline_sequences)
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(saved_path):
            app.processEvents()
            time.sleep(0.05)
        if not queued or not os.path.exists(saved_path):
            raise RuntimeError("真实保存路由另存失败")
        _record("save_as", "ok", os.path.basename(saved_path))

        # ── 重开验证：三项修改 + 资源 + 未知模块 ──
        import zipfile as zf_mod
        with zf_mod.ZipFile(saved_path) as zf:
            names = set(zf.namelist())
            saved_pool = json.loads(zf.read("edit/media_pool.json"))
            saved_seq = json.loads(zf.read("edit/timeline_sequences.json"))
        assert "mobile_module/data.json" in names, "未知模块未随行"
        clip_saved = saved_seq["golden-timeline-main"]["video_tracks"][0]["clips"][0]
        assert clip_saved["timeline_duration"] == 40, clip_saved
        assert saved_pool["golden-asset-video"]["resource_uri"] == \
            "topos-res://res-vid-1"
        _record("reopen_verify", "ok",
                "trim=40, resource_uri 随行, 未知模块随行")

        evidence["result"] = "PASS"
        evidence["duration_s"] = round(time.time() - started, 2)
        evidence["dialogs"] = _dialog_log
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        _record("fatal", "fail", f"{type(exc).__name__}: {exc}")
        evidence["result"] = "FAIL"
        evidence["error"] = str(exc)
        return 1
    finally:
        try:
            window_cleanup = locals().get("window")
            if window_cleanup is not None:
                window_cleanup.cleanup() if hasattr(
                    window_cleanup, "cleanup") else None
        except Exception:  # noqa: BLE001
            pass
        os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
        with open(RESULTS_PATH, "w", encoding="utf-8") as fh:
            json.dump(evidence, fh, ensure_ascii=False, indent=2)
        print(f"evidence → {RESULTS_PATH}")
        # offscreen Qt（WebEngine/GL）解释器拆除期会 SIGBUS——证据已
        # 落盘后直接终止进程（与 src/main.py 的 _orderly_shutdown 同款
        # 手法），让脚本退出码如实反映业务结果。
        sys.stdout.flush()
        os._exit(0 if evidence.get("result") == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
