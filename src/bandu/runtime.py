"""组装并运行实时语音会话。"""

import asyncio
from collections.abc import AsyncIterator

from pydantic_ai.realtime import RealtimeSession

from .agent import build_agent
from .provider import build_realtime_model
from .rules import Intent, canned_reply, classify
from .session_log import SessionLog
from .state import SessionState
from .voice import microphone_chunks, play_audio


async def _handle_transcript(session: RealtimeSession, text: str, log: SessionLog) -> None:
    """确定性规则硬兜底：命中要答案/跑题时打断并注入预置引导语，同时落对话日志。"""
    intent = classify(text)
    reply = canned_reply(intent) if intent in (Intent.ASK_ANSWER, Intent.OFF_TOPIC) else None
    log.user_turn(text, intent, rule_triggered=reply is not None)
    if reply is not None:
        await session.interrupt()
        # send(str) 是"让模型回复"的文本轮次，不加约束时模型会改写引导语，故要求逐字播报。
        await session.send(f"请一字不差地把这句话念给学生，不要改写、不要补充、不要解释：{reply}")


async def _logged_audio(chunks: AsyncIterator[bytes], log: SessionLog) -> AsyncIterator[bytes]:
    """透传模型音频，顺带打首字延迟埋点。"""
    async for chunk in chunks:
        log.first_audio()
        yield chunk


async def _run_session(session: RealtimeSession, log: SessionLog) -> None:
    mic = asyncio.create_task(session.send_audio(microphone_chunks()))
    speaker = asyncio.create_task(play_audio(_logged_audio(session.stream_audio(), log)))
    try:
        async for part in session.stream_transcripts():
            speaker_name = getattr(part, "speaker", "?")
            text = getattr(part, "transcript", "") or ""
            print(f"{speaker_name}: {text}", flush=True)
            if speaker_name == "user":
                await _handle_transcript(session, text, log)
            else:
                log.assistant_turn(text)
    finally:
        mic.cancel()
        speaker.cancel()


async def run() -> None:
    """启动实时语音对话。"""
    agent = build_agent()
    model = build_realtime_model()
    log = SessionLog(SessionState().session_id)
    print(f"对话日志：{log.path}", flush=True)
    async with agent.realtime(model).session(handle_barge_in=True) as session:
        await _run_session(session, log)
