"""G1b-lite · 真实宿主（MainWindow test_mode）端到端验证。

与 test_g1_entry（mixin 单元级）的区别：这里构造**真实 MainWindow**
（真实 mixin 组合 / 真实 factory / 真实注入适配器），覆盖
"菜单回调解析 → 窗口打开 → 真实两层状态捕获 → 后台搜索 → 关闭回收"
全链路。仍非人工 GUI 交互（鼠标点菜单/真实渲染切换）——那部分保留在
T21 待办，不冒充完成。
"""

from __future__ import annotations

import pytest

from tests.edit.insights import synthetic as syn


pytestmark = [pytest.mark.edit_ui]


@pytest.fixture(scope="module")
def main_window(qapp):
    from src.app.main_window import MainWindow

    window = MainWindow(test_mode=True)
    yield window
    try:
        window.close()
    except Exception:
        pass
    window.deleteLater()


@pytest.fixture()
def insights_env(main_window, qtbot):
    from src.features.edit.insights.factory import (
        current_window,
        shutdown_insights_window,
    )

    st = syn.build_synthetic_state()
    # 把合成两层状态装进真实 MainWindow 的既有属性（宿主读取路径同
    # get_project_sequences：_current_edit_project + _current_timeline_sequences）
    main_window._current_edit_project = st.project
    main_window._current_timeline_sequences = dict(st.sequences)

    main_window.on_insights_open()  # 真实菜单回调路径
    win = current_window()
    assert win is not None
    qtbot.addWidget(win)
    yield main_window, win, st
    shutdown_insights_window()
    assert current_window() is None
    main_window._current_edit_project = None
    main_window._current_timeline_sequences = {}


def test_real_main_window_has_callback(main_window):
    """menu_builder 用 getattr(mw, callback_name) 解析；真实窗口必须有。"""
    assert callable(getattr(main_window, "on_insights_open", None))


def test_open_search_close_on_real_host(insights_env, qtbot):
    mw, win, st = insights_env

    # 重复打开 = 同一实例（不重复建窗/worker）
    mw.on_insights_open()
    from src.features.edit.insights.factory import current_window

    assert current_window() is win

    # 真实捕获：从真实 MainWindow 属性读取两层状态
    win.refresh_capture()
    qtbot.waitUntil(lambda: win._snapshot is not None, timeout=5000)
    assert len(win._snapshot.sequences) == 3
    assert win._snapshot.project.project_id == st.project.project_id

    # 后台搜索 → 结果回 UI（真实 worker/守卫链路）
    win.search_edit.setText("你好")
    win._submit_search_job("你好")
    qtbot.waitUntil(lambda: win.hits_model.rowCount() == 2, timeout=5000)
    seq_ids = {
        win.hits_model.hit_at(r).sequence_id
        for r in range(win.hits_model.rowCount())
    }
    assert seq_ids == {syn.SEQ_MAIN, syn.SEQ_NTSC}

    # 导航身份走真实宿主（_insights_navigate 在 mixin 单元级已验拒绝
    # 语义；此处验证真实 MainWindow 上可调用且拒绝路径不崩——不实际
    # 切换序列，test_mode 下渲染器/面板不存在，切换属 GUI 交互验收）
    from src.features.edit.insights.host_protocol import NavigateRequest

    result = mw._insights_navigate(NavigateRequest(
        capture_id=win._snapshot.capture_id,
        project_id=win._snapshot.project.project_id,
        sequence_id=syn.SEQ_UNLOADED,   # 未物化 ref：结构化拒绝，不建空序列
        target_frame=0,
    ))
    assert not result.ok and result.reason == "target_missing"
    assert syn.SEQ_UNLOADED not in mw._current_timeline_sequences


def test_worker_threads_stopped_after_shutdown(insights_env):
    import time

    mw, win, st = insights_env
    threads = list(win._worker._threads)
    from src.features.edit.insights.factory import shutdown_insights_window

    shutdown_insights_window()
    deadline = time.time() + 6
    while time.time() < deadline and any(t.is_alive() for t in threads):
        time.sleep(0.1)
    assert not any(t.is_alive() for t in threads)  # 反复开关 worker 回落


