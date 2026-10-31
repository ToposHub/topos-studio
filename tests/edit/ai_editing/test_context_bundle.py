"""TAEP M5 — ContextBundle 数据模型测试。

验收（§7.1）：
- block 化：每块带 kind/priority/token_estimate/why_included/provenance/expiry。
- 优先级表（§7.1）：硬约束 > 选区 > 决策 > 证据 > 摘要 > 历史。
- with_budget：超限时按 priority 升序丢弃低优先级；HARD_CONSTRAINT 绝不丢。
- exclude_block：用户主动移除（UI"排除此上下文"）。
- to_messages：按 priority 装配 system + user 消息，每块带 [kind] 标记。
- needs_compression：达 60% 预算触发压缩标志。
- 不存 secret（content 假定已脱敏；ContextBundle 不抓数据）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.context_bundle import (
    EXPIRY_NEVER,
    EXPIRY_PLAN,
    ContextBlock,
    ContextBlockPriority,
    ContextBundle,
    TokenBudget,
    KIND_BRIEF,
    KIND_EVIDENCE,
    KIND_HISTORY,
)


def _block(
    kind: str = "test", *, priority: ContextBlockPriority = ContextBlockPriority.EVIDENCE,
    tokens: int = 100, role: str = "user", provenance: dict | None = None,
) -> ContextBlock:
    return ContextBlock(
        kind=kind, content=f"{kind} content", priority=priority,
        token_estimate=tokens, why_included=f"test {kind}",
        provenance=provenance or {}, expiry=EXPIRY_PLAN, prompt_role=role,
    )


class TestContextBlock:
    def test_id_from_kind_and_provenance(self) -> None:
        b = _block(KIND_EVIDENCE, provenance={"asset_id": "a1", "segment_id": "s1"})
        assert b.id == f"{KIND_EVIDENCE}:asset_id=a1|segment_id=s1"

    def test_id_without_provenance(self) -> None:
        b = _block("system_policy")
        assert b.id == "system_policy"


class TestPriorityOrdering:
    def test_hard_constraint_highest(self) -> None:
        assert ContextBlockPriority.HARD_CONSTRAINT > ContextBlockPriority.DELIVERY_TARGET
        assert ContextBlockPriority.DELIVERY_TARGET > ContextBlockPriority.APPROVED_DECISIONS
        assert ContextBlockPriority.APPROVED_DECISIONS > ContextBlockPriority.EVIDENCE
        assert ContextBlockPriority.EVIDENCE > ContextBlockPriority.RECENT_SUMMARY
        assert ContextBlockPriority.RECENT_SUMMARY > ContextBlockPriority.HISTORY


class TestBundleStatistics:
    def test_total_tokens(self) -> None:
        bundle = ContextBundle(blocks=(_block(tokens=100), _block(tokens=200)))
        assert bundle.total_tokens == 300

    def test_block_count(self) -> None:
        bundle = ContextBundle(blocks=(_block(), _block(), _block()))
        assert bundle.block_count == 3

    def test_blocks_by_kind_groups(self) -> None:
        b1 = _block("brief")
        b2 = _block("evidence")
        b3 = _block("evidence")
        bundle = ContextBundle(blocks=(b1, b2, b3))
        groups = bundle.blocks_by_kind()
        assert len(groups["brief"]) == 1
        assert len(groups["evidence"]) == 2

    def test_get_block_by_id(self) -> None:
        b = _block(KIND_BRIEF, provenance={"rev": 1})
        bundle = ContextBundle(blocks=(b,))
        assert bundle.get_block(b.id) is b
        assert bundle.get_block("missing") is None


class TestExcludeBlock:
    def test_exclude_removes_block(self) -> None:
        b1 = _block("brief", provenance={"rev": 1})
        b2 = _block("evidence", provenance={"seg": "s1"})
        bundle = ContextBundle(blocks=(b1, b2))
        new = bundle.exclude_block(b1.id)
        assert new.block_count == 1
        assert new.get_block(b1.id) is None
        assert new.get_block(b2.id) is not None
        # 原 bundle 不变（不可变）
        assert bundle.block_count == 2

    def test_exclude_unknown_is_noop(self) -> None:
        bundle = ContextBundle(blocks=(_block(),))
        new = bundle.exclude_block("missing")
        assert new.block_count == 1


class TestBudgetPruning:
    def test_no_prune_when_under_limit(self) -> None:
        bundle = ContextBundle(
            blocks=(_block(tokens=100),),
            budget=TokenBudget(limit=10_000, reserved_for_response=0),
        )
        pruned = bundle.with_budget()
        assert pruned.block_count == 1
        assert pruned.omitted_block_ids == ()

    def test_prune_drops_low_priority_first(self) -> None:
        """超限时低优先级（HISTORY）先被丢弃，高优先级（DELIVERY_TARGET）保留。"""
        hard = _block("policy", priority=ContextBlockPriority.HARD_CONSTRAINT, tokens=100)
        delivery = _block("brief", priority=ContextBlockPriority.DELIVERY_TARGET, tokens=100)
        history = _block("history", priority=ContextBlockPriority.HISTORY, tokens=100)
        bundle = ContextBundle(
            blocks=(hard, delivery, history),
            budget=TokenBudget(limit=250, reserved_for_response=0),  # input=250, total=300
        )
        pruned = bundle.with_budget()
        # history(100,HISTORY) 被丢；policy + brief 进（200 ≤ 250）
        ids = [b.id for b in pruned.blocks]
        assert history.id in pruned.omitted_block_ids
        assert hard.id in ids
        assert delivery.id in ids

    def test_hard_constraint_never_dropped(self) -> None:
        """HARD_CONSTRAINT 级别即使超预算也保留（§7.1：硬约束/用户锁定最优先）。"""
        hard = _block("policy", priority=ContextBlockPriority.HARD_CONSTRAINT, tokens=100)
        bundle = ContextBundle(
            blocks=(hard,),
            budget=TokenBudget(limit=50, reserved_for_response=0),  # input=50 < 100
        )
        pruned = bundle.with_budget()
        assert hard.id in [b.id for b in pruned.blocks]

    def test_needs_compression_at_60_percent(self) -> None:
        """§7.2：达 60% 预算触发结构化摘要压缩。"""
        budget = TokenBudget(limit=10_000, reserved_for_response=0)
        # input_limit=10000, compress_threshold=6000
        assert budget.compress_threshold == 6000
        under = ContextBundle(
            blocks=(_block(tokens=5000),), budget=budget,
        )
        over = ContextBundle(
            blocks=(_block(tokens=7000),), budget=budget,
        )
        assert under.needs_compression is False
        assert over.needs_compression is True


class TestToMessages:
    def test_assembles_system_and_user(self) -> None:
        sys_block = _block("policy", priority=ContextBlockPriority.HARD_CONSTRAINT, role="system")
        usr_block = _block("brief", priority=ContextBlockPriority.DELIVERY_TARGET, role="user")
        bundle = ContextBundle(blocks=(sys_block, usr_block))
        msgs = bundle.to_messages()
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_blocks_sorted_by_priority_desc(self) -> None:
        """HARD_CONSTRAINT 在消息前部，HISTORY 在后部。"""
        history = _block("history", priority=ContextBlockPriority.HISTORY, role="user")
        delivery = _block("brief", priority=ContextBlockPriority.DELIVERY_TARGET, role="user")
        hard = _block("policy", priority=ContextBlockPriority.HARD_CONSTRAINT, role="system")
        bundle = ContextBundle(blocks=(history, delivery, hard))
        msgs = bundle.to_messages()
        # hard 应在最前的 system 消息里
        assert "policy" in msgs[0]["content"]

    def test_kind_marker_in_content(self) -> None:
        b = _block("myblock", role="system")
        bundle = ContextBundle(blocks=(b,))
        msgs = bundle.to_messages()
        assert "[myblock]" in msgs[0]["content"]

    def test_dict_content_json_serialized(self) -> None:
        b = ContextBlock(
            kind="data", content={"k": "v"}, priority=ContextBlockPriority.EVIDENCE,
            token_estimate=10, why_included="t", prompt_role="user",
        )
        bundle = ContextBundle(blocks=(b,))
        msgs = bundle.to_messages()
        assert '"k"' in msgs[-1]["content"]

    def test_empty_bundle_returns_no_messages(self) -> None:
        bundle = ContextBundle()
        assert bundle.to_messages() == []


class TestTokenBudget:
    def test_input_limit_excludes_response_reserve(self) -> None:
        budget = TokenBudget(limit=10_000, reserved_for_response=2_000)
        assert budget.input_limit == 8_000

    def test_compress_threshold_60_percent(self) -> None:
        budget = TokenBudget(limit=10_000, reserved_for_response=0)
        assert budget.compress_threshold == 6_000

    def test_defaults(self) -> None:
        budget = TokenBudget()
        assert budget.limit == 32_000
        assert budget.input_limit == 28_000
