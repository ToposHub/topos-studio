#!/usr/bin/env python3
"""Real-API smoke test for the V2 ChatRunDriver (plan §A1 §4-5).

Loads the user's real ``~/.topos/ai_profiles.json`` and drives
``ChatRunDriver.run_chat`` through several conversation styles, profiles and
edge cases so we can verify the unified AgentRun pipeline works end-to-end
against a live model — not just fakes.

Run::

    .venv/bin/python scripts/v2_real_chat_smoke.py

Exit code 0 means every round reached the expected terminal state with a
non-empty reply and real usage tokens.  Failures are reported per-round.
"""

from __future__ import annotations

import sys
import time
import traceback

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.assistant_capability_router import (
    AssistantCapabilityRouter,
)
from src.features.edit.ai_editing.chat_run_driver import (
    ChatRunDriver,
    classify_chat_result,
)
from src.features.edit.ai_editing.credential_store import FileCredentialStore
from src.features.edit.ai_editing.llm_client import make_client
from src.features.edit.ai_editing.provider_profiles import ProfileRegistry

# Reasoning models (glm-5.2 / deepseek-v4) burn 700-1500 tokens in the
# thinking channel before emitting visible content. 1024 is too tight — the
# reasoning eats the whole budget and message.content comes back empty.
MAX_TOKENS = 4096


def _banner(text: str) -> None:
    print(f"\n{'=' * 72}\n  {text}\n{'=' * 72}")


def _build_driver(registry: ProfileRegistry, store: FileCredentialStore):
    orch = AgentOrchestrator()
    router = AssistantCapabilityRouter(registry)
    driver = ChatRunDriver(
        orch,
        router,
        client_factory=lambda profile: make_client(profile, store),
        profile_registry=registry,
    )
    return driver, orch


