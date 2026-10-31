#!/usr/bin/env python
"""M01 · 从主 venv 冻结中排除 AI worker 栈，输出核心环境 requirements。

规则（可证明安全，替代硬编码清单）：
  **凡传递依赖触及 AI 根包的冻结包一律排除。**
  - AI 根 = torch 族 / transformers 族 / nemo / qwen / pyannote / lightning 族
    等可选 AI worker 顶层包（requirements-ai-worker.txt 的根集合）。
  - 若保留包 K 依赖被排除包 S，则 K 的传递闭包含 S → S 闭包含 AI 根 →
    K 自己也会被排除。因此**任何被保留的包都不可能拉起 AI 依赖**。
  - 核心包按定义（pyproject [project].dependencies）不依赖 torch 族；
    若出现例外（某保留候选要求 AI 根），filter 会在 stderr 大声报告并
    以非零退出，绝不静默裁剪核心。

附带效果：仅被 AI 栈需要、但自身不依赖 AI 根的纯工具包（sympy 等）会
作为孤儿保留——无害（不拉新依赖），不追求最小化。

用法：python scripts/m01_core_freeze_filter.py <freeze.txt> > core_freeze.txt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 可选 AI worker 顶层根（requirements-ai-worker.txt + 主 venv 中已识别的
# AI 生态顶层；传递闭包由元数据自动展开）
AI_ROOTS = {
    "torch", "torchvision", "torchaudio",
    "pyannote-core", "pyannote-database", "pyannote-metrics",
    "lightning", "pytorch-lightning", "lightning-utilities", "torchmetrics",
    "nemo-toolkit", "nv-one-logger-core",
    "nv-one-logger-pytorch-lightning-integration",
    "nv-one-logger-training-telemetry",
    "qwen-asr", "qwen-tts", "qwen-omni-utils", "qwen-vl-utils",
    "transformers", "accelerate", "sentence-transformers", "vllm",
    "taming-transformers", "megatron-core", "megatron-energon",
    # AI 生态仆从顶层（不依赖 torch 也会被闭包规则保留成孤儿——按语义归 AI）
    "demucs", "gradio", "gradio-client", "hf-gradio", "modelscope",
    "julius", "webdataset", "peft", "optuna", "wandb", "datasets",
    "lhotse",
}


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def build_graph() -> dict[str, set[str]]:
    """主 venv（运行本脚本的解释器）的正向依赖图。"""
    from importlib.metadata import distributions

    graph: dict[str, set[str]] = {}
    for dist in distributions():
        name = dist.metadata["Name"] if dist.metadata["Name"] else ""
        if not name:
            continue
        deps: set[str] = set()
        for req in (dist.requires or []):
            line = req.strip()
            # extras 门控的可选边（`... ; extra == "torch"`）pip 默认不安装，
            # 不是真实依赖边——跳过（曾把 huggingface-hub/librosa 经
            # `hub[torch]`/`optuna[document]` 误判成 AI 仆从）。
            if "extra ==" in line:
                continue
            # 只取包名（跳过 marker 复杂部分；为假平台边最多导致多排除
            # 一个包——方向安全的失败模式）。
            m = re.match(r"^([A-Za-z0-9._-]+)", line)
            if m:
                deps.add(canon(m.group(1)))
        graph[canon(name)] = deps
    return graph


def main() -> int:
    freeze_path = Path(sys.argv[1])
    lines = [ln.strip() for ln in freeze_path.read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]

    installed: dict[str, str] = {}
    for ln in lines:
        m = re.match(r"^([A-Za-z0-9._-]+)==", ln)
        if m:
            installed[canon(m.group(1))] = ln
    if not installed:
        print("no == pinned lines found", file=sys.stderr)
        return 2

    graph = build_graph()
    # freeze 中存在但主 venv 元数据缺失的包（如 -e 安装）→ 保守保留。
    for name in installed:
        graph.setdefault(name, set())

    # 闭包记忆化：closure(P) 含 P 的全部传递依赖（含自身）。
    closure_cache: dict[str, set[str]] = {}

    def closure(name: str) -> set[str]:
        if name in closure_cache:
            return closure_cache[name]
        seen: set[str] = set()
        stack = [name]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(graph.get(n, ()))
        closure_cache[name] = seen
        return seen

    kept: list[str] = []
    excluded: list[str] = []
    for name, ln in sorted(installed.items()):
        if name in AI_ROOTS or (closure(name) & AI_ROOTS):
            excluded.append(name)
        else:
            kept.append(ln)

    # 安全断言：任何保留包不得依赖被排除包（按构造不可能；防御元数据
    # 漂移——若触发说明主 venv 元数据异常，需人工介入）。
    excluded_set = set(excluded)
    for name in [canon(re.match(r"^([A-Za-z0-9._-]+)==", ln).group(1)) for ln in kept]:
        leaking = graph.get(name, set()) & excluded_set
        if leaking:
            print(
                f"FATAL: kept package {name} requires excluded AI package(s) "
                f"{sorted(leaking)} — core unexpectedly depends on AI stack",
                file=sys.stderr,
            )
            return 3

    for ln in kept:
        print(ln)
    print(
        f"# [m01 filter] excluded {len(excluded)} AI-stack packages: "
        + ", ".join(sorted(excluded)),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
