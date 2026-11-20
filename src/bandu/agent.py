"""教学 Agent 装配。"""

from pydantic_ai import Agent

from .prompts import INSTRUCTIONS


def build_agent() -> Agent:
    """构建教学智能体（模型在 realtime 会话时注入）。"""
    # S1 能力集（W2 验证 realtime 兼容后按需启用）：
    #   Memory(FileMemoryStore('.bandu-memory'))        # 跨会话记忆
    #   SpendLimits(budgets=[...])                      # 成本上限
    #   InputGuardrail(...) / OutputGuardrail(...)      # 内容安全
    return Agent(instructions=INSTRUCTIONS)
