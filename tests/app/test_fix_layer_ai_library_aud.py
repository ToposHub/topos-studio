"""批 A Critical 修复回归测试（AUD-2000 / AUD-2900 / AUD-3800）。

- AUD-2000：``_PlanWorker`` 不再摘除/恢复 ``service.on_state_changed``，
  改为线程安全的 ``_PlanStateGate`` 计数门闩——重叠规划 worker 不再造成
  跨线程 UI 回填（worker 线程内操作 QWidget）或回调永久丢失。
- AUD-2900：应用预设/库调色时 ``UPDATE_AUX_TEX`` 的 ``layer_id`` 用
  ``layer.id``（UUID 字符串），与渲染端 ``layer_textures`` 键及
  ``REMOVE_LAYER``/``CLEAR_AUX_TEX`` 清理键一致；不再是 ``id(layer)``。
- AUD-3800：``rename_item`` 目标重名时拒绝覆盖（POSIX rename 会静默
  原子替换同名文件，造成不可恢复的数据丢失）。
"""
from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.app


# ─────────────────────────────────────────────────────────────────────
# AUD-2000：_PlanWorker 状态回调竞态
# ─────────────────────────────────────────────────────────────────────

class _RecordingService:
    """记录 ``on_state_changed`` 属性写入的假 service。

    属性协议把每次赋值都记入 ``writes``——旧实现（摘除/恢复回调）在
    run() 内会产生两次额外写入，新实现应为零次。
    """

    def __init__(self, plan_behavior=None) -> None:
        self._callback = None
        self.writes: list = []
        self.plan_calls = 0
        self._plan_behavior = plan_behavior

    @property
    def on_state_changed(self):
        return self._callback

    @on_state_changed.setter
    def on_state_changed(self, value):
        self.writes.append(value)
        self._callback = value

    def plan_variants(self, index, config):
        self.plan_calls += 1
        if self._plan_behavior is not None:
            self._plan_behavior()
        return []

    def record_error(self, message: str) -> None:
        pass


def test_plan_worker_never_touches_service_callback(qapp):
    """worker.run() 全程零次写 service.on_state_changed（AUD-2000 回归锁）。

    旧实现先置 None 再恢复；重叠时恢复交错导致跨线程回填或回调丢失。
    """
    from src.app.controllers.ai_edit_controller import _PlanWorker

    callback_during_plan = []

    def behavior():
        callback_during_plan.append(service.on_state_changed)

    service = _RecordingService(plan_behavior=behavior)
    sentinel = lambda snapshot: None  # noqa: E731
    service.on_state_changed = sentinel  # 初始安装（测试自身的唯一一次写入）

    worker = _PlanWorker(service=service, index=None, config=MagicMock(), parent=qapp)
    worker.run()

    assert service.plan_calls == 1
    assert service.writes == [sentinel], "worker 不应写 on_state_changed（摘除/恢复）"
    assert callback_during_plan == [sentinel], "规划期间回调必须保持安装"
    assert service.on_state_changed is sentinel


def test_plan_state_gate_reference_counting():
    """门闩按计数挂起：重叠 worker 之间互不干扰，过量 resume 不为负。"""
    from src.app.controllers.ai_edit_controller import _PlanStateGate

    gate = _PlanStateGate()
    assert gate.suspended() is False
    gate.suspend()          # worker A 开始
    gate.suspend()          # worker B 重叠开始
    assert gate.suspended() is True
    gate.resume()           # A 先结束，B 仍在规划
    assert gate.suspended() is True
    gate.resume()           # B 结束
    assert gate.suspended() is False
    gate.resume()           # 过量 resume：计数不为负
    assert gate.suspended() is False


