"""语音轮次会话：一条 WS 连接 = 一场对话，严格按「听 → 想 → 说」推进。

与已下线的 realtime 自然对话的区别：**不支持抢话打断**。伴读在想/在说的时候，
上行音频直接丢弃（前端也会停止采集），学生要等这一轮播完，或点「跳过」。
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from .agent import stream_sentences
from .asr import transcribe_hedged
from .provider import FRAME_SECONDS, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE
from .rules import Intent, canned_reply, classify
from .session_log import SessionLog
from .state import SessionState, TurnPhase
from .tts import synthesize
from .vad import ECHO_GUARD_FRAMES, EnergyVAD, Utterance

# 上行消息：bytes = 16kHz PCM16 音频帧，dict = JSON 控制消息
Inbound = bytes | dict[str, Any]


class Downstream(Protocol):
    """下行通道：控制事件走 JSON 文本帧，语音走二进制帧（24kHz PCM16）。"""

    async def send_event(self, event: str, **fields: Any) -> None: ...

    async def send_audio(self, chunk: bytes) -> None: ...


def turn_timings(marks: dict[str, float]) -> dict[str, int]:
    """本轮延迟（毫秒），一律以「学生说完」为零点。"""
    zero = marks["speech_end"]
    timings: dict[str, int] = {}
    if "asr" in marks:
        timings["asr_ms"] = _ms(marks["asr"], zero)
    if (first := marks.get("llm_first", marks.get("asr"))) is not None:
        timings["first_sentence_ms"] = _ms(first, zero)
    if "tts_first" in marks:
        timings["first_audio_ms"] = _ms(marks["tts_first"], zero)
    return timings


def _ms(moment: float, zero: float) -> int:
    return round((moment - zero) * 1000)


class VoiceSession:
    """单连接会话：持有 VAD、对话上下文、日志与轮次状态机。"""

    def __init__(
        self,
        downstream: Downstream,
        *,
        state: SessionState | None = None,
        log: SessionLog | None = None,
    ) -> None:
        self.downstream = downstream
        self.state = state or SessionState()
        self.log = log or SessionLog(self.state.session_id)
        self.history: list[ModelMessage] = []  # 轮次制的上下文得自己攒，服务端不记
        self.vad = EnergyVAD()
        self.dropped_frames = 0  # 非 listening 相位丢掉的上行帧数（前端没静音时看这里）
        self._turn: asyncio.Task[None] | None = None
        self._echo_guard = 0

    async def run(self, inbound: AsyncIterator[Inbound]) -> None:
        """主循环：边收上行边推进轮次，直到客户端断开。"""
        await self._emit(
            "ready",
            session_id=self.state.session_id,
            log_path=str(self.log.path),
            audio={
                "input_sample_rate": INPUT_SAMPLE_RATE,
                "output_sample_rate": OUTPUT_SAMPLE_RATE,
                "frame_ms": round(FRAME_SECONDS * 1000),
            },
        )
        await self._set_phase(TurnPhase.LISTENING)
        try:
            async for message in inbound:
                if isinstance(message, bytes):
                    await self._on_audio(message)
                else:
                    await self._on_control(message)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """结束会话：掐掉进行中的轮次（客户端断开或主动结束时）。"""
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn

    async def wait_turn(self) -> None:
        """等当前轮次跑完（诊断与测试用；主循环不阻塞在这里）。"""
        turn = self._turn
        if turn is None:
            await asyncio.sleep(0.005)
            return
        await asyncio.gather(turn, return_exceptions=True)

    async def _on_audio(self, chunk: bytes) -> None:
        """一帧上行音频：只在 listening 相位喂 VAD，其余相位丢弃。"""
        if self.state.phase is not TurnPhase.LISTENING:
            self.dropped_frames += 1
            return
        if self._echo_guard:  # 刚播完，先丢几帧，别把喇叭余音当成学生开口
            self._echo_guard -= 1
            return
        was_speaking = self.vad.speaking
        utterance = self.vad.feed(chunk)
        if self.vad.speaking and not was_speaking:
            await self._emit(
                "speech_start",
                noise_floor=round(self.vad.noise_floor),
                threshold=round(self.vad.threshold),
            )
        if utterance is not None:
            await self._start_turn(utterance)

    async def _on_control(self, message: dict[str, Any]) -> None:
        """控制消息：`speech_end` 手动断句，`cancel` 跳过本轮。"""
        kind = message.get("type")
        if kind == "speech_end":
            if self.state.phase is not TurnPhase.LISTENING:
                return
            if (utterance := self.vad.flush()) is not None:
                await self._start_turn(utterance)
            else:
                await self._emit("unheard", reason="empty")
        elif kind == "cancel":
            if self._turn is not None and not self._turn.done():
                self._turn.cancel()
        elif kind == "ping":
            await self._emit("pong", ts=time.time())
        else:
            await self._emit("error", message=f"未知控制消息：{kind!r}", fatal=False)

    async def _start_turn(self, utterance: Utterance) -> None:
        """开一轮：轮次任务在后台跑，主循环继续读上行（才收得到 cancel / 断开）。"""
        self.vad.discard()
        if self._turn is not None and not self._turn.done():
            return  # 上一轮还没收尾：这句作废，轮次制不允许两轮重叠
        # 先同步关门再建任务：上行帧可能在轮次任务被调度前就连着到达，
        # 相位晚一步切会让它们又攒出一句来。
        self.state.phase = TurnPhase.THINKING
        task = asyncio.create_task(self._run_turn(utterance), name=f"turn-{self.state.turns + 1}")
        task.add_done_callback(self._on_turn_done)
        self._turn = task
        # 必须让出一次事件循环，让轮次任务跑进自己的 try：否则后续 cancel 可能落在
        # 它第一步 await 之前，`finally` 里的收尾（发 turn_end / 相位回到 listening）
        # 根本不会执行，会话就永久卡在 thinking 上变僵尸。
        await asyncio.sleep(0)

    def _on_turn_done(self, task: asyncio.Task[None]) -> None:
        """轮次任务兜底：取消是预期，异常在 `_run_turn` 里已发过 error 事件。

        还要防一手：任务若在进入自己的 try 之前就被取消，`finally` 不会执行，
        相位就卡在 thinking/speaking 上下不来（僵尸会话），这里补一次收尾。
        """
        if not task.cancelled():
            task.exception()  # 取一次，避免 “exception was never retrieved” 警告
        if task is not self._turn:
            return  # 新一轮已经开跑：这是上一轮迟到的回调，不归它收尾
        if self.state.phase in (TurnPhase.IDLE, TurnPhase.LISTENING):
            return  # `_end_turn` 已经收过尾
        with contextlib.suppress(RuntimeError):  # 事件循环已关（客户端断开）就算了
            asyncio.create_task(self._recover_phase())

    async def _recover_phase(self) -> None:
        """补发 turn_end 并把相位放回 listening（正常路径不会走到这里）。"""
        await self._end_turn([], {"speech_end": time.monotonic()}, cancelled=True)

    async def _run_turn(self, utterance: Utterance) -> None:
        """一轮问答：ASR → 规则/Agent → 流式 TTS，各阶段耗时随 `turn_end` 回传。"""
        marks: dict[str, float] = {"speech_end": utterance.ended_at}
        reply_parts: list[str] = []
        cancelled = False
        try:
            await self._set_phase(TurnPhase.THINKING)
            text = await transcribe_hedged(utterance.pcm)
            marks["asr"] = time.monotonic()
            if not text.strip():
                await self._emit("unheard", seconds=round(utterance.seconds, 1))
                return
            intent = classify(text)
            canned = (
                canned_reply(intent) if intent in (Intent.ASK_ANSWER, Intent.OFF_TOPIC) else None
            )
            self.log.user_turn(
                text, intent, rule_triggered=canned is not None, since=utterance.ended_at
            )
            await self._emit(
                "user_text", text=text, intent=intent.value, rule_triggered=canned is not None
            )

            await self._set_phase(TurnPhase.SPEAKING)
            # aclosing 是必须的：跳过/断开时要立刻关掉 TTS 的 WS，别把它留在后台。
            async with contextlib.aclosing(
                self._sentences(text, canned, marks, reply_parts)
            ) as sentences:
                async with contextlib.aclosing(synthesize(sentences)) as audio:
                    async for chunk in audio:
                        if "tts_first" not in marks:
                            marks["tts_first"] = time.monotonic()
                            self.log.first_audio()
                        await self.downstream.send_audio(chunk)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # 弱网/服务异常不掀掉连接，这一轮作废即可
            await self._emit("error", message=f"{type(exc).__name__}: {exc}", fatal=False)
        finally:
            await self._end_turn(reply_parts, marks, cancelled)

    async def _sentences(
        self, text: str, canned: str | None, marks: dict[str, float], reply_parts: list[str]
    ) -> AsyncIterator[str]:
        """本轮要说的句子：命中硬规则就逐字播报预置话术，否则走 Agent 流式。"""
        if canned is not None:  # 不过 LLM，预置话术原样播出
            reply_parts.append(canned)
            await self._emit("assistant_delta", text=canned, rule_triggered=True)
            yield canned
            return
        async for sentence in stream_sentences(text, self.history):
            marks.setdefault("llm_first", time.monotonic())
            reply_parts.append(sentence)
            await self._emit("assistant_delta", text=sentence, rule_triggered=False)
            yield sentence

    async def _end_turn(
        self, reply_parts: list[str], marks: dict[str, float], cancelled: bool
    ) -> None:
        """轮次收尾：落日志 → 发 turn_end → 回到 listening。收尾失败不能掀掉会话。"""
        self.state.turns += 1
        self._echo_guard = ECHO_GUARD_FRAMES
        reply = "".join(reply_parts).strip()
        timings = turn_timings(marks)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            if reply:
                self.log.assistant_turn(reply)
            await self._emit(
                "turn_end",
                turn_id=self.state.turns,
                reply=reply,
                cancelled=cancelled,
                timings=timings,
            )
            await self._set_phase(TurnPhase.LISTENING)

    async def _set_phase(self, phase: TurnPhase) -> None:
        """切相位并广播给前端（前端据此决定采不采音、按钮亮不亮）。"""
        self.state.phase = phase
        await self._emit("state", state=phase.value, turn=self.state.turns)

    async def _emit(self, event: str, **fields: Any) -> None:
        await self.downstream.send_event(event, **fields)
