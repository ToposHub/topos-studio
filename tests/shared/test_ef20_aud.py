"""批 EF-20 修复测试 —— 8 目录 24 条 Suggestion 级审计发现。

对应修复批次：docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md
条目 AUD-11532/11534/11611、11901/11907/12001、23015/23019、23924/24001/24006、
24902/24910/24911/24913、26302/26312/26405、27644/27718/27801、
37307/37404/37411。

约束：不创建真实 GL 上下文（mock moderngl 对象）；不弹模态对话框
（Qt 控件不 exec）；不真实解码/追踪视频（假 worker / 打桩探测）。
每条修复都有正例 + 反例（回退到旧实现时测试必须失败）。
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
import threading
import time
from dataclasses import replace as dc_replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from PyQt6.QtCore import QEvent, QObject, pyqtSignal

# ---------------------------------------------------------------------- #
# color/tracking（AUD-24902 / 24910 / 24911 / 24913）
# ---------------------------------------------------------------------- #

from src.features.color.tracking import tracking_evaluator as te
from src.features.color.tracking.tracking_service import TrackingService
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.tracking.tracking_worker import TrackingWorker, _CancelToken
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.tracker_2d import (
    TrackDirection, TrackRequest, TrackResult,
)
from src.features.color.tracking.types import TrackStatus, TrackTransform
from src.shared.tracking.types import TrackingMode


_BASE_GEOMETRY: dict = {
    "type": "curve",
    "center": [0.5, 0.5],
    "size": [0.2, 0.2],
    "rotation": 0.0,
    "points": [
        {"pos": [0.4, 0.4], "c1": [0.3, 0.4], "c2": [0.5, 0.4], "fi": 0.5, "type": "smooth"},
        {"pos": [0.6, 0.6], "c1": [0.6, 0.5], "c2": [0.6, 0.7], "fi": 0.5, "type": "smooth"},
    ],
}


def test_aud24902_identity_returns_isolated_copy() -> None:
    """正例：参考帧（单位矩阵）返回与基准几何完全隔离的新结构。"""
    m = np.eye(3)
    result = te.apply_transform_to_geometry(_BASE_GEOMETRY, m, (1920, 1080))
    assert result == _BASE_GEOMETRY
    assert result is not _BASE_GEOMETRY
    # 嵌套容器不共享（渲染链可安全改写返回值）
    result["points"][0]["pos"][0] = 99.0
    result["center"][0] = 99.0
    assert _BASE_GEOMETRY["points"][0]["pos"][0] == 0.4
    assert _BASE_GEOMETRY["center"][0] == 0.5


def test_aud24902_copy_geometry_matches_deepcopy_semantics() -> None:
    """``_copy_geometry`` 与 copy.deepcopy 在几何结构上等价（热路径替代）。"""
    assert hasattr(te, "_copy_geometry"), "热路径轻量拷贝助手被回退删除"
    copied = te._copy_geometry(_BASE_GEOMETRY)
    assert copied == copy.deepcopy(_BASE_GEOMETRY)
    copied["points"][1]["c2"][1] = -1.0
    assert _BASE_GEOMETRY["points"][1]["c2"][1] == 0.7
    # 非容器（标量/tuple）按值/原样共享，与 deepcopy 语义一致
    assert te._copy_geometry(3.5) == 3.5
    tup = (1.0, 2.0)
    assert te._copy_geometry(tup) is tup


def test_aud24902_failed_frame_fallback_is_isolated(qapp=None) -> None:
    """失败帧回退基准几何：返回副本而非共享引用。"""
    store = TrackingStore()
    transform = TrackTransform.from_matrix(3, np.eye(3), 0.9, TrackStatus.FAILED)
    binding = TrackingBinding(
        binding_id="b1", clip_id="c1", shape_id="s1",
        source_frame_start=0, source_frame_end=10,
        transforms={3: transform},
        base_geometry=_BASE_GEOMETRY,
    )
    store.add_binding(binding)
    evaluator = te.TrackingEvaluator(store)
    result = evaluator.evaluate_geometry("c1", "s1", 3, (1920, 1080))
    assert result == _BASE_GEOMETRY
    result["points"][0]["pos"][1] = -5.0
    assert binding.base_geometry["points"][0]["pos"][1] == 0.4


class _FakeWorker(QObject):
    """可控假 worker：信号手动触发（不真跑线程）。"""

    progress = pyqtSignal(int, int, int)
    finished_with_result = pyqtSignal(int, object)
    failed_with_error = pyqtSignal(int, str)

    def __init__(self, request: TrackRequest) -> None:
        super().__init__()
        self.request = request
        self.started = False
        self.cancel_called = False

    def start(self) -> None:
        self.started = True

    def request_cancel(self) -> None:
        self.cancel_called = True

    def wait(self, ms: int = 0) -> bool:
        return True

    def isRunning(self) -> bool:
        return self.started and not self.cancel_called


def _make_service(workers: List[_FakeWorker]) -> TrackingService:
    def factory(request: TrackRequest) -> _FakeWorker:
        w = _FakeWorker(request)
        workers.append(w)
        return w

    service = TrackingService(worker_factory=factory)
    service._fingerprint = lambda media_path: "fp"  # type: ignore[method-assign]
    return service


def _start_task(service: TrackingService) -> int:
    return service.start(
        clip_id="clip1", shape_id="shape1", media_path="/tmp/fake.mp4",
        pattern_region=(0.375, 0.347, 0.25, 0.306),
        search_region=(0.225, 0.197, 0.55, 0.606),
        reference_frame=62, frame_start=0, frame_end=124,
        mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
        directions=(TrackDirection.FORWARD,),
    )


def test_aud24910_finished_worker_removed_by_identity(qapp) -> None:
    """正例：结束信号按对象身份移除 worker——即使 isRunning() 仍为真。"""
    workers: List[_FakeWorker] = []
    service = _make_service(workers)
    gen = _start_task(service)
    key = service.task_key("clip1", "shape1")
    assert service._workers.get(key) == [workers[0]]
    result = TrackResult(
        generation=gen, cancelled=False, transforms={}, observations={},
        diagnostics={},
    )
    # worker 线程尚未完全退出（isRunning() 仍真）——旧实现会把 worker
    # 残留在运行列表里，此后无人移除。
    workers[0].finished_with_result.emit(gen, result)
    assert key not in service._workers or workers[0] not in service._workers[key]
    # 线程仍在收尾：转入保活列表，防止 GC 析构运行中的 QThread
    assert workers[0] in service._zombie_workers


def test_aud24910_drop_worker_signature_has_no_gen(qapp) -> None:
    """反例（回退 tripwire）：``_drop_worker`` 不再携带误导性 gen 参数。"""
    sig = inspect.signature(TrackingService._drop_worker)
    assert "gen" not in sig.parameters


def test_aud24911_binding_index_maintained() -> None:
    """正例：(clip, shape) 二级索引随增删改查同步维护，get_binding O(1)。"""
    store = TrackingStore()
    assert hasattr(store, "_by_shape"), "get_binding 二级索引被回退删除"
    b1 = TrackingBinding(binding_id="b1", clip_id="c1", shape_id="s1")
    b2 = TrackingBinding(binding_id="b2", clip_id="c1", shape_id="s2")
    store.add_binding(b1)
    store.add_binding(b2)
    assert store._by_shape == {("c1", "s1"): "b1", ("c1", "s2"): "b2"}
    assert store.get_binding("c1", "s1") is b1
    assert store.get_binding("c1", "s2") is b2
    assert store.get_binding("c1", "missing") is None

    # remove → 索引同步失效
    store.remove_binding("b1")
    assert store.get_binding("c1", "s1") is None
    assert ("c1", "s1") not in store._by_shape

    # replace → 索引指向新 binding
    b3 = TrackingBinding(binding_id="b3", clip_id="c1", shape_id="s2")
    store.add_binding(b3, replace=True)
    assert store.get_binding("c1", "s2") is b3

    # from_dict 重建索引；clear 清空
    restored = TrackingStore.from_dict(store.to_dict())
    assert restored.get_binding("c1", "s2") is not None
    assert restored.get_binding("c1", "s2").binding_id == "b3"
    restored.clear()
    assert restored.get_binding("c1", "s2") is None
    assert restored._by_shape == {}


def test_aud24913_cancel_token_backed_by_event() -> None:
    """正例：_CancelToken 用 threading.Event 承载跨线程取消。"""
    token = _CancelToken()
    assert isinstance(token._event, threading.Event), \
        "取消 token 应由 threading.Event 背书"
    assert token.is_set() is False
    thread = threading.Thread(target=token.set)
    thread.start()
    thread.join(2.0)
    assert token.is_set() is True
    # tracker 协议兼容（is_set 可调用对象）
    assert callable(getattr(token, "is_set"))


def test_aud24913_worker_request_cancel_flow(qapp) -> None:
    """worker.request_cancel → cancel_requested 经 Event 生效。"""
    request = TrackRequest(
        media_path="x", pattern_region=(0, 0, 1, 1), search_region=(0, 0, 1, 1),
        reference_frame=0, frame_start=0, frame_end=1,
        mode=TrackingMode.TRANSLATION, direction=TrackDirection.FORWARD,
        generation=1,
    )
    worker = TrackingWorker(request)
    assert worker.cancel_requested() is False
    worker.request_cancel()
    assert worker.cancel_requested() is True


# ---------------------------------------------------------------------- #
# ai_workflow/ui（AUD-23924 / 24001 / 24006）
# ---------------------------------------------------------------------- #


class _FakeRegistry:
    def __init__(self, entries: List[Any]) -> None:
        self._entries = entries

    def all_templates(self) -> List[Any]:
        return list(self._entries)


def _gallery_entries() -> List[Any]:
    return [
        SimpleNamespace(
            template_id="title", display_name="Title",
            description="A title card", category="basic",
        ),
        SimpleNamespace(
            template_id="formula", display_name="Formula",
            description="A formula card", category="basic",
        ),
    ]


def test_aud23924_double_click_card_selects_template(qapp) -> None:
    """正例：双击卡片 emit template_selected（文档承诺的实现补齐）。"""
    from src.features.ai_workflow.ui.workflow_gallery_page import TemplateGalleryPage

    page = TemplateGalleryPage(_FakeRegistry(_gallery_entries()))
    picked: List[str] = []
    page.template_selected.connect(picked.append)
    cards = [
        page._make_card(entry) for entry in _gallery_entries()
    ]
    dbl = QEvent(QEvent.Type.MouseButtonDblClick)
    handled = page.eventFilter(cards[1], dbl)
    assert handled is True
    assert picked == ["formula"]
    for card in cards:
        card.deleteLater()
    page.deleteLater()


def test_aud23924_single_click_not_intercepted(qapp) -> None:
    """反例：非双击事件不被 eventFilter 拦截、不触发选择。"""
    from src.features.ai_workflow.ui.workflow_gallery_page import TemplateGalleryPage

    page = TemplateGalleryPage(_FakeRegistry(_gallery_entries()))
    picked: List[str] = []
    page.template_selected.connect(picked.append)
    card = page._make_card(_gallery_entries()[0])
    single = QEvent(QEvent.Type.MouseButtonPress)
    assert page.eventFilter(card, single) is False
    assert picked == []
    card.deleteLater()
    page.deleteLater()


class _FakeSpecRegistry:
    def __init__(self, specs: List[Any]) -> None:
        self._specs = specs

    def all_specs(self) -> List[Any]:
        return list(self._specs)


def _search_specs() -> List[Any]:
    long_tail = "x" * 60 + "UNIQUE-TAIL-KEYWORD"
    return [
        SimpleNamespace(
            spec_id="node_a", display_name="Node A", description=long_tail,
        ),
        SimpleNamespace(
            spec_id="node_b", display_name="Node B", description="short",
        ),
    ]


def test_aud24001_filter_matches_full_description(qapp) -> None:
    """正例：过滤匹配完整 description（含 48 字符截断点之后的关键字）。"""
    from src.features.ai_workflow.ui.workflow_node_search import (
        WorkflowNodeSearchPopup,
    )

    popup = WorkflowNodeSearchPopup(_FakeSpecRegistry(_search_specs()))
    popup._filter_items("unique-tail-keyword")
    item_a = popup.list_widget.item(0)
    item_b = popup.list_widget.item(1)
    # 显示文案截断到 48 字符——旧实现按截断后的 item.text() 匹配必然
    # 失配；新实现经 DescriptionRole 匹配完整描述。
    assert "unique-tail-keyword" not in item_a.text().lower()
    assert item_a.isHidden() is False
    assert item_b.isHidden() is True
    popup.deleteLater()


def test_aud24001_filter_still_matches_display_and_id(qapp) -> None:
    """display_name / spec_id 匹配行为保持不变。"""
    from src.features.ai_workflow.ui.workflow_node_search import (
        WorkflowNodeSearchPopup,
    )

    popup = WorkflowNodeSearchPopup(_FakeSpecRegistry(_search_specs()))
    popup._filter_items("node b")
    assert popup.list_widget.item(0).isHidden() is True
    assert popup.list_widget.item(1).isHidden() is False
    popup._filter_items("NODE_A")
    assert popup.list_widget.item(0).isHidden() is False
    popup.deleteLater()


def _make_panel(qapp):
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    from src.features.ai_workflow.core.template_registry import (
        WorkflowTemplateRegistry, register_official_templates,
    )
    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel

    node_reg = NodeSpecRegistry()
    register_official_specs(node_reg)
    tpl_reg = WorkflowTemplateRegistry()
    register_official_templates(tpl_reg)
    panel = WorkflowPanel(registry=node_reg, template_registry=tpl_reg)
    return panel


def test_aud24006_set_run_drawer_reentry_guarded(qapp) -> None:
    """正例：``_updating`` 被真实读取——嵌套 set_run_drawer 被忽略。"""
    panel = _make_panel(qapp)
    from src.features.edit.ui.ai_assistant_composer import RunDrawerState

    inner_calls: List[Any] = []

    def reentrant_set_state(state: Any) -> None:
        inner_calls.append(state)
        # 模拟 drawer 状态更新反向触发 controller 再灌回（信号递归）
        panel.set_run_drawer(RunDrawerState())

    panel.run_drawer.set_state = reentrant_set_state  # type: ignore[method-assign]
    panel.set_run_drawer(RunDrawerState())
    assert len(inner_calls) == 1, "嵌套 set_run_drawer 未被 _updating 锁拦截"
    assert panel._updating is False
    panel.deleteLater()


def test_aud24006_artifact_click_dropped_while_updating(qapp) -> None:
    """``_updating`` 期间程序性 artifact_clicked 被丢弃；正常转发保持。"""
    panel = _make_panel(qapp)
    forwarded: List[str] = []
    panel.artifact_requested.connect(forwarded.append)

    # 正常路径（用户点击，事件循环轮转后 _updating 已复位）
    panel.run_drawer.artifact_clicked.emit("asset-1")
    assert forwarded == ["asset-1"]

    # set_state 重绘期间的程序性信号 → 丢弃，阻断 drawer→panel→drawer 环
    panel._updating = True
    panel.run_drawer.artifact_clicked.emit("asset-2")
    assert forwarded == ["asset-1"]
    panel._updating = False
    panel.deleteLater()


# ---------------------------------------------------------------------- #
# composite/utils（AUD-11532 / 11534 / 11611）
# ---------------------------------------------------------------------- #


def test_aud11532_save_atomic_and_tmp_cleaned(tmp_path: Path) -> None:
    """正例：保存走临时文件 + os.replace，成功后无 .tmp 残留。"""
    from src.features.composite.utils.preset_manager import PresetManager

    mgr = PresetManager(presets_dir=str(tmp_path))
    assert mgr.save_preset("alpha", {"nodes": [], "connections": []})
    target = tmp_path / "alpha.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["name"] == "alpha"
    assert list(tmp_path.glob("*.tmp")) == []


def test_aud11532_midwrite_crash_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反例（回退 tripwire）：写入中途崩溃后旧文件必须完整保留。

    模拟方式：让 json.dump 写一半后抛 IOError。旧实现 open("w") 先截断
    目标文件 → 旧内容丢失（本测试失败）；新实现写的是临时文件，
    目标文件原样保留，且 .tmp 被清理。
    """
    import src.features.composite.utils.preset_manager as pm_module
    from src.features.composite.utils.preset_manager import PresetManager

    mgr = PresetManager(presets_dir=str(tmp_path))
    assert mgr.save_preset("keepme", {"nodes": [1, 2, 3], "connections": []})
    target = tmp_path / "keepme.json"
    original = target.read_text(encoding="utf-8")

    real_dump = json.dump

    def half_write_then_die(obj: Any, fh: Any, **kw: Any) -> None:
        fh.write("{ half written")
        raise IOError(28, "simulated crash mid-write")

    monkeypatch.setattr(pm_module.json, "dump", half_write_then_die)
    assert mgr.save_preset("keepme", {"nodes": [9], "connections": []}) is False
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == []
    assert real_dump is json.dump


