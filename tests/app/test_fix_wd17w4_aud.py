"""WD17-W4 遗留散件修复回归测试（跨 4 域 6 条）。

覆盖 REMEDIATION_PLAN 条目：

* AUD-16808 —— FX-C02 render-operation 计划接入生产消费者：
  ``timeline_preview_renderer._try_gpu_render`` 现按
  ``build_render_operations`` 输出装车（分类单一权威）；
  ``ApplyAdjustment`` 携带 T-P1-22 grade 快照（仅有 grade、无 effects
  的调整剪辑不再被 no-op 跳过规则误伤）。
* AUD-19302 —— View/Timeline 5 个菜单动作可达性：路径自动补建由
  AUD-3600 修复（已有测试锁定），本批补齐缺失的
  ``on_timeline_zoom_in/out/fit`` 回调，使 5 个动作全部可解析、可创建。
* AUD-28101 —— elastic_in_out Penner 中点/端点连续性（修复已随
  cb95e2f0b 落地，此处锁定行为防回退）。
* AUD-31615 —— ``ModelPackManager._verify_dir`` 目录摘要对遍历顺序
  确定（dirs.sort + sorted(files)，修复已随 218bb19d4 落地，此处锁定）。
* AUD-5608 —— ``CompositeEditPlan.from_dict`` 畸形输入归领域异常
  ``CompositeEditError``（修复已随 400510386 落地，此处锁定）。

AUD-6311 的测试在 tests/composite/test_fix_wd17w4_aud.py。
"""

from __future__ import annotations

import hashlib
import inspect
import os
from typing import Optional

import numpy as np
import pytest

from src.shared.actions import ActionRegistry, MenuActionDescriptor

# ═══════════════════════════════════════════════════════════════════════
# AUD-16808：FX-C02 render-operation 计划的生产消费点
# ═══════════════════════════════════════════════════════════════════════


class TestFxC02ProductionConsumer:
    """build_render_operations 必须被生产渲染路径消费（不再是无消费者的
    纯数据契约）。"""

    def test_preview_renderer_imports_plan_builder(self) -> None:
        from src.features.edit.core import effect_render_operations as ero
        from src.features.edit.core import timeline_preview_renderer as tpr

        assert tpr.build_render_operations is ero.build_render_operations, (
            "timeline_preview_renderer 必须导入并使用 effect_render_operations"
            ".build_render_operations（AUD-16808 生产消费点）")
        assert tpr.ApplyAdjustment is ero.ApplyAdjustment

    def test_try_gpu_render_iterates_plan_ops(self) -> None:
        from src.features.edit.core import timeline_preview_renderer as tpr

        source = inspect.getsource(tpr.PreviewRenderWorker._try_gpu_render)
        assert "build_render_operations(" in source
        assert "for op in render_ops" in source, (
            "_try_gpu_render 的层装车循环必须按计划输出遍历")
        assert "isinstance(op, ApplyAdjustment)" in source
        # 分类必须来自计划，而非独立重复判定
        assert "asset_type == 'adjustment'" not in source
        assert "asset_type == 'solid'" not in source


