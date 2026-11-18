"""G6：image_editor 接管——G 节行验收。

**预览/导出一致、undo、默认安全输出与写入事务，不能照搬直接写原图接口**。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PIL import Image

from src.features.files.image_editor_service import (
    ImageEditorError,
    ImageEditorService,
    default_output_path,
)


def _image(tmp_path: Path, name="photo.png", size=(48, 32)) -> Path:
    path = tmp_path / name
    Image.new("RGB", size, (200, 40, 40)).save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# 预览/导出一致（同一管线）
# ---------------------------------------------------------------------------

def test_preview_and_export_are_identical(tmp_path):
    src = _image(tmp_path)
    service = ImageEditorService(str(src))
    service.apply("rotate-90")
    service.apply("brightness+10")
    preview = service.render()                     # 预览
    target = service.export()                      # 导出（默认路径）
    exported = Image.open(target)
    assert exported.size == preview.size
    import numpy as np
    assert np.array_equal(np.asarray(exported), np.asarray(preview))


def test_unknown_operation_rejected(tmp_path):
    src = _image(tmp_path)
    service = ImageEditorService(str(src))
    with pytest.raises(ValueError):
        service.apply("sharpen")


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------

def test_undo_pops_operations_in_reverse(tmp_path):
    src = _image(tmp_path)
    service = ImageEditorService(str(src))
    service.apply("rotate-90")
    service.apply("flip-h")
    assert service.state.operations == ("rotate-90", "flip-h")
    service.undo()
    assert service.state.operations == ("rotate-90",)
    service.undo()
    assert service.state.operations == ()
    service.undo()                                  # 空栈幂等
    assert service.state.operations == ()
    # 全撤销后渲染 = 原图
    import numpy as np
    assert np.array_equal(np.asarray(service.render()),
                          np.asarray(Image.open(src)))


# ---------------------------------------------------------------------------
# 默认安全输出：绝不写源
# ---------------------------------------------------------------------------

def test_default_output_never_source_and_disambiguates(tmp_path):
    src = _image(tmp_path)
    first = default_output_path(str(src))
    assert Path(first).name == "photo-edited.png"
    assert first != str(src)
    Path(first).write_bytes(b"occupied")
    second = default_output_path(str(src))
    assert Path(second).name == "photo-edited-1.png"


def test_export_to_source_refused_domain_enforced(tmp_path):
    src = _image(tmp_path)
    service = ImageEditorService(str(src))
    service.apply("flip-v")
    with pytest.raises(ImageEditorError) as exc:
        service.export(target=str(src))             # 显式写原图也拒绝
    assert exc.value.code == "OVERWRITE_SOURCE"
    # 原图逐字节未动
    assert Image.open(src).size == (48, 32)
    import numpy as np
    assert np.array_equal(np.asarray(Image.open(src)),
                          np.asarray(Image.new("RGB", (48, 32), (200, 40, 40))))


def test_export_source_stays_readonly(tmp_path):
    """导出成功路径：源保持原样，输出为可读新文件（事务原子交付）。"""
    src = _image(tmp_path)
    before = src.read_bytes()
    service = ImageEditorService(str(src))
    service.apply("rotate-270")
    target = service.export()
    assert src.read_bytes() == before              # 源只读
    assert Image.open(target).size == (32, 48)     # 旋转生效
    assert not os.path.exists(str(target) + ".part")   # 无半成品


# ---------------------------------------------------------------------------
# 写入事务：源漂移拒绝
# ---------------------------------------------------------------------------

def test_export_refuses_source_drift(tmp_path):
    src = _image(tmp_path)
    service = ImageEditorService(str(src))
    service.apply("flip-h")
    # 外部改动源（mtime/size 变）
    Image.new("RGB", (10, 10), (0, 0, 0)).save(src, format="PNG")
    with pytest.raises(ImageEditorError) as exc:
        service.export()
    assert exc.value.code == "SOURCE_DRIFT"
    assert not list(tmp_path.glob("*.part"))


# ---------------------------------------------------------------------------
# 窗口与宿主接线
# ---------------------------------------------------------------------------

def test_window_flow_apply_undo_export(qtbot, tmp_path):
    from src.app.ui.image_editor_window import ImageEditorWindow

    src = _image(tmp_path)
    service = ImageEditorService(str(src))
    window = ImageEditorWindow(service=service)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window._task is None)
    assert window.preview.pixmap() and not window.preview.pixmap().isNull()
    window._apply("rotate-90")
    qtbot.waitUntil(lambda: window._task is None)
    window._undo()
    qtbot.waitUntil(lambda: window._task is None)
    assert "0 步" in window.notice.text()           # undo 到底
    window._apply("flip-v")
    qtbot.waitUntil(lambda: window._task is None)
    window._export()
    qtbot.waitUntil(lambda: window._task is None)
    assert "已导出" in window.notice.text()
    assert (tmp_path / "photo-edited.png").is_file()
    assert Image.open(src).getpixel((0, 0)) == (200, 40, 40)  # 源未被动过
