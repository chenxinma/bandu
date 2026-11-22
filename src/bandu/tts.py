"""流式 TTS：句子一到就喂进去，产出 24kHz PCM（首包快慢全看这里）。

走 DashScope WS 任务协议（`wss://…/api-ws/v1/inference/`）：
run-task → 多次 continue-task（每句一次）→ finish-task，音频以二进制帧回来。
"""

import asyncio
import json
import ssl
import uuid
from collections.abc import AsyncIterator

import websockets

from .provider import OUTPUT_SAMPLE_RATE, TTS_MODEL, TTS_WS_URL, VOICE, auth_headers

START_TIMEOUT = 15  # task-started 等不到就是服务端没接单，别把轮次挂死


async def synthesize(sentences: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """把句子流合成 PCM16（24kHz/mono）帧流。"""
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
        TTS_WS_URL,
        ssl=ssl.create_default_context(),
        additional_headers=auth_headers(),
        max_size=None,
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
                audio.put_nowait(None)  # 无论正常结束还是报错，都让下游收工

        await ws.send(frame("run-task"))
        receiver = asyncio.create_task(receive())
        try:
            await asyncio.wait_for(started.wait(), timeout=START_TIMEOUT)
            async for sentence in sentences:
                await ws.send(frame("continue-task", sentence))
            await ws.send(frame("finish-task"))
            while (chunk := await audio.get()) is not None:
                yield chunk
        finally:
            receiver.cancel()  # 提前退出（跳过本轮/客户端断开）时别把接收协程留在后台
            await asyncio.gather(receiver, return_exceptions=True)
