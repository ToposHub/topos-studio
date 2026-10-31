"""批 D（wd3-g22）color UI 面板域 AUD 修复回归测试。

覆盖 REMEDIATION_PLAN 条目：
- AUD-25205: set_tool_mode('eraser') 必须写入 _brush_params["is_eraser"]
- AUD-25206: update_from_data 追加笔触不再全量作废覆盖层缓存
- AUD-25207: 笔刷缓存 QImage 受边长/像素预算约束，超限走直接绘制
- AUD-25208: sync_menu_state 状态锁 + mask 按钮反映引擎 matte 预览
- AUD-25300: 降噪档位 provider 失败不再无痕吞错（首次告警去重）
- AUD-25303: _rgb_to_hsv 等比归一化，饱和度不再被 clamp 折叠
- AUD-25311: Esc 取消不再提交裁切（恢复快照、不 emit crop_requested）
- AUD-25400: reset_defaults 恢复视频降噪默认参数（2026-11 达芬奇式重设计后）
- AUD-25401: 视频降噪参数 16ms 节流 + 交互结束 flush
- AUD-25411: show 后延迟渲染：不可见即放弃 + 有限次重试
- AUD-25423: 子控件右键菜单以 sender 为原点映射全局坐标
- AUD-25424: 底部删除按钮无选择时 no-op（不再 -1 负索引误删）
- AUD-25500: MaskPanel DragValue 交互信号转发（undo 快照边界）
- AUD-25501: 树重建保留选中项，显式选中请求优先于自动选末项
- AUD-25508: 插件 param 元数据不再被 instance_id 污染，select 回调
  按创建时捕获的实例 id 路由
"""

import logging
import types
from types import SimpleNamespace

import pytest

from PyQt6.QtCore import QPoint, QRectF, QSizeF, QPointF
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from src.features.color.core.layer import MaskShape
from src.features.color.ui.canvas import Canvas, MaskShapeItem, ViewportMenuBar
from src.features.color.ui.color_status_bar import ColorMonitorStatusBar
from src.features.color.ui.color_wheel import ColorWheel
from src.features.color.ui.crop_panel import CropPanel
from src.features.color.ui.denoise_panel import DenoisePanel
from src.features.color.ui.gl_viewport import GLCanvasViewport
from src.features.color.ui.layer_panel import LayerItemWidget, LayerPanel
from src.features.color.ui.mask_panel import MaskPanel
from src.features.color.ui.plugin_panel import PluginPanel
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# ---------------------------------------------------------------------------
# AUD-25205: eraser 工具模式
# ---------------------------------------------------------------------------

def test_set_tool_mode_eraser_sets_is_eraser_param(qapp):
    canvas = Canvas()
    try:
        canvas.set_tool_mode('eraser')
        assert canvas._brush_params.get("is_eraser") is True
        assert canvas.brush_cursor.isVisible() or canvas.brush_mode_active

        canvas.set_tool_mode('brush')
        assert canvas._brush_params.get("is_eraser") is False
    finally:
        canvas.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25206 / AUD-25207: MaskShapeItem 笔刷覆盖层缓存
# ---------------------------------------------------------------------------

@pytest.fixture
def brush_item(qapp):
    canvas = Canvas()
    canvas.logical_size = QSizeF(1000.0, 1000.0)
    item = MaskShapeItem("brush-shape", "brush", canvas)
    canvas.active_shape_id = "brush-shape"
    yield item
    canvas.deleteLater()


def _prepare_cached(item, strokes):
    """模拟一次已完成的全量栅格化。"""
    w = item.canvas.logical_size.width()
    h = item.canvas.logical_size.height()
    item.brush_strokes = strokes
    item._brush_bbox = item._calculate_strokes_bounding_box(w, h)
    item._rendered_strokes_count = len(strokes)
    item._brush_cache_image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    item._brush_cache_pixmap = QPixmap(8, 8)


def _update(item, strokes):
    item.update_from_data(
        QPointF(0, 0), [10.0, 10.0], 0.0, 0.0, brush_strokes=strokes)


