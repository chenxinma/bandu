"""整段 ASR：Token Plan 只有非流式接口，靠请求对冲压服务端排队抖动。

上行音频来自浏览器（16kHz/mono/PCM16），这里包成 WAV 再整段上传。
"""

import asyncio
import base64
import io
import wave
from typing import Any

import requests

from .provider import ASR_MODEL, ASR_URL, INPUT_SAMPLE_RATE, auth_headers

# 整段 ASR 的服务端排队抖动极大（同一句实测 0.29–10.5s，与音频长短无关），只能对冲：
# 同时发几份取最先回来的。每份都是完整计费，3 份 ≈ 3 倍 ASR 时长；改回 1 就是单发。
ASR_FANOUT = 3
ASR_TIMEOUT = 60
_HTTP = requests.Session()  # ASR 在关键路径上，复用连接省掉每轮一次 TLS 握手


def wav(pcm: bytes, sample_rate: int = INPUT_SAMPLE_RATE) -> bytes:
    """裸 PCM → WAV 容器（ASR 接口要 wav）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buf.getvalue()


def extract_text(body: dict[str, Any]) -> str:
    """从 ASR 响应里取转写：兼容 `output.text` / `output.sentence.text` 与扁平结构。"""
    output = body.get("output") or {}
    candidates = (
        output.get("text"),
        (output.get("sentence") or {}).get("text"),
        body.get("text"),
        (body.get("sentence") or {}).get("text"),
    )
    return next((text for text in candidates if isinstance(text, str) and text.strip()), "")


def transcribe(pcm: bytes) -> str:
    """整段音频转文字（阻塞 HTTP，调用方负责放线程池）。"""
    data = f"data:audio/wav;base64,{base64.b64encode(wav(pcm)).decode()}"
    resp = _HTTP.post(
        ASR_URL,
        headers={**auth_headers(), "Content-Type": "application/json"},
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
        timeout=ASR_TIMEOUT,
    )
    resp.raise_for_status()
    return extract_text(resp.json())


async def transcribe_hedged(pcm: bytes, fanout: int = ASR_FANOUT) -> str:
    """同时发 fanout 份同样的请求，取最先回来的那份，剩下的作废。

    几份全挂（Key 错 / 断网）时把异常抛出去，别把配置问题伪装成“没听清”。
    """
    pending = {asyncio.create_task(asyncio.to_thread(transcribe, pcm)) for _ in range(fanout)}
    failures: list[BaseException] = []
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                if (exc := task.exception()) is not None:
                    failures.append(exc)  # 一份挂了不拖累其他份
                    continue
                if text := task.result().strip():
                    return text
        if failures:
            raise failures[0]
        return ""
    finally:
        for task in pending:
            task.cancel()
