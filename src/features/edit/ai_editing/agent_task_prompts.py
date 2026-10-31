"""System-prompt templates for the autonomous AgentTaskExecutor.

Three prompts drive the plan → execute → verify lifecycle:

- **Planning** — the model receives the user's goal, the current project
  snapshot and the tool catalog, then decomposes the goal into concrete,
  tool-callable steps.
- **Verification** — after execution the model receives the original goal,
  the step outcomes and the final project snapshot to judge whether the goal
  was achieved.
- **Summary** — produces a user-facing report of what was done.

All prompts are deliberately explicit about the JSON output contract so the
executor can parse the response deterministically.  Untrusted data (project
state, tool output) is inserted as JSON-serialised text — the model cannot
use it to alter the system instructions.
"""

from __future__ import annotations

import json
from typing import Any


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def build_planning_system_prompt(
    tools: list[dict[str, Any]],
    project_snapshot: dict[str, Any],
) -> str:
    """Build the system prompt for the planning LLM call.

    Parameters
    ----------
    tools
        OpenAI-format tool definitions (``{"type":"function","function":{...}}``).
    project_snapshot
        Sanitised read-only project state from ``get_project_snapshot``.
    """

    tool_catalog = _format_tool_catalog(tools)
    snapshot_text = json.dumps(project_snapshot, ensure_ascii=False, default=str)
    return (
        "你是一个视频/图像编辑器的自主任务规划器（Autonomous Task Planner）。\n"
        "\n"
        "## 你的职责\n"
        "根据用户的目标和当前项目状态，将任务分解为可执行的步骤。\n"
        "每个步骤必须指定一个具体的工具调用。\n"
        "\n"
        "## 当前项目状态\n"
        f"{snapshot_text}\n"
        "\n"
        "## 可用工具\n"
        f"{tool_catalog}\n"
        "\n"
        "## 输出格式（严格 JSON，不要加 markdown 围栏）\n"
        "{\n"
        '  "rationale": "为什么这样分解（一句话）",\n'
        '  "steps": [\n'
        "    {\n"
        '      "description": "这一步做什么（简短描述）",\n'
        '      "tool_name": "topos.xxx",\n'
        '      "tool_args": {"参数名": "值"},\n'
        '      "expected_outcome": "执行后应该看到什么结果"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "## 规划规则\n"
        "1. 每一步只能调用一个工具。\n"
        "2. 优先使用只读工具（get_project_snapshot, search_assets 等）收集信息。\n"
        "3. 在执行写操作（apply_edit_plan）之前，先用 validate_edit_plan 校验。\n"
        "4. 步骤数量不超过 8 步。如果目标太复杂，聚焦最核心的部分。\n"
        "5. tool_args 必须符合工具的 input_schema。\n"
        "6. 只输出 JSON，不要输出任何其他文字。"
    )


def build_planning_user_prompt(
    goal: str,
    failure_context: str = "",
) -> str:
    """Build the user message for the planning call.

    ``failure_context`` is non-empty on a replan attempt — it tells the model
    why the previous plan failed so it can avoid the same mistake.
    """

    msg = f"用户目标：{goal}"
    if failure_context:
        msg += (
            f"\n\n上一次执行的失败原因：{failure_context}\n"
            "请吸取教训，调整你的计划。"
        )
    return msg


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def build_verification_system_prompt(
    goal: str,
    step_results: list[dict[str, Any]],
    final_snapshot: dict[str, Any],
) -> str:
    """Build the system+user combined prompt for the verification LLM call.

    The model receives the original goal, what was done (step results) and the
    current project state.  It must return a JSON verdict.
    """

    steps_text = json.dumps(step_results, ensure_ascii=False, default=str)
    snapshot_text = json.dumps(final_snapshot, ensure_ascii=False, default=str)
    return (
        "你是一个任务验证器（Task Verifier）。你的职责是判断一个自主任务是否达成目标。\n"
        "\n"
        f"## 原始目标\n{goal}\n"
        "\n"
        f"## 执行的步骤及结果\n{steps_text}\n"
        "\n"
        f"## 当前项目状态\n{snapshot_text}\n"
        "\n"
        "## 输出格式（严格 JSON，不要加 markdown 围栏）\n"
        "{\n"
        '  "achieved": true 或 false,\n'
        '  "notes": "判断理由（一句话）"\n'
        "}\n"
        "\n"
        "## 判断标准\n"
        "- 如果所有步骤都成功且最终状态符合目标 → achieved=true\n"
        "- 如果有步骤失败或最终状态不符合目标 → achieved=false\n"
        "- 只输出 JSON，不要输出任何其他文字。"
    )


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


def build_summary_prompt(
    goal: str,
    step_results: list[dict[str, Any]],
    verification_notes: str,
) -> str:
    """Build the prompt for generating a user-facing summary."""

    steps_text = json.dumps(step_results, ensure_ascii=False, default=str)
    return (
        "请用中文为用户生成一份简洁的执行报告。\n"
        "\n"
        f"## 用户目标\n{goal}\n"
        "\n"
        f"## 执行的步骤及结果\n{steps_text}\n"
        "\n"
        f"## 验证结论\n{verification_notes}\n"
        "\n"
        "## 要求\n"
        "- 用 2-4 句话总结你做了什么、结果如何。\n"
        "- 如果有失败，说明原因。\n"
        "- 不要重复每一步的细节，只说关键信息。"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _format_tool_catalog(tools: list[dict[str, Any]]) -> str:
    """Format OpenAI tool definitions into a concise text catalog."""

    lines: list[str] = []
    for tool in tools:
        func = tool.get("function", tool)
        name = func.get("name", "?")
        desc = func.get("description", "")
        params = func.get("parameters", {})
        required = params.get("required", [])
        props = params.get("properties", {})
        param_parts = [
            f"{k}:{v.get('type', '?')}" for k, v in props.items()
        ]
        req_marker = " (必填)" if required else ""
        lines.append(f"- **{name}**{req_marker}: {desc}")
        if param_parts:
            lines.append(f"  参数: {', '.join(param_parts)}")
    return "\n".join(lines)


__all__ = [
    "build_planning_system_prompt",
    "build_planning_user_prompt",
    "build_verification_system_prompt",
    "build_summary_prompt",
]
