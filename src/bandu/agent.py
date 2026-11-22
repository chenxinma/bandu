"""教学 Agent：instructions 装配 + 流式按句切分（切好的句子直接喂 TTS）。"""

from collections.abc import AsyncIterator

import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .prompts import INSTRUCTIONS
from .provider import LLM_BASE_URL, LLM_MODEL, LLM_SETTINGS, get_api_key

pydantic_ai.BANNER_ENABLED = False  # 语音会话自己拥有屏幕输出，不要 banner 插在字幕中间
SENTENCE_ENDS = "。！？；!?;\n"


def build_agent() -> Agent:
    """构建教学智能体（模型在 stream_sentences 里注入）。"""
    # S1 能力集（按需启用）：
    #   Memory(FileMemoryStore('.bandu-memory'))        # 跨会话记忆
    #   SpendLimits(budgets=[...])                      # 成本上限
    #   InputGuardrail(...) / OutputGuardrail(...)      # 内容安全
    return Agent(instructions=INSTRUCTIONS)


def build_model() -> OpenAIChatModel:
    """指向 Token Plan 的 OpenAI 兼容文本模型。"""
    return OpenAIChatModel(
        LLM_MODEL, provider=OpenAIProvider(base_url=LLM_BASE_URL, api_key=get_api_key())
    )


async def stream_sentences(text: str, history: list[ModelMessage]) -> AsyncIterator[str]:
    """流式生成回复并按句切开，让 TTS 不必等整段。

    `history` 就地累积上下文：轮次制下服务端不记对话，得自己攒。
    """
    agent = build_agent()
    buffer = ""
    async with agent.run_stream(
        text, model=build_model(), model_settings=LLM_SETTINGS, message_history=history
    ) as result:
        async for delta in result.stream_text(delta=True):
            buffer += delta
            pieces, buffer = cut_sentences(buffer)
            for piece in pieces:  # async 生成器里不能用 yield from
                yield piece
        history[:] = result.all_messages()
    if buffer.strip():
        yield buffer.strip()


def cut_sentences(buffer: str) -> tuple[list[str], str]:
    """切出已完成的句子，剩下半句留在缓冲里。"""
    pieces: list[str] = []
    start = 0
    for index, char in enumerate(buffer):
        if char in SENTENCE_ENDS:
            piece = buffer[start : index + 1].strip()
            if piece:
                pieces.append(piece)
            start = index + 1
    return pieces, buffer[start:]
