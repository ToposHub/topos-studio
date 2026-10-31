"""Wave6-G7 regression tests (batch C + round2).

Covers:

- AUD-18206: v4→v5 migration must not iterate ``project["timelines"]``
  (TimelineSequenceRef dict) as a sequence list.  Old JSON projects with
  timelines must load without AttributeError, and the animated_params
  rename/scale must hit the real clip containers (wrapper-level
  ``timeline_sequences``).
- AUD-36920: composite save must rebuild EditProject by whole-object
  copy (``replace``), carrying smart_bins / render_cache_manifests /
  render_cache_settings / proxy_policy / revision through a save.
- AUD-4701: v6→v7 Scene3D split must only delete the keys it actually
  migrates to child nodes; scene environment keys survive to be lifted
  by v8→v9 into ``viewer_state["default_3d_scene"]``.

Run: .venv/bin/python -m pytest tests/edit/test_fix_w6g7_aud6.py
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.project import EditProject, SmartBin
from src.features.edit.core.serialization import EditProjectSerializer


# ================================================================
# AUD-18206: v4 → v5 migration structure
# ================================================================

def _v4_wrapper() -> dict:
    """A minimal v4 wrapper shaped exactly like the real on-disk layout.

    ``project.timelines`` holds TimelineSequenceRef dicts (metadata only);
    the clips live in the wrapper-level ``timeline_sequences`` map.
    """
    return {
        "version": 4,
        "project_type": "edit_project",
        "project": {
            "project_id": "p1",
            "name": "v4 project",
            "assets": {},
            "bins": {},
            # Ref dict — iterating this as a list is the AUD-18206 crash.
            "timelines": {
                "tl1": {
                    "timeline_id": "tl1",
                    "name": "Main",
                    "fps": 24.0,
                    "width": 1920,
                    "height": 1080,
                    "sample_rate": 48000,
                    "format_settings": {},
                    "created_at": 1.0,
                }
            },
            "root_bin_id": "",
            "created_at": 1.0,
            "modified_at": 1.0,
        },
        "timeline_sequences": {
            "tl1": {
                "timeline_id": "tl1",
                "name": "Main",
                "fps": "24/1",
                "width": 1920,
                "height": 1080,
                "sample_rate": 48000,
                "format_settings": {},
                "video_tracks": [
                    {
                        "track_id": "t1",
                        "index": 0,
                        "track_type": "video",
                        "clips": [
                            {
                                "clip_id": "c1",
                                "asset_id": "a1",
                                "track_id": "t1",
                                "timeline_start": 0,
                                "timeline_duration": 24,
                                "source_start": 0,
                                "source_duration": 24,
                                "animated_params": {
                                    "transform.position_x": {
                                        "base_value": 0.5,
                                        "keyframes": {
                                            "param_name": "transform.position_x",
                                            "keyframes": [
                                                {"frame": 0, "value": 0.5,
                                                 "interpolation": "linear"},
                                                {"frame": 12, "value": -0.25,
                                                 "interpolation": "linear"},
                                            ],
                                        },
                                        "min_value": -1920.0,
                                        "max_value": 1920.0,
                                    },
                                },
                            }
                        ],
                        "subtitle_clips": [],
                    }
                ],
                "audio_tracks": [],
                "subtitle_tracks": [],
            }
        },
    }


class TestV4ToV5Migration:
    """AUD-18206: loading a v4 project with timelines must not crash."""

    def test_from_json_loads_v4_project_with_timelines(self) -> None:
        project = EditProjectSerializer.from_json(json.dumps(_v4_wrapper()))
        assert project is not None
        assert project.project_id == "p1"
        # The ref map itself parses into refs.
        assert "tl1" in project.timelines

    def test_from_json_with_ui_state_loads_and_migrates_clips(self) -> None:
        wrapper = _v4_wrapper()
        wrapper["ui_state"] = {"active_timeline_id": "tl1", "playhead_frame": 3}
        project, ui_state, sequences = EditProjectSerializer.from_json_with_ui_state(
            json.dumps(wrapper)
        )
        assert ui_state is not None and ui_state.playhead_frame == 3
        assert sequences is not None and "tl1" in sequences
        seq = sequences["tl1"]
        clip = seq.video_tracks[0].clips[0]
        # animated_params key renamed to the pixel model + values scaled
        # by the reference resolution (0.5 → 960.0, -0.25 → -480.0).
        assert "transform.position_x" not in clip.animated_params
        anim = clip.animated_params.get("transform.translate_x")
        assert anim is not None
        values = [kf.value for kf in anim.keyframes.keyframes]
        assert values == [960.0, -480.0]

    def test_migrate_data_is_idempotent(self) -> None:
        wrapper = _v4_wrapper()
        once = EditProjectSerializer._migrate_data(wrapper, 4)
        twice = EditProjectSerializer._migrate_data(once, 4)
        clip = twice["timeline_sequences"]["tl1"]["video_tracks"][0]["clips"][0]
        anim = clip["animated_params"]
        assert "transform.position_x" not in anim
        assert anim["transform.translate_x"]["keyframes"]["keyframes"][0]["value"] == 960.0

    def test_project_timelines_ref_dict_never_iterated_as_list(self) -> None:
        """A ref dict under project.timelines stays untouched (no crash,
        no data mutation) — refs never carry clips."""
        wrapper = _v4_wrapper()
        ref_before = json.dumps(wrapper["project"]["timelines"], sort_keys=True)
        EditProjectSerializer._migrate_data(wrapper, 4)
        assert json.dumps(wrapper["project"]["timelines"], sort_keys=True) == ref_before


# ================================================================
# AUD-36920: composite save preserves all EditProject fields
# ================================================================

class TestCompositeSaveFieldPreservation:

    def _make_project(self) -> EditProject:
        base = EditProject.create("P")
        return EditProject(
            project_id=base.project_id,
            name=base.name,
            assets=base.assets,
            bins=base.bins,
            timelines=base.timelines,
            compositions={},
            color_stacks=base.color_stacks,
            comp_clips={},
            smart_bins={
                "sb1": SmartBin(
                    bin_id="sb1",
                    name="My Search",
                    filter_type="custom",
                    filter_params={"rating": 4},
                )
            },
            render_cache_manifests={"cache-1": {"cache_id": "cache-1", "clips": []}},
            render_cache_settings={"format": "exr", "enabled": True},
            proxy_policy={"proxy_policy": "half"},
            root_bin_id=base.root_bin_id,
            active_timeline_id=None,
            active_comp_clip_id=None,
            created_at=base.created_at,
            modified_at=base.modified_at,
            revision=17,
        )

    def test_composite_save_preserves_non_whitelist_fields(self) -> None:
        from src.shared.project.save_router import SaveRouter

        project = self._make_project()
        router = SaveRouter()
        updated_holder: list[EditProject] = []
        router.set_callback("on_edit_project_updated", updated_holder.append)
        router.set_callback("get_ui_state", lambda: None)
        router.set_callback("get_current_timeline_sequences", lambda: None)
        router.set_callback("get_edit_project_io", lambda: None)

        router._save_composite_to_edit_project(
            project_path="/tmp/w6g7-aud36920.topos",
            edit_project=project,
            composite_graph_data={
                "composition_id": "g2", "name": "C", "nodes": [], "connections": [],
            },
            composite_viewer_state=None,
            composite_id="g2",
        )

        assert len(updated_holder) == 1
        updated = updated_holder[0]
        # The rebuilt project must carry every field, not just the
        # historical whitelist (AUD-36920).
        assert updated.smart_bins == project.smart_bins
        assert updated.render_cache_manifests == project.render_cache_manifests
        assert updated.render_cache_settings == project.render_cache_settings
        assert updated.proxy_policy == project.proxy_policy
        assert updated.revision == project.revision
        # The composite itself lands.
        assert "g2" in updated.compositions


# ================================================================
# AUD-4701: v6→v7 keeps scene environment params for v8→v9
# ================================================================

class TestScene3DEnvParamsSurviveMigrationChain:

    def _v6_graph(self) -> dict:
        return {
            "composite_schema_version": 6,
            "nodes": [
                {
                    "id": "s1",
                    "type": "scene3d",
                    "position": [0, 0],
                    "parameters": {
                        "width": 1920, "height": 1080, "format_name": "S",
                        # environment keys — must survive v6→v7 (AUD-4701)
                        "bg_r": 0.1, "bg_g": 0.2, "bg_b": 0.3, "bg_opaque": True,
                        "show_grid": True, "show_axes": False,
                        "grid_cell_size": 12.0, "msaa": 8,
                        "active_camera_id": "cam_0", "hdri_preset": "studio.hdr",
                        "output_show_grid": True, "output_bg_opaque": False,
                        "light_color_r": 1.0, "light_color_g": 0.9,
                        "light_color_b": 0.8,
                        # split keys — migrate to child nodes in v6→v7
                        "cam_fov": 50.0, "cam_dist": 5.0,
                        "light_intensity": 2.0,
                        "primitive": "cube", "mesh_scale": 1.0,
                    },
                    "disabled": False,
                }
            ],
            "connections": [],
        }

    def test_v6_to_v7_keeps_environment_params(self) -> None:
        from src.features.composite.core.graph_migration import migrate_v6_to_v7

        result = migrate_v6_to_v7(self._v6_graph())
        scene3d = next(n for n in result["nodes"] if n["type"] == "scene3d")
        params = scene3d["parameters"]
        # Environment keys survive v6→v7 for the later v8→v9 lift.
        for key in ("bg_r", "bg_g", "bg_b", "bg_opaque", "show_grid", "show_axes",
                    "grid_cell_size", "msaa", "active_camera_id", "hdri_preset",
                    "output_show_grid", "output_bg_opaque",
                    "light_color_r", "light_color_g", "light_color_b"):
            assert key in params, f"{key} dropped by v6→v7 (AUD-4701)"
        # Split keys migrate to the spawned child nodes.
        for key in ("cam_fov", "cam_dist", "light_intensity", "primitive", "mesh_scale"):
            assert key not in params, f"{key} should have moved to a child node"
        camera = next(n for n in result["nodes"] if n["type"] == "camera3d")
        assert camera["parameters"]["cam_fov"] == 50.0

    def test_full_chain_lifts_env_params_to_default_3d_scene(self) -> None:
        from src.features.composite.core.graph_migration import migrate_graph

        result = migrate_graph(self._v6_graph())
        default_scene = result.get("viewer_state", {}).get("default_3d_scene", {})
        # The v8→v9 global migration now finds the keys (AUD-4701).
        assert default_scene.get("bg_r") == 0.1
        assert default_scene.get("msaa") == 8
        assert default_scene.get("hdri_preset") == "studio.hdr"
        assert default_scene.get("active_camera_id") == "cam_0"
        assert default_scene.get("light_color_g") == 0.9
        # And they are removed from the node at the end of the chain.
        scene3d = next(n for n in result["nodes"] if n["type"] == "scene3d")
        for key in ("bg_r", "msaa", "hdri_preset", "active_camera_id"):
            assert key not in scene3d["parameters"]
