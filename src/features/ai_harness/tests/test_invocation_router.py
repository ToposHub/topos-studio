"""InvocationRouter 单元测试（plan §10.3 P1.1）。

覆盖 plan §10.3 表格的验收：'分析所有素材' / '给当前字幕配中文配音' /
'这个功能怎么用' 分别选择正确 Skill/Help，不误入粗剪 Planner。

红线（plan §1.3 / §10.3）：

- 用户文本不直接拼接为 tool 参数（to_invocation 的 user_text 永远为空）。
- 不确定时只问一个最小问题（不一次问一堆）。
- 不默认进粗剪 VariantPlanner（unknown 不 matched）。
- 外部输入不可信：含绝对路径 / secret 的文本被脱敏。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.invocation_models import (
    InvocationScope, PermissionLevel, RunPolicy,
)
from src.features.ai_harness.invocation_router import (
    IntentRule,
    InvocationRouter,
)


# ================================================================
# P1.1 核心验收：三个示例短语分别选对 Skill/Help
# ================================================================


class TestPlanAcceptancePhrases:
    """plan §10.3 P1.1 验收的三句话。"""

    def test_analyze_all_assets(self):
        rt = InvocationRouter()
        r = rt.route(
            "分析所有素材",
            scope=InvocationScope(project_id="p1", analyze_all_assets=True),
        )
        assert r.intent == "analyze"
        assert r.matched is True
        assert "全部素材" in r.scope_hint

    def test_chinese_dubbing(self):
        rt = InvocationRouter()
        r = rt.route("给当前字幕配中文配音")
        assert r.intent == "translate_dub"
        assert r.matched is True
        # 中文已被确定性抽到，无需再问
        assert r.extracted_inputs.get("target_language") == "中文"
        assert r.clarify_question == ""

    def test_how_to_use(self):
        rt = InvocationRouter()
        r = rt.route("这个功能怎么用")
        assert r.intent == "help"
        assert r.matched is True

    def test_subtitle_phrase_not_into_rough_cut(self):
        """关键反证：字幕请求不误入粗剪 Planner（plan §10.3 验收）。"""
        rt = InvocationRouter()
        r = rt.route("给选中片段生成字幕")
        assert r.intent == "subtitle"
        assert r.skill_id == "topos.auto_caption"
        assert r.intent != "rough_cut"

    def test_unknown_does_not_match(self):
        """unknown 不 matched，UI 决定（不默认进 VariantPlanner）。"""
        rt = InvocationRouter()
        r = rt.route("今天天气真好")
        assert r.intent == "unknown"
        assert r.matched is False
        assert r.clarify_question == ""


# ================================================================
# 各意图匹配
# ================================================================


class TestIntentMatching:
    @pytest.mark.parametrize("text,expected", [
        ("生成字幕", "subtitle"),
        ("上字幕", "subtitle"),
        ("自动字幕", "subtitle"),
        ("口播精剪", "rough_cut"),
        ("剪一版 30 秒的", "rough_cut"),
        ("30 秒产品广告", "product_ad"),
        ("带货视频", "product_ad"),
        ("配音", "translate_dub"),
        ("翻译配音", "translate_dub"),
        ("分析当前素材", "analyze"),
        ("转录这段音频", "analyze"),
        ("写个脚本", "script"),
        ("分镜", "script"),
        ("生成图片", "generate_media"),
        ("文生视频", "generate_media"),
        ("导出成片", "export"),
        ("export", "export"),
        ("关键帧怎么做", "help"),
        ("快捷键在哪", "help"),
    ])
    def test_phrase_routes_to_intent(self, text, expected):
        rt = InvocationRouter()
        assert rt.route(text).intent == expected

    def test_empty_text_unknown(self):
        rt = InvocationRouter()
        assert rt.route("").intent == "unknown"
        assert rt.route("   ").intent == "unknown"

    def test_skill_id_resolved_for_official_skills(self):
        rt = InvocationRouter()
        assert rt.route("生成字幕").skill_id == "topos.auto_caption"
        assert rt.route("口播精剪").skill_id == "topos.voiceover_polish"
        assert rt.route("30 秒产品广告").skill_id == "topos.product_ad_30s"


# ================================================================
# 槽位提取 + 只问一个最小问题
# ================================================================


class TestSlotExtraction:
    def test_language_extracted(self):
        rt = InvocationRouter()
        r = rt.route("配英文配音")
        assert r.extracted_inputs["target_language"] == "英文"
        assert r.matched is True  # 已有语言，无需问

    def test_duration_extracted(self):
        rt = InvocationRouter()
        r = rt.route("30 秒口播精剪")
        assert r.extracted_inputs["target_duration"] == 30

    def test_platform_extracted(self):
        rt = InvocationRouter()
        r = rt.route("剪一版竖版短视频")
        assert r.extracted_inputs["platform"] == "vertical_social"

    def test_missing_language_asks_one_question(self):
        """缺语言时只问一个最小问题（plan §10.3 P1.1）。"""
        rt = InvocationRouter()
        r = rt.route("帮我把字幕配音")
        assert r.matched is False
        assert r.pending_slot == "target_language"
        assert "语言" in r.clarify_question

    def test_prefilled_inputs_skip_question(self):
        """用户已答过的槽位不重复问。"""
        rt = InvocationRouter()
        r = rt.route("配音", prefilled_inputs={"target_language": "日语"})
        assert r.matched is True
        assert r.extracted_inputs["target_language"] == "日语"


# ================================================================
# help 意图
# ================================================================


class TestHelpIntent:
    def test_help_calls_help_search(self):
        called = []
        def fake_help(q):
            called.append(q)
            return ["panel:timeline", "shortcut:keyframe"]
        rt = InvocationRouter(help_search=fake_help)
        r = rt.route("关键帧怎么做")
        assert r.intent == "help"
        assert r.help_entry_ids == ("panel:timeline", "shortcut:keyframe")
        assert called == ["关键帧怎么做"]

    def test_help_search_exception_does_not_crash(self):
        def bad(_):
            raise RuntimeError("boom")
        rt = InvocationRouter(help_search=bad)
        r = rt.route("怎么用")
        assert r.intent == "help"
        assert r.matched is True
        assert r.help_entry_ids == ()


# ================================================================
# skill_resolver 校验
# ================================================================


class TestSkillResolver:
    def test_disabled_skill_blocks_match(self):
        rt = InvocationRouter(skill_resolver=lambda sid: False)
        r = rt.route("生成字幕")
        assert r.matched is False
        assert "未安装" in r.clarify_question or "未启用" in r.clarify_question

    def test_enabled_skill_matches(self):
        rt = InvocationRouter(skill_resolver=lambda sid: True)
        r = rt.route("生成字幕")
        assert r.matched is True
        assert r.skill_id == "topos.auto_caption"

    def test_resolver_exception_does_not_crash(self):
        def bad(_):
            raise RuntimeError("boom")
        rt = InvocationRouter(skill_resolver=bad)
        r = rt.route("生成字幕")
        # 异常被吞；按未校验处理（仍 matched）
        assert r.matched is True


# ================================================================
# to_invocation
# ================================================================


class TestToInvocation:
    def test_matched_produces_invocation(self):
        rt = InvocationRouter()
        r = rt.route("生成字幕")
        scope = InvocationScope(project_id="p1", timeline_id="t1",
                                selected_asset_ids=("a1",))
        inv = rt.to_invocation(
            r, scope=scope,
            policy=RunPolicy(permission=PermissionLevel.PROPOSE),
            parent_session_id="sess1",
        )
        assert inv is not None
        assert inv.source == "chat"
        assert inv.skill_id == "topos.auto_caption"
        assert inv.parent_session_id == "sess1"

    def test_user_text_never_in_invocation(self):
        """红线：用户原文不进 invocation（防注入 / 防拼接 tool 参数）。"""
        rt = InvocationRouter()
        r = rt.route("生成字幕")
        inv = rt.to_invocation(
            r, scope=InvocationScope(project_id="p1"),
            policy=RunPolicy(),
        )
        assert inv.user_text == ""

    def test_unmatched_returns_none(self):
        rt = InvocationRouter()
        r = rt.route("今天天气不错")  # unknown
        inv = rt.to_invocation(
            r, scope=InvocationScope(project_id="p1"), policy=RunPolicy(),
        )
        assert inv is None

    def test_help_does_not_produce_invocation(self):
        rt = InvocationRouter()
        r = rt.route("怎么用")
        inv = rt.to_invocation(
            r, scope=InvocationScope(project_id="p1"), policy=RunPolicy(),
        )
        assert inv is None

    def test_extracted_inputs_carried(self):
        rt = InvocationRouter()
        r = rt.route("30 秒口播精剪")
        inv = rt.to_invocation(
            r, scope=InvocationScope(project_id="p1", timeline_id="t1"),
            policy=RunPolicy(),
        )
        assert inv.inputs.get("target_duration") == 30

    def test_generation_placement_inputs_are_typed(self):
        rt = InvocationRouter()
        result = rt.route("在第 10 秒生成视频并放入时间线")
        assert result.intent == "generate_media"
        assert result.extracted_inputs["modality"] == "video"
        assert result.extracted_inputs["placement"] == "seconds"
        assert result.extracted_inputs["placement_seconds"] == 10.0


# ================================================================
# 脱敏（红线 §1.3）
# ================================================================


class TestSanitization:
    def test_path_in_text_does_not_leak_into_result(self):
        rt = InvocationRouter()
        r = rt.route("分析 /Users/testuser/secret/file 的素材",
                     scope=InvocationScope(project_id="p1"))
        # 路由仍工作（analyze），但描述 / scope_hint 不含原始路径
        assert r.intent == "analyze"
        assert "/Users/testuser" not in r.scope_hint
        assert "/Users/testuser" not in r.description

    def test_secret_in_text_redacted(self):
        rt = InvocationRouter()
        r = rt.route("分析素材，key=sk-1234567890abcdef",
                     scope=InvocationScope(project_id="p1"))
        assert r.intent == "analyze"
        # extracted_inputs 不含 secret
        assert "sk-1234567890abcdef" not in str(r.extracted_inputs)


# ================================================================
# 自定义规则
# ================================================================


class TestCustomRules:
    def test_custom_rule_overrides_builtin(self):
        custom = (IntentRule(
            intent="subtitle", keywords=("my-special-subtitle",),
            skill_id="custom.subtitle", description="自定义",
        ),)
        rt = InvocationRouter(custom_rules=custom)
        r = rt.route("my-special-subtitle")
        assert r.intent == "subtitle"
        assert r.skill_id == "custom.subtitle"

    def test_custom_rule_priority_over_builtin(self):
        """自定义规则在同关键词时优先。"""
        custom = (IntentRule(
            intent="rough_cut", keywords=("字幕",),
            skill_id="custom.special",
        ),)
        rt = InvocationRouter(custom_rules=custom)
        r = rt.route("字幕")
        # 自定义规则先匹配，覆盖内置 subtitle
        assert r.intent == "rough_cut"
        assert r.skill_id == "custom.special"


# ================================================================
# 范围确认提示（plan §0.3 第 6 条）
# ================================================================


class TestScopeHint:
    def test_analyze_all_warns(self):
        rt = InvocationRouter()
        r = rt.route("分析所有素材",
                     scope=InvocationScope(project_id="p1", analyze_all_assets=True))
        assert "全部" in r.scope_hint

    def test_analyze_selection_counts(self):
        rt = InvocationRouter()
        r = rt.route("分析素材", scope=InvocationScope(
            project_id="p1", selected_asset_ids=("a1", "a2", "a3"),
        ))
        assert "3" in r.scope_hint

    def test_analyze_no_selection_warns(self):
        rt = InvocationRouter()
        r = rt.route("分析素材", scope=InvocationScope(project_id="p1"))
        assert "未选择" in r.scope_hint or "全部" in r.scope_hint