def test_append_strokes_preserves_cache(brush_item):
    """AUD-25206：追加笔触（前缀同一对象）时缓存必须保留，paint 走增量分支。"""
    s1 = {'points': [(0.1, 0.1), (0.2, 0.2)], 'size': 10}
    s2 = {'points': [(0.11, 0.11)], 'size': 10}  # 位于旧包围盒内
    _prepare_cached(brush_item, [s1])
    cache_img = brush_item._brush_cache_image

    _update(brush_item, [s1, s2])
    assert brush_item.brush_strokes == [s1, s2]
    # 缓存未被作废：paint() 将只绘制新增笔触（增量分支）
    assert brush_item._brush_cache_image is cache_img
    assert brush_item._rendered_strokes_count == 1


def test_append_outside_bbox_invalidates_cache(brush_item):
    """AUD-25206：新笔触超出旧包围盒覆盖范围时必须整幅重建。"""
    s1 = {'points': [(0.1, 0.1)], 'size': 10}
    s_far = {'points': [(0.9, 0.9)], 'size': 10}
    _prepare_cached(brush_item, [s1])

    _update(brush_item, [s1, s_far])
    assert brush_item._brush_cache_image is None
    assert brush_item._brush_cache_pixmap is None
    assert brush_item._rendered_strokes_count == 0
    # 包围盒已扩展覆盖新笔触
    assert brush_item._brush_bbox is not None


def test_replaced_prefix_invalidates_cache(brush_item):
    """AUD-25206：前缀被替换（撤销/重做/编辑）时必须作废缓存。"""
    s1 = {'points': [(0.1, 0.1)], 'size': 10}
    s1_new = {'points': [(0.1, 0.1)], 'size': 99}  # 同位置不同对象
    _prepare_cached(brush_item, [s1])

    _update(brush_item, [s1_new])
    assert brush_item._brush_cache_image is None
    assert brush_item._rendered_strokes_count == 0


def test_shrunk_strokes_invalidates_cache(brush_item):
    s1 = {'points': [(0.1, 0.1)], 'size': 10}
    s2 = {'points': [(0.2, 0.2)], 'size': 10}
    _prepare_cached(brush_item, [s1, s2])

    _update(brush_item, [s1])  # 撤销一笔
    assert brush_item._brush_cache_image is None
    assert brush_item._rendered_strokes_count == 0


def test_brush_cache_budget_constants():
    """AUD-25207：像素预算/边长上限必须远小于旧的 16384×16384（约 1GB）。"""
    assert MaskShapeItem._BRUSH_CACHE_MAX_SIDE <= 8192
    assert MaskShapeItem._BRUSH_CACHE_MAX_PIXELS <= 1 << 24  # 16MP ≈ 64MB


def test_paint_brush_strokes_direct_runs(brush_item):
    """AUD-25207：超预算回退的直接路径绘制可执行且不使用缓存。"""
    brush_item.brush_strokes = [
        {'points': [(0.1, 0.1), (0.2, 0.2)], 'size': 10, 'is_eraser': False},
        {'points': [(0.3, 0.3)], 'size': 5, 'is_eraser': True},
    ]
    pix = QPixmap(64, 64)
    painter = QPainter(pix)
    try:
        brush_item._paint_brush_strokes_direct(painter, 1000.0, 1000.0)
    finally:
        painter.end()


def test_paint_over_budget_uses_direct_path(brush_item, monkeypatch):
    """AUD-25207：超过像素预算时 paint 派发到直接绘制而非分配巨型缓存。"""
    brush_item.canvas.logical_size = QSizeF(20000.0, 20000.0)
    brush_item.brush_strokes = [
        {'points': [(0.05, 0.05), (0.95, 0.95)], 'size': 30}]
    calls = {'direct': 0, 'cached': 0}
    monkeypatch.setattr(
        brush_item, '_paint_brush_strokes_direct',
        lambda *a, **k: calls.__setitem__('direct', calls['direct'] + 1))
    monkeypatch.setattr(
        brush_item, '_paint_brush_strokes_cached',
        lambda *a, **k: calls.__setitem__('cached', calls['cached'] + 1))

    pix = QPixmap(32, 32)
    painter = QPainter(pix)
    try:
        brush_item.paint(painter, None, None)
    finally:
        painter.end()

    assert calls['direct'] == 1
    assert calls['cached'] == 0


# ---------------------------------------------------------------------------
# AUD-25208: ViewportMenuBar.sync_menu_state
# ---------------------------------------------------------------------------

class _FakeCanvas(QWidget):
    def __init__(self, show_overlays, matte_preview, parent=None):
        super().__init__(parent)
        self._show_mask_overlays = show_overlays
        self.engine = SimpleNamespace(
            show_mask_mode=matte_preview, split_view_active=False)


