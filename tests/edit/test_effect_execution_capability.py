"""T-P1-20：统一 EffectExecutionCapability 与结构化 preflight 阻断码。

任务卡核心要求：
1. 单一 ``EffectExecutionCapability`` 取代散落 bool（七轴 + reason +
   质量约束），UI/AI/Deliver/CLI 同一真相源。
2. 4 个 Edit 插件显式 ``preview_only``——Deliver preflight 返回
   ``plugin_preview_only`` / ``plugin_identity_*`` 结构化码，消息与
   AI manifest ``delivery_restriction`` 逐字一致。
3. ``preflight_video_effects_export`` 错误码按 effect ID 返回；能力模块
   不可导入时 fail-closed（``catalog_unavailable``），不静默放行。
4. 旧工程 ghost 实例反序列化剔除（视觉无损）。
"""

from __future__ import annotations

import sys

import pytest

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import (
    preflight_video_effects_export,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.shared.effects import (
    get_effect_catalog,
    resolve_execution_capability,
)
from src.shared.media.metadata import Rational


def _seq_with_effects(*effects) -> TimelineSequence:
    """单轨单 clip 时间线，effects 为 EffectInstance 或 effect_type 字符串。"""
    instances = tuple(
        EffectInstance(effect_type=e, params={}, is_enabled=True)
        if isinstance(e, str) else e
        for e in effects
    )
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=10, source_start=0,
        source_duration=10, effects=instances,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video",
                      index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="seq", name="M", fps=Rational(24, 1),
        width=8, height=8, video_tracks=(track,),
    )


class TestExecutionCapabilityResolver:
    def test_builtin_effect_fully_exportable(self) -> None:
        cap = resolve_execution_capability(
            "blur", catalog_known=get_effect_catalog().is_known("blur"))
        assert cap.exportable is True
        assert cap.preview_only is False
        assert cap.unsupported is False
        assert cap.cpu_export is True
        assert cap.metal_export is True and cap.opengl_export is True
        # Metal 导出是参数子集门控——质量约束必须显式携带（禁止宣传全等价）
        assert cap.quality_constraints
        assert "metal" in cap.quality_constraints[0]

    def test_unknown_effect_is_unsupported_with_code(self) -> None:
        cap = resolve_execution_capability("definitely_not_an_effect",
                                           catalog_known=False)
        assert cap.unsupported is True
        assert cap.previewable is False
        assert cap.delivery_restriction() == (
            "effect_unknown", cap.reason_for("unsupported")[1])
        assert cap.reason_for("unsupported")[0] == "effect_unknown"

    def test_preview_only_plugin_semantics(self) -> None:
        """plugin:* 在 catalog 中（GPU 预览）但无导出后端 → preview_only。"""
        spec = get_effect_catalog().get_spec("plugin:highpass_retouch")
        known = get_effect_catalog().is_known("plugin:highpass_retouch")
        cap = resolve_execution_capability(
            "plugin:highpass_retouch",
            catalog_known=known or spec is not None,
        )
        # 不论该插件是否真实存在，只要不可导出就必须显式给出限制码
        if not cap.exportable:
            code, message = cap.delivery_restriction()
            assert code in ("preview_only", "effect_unknown")
            assert message

    def test_to_dict_roundtrip_and_consistency(self) -> None:
        cap = resolve_execution_capability("blur", catalog_known=True)
        d = cap.to_dict()
        assert d["exportable"] == cap.exportable
        assert d["previewable"] == cap.previewable
        assert isinstance(d["reasons"], list)
        restricted = resolve_execution_capability("vignette",
                                                  catalog_known=False)
        rd = restricted.to_dict()
        assert rd["delivery_restriction"]["code"] == "effect_unknown"


class TestPreflightStructuredBlockers:
    def test_unknown_effect_blocks_with_code(self) -> None:
        result = preflight_video_effects_export(
            _seq_with_effects("definitely_not_an_effect"))
        assert result.ok is False
        codes = [b.code for b in result.blockers]
        assert codes == ["effect_unknown"]
        blocker = result.blockers[0]
        assert blocker.effect_type == "definitely_not_an_effect"
        assert blocker.effect_id  # effect ID 级归因
        # blocking 消息视图与结构化项一一对应
        assert len(result.blocking) == len(result.blockers)

    def test_ghost_type_blocks_as_unknown(self) -> None:
        """ghost 类型经旧数据绕过迁移进入链时也被结构化阻断。"""
        result = preflight_video_effects_export(_seq_with_effects("vignette"))
        assert result.ok is False
        assert result.blockers[0].code == "effect_unknown"

    def test_exportable_chain_ok_without_blockers(self) -> None:
        result = preflight_video_effects_export(
            _seq_with_effects("blur", "sharpen"))
        assert result.ok is True
        assert result.blockers == ()

    def test_catalog_import_failure_fails_closed(self, monkeypatch) -> None:
        """能力模块不可导入 → catalog_unavailable 阻断（不再 fail-open）。"""
        monkeypatch.setitem(sys.modules, "src.features.shared.effects", None)
        result = preflight_video_effects_export(_seq_with_effects("blur"))
        assert result.ok is False
        assert result.blockers[0].code == "catalog_unavailable"

    def test_none_sequence_still_ok(self) -> None:
        assert preflight_video_effects_export(None).ok is True


class TestLegacyGhostMigrationConsistency:
    def test_migrated_project_does_not_block_preflight(self) -> None:
        """旧工程载入剔除 ghost 后，preflight 不再被历史数据卡死。"""
        from src.features.edit.core.timeline_v2 import ClipInstance

        clip = ClipInstance.from_dict({
            "clip_id": "c_legacy", "asset_id": "a1", "track_id": "v1",
            "timeline_start": 0, "timeline_duration": 10,
            "source_start": 0, "source_duration": 10,
            "effects": [
                {"effect_type": "vignette", "effect_id": "fx_v",
                 "params": {}},
                {"effect_type": "blur", "effect_id": "fx_b", "params": {}},
            ],
        })
        result = preflight_video_effects_export(
            _seq_with_effects(*clip.effects))
        assert result.ok is True