def test_plan_worker_resumes_gate_before_emit(qapp):
    """worker 先恢复门闩再 emit finished_plan（主线程补回填不被误丢弃）。"""
    from src.app.controllers.ai_edit_controller import _PlanWorker, _PlanStateGate

    gate = _PlanStateGate()
    service = _RecordingService()
    worker = _PlanWorker(
        service=service, index=None, config=MagicMock(),
        state_gate=gate, parent=qapp,
    )
    gate_state_at_emit = {}
    worker.finished_plan.connect(
        lambda: gate_state_at_emit.setdefault("suspended", gate.suspended()),
    )
    worker.run()  # 同步直跑：direct connection 在 emit 点采样门闩状态

    assert gate_state_at_emit.get("suspended") is False, (
        "emit 时门闩必须已恢复（否则主线程 _refresh_state 会被丢弃）"
    )
    assert gate.suspended() is False


def _make_controller(qapp):
    """MagicMock panel/service 构造真实 AiEditController（仿 test_p1_plan_cancel）。"""
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_panel = MagicMock()
    controller = AiEditController(
        panel=fake_panel, service=fake_service, parent=qapp,
    )
    return controller, fake_panel


def test_on_service_state_dropped_while_gate_suspended(qapp):
    """挂起期间 _on_service_state 丢弃快照（不跨线程操作 panel）。"""
    controller, panel = _make_controller(qapp)
    snapshot = object()
    emitted = []
    controller.state_changed.connect(lambda s: emitted.append(s))

    controller._plan_state_gate.suspend()
    controller._on_service_state(snapshot)
    assert panel.refresh_state.call_count == 0, "挂起期间不应回填 panel"
    assert emitted == [], "挂起期间不应对外广播状态"

    controller._plan_state_gate.resume()
    controller._on_service_state(snapshot)
    assert panel.refresh_state.call_count == 1, "恢复后回填正常"
    assert emitted == [snapshot]


def test_overlapping_workers_no_cross_thread_backfill_no_callback_loss(qapp):
    """端到端复现 AUD-2000 场景。

    worker A 的 plan_variants 阻塞（模拟 LLM 调用超过 _start_plan 的 2s
    wait 超时），期间 worker B 完整跑完一轮规划：service 状态回调在两个
    worker 线程内触发，均被门闩丢弃（panel 零回填）；A 结束后回调仍是
    原对象（旧实现的"摘除/恢复"在重叠时要么复活跨线程回填、要么把回调
    恢复成 None）。
    """
    from src.app.controllers.ai_edit_controller import _PlanWorker

    controller, panel = _make_controller(qapp)
    snapshot = object()

    class _SharedService(_RecordingService):
        def plan_variants(self, index, config):
            super().plan_variants(index, config)
            # 模拟真实 service：规划路径内触发状态回调（在 worker 线程）。
            if callable(self.on_state_changed):
                self.on_state_changed(snapshot)
            if index == "slow":
                release.wait(timeout=5)
            return []

    release = threading.Event()
    service = _SharedService()
    service.on_state_changed = controller._on_service_state

    worker_a = _PlanWorker(
        service=service, index="slow", config=MagicMock(),
        state_gate=controller._plan_state_gate, parent=qapp,
    )
    worker_b = _PlanWorker(
        service=service, index="fast", config=MagicMock(),
        state_gate=controller._plan_state_gate, parent=qapp,
    )
    worker_a.start()
    worker_b.start()
    assert worker_b.wait(5000), "worker B 应正常结束"
    # B 已结束、A 仍阻塞：挂起期间 service 状态回调不得触碰 panel。
    assert panel.refresh_state.call_count == 0
    assert service.on_state_changed == controller._on_service_state

    release.set()
    assert worker_a.wait(5000), "worker A 应正常结束"

    assert panel.refresh_state.call_count == 0, "worker 线程触发的回调全被丢弃"
    assert service.writes == [controller._on_service_state], (
        "两个 worker 都不应改写 on_state_changed（旧实现此处会出现交叉写入）"
    )
    assert service.on_state_changed == controller._on_service_state, (
        "回调保持安装（不被摘除后恢复成 None）"
    )
    # 门闩已全部恢复：主线程回填恢复工作。
    assert controller._plan_state_gate.suspended() is False
    controller._on_service_state(snapshot)
    assert panel.refresh_state.call_count == 1