class _Resolver:
    """asset_id → media_type，模拟预览渲染器注入的 AssetTypeResolver。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def get_asset_type(self, asset_id: str) -> Optional[str]:
        return self._mapping.get(asset_id)


def _build_two_track_plan():
    """solid 底层 + adjustment 顶层（无 effects）的双轨夹具。"""
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    project = EditProject.create("W4")
    project, solid_id = project.create_solid_color_asset("Lower", (1.0, 0.0, 0.0))
    project, adj_id = project.create_adjustment_clip("Adj")

    solid = ClipInstance(
        clip_id="c-lower", asset_id=solid_id, track_id="t-lower",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
    )
    adj = ClipInstance(
        clip_id="c-adj", asset_id=adj_id, track_id="t-adj",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
    )
    seq = TimelineSequence(
        timeline_id="seq", name="W4", fps=Rational(24, 1), width=32, height=32,
        video_tracks=(
            EditTrack(track_id="t-lower", name="V1", track_type="video", index=0, clips=(solid,)),
            EditTrack(track_id="t-adj", name="V2", track_type="video", index=1, clips=(adj,)),
        ),
    )
    frame_plan = TimelineRenderPlan(seq).get_frame_plan(0)
    resolver = _Resolver({solid_id: "solid", adj_id: "adjustment"})
    return frame_plan, resolver


class TestPlanGradeSnapshot:
    """grade_snapshot 扩展：grade-only 调整剪辑不是 no-op（T-P1-22 契约）。"""

    def test_grade_only_adjustment_not_skipped(self) -> None:
        from src.features.edit.core.effect_render_operations import (
            ApplyAdjustment,
            build_render_operations,
        )

        frame_plan, resolver = _build_two_track_plan()
        grade = {"exposure": 0.5}
        ops = build_render_operations(
            frame_plan, resolver,
            effects_snapshot=lambda layer: (),
            grade_snapshot=lambda layer: grade,
        )
        adjustments = [op for op in ops if isinstance(op, ApplyAdjustment)]
        assert len(adjustments) == 1, (
            "仅有 ColorStack grade、无 effects 的调整剪辑必须保留"
            "（GPU 侧会原位 grade），no-op 跳过规则不得吞掉它")
        assert adjustments[0].grade is grade
        assert adjustments[0].effects == ()

    def test_true_no_op_adjustment_still_skipped(self) -> None:
        from src.features.edit.core.effect_render_operations import (
            ApplyAdjustment,
            build_render_operations,
        )

        frame_plan, resolver = _build_two_track_plan()
        ops = build_render_operations(
            frame_plan, resolver,
            effects_snapshot=lambda layer: (),
            grade_snapshot=lambda layer: None,
        )
        # 无 effects、无 grade、恒等 Transform、opacity=1 → GPU 侧像素级
        # no-op，计划跳过（与旧行为一致的省略）。
        assert not any(isinstance(op, ApplyAdjustment) for op in ops)

    def test_solid_classification_via_plan(self) -> None:
        from src.features.edit.core.effect_render_operations import (
            RenderClip,
            build_render_operations,
        )

        frame_plan, resolver = _build_two_track_plan()
        ops = build_render_operations(
            frame_plan, resolver,
            effects_snapshot=lambda layer: (),
            grade_snapshot=lambda layer: None,
        )
        clips = [op for op in ops if isinstance(op, RenderClip)]
        assert len(clips) == 1 and clips[0].is_solid is True


# ═══════════════════════════════════════════════════════════════════════
# AUD-19302：View/Timeline 5 个菜单动作可达
# ═══════════════════════════════════════════════════════════════════════

# module.py 注册的五个 View/Timeline 动作（action_id, callback_name）。
_W4_VIEW_TIMELINE_ACTIONS = (
    ("edit.timeline.zoom_in", "on_timeline_zoom_in"),
    ("edit.timeline.zoom_out", "on_timeline_zoom_out"),
    ("edit.timeline.fit", "on_timeline_fit"),
    ("edit.playback.hud", "on_toggle_playback_hud"),
    ("edit.playback.export_perf_report", "on_export_performance_report"),
)


class TestViewTimelineActionsReachable:
    """菜单路径自动补建（AUD-3600）+ 回调齐备（本批补 3 个 zoom/fit）
    → 5 个动作全部可创建。"""

    def test_module_registers_all_five_view_timeline_actions(self) -> None:
        from pathlib import Path

        module_path = (
            Path(__file__).resolve().parents[2]
            / "src" / "features" / "edit" / "module.py"
        )
        with open(module_path, encoding="utf-8") as fh:
            source = fh.read()
        for action_id, callback in _W4_VIEW_TIMELINE_ACTIONS:
            assert f'action_id="{action_id}"' in source, action_id
            assert f'callback_name="{callback}"' in source, action_id

    def test_all_five_callbacks_resolvable_on_controller(self) -> None:
        from src.app.controllers.edit_controller import EditControllerMixin

        for _, callback in _W4_VIEW_TIMELINE_ACTIONS:
            assert callable(getattr(EditControllerMixin, callback, None)), (
                f"{callback} 必须存在于 EditControllerMixin——MenuBuilder 的"
                " callback_resolver 用 getattr 解析，缺失则动作被整条跳过")

    def test_menu_builder_creates_all_five_under_view_timeline(self, qapp) -> None:
        from PyQt6.QtWidgets import QMenuBar
        from src.app.menu.menu_builder import MenuBuilder

        registry = ActionRegistry()
        for action_id, callback in _W4_VIEW_TIMELINE_ACTIONS:
            registry.register(MenuActionDescriptor(
                action_id=action_id,
                menu_path="View/Timeline",
                label=f"Label {action_id}",
                callback_name=callback,
                feature_id="edit",
            ))

        from src.app.controllers.edit_controller import EditControllerMixin

        menu_bar = QMenuBar()
        try:
            builder = MenuBuilder(
                menu_bar=menu_bar,
                action_registry=registry,
                # 生产等价解析：getattr(self, callback_name)
                callback_resolver=lambda name: getattr(EditControllerMixin, name, None),
            )
            actions = builder.build()
            assert set(actions) == {aid for aid, _ in _W4_VIEW_TIMELINE_ACTIONS}, (
                f"5 个动作必须全部创建，缺失: "
                f"{ {aid for aid, _ in _W4_VIEW_TIMELINE_ACTIONS} - set(actions) }")
            timeline = builder._menus.get("View/Timeline")
            assert timeline is not None, "View/Timeline 子菜单必须存在"
            mounted = {a.text() for a in timeline.actions()}
            assert any("zoom_in" in t for t in mounted), mounted
            assert any("export_perf_report" in t for t in mounted), mounted
        finally:
            menu_bar.deleteLater()


# ═══════════════════════════════════════════════════════════════════════
# AUD-28101：elastic_in_out Penner 连续性
# ═══════════════════════════════════════════════════════════════════════


class TestElasticInOutContinuity:
    """审计缺陷：旧实现两支包络均衰减——中点 0.0002→0.5002 跳变 0.5、
    终点前 ≈0.75 跳到 1。修复（Penner easeInOutElastic）必须端点精确、
    中点连续、终点收敛。"""

    def test_endpoint_values_exact(self) -> None:
        from src.features.teaching_animation.motion.easing import elastic_in_out

        assert elastic_in_out(0.0) == 0.0
        assert elastic_in_out(1.0) == 1.0

    def test_midpoint_value_and_continuity(self) -> None:
        from src.features.teaching_animation.motion.easing import elastic_in_out

        # Penner 公式在中点精确为 0.5（sin(-π/2) = -1）
        assert elastic_in_out(0.5) == pytest.approx(0.5, abs=1e-12)
        eps = 1e-4
        left = elastic_in_out(0.5 - eps)
        right = elastic_in_out(0.5 + eps)
        # 旧缺陷中点单侧跳变 0.5；连续曲线两侧差应为曲线斜率量级（≪0.5）
        assert abs(left - right) < 0.05, (left, right)
        assert abs(left - 0.5) < 0.05
        assert abs(right - 0.5) < 0.05

    def test_converges_to_one_before_endpoint(self) -> None:
        from src.features.teaching_animation.motion.easing import elastic_in_out

        # 旧缺陷 t→1⁻ ≈0.75 后跳到 1；修复后指数包络收敛到 1
        for t in (0.9, 0.99, 0.999, 0.9999):
            assert elastic_in_out(t) == pytest.approx(1.0, abs=5e-3), t

    def test_symmetric_overshoot_bounded(self) -> None:
        from src.features.teaching_animation.motion.easing import elastic_in_out

        samples = np.linspace(0.0, 1.0, 2001)
        values = [elastic_in_out(float(t)) for t in samples]
        # easeInOutElastic 允许小幅越过 [0,1]（弹性过冲），但必须有界
        assert min(values) >= -0.3 and max(values) <= 1.3
        # 关于 (0.5, 0.5) 中心对称
        for t in (0.1, 0.25, 0.4):
            assert elastic_in_out(t) == pytest.approx(
                1.0 - elastic_in_out(1.0 - t), abs=1e-9), t

    def test_penner_reference_values(self) -> None:
        """与标准 Penner easeInOutElastic 公式逐点一致。"""
        import math

        from src.features.teaching_animation.motion.easing import elastic_in_out

        def penner(t: float) -> float:
            if t <= 0.0:
                return 0.0
            if t >= 1.0:
                return 1.0
            if t < 0.5:
                return -0.5 * (2.0 ** (10.0 * (2.0 * t - 1.0))) * math.sin(
                    (2.0 * t - 1.1125) * (2.0 * math.pi / 0.45))
            return 0.5 * (2.0 ** (-10.0 * (2.0 * t - 1.0))) * math.sin(
                (2.0 * t - 1.1125) * (2.0 * math.pi / 0.45)) + 1.0

        for t in (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95):
            assert elastic_in_out(t) == pytest.approx(penner(t), abs=1e-12), t


# ═══════════════════════════════════════════════════════════════════════
# AUD-31615：_verify_dir 摘要遍历顺序确定
# ═══════════════════════════════════════════════════════════════════════


def _make_pack_tree(root, creation_order) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "subA").mkdir(exist_ok=True)
    (root / "subB").mkdir(exist_ok=True)
    payloads = {
        "model.bin": b"\x01" * 32,
        "subA/part-0.bin": b"\x02" * 16,
        "subB/part-9.bin": b"\x03" * 8,
        "subA/part-1.bin": b"\x04" * 4,
    }
    for rel in creation_order:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payloads[rel])


class TestVerifyDirDeterministicDigest:
    """同一目录树在不同枚举/创建顺序下必须得到同一 digest。"""

    def test_source_sorts_traversal(self) -> None:
        from src.shared.ai_models.generation import model_pack_manager as mpm

        source = inspect.getsource(mpm.ModelPackManager._verify_dir)
        assert "dirs.sort()" in source, "os.walk 的目录枚举必须排序（AUD-31615）"
        assert "sorted(files)" in source, "文件枚举必须排序（AUD-31615）"

    def test_digest_independent_of_enumeration_order(
            self, tmp_path, monkeypatch) -> None:
        from src.shared.ai_models.generation import model_pack_manager as mpm

        tree = tmp_path / "pack"
        _make_pack_tree(tree, [
            "subB/part-9.bin", "model.bin", "subA/part-1.bin", "subA/part-0.bin"])

        hexdigests: list[str] = []
        real_sha256 = hashlib.sha256

        def recording_sha256(*args, **kwargs):
            inner = real_sha256(*args, **kwargs)

            class _Wrap:
                def update(self, data: bytes) -> None:
                    inner.update(data)

                def hexdigest(self) -> str:
                    value = inner.hexdigest()
                    hexdigests.append(value)
                    return value

            return _Wrap()

        never_matches = "0" * 64  # 合法 64-hex，强制全量遍历并算出 digest
        monkeypatch.setattr(hashlib, "sha256", recording_sha256)
        assert mpm.ModelPackManager._verify_dir(str(tree), never_matches) is False
        assert hexdigests, "必须实际计算过目录摘要"
        digest_normal = hexdigests[-1]

        # 反转 os.walk 的目录/文件枚举顺序：未排序的实现会得到不同 digest
        real_walk = os.walk

        def reversed_walk(top, *args, **kwargs):
            for root, dirs, files in real_walk(top, *args, **kwargs):
                yield root, list(reversed(dirs)), list(reversed(files))

        monkeypatch.setattr(mpm.os, "walk", reversed_walk)
        hexdigests.clear()
        assert mpm.ModelPackManager._verify_dir(str(tree), never_matches) is False
        assert hexdigests[-1] == digest_normal, (
            "同一目录树在枚举顺序颠倒后 digest 必须一致"
            "（否则已装 pack 会被误判校验失败，AUD-31615）")

    def test_digest_independent_of_creation_order(self, tmp_path, monkeypatch) -> None:
        """两个内容相同、创建顺序不同的目录树 digest 相同（真实 FS 行为）。"""
        from src.shared.ai_models.generation import model_pack_manager as mpm

        tree_a = tmp_path / "pack-a"
        tree_b = tmp_path / "pack-b"
        _make_pack_tree(tree_a, ["model.bin", "subA/part-0.bin", "subA/part-1.bin", "subB/part-9.bin"])
        _make_pack_tree(tree_b, ["subB/part-9.bin", "subA/part-1.bin", "subA/part-0.bin", "model.bin"])

        hexdigests: list[str] = []
        real_sha256 = hashlib.sha256

        def recording_sha256(*args, **kwargs):
            inner = real_sha256(*args, **kwargs)

            class _Wrap:
                def update(self, data: bytes) -> None:
                    inner.update(data)

                def hexdigest(self) -> str:
                    value = inner.hexdigest()
                    hexdigests.append(value)
                    return value

            return _Wrap()

        never_matches = "0" * 64
        monkeypatch.setattr(hashlib, "sha256", recording_sha256)
        mpm.ModelPackManager._verify_dir(str(tree_a), never_matches)
        digest_a = hexdigests[-1]
        hexdigests.clear()
        mpm.ModelPackManager._verify_dir(str(tree_b), never_matches)
        assert hexdigests[-1] == digest_a


# ═══════════════════════════════════════════════════════════════════════
# AUD-5608：CompositeEditPlan.from_dict 畸形输入归领域异常
# ═══════════════════════════════════════════════════════════════════════


class TestCompositeEditPlanFromDictBoundary:
    """from_dict 是对脚本/AI 暴露的 JSON-safe 边界：畸形输入必须抛
    CompositeEditError（可显示给调用方），而非让 TypeError/ValueError
    穿透到宿主。"""

    def test_non_mapping_input_raises_domain_error(self) -> None:
        from src.features.composite.edit_service import (
            CompositeEditError,
            CompositeEditPlan,
        )

        for bad in (None, "plan", 42, [1, 2], ("a",), {1, 2}):
            with pytest.raises(CompositeEditError):
                CompositeEditPlan.from_dict(bad)

    def test_non_array_operations_raises_domain_error(self) -> None:
        from src.features.composite.edit_service import (
            CompositeEditError,
            CompositeEditPlan,
        )

        for bad_ops in ("ops", 7, {"add": 1}, 3.5):
            with pytest.raises(CompositeEditError):
                CompositeEditPlan.from_dict({"operations": bad_ops})

    def test_non_mapping_operation_element_raises_domain_error(self) -> None:
        from src.features.composite.edit_service import (
            CompositeEditError,
            CompositeEditPlan,
        )

        with pytest.raises(CompositeEditError, match="#1"):
            CompositeEditPlan.from_dict(
                {"operations": [{"op": "set_parameter"}, 42]})

    def test_bad_base_generation_raises_domain_error(self) -> None:
        from src.features.composite.edit_service import (
            CompositeEditError,
            CompositeEditPlan,
        )

        for bad_gen in ("abc", "twelve", [1], {"g": 1}, "1.5.2"):
            with pytest.raises(CompositeEditError):
                CompositeEditPlan.from_dict(
                    {"operations": [], "base_generation": bad_gen})

    def test_domain_error_is_value_error_subclass(self) -> None:
        # 领域异常须可被宿主以 ValueError 家族统一捕获
        from src.features.composite.edit_service import CompositeEditError

        assert issubclass(CompositeEditError, ValueError)

    def test_valid_minimal_dict_round_trips(self) -> None:
        from src.features.composite.edit_service import CompositeEditPlan

        plan = CompositeEditPlan.from_dict({
            "operations": ({"op": "add_keyframe", "node": "n1"},),
            "base_generation": "7",
            "plan_id": "p1",
        })
        assert plan.base_generation == 7
        assert plan.operations == ({"op": "add_keyframe", "node": "n1"},)
        data = plan.to_dict()
        assert CompositeEditPlan.from_dict(data).content_hash() == plan.content_hash()