def test_aud11532_all_write_sites_use_atomic_helper() -> None:
    """五个持久化写入点全部走 _atomic_write_json。"""
    import src.features.composite.utils.preset_manager as pm_module

    src = inspect.getsource(pm_module)
    assert src.count("_atomic_write_json(") >= 6  # 1 def + 5 call sites
    assert 'open(file_path, "w"' not in src
    assert 'open(dest_path, "w"' not in src
    assert 'open(new_path, "w"' not in src


def test_aud11534_default_dir_anchored_to_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正例：默认预设目录 = 项目根/presets，不随启动 cwd 漂移。"""
    from src.features.composite.utils.preset_manager import PresetManager
    from src.shared.paths.paths import get_project_root

    monkeypatch.chdir(tmp_path)
    mgr = PresetManager()
    expected = Path(get_project_root()) / "presets"
    assert mgr.get_presets_dir() == expected
    assert mgr.get_presets_dir() != tmp_path / "presets"


def test_aud11611_incremental_saver_lock_present() -> None:
    """正例：IncrementalSaver 持 RLock，读写在锁内完成。"""
    from src.features.composite.utils.serialization_formats import IncrementalSaver

    saver = IncrementalSaver()
    assert isinstance(saver._lock, type(threading.RLock()))
    graph = {"nodes": [{"id": "a"}], "connections": [], "fps": 30.0}
    saver.set_base_snapshot(graph, generation=1)
    delta = saver.compute_delta({**graph, "fps": 60.0})
    assert delta["delta_type"] == "incremental"
    assert delta["base_version"] == 1
    saver.clear_base_snapshot()
    assert saver.has_base_snapshot is False


def test_aud11611_concurrent_base_swap_never_torn() -> None:
    """并发 set_base_snapshot / compute_delta / save_incremental 不崩不脏读。"""
    from src.features.composite.utils.serialization_formats import (
        IncrementalSaver, save_incremental,
    )

    saver = IncrementalSaver()
    errors: List[BaseException] = []

    def writer(tag: str) -> None:
        try:
            for i in range(50):
                saver.set_base_snapshot({"nodes": [{"id": tag}], "n": i}, generation=i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def saver_thread() -> None:
        try:
            for i in range(50):
                save_incremental(
                    {"nodes": [], "n": i}, saver,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{t}",)) for t in range(3)]
    threads.append(threading.Thread(target=saver_thread))
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
    assert errors == []
    assert saver.base_generation >= 0


# ---------------------------------------------------------------------- #
# deliver/core（AUD-11901 / 11907 / 12001）
# ---------------------------------------------------------------------- #


def test_aud11901_vanished_file_mtime_failure_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正例：旧格式预设缺 created_at 且 getmtime 失败 → 降级 0.0，不炸列表。"""
    from src.features.deliver.core import custom_export_presets as cep

    directory = tmp_path / "user-presets"
    directory.mkdir()
    (directory / "legacy.json").write_text(json.dumps({
        "name": "legacy",
        "video_config": {},
    }), encoding="utf-8")
    monkeypatch.setattr(cep, "user_export_presets_dir", lambda: str(directory))

    def raise_oserror(_path: str) -> float:
        raise OSError(2, "vanished between listdir and getmtime")

    monkeypatch.setattr(cep.os.path, "getmtime", raise_oserror)
    presets = cep.list_user_presets()
    assert [p.fallback_title for p in presets] == ["legacy"]


