"""wd14-g74 regression tests: batch D Major findings (teaching motion + presets).

Covers the batch-D remediation fixes for REMEDIATION_PLAN lines 1069-1088:

* AUD-28119 — ``slide`` preset rejects unknown directions instead of
  silently falling back to the "up" sign.
* AUD-28200 — ``with_step_gap`` rejects oversized overlaps at edit time
  (with the offending step named) instead of blowing up inside ``layout()``.
* AUD-28203 — ``spring(duration=...)`` never snaps an unsettled spring to
  its target (no single-frame jump) and defers the exact snap to ``duration``.
* AUD-28206 — ``TimeRange.shifted`` stays a pure translation; negative
  overflow raises instead of silently clamping to frame 0.
* AUD-28301 — ``extract_fragment`` retimes saved keyframes to a 0-based
  preset frame space (host-global frames are normalised away).
* AUD-28320 — importing a preset whose id clashes rewrites ``preset.json``
  so the record rescans under the deduplicated id (no shadowing, no fake
  "failed to rescan", for scene and code presets alike).
* AUD-28321 — ``save_preset`` parses/validates the scene before touching
  disk, writes ``scene.json`` before ``preset.json``, cleans up on write
  failure, and logs (never silently swallows) preview failures.
* AUD-28900 — lens_flare_stack breathing scales pivot the disc centre.
* AUD-29200 — floating_orbs loops with uniform segment speed (no ~8x rush
  before the loop point) and a seamless wrap value.
* AUD-29701 — a preset whose scene-level animations all dangle loads (the
  animations are dropped) instead of failing validation.
* AUD-29702 — a malformed ``scene.json`` surfaces as ``PresetError``, not a
  raw ValueError/TypeError escaping the PresetError-only callers.

Run: .venv/bin/python -m pytest tests/teaching_animation/test_fix_wd14g74_aud.py
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.core.hierarchy import Hierarchy, write_hierarchy_into
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.core.validator import validate_scene
from src.features.teaching_animation.motion import (
    SeriesGroup,
    SeriesStep,
    SpringConfig,
    TimeRange,
    measure_spring,
    spring,
)
from src.features.teaching_animation.motion.presets import create_default_preset_registry
from src.features.teaching_animation.presets import builders
from src.features.teaching_animation.presets.fragment import (
    extract_fragment,
    fragment_max_keyframe_frame,
)
from src.features.teaching_animation.presets.library import MgPresetLibrary, PresetError
from src.features.teaching_animation.presets.loader import load_preset_scene
from src.features.teaching_animation.presets.manifest import PresetManifest
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_build_module(relative: str):
    path = REPO_ROOT / "src/features/teaching_animation/presets/library" / relative
    spec = importlib.util.spec_from_file_location(f"wd14g74_{path.parent.name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _sample_scene_data() -> dict:
    return builders.scene_document([
        builders.text_element("t1", 100, 100, "Saved Preset", size=64),
        builders.shape_element("s1", 100, 220, 400, 20, kind="rect", fill="#ff8800",
                               tracks=(builders.track("opacity", builders.key(0, 0.0),
                                                      builders.key(20, 1.0)),)),
    ], name="Saved Preset", duration_frames=30).to_dict()


# -- AUD-28119 --------------------------------------------------------------

class TestSlideDirectionValidation:
    @pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
    def test_known_directions_build(self, direction: str) -> None:
        registry = create_default_preset_registry()
        fn = registry.build("slide", direction=direction)
        assert callable(fn)

    def test_unknown_direction_raises(self) -> None:
        registry = create_default_preset_registry()
        with pytest.raises(ValueError, match="slide direction"):
            registry.build("slide", direction="top")

    def test_casing_is_not_normalized(self) -> None:
        registry = create_default_preset_registry()
        with pytest.raises(ValueError, match="slide direction"):
            registry.build("slide", direction="Up")

    def test_valid_directions_keep_distinct_signs(self) -> None:
        registry = create_default_preset_registry()
        up = registry.build("slide", direction="up", distance=80.0, duration=10)
        down = registry.build("slide", direction="down", distance=80.0, duration=10)
        assert up(0, 24.0) == pytest.approx(80.0)
        assert down(0, 24.0) == pytest.approx(-80.0)


# -- AUD-28200 --------------------------------------------------------------

class TestSeriesGapValidation:
    def test_moderate_overlap_still_allowed(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 10, gap_after=-4), SeriesStep("b", 8)))
        layout = dict(series.layout())
        assert layout["b"].start == 6

    def test_oversized_overlap_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="'b'"):
            SeriesGroup(steps=(SeriesStep("a", 5, gap_after=-100), SeriesStep("b", 5)))

    def test_with_step_gap_rejects_oversized_overlap_with_context(self) -> None:
        series = SeriesGroup(steps=(SeriesStep("a", 5), SeriesStep("b", 5)))
        with pytest.raises(ValueError, match="gap_after overlap is too large"):
            series.with_step_gap("a", -100)

    def test_layout_never_sees_a_negative_start(self) -> None:
        # The edit-time guard means any constructible group lays out cleanly.
        series = SeriesGroup(steps=(SeriesStep("a", 5, gap_after=-4),
                                    SeriesStep("b", 5, gap_after=-4),
                                    SeriesStep("c", 5)))
        assert all(tr.start >= 0 for _, tr in series.layout())


# -- AUD-28203 --------------------------------------------------------------

class TestSpringDurationContract:
    def test_slow_unsettled_spring_has_no_snap_jump(self) -> None:
        # zeta = 0.1, omega0 = 0.1 -> the true settle is far past the
        # 600-frame scan cap, so measure_spring reports the sentinel.
        cfg = SpringConfig(mass=100.0, damping=2.0, stiffness=1.0)
        assert measure_spring(24.0, cfg) == 600
        values = [spring(f, 24.0, cfg, duration=30) for f in range(0, 700)]
        steps = [abs(values[i + 1] - values[i]) for i in range(len(values) - 1)]
        assert max(steps) < 0.05, "unsettled spring must keep following physics"

    def test_duration_shorter_than_settle_does_not_truncate(self) -> None:
        cfg = SpringConfig(mass=1.0, damping=10.0, stiffness=100.0)
        settle = measure_spring(24.0, cfg)
        assert 20 < settle < 200
        # Between duration and settle the spring is still in motion.
        values = [spring(f, 24.0, cfg, duration=max(1, settle - 20)) for f in range(settle - 15, settle)]
        assert any(abs(v - 1.0) > 0.005 for v in values)

    def test_duration_defers_exact_snap_until_endpoint(self) -> None:
        cfg = SpringConfig(mass=1.0, damping=10.0, stiffness=100.0)
        settle = measure_spring(24.0, cfg)
        assert spring(settle + 5, 24.0, cfg) == 1.0
        endpoint = settle + 20
        mid = spring(settle + 5, 24.0, cfg, duration=endpoint)
        assert mid != 1.0  # residual within threshold is kept until duration
        assert spring(endpoint + 1, 24.0, cfg, duration=endpoint) == 1.0

    def test_near_critical_still_finite(self) -> None:
        # AUD-28202 regression guard must survive the snap restructure.
        cfg = SpringConfig(mass=1.0, damping=3.4641016151377544, stiffness=3.0)
        values = [spring(f, 24.0, cfg, duration=50) for f in range(0, 200)]
        assert all(abs(values[i + 1] - values[i]) < 0.05 for i in range(len(values) - 1))
        assert values[-1] == pytest.approx(1.0, abs=1e-3)


# -- AUD-28206 --------------------------------------------------------------

class TestShiftedContract:
    def test_negative_overflow_raises(self) -> None:
        with pytest.raises(ValueError, match="shift"):
            TimeRange(start=5, duration=10).shifted(-8)

    def test_exact_boundary_is_legal(self) -> None:
        assert TimeRange(start=5, duration=10).shifted(-5).start == 0

    def test_translation_preserves_sibling_geometry(self) -> None:
        a = TimeRange(start=5, duration=10).shifted(-3)
        b = TimeRange(start=10, duration=10).shifted(-3)
        assert b.start - a.start == 5
        assert (a.duration, a.trim_in) == (10, 0)

    def test_positive_shift_unchanged(self) -> None:
        tr = TimeRange(start=10, duration=5, trim_in=2)
        assert tr.shifted(8).start == 18
        assert tr.shifted(8).trim_in == 2


# -- AUD-28301 --------------------------------------------------------------

class TestExtractFragmentTimeOrigin:
    def _host(self, first_frame: int) -> TeachingSceneDocument:
        return TeachingSceneDocument.from_dict({
            "scene_id": "host", "name": "Host", "width": 1920, "height": 1080,
            "fps_num": 24, "fps_den": 1, "duration_frames": 120,
            "background": {"color": "#111827"},
            "elements": [
                {"element_id": "plate", "type": "shape", "z_index": 1,
                 "transform": {"x": 200, "y": 100, "width": 300, "height": 80},
                 "style": {"fill": "#ffffff", "stroke": "#000000", "stroke_width": 0.0},
                 "content": {"kind": "rect"}},
                {"element_id": "child", "type": "text", "z_index": 2,
                 "transform": {"x": 220, "y": 140, "width": 120, "height": 40},
                 "style": {}, "content": "label"},
            ],
            "animations": [{
                "animation_id": "anim_1", "element_id": "child", "property": "x",
                "param_name": "x",
                "keyframes": [
                    {"frame": first_frame, "value": 220.0},
                    {"frame": first_frame + 30, "value": 320.0, "interpolation": "ease_out"},
                ],
                "min_val": 0.0, "max_val": 1000.0,
            }],
            "metadata": write_hierarchy_into({}, Hierarchy(parents={"child": "plate"})),
        })

    def test_keyframes_normalized_to_zero_based_space(self) -> None:
        fragment = TeachingSceneDocument.from_dict(extract_fragment(self._host(40), ["plate"]))
        assert validate_scene(fragment).is_valid
        frames = [kf.frame
                  for element in fragment.elements
                  for track in element.local_animation_tracks
                  for kf in track.keyframes]
        assert frames == [0, 30]

    def test_fragment_max_keyframe_frame_is_fragment_relative(self) -> None:
        fragment = TeachingSceneDocument.from_dict(extract_fragment(self._host(40), ["plate"]))
        assert fragment_max_keyframe_frame(fragment.elements) == 30

    def test_relative_timing_between_elements_kept(self) -> None:
        host = self._host(40)
        # A second animated element starting 10 frames later than the first.
        extra = {
            "animation_id": "anim_2", "element_id": "plate", "property": "y",
            "param_name": "y",
            "keyframes": [
                {"frame": 50, "value": 100.0},
                {"frame": 80, "value": 160.0},
            ],
            "min_val": 0.0, "max_val": 1000.0,
        }
        host_dict = host.to_dict()
        host_dict["animations"] = host_dict["animations"] + [extra]
        fragment = TeachingSceneDocument.from_dict(
            extract_fragment(TeachingSceneDocument.from_dict(host_dict), ["plate"]))
        by_element = {
            element.element_id: [kf.frame for track in element.local_animation_tracks
                                 for kf in track.keyframes]
            for element in fragment.elements
        }
        assert by_element["child"] == [0, 30]
        assert by_element["plate"] == [10, 40]

    def test_static_selection_unchanged(self) -> None:
        host = self._host(40)
        static_host = TeachingSceneDocument.from_dict(
            {**host.to_dict(), "animations": []})
        fragment = TeachingSceneDocument.from_dict(extract_fragment(static_host, ["plate"]))
        assert fragment_max_keyframe_frame(fragment.elements) == -1


# -- AUD-28320 / AUD-28321 ---------------------------------------------------

class TestPresetLibrarySaveAndImport:
    def _library(self, tmp_path: Path) -> MgPresetLibrary:
        return MgPresetLibrary([tmp_path / "user"])

    def test_save_preset_rejects_invalid_scene_data(self, tmp_path: Path, qapp) -> None:
        library = self._library(tmp_path)
        with pytest.raises(PresetError, match="scene data"):
            library.save_preset(name="Broken", category="general",
                                scene_data={"elements": {}})
        user_root = tmp_path / "user"
        assert not user_root.exists() or not any(user_root.rglob("preset.json"))

    def test_save_preset_rejects_unparseable_scene_data(self, tmp_path: Path, qapp) -> None:
        library = self._library(tmp_path)
        with pytest.raises(PresetError):
            library.save_preset(name="Broken", category="general", scene_data={"width": "abc"})
        assert not any((tmp_path / "user").rglob("preset.json"))

    def test_save_preset_write_failure_leaves_no_ghost(self, tmp_path: Path, qapp, monkeypatch) -> None:
        library = self._library(tmp_path)
        real_write = Path.write_text

        def failing_write(self: Path, data: str, encoding: str | None = None,
                          errors: str | None = None) -> int:
            if self.name == "preset.json":
                raise OSError("disk full")
            return real_write(self, data, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "write_text", failing_write)
        with pytest.raises(OSError):
            library.save_preset(name="Ghost", category="general",
                                scene_data=_sample_scene_data())
        monkeypatch.undo()
        category_dir = tmp_path / "user" / "general"
        assert not category_dir.exists() or not any(category_dir.iterdir())

    def test_save_preset_preview_failure_is_logged_not_swallowed(
            self, tmp_path: Path, qapp, monkeypatch, caplog) -> None:
        import src.features.teaching_animation.presets.library as library_module

        def boom(scene, out_path, **kwargs):
            raise RuntimeError("preview boom")

        monkeypatch.setattr(library_module, "render_preset_gif", boom)
        library = self._library(tmp_path)
        with caplog.at_level(logging.WARNING):
            record = library.save_preset(name="No Preview", category="general",
                                         scene_data=_sample_scene_data())
        assert (record.folder / "scene.json").is_file()
        assert (record.folder / "preset.json").is_file()
        assert not (record.folder / "preview.gif").exists()
        assert any("preview" in message and record.preset_id in message
                   for message in caplog.messages)

    def _write_scene_preset(self, root: Path, preset_id: str) -> Path:
        folder = root / "general" / preset_id
        folder.mkdir(parents=True)
        (folder / "preset.json").write_text(json.dumps(
            {"preset_id": preset_id, "name": preset_id, "category": "general"}), "utf-8")
        (folder / "scene.json").write_text(json.dumps(_sample_scene_data()), "utf-8")
        return folder

    def test_import_conflict_rewrites_manifest_id(self, tmp_path: Path, qapp) -> None:
        library = self._library(tmp_path)
        original = self._write_scene_preset(tmp_path / "user", "clash")
        source = self._write_scene_preset(tmp_path / "elsewhere", "clash")

        imported = library.import_from(source)
        assert imported.preset_id != "clash"
        assert library.get(imported.preset_id) is not None
        # preset.json now carries the deduplicated id ...
        rewritten = json.loads((imported.folder / "preset.json").read_text("utf-8"))
        assert rewritten["preset_id"] == imported.preset_id
        # ... and the pre-existing preset is not shadowed.
        kept = library.get("clash")
        assert kept is not None and kept.folder == original

    def test_import_conflict_twice_keeps_records_distinct(self, tmp_path: Path, qapp) -> None:
        library = self._library(tmp_path)
        self._write_scene_preset(tmp_path / "user", "clash")
        source = self._write_scene_preset(tmp_path / "elsewhere", "clash")
        first = library.import_from(source)
        second = library.import_from(source)
        ids = {record.preset_id for record in library.records()}
        assert {"clash", first.preset_id, second.preset_id} <= ids
        assert len(ids) == len(library.records())

    def test_import_conflicting_code_preset_writes_manifest(self, tmp_path: Path, qapp) -> None:
        library = self._library(tmp_path)
        self._write_scene_preset(tmp_path / "user", "clash")
        source = tmp_path / "code_src" / "clash"
        source.mkdir(parents=True)
        (source / "build.py").write_text(
            "from src.features.teaching_animation.presets.builders import (\n"
            "    scene_document, text_element,\n"
            ")\n"
            "PRESET = {\"preset_id\": \"clash\", \"name\": \"Clash\", \"category\": \"general\"}\n"
            "def build():\n"
            "    return scene_document(\n"
            "        [text_element(\"t\", 10, 10, \"hi\")], name=\"Clash\",\n"
            "        duration_frames=12).to_dict()\n", "utf-8")

        imported = library.import_from(source)
        assert imported.preset_id != "clash"
        assert imported.manifest.entry == "build.py"
        manifest = json.loads((imported.folder / "preset.json").read_text("utf-8"))
        assert manifest["preset_id"] == imported.preset_id
        assert manifest["entry"] == "build.py"
        # The code preset still loads and executes through its build.py.
        scene = load_preset_scene(imported.manifest, imported.folder)
        assert [element.element_id for element in scene.elements] == ["t"]


# -- AUD-28900 / AUD-29200 ---------------------------------------------------

class TestBundledPresetGeometry:
    def _render(self, scene_dict: dict, frame: int) -> str:
        document = TeachingSceneDocument.from_dict(scene_dict)
        renderer = SvgSceneRenderer()
        context = RenderContext(fps=24.0, width=1920, height=1080, backend="svg",
                                strict_assets=False, render_profile="interactive")
        renderer.prepare(document, context)
        return renderer.render_frame(AnimationFrame(frame, 24.0)).data.decode("utf-8")

    def _group_for(self, svg: str, element_id: str) -> str:
        marker = f'data-element-id="{element_id}"'
        start = svg.index(marker)
        open_tag = svg.rindex("<g ", 0, start)
        return svg[open_tag:svg.index("</g>", start)]

    def test_lens_flare_breathing_pivots_disc_centre(self) -> None:
        module = _load_build_module("light/lens_flare_stack/build.py")
        scene_dict = module.build()
        document = TeachingSceneDocument.from_dict(scene_dict)
        assert validate_scene(document).is_valid

        halo = next(element for element in document.elements
                    if element.element_id == "halo_big")
        assert halo.content["kind"] == "path"
        assert halo.content["d"].startswith("M -320")

        frame0 = self._group_for(self._render(scene_dict, 0), "halo_big")
        frame26 = self._group_for(self._render(scene_dict, 26), "halo_big")
        # The group transform pins the disc centre for every pulse phase ...
        assert "translate(880.000000 540.000000)" in frame0
        assert "translate(880.000000 540.000000)" in frame26
        # ... while the breathing scale really animates (1.0 -> 1.07).
        assert "scale(1.000000 1.000000)" in frame0
        assert "scale(1.070000 1.070000)" in frame26

    def test_floating_orbs_loop_segments_are_uniform_and_seamless(self) -> None:
        module = _load_build_module("particles/floating_orbs/build.py")
        document = TeachingSceneDocument.from_dict(module.build())
        assert validate_scene(document).is_valid
        checked = 0
        for element in document.elements:
            for track_item in element.local_animation_tracks:
                if track_item.param_name != "y":
                    continue
                keyframes = track_item.keyframes
                assert keyframes[0].frame == 0
                assert keyframes[-1].frame == 96
                # Seamless wrap: identical value at frame 0 and frame 96.
                assert keyframes[0].value == pytest.approx(keyframes[-1].value)
                speeds = [
                    abs(keyframes[i + 1].value - keyframes[i].value)
                    / (keyframes[i + 1].frame - keyframes[i].frame)
                    for i in range(len(keyframes) - 1)
                ]
                assert speeds, "y track needs at least one segment"
                assert max(speeds) / min(speeds) < 1.02, (
                    f"{element.element_id}: segment speeds not uniform: {speeds}")
                checked += 1
        assert checked == 18


# -- AUD-29701 / AUD-29702 ---------------------------------------------------

def _write_preset_package(folder: Path, preset_id: str, scene_payload) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "preset.json").write_text(json.dumps(
        {"preset_id": preset_id, "name": preset_id, "category": "general"}), "utf-8")
    if isinstance(scene_payload, str):
        (folder / "scene.json").write_text(scene_payload, "utf-8")
    else:
        (folder / "scene.json").write_text(json.dumps(scene_payload), "utf-8")
    return folder


def _manifest(preset_id: str) -> PresetManifest:
    return PresetManifest(preset_id=preset_id, name=preset_id, category="general")


class TestLoadPresetScene:
    def _scene_with_animations(self, animations: list) -> dict:
        base = builders.scene_document([
            builders.shape_element("e1", 10, 10, 100, 40, kind="rect"),
        ], name="Dangle", duration_frames=30).to_dict()
        base["animations"] = animations
        return base

    def test_all_dangling_scene_animations_are_dropped(self, tmp_path: Path) -> None:
        payload = self._scene_with_animations([{
            "animation_id": "a1", "element_id": "ghost", "property": "x",
            "param_name": "x",
            "keyframes": [{"frame": 0, "value": 0.0}, {"frame": 10, "value": 5.0}],
            "min_val": 0.0, "max_val": 10.0,
        }])
        folder = _write_preset_package(tmp_path / "dangling", "dangling", payload)
        scene = load_preset_scene(_manifest("dangling"), folder)
        assert scene.animations == ()
        assert [element.element_id for element in scene.elements] == ["e1"]

    def test_matching_scene_animation_still_folds(self, tmp_path: Path) -> None:
        payload = self._scene_with_animations([{
            "animation_id": "a1", "element_id": "e1", "property": "x",
            "param_name": "x",
            "keyframes": [{"frame": 0, "value": 10.0}, {"frame": 12, "value": 50.0}],
            "min_val": 0.0, "max_val": 1000.0,
        }])
        folder = _write_preset_package(tmp_path / "folding", "folding", payload)
        scene = load_preset_scene(_manifest("folding"), folder)
        assert scene.animations == ()
        element = scene.element_by_id("e1")
        assert element is not None
        assert any(track.param_name == "x" for track in element.local_animation_tracks)

    def test_non_object_scene_json_wrapped_as_preset_error(self, tmp_path: Path) -> None:
        folder = _write_preset_package(tmp_path / "array", "array", "[1, 2, 3]")
        with pytest.raises(PresetError, match="malformed"):
            load_preset_scene(_manifest("array"), folder)

    def test_bad_numeric_fields_wrapped_as_preset_error(self, tmp_path: Path) -> None:
        payload = self._scene_with_animations([])
        payload["width"] = "not-a-number"
        folder = _write_preset_package(tmp_path / "numeric", "numeric", payload)
        with pytest.raises(PresetError):
            load_preset_scene(_manifest("numeric"), folder)