# ─────────────────────────────────────────────────────────────────────
# AUD-2900：UPDATE_AUX_TEX 的 layer_id 必须是 layer.id（UUID）
# ─────────────────────────────────────────────────────────────────────

class _RecordingRenderer:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def submit(self, cmd, data=None) -> None:
        self.commands.append((cmd, data))


class _EngineStub:
    def __init__(self, layers, image) -> None:
        self.layers = layers
        self.img_high = image
        self.img_full_original = image
        self.active_layer_index = 0
        self.renderer = _RecordingRenderer()
        self.history = MagicMock()

    def get_current_image(self):
        return self.img_high

    def get_active_layer(self):
        return self.layers[self.active_layer_index] if self.layers else None


def _graded_layer(name: str, lut_path: str = "/fake/lut.cube"):
    from src.features.color.core.layer import Layer

    layer = Layer(name=name)
    layer.active_lut_path = lut_path
    layer.warper_params = np.zeros((4, 4, 2), dtype=np.float32)
    return layer


def _make_layer_ctrl(tmp_path, layers):
    from src.app.controllers.layer_controller import LayerOpsMixin

    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    ctrl.engine = _EngineStub(list(layers), np.zeros((8, 8, 3), dtype=np.uint8))
    ctrl.status_bar = MagicMock()
    for attr in ("grading_panel", "layer_panel", "plugin_panel",
                 "curves_widget", "hsl_curves_panel", "mixer_panel",
                 "effects_panel", "qualifier_panel",
                 "lut_panel", "warper_panel", "mask_panel", "info_panel"):
        setattr(ctrl, attr, MagicMock())
    ctrl.current_file_is_video = False
    ctrl.save_router = MagicMock()
    ctrl.request_render = MagicMock()
    ctrl._trigger_autosave = MagicMock()
    ctrl.update_canvas = MagicMock()
    return ctrl


def _aux_tex_commands(renderer: _RecordingRenderer):
    from src.features.color.core.async_renderer import RenderCommand

    return [data for cmd, data in renderer.commands
            if cmd == RenderCommand.UPDATE_AUX_TEX]


def _assert_uuid_keys(aux_cmds: list[dict], layer_ids: list[str]) -> None:
    lut_cmds = [d for d in aux_cmds if d["type"] == "lut3d"]
    warper_cmds = [d for d in aux_cmds if d["type"] == "warper"]
    assert lut_cmds, "应提交 LUT3D 纹理"
    assert warper_cmds, "应提交 warper 纹理"
    for data in lut_cmds + warper_cmds:
        layer_id = data["layer_id"]
        assert isinstance(layer_id, str) and layer_id in layer_ids, (
            f"layer_id 必须是 layer.id（UUID 字符串）且在当前图层集合内，"
            f"实际：{layer_id!r}（id(layer) 是 int 内存地址，渲染端按 UUID 查找）"
        )


def test_apply_grade_from_library_uses_layer_uuid(qapp, tmp_path):
    """on_apply_grade_from_library：UPDATE_AUX_TEX 键 = layer.id。"""
    ctrl = _make_layer_ctrl(tmp_path, [_graded_layer("target")])
    src = _graded_layer("source")
    ctrl.save_router._get_project_path_for_file.return_value = "/fake/src.topos"
    ctrl.save_router.load_single_asset_data.return_value = {"layers": [src]}

    ctrl.on_apply_grade_from_library("/fake/source.arw")

    layer_ids = [layer.id for layer in ctrl.engine.layers]
    assert layer_ids == [ctrl.engine.layers[0].id]
    assert layer_ids[0] != id(ctrl.engine.layers[0]), "sanity：UUID ≠ 内存地址"
    _assert_uuid_keys(_aux_tex_commands(ctrl.engine.renderer), layer_ids)