def test_aud11901_mtime_success_still_sorts_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反例回归：getmtime 正常时排序行为不变（旧者在前）。"""
    from src.features.deliver.core import custom_export_presets as cep

    directory = tmp_path / "user-presets"
    directory.mkdir()
    (directory / "a-old.json").write_text(json.dumps({
        "name": "a-old", "created_at": 100.0, "video_config": {},
    }), encoding="utf-8")
    (directory / "b-new.json").write_text(json.dumps({
        "name": "b-new", "created_at": 200.0, "video_config": {},
    }), encoding="utf-8")
    monkeypatch.setattr(cep, "user_export_presets_dir", lambda: str(directory))
    presets = cep.list_user_presets()
    assert [p.fallback_title for p in presets] == ["a-old", "b-new"]


def test_aud11907_clear_completed_removes_all_terminal_jobs() -> None:
    """正例：字段全同的 DONE 作业被逐条移除，PENDING 作业保留。"""
    from src.features.deliver.core.export_queue import (
        ExportJob, ExportQueue, JobStatus,
    )

    queue = ExportQueue(runner=None)
    done_a = ExportJob(job_id="d1", name="x", output_path="/tmp/x.mp4",
                       status=JobStatus.DONE)
    done_b = ExportJob(job_id="d2", name="x", output_path="/tmp/x.mp4",
                       status=JobStatus.DONE)
    pending = ExportJob(job_id="p1", name="x", output_path="/tmp/x.mp4",
                        status=JobStatus.PENDING)
    for job in (done_a, done_b, pending):
        queue._jobs.append(job)
    queue._by_id = {j.job_id: j for j in queue._jobs}
    removed: List[str] = []
    queue.job_removed.connect(removed.append)
    queue.clear_completed()
    assert [j.job_id for j in queue._jobs] == ["p1"]
    assert set(queue._by_id) == {"p1"}
    assert sorted(removed) == ["d1", "d2"]
    assert queue.get_job("p1") is pending


def test_aud11907_no_value_equality_membership() -> None:
    """反例（回退 tripwire）：不再使用 O(n²) 值相等成员判断。"""
    from src.features.deliver.core.export_queue import ExportQueue

    src = inspect.getsource(ExportQueue.clear_completed)
    assert "if j not in keep" not in src
    assert "status not in active_statuses" in src


def test_aud12001_no_phantom_container_override_field() -> None:
    """正例：VideoPreset 文档不再声明不存在的 container_override 字段。"""
    from src.features.deliver.core.video_export_presets import VideoPreset

    assert VideoPreset.__doc__ is not None
    assert "container_override" not in VideoPreset.__doc__
    field_names = {f.name for f in VideoPreset.__dataclass_fields__.values()}
    assert "container_override" not in field_names


# ---------------------------------------------------------------------- #
# shared/effects（AUD-26302 / 26312 / 26405）
# ---------------------------------------------------------------------- #


def test_aud26302_docstring_corrected_and_mapping_still_works() -> None:
    """正例：文档更正 + 纯映射助手行为保持。"""
    from src.features.shared.effects.dctl_wrapper import default_uniform_values

    doc = default_uniform_values.__doc__ or ""
    assert "AUD-26302" in doc
    assert "Used by EffectCatalog" not in doc
    values = default_uniform_values([
        {"type": "slider", "var_name": "amount", "default": 0.5},
        {"type": "checkbox", "var_name": "on", "default": 1},
        {"type": "color", "var_name": "tint", "default": [0.1, 0.2, 0.3]},
        {"type": "select", "var_name": "mode", "default": 2},
        {"type": "slider", "var_name": "", "default": 9.0},  # 无 var → 跳过
    ])
    assert values == {
        "u_amount": 0.5,
        "u_on": 1,
        "u_tint": (0.1, 0.2, 0.3),
        "u_mode": 2,
    }


class _FakeGLObj:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeCtx:
    """可编程失败的假 moderngl ctx（不创建真实 GL 上下文）。

    ``armed`` 为 False 时（构造期）从不失败——``EffectPassRunner.__init__``
    自身要创建 quad vbo；置位后按 ``fail_at`` 在对应步骤抛错。
    """

    def __init__(self, fail_at: str = "") -> None:
        self.fail_at = fail_at
        self.armed = False
        self.created: List[_FakeGLObj] = []

    def program(self, *, vertex_shader: str, fragment_shader: str) -> _FakeGLObj:
        if self.armed and self.fail_at == "program":
            raise RuntimeError("program compile failed")
        obj = _FakeGLObj("program")
        self.created.append(obj)
        return obj

    def buffer(self, data: bytes) -> _FakeGLObj:
        if self.armed and self.fail_at == "buffer":
            raise RuntimeError("buffer alloc failed")
        obj = _FakeGLObj("buffer")
        self.created.append(obj)
        return obj

    def vertex_array(self, prog: Any, bindings: Any) -> _FakeGLObj:
        if self.armed and self.fail_at == "vertex_array":
            raise RuntimeError("vao setup failed")
        obj = _FakeGLObj("vertex_array")
        self.created.append(obj)
        return obj


def _make_runner(fail_at: str) -> Any:
    from src.features.shared.effects.effect_pass_runner import EffectPassRunner

    ctx = _FakeCtx(fail_at=fail_at)
    runner = EffectPassRunner(ctx)
    runner._ctx_created_at_init = len(ctx.created)
    return runner


@pytest.mark.parametrize("runner_attr,ensure", [
    ("_blit_program", "_ensure_blit_program"),
    ("_combine_program", "_ensure_combine_program"),
])
@pytest.mark.parametrize("fail_at", ["buffer", "vertex_array", "program"])
def test_aud26405_partial_failure_releases_created_objects(
    runner_attr: str, ensure: str, fail_at: str,
) -> None:
    """正例：中途失败时 ensure 内已创建的 program/vbo/vao 逆序释放。"""
    runner = _make_runner(fail_at)
    ctx = runner._ctx
    ctx.armed = True
    ok = getattr(runner, ensure)()
    assert ok is False
    assert getattr(runner, runner_attr) is None
    ensure_created = ctx.created[runner._ctx_created_at_init:]
    if fail_at == "program":
        assert ensure_created == []  # 第一步就失败，无部分对象可泄漏
    else:
        # buffer / vertex_array 失败：此前创建的对象必须全部被释放
        assert ensure_created, "失败发生前应已创建部分对象"
        assert all(obj.released for obj in ensure_created), \
            "已创建的 GL 对象未被释放（泄漏）"


def test_aud26405_success_path_stores_program() -> None:
    """反例回归：成功路径仍正常存储 program/vao。"""
    runner = _make_runner("")
    assert runner._ensure_blit_program() is True
    assert runner._blit_program is not None
    assert runner._blit_vao is not None
    assert runner._ensure_combine_program() is True
    assert runner._combine_program is not None


def test_aud26312_disk_scan_outside_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """正例：DCTL 插件目录扫描在锁外执行（探测时锁必须可获取）。"""
    from src.features.shared.effects import effect_catalog as ec_module

    catalog = ec_module.EffectCatalog()
    lock_free_during_scan: List[bool] = []

    def probe_scan() -> list:
        acquired = catalog._lock.acquire(blocking=False)
        if acquired:
            catalog._lock.release()
        lock_free_during_scan.append(acquired)
        return []

    monkeypatch.setattr(ec_module, "_build_dctl_plugin_specs", probe_scan)
    catalog._ensure_built()
    assert lock_free_during_scan == [True], "磁盘扫描仍发生在持锁区间内"
    assert catalog._built is True
    assert len(catalog.get_all_specs()) >= 7


def test_aud26312_is_known_doc_mentions_lazy_build_cost() -> None:
    """反例（回退 tripwire）：is_known 文档不再谎称无条件 cheap。"""
    from src.features.shared.effects.effect_catalog import EffectCatalog

    doc = EffectCatalog.is_known.__doc__ or ""
    assert "AUD-26312" in doc
    assert "AUD-26312" in (EffectCatalog._ensure_built.__doc__ or "")


# ---------------------------------------------------------------------- #
# teaching_animation/core（AUD-27644 / 27718 / 27801）
# ---------------------------------------------------------------------- #


def test_aud27644_readonly_mapping_convention_documented() -> None:
    """正例：frozen 文档 Mapping 字段携带只读约定（docstring + 注释）。"""
    from src.features.teaching_animation.core.model import (
        SceneElement, TeachingSceneDocument,
    )

    assert "AUD-27644" in (SceneElement.__doc__ or "")
    assert "AUD-27644" in (TeachingSceneDocument.__doc__ or "")
    element_src = inspect.getsource(SceneElement)
    document_src = inspect.getsource(TeachingSceneDocument)
    assert element_src.count("AUD-27644") >= 2  # docstring + 字段注释
    assert document_src.count("AUD-27644") >= 2


def test_aud27644_replace_shares_mapping_reference_pinned() -> None:
    """行为钉死：with_element_updated 经 replace 共享 Mapping 引用（约定）。"""
    from src.features.teaching_animation.core.model import SceneElement

    style = {"fill": "#ff0000"}
    element = SceneElement(element_id="e1", type="text", style=style)
    updated = dc_replace(element, visible=False)
    assert updated.style is style, "replace 共享 Mapping 引用的现状变化，需复核"


def test_aud27718_registry_lock_and_thread_safety() -> None:
    """正例：SceneTemplateRegistry 持 RLock，并发 register/list 不炸。"""
    from src.features.teaching_animation.core.template_registry import (
        SceneTemplate, SceneTemplateRegistry,
    )

    registry = SceneTemplateRegistry()
    assert isinstance(registry._lock, type(threading.RLock())), \
        "SceneTemplateRegistry 线程安全锁被回退删除"
    errors: List[BaseException] = []

    def register_many(tag: str) -> None:
        try:
            for i in range(40):
                registry.register(SceneTemplate(
                    template_id=f"t-{tag}-{i}", version=1,
                    display_name=f"T {tag} {i}",
                ))
                registry.list_templates()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=register_many, args=(f"w{t}",))
               for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15.0)
    assert errors == []
    assert len(registry.list_templates()) == 6 * 40

    # 显式重复处理契约保持
    first = SceneTemplate(template_id="dup", version=1, display_name="Dup")
    assert registry.register(first) is True
    assert registry.register(SceneTemplate(
        template_id="dup", version=1, display_name="Dup2")) is False
    assert registry.register(first, replace=True) is True
    assert "dup" in registry


def test_aud27801_ellipsis_width_matches_its_font_size() -> None:
    """正例：截断后省略号单元的 width 按其自身 font_size 测量。"""
    from src.features.teaching_animation.core.text_layout import (
        TextRun, TextSpan, cjk_aware_measurer, layout_text,
    )

    measure = cjk_aware_measurer()
    # 行内先放小字号词，再放大字号 CJK 单元；max_lines=1 触发截断且
    # 截断会删掉携带最大字号的单元。
    run = TextRun(
        spans=(
            TextSpan(text="aaaa ", font_size=10.0),
            TextSpan(text="大大", font_size=40.0),
        ),
        max_width=70.0,
        line_height=1.3,
        max_lines=1,
    )
    boxes = layout_text(run, measurer=measure)
    assert len(boxes) == 1
    fragments = boxes[0].fragments
    assert fragments, "截断后行不应为空"
    assert fragments[-1].text == run.ellipsis
    expected_width = measure.measure(run.ellipsis, fragments[-1].font_size)
    assert fragments[-1].width == pytest.approx(expected_width), \
        "省略号宽度未按其 font_size 测量（回退到截断前字号）"
    # 具体值钉死：截断删掉 40 号单元后行字号为 10，省略号宽 = 0.55*10
    assert fragments[-1].font_size == pytest.approx(10.0)
    assert fragments[-1].width == pytest.approx(5.5)


def test_aud27801_no_truncation_no_ellipsis() -> None:
    """反例回归：不截断时不产生省略号。"""
    from src.features.teaching_animation.core.text_layout import (
        TextRun, TextSpan, cjk_aware_measurer, layout_text,
    )

    run = TextRun(spans=(TextSpan(text="hello", font_size=12.0),),
                  max_width=800.0)
    boxes = layout_text(run, measurer=cjk_aware_measurer())
    assert len(boxes) == 1
    assert all(f.text != "\u2026" for f in boxes[0].fragments)


# ---------------------------------------------------------------------- #
# shared/tracking（AUD-37307 / 37404 / 37411）
# ---------------------------------------------------------------------- #


def test_aud37307_detection_mask_is_mask_intersect_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正例：传入形状掩码时，goodFeaturesToTrack 收到 mask ∩ region。"""
    import src.shared.tracking.feature_detector as fd_module
    from src.shared.tracking.feature_detector import FeatureDetector

    captured: Dict[str, Any] = {}

    def fake_gftt(gray: np.ndarray, **kwargs: Any) -> np.ndarray:
        captured["mask"] = kwargs["mask"]
        return np.array([[[5.0, 5.0]]], dtype=np.float32)

    monkeypatch.setattr(fd_module.cv2, "goodFeaturesToTrack", fake_gftt)
    gray = np.zeros((100, 100), dtype=np.uint8)
    mask = np.zeros((100, 100), dtype=np.uint8)
    mask[0:40, 0:40] = 255  # 覆盖 region(0,0,10,10) 之外的区域
    points = FeatureDetector().detect(gray, (0.0, 0.0, 10.0, 10.0), mask=mask)
    combined = captured["mask"]
    # region 内保留、region 外（掩码自身区域）剔除——旧实现直接传 mask，
    # (25,25) 处仍为 255，region 外的点会被 clip 夹到边界上。
    assert combined[5, 5] == 255
    assert combined[25, 25] == 0
    # 检出点在 region 内，无需夹取
    assert points.shape == (1, 2)
    assert points[0].tolist() == pytest.approx([5.0, 5.0])


