"""轮次状态机：听 → 想 → 说 → 再听；非听相位丢帧；跳过、没听清、后端异常。"""

import asyncio
import json

import pytest
from support import (
    SILENCE,
    SPEECH,
    FakeDownstream,
    frames,
    make_session,
    run_session,
    spoken_clip,
)

from bandu.rules import Intent, canned_reply
from bandu.session import turn_timings
from bandu.state import TurnPhase


def test_ready_handshake_and_listening(tmp_path, stubs):
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, []))

    ready = downstream.one("ready")
    assert ready["session_id"] == "sess_test"
    assert ready["audio"] == {
        "input_sample_rate": 16_000,
        "output_sample_rate": 24_000,
        "frame_ms": 100,
    }
    assert downstream.all("state")[0]["state"] == "listening"
    assert session.state.phase is TurnPhase.LISTENING


def test_full_turn_event_sequence(tmp_path, stubs):
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, spoken_clip()))

    assert downstream.types == [
        "ready",
        "state",  # listening
        "speech_start",  # 检测到开口，附底噪/阈值
        "state",  # thinking
        "user_text",
        "state",  # speaking
        "assistant_delta",
        "assistant_delta",
        "turn_end",
        "state",  # listening
    ]
    assert downstream.one("speech_start")["threshold"] >= 100

    user = downstream.one("user_text")
    assert user["text"] == stubs.text
    assert user["intent"] == Intent.NORMAL.value
    assert user["rule_triggered"] is False

    assert [item["text"] for item in downstream.all("assistant_delta")] == stubs.sentences
    assert downstream.audio == [chunk for _ in stubs.sentences for chunk in stubs.audio]

    end = downstream.one("turn_end")
    assert end["turn_id"] == 1
    assert end["cancelled"] is False
    assert end["reply"] == "".join(stubs.sentences)
    assert set(end["timings"]) == {"asr_ms", "first_sentence_ms", "first_audio_ms"}
    assert session.state.turns == 1
    assert session.state.phase is TurnPhase.LISTENING


def test_turn_is_written_to_session_log(tmp_path, stubs):
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, spoken_clip()))

    records = [json.loads(line) for line in session.log.path.read_text("utf-8").splitlines()]
    assert [record["event"] for record in records] == ["user", "first_audio", "assistant"]
    assert records[0]["text"] == stubs.text
    assert records[-1]["text"] == "".join(stubs.sentences)
    assert all(record["session_id"] == "sess_test" for record in records)


def test_hard_rule_reply_skips_llm(tmp_path, stubs):
    stubs.text = "别绕了，直接告诉我答案"
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, spoken_clip()))

    user = downstream.one("user_text")
    assert user["intent"] == Intent.ASK_ANSWER.value
    assert user["rule_triggered"] is True
    assert stubs.llm_calls == []  # 命中硬规则不过 LLM
    assert stubs.tts_sentences == [canned_reply(Intent.ASK_ANSWER)]  # 预置话术逐字播报
    assert downstream.one("turn_end")["reply"] == canned_reply(Intent.ASK_ANSWER)


def test_off_topic_is_redirected(tmp_path, stubs):
    stubs.text = "我们聊聊游戏吧"
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, spoken_clip()))

    assert stubs.llm_calls == []
    assert stubs.tts_sentences == [canned_reply(Intent.OFF_TOPIC)]


def test_empty_transcript_reports_unheard(tmp_path, stubs):
    stubs.text = "   "
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, spoken_clip()))

    assert downstream.one("unheard")["seconds"] == pytest.approx(0.7)  # 2 帧余音 + 5 帧说话
    assert "assistant_delta" not in downstream.types
    assert downstream.audio == []
    assert session.state.phase is TurnPhase.LISTENING  # 空轮也要回到听


