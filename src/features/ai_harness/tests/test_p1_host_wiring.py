"""P1 host 装配测试：SkillRegistry/Router/Compiler/Adapter/Help/Catalog 装配契约。

> 验证 §10.3 P1 在组合根（_setup_ai_editing）的装配契约：
> - build_p1_dependencies 装配全部 P1 依赖（router/compiler/adapter/help/catalog/registry）
> - router 的 skill_resolver 绑到共享 registry（5 个 skill 都 enabled）
> - adapter 与传入 runtime 是同一实例（单一 runtime 红线）
> - editorial provider 注入 adapter（None 时降级不崩）

controller 的依赖注入契约（需 qapp）放 tests/ui/test_p1_controller_deps.py。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_router import InvocationRouter
from src.features.ai_harness.skill_compiler import SkillCompiler
from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
from src.features.ai_harness.help_assistant import HelpAssistant
from src.features.ai_harness.skill_catalog import SkillCatalogView

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 装配辅助：build_p1_dependencies（_setup_ai_editing 调用的工厂）
# ---------------------------------------------------------------------- #


def test_build_p1_dependencies_returns_all_components():
    """build_p1_dependencies 装配 P1 全部依赖（router/compiler/adapter/help/catalog/registry）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    runtime = HarnessRuntime()
    service = MagicMock()
    deps = build_p1_dependencies(service=service, runtime=runtime)

    assert "router" in deps
    assert "compiler" in deps
    assert "adapter" in deps
    assert "help_assistant" in deps
    assert "catalog" in deps
    assert "skill_registry" in deps
    assert isinstance(deps["router"], InvocationRouter)
    assert isinstance(deps["compiler"], SkillCompiler)
    assert isinstance(deps["adapter"], LegacyHarnessAdapter)
    assert isinstance(deps["help_assistant"], HelpAssistant)
    assert isinstance(deps["catalog"], SkillCatalogView)


def test_build_p1_dependencies_shares_runtime_with_adapter():
    """adapter 与传入的 runtime 是同一实例（单一 HarnessRuntime 红线）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    runtime = HarnessRuntime()
    service = MagicMock()
    deps = build_p1_dependencies(service=service, runtime=runtime)
    assert deps["adapter"]._runtime is runtime


def test_build_p1_dependencies_registers_p2_editorial_skills():
    """registry 含 auto_caption + subtitle_localization（register_p2_skills）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    deps = build_p1_dependencies(service=MagicMock(), runtime=HarnessRuntime())
    reg = deps["skill_registry"]
    assert reg.is_enabled("topos.auto_caption"), "auto_caption 应注册并 enabled"
    assert reg.is_enabled("topos.subtitle_localization"), "subtitle_localization 应注册并 enabled"


def test_build_p1_dependencies_router_skill_resolver_bound_to_registry():
    """router 的 skill_resolver 绑到共享 registry（is_enabled 一致）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    deps = build_p1_dependencies(service=MagicMock(), runtime=HarnessRuntime())
    reg = deps["skill_registry"]
    router = deps["router"]
    # 路由正例：真实注册且启用的 skill 能路由出 skill_id
    result = router.route("自动字幕")
    assert result.skill_id == "topos.auto_caption"
    assert result.matched is True
    # 绑定契约直接断言（AUD-22615）：漏传 skill_resolver 时未注册 skill 不会被
    # 拦截，本测试必须失败——所以这里显式检查 router 持有 resolver 且其判定
    # 与 registry 的启用状态一致（未注册 → False）。
    resolver = getattr(router, "_skill_resolver", None)
    assert callable(resolver), "router 必须绑定 skill_resolver（漏传 = 契约失守）"
    for skill_id in ("topos.auto_caption", "topos.subtitle_localization"):
        assert resolver(skill_id) is True, f"已注册 skill 应被 resolver 放行: {skill_id}"
        assert reg.is_enabled(skill_id), "与 registry 启用状态一致"
    assert resolver("topos.not_registered_anywhere") is False, \
        "未注册的 skill 必须被 resolver 拦截"


def test_router_intercepts_skill_when_resolver_denies():
    """负向契约（AUD-22615）：resolver 拒绝时 route 不得产出可执行 skill 调用。"""
    router = InvocationRouter(help_search=None, skill_resolver=lambda skill_id: False)
    result = router.route("自动字幕")
    assert result.matched is False
    assert result.skill_id == "", "被 resolver 拒绝后 skill_id 必须清空"
    assert "未安装或未启用" in result.clarify_question


def test_build_p1_dependencies_editorial_provider_optional():
    """editorial provider 默认 None（editorial 路径降级 No evidence，不崩）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    deps = build_p1_dependencies(service=MagicMock(), runtime=HarnessRuntime())
    # adapter 的 _editorial_provider 为 None（未注入）—— editorial 路径会降级
    assert deps["adapter"]._editorial_provider is None


