"""Token 估算器（M5 / §7.2）。

计划 §7.2：只有 provider 返回真实 usage 才显示精确 token/费用；未知时显示"估算"。
本模块提供**无依赖**的启发式 token 估算（不依赖 tiktoken），用于 ContextBundle 的
预算/优先级/压缩触发判定（§7.1 每块有 token 估算；§7.2 达 60% 预算触发结构化摘要）。

设计：

- **中英混合启发式**：英文 ~4 char/token（OpenAI BPE 经验值）；中文按 ~1.5 char/token
  （UTF-8 多字节，BPE 切分更碎）。混合文本按字符类型加权。
- **结构化数据**：dict/list 先 JSON 序列化再估，额外 +2 token/键（结构开销）。
- **可选 tiktoken**：若环境装了 tiktoken，``accurate=True`` 时用它精确计数（不影响默认路径）。
- **保守上取整**：估算结果向上取整，避免低估导致超预算。

不替代 provider 真实 usage：真实 usage 在 LLMClient 层捕获（M5-5 一并接）。本估算器
只用于"塞 prompt 前的预算决策"和"UI 显示估算"。
"""

from __future__ import annotations

import json
import math
from typing import Any

try:
    import tiktoken as _tiktoken  # type: ignore
    _HAS_TIKTOKEN = True
except ImportError:
    _tiktoken = None  # type: ignore
    _HAS_TIKTOKEN = False


# 启发式系数（OpenAI cl100k_base 经验值，中英混合场景）
_LATIN_CHARS_PER_TOKEN = 4.0       # 英文/数字/标点 ~4 char/token
_CJK_CHARS_PER_TOKEN = 1.5         # 中文/日文/韩文 ~1.5 char/token（BPE 切得更碎）
_STRUCT_OVERHEAD_PER_KEY = 2       # JSON 键/括号的结构开销（token）


def _is_cjk(ch: str) -> bool:
    """字符是否为 CJK（中日韩）统一表意文字或相关范围。"""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF       # CJK 统一表意
        or 0x3040 <= cp <= 0x30FF    # 平假名/片假名
        or 0xAC00 <= cp <= 0xD7AF    # 韩文音节
        or 0x3400 <= cp <= 0x4DBF    # CJK 扩展 A
        or 0xFF00 <= cp <= 0xFFEF    # 全角字符
    )


def estimate_text_tokens(text: str) -> int:
    """启发式估算一段文本的 token 数（无依赖）。

    中英混合加权：拉丁字符按 ~4 char/token，CJK 按 ~1.5 char/token。
    向上取整。空串返回 0。
    """
    if not text:
        return 0
    cjk_count = 0
    latin_count = 0
    for ch in text:
        if ch.isspace():
            # AUD-14706：空白字符（ASCII 空格/换行/制表及 Unicode 空白）不计入任何
            # 桶——此前 ASCII 空白落入"其他 unicode"分支按 CJK 量级（1.5 char/token）
            # 计，英文文本估算系统性偏高约 25-30%，过早触发 §7.1 的 60% 压缩。
            continue
        if _is_cjk(ch):
            cjk_count += 1
        elif ch.isascii():
            latin_count += 1
        else:
            # 其他 unicode（emoji/重音字母）按 CJK 量级估
            cjk_count += 1
    heuristic = cjk_count / _CJK_CHARS_PER_TOKEN + latin_count / _LATIN_CHARS_PER_TOKEN
    # 保底：不超过 total_chars/4 太离谱时回退（纯 CJK 时 heuristic 会 > total_chars/4，合理）
    total_chars = len(text)
    estimate = max(heuristic, total_chars / _LATIN_CHARS_PER_TOKEN * 0.5)
    return max(1, math.ceil(estimate))


def estimate_json_tokens(data: Any) -> int:
    """估算结构化数据（dict/list/可序列化）序列化后的 token 数。

    JSON 序列化 + 文本估算 + 键结构开销。
    """
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # 不可序列化 → fallback 到 str()
        text = str(data)
    base = estimate_text_tokens(text)
    # 结构开销：键数量 × 2（每个键名 + 引号/冒号的开销）
    if isinstance(data, dict):
        base += len(data) * _STRUCT_OVERHEAD_PER_KEY
    elif isinstance(data, (list, tuple)):
        base += len(data)  # 每个元素 ~1 token 括号/逗号
    return base


def count_tokens_accurate(text: str, model: str = "gpt-4") -> int | None:
    """用 tiktoken 精确计数（若可用）。

    返回 None 表示 tiktoken 未安装或模型不支持（调用方应回退到 estimate_text_tokens）。
    默认不使用——本模块的契约是"无依赖估算"；精确计数仅在 provider 返回真实 usage 缺失
    且需要更准估算时用。
    """
    if not _HAS_TIKTOKEN or not text:
        return None
    try:
        enc = _tiktoken.encoding_for_model(model)
    except (KeyError, ValueError):
        try:
            enc = _tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None
    return len(enc.encode(text))


def estimate_tokens(value: Any) -> int:
    """统一入口：字符串走文本估算，其他走 JSON 估算。

    这是 ContextBundle 各 block 计算 token 估算的默认入口（§7.1 每块有 token 估算）。
    """
    if isinstance(value, str):
        return estimate_text_tokens(value)
    return estimate_json_tokens(value)


__all__ = [
    "estimate_text_tokens",
    "estimate_json_tokens",
    "estimate_tokens",
    "count_tokens_accurate",
]
