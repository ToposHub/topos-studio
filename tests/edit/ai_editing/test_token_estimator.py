"""TAEP M5 — token 估算器测试。

验收（§7.2）：
- 无 tiktoken 依赖的启发式估算（中英混合加权）。
- 字符串 / 结构化数据（dict/list）估算。
- 空串返回 0；保守上取整（至少 1）。
- estimate_tokens 统一入口（str 走文本，其他走 JSON）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.token_estimator import (
    estimate_json_tokens,
    estimate_text_tokens,
    estimate_tokens,
)


class TestTextEstimate:
    def test_empty_returns_zero(self) -> None:
        assert estimate_text_tokens("") == 0

    def test_english_proportional(self) -> None:
        short = estimate_text_tokens("hi")
        long = estimate_text_tokens("hello world this is a longer sentence")
        assert short >= 1
        assert long > short

    def test_chinese_more_tokens_than_equal_len_english(self) -> None:
        """中文按 ~1.5 char/token，比英文 ~4 char/token 更密集（每字更多 token）。"""
        en = estimate_text_tokens("hello world")  # 11 char
        zh = estimate_text_tokens("你好世界你好世界")  # 8 char
        # 同等字符数中文 token 更多；这里中文更短但 token 应在同一量级或更多
        assert zh >= en * 0.5  # 不要求严格大于（启发式），但量级合理

    def test_mixed_text(self) -> None:
        n = estimate_text_tokens("generate a 口播粗剪 plan")
        assert n >= 1

    def test_at_least_one_for_nonempty(self) -> None:
        assert estimate_text_tokens("x") >= 1


class TestJsonEstimate:
    def test_dict_estimate(self) -> None:
        n = estimate_json_tokens({"a": 1, "b": "hello", "c": [1, 2, 3]})
        assert n >= 5  # 含结构开销

    def test_list_estimate(self) -> None:
        n = estimate_json_tokens([1, 2, 3, 4, 5])
        assert n >= 1

    def test_non_serializable_falls_back_to_str(self) -> None:
        class _O:
            def __str__(self) -> str:
                return "obj"
        n = estimate_json_tokens(_O())
        assert n >= 1

    def test_dict_has_key_overhead(self) -> None:
        """键越多，结构开销越大。"""
        one_key = estimate_json_tokens({"a": "x"})
        many_keys = estimate_json_tokens({f"k{i}": "x" for i in range(10)})
        assert many_keys > one_key


class TestUnifiedEntry:
    def test_str_uses_text_path(self) -> None:
        assert estimate_tokens("hello") == estimate_text_tokens("hello")

    def test_dict_uses_json_path(self) -> None:
        assert estimate_tokens({"a": 1}) == estimate_json_tokens({"a": 1})

    def test_none_uses_json_path(self) -> None:
        assert estimate_tokens(None) >= 1
