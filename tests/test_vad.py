"""能量 VAD：底噪自适应阈值、尾静音收尾、手动断句、超长截断。"""

import array
import asyncio

import pytest
from support import SILENCE, SPEECH, frames, spoken_clip

from bandu.provider import FRAME_SAMPLES, FRAME_SECONDS
from bandu.vad import (
    MAX_FRAMES,
    NOISE_MARGIN,
    PRE_ROLL_FRAMES,
    SILENCE_FRAMES,
    SPEECH_RMS_MIN,
    EnergyVAD,
    record_utterance,
    rms,
    speech_threshold,
)


def loud(amplitude: int) -> bytes:
    """定幅 int16 帧：rms 恰好等于 amplitude，方便算阈值。"""
    return array.array("h", [amplitude] * FRAME_SAMPLES).tobytes()


def test_rms_matches_amplitude():
    assert rms(loud(1000)) == pytest.approx(1000)
    assert rms(SILENCE) == 0
    assert rms(b"") == 0


def test_rms_tolerates_odd_bytes():
    """网络分帧不保证 16bit 对齐，半截样本要能咽下去。"""
    assert rms(loud(500)[: FRAME_SAMPLES * 2 - 1]) == pytest.approx(500, rel=0.01)


def test_threshold_is_adaptive_with_absolute_floor():
    assert speech_threshold([]) == SPEECH_RMS_MIN
    assert speech_threshold([10.0] * 30) == SPEECH_RMS_MIN  # 安静房间：下限兜底
    assert speech_threshold([200.0] * 30) == pytest.approx(200 * NOISE_MARGIN)


def test_silence_never_finishes_utterance():
    vad = EnergyVAD()
    for _ in range(SILENCE_FRAMES * 3):
        assert vad.feed(SILENCE) is None
    assert vad.speaking is False
    assert vad.noise_floor == 0


def test_collects_one_utterance_and_trims_tail_silence():
    vad = EnergyVAD()
    speech, tail = 5, SILENCE_FRAMES
    for chunk in [*frames(SILENCE, 3), *frames(SPEECH, speech)]:
        assert vad.feed(chunk) is None
    assert vad.speaking is True

    utterance = None
    for _ in range(tail):
        utterance = vad.feed(SILENCE) or utterance

    assert utterance is not None
    # 开口前 PRE_ROLL_FRAMES 帧余音要带上，尾静音要剪掉
    assert len(utterance.pcm) == (PRE_ROLL_FRAMES + speech) * FRAME_SAMPLES * 2
    assert utterance.seconds == pytest.approx((PRE_ROLL_FRAMES + speech) * FRAME_SECONDS)
    assert utterance.truncated is False
    assert vad.speaking is False  # 收完即复位，能接着听下一句


def test_pause_shorter_than_tail_silence_does_not_cut_sentence():
    """句中停顿（< 800ms）不该把一句话切成两句。"""
    vad = EnergyVAD()
    for chunk in [*frames(SILENCE, 3), *frames(SPEECH, 4), *frames(SILENCE, SILENCE_FRAMES - 1)]:
        assert vad.feed(chunk) is None
    for chunk in [*frames(SPEECH, 3), *frames(SILENCE, SILENCE_FRAMES)]:
        utterance = vad.feed(chunk)
    assert utterance is not None
    assert utterance.seconds == pytest.approx(
        (PRE_ROLL_FRAMES + 4 + SILENCE_FRAMES - 1 + 3) * FRAME_SECONDS
    )


def test_max_frames_truncates_long_speech():
    vad = EnergyVAD()
    utterance = None
    for _ in range(MAX_FRAMES + 5):
        utterance = vad.feed(SPEECH) or utterance
        if utterance is not None:
            break
    assert utterance is not None
    assert utterance.truncated is True
    assert len(utterance.pcm) == MAX_FRAMES * FRAME_SAMPLES * 2


def test_flush_ends_utterance_without_waiting_silence():
    vad = EnergyVAD()
    for chunk in [*frames(SILENCE, 2), *frames(SPEECH, 3)]:
        vad.feed(chunk)
    utterance = vad.flush()
    assert utterance is not None
    assert len(utterance.pcm) == (PRE_ROLL_FRAMES + 3) * FRAME_SAMPLES * 2
    assert utterance.truncated is False


def test_flush_without_speech_is_none():
    vad = EnergyVAD()
    vad.feed(SILENCE)
    assert vad.flush() is None


def test_discard_drops_half_sentence():
    vad = EnergyVAD()
    for chunk in frames(SPEECH, 4):
        vad.feed(chunk)
    assert vad.speaking is True
    vad.discard()
    assert vad.speaking is False
    assert vad.flush() is None


def test_noise_estimate_survives_across_turns():
    """长连接复用同一个 VAD：第二轮不必重新估底噪。"""
    vad = EnergyVAD()
    for chunk in spoken_clip():
        vad.feed(chunk)
    first_threshold = vad.threshold
    for chunk in spoken_clip():
        vad.feed(chunk)
    assert vad.threshold == pytest.approx(first_threshold)


def test_record_utterance_over_frame_stream():
    async def stream():
        for chunk in spoken_clip():
            yield chunk

    utterance = asyncio.run(record_utterance(stream()))
    assert len(utterance.pcm) == (PRE_ROLL_FRAMES + 5) * FRAME_SAMPLES * 2
