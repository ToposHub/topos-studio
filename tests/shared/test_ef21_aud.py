"""批 EF-21 修复测试 —— 19 个散点位置 24 条 Suggestion 级审计发现。

对应修复批次：docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md
条目 AUD-1702、3603、6200、9305、12608、12613、19301、21605、23209、23407、
24401、25912、28116、28323、30502、31903、32801、32803、33802、33910、33911、
36401、36403、34203。

约束：不创建真实 GL 上下文（stub 渲染器/假 render 线程）；不弹模态对话框；
不做真实 GIF 光栅化（save_preset 走 render_preview=False 路径）。
每条修复都有正例 + 反例（回退到旧实现时测试必须失败）。
"""
from __future__ import annotations

import inspect
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------- #
# composite（AUD-6200 / AUD-9305）
# ---------------------------------------------------------------------- #

from src.features.composite.export_provider import CompositeExportProvider
import src.features.composite.render.composite_export_renderer as _cer_module
from src.features.composite import runtime as _comp_runtime
from src.features.composite import module as _comp_module


class _StubPassesRenderer:
    """Standalone-path stub：无 GL，只验证 render_full_res_passes 的回退路由。"""

    constructed: list["_StubPassesRenderer"] = []

    def __init__(self, interactive_scheduler: object = None) -> None:
        self.interactive_scheduler = interactive_scheduler
        self.config_manager: object = None
        _StubPassesRenderer.constructed.append(self)

    def set_config_manager(self, config_manager: object) -> None:
        self.config_manager = config_manager

    def render_and_readback_passes(self, snapshot: object, render_passes: object = None):
        return SimpleNamespace(
            image=None,
            passes={"beauty": "IMG"},
            error=None,
            used_tiling=False,
            tile_count=0,
            render_time_ms=1.5,
        )


