"""InvocationRouter —— chat 意图 → Skill + 类型化输入（plan §10.3 P1.1）。

P1.1 是把聊天变成 Skill 驱动 Agent 的关键。设计原则（plan §10.3 表格 P1.1）：

1. **确定性匹配优先**：先匹配明确命令 / 关键词 / 快捷卡（分析、字幕、翻译配音、
   口播精剪、脚本、帮助、生成）；模型只用于补槽位 / 解释 / 建议。
2. **不把用户文本直接拼接为 tool 参数**：用户文本先经 :func:`sanitize_for_storage`
   脱敏；只有 router 解析出的类型化字段才进 invocation inputs。
3. **不确定时只问一个最小问题**：返回 :class:`RouteResult` 的 ``clarify_question``
   而非直接执行；问的总是"必要"问题（语言 / 目标时长 / 范围）之一，不一次问一堆。
4. **不误入粗剪 Planner**：找不到明确意图时**不**默认走 VariantPlanner，而是
   返回 ``needs_more_info`` 让 UI 决定（plan §0.3 第 4 条）。

路由结果（:class:`RouteResult`）只产"路由决定"，**不执行**任何 provider / 模型
（plan §10.1 实施纪律：router 是纯 Python，不在 Qt slot 里跑模型）。

红线对照：

- 外部输入不可信（plan §1.3 红线 3）：用户文本只作数据，不能修改权限 / 系统指令。
- 意图表是声明式、可审计的；不存 secret / 绝对路径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    sanitize_for_storage,
)


# ---------------------------------------------------------------------- #
# 意图类别
# ---------------------------------------------------------------------- #


IntentKind = Literal[
    "analyze",            # 分析素材（selection / bin / all）
    "subtitle",           # 生成字幕
    "subtitle_edit",      # 编辑现有字幕轨道
    "subtitle_quality",   # 字幕质量检查
    "translate_dub",      # 翻译 + 配音
    "rough_cut",          # 口播精剪 / 粗剪方案
    "product_ad",         # 产品广告
    "script",             # 写脚本 / 分镜
    "generate_media",     # 生成图片 / 视频 / 音乐
    "export",             # 导出当前时间线成片
    "help",               # "这个功能怎么用"
    "unknown",            # 无法确定（需澄清或放弃）
]


# ---------------------------------------------------------------------- #
# 意图规则表（声明式、可审计）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntentRule:
    """一条确定性意图匹配规则。

    ``keywords`` 中的任意一个在用户文本（已分词 / 小写）里命中即触发。中文按
    子串匹配（不做分词）；英文按词边界。``scope_hint`` 是 router 推荐的范围
    确认提示（不强制；最终范围由用户在确认卡确认）。
    """

    intent: IntentKind
    keywords: tuple[str, ...]
    skill_id: str = ""            # 对应的官方 Skill（空表示该意图暂无 Skill）
    description: str = ""
    #: 该意图必须澄清的槽位及其"如何提问"模板（key→提问文案）。
    #: router 按顺序检查：第一个未填的槽位才问（只问一个最小问题）。
    optional_slots: tuple[str, ...] = ()


# 意图表：顺序敏感（更具体的动作规则放前面，help 放最后兜底）。
# 注意：这些 skill_id 与现有 SkillRegistry 的官方 skill 对齐；
# 暂无 Skill 的意图（analyze/translate_dub/script/generate_media）skill_id 留空，
# 由 controller 调相应 service / 在 P2/P3 落地真实 Skill。
INTENT_RULES: tuple[IntentRule, ...] = (
    # ---- 具体动作优先（避免被 help 的宽泛词吞掉）---- #
    IntentRule(
        intent="translate_dub",
        keywords=("配音", "翻译配音", "配中文", "配英文", "配日语", "tts", "朗读",
                  "念字幕", "ai 声音", "ai 声"),
        skill_id="topos.subtitle_localization",
        description="翻译字幕并生成配音（先入生成素材箱，再审批放置）",
        optional_slots=("target_language",),
    ),
    IntentRule(
        intent="export",
        keywords=("导出成片", "导出视频", "导出文件", "导出", "export"),
        description="对当前时间线执行导出预检，并在确认后写入视频文件",
    ),
    IntentRule(
        intent="subtitle_quality",
        keywords=("字幕质检", "字幕质量检查", "检查字幕", "字幕 QC", "字幕QC"),
        description="检查字幕时间、重叠、行数和阅读速度",
    ),
    IntentRule(
        intent="subtitle_edit",
        keywords=(
            "字幕居中", "字幕左对齐", "字幕右对齐", "字幕对齐", "字幕位置",
            "字幕样式", "字幕字体", "字幕颜色", "字幕大小", "字幕批量",
            "所有字幕", "全部字幕", "当前字幕轨道", "字幕轨道全部",
            "自定义字幕", "全局字幕", "修改字幕", "删除字幕", "翻译字幕",
            "字幕翻译", "字幕内容", "字幕时间", "字幕 qc", "字幕质量",
        ),
        description="读取当前字幕轨道并生成可预览、可撤销的字幕编辑计划",
    ),
    IntentRule(
        intent="subtitle",
        keywords=("自动字幕", "生成字幕", "上字幕", "做字幕", "字幕",
                  "subtitle", "caption"),
        skill_id="topos.auto_caption",
        description="从转录证据生成字幕草稿 + EditPlan 提议",
    ),
    IntentRule(
        intent="product_ad",
        keywords=("产品广告", "30 秒广告", "广告片", "带货", "卖点", "product ad"),
        skill_id="topos.product_ad_30s",
        description="产品 claim + 素材生成 A/B 广告方案",
    ),
    IntentRule(
        intent="rough_cut",
        keywords=("精剪", "粗剪", "剪辑方案", "ab 方案", "a/b 方案", "剪成",
                  "口播精剪", "剪一版"),
        skill_id="topos.voiceover_polish",
        description="基于转录证据生成 A/B 粗剪方案",
    ),
    IntentRule(
        intent="analyze",
        keywords=("分析所有素材", "分析全部", "分析", "转录", "asr", "识别",
                  "ocr", "镜头切分", "场景", "analyze"),
        skill_id="",
        description="分析音频 / 镜头 / OCR / 节拍 / 响度（产 evidence）",
    ),
    IntentRule(
        intent="script",
        keywords=("写脚本", "分镜", "文案", "脚本", "script", "创意"),
        skill_id="",
        description="生成创意脚本 / 分镜文案",
    ),
    IntentRule(
        intent="generate_media",
        keywords=("生成图片", "生成视频", "生成音乐", "文生图", "文生视频",
                  "图生视频", "ai 生成", "生成素材"),
        skill_id="",
        description="生成图片 / 视频 / 音乐（先入生成素材箱）",
    ),
    # ---- help 兜底：含"怎么/如何"且未命中上面的动作 ---- #
    IntentRule(
        intent="help",
        keywords=("怎么用", "怎么操作", "怎么做", "怎么", "在哪里", "如何使用",
                  "如何", "快捷键", "功能介绍", "help", "教程", "用法",
                  "什么意思", "是什么", "哪里找"),
        skill_id="",
        description="查询功能用法 / 入口 / 快捷键",
    ),
)


# 澄清问题模板（每个槽位对应一个最小问题；只问一个）。
_CLARIFY_QUESTIONS: dict[str, str] = {
    "target_language": "要配音成哪种语言？（例如：中文 / 英文 / 日语）",
    "target_duration": "目标时长大约多少秒？",
    "voice_style": "希望什么风格的声音？（例如：女声温柔 / 男声稳重）",
    "platform": "发布到哪个平台？（例如：竖版短视频 / 横版长视频）",
}


# ---------------------------------------------------------------------- #
# RouteResult
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class RouteResult:
    """router 的输出（路由决定 + 可选澄清）。

    - ``matched`` 为 True：已确定意图 / Skill，可构造 invocation（可能仍需补槽位）。
    - ``matched`` 为 False 且 ``clarify_question`` 非空：需先问用户一个问题再路由。
    - ``matched`` 为 False 且 ``clarify_question`` 为空：无法确定，UI 应提示重写或
      显示快捷卡（plan §0.3 第 4 条：不默认进粗剪 Planner）。
    """

    intent: IntentKind
    matched: bool
    skill_id: str = ""
    description: str = ""
    #: 规则命中的关键词（用于"我将要做 X"提示，让用户知道 router 理解了）。
    matched_keyword: str = ""
    #: 需要问用户的最小问题（只问一个）；空表示无需澄清。
    clarify_question: str = ""
    #: 槽位需要后续填充的字段名（供 UI 收集后回填）。
    pending_slot: str = ""
    #: 从用户文本解析出的类型化输入（脱敏后；非用户原文拼接）。
    extracted_inputs: dict[str, Any] = field(default_factory=dict)
    #: help 候选（intent=help 时由 HelpIndex 填，供 UI 直接展示）。
    help_entry_ids: tuple[str, ...] = ()
    #: 推荐的范围确认提示（如"将分析 N 个素材，预计…"）。
    scope_hint: str = ""


# ---------------------------------------------------------------------- #
# 槽位提取（确定性；模型在 P3+ 补充）
# ---------------------------------------------------------------------- #


# 语言枚举（确定性识别）。
_LANGUAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("中文", r"中文|汉语|普通话|mandarin|chinese"),
    ("英文", r"英文|英语|english"),
    ("日语", r"日语|日文|japanese"),
    ("韩语", r"韩语|韩文|korean"),
    ("西班牙语", r"西班牙语|spanish"),
    ("法语", r"法语|法文|french"),
)
_DURATION_RE = re.compile(r"(\d{2,4})\s*(?:秒|s\b)")
_PLATFORM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("vertical_social", r"竖屏|竖版|短视频|抖音|tiktok|reels|vertical"),
    ("horizontal_long", r"横屏|横版|长视频|youtube|b 站|bilibili|horizontal"),
)


def _extract_language(text: str) -> str:
    for label, pat in _LANGUAGE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return label
    return ""


def _extract_duration(text: str) -> int:
    m = _DURATION_RE.search(text)
    if m:
        try:
            return max(1, int(m.group(1)))
        except ValueError:
            return 0
    return 0


def _extract_platform(text: str) -> str:
    for label, pat in _PLATFORM_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return label
    return ""


# ---------------------------------------------------------------------- #
# InvocationRouter
# ---------------------------------------------------------------------- #


class InvocationRouter:
    """聊天意图 → Skill + 类型化输入（plan §10.3 P1.1）。

    构造时可注入：

    - ``help_search``：``Callable[[str], list[str]]``，把 help 查询转成 entry_id 列表
      （P1.4 接 HelpIndex.search；这里保持解耦，router 不直接依赖 HelpIndex）。
    - ``custom_rules``：额外的意图规则（项目级 / 用户级扩展），优先于内置规则。
    - ``skill_resolver``：``Callable[[str], bool]``，校验 skill_id 是否已注册启用；
      若返回 False，router 把 skill_id 清空并在 description 里标"未安装"。
    """

    def __init__(
        self,
        *,
        help_search: Callable[[str], list[str]] | None = None,
        custom_rules: tuple[IntentRule, ...] = (),
        skill_resolver: Callable[[str], bool] | None = None,
    ) -> None:
        self._help_search = help_search
        self._skill_resolver = skill_resolver
        # 自定义规则优先（更具体的项目 / 用户扩展）。
        self._rules: tuple[IntentRule, ...] = custom_rules + INTENT_RULES

    # ---------------- 主入口 ---------------- #

    def route(
        self,
        text: str,
        *,
        scope: InvocationScope | None = None,
        prefilled_inputs: dict[str, Any] | None = None,
    ) -> RouteResult:
        """路由用户文本。

        Args:
            text: 用户原始输入（会先脱敏）。
            scope: 当前作用范围（用于判断"分析所有素材"是否需要二次确认）。
            prefilled_inputs: UI 之前已收集的槽位（避免重复问）。
        """
        if not text or not text.strip():
            return RouteResult(intent="unknown", matched=False)
        # 红线：用户文本先脱敏；不直接拼接为 tool 参数。
        cleaned = sanitize_for_storage(text.strip())
        lowered = cleaned.lower()
        pre = dict(prefilled_inputs or {})

        # 1. 命中第一条规则
        rule = self._match_rule(lowered)
        if rule is None:
            return RouteResult(intent="unknown", matched=False)

        # 2. help 意图：调 help_search 拿候选 entry_id（P1.4）
        if rule.intent == "help":
            entry_ids: tuple[str, ...] = ()
            if self._help_search is not None:
                try:
                    entry_ids = tuple(self._help_search(cleaned)[:10])
                except Exception:  # noqa: BLE001 — help 不应让 router 崩
                    entry_ids = ()
            # help 不需要"确认范围"，直接 matched
            return RouteResult(
                intent="help", matched=True,
                description=rule.description,
                matched_keyword=rule.keywords[0] if rule.keywords else "",
                help_entry_ids=entry_ids,
            )

        # 3. skill_id 校验：未注册 / 未启用 → 清空并提示
        skill_id = rule.skill_id
        if skill_id and self._skill_resolver is not None:
            try:
                if not self._skill_resolver(skill_id):
                    return RouteResult(
                        intent=rule.intent, matched=False,
                        skill_id="", description=rule.description,
                        matched_keyword=self._matched_kw(rule, lowered),
                        clarify_question=f"该功能需要的 Skill「{skill_id}」未安装或未启用。",
                    )
            except Exception:  # noqa: BLE001
                pass

        # 4. 提取槽位（确定性）
        extracted = self._extract_slots(cleaned)
        # 合并：prefilled 优先（用户已答过），其次 extracted
        merged = {**extracted, **{k: v for k, v in pre.items() if v}}

        # 5. 检查必填槽位：只问一个最小问题
        for slot in rule.optional_slots:
            if slot == "target_language":
                if not merged.get("target_language"):
                    lang = _extract_language(cleaned)
                    if lang:
                        merged["target_language"] = lang
                    else:
                        return RouteResult(
                            intent=rule.intent, matched=False,
                            skill_id=skill_id, description=rule.description,
                            matched_keyword=self._matched_kw(rule, lowered),
                            clarify_question=_CLARIFY_QUESTIONS["target_language"],
                            pending_slot="target_language",
                            extracted_inputs=merged,
                        )
            # 其它槽位（duration/platform/voice_style）：缺则只问一个
            elif slot == "target_duration" and not merged.get("target_duration"):
                return RouteResult(
                    intent=rule.intent, matched=False,
                    skill_id=skill_id, description=rule.description,
                    matched_keyword=self._matched_kw(rule, lowered),
                    clarify_question=_CLARIFY_QUESTIONS["target_duration"],
                    pending_slot="target_duration",
                    extracted_inputs=merged,
                )

        # 6. 范围确认提示（plan §0.3 第 6 条：分析范围必须显式确认）
        scope_hint = self._build_scope_hint(rule, scope)

        return RouteResult(
            intent=rule.intent, matched=True,
            skill_id=skill_id, description=rule.description,
            matched_keyword=self._matched_kw(rule, lowered),
            extracted_inputs=merged,
            scope_hint=scope_hint,
        )

    # ---------------- 构造 invocation ---------------- #

    def to_invocation(
        self,
        result: RouteResult,
        *,
        scope: InvocationScope,
        policy: Any,
        parent_session_id: str = "",
    ) -> Invocation | None:
        """把已 matched 的 :class:`RouteResult` 转成 :class:`Invocation`。

        未 matched（需澄清 / 未知）时返回 None。help 意图也不产 invocation
        （它走 HelpIndex，不是 Skill 运行）。
        """
        if not result.matched or result.intent == "help":
            return None
        from src.features.ai_harness.invocation_models import InvocationSource, RunPolicy
        # run-time policy 默认值兜底
        if policy is None:
            policy = RunPolicy()
        return Invocation(
            source=InvocationSource.CHAT,
            skill_id=result.skill_id,
            scope=scope,
            policy=policy,
            inputs=dict(result.extracted_inputs),
            parent_session_id=parent_session_id,
            # 不把用户原文塞进 invocation（脱敏摘要都不塞；原文仅留在 UI 历史）
            user_text="",
        )

    # ---------------- 内部 ---------------- #

    def _match_rule(self, lowered: str) -> IntentRule | None:
        for rule in self._rules:
            for kw in rule.keywords:
                if kw.lower() in lowered:
                    return rule
        return None

    def _matched_kw(self, rule: IntentRule, lowered: str) -> str:
        for kw in rule.keywords:
            if kw.lower() in lowered:
                return kw
        return ""

    def _extract_slots(self, cleaned: str) -> dict[str, Any]:
        slots: dict[str, Any] = {}
        # 生成媒体的 modality 是类型化输入；未指定时由宿主默认 image。
        if "视频" in cleaned or "video" in cleaned.lower():
            slots["modality"] = "video"
        elif "音乐" in cleaned or "music" in cleaned.lower():
            slots["modality"] = "music"
        elif "配音" in cleaned or "声音" in cleaned or "voice" in cleaned.lower():
            slots["modality"] = "voice"
        elif "图片" in cleaned or "图像" in cleaned or "image" in cleaned.lower():
            slots["modality"] = "image"
        placement = _extract_placement(cleaned)
        if placement is not None:
            slots.update(placement)
        lang = _extract_language(cleaned)
        if lang:
            slots["target_language"] = lang
        dur = _extract_duration(cleaned)
        if dur:
            slots["target_duration"] = dur
        plat = _extract_platform(cleaned)
        if plat:
            slots["platform"] = plat
        return slots

    def _build_scope_hint(
        self, rule: IntentRule, scope: InvocationScope | None,
    ) -> str:
        if scope is None:
            return ""
        if rule.intent == "analyze":
            if scope.analyze_all_assets:
                return "将分析媒体池中的全部素材，预计耗时较长。"
            n = len(scope.selected_asset_ids)
            if n:
                return f"将分析已选的 {n} 个素材。"
            return "未选择素材。请先选择素材，或确认分析全部。"
        if rule.intent in ("subtitle", "translate_dub", "rough_cut", "product_ad"):
            n = len(scope.selected_asset_ids)
            if n:
                return f"作用于已选的 {n} 个素材。"
            return "未选择素材，将使用当前时间线。"
        return ""


def _extract_placement(text: str) -> dict[str, Any] | None:
    """提取生成媒体的放置提示；只返回类型化位置，不执行任何写入。"""
    lowered = text.lower()
    if "播放头" in text or "当前位置" in text or "playhead" in lowered:
        return {"placement": "playhead"}
    if "开头" in text or "起始" in text or "start" in lowered:
        return {"placement": "start"}
    if "末尾" in text or "片尾" in text or "结尾" in text or "end" in lowered:
        return {"placement": "end"}
    import re

    match = re.search(r"第\s*(\d+(?:\.\d+)?)\s*(秒|s)", text, flags=re.IGNORECASE)
    if match:
        return {"placement": "seconds", "placement_seconds": float(match.group(1))}
    return None


__all__ = [
    "INTENT_RULES",
    "IntentKind",
    "IntentRule",
    "InvocationRouter",
    "RouteResult",
]
