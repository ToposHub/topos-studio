"""Tests for bounded model-facing tool-result rendering."""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.tool_result_pruner import (
    PRUNE_MARKER,
    ToolResultPruneConfig,
    ToolResultPruner,
)


def test_short_result_is_unchanged():
    rendered = ToolResultPruner(ToolResultPruneConfig(threshold_chars=64, head_chars=8, tail_chars=4)).render({"ok": True})
    assert rendered.pruned is False
    assert rendered.text == '{"ok": true}'


def test_long_result_keeps_head_and_tail_with_marker():
    pruner = ToolResultPruner(
        ToolResultPruneConfig(threshold_chars=64, head_chars=8, tail_chars=4)
    )
    rendered = pruner.prune_text("HEAD-1234567890-" + ("MIDDLE-" * 20) + "TAIL")

    assert rendered.pruned is True
    assert rendered.text.startswith("HEAD-123")
    assert rendered.text.endswith("TAIL")
    assert PRUNE_MARKER in rendered.text
    assert rendered.chars_after <= 64
    assert rendered.chars_before > rendered.chars_after


def test_unicode_is_bounded_without_surrogate_splitting():
    pruner = ToolResultPruner(
        ToolResultPruneConfig(threshold_chars=64, head_chars=4, tail_chars=4)
    )
    rendered = pruner.prune_text("😀" * 100)

    assert rendered.pruned is True
    assert len(rendered.text) <= 64
    assert rendered.text.count("😀") == 8


def test_invalid_budget_is_rejected():
    with pytest.raises(ValueError):
        ToolResultPruneConfig(threshold_chars=4, head_chars=4, tail_chars=4)