def test_apply_grade_from_preset_uses_layer_uuid(qapp, tmp_path):
    """on_apply_grade_from_preset：UPDATE_AUX_TEX 键 = layer.id。"""
    ctrl = _make_layer_ctrl(tmp_path, [_graded_layer("target")])
    src = _graded_layer("preset")
    ctrl.save_router.load_single_asset_data.return_value = {
        "layers": [src], "active_layer_index": 0,
    }

    ctrl.on_apply_grade_from_preset("/fake/preset.topos")

    layer_ids = [layer.id for layer in ctrl.engine.layers]
    _assert_uuid_keys(_aux_tex_commands(ctrl.engine.renderer), layer_ids)


def test_apply_preset_categories_uses_layer_uuid(qapp, tmp_path):
    """on_apply_preset_categories_requested：粘贴后重提交键 = layer.id。"""
    ctrl = _make_layer_ctrl(tmp_path, [_graded_layer("target")])
    src = _graded_layer("preset-src")
    ctrl.save_router.load_single_asset_data.return_value = {
        "layers": [src], "active_layer_index": 0,
    }

    ctrl.on_apply_preset_categories_requested(
        "/fake/preset.topos", categories={"lut", "warper"})

    # 粘贴把源 LUT/warper 应用到当前图层；重提交必须按 UUID 寻址。
    target = ctrl.engine.layers[0]
    assert target.active_lut_path == "/fake/lut.cube"
    assert target.warper_params is not None
    _assert_uuid_keys(_aux_tex_commands(ctrl.engine.renderer), [target.id])


# ─────────────────────────────────────────────────────────────────────
# AUD-3800：rename_item 重名冲突检查
# ─────────────────────────────────────────────────────────────────────

def _make_rename_panel(monkeypatch, new_name):
    from src.app.ui.library_panel import LibraryPanel

    panel = LibraryPanel.__new__(LibraryPanel)

    def fake_get_text(*args, **kwargs):
        return new_name, True

    monkeypatch.setattr(
        "src.app.ui.library_panel.QInputDialog.getText", staticmethod(fake_get_text))

    warnings = []
    criticals = []
    monkeypatch.setattr(
        "src.app.ui.library_panel.QMessageBox.warning",
        lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(
        "src.app.ui.library_panel.QMessageBox.critical",
        lambda *a, **k: criticals.append(a))
    return panel, warnings, criticals


def test_rename_refuses_existing_target(qapp, tmp_path, monkeypatch):
    """目标名已存在：拒绝重命名（不覆盖、告警提示、内容完好）。"""
    src = tmp_path / "a.txt"
    src.write_text("A-content")
    victim = tmp_path / "b.txt"
    victim.write_text("B-content")
    panel, warnings, criticals = _make_rename_panel(monkeypatch, "b.txt")

    panel.rename_item(str(src))

    assert src.read_text() == "A-content", "源文件必须原样保留"
    assert victim.read_text() == "B-content", "同名文件不得被覆盖"
    assert warnings, "冲突时应给出告警"
    assert not criticals


def test_rename_allows_new_name(qapp, tmp_path, monkeypatch):
    """目标名不存在：正常重命名。"""
    src = tmp_path / "a.txt"
    src.write_text("A-content")
    panel, warnings, criticals = _make_rename_panel(monkeypatch, "renamed.txt")

    panel.rename_item(str(src))

    assert (tmp_path / "renamed.txt").read_text() == "A-content"
    assert not src.exists()
    assert not warnings and not criticals


def test_rename_same_name_is_noop(qapp, tmp_path, monkeypatch):
    """输入与现名相同：no-op，不弹冲突告警。"""
    src = tmp_path / "a.txt"
    src.write_text("A-content")
    panel, warnings, criticals = _make_rename_panel(monkeypatch, "a.txt")

    panel.rename_item(str(src))

    assert src.read_text() == "A-content"
    assert not warnings and not criticals
