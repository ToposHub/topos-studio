"""T03b · 功能页签用户入口测试（联系表 / 资料包 / 对照，T13/T14/T15）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.edit.insights import synthetic as syn
from tests.edit.insights.fake_host import FakeHost

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.host_adapter import InjectedHostAdapter
from src.features.edit.insights.window import InsightsWindow


pytestmark = [pytest.mark.edit_ui]


def _adapter_host() -> InjectedHostAdapter:
    st = syn.build_synthetic_state()
    return InjectedHostAdapter(capture_state_fn=lambda: st.state)


def _changed_state():
    """S1 的 CUE_ZH 文本改掉的派生宿主状态（对照 B 槽用）。"""
    from dataclasses import replace as dc_replace

    st = syn.build_synthetic_state()
    sub = st.main.subtitle_tracks[0]
    new_sub = dc_replace(
        sub,
        subtitle_clips=(
            type(sub.subtitle_clips[0])(
                clip_id=syn.CUE_ZH, track_id=syn.TRACK_SUB1,
                timeline_start=10, timeline_duration=60, text="改过的台词",
            ),
            sub.subtitle_clips[1],
        ),
    )
    changed = dc_replace(
        st.main, subtitle_tracks=(new_sub,) + st.main.subtitle_tracks[1:]
    )
    sequences = dict(st.sequences)
    sequences[syn.SEQ_MAIN] = changed
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    return ProjectSequences(project=st.project, sequences=sequences)


@pytest.fixture()
def window(qtbot):
    host = _adapter_host()
    win = InsightsWindow(host)
    qtbot.addWidget(win)
    win.show()
    win.refresh_capture()
    qtbot.waitUntil(lambda: win._snapshot is not None, timeout=5000)
    qtbot.waitUntil(lambda: win.seq_stats_table.rowCount() == 3, timeout=5000)
    yield win
    win.shutdown()


def test_six_tabs_present(window):
    labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert len(labels) == 6  # 搜索/总表/统计/联系表/资料包/对照


def test_contact_sheet_tab_exports(window, tmp_path):
    written = window.export_contact_sheets(str(tmp_path), sequence_id=syn.SEQ_MAIN)
    assert len(written) == 1
    out = Path(written[0])
    assert (out / "sheet.png").exists()
    assert (out / "cs_index.html").exists()
    # 无捕获提示路径
    assert "1" in window.sheet_status.text()
    # 指定未加载序列 → 无输出、状态提示
    assert window.export_contact_sheets(str(tmp_path), sequence_id=syn.SEQ_UNLOADED) == []


def test_package_tab_exports_with_audio_cues(window, tmp_path):
    """资料包含音频 cue CSV（经 adapter 的 capture_state_fn 复用 CueSheet）。"""
    result = window.export_review_package(str(tmp_path), make_zip=True)
    assert result is not None and result.ok, result and result.error
    pkg = Path(result.directory)
    cue_csvs = list(pkg.glob("audio_cues_*.csv"))
    assert len(cue_csvs) == 1  # 只有 S1 有音频轨（S2 无、S3 未加载）
    text = cue_csvs[0].read_text(encoding="utf-8")
    assert syn.CLIP_SFX in text
    assert result.zip_path and Path(result.zip_path).exists()
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    assert any(f["path"].startswith("audio_cues_") for f in manifest["files"])


def test_package_tab_without_capture_reports(qtbot):
    win = InsightsWindow(FakeHost(state_provider=_never))
    qtbot.addWidget(win)
    assert win.export_review_package("/tmp/x") is None
    win.shutdown()


def _never():
    raise AssertionError("not captured")


def test_diff_tab_save_open_compare_flow(window, qtbot, tmp_path):
    # 保存 A = 当前捕获
    path_a = tmp_path / "a.json"
    assert window.save_current_snapshot(str(path_a)) is True
    assert window.load_snapshot_slot("A", str(path_a)) is True

    # B = 内容变化后的新捕获（直接构造，不经宿主）
    st = syn.build_synthetic_state()
    snap_b = capture_readonly_state(
        _changed_state(), capture_id="cap-diff-b", captured_at=2.0
    )
    path_b = tmp_path / "b.json"
    from src.features.edit.insights.diff import save_snapshot_file

    save_snapshot_file(snap_b, path_b)
    assert window.load_snapshot_slot("B", str(path_b)) is True

    diff = window.run_compare()
    assert diff is not None and diff.has_changes
    text = window.diff_view.toPlainText()
    assert "gp-cue-zh" in text and "改过的台词" in text
    assert "不恢复" in text or "not" in text  # 只读声明可见

    # 槽位标签显示捕获前缀
    assert "cap-diff-b"[:8] in window.diff_slots_label.text()


def test_diff_tab_rejects_tampered_file(window, tmp_path):
    path_a = tmp_path / "a.json"
    window.save_current_snapshot(str(path_a))
    data = json.loads(path_a.read_text(encoding="utf-8"))
    data["project"]["name"] = "篡改"
    path_a.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert window.load_snapshot_slot("A", str(path_a)) is False
    assert "读取失败" in window.diff_slots_label.text() or "load failed" in window.diff_slots_label.text()


def test_diff_tab_needs_two_slots(window):
    assert window.run_compare() is None
    assert "A" in window.diff_view.toPlainText()  # 提示先载入两份