def test_localization_media_provider_only_accepts_imported_generated_asset():
    """A GenerationBin id cannot become a timeline asset before import."""
    from src.app.controllers.ai_editing_mixin import make_localization_media_provider

    class Bridge:
        def get_ai_generated_media_asset_id(self, generated_asset_id: str) -> str:
            return "media-tts-1" if generated_asset_id == "generated-tts-1" else ""

    resolver = make_localization_media_provider(Bridge())
    invocation = type("Invocation", (), {
        "inputs": {
            "tts_generated_asset_id": "generated-tts-1",
            "tts_duration_frames": 120,
            "tts_start_frame": 24,
        },
    })()
    artifact = resolver(invocation, (), "en")
    assert artifact is not None
    assert artifact.media_asset_id == "media-tts-1"
    assert artifact.duration_frames == 120


def test_build_p1_dependencies_idempotent():
    """重复调用返回独立实例（不共享可变状态；registry 每次新建）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    rt = HarnessRuntime()
    svc = MagicMock()
    deps1 = build_p1_dependencies(service=svc, runtime=rt)
    deps2 = build_p1_dependencies(service=svc, runtime=rt)
    assert deps1["skill_registry"] is not deps2["skill_registry"]
    assert deps1["adapter"] is not deps2["adapter"]


# ---------------------------------------------------------------------- #
# P1.4：help_sources 接入（真实 HelpIndex 装配）
# ---------------------------------------------------------------------- #


class _StubHelpSources:
    """满足 HelpSources Protocol 的最小 stub（带诊断码）。"""

    def list_diagnostics(self):
        return [
            {"code": "TAEP_ASSET_OFFLINE", "label": "素材离线",
             "message_template": "素材 {path} 离线", "suggested_action": "relink"},
        ]

    def list_shortcuts(self):
        return None

    def list_capabilities(self):
        return None

    def list_features(self):
        return None

    def list_panels(self):
        return None


class _BadHelpSources:
    """list_diagnostics 抛异常的坏源（验证 build_help_assistant 降级）。"""

    def list_diagnostics(self):
        raise RuntimeError("boom")

    def list_shortcuts(self):
        return None

    def list_capabilities(self):
        return None

    def list_features(self):
        return None

    def list_panels(self):
        return None


def test_build_p1_dependencies_help_sources_activates_help_assistant():
    """传 help_sources → help_assistant.is_available 为 True（index 已装配）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    deps = build_p1_dependencies(
        service=MagicMock(), runtime=HarnessRuntime(),
        help_sources=_StubHelpSources(),
    )
    ha = deps["help_assistant"]
    assert ha.available, "传 help_sources 后 HelpAssistant 应可用（index 非空）"


def test_build_p1_dependencies_no_help_sources_degrades():
    """help_sources=None → help_assistant.available 为 False（降级不变）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    deps = build_p1_dependencies(
        service=MagicMock(), runtime=HarnessRuntime(),
        help_sources=None,
    )
    ha = deps["help_assistant"]
    assert not ha.available, "无 help_sources 时应降级为空 index"


def test_build_p1_dependencies_bad_help_sources_degrades_gracefully():
    """help_sources 抛异常 → build_help_assistant 内部降级，返回不可用 assistant（不崩）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies

    deps = build_p1_dependencies(
        service=MagicMock(), runtime=HarnessRuntime(),
        help_sources=_BadHelpSources(),
    )
    ha = deps["help_assistant"]
    # build_help_assistant 捕获异常 → 空 index → available False
    assert not ha.available, "坏源应降级为空 index（不崩）"
    # 其他依赖应正常装配（单点失败不阻断）
    assert "router" in deps and "compiler" in deps
