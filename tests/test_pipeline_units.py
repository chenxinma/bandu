"""管道零件：ASR 的 WAV 封装/响应解析、Agent 的切句、TTS 的帧协议。"""

import array
import asyncio
import io
import json
import wave

import pytest

from bandu.agent import cut_sentences
from bandu.asr import extract_text, transcribe_hedged, wav
from bandu.provider import FRAME_SAMPLES, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE


def test_wav_wraps_pcm_with_expected_header():
    pcm = array.array("h", [1, -2, 3] * 100).tobytes()
    data = wav(pcm)
    with wave.open(io.BytesIO(data)) as fh:
        assert fh.getnchannels() == 1
        assert fh.getsampwidth() == 2
        assert fh.getframerate() == INPUT_SAMPLE_RATE
        assert fh.getnframes() == len(pcm) // 2
        assert fh.readframes(fh.getnframes()) == pcm


def test_wav_accepts_explicit_sample_rate():
    with wave.open(io.BytesIO(wav(b"\x00\x00" * 10, sample_rate=OUTPUT_SAMPLE_RATE))) as fh:
        assert fh.getframerate() == OUTPUT_SAMPLE_RATE


def test_extract_text_covers_response_shapes():
    assert extract_text({"output": {"text": "你好"}}) == "你好"
    assert extract_text({"output": {"sentence": {"text": "你好"}}}) == "你好"
    assert extract_text({"text": "你好"}) == "你好"
    assert extract_text({"sentence": {"text": "你好"}}) == "你好"
    assert extract_text({"output": {}}) == ""
    assert extract_text({"output": {"sentence": None}}) == ""
    assert extract_text({"text": "   "}) == ""
    assert extract_text({}) == ""


def test_cut_sentences_splits_on_chinese_and_ascii_punctuation():
    pieces, rest = cut_sentences("先看条件。再想一步！还有呢？ok;done\n尾巴")
    assert pieces == ["先看条件。", "再想一步！", "还有呢？", "ok;", "done"]
    assert rest == "尾巴"


def test_cut_sentences_keeps_half_sentence_buffered():
    pieces, rest = cut_sentences("这道题")
    assert pieces == []
    assert rest == "这道题"


def test_cut_sentences_drops_empty_pieces():
    assert cut_sentences("。。。")[0] == ["。", "。", "。"]
    assert cut_sentences("") == ([], "")


def test_frame_contract_matches_frontend():
    """前端 worklet 按 1600 样本/帧发，服务端 VAD 也按同一帧长算时间。"""
    assert FRAME_SAMPLES == 1600
    assert len(array.array("h", [0] * FRAME_SAMPLES).tobytes()) == 3200
    assert json.dumps({"frame": FRAME_SAMPLES})  # 协议里只传采样率与帧长，不传音频元数据


def test_transcribe_hedged_returns_first_non_empty(monkeypatch):
    monkeypatch.setattr("bandu.asr.transcribe", lambda pcm: "先回来的")
    assert asyncio.run(transcribe_hedged(b"xx", fanout=3)) == "先回来的"


def test_transcribe_hedged_raises_when_every_copy_fails(monkeypatch):
    """几份全挂（Key 错 / 断网）要抛出来，别把配置问题伪装成"没听清"。"""

    def boom(pcm: bytes) -> str:
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr("bandu.asr.transcribe", boom)
    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(transcribe_hedged(b"xx", fanout=2))
