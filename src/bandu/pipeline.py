"""方案 B（学习通道 / 弱网降级）：本地 VAD 录音 → 整段 ASR → 文本 Agent → 流式 TTS → 播放。

与方案 A（realtime 端到端）的取舍：ASR/LLM/TTS 各自可换可调、教学话术能逐字控制，
代价是一问一答——不支持抢话打断（要打断用默认的 `bandu`）。
"""

import array
import asyncio
import base64
import io
import json
import math
import ssl
import time
import uuid
import wave
from collections import deque
from collections.abc import AsyncIterator, Sequence

import pydantic_ai
import requests
import websockets
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .agent import build_agent
from .provider import get_api_key
from .rules import Intent, canned_reply, classify
from .session_log import SessionLog
from .state import SessionState
from .voice import BLOCK_SAMPLES, INPUT_SAMPLE_RATE, microphone_chunks, play_audio

BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com"
ASR_URL = f"{BASE_URL}/api/v1/services/aigc/multimodal-generation/generation"
TTS_WS_URL = "wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference/"
LLM_BASE_URL = f"{BASE_URL}/compatible-mode/v1"

ASR_MODEL = "qwen-audio-3.0-asr-flash"
TTS_MODEL = "qwen-audio-3.0-tts-plus"
# 实测首字 0.40s、输出干净；qwen3.8-flash 0.57s 且会反问学生已给的条件。
LLM_MODEL = "qwen3.6-flash"
# qwen3 系列默认开思考，首字会从 0.4s 掉到 4.9s，必须显式关掉。
LLM_SETTINGS = {"extra_body": {"enable_thinking": False}}
VOICE = "longanqian"
OUTPUT_SAMPLE_RATE = 24_000

FRAME_SECONDS = BLOCK_SAMPLES / INPUT_SAMPLE_RATE  # 0.1s/帧
pydantic_ai.BANNER_ENABLED = False  # 语音 demo 自己拥有屏幕输出，不要 banner 插在字幕中间
_HTTP = requests.Session()  # ASR 在关键路径上，复用连接省掉每轮一次 TLS 握手
# shortcut: 底噪自适应只能救“阈值不对”，救不了喳杂环境；换硬件先看 [收音中] 打的底噪/阈值。
NOISE_WINDOW = 30  # 用最近 3s 的安静帧估底噪
NOISE_MARGIN = 3.0  # 超过底噪几倍算开口
SPEECH_RMS_MIN = 100  # 绝对下限：安静房间里别把呼吸声当说话
PRE_ROLL_FRAMES = 2  # 连开口前的两帧一起送，别削掉第一个字
SILENCE_FRAMES = 8  # 连续 800ms 静音收尾（原来 600ms 会把带停顿的长句切断）
MAX_FRAMES = 200  # 单句上限 20s
ECHO_GUARD_SECONDS = 0.2  # 播完先丢几帧，避免把喇叭里的余音当成学生开口

# 整段 ASR 的服务端排队抖动极大（同一句实测 0.29–10.5s，与音频长短无关），只能对冲：
# 同时发几份取最先回来的。每份都是完整计费，3 份 ≈ 3 倍 ASR 时长；改回 1 就是单发。
ASR_FANOUT = 3

_SENTENCE_ENDS = "。！？；!?;\n"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_api_key()}"}


