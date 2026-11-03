"""Color 套件会话级约束（P0-QA-01）。

历史上有测试直接改写仓库根文件、删除生产 cache。本门禁保证 Color 套件
运行前后仓库工作区状态一致（git status --porcelain 无新增差异），
任何测试静默改写已跟踪文件或制造未跟踪产物都会让套件失败，
而不是把回归留在开发者工作区里。
"""

import os
import subprocess
from pathlib import Path

import pytest

# macOS 原生 'macos' QPA 平台下，同进程内先创建 standalone ModernGL/Metal
# 上下文再创建 Qt 控件会以约 50% 概率在 QWidget 创建时触发 Segmentation
# fault（test_plugin_params.py 处复现）。offscreen 平台不触碰原生 NSWindow
# 状态，合并套件可稳定通过；必须在首个 QApplication 创建前设置。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _flush_qt_deferred_deletes_at_teardown():
    """每个测试收尾投递 Qt 延迟删除（P0-QA-02）。

    ``QCoreApplication.processEvents()`` 不投递 DeferredDelete 事件，
    pytest-qt 的 ``_close_widgets``（close+deleteLater）里的 deleteLater
    在无真实事件循环的测试进程里等于静默空操作——C++ 控件树连同其
    pending QTimer 泄漏存活到测试之后，定时器在无关测试的事件处理中
    发射；其 Python 可调用对象一旦先被 GC 回收，分发即落进已释放内存
    （确定性复现：全量字母序运行在 test_clip_version_ui 提交的 15s 待
    决请求定时器 +15s 处 "line ??? in <lambda>" 段错误）。显式投递让
    每个测试的 C++ 树真正在本测试收尾时销毁。勿在此追加每测试
    ``gc.collect()``：实测会把引用环回收提前到运行中段，在
    test_presets_gallery 的 EXR 写入处以 SIGILL 激化（自动分代回收在同
    配置下全量绿，无需额外收口）。
    """
    yield
    from PyQt6.QtCore import QCoreApplication, QEvent
    app = QCoreApplication.instance()
    if app is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _porcelain():
    """返回 git status --porcelain 行列表；git 不可用时返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


@pytest.fixture(scope="session", autouse=True)
def repo_stays_clean():
    """套件结束后断言工作区与进入时完全一致（跟踪文件与未跟踪产物）。"""
    before = _porcelain()
    yield
    after = _porcelain()
    if before is None or after is None:
        return
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    assert not added and not removed, (
        "Color 测试套件修改了仓库工作区（P0-QA-01 门禁）：\n"
        f"  新增：{added}\n  消失：{removed}\n"
        "测试必须使用 tmp_path，禁止写入仓库文件或生产 cache。"
    )
