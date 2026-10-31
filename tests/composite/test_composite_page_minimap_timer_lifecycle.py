"""T-P1-71: CompositePage minimap 防抖 timer 生命周期回归。

根因（composite 套件间歇性 SIGSEGV，.ips 栈 + FIRE 插桩定位）：
``_minimap_timer.timeout`` 曾连接到 ``__init__`` 的 lambda——lambda 槽不带
接收者上下文，页面 ``deleteLater`` 与 Python 包装生命周期错位时，已武装的
50ms 单发 timer 把信号发进已释放的槽 PyObject，在 ``PyQtSlot::invoke``
内段错误（复现对：test_3d_camera_light_import + test_composite_cornerpin_
freeze，约 75% 崩溃率；把 lambda 强引用后 0 崩溃）。

修复形态：绑定方法 ``_update_minimap_deferred``（PyQt 注册接收者上下文，
析构自动断开）。本测试在"timer 已武装 + 页面销毁 + 事件循环推进"的竞态
窗口上重复 20 次；lambda 回归时此处段错误或被 pytestqt 以事件循环异常
暴露，绑定方法形态下静默通过。
"""
import gc
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.composite_core


def _exercise_minimap_cycle(qtbot) -> None:
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage(parent=None)
    try:
        # 武装 50ms 单发 minimap 防抖 timer（graph_changed 的合并路径）。
        page._update_minimap()
        assert page._minimap_timer.isActive()
    finally:
        page.deleteLater()
    # 关键窗口：timer 在飞、页面已排入延迟删除、Python 侧无强引用——
    # 事件循环推进时 timer 触发是否打进悬空槽取决于连接形态。
    del page
    gc.collect()
    qtbot.wait(80)


def test_minimap_timer_lifecycle_20_cycles(qtbot, qapp):
    """20 次“武装→销毁→事件推进”循环：无段错误、无事件循环异常。"""
    for _ in range(20):
        _exercise_minimap_cycle(qtbot)


def test_minimap_deferred_slot_is_bound_method(qtbot, qapp):
    """防御性契约：防抖回调必须是绑定方法（lambda 形态即回归）。"""
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage(parent=None)
    try:
        import types

        page._update_minimap()
        assert isinstance(
            page._update_minimap_deferred, types.MethodType)
        assert page._minimap_timer.isActive()
        page._minimap_timer.stop()
    finally:
        page.deleteLater()


def test_frame_ready_handler_survives_page_destruction(qtbot, qapp):
    """T-P1-71 第二处段错误回归：VideoContext 单例回调 vs 页面生命周期。

    旧形态把 ``self._video_node_frame_ready.emit``（C 函数槽，无接收者
    上下文）注册进全局单例——页面析构 + 包装回收后，解码 worker 线程
    调用 notify_frame_ready 即原生段错误（deflicker 真实视频套件崩溃，
    try/except 挡不住）。修复形态：弱引用蹦床（页面回收后回调 no-op）。
    另验证显式注销 API 的身份守卫。
    """
    import threading

    from src.features.composite.core.video_context import VideoContext
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage(parent=None)
    ctx = VideoContext.get_instance()
    handler = page._frame_ready_handler_ref
    with ctx._lock:
        assert ctx._frame_ready_handler is handler

    page.deleteLater()
    del page
    gc.collect()
    qtbot.wait(20)  # 处理延迟删除（C++ 析构）

    # worker 线程与主线程调用都必须安全（旧形态在此处段错误；
    # 蹦床形态：弱引用失效 → no-op）。
    t = threading.Thread(target=ctx.notify_frame_ready)
    t.start()
    t.join()
    ctx.notify_frame_ready()

    # 显式注销：仅当单例仍指向同一蹦床时清除（身份守卫）。
    ctx.clear_frame_ready_handler(handler)
    with ctx._lock:
        assert ctx._frame_ready_handler is None
    # 再次调用：无 handler 时安全返回。
    ctx.notify_frame_ready()
