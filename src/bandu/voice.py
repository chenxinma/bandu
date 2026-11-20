"""麦克风/扬声器 I/O（sounddevice：16kHz 入 / 24kHz 出，单声道 PCM16）。"""

import asyncio
from collections.abc import AsyncIterator

import sounddevice as sd

INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
CHANNELS = 1
BLOCK_SAMPLES = 1600  # 16kHz 下约 100ms/帧


async def microphone_chunks() -> AsyncIterator[bytes]:
    """采集麦克风 PCM 帧（16kHz/16bit/mono）。"""
    loop = asyncio.get_running_loop()
    with sd.RawInputStream(
        samplerate=INPUT_SAMPLE_RATE, channels=CHANNELS, dtype="int16"
    ) as stream:
        while True:
            data, _ = await loop.run_in_executor(None, stream.read, BLOCK_SAMPLES)
            yield bytes(data)


async def play_audio(chunks: AsyncIterator[bytes]) -> None:
    """播放扬声器 PCM 帧（24kHz/16bit/mono）。"""
    loop = asyncio.get_running_loop()
    with sd.RawOutputStream(
        samplerate=OUTPUT_SAMPLE_RATE, channels=CHANNELS, dtype="int16"
    ) as stream:
        async for chunk in chunks:
            await loop.run_in_executor(None, stream.write, chunk)
