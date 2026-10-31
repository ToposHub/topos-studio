"""WD17-W1 regression tests for six previously BLOCKED audit findings.

Covers (see docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md):

- AUD-29704 — PresetManifest.from_dict wraps tags/schema_version conversion
  errors as PresetError instead of leaking TypeError/ValueError into the
  scanner (scan of a corrupted preset degrades per-preset, never crashes).
- AUD-29705 — the ``preview`` field is validated as a plain in-folder
  relative path, so ``folder / manifest.preview`` can never escape the
  preset library (path traversal / absolute path write-elsewhere).
- AUD-29708 — preview.py offers PresetGifPreviewJob, a QThread worker with
  queued result signals, so the synchronous full-resolution GIF render is
  not the only API a UI slot can call.
- AUD-29801 — render_draw_svg degrades a non-numeric stroke_width to the
  default instead of leaking ValueError into the render thread.
- AUD-29803 — AssetResolver.data_uri enforces size and image/* type caps.
- AUD-30018 — apply_html_template routes on the template's own metadata
  (before schema-key injection), so the conservative sanitizer path is
  reachable again for legacy metadata-less templates.

Run: .venv/bin/python -m pytest tests/teaching_animation/test_fix_wd17w1_aud.py -q
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.core.executable_template import ParamSpec
from src.features.teaching_animation.core.html_template import HtmlTemplate
from src.features.teaching_animation.core.model import (
    SceneAssetRef,
    TeachingSceneDocument,
)
from src.features.teaching_animation.presets import builders
from src.features.teaching_animation.presets.manifest import (
    PRESET_PREVIEW_NAME,
    PresetError,
    PresetManifest,
    normalize_preview_path,
)
from src.features.teaching_animation.render.advanced import render_draw_svg
from src.features.teaching_animation.render.asset_resolver import (
    MAX_DATA_URI_ASSET_BYTES,
    AssetResolver,
)
from src.features.teaching_animation.render.html_layer import apply_html_template


def _manifest_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "preset_id": "my-preset",
        "name": "My Preset",
        "category": "general",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# AUD-29704 — tags / schema_version type errors must surface as PresetError
# --------------------------------------------------------------------------- #


class TestAud29704ManifestTypeErrorWrapped:
    @pytest.mark.parametrize("bad_tags", [5, None, 3.5])
    def test_from_dict_non_iterable_tags_raises_preset_error(self, bad_tags: Any) -> None:
        with pytest.raises(PresetError, match="tags"):
            PresetManifest.from_dict(_manifest_dict(tags=bad_tags))

    @pytest.mark.parametrize("bad_version", ["abc", None, [1], {}])
    def test_from_dict_bad_schema_version_raises_preset_error(self, bad_version: Any) -> None:
        with pytest.raises(PresetError, match="schema_version"):
            PresetManifest.from_dict(_manifest_dict(schema_version=bad_version))

    def test_direct_construction_with_non_iterable_tags_raises_preset_error(self) -> None:
        with pytest.raises(PresetError, match="tags"):
            PresetManifest(
                preset_id="my-preset", name="My Preset", category="general", tags=5)

    def test_lone_string_tag_is_one_tag_not_its_characters(self) -> None:
        manifest = PresetManifest.from_dict(_manifest_dict(tags="intro"))
        assert manifest.tags == ("intro",)

    def test_numeric_tags_are_stringified_and_numeric_schema_coerced(self) -> None:
        manifest = PresetManifest.from_dict(
            _manifest_dict(tags=[1, " ", 2.0], schema_version="2"))
        assert manifest.tags == ("1", "2.0")
        assert manifest.schema_version == 2

    def test_scan_survives_corrupted_manifest_and_degrades_single_preset(
            self, tmp_path: Path) -> None:
        """The library scanner only catches PresetError per folder — a leaked
        TypeError used to kill the whole scan; now the bad preset is skipped."""
        from src.features.teaching_animation.presets import MgPresetLibrary

        good = tmp_path / "general" / "good-preset"
        good.mkdir(parents=True)
        (good / "preset.json").write_text(
            '{"preset_id": "good-preset", "name": "Good", "category": "general"}',
            encoding="utf-8")
        (good / "scene.json").write_text("{}", encoding="utf-8")
        bad = tmp_path / "general" / "bad-preset"
        bad.mkdir(parents=True)
        (bad / "preset.json").write_text(
            '{"preset_id": "bad-preset", "name": "Bad", "category": "general", "tags": 5}',
            encoding="utf-8")

        library = MgPresetLibrary([tmp_path])
        records = library.records()
        assert [record.preset_id for record in records] == ["good-preset"]
        assert any("bad-preset" in error for error in library.errors)


# --------------------------------------------------------------------------- #
# AUD-29705 — preview path traversal / absolute path rejected
# --------------------------------------------------------------------------- #


class TestAud29705PreviewPathAnchored:
    @pytest.mark.parametrize("evil", [
        "../../evil.gif",                 # parent traversal
        "previews/../../evil.gif",        # nested traversal
        "..",                             # bare parent
        "/tmp/evil.gif",                  # absolute POSIX
        "/etc/overwrite.bin",             # absolute root write target
        "C:\\\\evil.gif",                    # absolute Windows drive
        "\\\\server\\\\share\\\\evil.gif",       # UNC path
        "C:evil.gif",                     # drive-relative Windows path
        "..\\\\..\\\\evil.gif",               # backslash traversal
        "sub\\\\..\\\\..\\\\evil.gif",         # mixed-separator traversal
        "  ",                             # blank
        "",                               # empty
    ])
    def test_from_dict_rejects_unsafe_preview(self, evil: str) -> None:
        with pytest.raises(PresetError):
            PresetManifest.from_dict(_manifest_dict(preview=evil))

    def test_direct_construction_rejects_unsafe_preview(self) -> None:
        with pytest.raises(PresetError, match="preview"):
            PresetManifest(
                preset_id="my-preset", name="My Preset", category="general",
                preview="../../evil.gif")

    def test_default_and_nested_relative_previews_pass(self) -> None:
        assert PresetManifest.from_dict(_manifest_dict()).preview == PRESET_PREVIEW_NAME
        manifest = PresetManifest.from_dict(_manifest_dict(preview="previews/nested.gif"))
        assert manifest.preview == "previews/nested.gif"

    def test_joined_preview_stays_inside_preset_folder(self, tmp_path: Path) -> None:
        """The actual security property: every read/write anchor
        ``folder / manifest.preview`` resolves inside ``folder``."""
        folder = tmp_path / "general" / "my-preset"
        folder.mkdir(parents=True)
        manifest = PresetManifest.from_dict(_manifest_dict(preview="previews/nested.gif"))
        assert (folder / manifest.preview).resolve().is_relative_to(folder.resolve())
        with pytest.raises(PresetError):
            PresetManifest.from_dict(_manifest_dict(preview="../x.gif"))

    def test_normalize_preview_path_round_trips_plain_name(self) -> None:
        assert normalize_preview_path("preview.gif") == "preview.gif"
        assert normalize_preview_path(" preview.gif ") == "preview.gif"


# --------------------------------------------------------------------------- #
# AUD-29708 — off-UI-thread preview rendering with queued signals
# --------------------------------------------------------------------------- #


def _probe_scene() -> TeachingSceneDocument:
    # The opacity track makes the sampled frames differ — a fully static
    # scene would be optimized by Pillow into a single-frame GIF.
    return builders.scene_document([
        builders.shape_element("bg", 0, 0, 1920, 1080, kind="rect", fill="#102040"),
        builders.shape_element(
            "pulse", 800, 400, 320, 320, kind="circle", fill="#22d3ee",
            tracks=(builders.track("opacity",
                                   builders.key(0, 0.2), builders.key(6, 1.0),
                                   builders.key(11, 0.2)),)),
    ], name="WD17W1 Probe", duration_frames=12)


class TestAud29708PresetGifPreviewJob:
    def test_job_is_a_qthread_with_result_signals(self) -> None:
        from PyQt6.QtCore import QThread
        from src.features.teaching_animation.presets.preview import PresetGifPreviewJob

        assert issubclass(PresetGifPreviewJob, QThread)
        assert hasattr(PresetGifPreviewJob, "succeeded")
        assert hasattr(PresetGifPreviewJob, "failed")

    def test_run_emits_succeeded_with_path_off_thread(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import src.features.teaching_animation.presets.preview as preview_module

        scene = _probe_scene()
        calls: list[tuple[str, int]] = []
        results: list[Path] = []
        failures: list[str] = []
        expected = tmp_path / "preview.gif"

        def fake_render(scene_arg: Any, out_path: Any, **kwargs: Any) -> Path:
            calls.append((str(out_path), threading.get_ident()))
            assert kwargs.get("width") == 320
            assert kwargs.get("max_frames") == 24
            return Path(out_path)

        monkeypatch.setattr(preview_module, "render_preset_gif", fake_render)
        job = preview_module.PresetGifPreviewJob(scene, expected)
        job.succeeded.connect(results.append)
        job.failed.connect(failures.append)
        # run() executes on the calling thread here, exactly as it will on
        # the QThread worker once started.
        job.run()

        assert calls and calls[0][0] == str(expected)
        assert results == [expected]
        assert failures == []

    def test_run_emits_failed_on_error(
            self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import src.features.teaching_animation.presets.preview as preview_module

        def boom(*args: Any, **kwargs: Any) -> Path:
            raise RuntimeError("renderer exploded")

        monkeypatch.setattr(preview_module, "render_preset_gif", boom)
        results: list[Path] = []
        failures: list[str] = []
        job = preview_module.PresetGifPreviewJob(
            _probe_scene(), tmp_path / "preview.gif")
        job.succeeded.connect(results.append)
        job.failed.connect(failures.append)
        job.run()

        assert results == []
        assert failures == ["renderer exploded"]

    def test_real_render_runs_on_worker_thread_and_writes_gif(
            self, qapp, tmp_path: Path) -> None:
        """End to end: start() renders a real GIF on a non-calling thread and
        the result lands through the queued signal."""
        import src.features.teaching_animation.presets.preview as pv
        from PIL import Image

        real_render = pv.render_preset_gif
        render_threads: list[int] = []

        def spy(scene: Any, out_path: Any, **kwargs: Any) -> Path:
            render_threads.append(threading.get_ident())
            return real_render(scene, out_path, **kwargs)

        # Bind the spy inside the module namespace so job.run() sees it.
        original = pv.render_preset_gif
        pv.render_preset_gif = spy
        try:
            out_path = tmp_path / "preview.gif"
            results: list[Path] = []
            failures: list[str] = []
            job = pv.PresetGifPreviewJob(
                _probe_scene(), out_path, width=160, max_frames=4)
            job.succeeded.connect(results.append)
            job.failed.connect(failures.append)
            job.start()
            assert job.wait(30_000), "preview render job did not finish"
            qapp.processEvents()
        finally:
            pv.render_preset_gif = original

        assert failures == []
        assert results == [out_path]
        assert out_path.is_file()
        assert render_threads and render_threads[0] != threading.get_ident()
        with Image.open(out_path) as gif:
            assert gif.n_frames >= 3


# --------------------------------------------------------------------------- #
# AUD-29801 — non-numeric stroke_width degrades to the default
# --------------------------------------------------------------------------- #


class TestAud29801StrokeWidthGuard:
    def _points(self) -> list[list[float]]:
        return [[0.0, 0.0], [10.0, 10.0], [20.0, 5.0]]

    @pytest.mark.parametrize("bad", ["thick", None, [1], {"v": 2}, object()])
    def test_non_numeric_stroke_width_falls_back_to_default(self, bad: Any) -> None:
        svg = render_draw_svg({"points": self._points(), "stroke_width": bad}, 100, 100)
        assert 'stroke-width="3.000000"' in svg

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "float('nan')"])
    def test_non_finite_stroke_width_falls_back_to_default(self, bad: str) -> None:
        svg = render_draw_svg({"points": self._points(), "stroke_width": bad}, 100, 100)
        assert 'stroke-width="3.000000"' in svg

    def test_numeric_stroke_width_still_honoured(self) -> None:
        for good, expected in [("4.5", "4.500000"), (6, "6.000000"), (2.25, "2.250000")]:
            svg = render_draw_svg(
                {"points": self._points(), "stroke_width": good}, 100, 100)
            assert f'stroke-width="{expected}"' in svg

    def test_missing_stroke_width_keeps_default(self) -> None:
        svg = render_draw_svg({"points": self._points()}, 100, 100)
        assert 'stroke-width="3.000000"' in svg


# --------------------------------------------------------------------------- #
# AUD-29803 — data_uri size and type caps
# --------------------------------------------------------------------------- #


def _resolver_with_asset(tmp_path: Path, filename: str, payload: bytes) -> tuple[
        AssetResolver, TeachingSceneDocument, SceneAssetRef]:
    asset_root = tmp_path / "assets"
    asset_root.mkdir(exist_ok=True)
    (asset_root / filename).write_bytes(payload)
    asset = SceneAssetRef(asset_id="a1", kind="image", path=filename)
    scene = builders.scene_document(
        [builders.shape_element("bg", 0, 0, 64, 64, kind="rect", fill="#123456")],
        name="Assets", duration_frames=6)
    scene = replace(scene, asset_refs=(asset,))
    context = RenderContext(
        fps=24.0, width=64, height=64, backend="svg",
        asset_root=str(asset_root), strict_assets=False, render_profile="preview")
    resolver = AssetResolver(context)
    resolver.preflight(scene)
    return resolver, scene, asset


class TestAud29803DataUriCaps:
    def test_small_image_asset_inlines_as_data_uri(self, tmp_path: Path) -> None:
        from PIL import Image
        import io
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "#ff0000").save(buffer, format="PNG")
        resolver, _, asset = _resolver_with_asset(
            tmp_path, "dot.png", buffer.getvalue())
        uri = resolver.data_uri(asset)
        assert uri is not None
        assert uri.startswith("data:image/png;base64,")

    def test_oversized_asset_is_not_inlined(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stat-size pre-check refuses an asset above the cap before any
        whole-file read happens."""
        import src.features.teaching_animation.render.asset_resolver as module
        monkeypatch.setattr(module, "MAX_DATA_URI_ASSET_BYTES", 16)
        resolver, _, asset = _resolver_with_asset(
            tmp_path, "huge.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64)
        assert resolver.data_uri(asset) is None

    def test_non_image_type_is_not_inlined(self, tmp_path: Path) -> None:
        resolver, _, asset = _resolver_with_asset(
            tmp_path, "notes.txt", b"not an image at all")
        assert resolver.data_uri(asset) is None

    def test_extensionless_asset_is_not_inlined(self, tmp_path: Path) -> None:
        resolver, _, asset = _resolver_with_asset(
            tmp_path, "blob", b"\x00\x01\x02")
        assert resolver.data_uri(asset) is None

    def test_unreadable_asset_degrades_to_none(self, tmp_path: Path) -> None:
        resolver, scene, asset = _resolver_with_asset(
            tmp_path, "dot.png", b"\x89PNG\r\n\x1a\n")
        (tmp_path / "assets" / "dot.png").unlink()
        # Re-run preflight: the missing asset resolves to None.
        fresh = AssetResolver(resolver.context)
        fresh.preflight(scene)
        assert fresh.data_uri(asset) is None

    def test_cap_constant_is_documented_size(self) -> None:
        assert MAX_DATA_URI_ASSET_BYTES == 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# AUD-30018 — conservative sanitizer path reachable for metadata-less templates
# --------------------------------------------------------------------------- #

# Authored markup with an event handler: the freeform Motion Graphic profile
# keeps it (it is the template author's own HTML), the conservative profile
# strips it — the observable difference between the two rebuild paths.
_TEMPLATE_HTML = (
    '<div data-topos-node-id="title">{{slot:title}}</div>'
    '<img src="x.png" onerror="alert(1)">'
)


def _template(metadata: dict[str, Any] | None) -> HtmlTemplate:
    return HtmlTemplate(
        template_id="t",
        display_name="T",
        html_template=_TEMPLATE_HTML,
        params_schema=(ParamSpec(name="title", type="string", default="Hello"),),
        viewport_width=320,
        viewport_height=180,
        metadata=metadata if metadata is not None else {},
    )


class TestAud30018ConservativePathNotDead:
    def test_empty_metadata_takes_conservative_sanitizer_path(self) -> None:
        layer = apply_html_template(_template({}), layer_id="L-legacy")
        # Conservative rebuild: no freeform browser profile flags are granted.
        assert "render_profile" not in layer.metadata
        # The authored on* handler is stripped by the conservative sanitizer.
        assert "onerror" not in layer.sanitized_html

    def test_nonempty_metadata_keeps_freeform_path(self) -> None:
        layer = apply_html_template(
            _template({"render_profile": "motion_graphic_freeform"}), layer_id="L-mg")
        assert layer.metadata.get("render_profile") == "motion_graphic_freeform"
        # The template author's own handler survives the freeform profile.
        assert "onerror" in layer.sanitized_html

    def test_parameter_schema_carried_on_both_paths(self) -> None:
        legacy = apply_html_template(_template({}), layer_id="L-legacy")
        freeform = apply_html_template(
            _template({"render_profile": "motion_graphic_freeform"}), layer_id="L-mg")
        for layer in (legacy, freeform):
            schema = layer.metadata.get("motion_graphic_parameter_schema")
            assert isinstance(schema, list) and schema
            assert schema[0]["name"] == "title"

    def test_params_still_substituted_on_conservative_path(self) -> None:
        layer = apply_html_template(
            _template({}), {"title": "WD17W1"}, layer_id="L-legacy")
        assert "WD17W1" in layer.sanitized_html
