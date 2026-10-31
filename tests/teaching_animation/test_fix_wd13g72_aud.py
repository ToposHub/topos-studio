"""wd13-g72 regression tests: batch D Major findings (AUD-27303…AUD-27717).

Covers the batch-D remediation fixes in ``teaching_animation/core``:

* AUD-27303 — missing file assets report a ``missing_file`` preflight issue
  in preview mode too, not only under ``for_delivery``.
* AUD-27309 — ``AudioRenderPlan.to_timeline`` jointly constrains fades so a
  legal clip never crashes when the scene window trims its duration.
* AUD-27311 — the steps template honours its advertised ``columns`` param.
* AUD-27401 — detach validates persisted override keys against the element
  field whitelist.
* AUD-27402 — master/override updates validate keys like UpdateElementCommand.
* AUD-27412 — ``keep_aspect`` resizes both axes by the uniform factor in
  mixed constraint modes (no stretch).
* AUD-27415 — token resolution survives cycles and malformed category refs.
* AUD-27505 — every fallback font that supplies glyphs gates delivery.
* AUD-27513 — ``from_dict`` recomputes a mismatching sanitizer_hash.
* AUD-27601 — corrupted page-timing numeric fields fall back defensively.
* AUD-27602 — NaN durations are rejected instead of crashing page_start_frame.
* AUD-27641 — non-array scene collection fields fail loudly (no silent loss).
* AUD-27702 — third-party manifests with non-positive viewport/fps/duration
  are rejected at load.
* AUD-27705 — a failed overwrite import keeps the previous template intact.
* AUD-27706 — zip imports enforce entry-count and total-size ceilings.
* AUD-27717 — re-theming a template scene preserves post-template edits.

Run: .venv/bin/python -m pytest tests/teaching_animation/test_fix_wd13g72_aud.py
"""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from src.features.teaching_animation.core.asset_provider import (
    AssetProviderRegistry,
    AssetLicenseStatus,
    LocalAssetProvider,
)
from src.features.teaching_animation.core.audio_plan import AudioClip, AudioRenderPlan
from src.features.teaching_animation.core.builtin_templates import create_builtin_template_registry
from src.features.teaching_animation.core.commands import (
    AddComponentInstanceCommand,
    DetachComponentInstanceCommand,
    RegisterComponentMasterCommand,
    SetComponentOverrideCommand,
    UpdateComponentMasterCommand,
)
from src.features.teaching_animation.core.components import ComponentInstance, ComponentMaster
from src.features.teaching_animation.core.constraints import (
    CanvasConstraints,
    resolve_constraint_transform,
)
from src.features.teaching_animation.core.design_tokens import BrandTheme, DesignTokenSet
from src.features.teaching_animation.core.font_registry import (
    FontEntry,
    FontLicense,
    FontRegistry,
)
from src.features.teaching_animation.core.html_layer import FreeformHtmlMotionLayer
from src.features.teaching_animation.core.html_page_deck import (
    DEFAULT_PAGE_SECONDS,
    PageDeckTiming,
    page_start_frame,
    page_timing_for,
)
from src.features.teaching_animation.core.model import (
    SceneCue,
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.core.motion_graphic_template_library import (
    load_motion_graphic_template_package,
)
from src.features.teaching_animation.core import motion_graphic_template_store as store
from src.features.teaching_animation.core.template_commands import (
    ApplyTemplateCommand,
    SetBrandThemeCommand,
)


# ---------------------------------------------------------------------------
# AUD-27303: preview preflight reports missing file assets
# ---------------------------------------------------------------------------

class TestAud27303MissingFilePreflight:
    def test_preview_mode_reports_missing_file(self, tmp_path: Path) -> None:
        provider = LocalAssetProvider(tmp_path, [{"asset_id": "gone", "path": "missing.png"}])
        registry = AssetProviderRegistry()
        registry.register_provider(provider)
        issues = registry.preflight(("gone",), for_delivery=False)
        assert [issue.code for issue in issues] == ["missing_file"]

    def test_delivery_mode_reports_missing_file_and_license(self, tmp_path: Path) -> None:
        provider = LocalAssetProvider(tmp_path, [{"asset_id": "gone", "path": "missing.png"}])
        registry = AssetProviderRegistry()
        registry.register_provider(provider)
        codes = {issue.code for issue in registry.preflight(("gone",), for_delivery=True)}
        assert "missing_file" in codes
        assert "license_blocks" in codes

    def test_present_file_has_no_missing_file_issue(self, tmp_path: Path) -> None:
        (tmp_path / "ok.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        provider = LocalAssetProvider(tmp_path, [{"asset_id": "ok", "path": "ok.png"}])
        registry = AssetProviderRegistry()
        registry.register_provider(provider)
        assert registry.preflight(("ok",), for_delivery=False) == ()


# ---------------------------------------------------------------------------
# AUD-27309: joint fade constraint after timeline trimming
# ---------------------------------------------------------------------------

class TestAud27309FadeTrimming:
    def test_trimmed_clip_fades_fit_remaining_duration(self) -> None:
        clip = AudioClip(
            clip_id="narration", asset_ref="aud", duration_frames=100,
            fade_in_frames=50, fade_out_frames=50,
        )
        plan = AudioRenderPlan(clips=(clip,))
        mapped = plan.to_timeline(timeline_start=0, scene_in_frame=0, scene_out_frame=60)
        assert len(mapped.clips) == 1
        trimmed = mapped.clips[0]
        assert trimmed.duration_frames == 60
        assert trimmed.fade_in_frames + trimmed.fade_out_frames <= 60

    def test_trimmed_odd_duration_fades_fit(self) -> None:
        clip = AudioClip(
            clip_id="sfx", asset_ref="aud", duration_frames=100,
            fade_in_frames=49, fade_out_frames=50,
        )
        mapped = AudioRenderPlan(clips=(clip,)).to_timeline(
            timeline_start=5, scene_in_frame=0, scene_out_frame=61)
        trimmed = mapped.clips[0]
        assert trimmed.duration_frames == 61
        assert trimmed.fade_in_frames + trimmed.fade_out_frames <= 61

    def test_untrimmed_clip_unchanged(self) -> None:
        clip = AudioClip(
            clip_id="full", asset_ref="aud", duration_frames=100,
            fade_in_frames=20, fade_out_frames=20,
        )
        mapped = AudioRenderPlan(clips=(clip,)).to_timeline(
            timeline_start=0, scene_in_frame=0, scene_out_frame=100)
        assert mapped.clips[0].fade_in_frames == 20
        assert mapped.clips[0].fade_out_frames == 20


# ---------------------------------------------------------------------------
# AUD-27311: steps template honours the columns parameter
# ---------------------------------------------------------------------------

class TestAud27311StepsColumns:
    @staticmethod
    def _compile(params: dict[str, object]) -> TeachingSceneDocument:
        registry = create_builtin_template_registry()
        instance = registry.instantiate("steps", params)
        return registry.compile(instance)

    def test_columns_two_wraps_after_two_cards(self) -> None:
        scene = self._compile({"steps": "a, b, c, d, e", "columns": 2})
        row1 = scene.element_by_id("card_1").transform.y
        row2 = scene.element_by_id("card_2").transform.y
        row3 = scene.element_by_id("card_3").transform.y
        assert row2 == pytest.approx(row1)   # second card stays on row 1
        assert row3 > row1                   # third card wraps to row 2

    def test_columns_zero_keeps_auto_cap(self) -> None:
        scene = self._compile({"steps": "a, b, c, d, e", "columns": 0})
        row1 = scene.element_by_id("card_1").transform.y
        row3 = scene.element_by_id("card_3").transform.y
        assert row3 == pytest.approx(row1)   # auto: min(5, 4) columns

    def test_negative_columns_falls_back_to_auto(self) -> None:
        # Negative integers pass the param-schema validation; the documented
        # contract is "<= 0 means auto".
        scene = self._compile({"steps": "a, b, c, d, e", "columns": -3})
        row1 = scene.element_by_id("card_1").transform.y
        row3 = scene.element_by_id("card_3").transform.y
        assert row3 == pytest.approx(row1)

    def test_columns_above_step_count_clamped(self) -> None:
        scene = self._compile({"steps": "a, b", "columns": 9})
        row1 = scene.element_by_id("card_1").transform.y
        row2 = scene.element_by_id("card_2").transform.y
        assert row2 == pytest.approx(row1)  # one row, two columns


# ---------------------------------------------------------------------------
# AUD-27401 / AUD-27402: element-field whitelist on component write paths
# ---------------------------------------------------------------------------

def _element(element_id: str, x: float, color: str = "#fff") -> SceneElement:
    return SceneElement(
        element_id=element_id, type="shape",
        transform=SceneTransform(x=x, y=10.0, width=40.0, height=40.0),
        style={"color": color},
    )


def _component_scene() -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default("cmd", width=320, height=200, duration_frames=12)
    base = replace(base, elements=(_element("master", 10.0), _element("instance", 10.0)))
    master = ComponentMaster(component_id="card", display_name="Card", element_ids=("master",))
    return RegisterComponentMasterCommand(master).apply(base)


class TestAud27401DetachWhitelist:
    def test_detach_rejects_dirty_persisted_override_keys(self) -> None:
        scene = _component_scene()
        dirty = ComponentInstance(
            "card-instance", "card", ("instance",),
            overrides={"instance": {"element_id": "evil", "style": {"color": "#000"}}},
        )
        scene = AddComponentInstanceCommand(dirty).apply(scene)
        with pytest.raises(ValueError, match="Unsupported element fields"):
            DetachComponentInstanceCommand("card-instance").apply(scene)

    def test_detach_still_bakes_clean_overrides(self) -> None:
        scene = _component_scene()
        instance = ComponentInstance(
            "card-instance", "card", ("instance",),
            overrides={"instance": {"style": {"color": "#ef4444"}}},
        )
        scene = AddComponentInstanceCommand(instance).apply(scene)
        detached = DetachComponentInstanceCommand("card-instance").apply(scene)
        assert detached.element_by_id("instance").style["color"] == "#ef4444"


class TestAud27402UpdateKeyWhitelist:
    def test_master_update_rejects_unknown_keys(self) -> None:
        scene = _component_scene()
        scene = AddComponentInstanceCommand(
            ComponentInstance("card-instance", "card", ("instance",))).apply(scene)
        with pytest.raises(ValueError, match="Unsupported element fields"):
            UpdateComponentMasterCommand("card", {"master": {"bogus": 1}}).apply(scene)

    def test_master_update_rejects_identity_key(self) -> None:
        scene = _component_scene()
        scene = AddComponentInstanceCommand(
            ComponentInstance("card-instance", "card", ("instance",))).apply(scene)
        with pytest.raises(ValueError, match="Unsupported element fields"):
            UpdateComponentMasterCommand("card", {"master": {"element_id": "other"}}).apply(scene)

    def test_override_command_rejects_unknown_keys(self) -> None:
        scene = _component_scene()
        scene = AddComponentInstanceCommand(
            ComponentInstance("card-instance", "card", ("instance",))).apply(scene)
        with pytest.raises(ValueError, match="Unsupported element fields"):
            SetComponentOverrideCommand(
                "card-instance", "instance", {"element_id": "evil"}).apply(scene)

    def test_valid_master_update_still_propagates(self) -> None:
        scene = _component_scene()
        scene = AddComponentInstanceCommand(
            ComponentInstance("card-instance", "card", ("instance",))).apply(scene)
        updated = UpdateComponentMasterCommand(
            "card", {"master": {"style": {"color": "#111111"}}}).apply(scene)
        assert updated.element_by_id("master").style["color"] == "#111111"
        assert updated.element_by_id("instance").style["color"] == "#111111"


# ---------------------------------------------------------------------------
# AUD-27412: keep_aspect never stretches in mixed constraint modes
# ---------------------------------------------------------------------------

class TestAud27412KeepAspectMixedModes:
    BASE = (1920.0, 1080.0)
    TARGET = (1080.0, 1920.0)

    def test_scale_horizontal_top_vertical_no_stretch(self) -> None:
        transform = SceneTransform(x=100.0, y=100.0, width=400.0, height=400.0)
        constraints = CanvasConstraints(horizontal="scale", vertical="top", keep_aspect=True)
        out = resolve_constraint_transform(*self.BASE, *self.TARGET, transform, constraints)
        uniform = 1080.0 / 1920.0
        assert out.width == pytest.approx(400.0 * uniform)   # 225
        assert out.height == pytest.approx(400.0 * uniform)  # not stretched
        assert out.x == pytest.approx(100.0 * uniform)
        assert out.y == pytest.approx(100.0)  # top pin keeps origin distance

    def test_scale_horizontal_bottom_vertical_preserves_bottom_edge(self) -> None:
        transform = SceneTransform(x=100.0, y=100.0, width=400.0, height=400.0)
        constraints = CanvasConstraints(horizontal="scale", vertical="bottom", keep_aspect=True)
        out = resolve_constraint_transform(*self.BASE, *self.TARGET, transform, constraints)
        bottom_margin_before = self.BASE[1] - (transform.y + transform.height)
        assert (self.TARGET[1] - (out.y + out.height)) == pytest.approx(bottom_margin_before)

    def test_scale_vertical_left_horizontal_preserves_left_edge(self) -> None:
        transform = SceneTransform(x=100.0, y=100.0, width=400.0, height=400.0)
        constraints = CanvasConstraints(horizontal="left", vertical="scale", keep_aspect=True)
        out = resolve_constraint_transform(*self.BASE, *self.TARGET, transform, constraints)
        assert out.width == pytest.approx(out.height)  # aspect preserved
        assert out.x == pytest.approx(100.0)

    def test_double_scale_behaviour_unchanged(self) -> None:
        transform = SceneTransform(x=100.0, y=100.0, width=400.0, height=400.0)
        constraints = CanvasConstraints(horizontal="scale", vertical="scale", keep_aspect=True)
        out = resolve_constraint_transform(*self.BASE, *self.TARGET, transform, constraints)
        uniform = 1080.0 / 1920.0
        assert out.width == pytest.approx(400.0 * uniform)
        assert out.height == pytest.approx(400.0 * uniform)
        assert out.x == pytest.approx(100.0 * uniform)
        assert out.y == pytest.approx(100.0 * uniform)


# ---------------------------------------------------------------------------
# AUD-27415: token resolution survives cycles / malformed categories
# ---------------------------------------------------------------------------

class TestAud27415TokenResolution:
    def test_cyclic_reference_degrades_to_unresolved(self) -> None:
        cyclic = DesignTokenSet(palette={"a": "$palette.b", "b": "$palette.a"})
        assert cyclic.resolve_value("$palette.a") == "$palette.a"
        assert cyclic.resolve_value("$palette.b") == "$palette.b"

    def test_self_reference_degrades_to_unresolved(self) -> None:
        tokens = DesignTokenSet(palette={"a": "$palette.a"})
        assert tokens.resolve_value("$palette.a") == "$palette.a"

    def test_malformed_category_returns_ref(self) -> None:
        tokens = DesignTokenSet()
        assert tokens.resolve_value("$to_dict.x") == "$to_dict.x"
        assert tokens.resolve_value("$__class__.x") == "$__class__.x"

    def test_long_chain_still_resolves(self) -> None:
        tokens = DesignTokenSet(palette={
            "a": "$palette.b", "b": "$palette.c", "c": "#00ff00",
        })
        assert tokens.resolve_value("$palette.a") == "#00ff00"

    def test_plain_values_pass_through(self) -> None:
        tokens = DesignTokenSet(palette={"primary": "#123456"})
        assert tokens.resolve_value("#abc") == "#abc"
        assert tokens.resolve_value(5) == 5


# ---------------------------------------------------------------------------
# AUD-27505: fallback fonts that supply glyphs gate delivery
# ---------------------------------------------------------------------------

class TestAud27505FallbackLicenses:
    def test_preview_only_fallback_blocks_delivery(self) -> None:
        registry = FontRegistry()
        registry.register(FontEntry(
            family="Sans", coverage_blocks=frozenset({"basic_latin"}), license=FontLicense.OPEN))
        registry.register(FontEntry(
            family="CJK", coverage_blocks=frozenset({"cjk_unified"}),
            license=FontLicense.PREVIEW_ONLY))
        resolution = registry.resolve_chain("Hello 你好", ["Sans", "CJK"])
        assert resolution.chosen is not None and resolution.chosen.family == "Sans"
        assert resolution.blocks_delivery is True

    def test_open_fallbacks_do_not_block(self) -> None:
        registry = FontRegistry()
        registry.register(FontEntry(
            family="Sans", coverage_blocks=frozenset({"basic_latin"}), license=FontLicense.OPEN))
        registry.register(FontEntry(
            family="CJK", coverage_blocks=frozenset({"cjk_unified"}), license=FontLicense.EMBEDDED))
        resolution = registry.resolve_chain("Hello 你好", ["Sans", "CJK"])
        assert resolution.blocks_delivery is False

    def test_unused_restricted_fallback_does_not_block(self) -> None:
        registry = FontRegistry()
        registry.register(FontEntry(
            family="Sans", coverage_blocks=frozenset({"basic_latin", "cjk_unified"}),
            license=FontLicense.OPEN))
        registry.register(FontEntry(
            family="Emoji", coverage_blocks=frozenset({"emoji"}), license=FontLicense.PREVIEW_ONLY))
        resolution = registry.resolve_chain("Hello 你好", ["Sans", "Emoji"])
        assert resolution.blocks_delivery is False


# ---------------------------------------------------------------------------
# AUD-27513: from_dict recomputes a mismatching sanitizer_hash
# ---------------------------------------------------------------------------

class TestAud27513SanitizerHash:
    PAYLOAD: dict[str, object] = {
        "layer_id": "layer_a",
        "sanitized_html": "<p>hello</p>",
        "viewport_width": 1920,
        "viewport_height": 1080,
        "fps_num": 24,
        "fps_den": 1,
        "duration_frames": 240,
    }

    def test_mismatching_hash_is_recomputed(self) -> None:
        payload = {**self.PAYLOAD, "sanitizer_hash": "deadbeef"}
        layer = FreeformHtmlMotionLayer.from_dict(payload)
        expected = hashlib.sha256(b"<p>hello</p>").hexdigest()
        assert layer.sanitizer_hash == expected
        assert layer.sanitizer_hash != "deadbeef"

    def test_matching_hash_is_kept(self) -> None:
        expected = hashlib.sha256(b"<p>hello</p>").hexdigest()
        payload = {**self.PAYLOAD, "sanitizer_hash": expected}
        assert FreeformHtmlMotionLayer.from_dict(payload).sanitizer_hash == expected

    def test_round_trip_stays_consistent(self) -> None:
        layer = FreeformHtmlMotionLayer.from_dict(dict(self.PAYLOAD))
        restored = FreeformHtmlMotionLayer.from_dict(layer.to_dict())
        assert restored == layer


# ---------------------------------------------------------------------------
# AUD-27601 / AUD-27602: defensive + finite page timing numbers
# ---------------------------------------------------------------------------

class TestAud27601DefensiveTimingNumbers:
    def test_corrupt_default_seconds_falls_back(self) -> None:
        timing = page_timing_for(
            {"page_timing": {"default_seconds": "abc"}}, page_count=2)
        assert timing.default_seconds == DEFAULT_PAGE_SECONDS

    def test_corrupt_duration_entry_degrades_to_default_mode(self) -> None:
        timing = page_timing_for(
            {"page_timing": {"mode": "custom", "durations_seconds": [3.0, "bad"]}},
            page_count=2)
        assert timing.mode == "default"

    def test_durations_container_type_mismatch_degrades(self) -> None:
        timing = page_timing_for(
            {"page_timing": {"mode": "custom", "durations_seconds": 7}}, page_count=1)
        assert timing.mode == "default"

    def test_valid_custom_timing_survives(self) -> None:
        timing = page_timing_for(
            {"page_timing": {"mode": "custom", "durations_seconds": [2.0, 4.0]}},
            page_count=2)
        assert timing.mode == "custom"
        assert timing.durations == (2.0, 4.0)


class TestAud27602NanTimingRejected:
    def test_nan_default_seconds_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            PageDeckTiming(2, "default", float("nan"))

    def test_nan_custom_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            PageDeckTiming(2, "custom", 5.0, (float("nan"), 2.0))

    def test_nan_transition_seconds_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            PageDeckTiming(2, "default", 5.0, transition_seconds=float("nan"))

    def test_nan_metadata_falls_back_and_page_start_frame_works(self) -> None:
        timing = page_timing_for(
            {"page_timing": {"default_seconds": float("nan")}}, page_count=2)
        assert math.isfinite(timing.default_seconds)
        assert page_start_frame(1, 24.0, timing) == round(DEFAULT_PAGE_SECONDS * 24.0)


# ---------------------------------------------------------------------------
# AUD-27641: non-array scene collections fail loudly
# ---------------------------------------------------------------------------

class TestAud27641SceneCollections:
    def test_elements_dict_rejected(self) -> None:
        data = TeachingSceneDocument.create_default("m").to_dict()
        data["elements"] = {"bad": "shape"}
        with pytest.raises(ValueError, match="'elements' must be an array"):
            TeachingSceneDocument.from_dict(data)

    def test_elements_none_rejected(self) -> None:
        data = TeachingSceneDocument.create_default("m").to_dict()
        data["elements"] = None
        with pytest.raises(ValueError, match="'elements' must be an array"):
            TeachingSceneDocument.from_dict(data)

    def test_other_collections_rejected(self) -> None:
        for field in ("animations", "markers", "asset_refs", "cues", "expressions", "pages"):
            data = TeachingSceneDocument.create_default("m").to_dict()
            data[field] = "nope"
            with pytest.raises(ValueError, match=field):
                TeachingSceneDocument.from_dict(data)

    def test_valid_document_still_round_trips(self) -> None:
        scene = TeachingSceneDocument.create_default("m")
        assert TeachingSceneDocument.from_dict(scene.to_dict()) == scene


# ---------------------------------------------------------------------------
# AUD-27702: third-party manifest numeric validation
# ---------------------------------------------------------------------------

def _write_package(root: Path, *, manifest: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "page.html").write_text("<!doctype html><html><body>hi</body></html>", encoding="utf-8")
    (root / "preview.gif").write_bytes(b"GIF89a-fake-preview-bytes")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _valid_manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "template_id": "pkg_a",
        "display_name": "Package A",
        "template_file": "page.html",
        "preview_file": "preview.gif",
    }
    manifest.update(overrides)
    return manifest


class TestAud27702ManifestPositiveNumbers:
    @pytest.mark.parametrize("field", [
        "viewport_width", "viewport_height", "fps_num", "fps_den", "duration_frames",
    ])
    def test_non_positive_field_rejected(self, tmp_path: Path, field: str) -> None:
        package = _write_package(tmp_path / "pkg", manifest=_valid_manifest(**{field: 0}))
        with pytest.raises(ValueError, match=field):
            load_motion_graphic_template_package(package)

    def test_negative_fps_den_rejected(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path / "pkg", manifest=_valid_manifest(fps_den=-2))
        with pytest.raises(ValueError, match="fps_den"):
            load_motion_graphic_template_package(package)

    def test_valid_manifest_still_loads(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path / "pkg", manifest=_valid_manifest())
        assert load_motion_graphic_template_package(package).template.template_id == "pkg_a"


# ---------------------------------------------------------------------------
# AUD-27705 / AUD-27706: store import atomicity + zip ceilings
# ---------------------------------------------------------------------------

class TestAud27705AtomicOverwrite:
    @staticmethod
    def _import(root: Path, package: Path) -> None:
        store.import_template_folder(package, root, origin=store.ORIGIN_USER)

    def test_failed_overwrite_keeps_previous_template(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "library"
        old = _write_package(tmp_path / "old", manifest=_valid_manifest(template_id="pkg_a"))
        self._import(root, old)
        installed_old = load_motion_graphic_template_package(root / "pkg_a")

        new = _write_package(tmp_path / "new", manifest=_valid_manifest(template_id="pkg_a"))

        def boom(src: object, dst: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store.shutil, "copytree", boom)
        with pytest.raises(OSError, match="disk full"):
            store.import_template_folder(new, root, origin=store.ORIGIN_USER, overwrite=True)

        # The original template is intact — not the half-written new package.
        installed_after = load_motion_graphic_template_package(root / "pkg_a")
        assert installed_after.template.html_template == installed_old.template.html_template

    def test_successful_overwrite_replaces_content(self, tmp_path: Path) -> None:
        root = tmp_path / "library"
        old = _write_package(tmp_path / "old", manifest=_valid_manifest(template_id="pkg_a"))
        self._import(root, old)
        (tmp_path / "old" / "page.html").write_text(
            "<!doctype html><html><body>v2</body></html>", encoding="utf-8")
        store.import_template_folder(old, root, origin=store.ORIGIN_USER, overwrite=True)
        installed = load_motion_graphic_template_package(root / "pkg_a")
        assert "v2" in installed.template.html_template

    def test_overwrite_without_flag_refuses(self, tmp_path: Path) -> None:
        root = tmp_path / "library"
        old = _write_package(tmp_path / "old", manifest=_valid_manifest(template_id="pkg_a"))
        self._import(root, old)
        with pytest.raises(store.TemplateStoreError, match="already exists"):
            self._import(root, old)


class TestAud27706ZipCeilings:
    def test_too_many_entries_rejected(self, tmp_path: Path) -> None:
        archive = tmp_path / "bomb_entries.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for index in range(store._MAX_ZIP_ENTRIES + 1):
                zf.writestr(f"f{index}.txt", "x")
        with pytest.raises(store.TemplateStoreError, match="too many entries"):
            store.import_template_zip(archive, tmp_path / "library", origin=store.ORIGIN_USER)

    def test_total_uncompressed_size_rejected(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(store, "_MAX_ZIP_TOTAL_BYTES", 128)
        archive = tmp_path / "bomb_size.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.txt", "a" * 512)
        with pytest.raises(store.TemplateStoreError, match="byte limit"):
            store.import_template_zip(archive, tmp_path / "library", origin=store.ORIGIN_USER)

    def test_small_valid_zip_passes_ceilings(self, tmp_path: Path) -> None:
        package = _write_package(tmp_path / "pkg", manifest=_valid_manifest())
        archive = tmp_path / "pkg.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(package))
        stored = store.import_template_zip(archive, tmp_path / "library", origin=store.ORIGIN_USER)
        assert stored.template_id == "pkg_a"


# ---------------------------------------------------------------------------
# AUD-27717: re-theming preserves post-template edits
# ---------------------------------------------------------------------------

class TestAud27717BrandThemePreservesEdits:
    def test_post_template_edits_survive_retheme(self) -> None:
        scene = ApplyTemplateCommand("steps", {"title": "Demo", "steps": "a, b"}).apply(
            TeachingSceneDocument.create_default("brand"))
        badge = SceneElement(
            element_id="user_badge", type="shape",
            transform=SceneTransform(x=10.0, y=10.0, width=20.0, height=20.0),
        )
        scene = replace(
            scene,
            elements=scene.elements + (badge,),
            cues=(SceneCue(cue_id="cue1", frame=5, label="hello"),),
            metadata={**dict(scene.metadata), "custom_marker": "keep"},
        )

        theme = BrandTheme(theme_id="brandx", palette={"primary": "#ff0000"})
        themed = SetBrandThemeCommand(theme).apply(scene)

        # Post-template increments survive the re-theme.
        assert themed.element_by_id("user_badge") is not None
        assert [cue.cue_id for cue in themed.cues] == ["cue1"]
        assert themed.metadata["custom_marker"] == "keep"
        assert themed.metadata["brand_theme"] == "brandx"
        # Template-owned elements still picked up the themed tokens.
        number = themed.element_by_id("card_1_number")
        assert number is not None
        assert number.style["color"] == "#ff0000"
        # And the instance stays recorded for future re-compiles.
        assert themed.metadata["template_instance"]["template_id"] == "steps"

    def test_user_removed_template_element_stays_removed(self) -> None:
        scene = ApplyTemplateCommand("steps", {"title": "Demo", "steps": "a, b"}).apply(
            TeachingSceneDocument.create_default("brand"))
        scene = scene.with_element_removed("card_1")
        themed = SetBrandThemeCommand(
            BrandTheme(theme_id="brandx", palette={"primary": "#ff0000"})).apply(scene)
        assert themed.element_by_id("card_1") is None
        assert themed.element_by_id("card_1_label") is not None

    def test_undo_restores_pre_theme_state(self) -> None:
        scene = ApplyTemplateCommand("steps", {"title": "Demo", "steps": "a, b"}).apply(
            TeachingSceneDocument.create_default("brand"))
        command = SetBrandThemeCommand(BrandTheme(theme_id="brandx"))
        themed = command.apply(scene)
        assert command.undo(themed) == scene
