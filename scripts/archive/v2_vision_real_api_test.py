#!/usr/bin/env python3
"""V2 多模态视觉输入 — 真实 API 测试。

验证发图提问端到端打通：构造一张内容明确（红圆 + 蓝矩形 + 文字）的图片，
经 image_attachment 编码为多模态 content，发送给配置好的视觉模型，检查它
能否准确描述图片内容。

用法：.venv/bin/python scripts/v2_vision_real_api_test.py [profile_id ...]
不传 profile_id 则测试所有 language/vision 能力的 profile。
"""
from __future__ import annotations

import io
import os
import sys
import time

# 确保可 import src
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
os.chdir(_REPO)


def _make_test_image_png() -> bytes:
    """生成一张内容明确的测试图：白底 + 红色大圆 + 蓝色矩形 + 文字 'TOPOS 2026'。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 400), "white")
    draw = ImageDraw.Draw(img)
    # 红色大圆
    draw.ellipse((80, 80, 280, 280), fill="red", outline="darkred", width=4)
    # 蓝色矩形
    draw.rectangle((360, 100, 580, 260), fill="blue", outline="navy", width=4)
    # 文字（尝试系统字体，失败回退默认）
    try:
        font = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf", 48
        )
    except Exception:
        font = ImageFont.load_default()
    draw.text((150, 310), "TOPOS 2026", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _banner(text: str) -> None:
    print("\n" + "=" * 64 + f"\n {text}\n" + "=" * 64, flush=True)


def _record(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""), flush=True)


def _encode_png_to_attachment(png_bytes: bytes):
    """PNG bytes → ImageAttachment（走真实编码路径，但跳过 QImage，直接 bytes→data URL）。"""
    import base64
    from src.features.edit.ai_editing.image_attachment import (
        ImageAttachment, encode_bytes_to_data_url,
    )
    url = encode_bytes_to_data_url(png_bytes, mime="image/png")
    return ImageAttachment(data_url=url, mime="image/png", width=640, height=400, source="test")


def _send_vision(profile, credential_store, prompt: str, attachment) -> tuple[str, float]:
    """发送一条多模态消息，返回 (回复文本, 耗时秒)。"""
    from src.features.edit.ai_editing.llm_client import make_client
    from src.features.edit.ai_editing.image_attachment import message_content

    client = make_client(profile, credential_store)
    content = message_content(prompt, [attachment])
    messages = [{"role": "user", "content": content}]
    t0 = time.perf_counter()
    reply = client.complete(messages, temperature=0.3, max_tokens=512)
    return (reply or "").strip(), time.perf_counter() - t0


def main() -> int:
    _banner("V2 多模态视觉输入 — 真实 API 测试")

    from src.features.edit.ai_editing.provider_profiles import ProfileRegistry
    from src.features.edit.ai_editing.credential_store import HybridCredentialStore

    store = HybridCredentialStore(prefer="file")
    registry = ProfileRegistry()
    if not registry.load_from_user_file():
        print("无法加载 ~/.topos/ai_profiles.json，终止。")
        return 2

    # 选目标 profile：命令行参数 或 所有 language 能力的 profile
    requested = [a for a in sys.argv[1:] if not a.startswith("-")]
    profiles = []
    for pid, p in registry._profiles.items():
        if requested:
            if pid in requested or p.model_id in requested or p.display_name in requested:
                profiles.append(p)
        elif "language" in (p.capabilities or ()):
            profiles.append(p)

    if not profiles:
        print("没有可测试的 profile。")
        print(f"  可用: {[(p.profile_id, p.display_name, p.model_id) for p in registry._profiles.values()]}")
        return 2

    print(f"将测试 {len(profiles)} 个 profile:")
    for p in profiles:
        print(f"  · {p.display_name} ({p.model_id}) [{p.provider_kind}] caps={p.capabilities}")

    png = _make_test_image_png()
    print(f"\n测试图已生成（PNG, {len(png)} bytes）：白底 + 红圆 + 蓝矩形 + 'TOPOS 2026' 文字")

    attachment = _encode_png_to_attachment(png)

    prompt = (
        "请用中文简要描述这张图片的内容：有哪些形状、颜色、文字？"
        "请准确说出文字内容。"
    )

    overall_ok = True
    for profile in profiles:
        _banner(f"测试：{profile.display_name} ({profile.model_id})")
        try:
            reply, elapsed = _send_vision(profile, store, prompt, attachment)
        except Exception as exc:
            import traceback
            print(f"  ✗ 调用失败：{type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            overall_ok = False
            continue

        print(f"  耗时: {elapsed:.1f}s", flush=True)
        print(f"  回复:\n    {reply[:600]}", flush=True)

        low = reply.lower()
        # 关键内容核验：模型应提到红/圆、蓝/矩形、TOPOS 文字
        has_red = ("红" in reply) or ("red" in low)
        has_blue = ("蓝" in reply) or ("blue" in low)
        has_circle = ("圆" in reply) or ("circle" in low)
        has_rect = ("矩形" in reply or "方形" in reply or "长方" in reply) or ("rect" in low or "square" in low)
        has_text = ("topos" in low) or ("2026" in reply)
        hits = sum([has_red, has_blue, has_circle, has_rect, has_text])
        _record("识别红色", has_red)
        _record("识别蓝色", has_blue)
        _record("识别圆形", has_circle)
        _record("识别矩形", has_rect)
        _record("识别文字 TOPOS/2026", has_text)
        _record(f"总命中 {hits}/5 关键内容", hits >= 3)
        if hits < 3:
            overall_ok = False

    _banner("总结")
    print("结果：" + ("✓ 至少能识别关键内容" if overall_ok else "✗ 部分模型未能准确描述"))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