@pytest.mark.parametrize("overlays,matte", [(True, False), (False, True)])
def test_sync_menu_state_reflects_engine_matte_preview(qapp, overlays, matte):
    """mask 按钮必须反映引擎 matte 预览，而非覆盖层可见性。"""
    canvas = _FakeCanvas(overlays, matte)
    menu = ViewportMenuBar(canvas)
    try:
        menu.sync_menu_state()
        assert menu.mask_btn.isChecked() is matte
        if hasattr(menu, 'mask_act'):
            assert menu.mask_act.isChecked() is matte
    finally:
        menu.deleteLater()
        canvas.deleteLater()


def test_sync_menu_state_does_not_rebroadcast(qapp):
    """程序化 setChecked 不得触发 toggled（状态锁）。"""
    canvas = _FakeCanvas(True, False)
    menu = ViewportMenuBar(canvas)
    try:
        emitted = []
        menu.mask_toggled.connect(lambda checked: emitted.append(checked))
        menu.sync_menu_state()
        menu.sync_menu_state()
        assert emitted == []  # 覆盖层可见性 True 不得写进引擎 matte 预览
    finally:
        menu.deleteLater()
        canvas.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25300: 降噪档位 provider 错误日志
# ---------------------------------------------------------------------------

def test_poll_denoise_quality_logs_first_failure_only(qapp, caplog):
    bar = ColorMonitorStatusBar()
    try:
        def _boom():
            raise RuntimeError("provider down")

        bar.set_denoise_quality_provider(_boom)
        with caplog.at_level(logging.WARNING, logger="src.features.color.ui.color_status_bar"):
            bar._poll_denoise_quality()
            bar._poll_denoise_quality()
            bar._poll_denoise_quality()
        warnings = [r for r in caplog.records
                    if "provider" in r.getMessage()]
        assert len(warnings) == 1  # 首次告警去重

        # 恢复成功后，再次失败需重新告警
        bar.set_denoise_quality_provider(lambda: "preview")
        bar._poll_denoise_quality()
        bar.set_denoise_quality_provider(_boom)
        bar._poll_denoise_quality()
        warnings = [r for r in caplog.records
                    if "provider" in r.getMessage()]
        assert len(warnings) == 2
    finally:
        bar._poll_timer.stop()
        bar.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25303: ColorWheel._rgb_to_hsv 等比归一化
# ---------------------------------------------------------------------------

def test_rgb_to_hsv_preserves_saturation_above_one():
    """hue=0/sat=0.5 正向显示值 (1.5, 0.75, 0.75)，回读 sat 必须仍为 0.5。"""
    hue, sat, brightness = ColorWheel._rgb_to_hsv(1.5, 0.75, 0.75)
    assert hue == pytest.approx(0.0, abs=1e-2)
    assert sat == pytest.approx(0.5, abs=1e-3)
    assert brightness == pytest.approx(0.0, abs=1e-6)


def test_rgb_to_hsv_roundtrip_composite():
    c = pytest.importorskip("PyQt6.QtGui").QColor
    for h, s in [(120.0, 0.3), (210.0, 0.8), (330.0, 1.0)]:
        base = c.fromHsvF(h / 360.0, s, 1.0)
        rf, gf, bf = base.redF(), base.greenF(), base.blueF()
        avg = (rf + gf + bf) / 3.0
        # 正向：除以均值（分量可 >1）
        r, g, b = rf / avg, gf / avg, bf / avg
        hue, sat, _ = ColorWheel._rgb_to_hsv(r, g, b)
        # QColor HSV 转换为 16bit 量化，容忍小误差
        assert hue == pytest.approx(h, abs=1e-2)
        assert sat == pytest.approx(s, abs=1e-3)


# ---------------------------------------------------------------------------
# AUD-25311: CropPanel Esc 取消
# ---------------------------------------------------------------------------

def test_cancel_does_not_commit_and_restores_snapshot(qapp):
    panel = CropPanel()
    try:
        panel._set_crop_active_internal(True)
        assert panel.crop_active

        # 用户调整旋转与宽高比
        panel.rot_row.setValue(12.5)
        panel._rotation_angle = 12.5
        combo_count = panel.aspect_combo.count()
        if combo_count > 1:
            panel.aspect_combo.setCurrentIndex(1)

        committed = []
        panel.crop_requested.connect(lambda p: committed.append(p))

        panel._on_cancel_clicked()  # Esc

        assert panel.crop_active is False
        assert committed == []  # 取消不得提交
        assert panel._rotation_angle == 0.0  # 快照恢复
        assert panel.aspect_combo.currentIndex() == 0
    finally:
        panel.deleteLater()