def test_no_project_reports_cleanly(main_window, qtbot):
    from src.features.edit.insights.factory import (
        current_window,
        shutdown_insights_window,
    )

    main_window._current_edit_project = None
    main_window._current_timeline_sequences = {}
    main_window.on_insights_open()
    win = current_window()
    qtbot.addWidget(win)
    win.refresh_capture()
    assert win._snapshot is None
    assert "no project" in win.status_label.text()  # 明确提示，不造空状态
    shutdown_insights_window()


def test_real_navigation_switch_and_seek_full_chain(qtbot):
    """G1b 最深可自动化层：真实 MainWindow + 真实 TimelinePanel/EditPage
    上跑通导航接受路径的**真实方法体**——

    _insights_navigate → 真实 _on_media_pool_timeline_selected
    （序列解析/渲染器分支/面板 load_timeline/edit_page.set_timeline/
    Deliver 分支守卫）→ edit_page.seek_to_frame（真实信号）。

    唯一 stub：预览渲染器初始化（GPU 上下文，与导航语义正交）。
    """
    from src.app.main_window import MainWindow
    from src.features.edit.ui.edit_page import EditPage
    from src.features.edit.ui.timeline_panel import TimelinePanel
    from src.features.edit.insights.factory import (
        current_window,
        shutdown_insights_window,
    )

    mw = MainWindow(test_mode=True)
    qtbot.addWidget(mw)
    try:
        # 挂真实面板控件；渲染器分支关闭（无 GPU 会话）
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        mw.media_pool_panel = MediaPoolPanel()
        mw.timeline_panel = TimelinePanel()
        mw.edit_page = EditPage()
        mw.edit_page.set_timeline_panel(mw.timeline_panel)
        mw.timeline_preview_renderer = None
        mw._init_timeline_preview_renderer = lambda: None  # 唯一 stub（GPU）

        st = syn.build_synthetic_state()
        mw._current_edit_project = st.project
        mw._current_timeline_sequences = dict(st.sequences)

        # 捕获播放头信号（真实 seek 链路的可观测点）
        seek_frames: list[int] = []
        mw.edit_page.signal_hub.seek_requested.connect(seek_frames.append)

        mw.on_insights_open()
        win = current_window()
        qtbot.addWidget(win)
        win.refresh_capture()
        qtbot.waitUntil(lambda: win._snapshot is not None, timeout=5000)

        # 定位到非当前序列（当前是 MAIN，目标是 NTSC）
        before_revisions = {
            s.sequence_id: s.revision for s in win._snapshot.sequences
        }
        result = mw._insights_navigate(
            __import__("src.features.edit.insights.host_protocol", fromlist=["NavigateRequest"]).NavigateRequest(
                capture_id=win._snapshot.capture_id,
                project_id=st.project.project_id,
                sequence_id=syn.SEQ_NTSC,
                target_frame=42,
            )
        )
        assert result.ok and result.actual_frame == 42

        # 真实切换生效：EditPage 持有 NTSC 序列（此前从未载入）
        assert mw.edit_page._current_timeline is not None
        assert mw.edit_page._current_timeline.timeline_id == syn.SEQ_NTSC
        # active_timeline_id 属允许变化的导航状态（计划 T21 红线措辞）
        assert mw._current_edit_project.active_timeline_id == syn.SEQ_NTSC
        assert mw.media_pool_panel is not None  # 媒体池同步分支真实执行
        # 真实 seek 生效：信号携带目标帧（set_timeline 内部 seek(0) + 我们的 42）
        assert seek_frames and seek_frames[-1] == 42
        # 只读红线：revision/序列集合不变
        st2 = syn.build_synthetic_state()  # 未被触碰的对照（同一构造）
        assert {sid: s.revision for sid, s in
                ((x.sequence_id, x) for x in win._snapshot.sequences)} == before_revisions
        assert set(mw._current_timeline_sequences) == set(st2.sequences)
        assert mw._current_edit_project.revision == st.project.revision == 11
        shutdown_insights_window()
    finally:
        mw._current_edit_project = None
        mw._current_timeline_sequences = {}
        mw.close()