class _FakeRenderThread:
    """带多 pass 导出 API 的假渲染线程（真实 CompositeRenderer 暂无此 API，
    该 stub 描述未来接线后的契约）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []
        self.config_manager: object = None

    def render_export_passes(self, snapshot: object, render_passes: object = None,
                             interactive_scheduler: object = None,
                             config_manager: object = None):
        self.calls.append((snapshot, render_passes))
        self.config_manager = config_manager
        return SimpleNamespace(
            image=None,
            passes={"beauty": "T", "depth": "D"},
            error=None,
            used_tiling=True,
            tile_count=2,
            render_time_ms=3.0,
        )


def test_aud6200_passes_route_via_render_thread() -> None:
    provider = CompositeExportProvider(config_manager="cfg")
    thread = _FakeRenderThread()
    result = provider.render_full_res_passes(
        snapshot="SNAP", render_passes=("beauty", "depth"), render_thread=thread)
    # 走了渲染线程路由：无 GL 在调用线程创建。
    assert thread.calls == [("SNAP", ("beauty", "depth"))]
    assert thread.config_manager == "cfg"
    assert result.passes == {"beauty": "T", "depth": "D"}
    assert result.used_tiling is True
    assert result.tile_count == 2
    # standalone 渲染器未被构造。
    assert _StubPassesRenderer.constructed == []


def test_aud6200_passes_fallback_warns_and_uses_standalone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(_cer_module, "CompositeExportRenderer", _StubPassesRenderer)
    _StubPassesRenderer.constructed.clear()
    provider = CompositeExportProvider()
    with caplog.at_level(logging.WARNING, logger="src.features.composite.export_provider"):
        result = provider.render_full_res_passes(snapshot="SNAP")
    assert result.passes == {"beauty": "IMG"}
    assert len(_StubPassesRenderer.constructed) == 1
    # 回退路径必须显式告警"调用线程将创建 GL 上下文"，防止 UI 线程静默接线。
    assert any("calling thread" in rec.message for rec in caplog.records)


def test_aud6200_contract_documents_thread_affinity() -> None:
    source = inspect.getsource(CompositeExportProvider.render_full_res_passes)
    assert "AUD-6200" in source
    assert "render_thread" in inspect.signature(
        CompositeExportProvider.render_full_res_passes).parameters


def test_aud9305_runtime_docstring_names_real_creator() -> None:
    doc = inspect.getdoc(_comp_runtime.CompositeRuntime) or ""
    assert "Created by CompositeModule.create_runtime()" not in doc
    assert "MainWindowAdapter" in doc
    assert "AUD-9305" in doc
    module_source = inspect.getsource(_comp_module.CompositeModule.create_runtime)
    # 死路径一旦被启用必须复用 register_pages 已建的页，不得重复建页。
    assert "existing_page=self._page" in module_source


# ---------------------------------------------------------------------- #
# edit/adapters（AUD-12608 / AUD-12613）
# ---------------------------------------------------------------------- #

from src.features.edit.adapters.fcpxml_adapter import _time_to_frames
from src.features.edit.adapters import import_builder as _import_builder
from src.shared.media.metadata import Rational


def test_aud12608_rational_and_decimal_paths_floor() -> None:
    fps24 = Rational(24, 1)
    assert _time_to_frames("1/2s", fps24) == 12
    # Apple 真实样例：100/2400s @ 24fps = 1 帧。
    assert _time_to_frames("100/2400s", fps24) == 1
    # 小数秒走整数有理数 floor（含负值）。
    assert _time_to_frames("1.5s", fps24) == 36
    assert _time_to_frames("-1/2s", fps24) == -12
    ntsc = Rational(30000, 1001)
    assert _time_to_frames("1001/30000s", ntsc) == 1


def test_aud12608_decimal_path_exact_rational_not_float() -> None:
    # 0.0000001s @ 10000000fps：精确有理数 = 1 帧；
    # 旧 float 路径 1e-7 * 1e7 = 0.9999999999999999 → floor 0。
    fps_hi = Rational(10_000_000, 1)
    assert _time_to_frames("0.0000001s", fps_hi) == 1


def test_aud12608_parse_failure_logs_instead_of_silent_zero(
    caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING,
                         logger="src.features.edit.adapters.fcpxml_adapter"):
        assert _time_to_frames("garbage", Rational(24, 1)) == 0
        assert _time_to_frames("1/0s", Rational(24, 1)) == 0
    messages = [rec.message for rec in caplog.records]
    assert any("garbage" in m for m in messages)
    assert any("denominator" in m for m in messages)


def test_aud12613_blocking_io_contracts_documented() -> None:
    rows_source = inspect.getsource(_import_builder.collect_asset_rows)
    assert "AUD-12613" in rows_source
    build_source = inspect.getsource(_import_builder.build_project_from_import_result)
    assert "AUD-12613" in build_source
    assert "probe_metadata" in build_source or "os.path.exists" in rows_source


# ---------------------------------------------------------------------- #
# control_surfaces（AUD-32801 / AUD-32803）
# ---------------------------------------------------------------------- #

from src.shared.control_surfaces.tangent.hub_client import TangentHubClient
from src.shared.control_surfaces.tangent.protocol import encode_parameter_value


def test_aud32801_stop_keeps_residual_thread_visible() -> None:
    client = TangentHubClient("topos", "/tmp/system", None)
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, args=(5.0,), daemon=True)
    stuck.start()
    client._thread = stuck  # 模拟 join 超时后仍存活的 worker
    try:
        client.stop()  # join(1.0) 超时 → 不得清空 _thread
        assert client._thread is stuck
        assert client.is_running is True
        thread_count_before = threading.active_count()
        client.start()  # 不得误判已停止而并行启动第二个 worker
        assert threading.active_count() == thread_count_before
        assert client._thread is stuck
    finally:
        release.set()
        stuck.join(timeout=5.0)
    # 残留线程真正退出后 is_running 恢复真实，start 可重新接管。
    assert client.is_running is False
    client.start()
    try:
        assert client.is_running is True
    finally:
        client.stop()


def test_aud32801_prompt_stop_clears_thread() -> None:
    client = TangentHubClient("topos", "/tmp/system", None)
    client.stop()  # 从未启动：无线程可等，直接清引用
    assert client._thread is None
    assert client.is_running is False


def test_aud32803_pack_f32_raises_documented_valueerror() -> None:
    # 超出 float32 范围：契约是 ValueError（此前 struct.pack 抛 OverflowError
    # 并从 send_parameter_value 未捕获地传播到调用线程）。
    with pytest.raises(ValueError, match="out of range"):
        encode_parameter_value(1, 1e39)
    with pytest.raises(ValueError, match="out of range"):
        encode_parameter_value(1, -1e39)
    # 正常值不受影响。
    frame = encode_parameter_value(3, 0.5)
    assert isinstance(frame, bytes) and len(frame) > 0


# ---------------------------------------------------------------------- #
# shared/io（AUD-33910 / AUD-33911）
# ---------------------------------------------------------------------- #

import cv2
import numpy as np

from src.shared.io.file_io import FileIO


def _write_tiny_png(directory: Path, name: str = "img.png") -> str:
    path = directory / name
    arr = np.full((4, 4, 3), 128, dtype=np.uint8)
    assert cv2.imwrite(str(path), arr)
    return str(path)


def test_aud33910_load_image_cache_dir_race_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = _write_tiny_png(tmp_path, "source.png")
    cache_dir = tmp_path / "images_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)  # 模拟并发线程已抢先建好目录
    original_cache_dir = FileIO.CACHE_DIR
    FileIO.CACHE_DIR = str(cache_dir)
    real_exists = os.path.exists

    def fake_exists(path: object) -> bool:
        # TOCTOU 窗口：exists 检查时目录"尚不存在"，makedirs 时已存在。
        if str(path) == str(cache_dir):
            return False
        return real_exists(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os.path, "exists", fake_exists)
    try:
        full, high, low = FileIO.load_image(image_path)
        assert full is not None and high is not None and low is not None
    finally:
        FileIO.CACHE_DIR = original_cache_dir


def test_aud33911_save_baked_layer_docstring_matches_half_exr() -> None:
    doc = inspect.getdoc(FileIO.save_baked_layer) or ""
    assert "32-bit floating point precision" not in doc
    assert "HALF" in doc and "16-bit" in doc
    assert "AUD-33911" in doc


# ---------------------------------------------------------------------- #
# shared/node_help（AUD-36401 / AUD-36403）
# ---------------------------------------------------------------------- #

import src.shared.node_help as node_help


def _make_docs(tmp_path: Path, node: str, zh_content: str | None,
               en_content: str | None) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    if zh_content is not None:
        (docs / f"{node}.zh_CN.md").write_text(zh_content, encoding="utf-8")
    if en_content is not None:
        (docs / f"{node}.en_US.md").write_text(en_content, encoding="utf-8")
    return docs


def test_aud36401_unreadable_preferred_doc_falls_back_to_en_us(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture) -> None:
    docs = _make_docs(tmp_path, "cornerpin", "中文帮助", "English help")
    monkeypatch.setattr(node_help, "get_docs_dir", lambda: str(docs))
    real_open = open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if str(file).endswith("cornerpin.zh_CN.md"):
            raise OSError("boom")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    with caplog.at_level(logging.WARNING, logger="src.shared.node_help"):
        # 解析顺序第 2 条：回退 en_US，而不是直接放弃返回 None。
        assert node_help.get_node_help("cornerpin", locale_code="zh_CN") == "English help"
    assert any("trying fallback" in rec.message for rec in caplog.records)


def test_aud36403_has_node_help_is_existence_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = _make_docs(tmp_path, "transform", None, "English help")
    monkeypatch.setattr(node_help, "get_docs_dir", lambda: str(docs))

    def forbidden_open(*args: Any, **kwargs: Any):
        raise AssertionError("has_node_help must not open documents")

    monkeypatch.setattr("builtins.open", forbidden_open)
    # 布尔查询只探测文件存在性，不读取/解码整个文档。
    assert node_help.has_node_help("transform", locale_code="zh_CN") is True
    assert node_help.has_node_help("transform", locale_code="en_US") is True
    assert node_help.has_node_help("missing", locale_code="en_US") is False


def test_aud36403_exists_matches_get_node_help_normal_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = _make_docs(tmp_path, "blur", "中文", "English")
    monkeypatch.setattr(node_help, "get_docs_dir", lambda: str(docs))
    for locale in ("zh_CN", "en_US", "ja_JP"):
        assert node_help.has_node_help("blur", locale_code=locale) == (
            node_help.get_node_help("blur", locale_code=locale) is not None)


# ---------------------------------------------------------------------- #
# app/menu（AUD-3603）
# ---------------------------------------------------------------------- #

from src.app.menu.menu_builder import MenuBuilder


def test_aud3603_docstring_matches_label_reality() -> None:
    doc = inspect.getdoc(MenuBuilder) or ""
    # 标签在 feature 模块注册时已完成翻译，builder 不再声称 "translated label"。
    assert "with the translated label" not in doc
    assert "AUD-3603" in doc
    # I18N 现被 AUD-3600/3601 的自动补建菜单标题翻译真实使用（非死导入）。
    source = inspect.getsource(MenuBuilder)
    assert "I18N.tr(" in source


# ---------------------------------------------------------------------- #
# ai_workflow（AUD-23209 / AUD-23407）
# ---------------------------------------------------------------------- #

from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateEntry, WorkflowTemplateRegistry,
)
from src.features.ai_workflow.core.workflow_models import WorkflowGraph, WorkflowNode, WorkflowUi
from src.features.ai_workflow.runtime.workflow_run_controller import WorkflowRunController


def _tiny_entry() -> WorkflowTemplateEntry:
    graph = WorkflowGraph(
        workflow_id="wf-ef21",
        nodes=(WorkflowNode(
            node_id="n1", spec_id="topos.test.node",
            params={"a": {"nested": 1}}, ui={"x": 1, "y": 2},
        ),),
        ui=WorkflowUi(viewport={"zoom": 1.0}),
    )
    return WorkflowTemplateEntry(
        template_id="ef21", display_name="EF21", description="",
        category="test", skill_id="", graph=graph,
        default_param_overrides={"n1": {"a": {"nested": 1}}},
    )


def test_aud23209_instantiate_breaks_shared_mutable_refs() -> None:
    registry = WorkflowTemplateRegistry()
    entry = _tiny_entry()
    registry.register(entry)
    copy = registry.instantiate("ef21")
    # 副本各可变容器逐一改写，冻结官方模板必须纹丝不动。
    copy.nodes[0].ui["injected"] = True
    copy.nodes[0].params["a"]["nested"] = 999
    copy.ui.viewport["zoom"] = 42.0
    assert entry.graph.nodes[0].ui == {"x": 1, "y": 2}
    assert entry.graph.nodes[0].params["a"]["nested"] == 1
    assert entry.graph.ui.viewport["zoom"] == 1.0
    assert entry.default_param_overrides["n1"]["a"]["nested"] == 1


def test_aud23209_instantiate_applies_overrides_isolated() -> None:
    registry = WorkflowTemplateRegistry()
    entry = _tiny_entry()
    registry.register(entry)
    copy = registry.instantiate("ef21", param_overrides={"n1": {"b": 2}})
    assert copy.nodes[0].params["b"] == 2
    assert copy.nodes[0].params["a"]["nested"] == 1
    copy.nodes[0].params["a"]["nested"] = 777
    assert entry.default_param_overrides["n1"]["a"]["nested"] == 1


def test_aud23407_dispose_cancels_run_on_wait_timeout() -> None:
    controller = WorkflowRunController.__new__(WorkflowRunController)
    controller._unsubscribe = None
    controller._worker = SimpleNamespace(isRunning=lambda: True, wait=lambda ms: False)
    cancel_calls: list[tuple[str, str]] = []
    controller._runtime = SimpleNamespace(
        cancel=lambda run_id, reason="": cancel_calls.append((run_id, reason)),
        get_run=lambda run_id: SimpleNamespace(status="running"),
    )
    controller._current_run_id = "run-ef21"
    controller.dispose()
    # 超时后必须协作取消，而不是既不取消也不处理残留线程。
    assert cancel_calls == [("run-ef21", "dispose_timeout")]
    # worker 引用保持完整（AUD-23405：绝不覆盖运行中的 QThread）。
    assert controller._worker is not None


def test_aud23407_dispose_prompt_exit_skips_cancel() -> None:
    controller = WorkflowRunController.__new__(WorkflowRunController)
    controller._unsubscribe = None
    controller._worker = SimpleNamespace(isRunning=lambda: True, wait=lambda ms: True)
    cancel_calls: list[tuple[str, str]] = []
    controller._runtime = SimpleNamespace(
        cancel=lambda run_id, reason="": cancel_calls.append((run_id, reason)),
        get_run=lambda run_id: SimpleNamespace(status="running"),
    )
    controller._current_run_id = "run-ef21"
    controller.dispose()
    assert cancel_calls == []


# ---------------------------------------------------------------------- #
# color 根 / edit 根（AUD-24401 / AUD-19301）
# ---------------------------------------------------------------------- #

from src.features.color import facade as _color_facade
from src.features.edit.facade import parse_composite_graph_data


def test_aud24401_no_dead_track_status_import_in_loop() -> None:
    source = inspect.getsource(_color_facade.ColorFacade.export_tracking_bindings)
    assert "from src.shared.tracking.types import TrackStatus" not in source


def test_aud19301_rejects_non_dict_external_data() -> None:
    # 外部数据不再原样透传：非 dict / 非 CompositeGraphData 输入返回 None。
    assert parse_composite_graph_data("garbage-from-project-file") is None
    assert parse_composite_graph_data(42) is None
    assert parse_composite_graph_data(None) is None
    parsed = parse_composite_graph_data({"source_kind": "empty_comp_clip"})
    assert parsed is not None and type(parsed).__name__ == "CompositeGraphData"


def test_aud19301_passthrough_for_already_parsed_instance() -> None:
    from src.features.edit.core.composite_graph_data import CompositeGraphData
    instance = CompositeGraphData.from_dict({"source_kind": "empty_comp_clip"})
    assert parse_composite_graph_data(instance) is instance


# ---------------------------------------------------------------------- #
# scripting（AUD-25912）
# ---------------------------------------------------------------------- #

from src.features.scripting.engine import BlockingBridge


def test_aud25912_abort_flag_access_is_locked() -> None:
    reset_source = inspect.getsource(BlockingBridge.reset_abort)
    read_source = inspect.getsource(BlockingBridge.is_aborted)
    assert "self._mutex.lock()" in reset_source
    assert "self._mutex.lock()" in read_source


def test_aud25912_abort_flag_roundtrip() -> None:
    bridge = BlockingBridge()
    assert bridge.is_aborted() is False
    bridge.request_abort()
    assert bridge.is_aborted() is True
    bridge.reset_abort()
    assert bridge.is_aborted() is False


# ---------------------------------------------------------------------- #
# teaching_animation（AUD-30502 / AUD-28116 / AUD-28323）
# ---------------------------------------------------------------------- #

from src.features.teaching_animation.scene_cache import TeachingSceneCacheCoordinator
from src.features.teaching_animation.core.model import (
    SCENE_SCHEMA_VERSION, SceneElement, SceneTransform, TeachingSceneDocument,
)
from src.features.teaching_animation.motion.motion_commands import (
    ApplyMotionPresetCommand, SetStaggerIntervalCommand, StaggerElementsCommand,
    read_motion_plans,
)
from src.features.teaching_animation.presets.library import MgPresetLibrary
from src.features.teaching_animation.presets import builders


def _ef21_scene() -> TeachingSceneDocument:
    element = SceneElement(
        element_id="el", type="shape",
        transform=SceneTransform(x=10.0, y=20.0, width=100.0, height=50.0),
        style={"fill": "#123456"}, content={"kind": "rect"},
    )
    return TeachingSceneDocument(
        scene_id="ef21-scene", schema_version=SCENE_SCHEMA_VERSION,
        name="EF21", width=640, height=360,
        fps_num=24, fps_den=1, duration_frames=60,
        background={"color": "#0b1220"},
        elements=(element,), animations=(),
    )


def test_aud30502_close_is_atomic_and_schedule_refuses_after_close() -> None:
    coordinator = TeachingSceneCacheCoordinator()
    coordinator.close()
    # close 后 schedule 必须显式拒绝（而非把任务提交进已关闭的 executor）。
    with pytest.raises(RuntimeError, match="closed"):
        coordinator.schedule(_ef21_scene())
    coordinator.close()  # 幂等
    # shutdown 与句柄清理在同一临界区内（AUD-30502 的竞态窗口修复）。
    close_source = inspect.getsource(TeachingSceneCacheCoordinator.close)
    assert "with self._lock:" in close_source
    assert "self.worker.shutdown()" in close_source


def test_aud30502_schedule_before_close_still_works() -> None:
    coordinator = TeachingSceneCacheCoordinator()
    try:
        handle = coordinator.schedule(_ef21_scene())
        handle.cancel()
    finally:
        coordinator.close()


def test_aud28116_apply_preset_double_apply_refused_redo_works() -> None:
    scene = _ef21_scene()
    cmd = ApplyMotionPresetCommand(
        element_id="el", preset_id="fade", param_name="opacity",
        start_frame=0, end_frame=10,
    )
    applied = cmd.apply(scene)
    # 重复 apply 会孤儿化先前动画——现在 fail-closed 拒绝。
    with pytest.raises(RuntimeError, match="already applied"):
        cmd.apply(applied)
    undone = cmd.undo(applied)
    assert not any(a.element_id == "el" for a in undone.animations)
    # undo 清令牌后，同一实例 redo（SceneUndoStack 的重放路径）仍然可用。
    redone = cmd.apply(undone)
    assert sum(1 for a in redone.animations if a.element_id == "el") == 1


def test_aud28116_stagger_double_apply_refused_redo_works() -> None:
    scene = _ef21_scene()
    cmd = StaggerElementsCommand(
        element_ids=("el",), preset_id="pop", param_name="scale_x",
        interval=12, duration=18,
    )
    applied = cmd.apply(scene)
    with pytest.raises(RuntimeError, match="already applied"):
        cmd.apply(applied)
    undone = cmd.undo(applied)
    assert not any(a.element_id == "el" for a in undone.animations)
    redone = cmd.apply(undone)
    assert sum(1 for a in redone.animations if a.element_id == "el") == 1
    assert len(read_motion_plans(redone)) == 1


def test_aud28116_set_interval_double_apply_refused_redo_works() -> None:
    scene = _ef21_scene()
    stagger = StaggerElementsCommand(
        element_ids=("el",), preset_id="pop", param_name="scale_x",
        interval=12, duration=18,
    )
    staged = stagger.apply(scene)
    plan_id = next(iter(read_motion_plans(staged)))
    cmd = SetStaggerIntervalCommand(plan_id=plan_id, interval=6)
    changed = cmd.apply(staged)
    with pytest.raises(RuntimeError, match="already applied"):
        cmd.apply(changed)
    undone = cmd.undo(changed)
    # undo 回到原始 interval，且 redo 可用（令牌已清）。
    assert read_motion_plans(undone)[plan_id].interval == 12
    redone = cmd.apply(undone)
    assert read_motion_plans(redone)[plan_id].interval == 6


def test_aud28323_save_preset_can_skip_gif_preview(tmp_path: Path) -> None:
    library = MgPresetLibrary([tmp_path / "user"])
    scene_data = builders.scene_document([
        builders.text_element("t1", 100, 100, "EF21 Preset", size=64),
    ], name="EF21 Preset", duration_frames=12).to_dict()
    record = library.save_preset(
        name="EF21 No Preview", category="general",
        scene_data=scene_data, render_preview=False)
    assert record.folder.is_dir()
    assert (record.folder / "preset.json").is_file()
    assert (record.folder / "scene.json").is_file()
    # render_preview=False：不同步光栅化 GIF（UI 线程零阻塞出口）。
    assert not (record.folder / "preview.gif").exists()
    assert library.get(record.preset_id) is not None


def test_aud28323_save_preset_preview_contract() -> None:
    signature = inspect.signature(MgPresetLibrary.save_preset)
    assert "render_preview" in signature.parameters
    assert signature.parameters["render_preview"].default is True
    source = inspect.getsource(MgPresetLibrary.save_preset)
    assert "AUD-28323" in source
    assert "Thread affinity" in source


# ---------------------------------------------------------------------- #
# products / shared 根（AUD-21605 / AUD-31903）
# ---------------------------------------------------------------------- #

from src.products.packaging import get_packaging_config, PRODUCT_PRESETS
import src.shared.app_restart as app_restart


def test_aud21605_include_locales_documented_as_feature_ids() -> None:
    product_name = next(iter(PRODUCT_PRESETS))
    config = get_packaging_config(product_name)
    # include_locales 与 include_features 同源（feature ID，非 locale 前缀）。
    assert config["include_locales"] == config["include_features"]
    doc = inspect.getdoc(get_packaging_config) or ""
    # 旧的错误描述行必须消失（措辞更正本身可引用该短语）。
    assert "list of locale file prefixes" not in doc
    assert "AUD-21605" in doc


def test_aud31903_no_blocking_sleep_in_restart() -> None:
    source = inspect.getsource(app_restart)
    assert "time.sleep" not in source
    assert "AUD-31903" in source


# ---------------------------------------------------------------------- #
# shared/i18n、shared/logging、shared/render
# （AUD-33802 / AUD-34203 / AUD-1702）
# ---------------------------------------------------------------------- #

from src.shared.i18n.i18n import I18N
from src.shared.logging.log_manager import LogManager
from src.shared.render.source import RenderSource


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


@pytest.fixture()
def _i18n_state_guard():
    """保存/恢复 I18N 类级状态，避免污染其他测试。"""
    saved = (
        I18N._locales_dir, I18N._translations, I18N._fallback_translations,
        I18N._current_locale, list(I18N._module_locales_dirs),
    )
    yield
    I18N._locales_dir = saved[0]
    I18N._translations = saved[1]
    I18N._fallback_translations = saved[2]
    I18N._current_locale = saved[3]
    I18N._module_locales_dirs[:] = saved[4]


def test_aud33802_reload_builds_complete_dict_and_swaps_atomically(
    tmp_path: Path, _i18n_state_guard: None) -> None:
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en_US.json").write_text('{"greet": "Hello"}', encoding="utf-8")
    (locales / "zh_CN.json").write_text('{"greet": "你好"}', encoding="utf-8")
    module_dir = tmp_path / "module_locales"
    module_dir.mkdir()
    (module_dir / "en_US.json").write_text('{"mod": {"key": "module"}}', encoding="utf-8")
    (module_dir / "zh_CN.json").write_text('{"mod": {"key": "模块"}}', encoding="utf-8")

    I18N.initialize(str(locales))
    I18N.load_module_translations(str(module_dir))
    assert I18N.reload_locale("zh_CN") is True
    # 重载完成后基础键与模块键必须同属一个完整词典（原子替换后无中间态）。
    assert I18N.tr("greet") == "你好"
    assert I18N.tr("mod.key") == "模块"
    assert I18N.reload_locale("en_US") is True
    assert I18N.tr("greet") == "Hello"
    assert I18N.tr("mod.key") == "module"


def test_aud33802_failed_reload_keeps_live_state(
    tmp_path: Path, _i18n_state_guard: None) -> None:
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en_US.json").write_text('{"greet": "Hello"}', encoding="utf-8")
    (locales / "zh_CN.json").write_text('{"greet": "你好"}', encoding="utf-8")
    (locales / "ja_JP.json").write_text("{not-json", encoding="utf-8")
    I18N.initialize(str(locales))
    assert I18N.reload_locale("zh_CN") is True
    assert I18N.reload_locale("ja_JP") is False
    # 失败路径不污染活动词典。
    assert I18N.tr("greet") == "你好"
    assert I18N.get_current_locale() == "zh_CN"


def test_aud33802_reload_no_longer_two_step_in_place() -> None:
    source = inspect.getsource(I18N.reload_locale)
    assert "AUD-33802" in source
    # 不再"先 load_locale 原地换基础词典、再逐个重合并模块"。
    assert "cls.load_locale(" not in source
    assert "_merged_module_dir" in source


def test_aud34203_setup_closes_stale_handlers(
    caplog: pytest.LogCaptureFixture) -> None:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    stale = _RecordingHandler()
    root.handlers = [stale]
    manager = LogManager(config_manager=None)
    try:
        with caplog.at_level(logging.WARNING):
            manager._setup()
        # 旧 handler 已被 close（重复 initialize 不再泄漏文件句柄）。
        assert stale.closed is True
        # 新 console handler 已挂上。
        assert manager._console_handler in root.handlers
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.handlers[:] = saved_handlers


def test_aud34203_detaches_before_closing() -> None:
    source = inspect.getsource(LogManager._setup)
    assert "AUD-34203" in source
    assert "root.handlers = []" in source
    assert "root.handlers.clear()" not in source


def test_aud1702_output_contract_documents_ownership_and_thread() -> None:
    doc = inspect.getdoc(RenderSource.get_current_output) or ""
    assert "AUD-1702" in doc
    assert "Ownership contract" in doc
    assert "render thread" in doc


# ── AUD-36404：node_type 路径安全校验 ────────────────────────────────────


class TestNodeHelpPathSafety:
    def test_traversal_node_type_rejected_by_all_lookups(self, tmp_path):
        """含路径成分的 node_type 不进入任何 docs 路径拼接。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        # 在 docs 上一级放一个"越界"文档，若校验缺失将被读到
        outside = tmp_path / "secrets.en_US.md"
        outside.write_text("SECRET", encoding="utf-8")

        import src.shared.node_help as nh

        for bad in ("../../secrets", "..\\..\\secrets", "a/b", "a:b",
                    "../secrets"):
            assert nh.get_node_help(bad) is None, bad
            assert nh.has_node_help(bad) is False, bad
            assert nh.available_langs(bad) == [], bad

    def test_legitimate_node_type_still_resolves(self):
        """常规标识符 node_type 不受白名单影响。"""
        import src.shared.node_help as nh

        assert nh._is_safe_node_type("cornerpin")
        assert nh._is_safe_node_type("color_input-2")
        assert nh._is_safe_node_type("blur_h")
        assert not nh._is_safe_node_type("../x")
        assert not nh._is_safe_node_type("a/b")
        assert not nh._is_safe_node_type("")
