from src.features.teaching_animation.ai.html_edit_audit import diff_html_edit


def _deck(*pages: str, color: str = "#111111") -> str:
    body = "".join(
        f'<main data-topos-page="{index}">{page}</main>'
        for index, page in enumerate(pages, start=1)
    )
    return f"<!doctype html><html><head><style>body{{color:{color}}}</style></head><body>{body}</body></html>"


def test_diff_reports_page_copy_palette_and_page_count() -> None:
    before = _deck("旧标题", "第二页", color="#111111")
    after = _deck("新标题", "第二页", "总结", color="#ff8800")
    diff = diff_html_edit(before, after)

    assert diff.before_page_count == 2
    assert diff.after_page_count == 3
    assert diff.changed_pages == (1, 3)
    assert diff.added_colors == ("#ff8800",)
    assert diff.removed_colors == ("#111111",)
    assert "页数 2→3" in diff.summary()
    assert "更新第 1、3 页" in diff.summary()


def test_diff_does_not_claim_change_for_equivalent_html() -> None:
    document = _deck("一", color="#112233")
    diff = diff_html_edit(document, document)
    assert not diff.changed
    assert diff.summary() == "未检测到可见内容或颜色变化"
