#!/usr/bin/env python
"""Demo: generate a Freeform HTML motion layer with the configured model and
export both the sanitized HTML and a rendered PNG frame, so the output is
inspectable directly.

Pipeline: active LLM profile (e.g. DeepSeek-v4-flash) -> generate_html_with_llm
(sanitize -> static analysis -> preview on pinned Chromium) -> render one frame
through HtmlSceneRendererV2 -> write HTML + PNG to /tmp.

Run:
    .venv/bin/python scripts/demo_html_llm_generate.py
    # then open /tmp/topos_llm_demo.html and /tmp/topos_llm_demo_frame.png
"""

from __future__ import annotations

import sys
from dataclasses import replace

from src.features.edit.ai_editing.credential_store import HybridCredentialStore
from src.features.edit.ai_editing.llm_client import make_client
from src.features.edit.ai_editing.provider_profiles import ProfileRegistry

from src.features.teaching_animation.ai.html_generation_protocol import (
    HtmlGenerationConstraints, HtmlPipelineStage,
)
from src.features.teaching_animation.ai.html_llm_generator import (
    LlmGenerationRequest, generate_html_with_llm,
)
from src.features.teaching_animation.core.contracts import (
    AlphaMode, CanvasSpace, FrameContext, RenderSurface,
)
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render import scene_renderer_router as router
from src.features.teaching_animation.render.html_renderer_spike import CaptureSurface
from src.features.teaching_animation.render.html_scene_renderer_v2 import HtmlSceneRendererV2


W, H, FPS, DUR_S = 480, 270, 24, 5


def main() -> int:
    # 1. Active LLM profile.
    reg = ProfileRegistry()
    reg.load_from_user_file()
    profile = reg.active_profile
    if profile is None:
        print("no active AI profile in ~/.topos/ai_profiles.json", file=sys.stderr)
        return 1
    print(f"== model: {profile.display_name} ({profile.model_id}) ==")
    client = make_client(profile, HybridCredentialStore())

    # 2. Pinned Chromium backend (for preview + final render).
    backend = router.make_playwright_backend_factory()()
    if not backend.is_available():
        print("pinned Chromium unavailable — run scripts/bootstrap_html_renderer.sh",
              file=sys.stderr)
        return 1

    # 3. Generate -> pipeline.
    request = LlmGenerationRequest(
        user_prompt=(
            f"Design a {DUR_S}-second dark tech title card, {W}x{H} CSS px: a bold "
            "title 'TOPOS COLOR', a one-line subtitle, and three small feature "
            "cards that stagger in with a CSS keyframe entrance. Use a confident "
            "dark palette (deep navy background) with high-contrast white text "
            "and one accent color. Output only the HTML document."
        ),
        temperature=0.5, max_tokens=4000,
    )
    constraints = HtmlGenerationConstraints(
        viewport_width=W, viewport_height=H, max_duration_seconds=float(DUR_S))
    print("== asking the model to design the page... ==")
    outcome = generate_html_with_llm(
        client, request=request, constraints=constraints,
        approved=True, renderer_available=True,
        preview_backend=backend, preview_frame=FPS * 1,            # 1s in
        preview_surface=CaptureSurface(W, H, 1.0),
    )

    print("== pipeline result ==")
    for stage in outcome.stages:
        flag = "OK " if stage.passed else "XX "
        print(f"  {flag}{stage.stage.value:<16} {stage.detail}")
    print(f"  writable={outcome.writable}  quality_errors={len(outcome.quality.errors)}")
    if outcome.preview:
        print(f"  preview: deterministic={outcome.preview.get('deterministic')} "
              f"build={outcome.preview.get('backend_build')}")
    if not outcome.sanitized_html:
        print("FAILED: sanitized HTML is empty. blocking_reasons:")
        for reason in outcome.blocking_reasons:
            print(f"    - {reason}")
        return 2

    # 4. Write sanitized HTML.
    html_path = "/tmp/topos_llm_demo.html"
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(outcome.sanitized_html)
    print(f"== wrote HTML -> {html_path} ({len(outcome.sanitized_html)} chars) ==")

    # 5. Render one frame to PNG via the V2 path.
    scene = TeachingSceneDocument.create_default(name="LLM Demo")
    scene = replace(scene, fps_num=FPS, fps_den=1)
    layer = build_html_layer(
        layer_id="demo", sanitized_html=outcome.sanitized_html,
        authored_source=outcome.sanitized_html,
        viewport_width=W, viewport_height=H,
        fps_num=FPS, fps_den=1, duration_frames=DUR_S * FPS,
        csp="default-src 'none'; style-src 'unsafe-inline'",
    )
    scene = replace(scene, html_layer=layer)
    service = router.get_shared_html_service()
    renderer = HtmlSceneRendererV2(service)
    fc = FrameContext(frame=FPS * 2, fps_num=FPS, fps_den=1,            # 2s in
                      canvas=CanvasSpace.from_dimensions(W, H),
                      surface=RenderSurface(pixel_width=W, pixel_height=H, dpr=1.0,
                                            alpha_mode=AlphaMode.STRAIGHT))
    session = renderer.prepare(scene, fc)
    try:
        result = renderer.render(session, fc)
    finally:
        renderer.close(session)

    png_path = "/tmp/topos_llm_demo_frame.png"
    import io
    from PIL import Image
    img = Image.frombytes("RGBA", (result.frame.width, result.frame.height),
                          result.frame.data)
    img.save(png_path)
    print(f"== wrote PNG  -> {png_path} ({result.frame.width}x{result.frame.height}, "
          f"frame_hash={result.evidence.frame_hash[:12]}…) ==")
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
