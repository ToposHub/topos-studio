"""Deterministic completeness checks for freeform Motion Graphic HTML."""

from src.features.teaching_animation.ai.html_design_quality import evaluate_html_design


def _document(*pages: str, css: str = "") -> str:
    body = "".join(
        f'<section data-topos-page="{index}">{page}</section>'
        for index, page in enumerate(pages, start=1)
    )
    return f"<!doctype html><html><head><style>{css}</style></head><body>{body}</body></html>"


def test_sparse_placeholder_page_requests_repair_with_actionable_detail() -> None:
    report = evaluate_html_design(_document("<h1>AI Agent</h1>"))

    assert report.needs_repair is True
    assert report.pages[0].richness_score < 0.42
    assert "第 1 页" in report.repair_feedback()
    assert "信息单元" in report.repair_feedback()


def test_rich_multi_page_explainer_passes_local_completeness_baseline() -> None:
    report = evaluate_html_design(_document(
        "<main class='card'><h1>什么是 AI Agent？</h1><p>它会理解目标、制定计划并调用工具。</p>"
        "<ul><li>感知上下文</li><li>拆分任务</li><li>执行与复盘</li></ul><svg><circle /></svg></main>",
        "<main class='card'><h1>从目标到行动</h1><p>每一步都会读取最新状态，而不是只输出一次答案。</p>"
        "<div class='grid'><article>规划</article><article>调用工具</article><article>验证结果</article></div>"
        "<svg><path /></svg></main>",
        css=(
            "body{background:linear-gradient(135deg,#111827,#312e81);color:#f8fafc}"
            ".grid{display:grid;grid-template-columns:repeat(3,1fr)}"
            ".card{display:flex;flex-direction:column;width:100%;min-height:100vh}"
            "@keyframes rise{to{opacity:1}}"
        ),
    ))

    assert report.needs_repair is False
    assert report.overall_score >= 0.58
    assert all(page.richness_score >= 0.42 for page in report.pages)
    assert report.visual_language_score >= 0.30
    assert report.responsive_layout_score >= 0.70


def test_fixed_delivery_canvas_requests_responsive_repair() -> None:
    report = evaluate_html_design(
        _document(
            "<main class='card'><h1>什么是 AI Agent？</h1><p>它会感知、规划、执行与复盘。</p>"
            "<article>理解目标</article><article>调用工具</article><svg><circle /></svg></main>",
            css=(
                "html,body{width:1920px;height:1080px;background:#111827;color:#f8fafc}"
                ".card{display:grid;grid-template-columns:repeat(2,1fr)}"
            ),
        ),
        viewport_width=1920,
        viewport_height=1080,
    )

    assert report.fixed_canvas_rules == 2
    assert report.needs_repair is True
    assert any("写死" in warning for warning in report.warnings)
    assert "不能写死" in report.repair_feedback()


def test_inline_fixed_delivery_canvas_is_not_missed() -> None:
    report = evaluate_html_design(
        """<!doctype html><html><body style="width: 1920px; height: 1080px;"
        ><section data-topos-page="1"><h1>AI Agent</h1><p>理解目标并调用工具。</p>
        <article>计划</article><article>执行</article></section></body></html>""",
        viewport_width=1920,
        viewport_height=1080,
    )

    assert report.fixed_canvas_rules == 2
    assert report.needs_repair is True


def test_percent_fluid_size_counts_as_responsive_signal() -> None:
    report = evaluate_html_design(_document(
        "<main class='card'><h1>AI Agent</h1><p>理解目标、调用工具并复盘结果。</p>"
        "<article>计划</article><article>执行</article></main>",
        css=(
            "body{width:100%;background:#111827;color:#f8fafc}"
            ".card{display:grid;grid-template-columns:repeat(2,1fr)}"
        ),
    ))

    assert report.responsive_layout_score >= 0.70
    assert report.fixed_canvas_rules == 0


def test_visual_title_class_counts_as_a_clear_heading_signal() -> None:
    report = evaluate_html_design(_document(
        "<main class='card'><div class='section-title'>什么是 AI Agent？</div>"
        "<p>它会理解目标、制定计划，并调用工具完成一系列可验证的动作。</p>"
        "<article>感知上下文</article><article>执行与复盘</article></main>",
        css="body{background:#111827;color:#f8fafc}.card{display:grid}",
    ))

    assert report.pages[0].heading_count == 1
    assert "缺少明确标题层级" not in report.pages[0].findings


def test_repeated_pages_are_reported_without_blocking_an_otherwise_rich_deck() -> None:
    page = (
        "<main class='card'><h1>重复段落</h1><p>这是一段足够完整的说明文字，包含背景、动作和结果。</p>"
        "<ul><li>第一点</li><li>第二点</li><li>第三点</li></ul></main>"
    )
    report = evaluate_html_design(_document(
        page, page,
        css="body{background:#111827;color:#f8fafc}.card{display:grid}",
    ))

    assert report.needs_repair is False
    assert report.page_diversity_score < 0.12
    assert any("高度重复" in warning for warning in report.warnings)


def test_nested_page_boundary_is_rejected_before_browser_export() -> None:
    # A missing closing wrapper in a model response can make page 2 a child of
    # page 1. Static page counting still sees two markers, but the page-deck
    # runtime hides the child together with its inactive parent.
    report = evaluate_html_design(_document(
        "<main><h1>第一步</h1><p>读取上下文、理解目标并建立任务状态。</p>"
        "<article>输入</article><article>目标</article>"
        "<section data-topos-page=\"2\"><h1>第二步</h1><p>规划并调用工具完成任务。</p>"
        "<article>计划</article><article>执行</article></section>",
        css="body{width:100%;min-height:100vh}.grid{display:grid}",
    ))

    assert report.page_structure_issues
    assert any("嵌套" in issue for issue in report.page_structure_issues)
    assert report.needs_repair is True
    assert "独立的兄弟容器" in report.repair_feedback()


def test_final_optional_page_closing_tag_is_allowed() -> None:
    # HTML5 permits an omitted final closing tag; only a later nested page is
    # a blocking boundary error.
    report = evaluate_html_design(
        "<section data-topos-page=\"1\"><h1>完成</h1><p>结果与下一步说明。</p>"
        "<article>结果</article><article>下一步</article>",
        viewport_width=1920,
        viewport_height=1080,
    )

    assert report.page_structure_issues == ()