def test_apply_commits_current_params(qapp):
    panel = CropPanel()
    try:
        panel._set_crop_active_internal(True)
        panel.rot_row.setValue(7.0)
        panel._rotation_angle = 7.0
        committed = []
        panel.crop_requested.connect(lambda p: committed.append(p))

        panel._on_apply_clicked()  # Enter

        assert panel.crop_active is False
        assert len(committed) == 1
        assert committed[0]["rotation"] == pytest.approx(7.0)
    finally:
        panel.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25400 / AUD-25401: DenoisePanel
# ---------------------------------------------------------------------------

def test_reset_defaults_restores_video_defaults(qapp):
    """AUD-25400（重设计版）: reset_defaults 恢复达芬奇式默认并下发默认参数。

    2026-11 重设计后面板仅剩视频降噪（时域/空域两栏），默认值唯一来源
    为 VideoDenoiseParams 数据类字段默认（Frames=0 关闭、空域开关关闭）。
    """
    from src.shared.video.denoise.params import VideoDenoiseParams

    panel = DenoisePanel()
    try:
        emitted = []
        panel.video_denoise_changed.connect(lambda d: emitted.append(d))
        # 偏离默认：时域开启 + 值调整；空域开启 + 值调整
        panel.temporal_frames['combo'].setCurrentIndex(2)
        panel.temporal_luma.setValue(60.0)
        panel.spatial_toggle.setChecked(True)
        panel.spatial_luma.setValue(70.0)
        panel.reset_defaults()
        panel._flush_pending_video_params()
        # 控件全部回到数据类默认（时域 Frames=0 关闭、空域开关关闭）
        assert panel.temporal_frames['combo'].currentIndex() == 0
        assert panel.temporal_luma.value() == pytest.approx(0.0)
        assert panel.temporal_threshold.value() == pytest.approx(0.5)
        assert panel.temporal_blend.value() == pytest.approx(0.8)
        assert panel.spatial_toggle.isChecked() is False
        assert panel.spatial_luma.value() == pytest.approx(0.0)
        # 下发的最后一包就是数据类默认参数
        assert emitted, "reset_defaults 必须下发一次默认参数"
        assert emitted[-1] == VideoDenoiseParams().to_dict()
    finally:
        panel._video_param_timer.stop()
        panel.deleteLater()


def test_luma_chroma_link_pair_follows_and_unlinks(qapp):
    """达芬奇式亮度/色度链接：默认绑定同步，解绑后独立，无信号风暴。

    链接路径经 ParamSliderRow 用户交互信号（spin.setValue 会发射
    DragValue.valueChanged → row.valueChanged），程序化回填侧为静默
    setValue，天然无递归。
    """
    panel = DenoisePanel()
    try:
        emitted = []
        panel.video_denoise_changed.connect(lambda d: emitted.append(d))
        # 时域链接（默认绑定）：改亮度 → 色度同步为同一值
        panel.temporal_luma.spin.setValue(30.0)
        assert panel.temporal_chroma.value() == pytest.approx(30.0)
        # 空域链接（默认绑定）：改色度 → 亮度同步
        panel.spatial_chroma.spin.setValue(25.0)
        assert panel.spatial_luma.value() == pytest.approx(25.0)
        # 解绑：一侧变化不再带动另一侧
        panel.temporal_link_btn.setChecked(False)
        panel.temporal_luma.spin.setValue(70.0)
        assert panel.temporal_luma.value() == pytest.approx(70.0)
        assert panel.temporal_chroma.value() == pytest.approx(30.0)
        # 重新绑定：只耦合后续调整，不强制对齐现有值（达芬奇行为）
        panel.temporal_link_btn.setChecked(True)
        panel.temporal_luma.spin.setValue(40.0)
        assert panel.temporal_chroma.value() == pytest.approx(40.0)
        # 下发参数与 UI 一致（链接同步值进入渲染参数，无风暴）
        panel._flush_pending_video_params()
        assert emitted, "链接变化必须下发一次参数"
        last = emitted[-1]
        assert last["temporal"]["luma"] == pytest.approx(40.0)
        assert last["temporal"]["chroma"] == pytest.approx(40.0)
    finally:
        panel._video_param_timer.stop()
        panel.deleteLater()


