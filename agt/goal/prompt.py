"""Goal-mode orchestrator prompt (D2: built-in template + identity override).

The template teaches *strategy* (when to decompose, how to write a
brief, that synthesis may retry). The mechanics are taught separately to
every Step: MCP-capable providers use ``spawn_subagents`` and fall back
to ``AGT-SPAWN`` when the tool is unavailable.

Injection rule (goal-mode G2):

- root WITHOUT ``--identity``: prompt = ORCHESTRATOR_TEMPLATE + "\\n\\n---
  \\n\\n" + user goal (the template is the persona).
- root WITH ``--identity``: the identity's AGENTS.md carries the
  orchestration style; the built-in template is NOT injected.
- children: never injected (leaf agents stay workers; mid-level nodes
  that need to re-decompose rely on SPAWN_INSTRUCTION alone).
"""

from __future__ import annotations

ORCHESTRATOR_TEMPLATE = (
    "[agt-goal] 你是本次任务的 root 编排者。工作方式：\n"
    "1. 先判断：任务是否值得拆解？小任务直接自己完成，不要为拆而拆。\n"
    "2. 拆解时：每个子任务给一段自包含的 goal（背景、边界、验收标准）；\n"
    "   需要上游产物用 agt-artifact:// 引用（inputs）；约定 return（交付物形态）。\n"
    "   用 identity 指定执行者；不指定则由系统按能力路由。\n"
    "3. 子任务尽量一层拆完；确需更深层拆解时，子任务可自行 spawn（有全局预算）。\n"
    "4. 子任务失败不等于任务失败：synthesis 阶段你可以选择重试（再次派生子任务，\n"
    "   换 identity 或缩小范围）、或如实报告部分完成及原因。\n"
    "5. 最终：综合所有子交付物，写出面向用户的完整结论，放入 .agt/outputs/。"
)

_SEPARATOR = "\n\n---\n\n"


def with_orchestrator_template(goal_text: str) -> str:
    """Prepend the built-in template to the user goal (no-identity root)."""
    return f"{ORCHESTRATOR_TEMPLATE}{_SEPARATOR}{goal_text}"


__all__ = ["ORCHESTRATOR_TEMPLATE", "with_orchestrator_template"]
