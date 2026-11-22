"""教学 Agent：instructions 装配 + 流式按句切分（切好的句子直接喂 TTS）。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .knowledge import get_graph
from .prompts import INSTRUCTIONS
from .provider import LLM_BASE_URL, LLM_MODEL, LLM_SETTINGS, get_api_key

pydantic_ai.BANNER_ENABLED = False  # 语音会话自己拥有屏幕输出，不要 banner 插在字幕中间
SENTENCE_ENDS = "。！？；!?;\n"


def build_agent() -> Agent:
    """构建教学智能体（模型在 stream_sentences 里注入），挂上学科知识图谱检索工具。"""
    # S1 能力集（按需启用）：
    #   Memory(FileMemoryStore('.bandu-memory'))        # 跨会话记忆
    #   SpendLimits(budgets=[...])                      # 成本上限
    #   InputGuardrail(...) / OutputGuardrail(...)      # 内容安全
    agent = Agent(instructions=INSTRUCTIONS)
    _register_knowledge_tools(agent)
    return agent


def _register_knowledge_tools(agent: Agent) -> None:
    """挂学科知识图谱检索工具（模式见 graph/graph-schema.md）。

    查询打本地只读图库，同步调用放线程池，别卡住 TTS 的流式循环。
    """

    @agent.tool_plain
    async def search_topics(keyword: str, subject: str = "") -> list[dict[str, Any]]:
        """按名称或说明模糊检索知识点，拿到 id 后再查详情、先修或习题。

        Args:
            keyword: 知识点关键词，如「配方法」「一般现在时」。
            subject: 学科过滤：math（数学）或 english（英语），留空两个都搜。
        """
        return await asyncio.to_thread(get_graph().search_topics, keyword, subject)

    @agent.tool_plain
    async def topic_profile(topic_id: str) -> dict[str, Any] | None:
        """查一个知识点的全景：说明、掌握要求、直接先修、用到它的知识点、课标对齐和诊断题。

        Args:
            topic_id: 知识点 id（search_topics 返回，如「cnm_221」）。
        """
        return await asyncio.to_thread(get_graph().topic_profile, topic_id)

    @agent.tool_plain
    async def prerequisite_chain(topic_id: str, depth: int = 3) -> list[dict[str, Any]]:
        """沿先修关系往回找前置知识点，学生卡壳时用来定位缺了哪些基础。

        Args:
            topic_id: 知识点 id。
            depth: 往回最多几层（1-6），默认 3。
        """
        return await asyncio.to_thread(get_graph().prerequisite_chain, topic_id, depth)

    @agent.tool_plain
    async def practice_questions(
        topic_id: str, difficulty: str = "", limit: int = 3
    ) -> list[dict[str, Any]]:
        """取某个知识点的练习题（题干、答案、解析），要出题或换题时用。

        Args:
            topic_id: 知识点 id。
            difficulty: 难度过滤：basic / standard / advanced，留空不限。
            limit: 最多几题，默认 3。
        """
        return await asyncio.to_thread(get_graph().practice_questions, topic_id, difficulty, limit)


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