def test_aud37307_none_mask_falls_back_to_region_rect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反例回归：mask=None 时退化为 region 矩形掩码。"""
    import src.shared.tracking.feature_detector as fd_module
    from src.shared.tracking.feature_detector import FeatureDetector

    captured: Dict[str, Any] = {}

    def fake_gftt(gray: np.ndarray, **kwargs: Any) -> np.ndarray:
        captured["mask"] = kwargs["mask"]
        return np.array([[[5.0, 5.0]]], dtype=np.float32)

    monkeypatch.setattr(fd_module.cv2, "goodFeaturesToTrack", fake_gftt)
    gray = np.zeros((100, 100), dtype=np.uint8)
    FeatureDetector().detect(gray, (0.0, 0.0, 10.0, 10.0), mask=None)
    rect = captured["mask"]
    assert rect[5, 5] == 255
    assert rect[25, 25] == 0
    assert rect[50, 50] == 0


def test_aud37404_all_fail_step_call_sites_pass_ref_template() -> None:
    """正例 + 反例（回退 tripwire）：每个 _fail_step 调用都传 ref_template。"""
    import src.shared.tracking.tracker_2d as t2_module

    src = inspect.getsource(t2_module)
    call_count = src.count("self._fail_step(")
    assert call_count >= 2, "至少存在两条失败路径调用点"
    passed = src.count("ref_template=ref_template")
    assert passed >= call_count, (
        f"{call_count} 个 _fail_step 调用点中只有 {passed} 个传了 "
        "ref_template=ref_template（有效点不足路径又漏传了）"
    )


def test_aud37411_probe_timeout_degrades_to_file_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正例：容器探测超时 → 限时降级为纯文件身份指纹，不阻塞调用线程。"""
    import src.shared.tracking.tracking_cache as tc_module

    media = tmp_path / "slow.mp4"
    media.write_bytes(b"\x00\x01")
    monkeypatch.setattr(tc_module, "_MEDIA_PROBE_TIMEOUT_S", 0.05)

    def slow_probe(_path: str) -> str:
        time.sleep(1.5)
        return "1920x1080:99:av1"

    monkeypatch.setattr(tc_module, "_probe_container_meta", slow_probe)
    start = time.perf_counter()
    fingerprint = tc_module.compute_media_fingerprint(str(media))
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, "容器探测仍同步阻塞调用线程（超时保护被回退）"
    assert len(fingerprint) == 32


