"""会话日志：字段、轮次号、首字延迟每轮只记一次。"""

import json

from bandu.rules import Intent
from bandu.session_log import SessionLog


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_user_and_assistant_records(tmp_path):
    log = SessionLog("sess_test", directory=tmp_path)
    log.user_turn("直接告诉我答案", Intent.ASK_ANSWER, rule_triggered=True)
    log.assistant_turn("我们先不急着看答案")

    records = _records(log.path)
    assert [r["event"] for r in records] == ["user", "assistant"]
    assert records[0]["intent"] == "ask_answer"
    assert records[0]["rule_triggered"] is True
    assert records[0]["turn_id"] == 1
    assert all(r["session_id"] == "sess_test" and r["ts"] for r in records)


def test_first_audio_logged_once_per_turn(tmp_path):
    log = SessionLog("sess_test", directory=tmp_path)
    log.first_audio()  # 没有进行中的轮次 → 不落记录
    assert not log.path.exists()

    log.user_turn("这道题的第二个条件怎么用", Intent.NORMAL, rule_triggered=False)
    log.first_audio()
    log.first_audio()
    log.user_turn("那我下一步做什么", Intent.NORMAL, rule_triggered=False)
    log.first_audio()

    records = _records(log.path)
    assert [r["event"] for r in records] == ["user", "first_audio", "user", "first_audio"]
    assert [r["turn_id"] for r in records] == [1, 1, 2, 2]
    latencies = [r["first_audio_ms"] for r in records[1::2]]
    assert all(isinstance(ms, int) and ms >= 0 for ms in latencies)
