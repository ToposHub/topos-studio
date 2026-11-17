"""F8：provider 执行边界——owner/优先级/冲突解释/缓存签名/回退类别 + Inspector。"""
from __future__ import annotations

import threading

import pytest

from src.shared.plugins.manifest import FormatProviderDeclaration
from src.shared.plugins.providers import (
    ProviderError, ProviderInspector, ProviderRegistry,
)


def _decl(provider_id="p1", *, priority=100, formats=("mov",), provides=("probe",),
          cache_signature="") -> FormatProviderDeclaration:
    return FormatProviderDeclaration(
        provider_id=provider_id, priority=priority, formats=formats,
        provides=provides, cache_signature=cache_signature, version="1.0.0")


# ---------------------------------------------------------------------------
# owner / 优先级 / 冲突解释
# ---------------------------------------------------------------------------

def test_owner_registration_and_unregistration():
    registry = ProviderRegistry()
    registry.register("files.video_pro", _decl("vp", priority=10),
                      lambda kind, target: "ok")
    registry.register("files.other", _decl("ot", priority=200),
                      lambda kind, target: "ok")
    assert registry.owners() == ("files.other", "files.video_pro")
    assert registry.unregister_owner("files.other") == 1   # 停用摘除
    assert registry.owners() == ("files.video_pro",)
    assert registry.unregister_owner("files.other") == 0   # 幂等
    assert registry.resolve("probe", "x.mov").provider_id == "vp"


def test_priority_resolution_with_explicit_conflict_explanation():
    registry = ProviderRegistry()
    registry.register("files.a", _decl("a-probe", priority=100),
                      lambda kind, target: "A")
    registry.register("files.b", _decl("b-probe", priority=10),
                      lambda kind, target: "B")
    resolution = registry.resolve("probe", "clip.mov")
    assert resolution.provider_id == "b-probe"        # 小值优先
    assert resolution.owner == "files.b"
    conflicts = resolution.conflicts
    assert len(conflicts) == 1 and conflicts[0].provider_id == "a-probe"
    assert "优先级" in conflicts[0].reason and "b-probe" in conflicts[0].reason


def test_same_priority_first_registered_wins_deterministically():
    registry = ProviderRegistry()
    registry.register("files.a", _decl("first", priority=50),
                      lambda kind, target: "A")
    registry.register("files.b", _decl("second", priority=50),
                      lambda kind, target: "B")
    assert registry.resolve("probe", "v.mov").provider_id == "first"


def test_format_and_magic_matching_with_declared_only_fallback():
    registry = ProviderRegistry()
    registry.register("files.any", _decl("any", priority=10, formats=(),
                                         provides=("metadata",)),
                      lambda kind, target: "any")
    # 空 formats/magic = 声明型兜底：未匹配到具体格式时参与
    assert registry.resolve("metadata", "zzz.unknown").provider_id == "any"
    # 具体格式声明者优先于兜底
    registry.register("files.fmt", _decl("fmt", priority=5, formats=("mov",),
                                         provides=("metadata",)),
                      lambda kind, target: "fmt")
    assert registry.resolve("metadata", "a.mov").provider_id == "fmt"
    assert registry.resolve("metadata", "b.other").provider_id == "any"


def test_unknown_provides_kind_is_caller_error_not_absence():
    registry = ProviderRegistry()
    with pytest.raises(ValueError):
        registry.resolve("bogus", "mov")
    with pytest.raises(ValueError):
        registry.run("bogus", "mov")


# ---------------------------------------------------------------------------
# 缓存签名：revision + 候选签名
# ---------------------------------------------------------------------------

def test_cache_key_invalidates_on_registry_change_and_signature():
    registry = ProviderRegistry()
    registry.register("files.a", _decl("a", priority=10, cache_signature="s1"),
                      lambda kind, target: "A")
    key1 = registry.resolve("probe", "x.mov").cache_key
    key1_again = registry.resolve("probe", "x.mov").cache_key
    assert key1 == key1_again                      # 稳定（同 revision/签名）
    # 新候选注册 → revision 变 → 缓存键失效
    registry.register("files.b", _decl("b", priority=1),
                      lambda kind, target: "B")
    key2 = registry.resolve("probe", "x.mov").cache_key
    assert key2 != key1 and key2.startswith("r2:")
    # 同 provider 换代（签名变化）→ 失效
    registry.register("files.b", _decl("b", priority=1, cache_signature="v2"),
                      lambda kind, target: "B2")
    key3 = registry.resolve("probe", "x.mov").cache_key
    assert key3 != key2
    # owner 摘除 → 失效
    registry.unregister_owner("files.a")
    assert registry.resolve("probe", "x.mov").cache_key != key3


# ---------------------------------------------------------------------------
# 执行：只有 fallback 类错误回退
# ---------------------------------------------------------------------------

def test_fallback_class_errors_walk_the_chain():
    registry = ProviderRegistry()
    calls: list[str] = []

    def broken(kind, target):
        calls.append("high")
        raise ProviderError("fallback", "高级 provider 失败")

    def healthy(kind, target):
        calls.append("low")
        return "low-result"

    registry.register("files.a", _decl("high", priority=1), broken)
    registry.register("files.b", _decl("low", priority=100), healthy)
    result, resolution = registry.run("probe", "x.mov")
    assert result == "low-result"
    assert calls == ["high", "low"]                 # 按序回退成功
    assert resolution.conflicts[0].provider_id == "low"


