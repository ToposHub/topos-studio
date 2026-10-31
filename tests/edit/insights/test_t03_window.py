"""T03 · 统一洞察窗口测试（offscreen Qt；组件级证据，非真实宿主验收）。"""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace

import pytest

from tests.edit.insights import synthetic as syn
from tests.edit.insights.fake_host import FakeHost

from src.features.edit.insights.dto import LOAD_STATE_LOADED
from src.features.edit.insights.runtime import JobResult
from src.features.edit.insights.search import SearchQuery, unified_search
from src.features.edit.insights.window import InsightsWindow


pytestmark = [pytest.mark.edit_ui]


def _make_host() -> FakeHost:
    st = syn.build_synthetic_state()
    return FakeHost(state_provider=lambda: st.state)


@pytest.fixture()
def window(qtbot):
    host = _make_host()
    win = InsightsWindow(host)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.shutdown()


def test_refresh_capture_and_async_search(window, qtbot):
    window.refresh_capture()
    qtbot.waitUntil(lambda: window.hits_model.rowCount() >= 0, timeout=5000)
    window.search_edit.setText("你好")
    window._submit_search_job("你好")
    qtbot.waitUntil(lambda: window.hits_model.rowCount() == 2, timeout=5000)
    # 状态如实报告未加载排除
    assert "未加载" in window.search_status.text() or "not loaded" in window.search_status.text()
    # 统计页已异步填充（3 个序列行）
    qtbot.waitUntil(lambda: window.seq_stats_table.rowCount() == 3, timeout=5000)


def test_same_text_hits_distinguishable_by_sequence(window, qtbot):
    window.refresh_capture()
    window._submit_search_job("你好")
    qtbot.waitUntil(lambda: window.hits_model.rowCount() == 2, timeout=5000)
    seq_ids = {
        window.hits_model.hit_at(r).sequence_id for r in range(window.hits_model.rowCount())
    }
    assert seq_ids == {syn.SEQ_MAIN, syn.SEQ_NTSC}


def test_same_name_sequences_distinguishable_in_scope(qtbot):
    """两条同名序列：范围下拉与结果行都靠 id 前缀区分。"""
    base = syn.build_synthetic_state()
    renamed_ntsc = dc_replace(base.ntsc, name=base.main.name)  # 与主时间线同名

    def provider():
        sequences = dict(base.sequences)
        sequences[syn.SEQ_NTSC] = renamed_ntsc
        from src.features.edit.ai_editing.edit_project_service import ProjectSequences

        return ProjectSequences(project=base.project, sequences=sequences)

    host = FakeHost(state_provider=provider)
    win = InsightsWindow(host)
    qtbot.addWidget(win)
    win.refresh_capture()
    labels = [win.scope_combo.itemText(i) for i in range(win.scope_combo.count())]
    assert len(labels) == 3  # 全部 + 2 条已加载
    assert labels[1] != labels[2]  # 同名但 id 前缀不同
    assert syn.SEQ_NTSC[:8] in labels[2]
    win.shutdown()


def test_stale_capture_results_rejected(window, qtbot):
    window.refresh_capture()
    window._submit_search_job("你好")
    qtbot.waitUntil(lambda: window.hits_model.rowCount() == 2, timeout=5000)

    # 构造一份"旧捕获"的结果：capture_id 不再是当前值
    snapshot_before = window._snapshot
    stale_search = unified_search(snapshot_before, SearchQuery(text="Cam"))
    stale = JobResult(
        job_id="stale-job",
        capture_id=snapshot_before.capture_id,
        ok=True,
        value=stale_search,
    )
    window.refresh_capture()  # 新捕获 → 旧 capture_id 全部过时
    qtbot.waitUntil(lambda: window.seq_stats_table.rowCount() == 3, timeout=5000)
    rows_before = window.hits_model.rowCount()
    status_before = window.search_status.text()
    window._on_result(stale)  # 迟到的旧捕获结果
    assert window.hits_model.rowCount() == rows_before
    assert window.search_status.text() == status_before  # UI 不被旧结果改写


def test_navigation_only_via_host_callback(window, qtbot):
    window.refresh_capture()
    window._submit_search_job("你好")
    qtbot.waitUntil(lambda: window.hits_model.rowCount() == 2, timeout=5000)
    assert window._host.navigate_calls == []  # 刷新/搜索不触发导航
    window._navigate_index(window.hits_model.index(0, 0))
    assert len(window._host.navigate_calls) == 1
    call = window._host.navigate_calls[0]
    assert call.sequence_id in (syn.SEQ_MAIN, syn.SEQ_NTSC)
    assert call.entity_id in (syn.CUE_ZH, syn.CUE_SHARED)
    assert call.capture_id == window._snapshot.capture_id


def test_navigation_rejection_shown_in_status(window, qtbot):
    window._host.navigate_ok = False
    window.refresh_capture()
    window._submit_search_job("你好")
    qtbot.waitUntil(lambda: window.hits_model.rowCount() == 2, timeout=5000)
    window._navigate_index(window.hits_model.index(0, 0))
    assert "拒绝" in window.search_status.text() or "rejected" in window.search_status.text()


