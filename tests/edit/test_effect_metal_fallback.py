"""阶段 6.5：Metal 会话下的效果后端回退与既有导出预检同源。

规则：预览与导出分别做能力预检；只有兼容链路完整才回退；链路不完整的实例必须
被 ``preflight_video_effects_export`` 阻断——导出不能产出省略了效果的文件。
本文件把新的回退规划（``effect_backend_fallback``）与既有预检逐实例对照。
"""

from __future__ import annotations

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import preflight_video_effects_export
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.shared.effects import get_effect_catalog
from src.features.shared.effects.effect_backend_fallback import plan_backend_fallback
from src.features.shared.effects.plugin_identity import build_plugin_identity
from src.shared.media.metadata import Rational


def _sequence(*effects):
    clip = ClipInstance(clip_id="c1", asset_id="a1", track_id="v1",
                        timeline_start=0, timeline_duration=10, source_start=0,
                        source_duration=10, effects=tuple(effects))
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(timeline_id="seq", name="M", fps=Rational(24, 1),
                            width=8, height=8, video_tracks=(track,))


def _identity(plugin_id: str, **changes):
    data = build_plugin_identity(get_effect_catalog().get_spec(plugin_id)).to_dict()
    data.update(changes)
    return data


def _chain():
    return [
        EffectInstance(effect_id="b", effect_type="blur", params={}),
        EffectInstance(effect_id="h", effect_type="plugin:Film_Halation", params={},
                       plugin_identity=_identity("plugin:Film_Halation")),
        EffectInstance(effect_id="w", effect_type="plugin:warm_tint", params={}),
        EffectInstance(effect_id="x", effect_type="plugin:Was_Never_Here", params={},
                       plugin_identity={"plugin_id": "plugin:Was_Never_Here", "version": "1"}),
        EffectInstance(effect_id="g", effect_type="vignette", params={}),
    ]


def test_incompatible_instances_are_exactly_the_preflight_blockers():
    effects = _chain()
    result = preflight_video_effects_export(_sequence(*effects))
    decisions = plan_backend_fallback(effects, stage="export")
    blocked = {blocker.effect_id for blocker in result.blockers}
    incompatible = {d.effect_id for d in decisions if not d.compatible}
    assert incompatible == blocked == {"h", "x", "g"}
    assert result.ok is False
    # blur：原生 Metal 导出；warm_tint：无 Metal 导出链路 → 回退到它声明的 OpenGL 导出
    by_id = {d.effect_id: d for d in decisions}
    assert (by_id["b"].selected_backend, by_id["b"].fell_back) == ("metal", False)
    assert (by_id["w"].selected_backend, by_id["w"].fell_back, by_id["w"].compatible) == ("opengl", True, True)
    assert by_id["w"].detail == "无 metal 导出实现"


def test_blockers_name_the_effect_instance():
    result = preflight_video_effects_export(_sequence(*_chain()))
    codes = {(blocker.effect_id, blocker.code) for blocker in result.blockers}
    assert ("h", "plugin_preview_only") in codes
    assert ("x", "plugin_identity_missing") in codes and ("x", "effect_unknown") in codes
    assert ("g", "effect_unknown") in codes
    assert all(blocker.clip_id == "c1" for blocker in result.blockers)
    assert any("h" in message or "Film_Halation" in message for message in result.blocking)


def test_metal_preview_does_not_imply_metal_export():
    """同一个插件：预览可用 Metal，导出没有 Metal 链路——两份结论必须分别成立。"""
    effect = [("plugin:warm_tint", "w")]
    preview = plan_backend_fallback(effect, stage="preview")[0]
    export = plan_backend_fallback(effect, stage="export")[0]
    assert (preview.selected_backend, preview.fell_back) == ("metal", False)
    assert (export.selected_backend, export.fell_back) == ("opengl", True)
    assert preflight_video_effects_export(_sequence(
        EffectInstance(effect_id="w", effect_type="plugin:warm_tint", params={}))).ok is True


def test_preview_only_plugin_blocks_export_instead_of_being_dropped():
    instance = EffectInstance(effect_id="h", effect_type="plugin:Film_Halation", params={},
                              plugin_identity=_identity("plugin:Film_Halation"))
    result = preflight_video_effects_export(_sequence(instance))
    assert result.ok is False
    assert [blocker.code for blocker in result.blockers if blocker.effect_id == "h"] == ["plugin_preview_only"]
    assert any("静默丢失" in blocker.message for blocker in result.blockers)
    decision = plan_backend_fallback([instance], stage="export")[0]
    assert decision.compatible is False and decision.reason_code == "no_compatible_fallback"


def test_corrupted_plugin_identity_blocks_with_the_instance_id():
    instance = EffectInstance(effect_id="h2", effect_type="plugin:Film_Halation", params={},
                              plugin_identity=_identity("plugin:Film_Halation",
                                                        parameter_schema_hash="0" * 64))
    result = preflight_video_effects_export(_sequence(instance))
    codes = {blocker.code for blocker in result.blockers if blocker.effect_id == "h2"}
    assert "plugin_identity_schema_mismatch" in codes
    assert plan_backend_fallback([instance], stage="export")[0].compatible is False


def test_disabled_preview_only_plugin_is_reported_but_not_blocking():
    instance = EffectInstance(effect_id="h3", effect_type="plugin:Film_Halation", params={},
                              is_enabled=False, plugin_identity=_identity("plugin:Film_Halation"))
    assert preflight_video_effects_export(_sequence(instance)).ok is True
    decision = plan_backend_fallback([instance], stage="export")[0]
    assert (decision.reason_code, decision.effect_id) == ("disabled", "h3")


def test_fully_exportable_chain_passes_both_checks():
    effects = [EffectInstance(effect_id="b", effect_type="blur", params={}),
               EffectInstance(effect_id="s", effect_type="sharpen", params={})]
    assert preflight_video_effects_export(_sequence(*effects)).ok is True
    assert all(d.compatible for d in plan_backend_fallback(effects, stage="export"))
    assert preflight_video_effects_export(None).ok is True


def test_empty_sequence_has_no_decisions():
    assert plan_backend_fallback([], stage="export") == ()
