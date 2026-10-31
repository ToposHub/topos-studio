"""End-to-end HTML scene → video/sequence export, with delivery provenance.

The ultimate deliverable of the HTML motion layer is an exported video the user
can ship.  These run the real pinned Chromium (captures) + FFmpeg (encode) and
assert the full chain produces a valid, decodable file, plus that the export
manifest carries the W0-A/FH-01 provenance (renderer build, font-set hash,
sanitizer) so a delivered package is auditable.

Marked ``html_renderer`` (CI gate installs Chromium).  The mp4 case also skips
when FFmpeg is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace

import pytest

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.export_service import ExportOptions, TeachingAnimationExportService


W, H, FPS, N = 320, 180, 24, 24           # 1 second — keep the real-browser suite fast


def _backend():
    from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
        PlaywrightChromiumBackend,
    )
    return PlaywrightChromiumBackend()


def _skip_if_no_backend(backend):
    if not backend.is_available():
        pytest.skip(
            "pinned headless browser unavailable (playwright/chromium not installed); "
            "run scripts/bootstrap_html_renderer.sh")


def _scene() -> TeachingSceneDocument:
    html = ("<!DOCTYPE html><html><head><style>"
            "html,body{margin:0;background:#0a1128;width:320px;height:180px}"
            ".bar{position:absolute;top:60px;left:0;width:120px;height:40px;"
            "background:#00e676;animation:m 1s linear infinite}"
            ".t{position:absolute;top:100px;left:40px;color:#fff;font-size:36px;"
            "font-weight:bold}"
            "@keyframes m{0%{left:0}100%{left:200px}}"
            "</style></head><body><div class='bar'></div>"
            "<div class='t' data-topos-node-id='t'>TOPOS</div></body></html>")
    scene = replace(TeachingSceneDocument.create_default(name="export e2e"),
                    fps_num=FPS, fps_den=1, duration_frames=N, width=W, height=H)
    layer = build_html_layer(
        layer_id="L", sanitized_html=html, authored_source=html,
        viewport_width=W, viewport_height=H, fps_num=FPS, fps_den=1, duration_frames=N,
        sanitizer_version="test-sanitizer-1.0",
        csp="default-src 'none'; style-src 'unsafe-inline'",
    )
    return replace(scene, html_layer=layer)


def _provenance(manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("provenance", {}).get("html_layer", {})


def _top_provenance(manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("provenance", {})


@pytest.mark.html_renderer
class TestHtmlExportEndToEnd:
    def test_rgba_sequence_manifest_records_provenance(self, tmp_path):
        backend = _backend()
        _skip_if_no_backend(backend)
        out = tmp_path / "seq"
        svc = TeachingAnimationExportService()
        try:
            svc.export(_scene(), out, options=ExportOptions(
                format="rgba_sequence", fps=float(FPS), width=W, height=H))
        finally:
            svc.close()
        frames = sorted(out.glob("frame_*.rgba"))
        assert len(frames) == N
        prov = _provenance(out / "manifest.json")
        # The font-set hash is the pinned default (the renderer always applies
        # the font lock); the sanitizer + source hashes are stamped on the
        # layer.  All must reach the manifest for the export to be auditable.
        assert prov.get("font_set_hash"), "font_set_hash missing from export manifest"
        assert prov.get("sanitizer_version") == "test-sanitizer-1.0"
        assert prov.get("source_hash") and prov.get("sanitizer_hash")
        assert prov.get("csp")  # the runtime sandbox policy is recorded
        # The manifest locks the render surface and the pinned build that
        # rasterised these frames (release gate: viewport/DPR + renderer build).
        top = _top_provenance(out / "manifest.json")
        assert top.get("viewport") == [W, H]
        assert top.get("dpr") == 1.0
        assert top.get("backend") == "html"
        assert top.get("renderer", "").startswith("teaching-animation-html/"), top.get("renderer")
        assert prov.get("renderer_build"), "renderer_build missing from export manifest"

    def test_html_scene_exports_to_mp4_with_sidecar_provenance(self, tmp_path):
        backend = _backend()
        _skip_if_no_backend(backend)
        if not shutil.which("ffmpeg"):
            pytest.skip("FFmpeg not installed")
        out = tmp_path / "out.mp4"
        svc = TeachingAnimationExportService()
        try:
            result = svc.export(_scene(), out, options=ExportOptions(
                format="mp4", fps=float(FPS), width=W, height=H))
        finally:
            svc.close()
        assert result.frame_count == N and result.width == W and result.height == H
        assert out.is_file() and out.stat().st_size > 0
        # Decodable H.264 with the right frame count/dimensions.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,width,height,codec_name",
             "-of", "default=noprint_wrappers=1", str(out)],
            capture_output=True, text=True, check=True)
        info = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
        assert info.get("codec_name") == "h264"
        assert int(info.get("nb_frames", 0)) == N
        assert info.get("width") == str(W) and info.get("height") == str(H)
        # Sidecar manifest alongside the video carries the same provenance.
        sidecar = out.with_suffix(out.suffix + ".manifest.json")
        assert sidecar.is_file()
        prov = _provenance(sidecar)
        assert prov.get("font_set_hash"), "font_set_hash missing from mp4 sidecar manifest"
