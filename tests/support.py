"""测试公用件：合成 PCM 帧、假下行通道、ASR/LLM/TTS 打桩。"""

import array
import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from bandu.provider import FRAME_SAMPLES
from bandu.session import Inbound, VoiceSession
from bandu.session_log import SessionLog
from bandu.state import SessionState

SILENCE = array.array("h", [0] * FRAME_SAMPLES).tobytes()
SPEECH = array.array("h", [4000] * FRAME_SAMPLES).tobytes()  # 远高于 SPEECH_RMS_MIN=100


def frames(kind: bytes, count: int) -> list[bytes]:
    return [kind] * count


def spoken_clip(speech: int = 5, tail: int = 8, noise: int = 3) -> list[bytes]:
    """一段能触发 VAD 收尾的上行：底噪 → 说话 → 尾静音。"""
    return [*frames(SILENCE, noise), *frames(SPEECH, speech), *frames(SILENCE, tail)]


@dataclass
class FakeDownstream:
    """记录所有下行事件与音频块的假通道。"""

    events: list[dict[str, Any]] = field(default_factory=list)
    audio: list[bytes] = field(default_factory=list)

    async def send_event(self, event: str, **fields: Any) -> None:
        self.events.append({"type": event, **fields})

    async def send_audio(self, chunk: bytes) -> None:
        self.audio.append(chunk)

    @property
    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    def one(self, event: str) -> dict[str, Any]:
        return next(item for item in self.events if item["type"] == event)

    def all(self, event: str) -> list[dict[str, Any]]:
        return [item for item in self.events if item["type"] == event]


@dataclass
class Stubs:
    """三段打桩的可控开关与调用记录。"""

    text: str = "这道题第二个条件怎么用"
    sentences: list[str] = field(default_factory=lambda: ["先看条件。", "再想一步。"])
    audio: list[bytes] = field(default_factory=lambda: [b"aa", b"bb", b"cc"])
    gate: asyncio.Event | None = None  # 设了就等它，用来把轮次卡在 thinking
    asr_calls: int = 0
    llm_calls: list[str] = field(default_factory=list)
    tts_sentences: list[str] = field(default_factory=list)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Stubs:
    """把 ASR / LLM / TTS 三段全换成假的，测试不碰网络。"""
    state = Stubs()

    async def transcribe(pcm: bytes) -> str:
        state.asr_calls += 1
        if state.gate is not None:
            await state.gate.wait()
        return state.text

    async def sentences(text: str, history: list) -> AsyncIterator[str]:
        state.llm_calls.append(text)
        for sentence in state.sentences:
            yield sentence

    async def synthesize(stream: AsyncIterator[str]) -> AsyncIterator[bytes]:
        async for sentence in stream:
            state.tts_sentences.append(sentence)
            for chunk in state.audio:
                yield chunk

    monkeypatch.setattr("bandu.session.transcribe_hedged", transcribe)
    monkeypatch.setattr("bandu.session.stream_sentences", sentences)
    monkeypatch.setattr("bandu.session.synthesize", synthesize)
    return state


def make_session(
    downstream: FakeDownstream, tmp_path, session_id: str = "sess_test"
) -> VoiceSession:
    return VoiceSession(
        downstream,
        state=SessionState(session_id=session_id),
        log=SessionLog(session_id, directory=tmp_path),
    )


async def run_session(
    session: VoiceSession, messages: Sequence[Inbound], *, settle: bool = True
) -> None:
    """把上行消息喂进会话；`settle` 会等最后一轮跑完再断开（模拟客户端不立刻关页）。"""

    async def inbound() -> AsyncIterator[Inbound]:
        for message in messages:
            yield message
        if settle:
            await session.wait_turn()

    await session.run(inbound())
