#!/usr/bin/env python3
"""Real-API smoke test for every AI Assistant mode (plan §A1.5 §3).

Beyond plain chat, this drives the real generation pipelines that the editor
uses for each composer mode:

- **Motion Graphic** — language model + HTML system prompt → animated HTML doc
- **Image** — Volcengine Seedream → image asset
- **Voice / TTS** — Volcengine Doubao Seed-TTS → audio asset
- **Capability router** — every mode resolves to the right capability/provider,
  and unavailable modes fail closed (video/music with no provider)

Loads the user's real ``~/.topos/ai_profiles.json`` (file + keyring hybrid
store).  Run::

    PYTHONPATH=$(pwd) .venv/bin/python -u scripts/v2_real_multimode_smoke.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid

from src.features.edit.ai_editing.assistant_capability_router import (
    AssistantCapabilityRouter,
)
from src.features.edit.ai_editing.credential_store import HybridCredentialStore
from src.features.edit.ai_editing.generation_models import (
    GenerationBudget,
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)
from src.features.edit.ai_editing.llm_client import make_client
from src.features.edit.ai_editing.provider_profiles import ProfileRegistry
from src.features.edit.ai_editing.volcengine_generation_provider import (
    make_volcengine_generation_provider,
)

results: list[tuple[str, bool, str]] = []


def _banner(text: str) -> None:
    print(f"\n{'=' * 72}\n  {text}\n{'=' * 72}", flush=True)


def _record(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# ---------------------------------------------------------------------- #
# Mode 1: Motion Graphic HTML generation
# ---------------------------------------------------------------------- #


def test_motion_graphic(registry: ProfileRegistry, store: HybridCredentialStore) -> None:
    _banner("模式 1 · Motion Graphic Clip（语言模型 → HTML 动效）")
    from src.features.teaching_animation.ai.html_generation_protocol import (
        HtmlGenerationConstraints,
    )
    from src.features.teaching_animation.ai.html_llm_generator import (
        LlmGenerationRequest,
        generate_raw_html,
    )

    prof = registry.active_profile
    client = make_client(prof, store)
    print(f"  使用模型: {prof.display_name} ({prof.model_id})", flush=True)

    # 三种不同风格的动效，模拟真实使用场景
    prompts = [
        (
            "科技标题卡",
            "Design a 4-second dark tech-style title card for a video editor "
            "called 'Topos Color'. Big bold title 'TOPOS COLOR' with a gradient "
            "text fill, a subtitle 'Professional Color Grading', and three small "
            "feature pills ('HDR', 'GLSL', 'Real-time') that fade-in with a "
            "stagger delay. Dark background with a subtle animated glow.",
        ),
        (
            "数据可视化",
            "Design a 4-second animated stat card. Show '渲染速度' as a big "
            "number counting up from 0 to 60 FPS, with a thin progress bar "
            "filling from left to right underneath. Modern dark theme, accent "
            "color cyan. The number and bar animate in sync via @keyframes.",
        ),
        (
            "极简入场",
            "Design a minimal 3-second intro: a single word '色彩' that scales "
            "up from 50% to 100% with an ease-out curve, while a thin underline "
            "grows from center. Clean white background, black text, no clutter.",
        ),
    ]

    for label, prompt in prompts:
        print(f"\n  --- [{label}]", flush=True)
        t0 = time.perf_counter()
        try:
            raw_html = generate_raw_html(
                client,
                LlmGenerationRequest(user_prompt=prompt, temperature=0.4, max_tokens=6000),
                constraints=HtmlGenerationConstraints(
                    viewport_width=1920, viewport_height=1080,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _record(f"MG·{label}", False, f"{type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - t0
        html = raw_html.strip()
        checks = {
            "非空": bool(html),
            "DOCTYPE": html.lower().startswith("<!doctype html"),
            "有 <style>": "<style" in html.lower(),
            "@keyframes 动画": "@keyframes" in html,
            "无 <script>": "<script" not in html.lower(),
            "无 markdown 围栏": not html.startswith("```"),
        }
        passed = all(checks.values())
        detail = f"{len(html)} 字符, {elapsed:.1f}s | " + " ".join(
            f"{'✓' if v else '✗'}{k}" for k, v in checks.items()
        )
        print(f"      {detail}", flush=True)
        if passed:
            preview = html.replace("\n", " ")[:120]
            print(f"      预览: {preview!r}", flush=True)
        _record(f"MG·{label}", passed, f"{len(html)}字符 {elapsed:.1f}s")


# ---------------------------------------------------------------------- #
# Mode 2: Image generation (Seedream)
# ---------------------------------------------------------------------- #


def test_image_generation(
    registry: ProfileRegistry, store: HybridCredentialStore
) -> None:
    _banner("模式 2 · 图片生成（火山方舟 Seedream）")
    seedream = next(
        (p for p in registry._profiles.values()
         if p.provider_kind == "volcengine_seedream"),
        None,
    )
    if seedream is None:
        _record("Image", False, "未找到 Seedream profile")
        return
    provider = make_volcengine_generation_provider(seedream, store)
    if provider is None:
        _record("Image", False, "无法构造 Seedream provider（缺凭证？）")
        return
    print(f"  模型: {seedream.display_name} ({seedream.model_id})", flush=True)

    prompts = [
        ("电影感风景", "A cinematic mountain landscape at golden hour, dramatic "
                      "clouds, warm sunlight hitting the peaks, ultra detailed, "
                      "professional photography"),
        ("抽象色彩", "Abstract color composition, flowing gradient waves of "
                     "teal magenta and gold, modern digital art, smooth blending"),
    ]
    for label, prompt in prompts:
        print(f"\n  --- [{label}]", flush=True)
        req = GenerationRequest(
            request_id=f"smoke-img-{uuid.uuid4().hex[:8]}",
            modality=GenerationModality.IMAGE,
            prompt=prompt,
            consent=GenerationConsent(confirmed=True, provider_id=seedream.profile_id),
            budget=GenerationBudget(max_candidates=1),
        )
        t0 = time.perf_counter()
        try:
            result = provider.generate(req)
        except Exception as exc:  # noqa: BLE001
            _record(f"Image·{label}", False, f"{type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - t0
        if result.error_code:
            _record(f"Image·{label}", False,
                    f"err={result.error_code}: {result.error_message}")
            continue
        n = len(result.candidates)
        handle = result.candidates[0].preview_handle if n else ""
        _record(f"Image·{label}", n > 0,
                f"{n} 张, handle={str(handle)[:60]}, {elapsed:.1f}s")


# ---------------------------------------------------------------------- #
# Mode 3: Voice / TTS (Doubao Seed-TTS)
# ---------------------------------------------------------------------- #


def test_tts(registry: ProfileRegistry, store: HybridCredentialStore) -> None:
    _banner("模式 3 · 语音合成（火山方舟 豆包 Seed-TTS 2.0）")
    tts_prof = next(
        (p for p in registry._profiles.values()
         if p.provider_kind == "volcengine_plan_tts"),
        None,
    )
    if tts_prof is None:
        _record("TTS", False, "未找到 TTS profile")
        return
    provider = make_volcengine_generation_provider(tts_prof, store)
    if provider is None:
        _record("TTS", False, "无法构造 TTS provider（缺凭证？）")
        return
    print(f"  模型: {tts_prof.display_name} ({tts_prof.model_id})", flush=True)

    texts = [
        ("中文播报", "欢迎来到 Topos Color 专业色彩编辑器。在这里，你可以用 GPU 加速的着色器，实时处理四K视频。"),
        ("中英混合", "这是一段测试语音。The quick brown fox jumps over the lazy dog. 谢谢收听。"),
    ]
    for label, text in texts:
        print(f"\n  --- [{label}] ({len(text)} 字)", flush=True)
        req = GenerationRequest(
            request_id=f"smoke-tts-{uuid.uuid4().hex[:8]}",
            modality=GenerationModality.VOICE,
            prompt=text,
            consent=GenerationConsent(confirmed=True, provider_id=tts_prof.profile_id),
            budget=GenerationBudget(max_candidates=1),
        )
        t0 = time.perf_counter()
        try:
            result = provider.generate(req)
        except Exception as exc:  # noqa: BLE001
            _record(f"TTS·{label}", False, f"{type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - t0
        if result.error_code:
            _record(f"TTS·{label}", False,
                    f"err={result.error_code}: {result.error_message}")
            continue
        n = len(result.candidates)
        asset = result.candidates[0] if n else None
        handle = asset.preview_handle if asset else ""
        # 音频缓存到本地文件，检查是否真实存在
        file_ok = bool(handle and os.path.exists(str(handle)))
        size = os.path.getsize(str(handle)) if file_ok else 0
        dur = asset.duration_seconds if asset and asset.duration_seconds else None
        _record(f"TTS·{label}", n > 0 and file_ok and size > 0,
                f"{n} 段, {size} bytes, dur={dur}, {elapsed:.1f}s")


# ---------------------------------------------------------------------- #
# Mode 4: Capability router — every mode resolves correctly
# ---------------------------------------------------------------------- #


def test_capability_router(registry: ProfileRegistry) -> None:
    _banner("模式 4 · 能力路由解析（fail-closed 验证）")
    router = AssistantCapabilityRouter(registry)

    expectations = {
        # mode: (should_block, description)
        "chat": (False, "语言模型可用 → 正常"),
        "motion_graphic": (True, "教学动画模块未装配 → fail closed（standalone 期望）"),
        "image": (True, "无生成 Provider 注册表 → fail closed（standalone 期望）"),
        "video": (True, "无 video provider → fail closed"),
        "music": (True, "无 music provider → fail closed"),
        "tts": (True, "tts profile 无 language capability 匹配 → fail closed（standalone）"),
        "vision": (True, "Seedream 是图片生成 ≠ 视觉理解 → 正确拆分"),
    }
    for mode, (expect_block, desc) in expectations.items():
        res = router.resolve(mode)
        blocked = res.is_blocking
        ok = blocked == expect_block
        flag = "BLOCKED" if blocked else "OK"
        _record(
            f"Router·{mode}", ok,
            f"{flag} avail={res.availability} | {desc}",
        )


def main() -> int:
    _banner("V2 多模式真实 API 冒烟测试")

    store = HybridCredentialStore()
    registry = ProfileRegistry()
    if not registry.load_from_user_file():
        print("无法加载 ~/.topos/ai_profiles.json，终止。")
        return 2

    n_lang = sum(
        1 for p in registry._profiles.values()
        if "language" in (p.capabilities or ())
    )
    n_media = sum(
        1 for p in registry._profiles.values()
        if p.provider_kind.startswith("volcengine_seedream")
        or p.provider_kind.startswith("volcengine_plan_tts")
    )
    print(f"已加载 {len(registry._profiles)} 个 Profile "
          f"({n_lang} 语言, {n_media} 媒体)", flush=True)
    print(f"激活: {registry.active_profile.display_name}", flush=True)

    # 确保使用混合凭证存储（文件 + 钥匙串）
    test_capability_router(registry)
    test_motion_graphic(registry, store)
    test_image_generation(registry, store)
    test_tts(registry, store)

    # ---- summary ----
    _banner("汇总")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for label, ok, detail in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {label:24s} {detail}")
    print(f"\n  {passed}/{total} 通过", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