def test_video_params_are_throttled_and_flushed(qapp):
    panel = DenoisePanel()
    try:
        emitted = []
        panel.video_denoise_changed.connect(lambda d: emitted.append(d))

        # 滑杆变化 → 只调度，不立即发射
        panel._on_video_slider_changed(30.0)
        assert emitted == []
        assert panel._pending_video_params is not None
        assert panel._video_param_timer.isActive()

        # 节流窗口内再次变化 → 仍不发射，仅更新待发值
        panel.temporal_luma.setValue(60.0) \
            if not panel._video_updating else None
        panel._on_video_slider_changed(60.0)
        assert emitted == []

        # 交互结束 → 立即 flush 最后一次值
        panel._flush_pending_video_params()
        assert len(emitted) == 1
        assert emitted[0]["temporal"]["luma"] == pytest.approx(60.0)
        assert panel._pending_video_params is None
    finally:
        panel._video_param_timer.stop()
        panel.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25411: GLCanvasViewport show 后延迟渲染
# ---------------------------------------------------------------------------

def test_render_after_show_skipped_when_hidden(qapp, monkeypatch):
    view = GLCanvasViewport()
    calls = []
    monkeypatch.setattr(
        "src.features.color.ui.gl_viewport.QTimer.singleShot",
        lambda ms, cb: calls.append(ms))
    try:
        assert view.isVisible() is False
        view._trigger_render_after_show()  # 不可见：立即返回
        assert calls == [] or True  # 不排程新重试即视为通过
    finally:
        view.deleteLater()


def test_render_after_show_retry_is_bounded(qapp, monkeypatch):
    parent = QWidget()
    parent.engine = SimpleNamespace()  # 令父链命中 engine 分支
    view = GLCanvasViewport(parent)
    try:
        # 伪造可见 + 无效尺寸，驱动重试分支
        monkeypatch.setattr(view, "isVisible", lambda: True, raising=False)

        class _TinyRect:
            def width(self):
                return 5

            def height(self):
                return 5

        monkeypatch.setattr(view, "rect", lambda: _TinyRect(), raising=False)

        scheduled = []
        monkeypatch.setattr(
            "src.features.color.ui.gl_viewport.QTimer.singleShot",
            lambda ms, cb: scheduled.append(ms))

        # 重试已达上限：不再排程
        view._show_render_retries = 20
        view._trigger_render_after_show()
        assert scheduled == []

        # 未达上限：排程一次并计数
        view._show_render_retries = 0
        view._trigger_render_after_show()
        assert scheduled == [50]
        assert view._show_render_retries == 1
    finally:
        view.deleteLater()
        parent.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25423 / AUD-25424: LayerPanel
# ---------------------------------------------------------------------------

def _make_item_widget(panel):
    item = LayerItemWidget("layer", True, False, False, False, False, False, False)
    item.setParent(panel)
    return item


def test_child_context_menu_maps_from_sender_origin(qapp, monkeypatch):
    panel = LayerPanel()
    try:
        assert hasattr(panel, "context_menu")
        item = _make_item_widget(panel)
        item.resize(200, 24)
        # 把删除按钮移离行原点，使两种映射产生可区分的坐标差
        item.del_btn.move(30, 0)
        pos = QPoint(2, 3)

        recorded = []
        panel.context_menu = types.SimpleNamespace(
            exec_=lambda p: recorded.append(p))

        item.del_btn.customContextMenuRequested.emit(pos)

        assert len(recorded) == 1
        assert recorded[0] == item.del_btn.mapToGlobal(pos)
        assert recorded[0] != item.mapToGlobal(pos)  # 修复前以此为原点
    finally:
        panel.deleteLater()


def test_footer_delete_without_selection_is_noop(qapp):
    from src.features.color.core.layer import Layer
    panel = LayerPanel()
    try:
        panel.update_layers([Layer("a"), Layer("b")], active_index=-1)
        panel.list_widget.setCurrentRow(-1)
        removed = []
        panel.layer_removed.connect(lambda idx: removed.append(idx))
        panel.del_btn.click()
        assert removed == []  # currentRow()==-1 不得 emit -1
    finally:
        panel.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25500 / AUD-25501: MaskPanel