def test_aud37411_probe_success_deterministic_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测成功 → 指纹 = sha256(路径|大小|mtime_ns|meta) 截断 32 位。"""
    import src.shared.tracking.tracking_cache as tc_module

    media = tmp_path / "ok.mp4"
    media.write_bytes(b"abc")
    monkeypatch.setattr(tc_module, "_probe_container_meta",
                        lambda _p: "640x360:30:h264")
    stat = os.stat(media)
    expected_raw = f"{media}|{stat.st_size}|{int(stat.st_mtime_ns)}|640x360:30:h264"
    expected = hashlib.sha256(expected_raw.encode("utf-8")).hexdigest()[:32]
    assert tc_module.compute_media_fingerprint(str(media)) == expected
    # 文件不存在 → 空串
    assert tc_module.compute_media_fingerprint(str(tmp_path / "nope.mp4")) == ""


# ---------------------------------------------------------------------- #
# ai_workflow/adapters（AUD-23015 / 23019）
# ---------------------------------------------------------------------- #


def _make_executor() -> Any:
    from src.features.ai_workflow.adapters.comfyui_executor import (
        ComfyWorkflowExecutor, FakeComfyServer,
    )

    return ComfyWorkflowExecutor(
        server=FakeComfyServer(profile_id="p"),
        server_capabilities=SimpleNamespace(),
        poll_timeout_s=1.0, poll_interval_s=0.0,
    )


def test_aud23015_submitted_jobs_lru_capped() -> None:
    """正例：追踪表为 OrderedDict 且维持上限，最旧条目先淘汰。"""
    from src.features.ai_workflow.adapters import comfyui_executor as ce_module
    from src.features.ai_workflow.adapters.comfyui_executor import (
        ComfyWorkflowExecutor, FakeComfyServer,
    )

    assert isinstance(ce_module._MAX_TRACKED_JOBS, int)
    assert ce_module._MAX_TRACKED_JOBS >= 16
    ex = ComfyWorkflowExecutor(
        server=FakeComfyServer(profile_id="p"),
        server_capabilities=SimpleNamespace(),
        poll_timeout_s=1.0, poll_interval_s=0.0,
    )
    assert hasattr(ex._submitted_jobs, "move_to_end"), \
        "追踪表应退回 OrderedDict LRU（AUD-23015 被回退）"
    cap = ce_module._MAX_TRACKED_JOBS
    for i in range(cap + 10):
        ex._track_submitted(f"req-{i}", f"fp-{i}", object())
    assert len(ex._submitted_jobs) == cap
    # 最旧的 req-0..9 已被淘汰，最新条目保留
    assert "req-0" not in ex._submitted_jobs
    assert f"req-{cap + 9}" in ex._submitted_jobs
    # 重复提交同一 request_id 视为最近使用（不淘汰）
    ex._track_submitted(f"req-{cap}", "fp-refresh", object())
    assert ex._submitted_jobs[f"req-{cap}"] == ("fp-refresh", ex._submitted_jobs[f"req-{cap}"][1])


def test_aud23019_bind_state_synchronized() -> None:
    """正例：bind/capabilities 经 _bind_lock 同步；generate 快照读取一致。"""
    from src.features.ai_workflow.adapters.comfyui_executor import (
        ComfyWorkflowExecutor, FakeComfyServer,
    )

    ex = ComfyWorkflowExecutor(
        server=FakeComfyServer(profile_id="p"),
        server_capabilities=SimpleNamespace(),
        poll_timeout_s=1.0, poll_interval_s=0.0,
    )
    assert isinstance(ex._bind_lock, type(threading.Lock())), \
        "bind 状态同步锁被回退删除"
    snapshot = SimpleNamespace(name="wf")
    profile = SimpleNamespace(is_cloud=False)
    ex.bind_workflow(snapshot, profile=profile, bindings=())
    assert ex._bound_snapshot is snapshot
    assert ex._bound_profile is profile
    caps = SimpleNamespace(profile_id="p2")
    ex.set_server_capabilities(caps)
    assert ex._caps is caps

    # 并发绑定 / 读取不崩
    errors: List[BaseException] = []

    def binder(tag: int) -> None:
        try:
            for i in range(100):
                ex.bind_workflow(
                    SimpleNamespace(name=f"wf-{tag}-{i}"),
                    profile=SimpleNamespace(is_cloud=bool(i % 2)),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=binder, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
    assert errors == []


def test_aud23019_generate_uses_locked_snapshot(qapp=None) -> None:
    """generate 入口在锁内一次性快照，未绑定时返回绑定缺失诊断。"""
    from src.features.ai_workflow.adapters.comfyui_executor import (
        ComfyExecutorErrorCode, ComfyWorkflowExecutor, FakeComfyServer,
    )

    ex = ComfyWorkflowExecutor(
        server=FakeComfyServer(profile_id="p"),
        server_capabilities=SimpleNamespace(),
        poll_timeout_s=1.0, poll_interval_s=0.0,
    )
    request = SimpleNamespace(
        request_id="r1",
        consent=SimpleNamespace(is_complete=True, provider_id=""),
        modality=SimpleNamespace(),
        prompt="p",
        budget=SimpleNamespace(),
    )
    result = ex.generate(request)
    assert result.error_code == ComfyExecutorErrorCode.SUBMIT_FAILED
    assert "bind_workflow" in (result.error_message or "")
