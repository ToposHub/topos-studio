"""MT-11 AudioRenderPlan + SG-02/SG-04 composition schema."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.audio_plan import (
    AudioRenderPlan, AudioClip, DuckingIntent, audio_plan_for_scene, preflight_audio,
    read_audio_render_plan, write_audio_render_plan_into,
)
from src.features.teaching_animation.core.commands import SceneCommandStack, SetAudioRenderPlanCommand
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.host_adapters import map_scene_audio_to_timeline, SceneReferenceManifest
from src.features.teaching_animation.core.asset_provider import (
    AssetKind, AssetLicenseStatus, AssetProviderRegistry, LocalAssetProvider,
)
from src.features.teaching_animation.core.composition_schema import (
    Mask, MaskKind, MatteMode, SceneInstance, TrackMatte,
    read_instances, read_masks,
)
from src.features.teaching_animation.core.commands import SetCompositionCommand


class TestAudioClip:
    def test_valid_clip(self) -> None:
        clip = AudioClip("a", "narration", start_frame=10, duration_frames=60, gain=0.8,
                         fade_in_frames=6, fade_out_frames=6)
        assert clip.end_frame == 70

    def test_gain_envelope(self) -> None:
        clip = AudioClip("a", "n", start_frame=0, duration_frames=20, gain=1.0,
                         fade_in_frames=10, fade_out_frames=10)
        assert clip.gain_at(0) == 0.0
        assert clip.gain_at(5) == pytest.approx(0.5)
        assert clip.gain_at(10) == pytest.approx(1.0)
        assert clip.gain_at(19) < 1.0   # in fade-out
        assert clip.gain_at(20) == 0.0  # outside

    def test_invalid_fades(self) -> None:
        with pytest.raises(ValueError):
            AudioClip("a", "n", duration_frames=10, fade_in_frames=8, fade_out_frames=8)

    def test_round_trip(self) -> None:
        clip = AudioClip("a", "n", start_frame=4, duration_frames=12, ducking=DuckingIntent.DUCK_OTHERS)
        assert AudioClip.from_dict(clip.to_dict()) == clip


class TestAudioRenderPlan:
    def test_active_clips_at_frame(self) -> None:
        plan = AudioRenderPlan(clips=(
            AudioClip("a", "n1", start_frame=0, duration_frames=10),
            AudioClip("b", "n2", start_frame=5, duration_frames=10),
        ))
        assert {c.clip_id for c in plan.active_clips(7)} == {"a", "b"}
        assert {c.clip_id for c in plan.active_clips(12)} == {"b"}

    def test_duplicate_clip_ids_rejected(self) -> None:
        with pytest.raises(ValueError):
            AudioRenderPlan(clips=(AudioClip("a", "n", duration_frames=2), AudioClip("a", "n", duration_frames=2)))

    def test_round_trip(self) -> None:
        plan = AudioRenderPlan(clips=(AudioClip("a", "n", duration_frames=5),), master_gain=0.9)
        assert AudioRenderPlan.from_dict(plan.to_dict()) == plan

    def test_scene_metadata_and_timeline_mapping_preserve_seek(self) -> None:
        plan = AudioRenderPlan(clips=(AudioClip("a", "n", start_frame=2, duration_frames=10),))
        scene = TeachingSceneDocument.create_default(name="audio", duration_frames=30)
        metadata = write_audio_render_plan_into(scene.metadata, plan)
        assert read_audio_render_plan(metadata) == plan
        updated = scene.__class__.from_dict({**scene.to_dict(), "metadata": metadata})
        mapped = map_scene_audio_to_timeline(updated, timeline_start=100, scene_in_frame=5)
        assert mapped.clips[0].start_frame == 100
        assert mapped.clips[0].source_start_frame == 3

    def test_audio_command_and_manifest_are_reversible_and_auditable(self) -> None:
        scene = TeachingSceneDocument.create_default(name="audio")
        plan = AudioRenderPlan(clips=(AudioClip("a", "n", duration_frames=5),))
        stack = SceneCommandStack()
        updated = stack.execute(SetAudioRenderPlanCommand(plan), scene)
        assert audio_plan_for_scene(updated) == plan
        manifest = SceneReferenceManifest.from_scene(updated)
        assert manifest.audio_asset_ids == ("n",)
        assert manifest.audio_plan_hash
        assert stack.undo(updated) == scene


class TestAudioPreflight:
    def test_missing_asset_reported(self) -> None:
        plan = AudioRenderPlan(clips=(AudioClip("a", "missing", duration_frames=5),))
        diag = preflight_audio(plan, frozenset({"present"}))
        assert "a" in diag.missing_assets

    def test_unsupported_ducking_reported(self) -> None:
        plan = AudioRenderPlan(clips=(AudioClip("a", "n", duration_frames=5, ducking=DuckingIntent.DUCK_OTHERS),))
        diag = preflight_audio(plan, frozenset({"n"}), mixer_supports_ducking=False)
        assert "a" in diag.unsupported_mix

    def test_scene_range_reported_and_all_missing_is_explicit_silence(self) -> None:
        plan = AudioRenderPlan(clips=(AudioClip("a", "n", start_frame=8, duration_frames=5),))
        diag = preflight_audio(plan, frozenset(), scene_duration_frames=10)
        assert diag.out_of_range == ("a",)
        assert diag.silent_fallback is True
        assert diag.blocking is True


class TestSceneInstance:
    def test_reference_not_copy(self) -> None:
        inst = SceneInstance(instance_id="i1", source_scene_id="scene_logo", start_frame=0)
        # stores only the reference id, never scene JSON
        d = inst.to_dict()
        assert "source_scene_id" in d and "elements" not in d
        assert SceneInstance.from_dict(d) == inst

    def test_invalid_duration(self) -> None:
        with pytest.raises(ValueError):
            SceneInstance(instance_id="i", source_scene_id="s", duration_frames=0)


class TestMaskMatte:
    def test_mask_round_trip(self) -> None:
        mask = Mask(mask_id="m1", target="card", kind=MaskKind.LUMA, feather=4.0, opacity=0.8)
        assert Mask.from_dict(mask.to_dict()) == mask

    def test_invalid_opacity(self) -> None:
        with pytest.raises(ValueError):
            Mask(mask_id="m", target="t", opacity=1.5)

    def test_track_matte_round_trip(self) -> None:
        matte = TrackMatte(matte_id="mt1", target="card", source="matte_layer", mode=MatteMode.INVERTED)
        assert TrackMatte.from_dict(matte.to_dict()) == matte

    def test_read_from_metadata(self) -> None:
        metadata = {
            "scene_instances": [{"instance_id": "i", "source_scene_id": "s"}],
            "scene_masks": [{"mask_id": "m", "target": "t"}],
        }
        instances = read_instances(metadata)
        masks = read_masks(metadata)
        assert len(instances) == 1 and instances[0].source_scene_id == "s"
        assert len(masks) == 1 and masks[0].kind == MaskKind.ALPHA

    def test_read_from_empty_metadata(self) -> None:
        assert read_instances({}) == ()
        assert read_masks(None) == ()  # type: ignore[arg-type]

    def test_composition_command_writes_and_undoes_all_references(self) -> None:
        scene = TeachingSceneDocument.create_default(name="composition")
        target = scene.elements[0].element_id
        command = SetCompositionCommand(
            instances=(SceneInstance("pre", "other"),),
            masks=(Mask("mask", target, kind=MaskKind.VECTOR, path_ref="M0 0 L10 0 L10 10 Z"),),
            mattes=(),
        )
        stack = SceneCommandStack()
        updated = stack.execute(command, scene)
        assert read_masks(updated.metadata)[0].path_ref.startswith("M0")
        assert stack.undo(updated) == scene


class TestLocalAssetProvider:
    def test_relative_resolution_hash_and_seek(self, tmp_path) -> None:
        media = tmp_path / "clip.bin"
        media.write_bytes(b"asset-data")
        provider = LocalAssetProvider(tmp_path, ({
            "asset_id": "clip", "kind": "video", "path": "clip.bin",
            "license": "ok", "metadata": {"duration_frames": 20},
        },))
        descriptor = provider.resolve("clip")
        assert descriptor is not None
        assert descriptor.sha256
        assert descriptor.license == AssetLicenseStatus.OK
        assert provider.seek(descriptor, 19).available
        assert not provider.seek(descriptor, 20).available

    def test_escape_and_missing_are_diagnosed(self, tmp_path) -> None:
        provider = LocalAssetProvider(tmp_path, ({
            "asset_id": "escape", "kind": "raster", "path": "../outside.png",
        },))
        registry = AssetProviderRegistry()
        registry.register_provider(provider)
        issues = registry.preflight(("escape",), for_delivery=True)
        # AUD-27303: a MISSING-licensed (unresolvable/absent) file is reported
        # as its own ``missing_file`` diagnostic in preview mode as well;
        # delivery keeps the license_blocks issue on top.
        assert {issue.code for issue in issues} == {"license_blocks", "missing_file"}
        assert registry.preflight(("escape",), for_delivery=False)[0].code == "missing_file"
        assert provider.resolve("escape").license == AssetLicenseStatus.MISSING