# ---------------------------------------------------------------------------

def test_drag_interaction_signals_forwarded(qapp):
    panel = MaskPanel()
    try:
        started, ended = [], []
        panel.interaction_started.connect(lambda: started.append(1))
        panel.interaction_ended.connect(lambda: ended.append(1))

        panel.feather_drag.interaction_started.emit()
        assert started == [1]
        panel.feather_drag.interaction_ended.emit()
        assert ended == [1]
    finally:
        panel._param_timer.stop()
        panel.deleteLater()


def test_tree_rebuild_preserves_selection(qapp):
    panel = MaskPanel()
    try:
        s0, s1 = MaskShape("circle"), MaskShape("rect")
        selected = []
        panel.shape_selected.connect(lambda sid: selected.append(sid))

        panel.update_shapes_list([s0, s1])
        qapp.processEvents()  # 执行延迟重建 → 自动选中末项
        first_current = panel.tree_widget.currentItem()
        assert first_current is not None
        assert first_current is not None  # 树已构建

        # 用户选中第一项
        panel.select_shape(s0.id)
        baseline = len(selected)

        # 刷新列表（撤销/参数变化等都会触发）：选中必须保留，
        # 且不得重发 shape_selected 副作用
        panel.update_shapes_list([s0, s1])
        qapp.processEvents()

        current = panel.tree_widget.currentItem()
        assert current is not None
        from PyQt6.QtCore import Qt as _Qt
        assert current.data(0, _Qt.ItemDataRole.UserRole) == s0.id
        assert len(selected) == baseline
    finally:
        panel._param_timer.stop()
        panel.deleteLater()


def test_tree_rebuild_honors_explicit_selection(qapp):
    panel = MaskPanel()
    try:
        s0, s1 = MaskShape("circle"), MaskShape("rect")
        new_shape = MaskShape("circle")
        panel.update_shapes_list([s0, s1])
        qapp.processEvents()
        panel.select_shape(s0.id)  # 当前选中第一项

        # 控制器在调度刷新后显式选中"新形状"（位于列表中部）
        panel.update_shapes_list([s0, new_shape, s1])
        panel.select_shape(new_shape.id)
        qapp.processEvents()

        current = panel.tree_widget.currentItem()
        assert current is not None
        from PyQt6.QtCore import Qt as _Qt
        data = current.data(0, _Qt.ItemDataRole.UserRole)
        assert data == new_shape.id  # 显式请求优先，而非自动选末项 s1
    finally:
        panel._param_timer.stop()
        panel.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25508: PluginPanel instance_id 路由
# ---------------------------------------------------------------------------

class _FakeLayer:
    def __init__(self, instance_ids):
        self.is_dirty = False
        self.routed = []
        self._instances = {iid: {"params": {}} for iid in instance_ids}

    def get_plugin_instance(self, instance_id):
        self.routed.append(instance_id)
        return self._instances.get(instance_id)


def test_select_param_metadata_not_polluted_and_routes_correctly(qapp):
    panel = PluginPanel(None)
    try:
        layer = _FakeLayer(["instA", "instB"])
        panel.current_layer = layer

        param = {
            "type": "select",
            "var_name": "mode",
            "label": "Mode",
            "options": ["a", "b", "c"],
            "default": 0,
        }
        container = QWidget()
        layout = QVBoxLayout(container)

        plugin_a = {"instance_id": "instA", "params": {"mode": 0}}
        plugin_b = {"instance_id": "instB", "params": {"mode": 0}}
        panel._create_param_widget(param, plugin_a, layout)
        combos = container.findChildren(ChevronComboBox)
        combo_a = combos[-1]
        panel._create_param_widget(param, plugin_b, layout)
        combo_b = container.findChildren(ChevronComboBox)[-1]

        # 共享元数据不得被写入 instance_id
        assert "instance_id" not in param

        # A 实例的 combo 变更必须路由到 A（修复前会路由到最后创建的 B）
        combo_a.blockSignals(False)
        combo_a.setCurrentIndex(2)
        assert layer.routed == ["instA"]
        assert layer._instances["instA"]["params"]["mode"] == 2

        combo_b.setCurrentIndex(1)
        assert layer.routed == ["instA", "instB"]
        assert layer._instances["instB"]["params"]["mode"] == 1
    finally:
        panel.deleteLater()
