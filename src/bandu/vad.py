"""能量 VAD：底噪自适应阈值 + 尾静音收尾，逐帧喂入。

轮次制的关键件——服务端靠它判断"学生说完了"，然后才进 ASR/LLM/TTS。
shortcut: 底噪自适应只能救"阈值不对"，救不了嘈杂环境；`noise_floor`/`threshold`
会随 `vad` 事件回传前端显示，换设备先看这两个数。
"""

import array
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from .provider import FRAME_SECONDS

NOISE_WINDOW = 30  # 用最近 3s 的安静帧估底噪
NOISE_MARGIN = 3.0  # 超过底噪几倍算开口
SPEECH_RMS_MIN = 100  # 绝对下限：安静房间里别把呼吸声当说话
PRE_ROLL_FRAMES = 2  # 连开口前的两帧一起送，别削掉第一个字
SILENCE_FRAMES = 8  # 连续 800ms 静音收尾（600ms 会把带停顿的长句切断）
MAX_FRAMES = 200  # 单句上限 20s
ECHO_GUARD_FRAMES = 2  # 播完先丢几帧，避免把喇叭余音当成学生开口


@dataclass(slots=True)
class Utterance:
    """收完的一句话。`ended_at` 已扣掉尾静音，是本轮所有延迟埋点的零点。"""

    pcm: bytes
    ended_at: float
    seconds: float
    truncated: bool = False


def rms(chunk: bytes) -> float:
    """一帧 int16 PCM 的均方根音量（容忍奇数字节，网络分帧不保证对齐）。"""
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) - len(chunk) % 2])
    return math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))


def speech_threshold(noise: Sequence[float]) -> float:
    """由环境底噪算开口阈值：取 20 分位抵抗混入的人声，再乘倍数，并有绝对下限。"""
    if not noise:
        return float(SPEECH_RMS_MIN)
    ordered = sorted(noise)
    return max(SPEECH_RMS_MIN, ordered[len(ordered) // 5] * NOISE_MARGIN)


class EnergyVAD:
    """喂一帧 PCM 进来，攒满一句（尾静音 / 超长 / 手动）就吐出 Utterance。

    长连接复用同一个实例：底噪窗口跨轮保留，第二轮起阈值立刻可用。
    """

    def __init__(self) -> None:
        self._noise: deque[float] = deque(maxlen=NOISE_WINDOW)
        self._pre: deque[bytes] = deque(maxlen=PRE_ROLL_FRAMES)
        self._frames: list[bytes] = []
        self._quiet = 0
        self.threshold = float(SPEECH_RMS_MIN)
        self.speaking = False

    @property
    def noise_floor(self) -> float:
        """当前底噪估计（20 分位）。"""
        return sorted(self._noise)[len(self._noise) // 5] if self._noise else 0.0

    @property
    def pending_seconds(self) -> float:
        """已攒了多长音频（含尾静音），前端"还在听"提示用。"""
        return len(self._frames) * FRAME_SECONDS

    def feed(self, chunk: bytes) -> Utterance | None:
        """喂一帧：这句话收完返回 Utterance，否则返回 None。"""
        level = rms(chunk)
        if not self._frames:  # 还没开口：一边估底噪，一边留住开口前的余音
            if level < self.threshold:
                self._noise.append(level)
                self.threshold = speech_threshold(self._noise)
                self._pre.append(chunk)
                return None
            self.speaking = True
            self._frames.extend(self._pre)  # 带上开口前的余音，第一个字才不会被削掉
            self._frames.append(chunk)
            return None
        self._frames.append(chunk)
        self._quiet = 0 if level >= self.threshold else self._quiet + 1
        if self._quiet >= SILENCE_FRAMES or len(self._frames) >= MAX_FRAMES:
            return self._collect()
        return None

    def flush(self) -> Utterance | None:
        """手动断句（前端"说完了"）：不等尾静音，攒到的立刻收。"""
        return self._collect() if self._frames else None

    def discard(self) -> None:
        """丢掉当前半句（本轮作废时用）。"""
        self._reset()

    def _collect(self) -> Utterance:
        quiet = min(self._quiet, len(self._frames))
        frames = self._frames[: len(self._frames) - quiet]
        utterance = Utterance(
            pcm=b"".join(frames),
            # 说话真正结束的时刻要扣掉尾静音，延迟口径才是"学生说完 → 开口"。
            ended_at=time.monotonic() - quiet * FRAME_SECONDS,
            seconds=len(frames) * FRAME_SECONDS,
            truncated=len(self._frames) >= MAX_FRAMES,
        )
        self._reset()
        return utterance

    def _reset(self) -> None:
        self._frames.clear()
        self._pre.clear()
        self._quiet = 0
        self.speaking = False


async def record_utterance(chunks: AsyncIterator[bytes]) -> Utterance:
    """从音频帧流里收一整句（测试与离线脚本用；服务侧直接逐帧喂 EnergyVAD）。"""
    vad = EnergyVAD()
    async for chunk in chunks:
        if (utterance := vad.feed(chunk)) is not None:
            return utterance
    raise RuntimeError("音频流已结束")
