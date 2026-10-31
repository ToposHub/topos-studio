"""FH-07 / FH-08: HTML complexity budget enforcement + render evidence.

FH-07 turns the W0-A ``ResourceBudget`` into an actionable validator over
sanitized HTML — it counts DOM nodes/depth, stylesheet length, keyframes and
data-URI sizes so a runaway page is reported (with a compress hint) before it
can freeze the renderer.  FH-08 captures the renderer-build/viewport/DPR/
font-set/asset-hash evidence that participates in the cache key and lets a
human or CI explain a cache miss or a preview/export mismatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Mapping

from ..html_renderer_spike.security import ResourceBudget, BudgetViolation


# ---------------------------------------------------------------------------
# FH-07: complexity budget enforcement over sanitized HTML
# ---------------------------------------------------------------------------

class _DomCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.node_count = 0
        self.max_depth = 0
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.node_count += 1
        self._depth += 1
        if self._depth > self.max_depth:
            self.max_depth = self._depth

    def handle_endtag(self, tag: str) -> None:
        if self._depth > 0:
            self._depth -= 1


@dataclass(frozen=True)
class DomStats:
    node_count: int
    max_depth: int
    stylesheet_chars: int
    keyframes: int
    data_uri_bytes: int
    #: False when the DOM walk aborted mid-parse: node_count/max_depth are
    #: then partial values, not evidence the page fits the budget.
    count_complete: bool = True
    #: Structured reason the DOM walk aborted ("" when it completed).
    count_error: str = ""


@dataclass(frozen=True)
class BudgetReport:
    exceeded: tuple[BudgetViolation, ...]
    stats: DomStats

    @property
    def ok(self) -> bool:
        return not self.exceeded


def measure_dom(sanitized_html: str) -> DomStats:
    """Count DOM nodes/depth, stylesheet size, keyframes and data-URI sizes.

    ``data_uri_bytes`` is the *largest single* inline ``data:`` URI, matching
    ``ResourceBudget.max_data_uri_bytes`` ("individual inline data: payload");
    summing every URI made many small legitimate icons trip a per-item limit
    (AUD-30002).  If the DOM walk aborts mid-parse the stats are flagged
    ``count_complete=False`` so ``enforce_budget`` can fail closed instead of
    approving a partially counted runaway page (AUD-30001).
    """
    parser = _DomCounter()
    count_error = ""
    try:
        parser.feed(sanitized_html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - surfaced via count_complete/count_error, never silent
        count_error = f"dom count incomplete: {type(exc).__name__}: {exc}"
    # Pull <style> contents.
    style_chars = 0
    keyframes = 0
    for match in re.finditer(r"<style[^>]*>(.*?)</style>", sanitized_html, re.IGNORECASE | re.DOTALL):
        body = match.group(1)
        style_chars += len(body)
        keyframes += len(re.findall(r"@\s*keyframes", body, re.IGNORECASE))
    # Add inline style attribute lengths to the stylesheet budget view.
    style_chars += sum(len(m) for m in re.findall(r'style="[^"]*"', sanitized_html))
    data_uri_bytes = max((len(m) for m in re.findall(r"data:[^\"')]+", sanitized_html)), default=0)
    return DomStats(
        node_count=parser.node_count,
        max_depth=parser.max_depth,
        stylesheet_chars=style_chars,
        keyframes=keyframes,
        data_uri_bytes=data_uri_bytes,
        count_complete=not count_error,
        count_error=count_error,
    )


def enforce_budget(sanitized_html: str, budget: ResourceBudget | None = None) -> BudgetReport:
    """Return every budget violation plus full DOM stats (fail-closed on partial counts)."""
    budget = budget or ResourceBudget()
    stats = measure_dom(sanitized_html)
    violations: list[BudgetViolation] = []
    if not stats.count_complete:
        # AUD-30001: the parser aborting must never read as "within budget".
        # Report the partial measurement as a structured violation so the
        # quality gate surfaces it instead of silently passing the page.
        violations.append(BudgetViolation("dom_count_incomplete", 1, 0))
    if stats.node_count > budget.max_dom_nodes:
        violations.append(BudgetViolation("max_dom_nodes", stats.node_count, budget.max_dom_nodes))
    if stats.max_depth > budget.max_dom_depth:
        violations.append(BudgetViolation("max_dom_depth", stats.max_depth, budget.max_dom_depth))
    if stats.stylesheet_chars > budget.max_stylesheet_chars:
        violations.append(BudgetViolation("max_stylesheet_chars", stats.stylesheet_chars, budget.max_stylesheet_chars))
    if stats.keyframes > budget.max_keyframes:
        violations.append(BudgetViolation("max_keyframes", stats.keyframes, budget.max_keyframes))
    if stats.data_uri_bytes > budget.max_data_uri_bytes:
        violations.append(BudgetViolation("max_data_uri_bytes", stats.data_uri_bytes, budget.max_data_uri_bytes))
    return BudgetReport(tuple(violations), stats)


# ---------------------------------------------------------------------------
# FH-08: HTML render evidence (cache-key + diagnostic record)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HtmlRenderEvidence:
    """Provenance recorded for every HTML capture (FH-08 / plan §3.2 RenderEvidence)."""

    renderer_id: str
    renderer_build: str
    viewport: tuple[int, int]
    dpr: float
    source_hash: str
    font_set_hash: str = ""
    asset_set_hash: str = ""
    sanitizer_version: str = ""
    frame_bridge_version: str = ""
    duration_ms: float = 0.0
    frame_hash: str = ""
    notes: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "renderer_id": self.renderer_id, "renderer_build": self.renderer_build,
            "viewport": list(self.viewport), "dpr": self.dpr,
            "source_hash": self.source_hash, "font_set_hash": self.font_set_hash,
            "asset_set_hash": self.asset_set_hash, "sanitizer_version": self.sanitizer_version,
            "frame_bridge_version": self.frame_bridge_version, "duration_ms": self.duration_ms,
            "frame_hash": self.frame_hash, "notes": dict(self.notes),
        }

    def cache_key(self) -> str:
        """Compose the cache-key inputs mandated by the determinism contract."""
        return "|".join([
            self.renderer_build, self.source_hash[:16], self.font_set_hash[:16],
            self.asset_set_hash[:16], f"{self.viewport[0]}x{self.viewport[1]}", f"{self.dpr:.4f}",
            self.sanitizer_version, self.frame_bridge_version,
        ])


__all__ = [
    "BudgetReport", "DomStats", "HtmlRenderEvidence",
    "enforce_budget", "measure_dom",
]
