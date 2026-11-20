"""确定性教学规则：意图分类与预置引导语。"""

from bandu.rules import Intent, canned_reply, classify


def test_classify_ask_answer():
    assert classify("别绕了，直接告诉我答案") is Intent.ASK_ANSWER


def test_classify_off_topic():
    assert classify("我们聊聊游戏吧") is Intent.OFF_TOPIC


def test_classify_unclear():
    assert classify("嗯") is Intent.UNCLEAR
    assert classify("   ") is Intent.UNCLEAR


def test_classify_normal():
    assert classify("这道题的第二个条件怎么用") is Intent.NORMAL


def test_canned_reply_covers_hard_fallback_intents():
    assert canned_reply(Intent.ASK_ANSWER)
    assert canned_reply(Intent.OFF_TOPIC)
    assert canned_reply(Intent.NORMAL) is None