def test_input_focus_does_not_navigate(window, qtbot):
    window.refresh_capture()
    window.search_edit.setFocus()
    qtbot.waitUntil(lambda: window.search_edit.hasFocus(), timeout=5000)
    assert window._host.navigate_calls == []  # 聚焦输入框不产生导航/宿主调用


def test_close_window_then_late_result_ignored(qtbot):
    host = _make_host()
    win = InsightsWindow(host)
    qtbot.addWidget(win)
    win.refresh_capture()
    snapshot = win._snapshot
    win.close()  # closeEvent → shutdown
    late = JobResult(
        job_id="late", capture_id=snapshot.capture_id, ok=True,
        value=unified_search(snapshot, SearchQuery(text="你好")),
    )
    win._on_result(late)  # 关闭后迟到结果：不改 UI、不崩溃
    assert win.hits_model.rowCount() == 0


def test_export_report_files(window, qtbot, tmp_path):
    window.refresh_capture()
    qtbot.waitUntil(lambda: window.seq_stats_table.rowCount() == 3, timeout=5000)
    written = window.export_report_files(str(tmp_path))
    assert len(written) == 3
    csv_path, json_path, html_path = (tmp_path / "report.csv", tmp_path / "report.json", tmp_path / "report.html")
    assert csv_path.exists() and json_path.exists() and html_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload["clips"]) == 6  # 手算：S1 5 + S2 1
    # 临时目录已清理（只留最终产物）
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("insights-")]
    assert leftovers == []


def test_no_capture_states(window):
    assert window.export_report_files("/tmp/should-not-write") == []
    window.search_edit.setText("你好")
    window._submit_search_job("你好")
    assert window.hits_model.rowCount() == 0
    assert "刷新" in window.search_status.text() or "refresh" in window.search_status.text()


def test_capture_failure_reported(qtbot):
    host = _make_host()
    host.fail_capture = True
    win = InsightsWindow(host)
    qtbot.addWidget(win)
    win.refresh_capture()
    assert "no project" in win.status_label.text()
    win.shutdown()


def test_locale_files_key_parity():
    """zh/en 词条键集一致（缺失回退英文默认，但键本身必须成对维护）。"""
    from pathlib import Path

    locales = (
        Path(__file__).resolve().parents[3] / "src/features/edit/insights/locales"
    )

    def keys_of(path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))

        def flat(d, prefix=""):
            out = set()
            for k, v in d.items():
                key = f"{prefix}{k}"
                if isinstance(v, dict):
                    out |= flat(v, key + ".")
                else:
                    out.add(key)
            return out

        return flat(data)

    zh = keys_of(locales / "zh_CN.json")
    en = keys_of(locales / "en_US.json")
    assert zh == en
    assert all(k.startswith("insights.") for k in zh)  # 不覆盖旧键


def test_double_click_signal_wiring_navigates(window, qtbot):
    """双击导航路径：经 doubleClicked 信号连接触发（真实鼠标双击的合成
    事件在 offscreen QPA 下不被识别为双击——最小 QTableView 探针验证
    clicked 可达而 doubleClicked 不可达，属平台限制非本窗口缺陷；真实
    桌面双击走同一连接，键盘等价路径见下一测试）。"""
    window.refresh_capture()
    window._submit_search_job("你好")
    qtbot.waitUntil(lambda: window.hits_model.rowCount() == 2, timeout=5000)
    assert window._host.navigate_calls == []

    window.hits_view.doubleClicked.emit(window.hits_model.index(0, 0))
    qtbot.waitUntil(lambda: len(window._host.navigate_calls) == 1, timeout=5000)
    call = window._host.navigate_calls[0]
    assert call.entity_id in (syn.CUE_ZH, syn.CUE_SHARED)
    assert call.capture_id == window._snapshot.capture_id


def test_enter_key_via_real_qtest_event_navigates(window, qtbot):
    """GUI 事件级（真实键盘事件投递）：结果表聚焦 + Enter → 定位。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    window.refresh_capture()
    window._submit_search_job("你好")
    qtbot.waitUntil(lambda: window.hits_model.rowCount() == 2, timeout=5000)
    window.hits_view.setCurrentIndex(window.hits_model.index(1, 0))
    window.hits_view.setFocus()
    qtbot.waitUntil(lambda: window.hits_view.hasFocus(), timeout=5000)

    QTest.keyClick(window.hits_view, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: len(window._host.navigate_calls) == 1, timeout=5000)
    call = window._host.navigate_calls[0]
    assert call.target_frame is not None
    assert call.entity_id in (syn.CUE_ZH, syn.CUE_SHARED)


def test_refresh_button_and_search_button_via_qtest_clicks(window, qtbot):
    """GUI 事件级：真实按钮点击驱动刷新与搜索（不调用内部方法）。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    QTest.mouseClick(window.refresh_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._snapshot is not None, timeout=5000)
    window.search_edit.setText("CamA")
    QTest.mouseClick(window.search_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.hits_model.rowCount() >= 3, timeout=5000)
    labels = {window.hits_model.hit_at(r).entity_id for r in range(window.hits_model.rowCount())}
    assert {syn.CLIP_A1, syn.CLIP_A2, syn.CLIP_NTSC1} <= labels