def test_upstream_audio_dropped_outside_listening(tmp_path, stubs):
    """轮次制的核心约束：伴读在想/在说时到达的音频一律丢弃，绝不开第二轮。"""

    async def scenario():
        gate = asyncio.Event()
        stubs.gate = gate  # 把这一轮卡在 ASR 上，好观察 thinking 相位
        downstream = FakeDownstream()
        session = make_session(downstream, tmp_path)
        messages = [*spoken_clip(), *frames(SPEECH, 5), {"type": "speech_end"}]

        task = asyncio.create_task(run_session(session, messages))
        await asyncio.sleep(0.05)
        assert session.state.phase is TurnPhase.THINKING
        assert session.dropped_frames == 5  # 第二句的 5 帧全丢
        gate.set()
        await task
        return session

    session = asyncio.run(scenario())
    assert stubs.asr_calls == 1  # 只识别了一句
    assert session.state.turns == 1


def test_manual_speech_end_flushes_partial_utterance(tmp_path, stubs):
    """前端「说完了」：不等 800ms 尾静音，立刻收句开轮。"""
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)
    messages = [*frames(SILENCE, 3), *frames(SPEECH, 4), {"type": "speech_end"}]

    asyncio.run(run_session(session, messages))

    assert downstream.one("user_text")["text"] == stubs.text
    assert stubs.asr_calls == 1
    assert downstream.one("speech_start")["threshold"] >= 100  # 开口事件也发了，只是不等尾静音


def test_speech_end_without_speech_reports_unheard(tmp_path, stubs):
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, [SILENCE, {"type": "speech_end"}]))

    assert downstream.one("unheard")["reason"] == "empty"
    assert stubs.asr_calls == 0
    assert session.state.phase is TurnPhase.LISTENING


def test_cancel_skips_current_turn(tmp_path, stubs):
    async def scenario():
        stubs.gate = asyncio.Event()  # 永远不放行：模拟「伴读还在想」时学生点跳过
        downstream = FakeDownstream()
        session = make_session(downstream, tmp_path)

        await run_session(session, [*spoken_clip(), {"type": "cancel"}])
        return session, downstream

    session, downstream = asyncio.run(scenario())

    end = downstream.one("turn_end")
    assert end["cancelled"] is True
    assert end["reply"] == ""
    assert downstream.audio == []
    assert session.state.phase is TurnPhase.LISTENING  # 跳过后立刻能接着说
    assert session.state.turns == 1


def test_backend_error_keeps_session_alive(tmp_path, monkeypatch):
    async def boom(pcm: bytes) -> str:
        raise RuntimeError("ASR 502")

    monkeypatch.setattr("bandu.session.transcribe_hedged", boom)
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, spoken_clip()))

    assert "ASR 502" in downstream.one("error")["message"]
    assert downstream.one("error")["fatal"] is False
    assert session.state.phase is TurnPhase.LISTENING


def test_unknown_control_message_is_reported_not_fatal(tmp_path, stubs):
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    asyncio.run(run_session(session, [{"type": "nope"}]))

    assert "nope" in downstream.one("error")["message"]
    assert session.state.phase is TurnPhase.LISTENING


def test_two_turns_accumulate_context(tmp_path, stubs):
    """轮次制下上下文靠客户端攒：第二轮之后 history 不再是空的。"""
    downstream = FakeDownstream()
    session = make_session(downstream, tmp_path)

    async def inbound():
        for message in [*spoken_clip(), *spoken_clip()]:
            yield message
            await session.wait_turn()

    asyncio.run(session.run(inbound()))

    assert session.state.turns == 2
    assert stubs.asr_calls == 2
    assert len(downstream.all("turn_end")) == 2


def test_turn_timings_zero_point_is_speech_end():
    marks = {"speech_end": 10.0, "asr": 10.7, "llm_first": 11.3, "tts_first": 11.8}
    assert turn_timings(marks) == {
        "asr_ms": 700,
        "first_sentence_ms": 1300,
        "first_audio_ms": 1800,
    }


def test_turn_timings_partial_marks():
    assert turn_timings({"speech_end": 0.0, "asr": 0.5}) == {
        "asr_ms": 500,
        "first_sentence_ms": 500,
    }
    assert turn_timings({"speech_end": 0.0}) == {}