def _run_one(
    driver: ChatRunDriver,
    orch: AgentOrchestrator,
    *,
    label: str,
    user_text: str,
    messages: list[dict[str, str]] | None = None,
    mode: str = "chat",
    stream: bool = True,
    temperature: float = 0.6,
    expect_cancelled: bool = False,
) -> bool:
    """Run one chat round and print a verdict. Returns True on success."""

    deltas: list[str] = []
    print(f"\n--- [{label}] mode={mode} stream={stream} temp={temperature}")
    print(f"    用户: {user_text[:80]}")
    t0 = time.perf_counter()
    try:
        res = driver.run_chat(
            session_id="smoke",
            user_text=user_text,
            messages=messages or [{"role": "user", "content": user_text}],
            mode=mode,
            stream=stream,
            temperature=temperature,
            max_tokens=MAX_TOKENS,
            on_delta=deltas.append if stream else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    ✗ 异常: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False
    elapsed = time.perf_counter() - t0

    cat = classify_chat_result(res)
    run = orch.get_run(res.run_id)
    events = [e.kind for e in orch.get_events(res.run_id)]
    usage = res.usage

    print(f"    终态: {res.terminal_state}  分类: {cat}  耗时: {elapsed:.2f}s")
    if usage is not None:
        est = "估算" if usage.window_prompt_is_estimated else "实测"
        ap, ac = usage.actual_prompt, usage.actual_completion
        total = (ap or 0) + (ac or 0) if (ap is not None or ac is not None) else None
        print(
            f"    Token({est}): prompt={ap} "
            f"completion={ac} total={total} "
            f"window_limit={usage.window_limit}"
        )
    else:
        print("    Token: (无 usage)")
    print(f"    事件流: {' → '.join(events)}")
    reply_preview = (res.reply or "").strip().replace("\n", " ")[:140]
    print(f"    回复: {reply_preview!r}")
    if stream and deltas:
        print(f"    流式增量数: {len(deltas)}（首块: {deltas[0][:40]!r}）")

    # ---- verdict ----
    ok = True
    if expect_cancelled:
        if res.terminal_state != AgentRunState.CANCELLED:
            print("    ✗ 期望 CANCELLED，实际 " + res.terminal_state)
            ok = False
        else:
            print("    ✓ 取消路径正确（CANCELLED，非 SUCCEEDED）")
    else:
        if res.terminal_state != AgentRunState.SUCCEEDED:
            print("    ✗ 期望 SUCCEEDED，实际 " + res.terminal_state)
            ok = False
        if not (res.reply or "").strip():
            print("    ✗ 回复为空")
            ok = False
        if run is not None and run.state != res.terminal_state:
            print(f"    ✗ 编排器状态 {run.state} ≠ 结果 {res.terminal_state}")
            ok = False
        ap, ac = (usage.actual_prompt, usage.actual_completion) if usage else (None, None)
        total = (ap or 0) + (ac or 0) if (ap is not None or ac is not None) else None
        if (
            total is not None
            and total <= 0
            and not usage.window_prompt_is_estimated
        ):
            print("    ✗ 实测 token 为 0")
            ok = False
        if AgentEventKind.RUN_COMPLETED not in events and not expect_cancelled:
            # SUCCEEDED runs must end with RUN_COMPLETED
            if res.terminal_state == AgentRunState.SUCCEEDED:
                print("    ✗ 缺少 RUN_COMPLETED 事件")
                ok = False
    if ok:
        print("    ✓ 通过")
    return ok


def main() -> int:
    _banner("V2 ChatRunDriver 真实 API 冒烟测试")

    store = FileCredentialStore()
    registry = ProfileRegistry()
    if not registry.load_from_user_file():
        print("无法加载 ~/.topos/ai_profiles.json，终止。")
        return 2
    active = registry.active_profile
    print(f"激活 Profile: {active.display_name} ({active.provider_kind})")
    print(f"端点: {active.endpoint}  模型: {active.model_id}")

    results: list[tuple[str, bool]] = []

    # ================================================================== #
    # 第一组：同一个 active profile，多种对话风格
    # ================================================================== #
    _banner("第一组 · 多对话风格（glm-5.2）")
    driver, orch = _build_driver(registry, store)

    styles = [
        ("极简问候", "你好呀，简单介绍一下你自己。", False, 0.3),
        ("闲聊创意", "用一句话告诉我，为什么色彩校正对视频后期那么重要？", True, 0.8),
        ("技术深问", "ModernGL 里纹理对象和 framebuffer 对象是什么关系？请用两句话回答。", True, 0.4),
        ("英文问答", "In exactly one sentence, explain what tone mapping does in computer graphics.", False, 0.5),
        ("代码请求", "写三行 Python 代码，用 numpy 计算一个数组的平均值。只给代码，不要解释。", True, 0.2),
        ("长篇叙述", "我正在做一个 PyQt6 的图像编辑器，需要给用户解释什么是 LUT（颜色查找表）。请用通俗易懂的方式写一段 100 字以内的说明。", True, 0.7),
    ]
    for label, text, stream, temp in styles:
        ok = _run_one(
            driver, orch, label=label, user_text=text,
            stream=stream, temperature=temp,
        )
        results.append((f"风格·{label}", ok))

    # ================================================================== #
    # 第二组：多轮上下文（带历史消息）
    # ================================================================== #
    _banner("第二组 · 多轮上下文")
    driver2, orch2 = _build_driver(registry, store)
    history = [
        {"role": "user", "content": "我刚拍了一段日落视频，颜色偏暖。"},
        {"role": "assistant", "content": "可以试试色温调整，把色温往冷调拉一些。"},
    ]
    ok = _run_one(
        driver2, orch2,
        label="多轮追问",
        user_text="那具体应该调多少 K？给我一个大概的范围就行。",
        messages=history + [{"role": "user", "content": "那具体应该调多少 K？给我一个大概的范围就行。"}],
        stream=True, temperature=0.5,
    )
    results.append(("多轮上下文", ok))

    # ================================================================== #
    # 第三组：取消路径（飞行中取消 → CANCELLED）
    # ================================================================== #
    _banner("第三组 · 飞行中取消")

    class _CancelMidFlightClient:
        """Wraps the real client but cancels the orchestrator mid-stream."""

        def __init__(self, real_client, orchestrator):
            self._real = real_client
            self._orch = orchestrator
            self.last_usage = None

        def complete(self, messages, **kw):
            return self._real.complete(messages, **kw)

        def stream_complete(self, messages, **kw):
            on_delta = kw.get("on_delta")
            # fire one delta then cancel — simulates the user hitting stop
            if on_delta:
                on_delta("（用户即将取消……）")
            self._orch.request_cancel_for_session("smoke")
            # still return something to exercise the post-call re-check
            return self._real.complete(messages, **{k: v for k, v in kw.items() if k != "on_delta"})

    driver3, orch3 = _build_driver(registry, store)
    real_client = make_client(active, store)
    # patch the client_factory to wrap the real client
    driver3._client_factory = lambda profile: _CancelMidFlightClient(real_client, orch3)
    ok = _run_one(
        driver3, orch3,
        label="流式中途取消",
        user_text="写一篇 500 字关于色彩心理学的短文。",
        stream=True, temperature=0.6,
        expect_cancelled=True,
    )
    results.append(("取消路径", ok))

    # ================================================================== #
    # 第四组：切换到 DeepSeek profile（如果可用）
    # ================================================================== #
    deepseek_profiles = [
        p for p in registry._profiles.values()
        if "deepseek" in (p.provider_kind or "").lower()
        or "deepseek" in (p.display_name or "").lower()
    ]
    if deepseek_profiles:
        _banner("第四组 · 切换 Profile（DeepSeek）")
        ds = deepseek_profiles[0]
        print(f"切换到: {ds.display_name} ({ds.endpoint})")
        # 临时把 language capability 指向 deepseek
        registry.set_active_for_capability("language", ds.profile_id)
        driver4, orch4 = _build_driver(registry, store)
        ok = _run_one(
            driver4, orch4,
            label="DeepSeek 风格",
            user_text="用一句话解释什么是伽马校正（gamma correction）。",
            stream=True, temperature=0.4,
        )
        results.append(("Profile·DeepSeek", ok))
        # 还原
        registry.set_active_for_capability("language", active.profile_id)
    else:
        print("\n（跳过第四组：未找到 DeepSeek profile）")

    # ================================================================== #
    # 汇总
    # ================================================================== #
    _banner("汇总")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    for label, ok in results:
        print(f"  {'✓' if ok else '✗'}  {label}")
    print(f"\n  {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