def _rms(chunk: bytes) -> float:
    """一帧 int16 PCM 的均方根音量。"""
    samples = array.array("h")
    samples.frombytes(chunk)
    return math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    """裸 PCM → WAV 容器（ASR 接口要 wav）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buf.getvalue()


def _speech_threshold(noise: Sequence[float]) -> float:
    """由环境底噪算开口阈值：取 20 分位抵抗混入的人声，再乘倍数，并有绝对下限。"""
    if not noise:
        return float(SPEECH_RMS_MIN)
    ordered = sorted(noise)
    return max(SPEECH_RMS_MIN, ordered[len(ordered) // 5] * NOISE_MARGIN)


async def record_utterance(chunks: AsyncIterator[bytes]) -> tuple[bytes, float]:
    """能量 VAD 收一整句：底噪自适应定阈值，开口后连续静音才收尾。返回 (PCM, 说话结束时刻)。"""
    print("[听] 请说话…", flush=True)
    noise: deque[float] = deque(maxlen=NOISE_WINDOW)
    pre: deque[bytes] = deque(maxlen=PRE_ROLL_FRAMES)
    frames: list[bytes] = []
    threshold = float(SPEECH_RMS_MIN)
    quiet = 0
    async for chunk in chunks:
        rms = _rms(chunk)
        if not frames:  # 还没开口：一边估底噪，一边留住开口前的余音
            if rms < threshold:
                noise.append(rms)
                threshold = _speech_threshold(noise)
                pre.append(chunk)
                continue
            floor = sorted(noise)[len(noise) // 5] if noise else 0.0
            print(f"[收音中] 底噪 {floor:.0f} → 阈值 {threshold:.0f}", flush=True)
            frames.extend(pre)  # 带上开口前的余音，第一个字才不会被削掉
            frames.append(chunk)
            continue
        frames.append(chunk)
        quiet = 0 if rms >= threshold else quiet + 1
        if quiet >= SILENCE_FRAMES or len(frames) >= MAX_FRAMES:
            # 说话真正结束的时刻要扣掉尾静音，延迟口径才和方案 A 一致。
            spoken = (len(frames) - quiet) * FRAME_SECONDS
            print(f"[收完] {spoken:.1f}s 音频，识别中…", flush=True)
            return b"".join(frames[: len(frames) - quiet]), time.monotonic() - quiet * FRAME_SECONDS
    raise RuntimeError("麦克风已结束")


def transcribe(pcm: bytes) -> str:
    """整段音频转文字（Token Plan 的 ASR 只有非流式接口）。"""
    data = f"data:audio/wav;base64,{base64.b64encode(_wav(pcm, INPUT_SAMPLE_RATE)).decode()}"
    resp = _HTTP.post(
        ASR_URL,
        headers={**_headers(), "Content-Type": "application/json"},
        json={
            "model": ASR_MODEL,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"data": data}}],
                    }
                ]
            },
            "parameters": {"format": "wav"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("text") or body.get("sentence", {}).get("text") or ""


async def transcribe_hedged(pcm: bytes) -> str:
    """同时发 ASR_FANOUT 份同样的请求，取最先回来的那份，剩下的作废。"""
    pending = {asyncio.create_task(asyncio.to_thread(transcribe, pcm)) for _ in range(ASR_FANOUT)}
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled() or task.exception() is not None:
                    continue  # 一份挂了不拖累其他份
                if text := task.result().strip():
                    return text
        return ""
    finally:
        for task in pending:
            task.cancel()


async def _agent_sentences(text: str, history: list[ModelMessage]) -> AsyncIterator[str]:
    """文本 Agent 流式输出，按句切开让 TTS 不必等整段；history 跨轮累积上下文。"""
    agent = build_agent()
    model = OpenAIChatModel(
        LLM_MODEL, provider=OpenAIProvider(base_url=LLM_BASE_URL, api_key=get_api_key())
    )
    buffer = ""
    async with agent.run_stream(
        text, model=model, model_settings=LLM_SETTINGS, message_history=history
    ) as result:
        async for delta in result.stream_text(delta=True):
            buffer += delta
            pieces, buffer = _cut_sentences(buffer)
            for piece in pieces:
                yield piece
        history[:] = result.all_messages()
    if buffer.strip():
        yield buffer.strip()


def _cut_sentences(buffer: str) -> tuple[list[str], str]:
    """切出已完成的句子，剩下半句留在缓冲里。"""
    pieces: list[str] = []
    start = 0
    for index, char in enumerate(buffer):
        if char in _SENTENCE_ENDS:
            piece = buffer[start : index + 1].strip()
            if piece:
                pieces.append(piece)
            start = index + 1
    return pieces, buffer[start:]


async def synthesize(sentences: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """流式 TTS：句子一到就喂进去，产出 24kHz PCM（首包快慢全看这里）。"""
    task_id = uuid.uuid4().hex

    def frame(action: str, text: str | None = None) -> str:
        payload: dict[str, object] = {"input": {"text": text} if text is not None else {}}
        if action == "run-task":
            payload = {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": TTS_MODEL,
                "parameters": {
                    "text_type": "PlainText",
                    "voice": VOICE,
                    "format": "pcm",
                    "sample_rate": OUTPUT_SAMPLE_RATE,
                },
                "input": {},
            }
        return json.dumps(
            {
                "header": {"action": action, "task_id": task_id, "streaming": "duplex"},
                "payload": payload,
            }
        )

    async with websockets.connect(
        TTS_WS_URL, ssl=ssl.create_default_context(), additional_headers=_headers(), max_size=None
    ) as ws:
        audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        started = asyncio.Event()

        async def receive() -> None:
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        audio.put_nowait(message)
                        continue
                    header = json.loads(message)["header"]
                    if header["event"] == "task-started":
                        started.set()
                    elif header["event"] == "task-finished":
                        return
                    elif header["event"] == "task-failed":
                        raise RuntimeError(f"TTS 失败：{header.get('error_message')}")
            finally:
                audio.put_nowait(None)  # 无论正常结束还是报错，都让播放侧收工

        await ws.send(frame("run-task"))
        receiver = asyncio.create_task(receive())
        await asyncio.wait_for(started.wait(), timeout=15)
        async for sentence in sentences:
            await ws.send(frame("continue-task", sentence))
        await ws.send(frame("finish-task"))
        while (chunk := await audio.get()) is not None:
            yield chunk
        await receiver


async def _handle_turn(
    chunks: AsyncIterator[bytes],
    log: SessionLog,
    marks: dict[str, float],
    history: list[ModelMessage],
) -> None:
    """一轮问答：录音 → ASR → 规则/Agent → 流式 TTS → 播放，并把各阶段耗时打到屏幕。"""
    pcm, speech_end = await record_utterance(chunks)
    marks["speech_end"] = speech_end
    text = await transcribe_hedged(pcm)
    marks["asr"] = time.monotonic()
    if not text.strip():
        print("（没听清，再说一次？）", flush=True)
        return

    intent = classify(text)
    canned = canned_reply(intent) if intent in (Intent.ASK_ANSWER, Intent.OFF_TOPIC) else None
    log.user_turn(text, intent, rule_triggered=canned is not None, since=speech_end)
    print(f"你: {text}" + ("   [规则硬兜底]" if canned else ""), flush=True)

    async def sentences() -> AsyncIterator[str]:
        if canned is not None:  # 命中硬规则：不过 LLM，预置话术逐字播报
            print(f"伴读: {canned}", flush=True)
            yield canned
            return
        print("伴读: ", end="", flush=True)
        async for sentence in _agent_sentences(text, history):
            marks.setdefault("llm_first", time.monotonic())
            print(sentence, end="", flush=True)  # 字幕跟着 LLM 流式上屏，不等播放
            yield sentence
        print(flush=True)

    async def audio() -> AsyncIterator[bytes]:
        async for chunk in synthesize(sentences()):
            if "tts_first" not in marks:
                marks["tts_first"] = time.monotonic()
                log.first_audio()
            yield chunk

    await play_audio(audio())
    await asyncio.sleep(ECHO_GUARD_SECONDS)
    for _ in range(int(ECHO_GUARD_SECONDS / FRAME_SECONDS)):  # 丢掉喇叭余音，别自己打断自己
        await anext(chunks)
    print(_timings(marks), flush=True)


def _timings(marks: dict[str, float]) -> str:
    """本轮延迟摘要，全部以“学生说完”为零点，便于和方案 A 同口径对比。"""
    end = marks["speech_end"]
    first_sentence = marks.get("llm_first", marks["asr"])
    parts = [f"ASR {_ms(marks['asr'], end)}", f"首句 {_ms(first_sentence, end)}"]
    if "tts_first" in marks:
        parts.append(f"开口 {_ms(marks['tts_first'], end)}")
    return "  [" + " · ".join(parts) + "]"


def _ms(moment: float, zero: float) -> str:
    return f"{round((moment - zero) * 1000)}ms"


async def run() -> None:
    """方案 B 主循环：一直听，说完一句答一句，Ctrl+C 结束。"""
    log = SessionLog(f"{SessionState().session_id}-pipeline")
    print(
        f"方案 B（ASR + 文本 Agent + 流式 TTS，不支持抢话打断）  对话日志：{log.path}", flush=True
    )
    chunks = microphone_chunks()
    history: list[ModelMessage] = []  # 方案 B 的对话上下文得自己攒，服务端不记
    while True:
        await _handle_turn(chunks, log, {}, history)
