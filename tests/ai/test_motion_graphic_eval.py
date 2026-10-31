"""Regression coverage for the versioned Motion Graphic HTML evaluation corpus."""

from __future__ import annotations

from src.features.teaching_animation.ai.motion_graphic_eval import (
    DEFAULT_MOTION_GRAPHIC_EVAL_CASES,
    MOTION_GRAPHIC_EVAL_CORPUS_VERSION,
    evaluate_motion_graphic_html_case,
    run_motion_graphic_eval,
)


def _page(title: str, summary: str, *, index: int) -> str:
    return (
        f'<section data-topos-page="{index}" class="page page-{index}">'
        f"<h1>{title}</h1><p>{summary}</p>"
        "<div class='grid'><article class='card'>目标</article>"
        "<article class='card'>规划</article><article class='card'>验证</article></div>"
        "<ul><li>读取实时上下文</li><li>调用合适工具</li><li>复盘并推进下一步</li></ul>"
        "<svg aria-hidden='true'><circle cx='20' cy='20' r='12'/><path d='M0 0L40 40'/></svg>"
        "</section>"
    )


def _deck(*pages: str, accent: str = "#ff8800") -> str:
    return (
        "<!doctype html><html><head><style>"
        f":root{{--accent:{accent};}}"
        "html,body{margin:0;min-height:100vh;background:linear-gradient(135deg,#3b1604,var(--accent));color:#fff}"
        ".page{min-height:100vh;padding:clamp(24px,5vw,96px);display:grid;gap:clamp(12px,2vw,30px)}"
        ".grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}"
        ".card{padding:20px;border:1px solid var(--accent)}"
        "@media (max-width:700px){.grid{grid-template-columns:1fr}.page{padding:24px}}"
        "@keyframes enter{to{opacity:1}}</style></head><body>"
        + "".join(pages)
        + "</body></html>"
    )


def test_default_corpus_passes_rich_responsive_artifacts() -> None:
    artifacts = {
        "agent_intro_1080p_responsive": _deck(
            _page("什么是 AI Agent？", "它把目标拆成可验证的行动，并在每次行动后读取新状态。", index=1),
            _page("从意图到成果", "感知、规划、调用工具和复盘组成一个可持续推进的工作循环。", index=2),
        ),
        "orange_revision_with_fourth_page": _deck(
            _page("从蓝图到行动", "新的橙色色系让每个阶段都更清晰、更有推进感。", index=1),
            _page("理解任务", "先读取约束、资料与当前状态，再决定真正需要解决的问题。", index=2),
            _page("调用工具", "将计划落实为搜索、编辑、测试和复核等可观察的动作。", index=3),
            _page("下一步行动", "把结果、证据和待办回写给用户，开启下一次可靠的循环。", index=4),
        ),
        "explicit_brand_copy_and_color": _deck(
            _page("新品发布", "用 #ff8800 建立统一的品牌识别，并清晰呈现本次发布的价值。", index=1),
            _page("立即体验", "第 2 页提供明确的试用路径、关键亮点与下一步行动说明。", index=2),
        ),
    }

    report = run_motion_graphic_eval(artifacts)

    assert report.corpus_version == MOTION_GRAPHIC_EVAL_CORPUS_VERSION
    assert report.passed is True
    assert report.passed_count == len(DEFAULT_MOTION_GRAPHIC_EVAL_CASES)
    assert all(result.responsive_layout_score >= 0.70 for result in report.results)


def test_corpus_rejects_fixed_canvas_and_missing_explicit_copy() -> None:
    case = DEFAULT_MOTION_GRAPHIC_EVAL_CASES[0]
    artifact = (
        "<html><head><style>html,body{width:1920px;height:1080px;background:#111827}"
        ".page{display:grid}</style></head><body>"
        "<section data-topos-page='1'><h1>AI Agent</h1></section></body></html>"
    )

    result = evaluate_motion_graphic_html_case(case, artifact)

    assert result.passed is False
    assert any("什么是 AI Agent？" in issue for issue in result.issues)
    assert "设计完整度或响应式布局未通过生产门槛" in result.issues
