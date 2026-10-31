"""Deterministic, user-readable audit records for HTML edit steps.

The browser validation gate proves that every page paints.  This module records
what changed between two authored documents so an autonomous edit can explain
each committed step and a user can decide whether to keep or undo it without
reading raw HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Mapping


_PAGE_RE = re.compile(
    r"<(?P<tag>section|article|div|main|aside|header|footer)\b[^>]*"
    r"\bdata-topos-page\s*=\s*[\"'][^\"']*[\"'][^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def _visible_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _page_texts(document: str) -> tuple[str, ...]:
    value = str(document or "")
    marks = list(_PAGE_RE.finditer(value))
    if not marks:
        text = _visible_text(value)
        return (text,) if text else ()
    result: list[str] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(value)
        result.append(_visible_text(value[mark.end():end]))
    return tuple(result)


@dataclass(frozen=True)
class HtmlEditDiff:
    """A compact, serializable description of one edit step."""

    before_page_count: int
    after_page_count: int
    changed_pages: tuple[int, ...]
    added_colors: tuple[str, ...]
    removed_colors: tuple[str, ...]
    html_size_delta: int

    @property
    def changed(self) -> bool:
        return bool(
            self.before_page_count != self.after_page_count
            or self.changed_pages
            or self.added_colors
            or self.removed_colors
            or self.html_size_delta
        )

    def summary(self) -> str:
        parts: list[str] = []
        if self.before_page_count != self.after_page_count:
            parts.append(f"页数 {self.before_page_count}→{self.after_page_count}")
        if self.changed_pages:
            pages = "、".join(str(item) for item in self.changed_pages[:8])
            suffix = "等" if len(self.changed_pages) > 8 else ""
            parts.append(f"更新第 {pages} 页{suffix}")
        if self.added_colors:
            parts.append("新增颜色 " + "、".join(self.added_colors[:4]))
        if self.removed_colors:
            parts.append("移除颜色 " + "、".join(self.removed_colors[:4]))
        if not parts:
            return "未检测到可见内容或颜色变化"
        return "；".join(parts)

    def to_dict(self) -> Mapping[str, object]:
        return {
            "before_page_count": self.before_page_count,
            "after_page_count": self.after_page_count,
            "changed_pages": list(self.changed_pages),
            "added_colors": list(self.added_colors),
            "removed_colors": list(self.removed_colors),
            "html_size_delta": self.html_size_delta,
            "summary": self.summary(),
        }


def diff_html_edit(before: str, after: str) -> HtmlEditDiff:
    """Compare page-visible copy and palette without judging subjective design."""
    before_value = str(before or "")
    after_value = str(after or "")
    old_pages = _page_texts(before_value)
    new_pages = _page_texts(after_value)
    changed = tuple(
        index + 1
        for index in range(max(len(old_pages), len(new_pages)))
        if (old_pages[index] if index < len(old_pages) else "")
        != (new_pages[index] if index < len(new_pages) else "")
    )
    old_colors = {item.lower() for item in _COLOR_RE.findall(before_value)}
    new_colors = {item.lower() for item in _COLOR_RE.findall(after_value)}
    return HtmlEditDiff(
        before_page_count=len(old_pages),
        after_page_count=len(new_pages),
        changed_pages=changed,
        added_colors=tuple(sorted(new_colors - old_colors)),
        removed_colors=tuple(sorted(old_colors - new_colors)),
        html_size_delta=len(after_value) - len(before_value),
    )


__all__ = ["HtmlEditDiff", "diff_html_edit"]
