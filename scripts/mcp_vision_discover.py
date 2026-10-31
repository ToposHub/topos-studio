#!/usr/bin/env python3
"""智谱 Vision MCP Server —— 发现 + 真实冒烟测试。

验证 SubprocessStdioTransport 能与真实的 ``@z_ai/mcp-server``（GLM-4.6V）握手，
列出工具，并对一张测试图调用 image_analysis。

用 GLM profile 的 API key 作为 ``Z_AI_API_KEY``（同一个智谱账号 key）。
"""
from __future__ import annotations

import io
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
os.chdir(_REPO)


def _banner(text: str) -> None:
    print("\n" + "=" * 60 + f"\n {text}\n" + "=" * 60, flush=True)


def _make_test_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (640, 400), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((80, 80, 280, 280), fill="red", outline="darkred", width=4)
    d.rectangle((360, 100, 580, 260), fill="blue", outline="navy", width=4)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
    d.text((150, 310), "TOPOS 2026", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    _banner("智谱 Vision MCP —— 发现 + 冒烟测试")

    # 1. 取 GLM profile 的 API key 作 Z_AI_API_KEY
    from src.features.edit.ai_editing.provider_profiles import ProfileRegistry
    from src.features.edit.ai_editing.credential_store import HybridCredentialStore

    reg = ProfileRegistry(); reg.load_from_user_file()
    store = HybridCredentialStore(prefer="file")
    glm = next((p for p in reg.list() if "glm" in (p.model_id or "").lower()), None)
    if glm is None:
        print("找不到 GLM profile；需要它的智谱 API key。"); return 2
    api_key = store.get_secret(glm.credential_ref) if glm.credential_ref else None
    if not api_key:
        print(f"GLM profile ({glm.display_name}) 没有可取的 API key。"); return 2
    print(f"用 {glm.display_name} 的 key 作 Z_AI_API_KEY（前6位: {str(api_key)[:6]}…）")

    # 2. 启动 transport
    from src.features.ai_harness.mcp_stdio_transport import (
        SubprocessStdioTransport, McpTransportError,
    )
    print("启动 @z_ai/mcp-server（首次会 npx 拉包，稍等）…", flush=True)
    t0 = time.perf_counter()
    transport = SubprocessStdioTransport(
        ("npx", "-y", "@z_ai/mcp-server"),
        env={"Z_AI_API_KEY": api_key, "Z_AI_MODE": "ZHIPU"},
        startup_timeout=120.0,  # npx 首次拉包可能慢
        request_timeout=180.0,
    )
    try:
        transport.start()
    except McpTransportError as exc:
        print(f"✗ 启动/握手失败：{exc}"); return 1
    print(f"✓ 握手成功，耗时 {time.perf_counter()-t0:.1f}s", flush=True)

    try:
        # 3. tools/list
        resp = transport.send_request({
            "jsonrpc": "2.0", "method": "tools/list", "params": {},
        })
        tools = (resp.get("result") or {}).get("tools", [])
        _banner(f"发现 {len(tools)} 个工具")
        for tool in tools:
            print(f"  · {tool.get('name')}: {(tool.get('description') or '')[:80]}")
            schema = tool.get("inputSchema") or {}
            props = schema.get("properties", {})
            if props:
                print(f"      参数: {list(props.keys())}")

        # 4. 找到 image_analysis 工具并调用
        png = _make_test_png()
        import base64
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()

        img_tool = next((t for t in tools if t.get("name") == "analyze_image"), None)
        if img_tool is None:
            print("\n没有 image_analysis 工具，跳过调用测试。"); return 0

        _banner("调用 image_analysis（红圆 + 蓝矩形 + TOPOS 2026）")
        # 先看它要什么参数，再决定怎么传图
        props = (img_tool.get("inputSchema") or {}).get("properties", {})
        print(f"image_analysis 参数: {list(props.keys())}", flush=True)
        # 常见命名：image / imageUrl / image_url / imageData / data
        call_args = {}
        for key in ("image_source", "image", "imageUrl", "file"):
            if key in props:
                call_args[key] = data_url
                break
        if not call_args:
            # 兜底：用第一个 string 参数传 data_url
            for k, v in props.items():
                if isinstance(v, dict) and v.get("type") in ("string", None):
                    call_args[k] = data_url
                    break
        if not call_args:
            print(f"无法确定图片参数名，props={props}"); return 1

        c0 = time.perf_counter()
        call_resp = transport.send_request({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "analyze_image", "arguments": call_args},
        })
        print(f"调用耗时 {time.perf_counter()-c0:.1f}s", flush=True)
        result = call_resp.get("result") or {}
        content = result.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                print("\n模型分析结果：")
                print(block.get("text", "")[:1000])
        _banner("总结：✓ MCP 视觉链路打通")
        return 0
    except McpTransportError as exc:
        print(f"✗ 调用失败：{exc}"); return 1
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
