"""P0.5 测试：上下文压缩例程（规则摘要 + CompressionCheckpoint）。

> 验证 §10.2 P0.5：
> - ``ContextService.compress`` 把低优先级（HISTORY/RECENT_SUMMARY）blocks 合并成
>   一条 RECENT_SUMMARY 优先级的摘要 block（不调 LLM，用规则摘要：role + 前 80 字符）
> - 压缩后 bundle.block_count 减少、total_tokens 下降
> - HARD_CONSTRAINT block 不被压缩
> - 原始 evidence/artifact refs 保留进 CompressionCheckpoint（红线：evidence 不丢）
> - controller _on_context_compress 调 store.add_compression_checkpoint
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.ai_editing.context_bundle import (
    ContextBlock,
    ContextBlockPriority,
    KIND_HISTORY,
    KIND_SYSTEM_POLICY,
    KIND_EVIDENCE,
    TokenBudget,
)
from src.features.edit.ai_editing.context_service import ContextService
from src.features.edit.ai_editing.assistant_session_store import AssistantSessionStore

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


def _make_block(
    *, kind: str = KIND_HISTORY, priority=ContextBlockPriority.HISTORY,
    content: str = "x" * 200, tokens: int = 100, provenance: dict | None = None,
) -> ContextBlock:
    return ContextBlock(
        kind=kind, content=content, priority=priority,
        token_estimate=tokens, why_included="test",
        provenance=provenance or {},
    )


def _make_bundle(blocks, *, limit=1000):
    from src.features.edit.ai_editing.context_bundle import ContextBundle
    return ContextBundle(
        blocks=tuple(blocks),
        budget=TokenBudget(limit=limit, reserved_for_response=0),
    )


# ---------------------------------------------------------------------- #
# ContextService.compress
# ---------------------------------------------------------------------- #


def test_compress_merges_history_into_summary_block():
    """compress 把多个 HISTORY blocks 合并成一条 RECENT_SUMMARY block。"""
    bundle = _make_bundle([
        _make_block(content="用户问 A", tokens=100),
        _make_block(content="助手答 B", tokens=100),
        _make_block(content="用户问 C", tokens=100),
    ])
    new_bundle, cp = ContextService().compress(bundle)
    # 摘要 block 应是 RECENT_SUMMARY 优先级
    summary_blocks = [
        b for b in new_bundle.blocks
        if b.priority == ContextBlockPriority.RECENT_SUMMARY
    ]
    assert len(summary_blocks) >= 1, "应产至少一条 RECENT_SUMMARY 摘要 block"


def test_compress_reduces_token_count():
    """压缩后 total_tokens 下降（摘要比原文短）。"""
    bundle = _make_bundle([
        _make_block(content="很长的历史消息" * 20, tokens=500),
        _make_block(content="另一段长历史" * 20, tokens=500),
    ])
    new_bundle, _ = ContextService().compress(bundle)
    assert new_bundle.total_tokens < bundle.total_tokens, (
        "压缩后 token 数应下降"
    )


def test_compress_preserves_hard_constraint_blocks():
    """HARD_CONSTRAINT block（system_policy 等）不被压缩。"""
    hard = _make_block(
        kind=KIND_SYSTEM_POLICY, priority=ContextBlockPriority.HARD_CONSTRAINT,
        content="系统策略", tokens=50,
    )
    bundle = _make_bundle([hard, _make_block(content="历史", tokens=300)])
    new_bundle, _ = ContextService().compress(bundle)
    # hard block 应原样保留
    hard_blocks = [b for b in new_bundle.blocks if b.kind == KIND_SYSTEM_POLICY]
    assert len(hard_blocks) == 1
    assert hard_blocks[0].content == "系统策略"


def test_compress_preserves_evidence_blocks():
    """EVIDENCE 优先级 block 不被压缩成摘要（保留高置信证据原文）。"""
    evidence = _make_block(
        kind=KIND_EVIDENCE, priority=ContextBlockPriority.EVIDENCE,
        content="转录证据", tokens=200,
    )
    bundle = _make_bundle([evidence, _make_block(content="历史", tokens=300)])
    new_bundle, _ = ContextService().compress(bundle)
    evidence_blocks = [b for b in new_bundle.blocks if b.kind == KIND_EVIDENCE]
    assert len(evidence_blocks) == 1
    assert evidence_blocks[0].content == "转录证据"


def test_compress_checkpoint_preserves_evidence_refs():
    """CompressionCheckpoint.preserved_evidence_refs 含原 evidence block 的 provenance。"""
    evidence = _make_block(
        kind=KIND_EVIDENCE, priority=ContextBlockPriority.EVIDENCE,
        content="转录", tokens=200,
        provenance={"evidence_id": "ev-1", "asset_id": "a1"},
    )
    bundle = _make_bundle([evidence, _make_block(content="历史", tokens=300)])
    _, cp = ContextService().compress(bundle)
    assert "ev-1" in cp.preserved_evidence_refs or any(
        "ev-1" in r for r in cp.preserved_evidence_refs
    ), "checkpoint 应保留 evidence 引用"


def test_compress_checkpoint_message_count():
    """CompressionCheckpoint 记录压缩前后的 block 数。"""
    bundle = _make_bundle([
        _make_block(content="历史 1", tokens=100),
        _make_block(content="历史 2", tokens=100),
    ])
    _, cp = ContextService().compress(bundle)
    assert cp.message_count_before == 2
    assert cp.message_count_after < cp.message_count_before


def test_compress_summary_content_is_redacted():
    """摘要落盘时脱敏（绝对路径不进 checkpoint.to_dict()['summary']）。

    CompressionCheckpoint 内存保留原文；to_dict（落盘）时经 sanitize_for_storage
    把绝对路径 / secret 替换为占位符（与 Invocation.user_text 同模式）。
    """
    bundle = _make_bundle([
        _make_block(content="处理 /Users/testuser/secret/x.mp4", tokens=100),
    ])
    _, cp = ContextService().compress(bundle)
    dumped = cp.to_dict()
    assert "/Users/testuser/secret" not in dumped["summary"], (
        "checkpoint.to_dict()['summary'] 落盘时必须脱敏绝对路径"
    )


def test_compress_empty_bundle_is_noop():
    """空 bundle 压缩返回空 bundle + 空 checkpoint（不抛错）。"""
    bundle = _make_bundle([])
    new_bundle, cp = ContextService().compress(bundle)
    assert len(new_bundle.blocks) == 0


# ------------------------------------------------------------------ #
# controller _on_context_compress → store.add_compression_checkpoint
# ------------------------------------------------------------------ #


def _make_controller(qapp, *, store=None):
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

    fake_panel = MagicMock()
    sig_names = (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
        "composer_scope_requested", "composer_permission_changed",
        "composer_profile_requested", "context_compress_requested",
        "new_session_requested", "show_history_requested",
        "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
    )
    for n in sig_names:
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()

    return AiEditController(
        panel=fake_panel, service=fake_service, session_store=store, parent=qapp,
    )


def test_context_compress_writes_checkpoint_to_store(qapp):
    """_on_context_compress 调 store.add_compression_checkpoint。"""
    store = AssistantSessionStore()
    controller = _make_controller(qapp, store=store)

    # 构造一个含 history 的 bundle 灌进 controller（模拟 _on_inspect_context 已 build）
    bundle = _make_bundle([_make_block(content="历史", tokens=300)])
    controller._on_context_compress(bundle)

    entry = store.get_active_session()
    assert len(entry.compression_checkpoints) >= 1


def test_context_compress_noop_without_store(qapp):
    """store 未注入时 _on_context_compress 不抛错。"""
    controller = _make_controller(qapp, store=None)
    bundle = _make_bundle([_make_block(content="历史", tokens=300)])
    controller._on_context_compress(bundle)  # 不抛错


def test_context_compress_refreshes_footer(qapp):
    """压缩后 footer 显示 context_compressed=True。"""
    from src.features.edit.ui.ai_assistant_composer import ComposerFooterState
    store = AssistantSessionStore()
    footer_states: list[ComposerFooterState] = []
    controller = _make_controller(qapp, store=store)
    controller._panel.set_composer_footer = lambda s: footer_states.append(s)

    bundle = _make_bundle([_make_block(content="历史", tokens=300)])
    controller._on_context_compress(bundle)

    assert len(footer_states) >= 1
    assert footer_states[-1].context_compressed is True