def test_unsupported_is_terminal_no_silent_detour():
    """不支持类错误终态：绝不静默绕道下一个 provider（失败策略原文）。"""
    registry = ProviderRegistry()

    def unsupported(kind, target):
        raise ProviderError("unsupported", "权限/格式不支持")

    def healthy(kind, target):
        raise AssertionError("unsupported 不许绕道到这里")

    registry.register("files.a", _decl("high", priority=1), unsupported)
    registry.register("files.b", _decl("low", priority=100), healthy)
    with pytest.raises(ProviderError) as exc:
        registry.run("probe", "x.mov")
    assert exc.value.kind == "unsupported"


def test_transient_retries_once_same_provider_then_falls_back():
    registry = ProviderRegistry()
    attempts = {"n": 0}

    def flaky(kind, target):
        attempts["n"] += 1
        raise ProviderError("transient", "抖动")

    def healthy(kind, target):
        return "recovered"

    registry.register("files.a", _decl("flaky", priority=1), flaky)
    registry.register("files.b", _decl("low", priority=100), healthy)
    result, _ = registry.run("probe", "x.mov")
    assert attempts["n"] == 2 and result == "recovered"   # 同 provider 两次后回退


def test_transient_recovers_within_same_provider():
    registry = ProviderRegistry()
    attempts = {"n": 0}

    def flaky_once(kind, target):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderError("transient")
        return "self-recovered"

    registry.register("files.a", _decl("a", priority=1), flaky_once)
    result, _ = registry.run("probe", "x.mov")
    assert result == "self-recovered" and attempts["n"] == 2


def test_cancelled_never_falls_back():
    registry = ProviderRegistry()

    def cancelled(kind, target):
        raise ProviderError("cancelled", "用户取消")

    def other(kind, target):
        raise AssertionError("取消不得回退到其他 provider")

    registry.register("files.a", _decl("a", priority=1), cancelled)
    registry.register("files.b", _decl("b", priority=2), other)
    with pytest.raises(ProviderError) as exc:
        registry.run("probe", "x.mov")
    assert exc.value.kind == "cancelled"


def test_no_candidates_raises_unsupported():
    registry = ProviderRegistry()
    with pytest.raises(ProviderError) as exc:
        registry.run("export", "zzz")
    assert exc.value.kind == "unsupported"


# ---------------------------------------------------------------------------
# Inspector：异步装配 / 世代失效 / 有界重试
# ---------------------------------------------------------------------------

def test_inspector_assembles_availability_async():
    registry = ProviderRegistry()
    registry.register("files.a", _decl("a", priority=10),
                      lambda kind, target: "ok")
    registry.register("files.b", _decl("b", priority=20, provides=("metadata",)),
                      lambda kind, target: "ok")

    def broken(kind, target):
        raise ProviderError("unsupported", "不处理")

    registry.register("files.c", _decl("c", priority=30), broken)
    done = threading.Event()
    results: list[tuple[int, list[dict]]] = []

    def spawn_sync(work):   # 同步执行器：测试确定性
        work()

    inspector = ProviderInspector(registry, executor_provider=spawn_sync)
    generation = inspector.inspect("clip.mov", on_result=lambda g, e: (
        results.append((g, e)), done.set()))
    assert done.wait(5)
    generation_out, entries = results[0]
    assert generation_out == generation
    by_id = {entry["provider_id"]: entry for entry in entries}
    assert by_id["a"]["available"] is True
    assert by_id["c"]["available"] is False
    assert by_id["c"]["error_kind"] == "unsupported"


def test_inspector_stale_generation_results_are_dropped():
    registry = ProviderRegistry()
    gate = threading.Event()
    delivered: list[int] = []

    def spawn_gated_first(work):
        def release_then_work():
            gate.wait(5)
            work()
        threading.Thread(target=release_then_work, daemon=True).start()

    calls = {"n": 0}

    def spawn(work):
        calls["n"] += 1
        if calls["n"] == 1:
            spawn_gated_first(work)
        else:
            work()

    inspector = ProviderInspector(registry, executor_provider=spawn)
    stale_gen = inspector.inspect("a.mov", on_result=lambda g, e: delivered.append(g))
    fresh_gen = inspector.inspect("b.mov", on_result=lambda g, e: delivered.append(g))
    assert fresh_gen != stale_gen
    gate.set()                                     # 旧代现在才完成
    for _ in range(50):
        if len(delivered) >= 1:
            break
        threading.Event().wait(0.02)
    assert delivered == [fresh_gen]                # 旧代被世代失效丢弃


def test_inspector_transient_retried_once_per_provider():
    registry = ProviderRegistry()
    attempts = {"n": 0}

    def flaky(kind, target):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderError("transient")
        return "ok"

    registry.register("files.a", _decl("a"), flaky)
    results: list[list[dict]] = []
    done = threading.Event()
    inspector = ProviderInspector(registry, executor_provider=lambda work: work())
    inspector.inspect("x.mov", on_result=lambda g, e: (
        results.append(e), done.set()))
    assert done.wait(5)
    assert attempts["n"] == 2                      # 重试一次后可用
    assert results[0][0]["available"] is True


# ---------------------------------------------------------------------------
# 宿主会话接线：停用/退役摘除 provider
# ---------------------------------------------------------------------------

def test_host_session_retirement_removes_providers():
    from src.shared.plugins.host_session import PluginHostSession

    session = PluginHostSession()
    session.providers.register("files.a", _decl("a", priority=1),
                               lambda kind, target: "A")
    session.activate_plugin("files.a", lambda c: c.register("m", "x", lambda: None))
    report = session.retire_plugin("files.a")
    assert report["providers_removed"] == 1
    assert session.providers.owners() == ()
    # release_contributions（restart-bound 停用）同样摘除
    session.providers.register("files.b", _decl("b", priority=1),
                               lambda kind, target: "B")
    report2 = session.release_contributions("files.b")
    assert report2["providers_removed"] == 1
