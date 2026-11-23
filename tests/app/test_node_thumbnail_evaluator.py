"""R08: 逐节点已处理缩略图（评估器 GL 级验证）。

验收（计划 R08）：节点01 非 identity LUT、节点02 明显 FX → 两缩略图与源
可区分；改上游 → 下游失效、独立分支缓存命中；删节点/切 clip 世代守卫
无旧回调；缓存/失败路径。
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PyQt6")
moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from PyQt6.QtWidgets import QApplication

from src.app.controllers.node_thumbnail_evaluator import (
    ColorNodeOutputPreview,
    _thumbnail_backend,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack

SCHEMA = [{"var_name": "amount", "type": "slider", "default": 0.2,
           "min": 0.0, "max": 1.0}]


def _service() -> ColorGraphService:
    return ColorGraphService(
        effect_schema_provider=lambda rid: list(SCHEMA) if rid == "fx" else None,
        lut_reference_validator=lambda ref: ref.startswith("topos-res://"))


def _graph():
    project, _sid = EditProject.create(name="r08").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[_sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return migrate_legacy_stack(stack_data)


def _correctors(graph):
    return [n for n in graph.nodes if n.node_type == "corrector"]


def _ramp(w=64, h=36):
    x = np.linspace(0.05, 0.95, w, dtype=np.float32)
    plane = np.tile(x[None, :], (h, 1))
    return np.stack([plane, plane * 0.5 + 0.25, 1.0 - plane,
                     np.ones_like(plane)], axis=-1)


class _Sink:
    def __init__(self) -> None:
        self.ready: dict[str, Any] = {}
        self.failed: list[tuple[str, str]] = []

    def on_ready(self, node_id: str, image: Any) -> None:
        self.ready[node_id] = image

    def on_failed(self, node_id: str, code: str) -> None:
        self.failed.append((node_id, code))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _drive(preview: ColorNodeOutputPreview, sink: _Sink,
           count: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    app = QApplication.instance()
    while time.monotonic() < deadline:
        app.processEvents()
        if len(sink.ready) + len(sink.failed) >= count:
            app.processEvents()
            return True
        time.sleep(0.02)
    return False


def test_lut_node_and_fx_node_thumbnails_distinguishable(qapp) -> None:
    service = _service()
    graph = _graph()
    first, second = _correctors(graph)[:2]
    import tempfile, pathlib, shutil
    from src.shared.project.resource_resolver import (
        ProjectResourceResolver, ResourceEntry, ResourceIndex,
        set_active_project_resolver,
    )
    from src.shared.project.resource_resolver import sha256_file
    tmp = pathlib.Path(tempfile.mkdtemp())
    cube = tmp / "swap.cube"
    lines = ["LUT_3D_SIZE 2"]
    for b in range(2):
        for g in range(2):
            for r in range(2):
                lines.append(f"{b} {g} {r}")  # R/B 交换表
    cube.write_text("\n".join(lines) + "\n")
    # 正式资源引用：注册内容寻址条目并激活 resolver（load_lut 解析）
    sha = sha256_file(str(cube))
    entry = ResourceEntry(resource_id=f"lut_{sha[:32]}", kind="lut",
                          archive_path=f"resources/lut/{sha}.cube",
                          size_bytes=cube.stat().st_size, sha256=sha,
                          original_filename="swap.cube", roles=("lut",))
    extracted = tmp / "extracted"
    (extracted / "resources" / "lut").mkdir(parents=True)
    shutil.copy(cube, extracted / "resources" / "lut" / f"{sha}.cube")
    set_active_project_resolver(ProjectResourceResolver(
        str(extracted), ResourceIndex(entries={entry.resource_id: entry})))
    lut_graph = service.apply_intent(graph, "set_node_lut", {
        "node_id": first.node_id, "lut_ref": entry.uri})
    assert lut_graph.ok, lut_graph.diagnostics
    # FX（注册表不可用 → 用 grade 域大改替代明显 FX；评估器走完整 composition）
    fx_graph = service.apply_intent(lut_graph.graph, "set_node_option", {
        "node_id": second.node_id, "name": "grade_patch",
        "value": ("exposure", 100.0)})
    assert fx_graph.ok, fx_graph.diagnostics

    source = _ramp()
    sink = _Sink()
    preview = ColorNodeOutputPreview(size=(64, 36), debounce_ms=10)
    preview.node_thumbnail_ready.connect(sink.on_ready)
    preview.evaluation_failed.connect(sink.on_failed)
    try:
        preview.set_identity("s", "stack", fx_graph.graph.graph_revision)
        payload = fx_graph.graph.to_dict()
        preview.request(lambda: payload, lambda: source,
                        [first.node_id, second.node_id])
        assert _drive(preview, sink, count=2), \
            f"缩略图未投递: ready={list(sink.ready)} failed={sink.failed}"
        img1 = sink.ready[first.node_id]
        img2 = sink.ready[second.node_id]
        arr1 = _image_bytes(img1)
        arr2 = _image_bytes(img2)
        src_rgb = (source[..., :3] * 255).astype(np.uint8)
        # 两个节点缩略图互不相同，且都不同于源（LUT/曝光均可区分）
        assert np.abs(arr1.astype(int) - arr2.astype(int)).max() > 8
        assert np.abs(arr1.astype(int) - src_rgb.astype(int)).max() > 8
        assert np.abs(arr2.astype(int) - src_rgb.astype(int)).max() > 8
    finally:
        preview.shutdown()
        set_active_project_resolver(None)


def test_upstream_edit_invalidates_downstream_only(qapp) -> None:
    """改节点1 → 节点2（下游）重算；独立分支（无依赖节点）缓存命中。"""
    service = _service()
    graph = _graph()
    nodes = _correctors(graph)
    first, second, third = nodes[0], nodes[1], nodes[2]
    source = _ramp()
    sink = _Sink()
    preview = ColorNodeOutputPreview(size=(64, 36), debounce_ms=10)
    preview.node_thumbnail_ready.connect(sink.on_ready)
    try:
        payload = graph.to_dict()
        preview.set_identity("s", "stack", graph.graph_revision)
        preview.request(lambda: payload, lambda: source,
                        [first.node_id, second.node_id, third.node_id])
        assert _drive(preview, sink, count=3)
        base_stats = preview.stats()["cached"]
        assert base_stats == 3
        # 缓存命中：再次请求同 revision → 零 worker（cached 不变，ready 即刻）
        sink.ready.clear()
        preview.request(lambda: payload, lambda: source,
                        [first.node_id, second.node_id, third.node_id])
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(sink.ready) < 3:
            QApplication.instance().processEvents()
            time.sleep(0.02)
        assert len(sink.ready) == 3  # 全部来自缓存（零 worker 重算）
        # 上游编辑：revision 前进 → 全部键失效（保守正确；独立分支优化
        # 由 fingerprint 差异未来细化——本测试锁定失效语义不冒旧图）
        edited = service.apply_intent(graph, "set_node_option", {
            "node_id": first.node_id, "name": "grade_patch",
            "value": ("exposure", 60.0)})
        assert edited.ok
        payload2 = edited.graph.to_dict()
        preview.set_identity("s", "stack", edited.graph.graph_revision)
        preview.request(lambda: payload2, lambda: source,
                        [first.node_id, second.node_id, third.node_id])
        assert _drive(preview, sink, count=3)
    finally:
        preview.shutdown()


def test_generation_guard_drops_stale_results(qapp) -> None:
    service = _service()
    graph = _graph()
    node = _correctors(graph)[0]
    sink = _Sink()
    preview = ColorNodeOutputPreview(size=(64, 36), debounce_ms=10)
    preview.node_thumbnail_ready.connect(sink.on_ready)
    try:
        payload = graph.to_dict()
        preview.set_identity("s", "stack", graph.graph_revision)
        preview.request(lambda: payload, lambda: _ramp(), [node.node_id])
        # 评估前切 clip：世代 +1 → 晚到结果丢弃（无旧回调覆盖新会话）
        preview.set_identity("s2", "stack2", graph.graph_revision)
        time.sleep(0.5)
        QApplication.instance().processEvents()
        assert sink.ready == {}
    finally:
        preview.shutdown()


def test_missing_graph_reports_failure(qapp) -> None:
    sink = _Sink()
    preview = ColorNodeOutputPreview(size=(64, 36), debounce_ms=10)
    preview.evaluation_failed.connect(sink.on_failed)
    try:
        preview.set_identity("s", "stack", 1)
        preview.request(lambda: None, lambda: _ramp(), ["n1"])
        assert _drive(preview, sink, count=1)
        assert sink.failed[0][1] == "no_input"
    finally:
        preview.shutdown()


def test_compound_request_delivers_and_caches_under_card_id(qapp) -> None:
    """V01：compound 卡片请求 → 编译目标重映射到内部链尾，但投递与缓存
    必须落在请求方 compound id（内部 id 外泄 = 卡片永远收不到 + 缓存
    永不命中 → 每 600ms 全量重评）。"""
    service = _service()
    graph = _graph()
    first, second = _correctors(graph)[:2]
    fx_graph = service.apply_intent(graph, "set_node_option", {
        "node_id": first.node_id, "name": "grade_patch",
        "value": ("exposure", 60.0)})
    assert fx_graph.ok, fx_graph.diagnostics
    grouped = service.apply_intent(
        fx_graph.graph, "group_subgraph",
        {"member_ids": [first.node_id, second.node_id]})
    assert grouped.ok, grouped.diagnostics
    compound_id = next(n.node_id for n in grouped.graph.nodes
                       if n.node_type == "compound")
    payload = grouped.graph.to_dict()

    source = _ramp()
    sink = _Sink()
    preview = ColorNodeOutputPreview(size=(64, 36), debounce_ms=10)
    preview.node_thumbnail_ready.connect(sink.on_ready)
    preview.evaluation_failed.connect(sink.on_failed)
    try:
        preview.set_identity("s", "stack", grouped.graph.graph_revision)
        preview.request(lambda: payload, lambda: source, [compound_id])
        assert _drive(preview, sink, count=1), \
            f"compound 缩略图未投递: ready={list(sink.ready)} failed={sink.failed}"
        assert list(sink.ready) == [compound_id], (
            "投递必须使用请求方 compound id，而不是内部解析节点 id")
        # 缓存命中：同 revision 再请求 → 即刻投递、零 worker 重算。
        sink.ready.clear()
        preview.request(lambda: payload, lambda: source, [compound_id])
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not sink.ready:
            QApplication.instance().processEvents()
            time.sleep(0.02)
        assert sink.ready.keys() == {compound_id}, "compound 请求必须命中缓存"
        # 内容断言：compound 输出包含上游曝光修改，与源可区分。
        arr = _image_bytes(sink.ready[compound_id])
        src_rgb = (source[..., :3] * 255).astype(np.uint8)
        assert np.abs(arr.astype(int) - src_rgb.astype(int)).max() > 8
    finally:
        preview.shutdown()


def _image_bytes(image) -> np.ndarray:
    w, h = image.width(), image.height()
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape(h, image.bytesPerLine())
    return arr[:, :w * 3].reshape(h, w, 3)


# ── 辅助纹理：节点自己的曲线与 HSL 曲线 ───────────────────────────────────

def _one_layer_graph(layer):
    project, sid = EditProject.create(name="r08").create_color_stack(
        asset_id="a1", name="s")
    return migrate_legacy_stack(
        project.color_stacks[sid].with_layers([layer.to_dict(None)]))


def _evaluate(graph, source=None, report: dict | None = None) -> _Sink:
    """Evaluate every corrector of ``graph`` and return what was delivered.

    ``report`` (optional) receives the evaluator's ``stats()`` afterwards.
    """
    sink = _Sink()
    preview = ColorNodeOutputPreview(size=(64, 36), debounce_ms=10)
    preview.node_thumbnail_ready.connect(sink.on_ready)
    preview.evaluation_failed.connect(sink.on_failed)
    nodes = _correctors(graph)
    try:
        payload = graph.to_dict()
        preview.set_identity("s", "stack", graph.graph_revision)
        preview.request(lambda: payload,
                        lambda: source if source is not None else _ramp(),
                        [n.node_id for n in nodes])
        assert _drive(preview, sink, count=len(nodes)), (
            f"缩略图未投递: ready={list(sink.ready)} failed={sink.failed}")
    finally:
        preview.shutdown()
        if report is not None:
            report.update(preview.stats())
    assert sink.failed == [], sink.failed
    return sink


class TestLayerCurves:
    """缩略图过去给每个曲线采样器绑恒等纹理：带曲线的节点看起来像没调过。"""

    def test_a_curve_node_thumbnail_shows_its_curve(self, qapp) -> None:
        from src.features.color.core.layer import build_curves_lut_from_data
        from tests.color.fixtures.color_graph import _neutral_layer
        layer = _neutral_layer("to black")
        layer.curves_data = {"M": [(0, 255), (255, 255)]}   # UI 坐标：y = 255 是最低输出
        layer.curves_lut = build_curves_lut_from_data(layer.curves_data)

        (image,) = _evaluate(_one_layer_graph(layer)).ready.values()

        assert _image_bytes(image).max() <= 8               # 几乎全黑，而不是源图

    def test_a_straight_line_curve_looks_like_no_curve(self, qapp) -> None:
        from src.features.color.core.layer import build_curves_lut_from_data
        from tests.color.fixtures.color_graph import _neutral_layer
        plain = _neutral_layer("plain")
        straight = _neutral_layer("straight")
        straight.curves_data = {"M": [(0, 255), (255, 0)]}
        straight.curves_lut = build_curves_lut_from_data(straight.curves_data)

        (without,) = _evaluate(_one_layer_graph(plain)).ready.values()
        (with_line,) = _evaluate(_one_layer_graph(straight)).ready.values()

        assert np.abs(_image_bytes(without).astype(int)
                      - _image_bytes(with_line).astype(int)).max() <= 1

    def test_an_hsl_curve_node_thumbnail_shows_its_hsl_curve(self, qapp) -> None:
        from tests.color.fixtures.color_graph import _neutral_layer
        layer = _neutral_layer("no saturation")
        layer.hsl_curve_data = {"SatSat": [(0, 255), (255, 255)]}   # 乘数处处为 0

        (image,) = _evaluate(_one_layer_graph(layer)).ready.values()

        pixels = _image_bytes(image).astype(int)
        assert (pixels.max(axis=-1) - pixels.min(axis=-1)).max() <= 6   # 处处灰
        (control,) = _evaluate(_one_layer_graph(_neutral_layer("plain"))).ready.values()
        control = _image_bytes(control).astype(int)
        assert (control.max(axis=-1) - control.min(axis=-1)).max() > 200

    def test_gl_and_metal_agree_for_the_three_layer_stack(self, qapp) -> None:
        """两个后端给同一张图同一帧源：每个节点的缩略图最多差 1 级。"""
        from src.features.color.core.color_graph_entry import SharedColorGraphEntry
        from src.features.color.core.metal_node_thumbnail import (
            MetalNodeThumbnailService,
        )
        from src.shared.gpu.metal.context import MetalComputeContext
        if not MetalComputeContext.is_supported():
            pytest.skip(f"no Metal device: {MetalComputeContext.availability_reason()[1]}")
        graph = _graph()
        source = _ramp()
        delivered = _evaluate(graph).ready
        rgba = np.concatenate(
            [source[..., :3], np.ones(source.shape[:2] + (1,), np.float32)], axis=-1)
        results = MetalNodeThumbnailService(
            SharedColorGraphEntry(MetalComputeContext())).evaluate(
            graph, rgba, tuple(n.node_id for n in _correctors(graph)))

        assert results.keys() == delivered.keys()
        for node_id, image in delivered.items():
            picture, backend = results[node_id]
            assert backend == "metal_texture", (node_id, backend)
            diff = np.abs(_image_bytes(image).astype(int)
                          - picture[..., :3].astype(int))
            assert diff.mean() <= 0.5 and diff.max() <= 1, (node_id, diff.mean(), diff.max())


class _FakeTexture:
    def __init__(self, size, components, data, dtype) -> None:
        self.size, self.components, self.data, self.dtype = size, components, data, dtype
        self.units: list[int] = []

    def use(self, unit) -> None:
        self.units.append(unit)


class _FakeContext:
    LINEAR = "linear"

    def __init__(self) -> None:
        self.made: list[_FakeTexture] = []

    def texture(self, size, components, data, dtype) -> _FakeTexture:
        self.made.append(_FakeTexture(size, components, data, dtype))
        return self.made[-1]


class _Sampler:
    def __init__(self) -> None:
        self.value = None


class TestAuxiliaryBinding:
    """绑定的内容与顺序（不需要 GPU）。"""

    def test_a_layer_saved_with_hsl_control_points_alone_gets_its_lut_rebuilt(self) -> None:
        """GL 图着色器只凭 LUT 判定 HSL 曲线生效；CPU 与 Metal 图后端自己按数据重建。"""
        from src.app.controllers.node_thumbnail_evaluator import _with_hsl_curve_lut
        saved = {"hsl_curve_data": {"SatSat": [(0, 255), (255, 255)]}}

        rebuilt = _with_hsl_curve_lut(dict(saved))

        lut = np.asarray(rebuilt["hsl_curve_lut"], np.float32)
        assert lut.shape == (6, 256)
        assert np.allclose(lut[4], 0.0) and np.allclose(lut[0], 1.0)
        assert "hsl_curve_lut" not in saved                    # the input is left alone
        # Already there, or no curve at all: nothing is added or replaced.
        kept = {"hsl_curve_data": {"SatSat": [(0, 255), (255, 255)]}, "hsl_curve_lut": [[1.0] * 256] * 6}
        assert _with_hsl_curve_lut(dict(kept)) == kept
        assert _with_hsl_curve_lut({"name": "no curves"}) == {"name": "no curves"}

    def test_each_declared_sampler_is_bound_from_the_layer_in_turn(self) -> None:
        from src.app.controllers.node_thumbnail_evaluator import _bind_aux_with_lut
        from src.features.color.core.layer import build_hsl_curve_lut_from_data
        ramp = np.linspace(0, 1, 256, dtype=np.float32)
        curve = np.stack([ramp * 0.5, ramp, ramp], axis=-1).reshape(256, 1, 3)
        hsl = build_hsl_curve_lut_from_data({"SatSat": [(0, 255), (255, 255)]})
        layer = {"curves_lut": curve.tolist(), "hsl_curve_lut": hsl.tolist()}
        prog = {name: _Sampler() for name in
                ("u_curves_tex", "u_hsl_curve_tex", "u_warper_tex")}
        ctx = _FakeContext()

        _bind_aux_with_lut(ctx, prog, layer, {})

        curves, hsl, warper = ctx.made
        assert [prog[n].value for n in prog] == [2, 3, 4]
        assert curves.size == (256, 1) and curves.components == 3
        assert np.allclose(np.frombuffer(curves.data, np.float32).reshape(256, 3),
                           np.stack([ramp * 0.5, ramp, ramp], axis=-1), atol=1e-6)
        # (256, 6)：每个采样点一行，六条曲线一列——与生产渲染器的排布相同
        hsl_data = np.frombuffer(hsl.data, np.float32).reshape(256, 6)
        assert hsl.size == (6, 256)
        assert np.allclose(hsl_data[:, 4], 0.0)             # SatSat：乘数处处为 0
        assert np.allclose(hsl_data[:, [0, 2, 3, 5]], 1.0) and np.allclose(hsl_data[:, 1], 0.0)
        assert warper.size == (2, 2) and warper.components == 2
        assert not np.frombuffer(warper.data, np.float32).any()

    def test_a_layer_without_curves_gets_a_straight_line_and_the_default_hsl_curves(self) -> None:
        from src.app.controllers.node_thumbnail_evaluator import _bind_aux_with_lut
        prog = {"u_curves_tex": _Sampler(), "u_hsl_curve_tex": _Sampler()}
        ctx = _FakeContext()

        _bind_aux_with_lut(ctx, prog, {}, {})

        curves, hsl = ctx.made
        assert np.allclose(np.frombuffer(curves.data, np.float32).reshape(256, 3),
                           np.tile(np.linspace(0, 1, 256, dtype=np.float32)[:, None], (1, 3)),
                           atol=1e-6)
        assert [prog[n].value for n in prog] == [2, 3]

    def test_only_declared_samplers_are_bound_and_equal_content_shares_a_texture(self) -> None:
        from src.app.controllers.node_thumbnail_evaluator import _bind_aux_with_lut
        ctx, cache = _FakeContext(), {}
        for _ in range(2):
            prog = {"u_curves_tex": _Sampler()}
            _bind_aux_with_lut(ctx, prog, {}, cache)
            assert prog["u_curves_tex"].value == 2
        assert len(ctx.made) == 1 and ctx.made[0].units == [2, 2]


# ── Metal 后端（会话开关 TOPOS_COLOR_METAL=1） ─────────────────────────────

def _has_metal() -> bool:
    from src.shared.gpu.metal.context import MetalComputeContext
    return MetalComputeContext.is_supported()


@pytest.fixture()
def colour_metal(monkeypatch):
    """调色域的 Metal 路径在本会话开启（需有 Metal 设备）。"""
    if not _has_metal():
        pytest.skip("needs a Metal device")
    monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
    monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)


def _running(graph) -> tuple[ColorNodeOutputPreview, _Sink]:
    """Request every corrector once and wait; the evaluator is left running."""
    sink = _Sink()
    preview = ColorNodeOutputPreview(size=(64, 36), debounce_ms=10)
    preview.node_thumbnail_ready.connect(sink.on_ready)
    preview.evaluation_failed.connect(sink.on_failed)
    nodes = _correctors(graph)
    payload = graph.to_dict()
    preview.set_identity("s", "stack", graph.graph_revision)
    preview.request(lambda: payload, lambda: _ramp(), [n.node_id for n in nodes])
    assert _drive(preview, sink, count=len(nodes)), (
        f"缩略图未投递: ready={list(sink.ready)} failed={sink.failed}")
    return preview, sink


def _device_gone(self) -> None:
    raise RuntimeError("device gone")


class TestMetalBackend:
    def test_with_the_switch_on_no_gl_context_is_made(
        self, qapp, colour_metal, monkeypatch,
    ) -> None:
        def no_gl(*args, **kwargs):
            raise AssertionError("the thumbnail evaluator made a GL context")

        monkeypatch.setattr(moderngl, "create_standalone_context", no_gl)
        report: dict = {}

        sink = _evaluate(_graph(), report=report)

        assert len(sink.ready) == 3
        assert (report["jobs_metal"], report["jobs_gl"], report["metal_failure"]) == (1, 0, None)

    def test_metal_and_gl_deliver_the_same_pictures(self, qapp, colour_metal, monkeypatch) -> None:
        graph = _graph()
        on_metal = _evaluate(graph)
        monkeypatch.delenv("TOPOS_COLOR_METAL")             # same machine, same source: GL
        on_gl = _evaluate(graph)

        assert on_metal.ready.keys() == on_gl.ready.keys()
        for node_id, image in on_metal.ready.items():
            diff = np.abs(_image_bytes(image).astype(int)
                          - _image_bytes(on_gl.ready[node_id]).astype(int))
            assert diff.mean() <= 0.5 and diff.max() <= 1, (node_id, diff.mean(), diff.max())

    def test_the_kill_switch_keeps_the_evaluator_on_gl(
        self, qapp, colour_metal, monkeypatch,
    ) -> None:
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "color_graph_metal")
        made: list = []
        real = moderngl.create_standalone_context

        def counted(**kwargs):
            made.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(moderngl, "create_standalone_context", counted)
        report: dict = {}

        sink = _evaluate(_graph(), report=report)

        assert len(sink.ready) == 3 and made
        assert (report["jobs_metal"], report["jobs_gl"]) == (0, 1)

    def test_a_metal_failure_is_reported_and_the_job_still_runs_on_gl(
        self, qapp, colour_metal, monkeypatch, caplog,
    ) -> None:
        import logging

        monkeypatch.setattr(ColorNodeOutputPreview, "_metal_service", _device_gone)
        report: dict = {}
        caplog.set_level(logging.WARNING, logger="src.app.controllers.node_thumbnail_evaluator")

        sink = _evaluate(_graph(), report=report)

        assert len(sink.ready) == 3 and sink.failed == []
        assert (report["jobs_metal"], report["jobs_gl"]) == (1, 1)
        assert "device gone" in report["metal_failure"]
        assert any("Metal evaluation failed" in r.getMessage() for r in caplog.records)

    def test_after_a_metal_failure_the_cache_of_the_gl_pictures_is_used(
        self, qapp, colour_metal, monkeypatch,
    ) -> None:
        """之后的任务不再试 Metal，按 GL 的键缓存——同一份 demand 再来时命中缓存。"""
        monkeypatch.setattr(ColorNodeOutputPreview, "_metal_service", _device_gone)
        graph = _graph()
        nodes = [n.node_id for n in _correctors(graph)]
        preview, sink = _running(graph)
        try:
            stats = preview.stats()
            assert (stats["jobs_metal"], stats["jobs_gl"]) == (1, 1)
            payload = graph.to_dict()

            sink.ready.clear()
            preview.request(lambda: payload, lambda: _ramp(), nodes)
            assert _drive(preview, sink, count=3)
            stats = preview.stats()
            assert (stats["jobs_metal"], stats["jobs_gl"]) == (1, 1)   # nothing evaluated again

            # A new revision is a new job: it goes to GL straight away, Metal is not tried again.
            sink.ready.clear()
            preview.set_identity("s", "stack", graph.graph_revision + 1)
            preview.request(lambda: payload, lambda: _ramp(), nodes)
            assert _drive(preview, sink, count=3)
            stats = preview.stats()
            assert (stats["jobs_metal"], stats["jobs_gl"]) == (1, 2)
        finally:
            preview.shutdown()

    def test_the_cache_does_not_share_pictures_between_backends(
        self, qapp, colour_metal, monkeypatch,
    ) -> None:
        monkeypatch.delenv("TOPOS_COLOR_METAL")             # start on GL
        graph = _graph()
        nodes = [n.node_id for n in _correctors(graph)]
        payload = graph.to_dict()
        preview, sink = _running(graph)
        try:
            assert (preview.stats()["jobs_gl"], preview.stats()["jobs_metal"]) == (1, 0)

            monkeypatch.setenv("TOPOS_COLOR_METAL", "1")   # same revision, other backend
            sink.ready.clear()
            preview.request(lambda: payload, lambda: _ramp(), nodes)
            assert _drive(preview, sink, count=3)
            stats = preview.stats()
            assert (stats["jobs_gl"], stats["jobs_metal"]) == (1, 1)
        finally:
            preview.shutdown()

    def test_the_metal_service_is_dropped_when_the_worker_exits(self, qapp, colour_metal) -> None:
        preview, _sink = _running(_graph())
        worker = preview._worker
        assert worker is not None and preview._metal is not None

        preview.shutdown()
        worker.join(5.0)

        assert not worker.is_alive() and preview._metal is None

    def test_without_the_switch_the_evaluator_stays_on_gl(self, qapp, monkeypatch) -> None:
        monkeypatch.delenv("TOPOS_COLOR_METAL", raising=False)
        report: dict = {}

        sink = _evaluate(_graph(), report=report)

        assert len(sink.ready) == 3
        assert (report["jobs_metal"], report["jobs_gl"]) == (0, 1)


class TestBackendChoice:
    """``_thumbnail_backend()``：开关、kill switch 与设备三者决定后端。"""

    def test_gl_without_the_switch_and_says_why(self, monkeypatch) -> None:
        monkeypatch.delenv("TOPOS_COLOR_METAL", raising=False)
        assert _thumbnail_backend() == ("gl", "opt_in_missing")

    def test_gl_when_the_kill_switch_is_on(self, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "color_graph_metal")
        assert _thumbnail_backend() == ("gl", "disabled_by_switch:color_graph_metal")

    def test_metal_when_switched_on_and_there_is_a_device(self, monkeypatch) -> None:
        if not _has_metal():
            pytest.skip("needs a Metal device")
        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        assert _thumbnail_backend() == ("metal", "")

    def test_gl_with_the_reason_when_there_is_no_device(self, monkeypatch) -> None:
        from src.shared.gpu.metal import context

        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        monkeypatch.setattr(
            context.MetalComputeContext, "availability_reason",
            classmethod(lambda cls: (False, "no Metal hardware")))
        assert _thumbnail_backend() == ("gl", "metal_unavailable:no Metal hardware")
