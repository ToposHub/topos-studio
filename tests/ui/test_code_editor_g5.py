"""G5：code_editor 接管——G 节行验收。

**读取/编码、外部变更、安全保存、恢复、新建与多文件入口；不依赖旧
routes/JS globals**。等价编辑工具（QPlainTextEdit 多标签）+ 域层文件
服务；原生命令（零 HTTP）+ F5 singleton。
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.files.code_editor_service import (
    EditorFileError,
    new_file_buffer,
    read_file,
    reload_file,
    save_file,
)


# ---------------------------------------------------------------------------
# 读取/编码
# ---------------------------------------------------------------------------

def test_read_utf8_and_bom_variants(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text("héllo wörld", encoding="utf-8")
    loaded = read_file(str(plain))
    assert loaded.text == "héllo wörld" and loaded.encoding == "utf-8"
    assert loaded.bom_declared is False

    bom8 = tmp_path / "bom8.txt"
    bom8.write_bytes(b"\xef\xbb\xbf" + "内容".encode("utf-8"))
    loaded8 = read_file(str(bom8))
    assert loaded8.encoding == "utf-8-sig" and loaded8.bom_declared is True

    utf16 = tmp_path / "u16.txt"
    utf16.write_text("文本", encoding="utf-16")   # 平台 BOM（LE）
    loaded16 = read_file(str(utf16))
    assert loaded16.bom_declared is True
    assert loaded16.text == "文本"


def test_read_declares_fallback_honestly(tmp_path):
    binary = tmp_path / "blob.bin"
    binary.write_bytes(bytes(range(256)))
    loaded = read_file(str(binary))
    assert loaded.encoding == "latin-1" and loaded.bom_declared is False
    # 降级读取按原字节域写回（roundtrip 不损坏）
    saved = save_file(loaded, loaded.text)
    assert tmp_path.joinpath("blob.bin").read_bytes() == bytes(range(256))
    assert saved.encoding == "latin-1"


def test_read_missing_and_io_errors(tmp_path):
    with pytest.raises(EditorFileError) as exc:
        read_file(str(tmp_path / "gone.txt"))
    assert exc.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 外部变更 + 安全保存
# ---------------------------------------------------------------------------

def test_save_is_atomic_and_updates_snapshot(tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("v1", encoding="utf-8")
    loaded = read_file(str(target))
    saved = save_file(loaded, "v2-内容")
    assert target.read_text(encoding="utf-8") == "v2-内容"
    assert saved.disk_matches() is True
    assert not (tmp_path / "doc.txt.part").exists()   # 无残留


def test_save_refuses_external_change(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("mine", encoding="utf-8")
    loaded = read_file(str(target))
    # 外部程序改动
    target.write_text("someone else", encoding="utf-8")
    with pytest.raises(EditorFileError) as exc:
        save_file(loaded, "my overwrite")
    assert exc.value.code == "EXTERNAL_CHANGE"
    assert target.read_text(encoding="utf-8") == "someone else"  # 他人改动保留


def test_save_failure_leaves_original_intact(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("original", encoding="utf-8")
    loaded = read_file(str(target))
    broken = type(loaded)(path=str(tmp_path / "no-dir" / "x" / "keep.txt"),
                          text="x", encoding="utf-8", bom_declared=False,
                          mtime_ns=0, size_bytes=0) if False else loaded
    # 目标目录变只读制造 IO 失败（macOS chmod 也可对 tmp 生效）
    import stat as _stat
    os.chmod(tmp_path, _stat.S_IRUSR | _stat.S_IXUSR)
    try:
        with pytest.raises(EditorFileError) as exc:
            save_file(loaded, "failed write")
        assert exc.value.code == "IO_ERROR"
    finally:
        os.chmod(tmp_path, 0o755)
    assert target.read_text(encoding="utf-8") == "original"


def test_reload_restores_disk_truth(tmp_path):
    target = tmp_path / "re.txt"
    target.write_text("before", encoding="utf-8")
    loaded = read_file(str(target))
    target.write_text("after-external", encoding="utf-8")
    restored = reload_file(loaded)
    assert restored.text == "after-external"
    assert restored.disk_matches()           # 快照重锚


# ---------------------------------------------------------------------------
# 窗口：新建/多文件/保存/恢复（等价编辑工具，无旧 routes/JS globals）
# ---------------------------------------------------------------------------

def test_window_new_file_and_multi_file_tabs(qtbot, tmp_path):
    from src.app.ui.code_editor_window import CodeEditorWindow

    first = tmp_path / "a.txt"
    first.write_text("AAA", encoding="utf-8")
    second = tmp_path / "b.txt"
    second.write_text("BBB", encoding="utf-8")
    window = CodeEditorWindow(paths=(str(first), str(second)))
    qtbot.addWidget(window)
    assert window.tabs.count() == 2           # 多文件入口
    window.new_file()                          # 新建入口
    assert window.tabs.count() == 3
    new_tab = window.current()
    assert new_tab.buffer.path == "" and new_tab.text == ""


def test_window_save_and_reload_flow(qtbot, tmp_path):
    from src.app.ui.code_editor_window import CodeEditorWindow

    doc = tmp_path / "flow.txt"
    doc.write_text("v1", encoding="utf-8")
    window = CodeEditorWindow(paths=(str(doc),))
    qtbot.addWidget(window)
    window.current().editor.setPlainText("v2")
    assert window.save_current() is True
    assert doc.read_text(encoding="utf-8") == "v2"
    # 外部变更 → 保存拒绝（提示），重载恢复磁盘
    doc.write_text("v3-external", encoding="utf-8")
    window.current().editor.setPlainText("v3-mine")
    assert window.save_current() is False
    assert "外部" in window.notice.text()
    assert window.reload_current() is True
    assert window.current().text == "v3-external"


def test_window_new_buffer_save_refuses_without_path(qtbot):
    from src.app.ui.code_editor_window import CodeEditorWindow

    window = CodeEditorWindow()
    qtbot.addWidget(window)
    assert window.current().buffer.path == ""
    assert window.save_current() is False      # 未绑定路径如实拒绝
    assert "另存" in window.notice.text()


# ---------------------------------------------------------------------------
# 宿主接线：原生命令可用 + 零 HTTP + singleton
# ---------------------------------------------------------------------------
